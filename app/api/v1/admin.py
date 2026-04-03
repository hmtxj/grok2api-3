from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
from typing import Optional
from pydantic import BaseModel
from app.core.auth import verify_api_key, verify_api_key_if_private, verify_app_key, get_admin_api_key
from app.core.config import config, get_config, is_public_mode
from app.core.batch_tasks import create_task, get_task, expire_task
from app.core.storage import get_storage, LocalStorage, RedisStorage, SQLStorage
from app.core.exceptions import AppException
from app.services.token.manager import get_token_manager
from app.services.grok.utils.batch import run_in_batches
import base64 as base64_module
import os
import time
import uuid
from pathlib import Path
import aiofiles
import asyncio
import orjson
from app.core.logger import logger
from app.core.storage import DATA_DIR
from app.api.v1.image import resolve_aspect_ratio
from app.services.grok.services.prompt_randomizer import randomize_prompt, get_available_wildcards, load_wildcard
from app.services.grok.services.voice import VoiceService
from app.services.grok.services.image import image_service
from app.services.grok.models.model import ModelService
from app.services.grok.processors.image_ws_processors import ImageWSCollectProcessor
from app.services.grok.processors.image_processors import ImageCollectProcessor
from app.services.grok.services.chat import GrokChatService
from app.services.grok.services.assets import UploadService
from app.services.token import EffortType, TokenStatus
from app.services.grok.services.image_meta import generate_task_id, record_images
from typing import List

# Imagine 图片本地缓存目录
_IMAGINE_CACHE_DIR = DATA_DIR / "tmp" / "image"


def _save_imagine_b64(img_b64: str, seq: int, run_id: str) -> str:
    """将 Imagine 的 base64 图片保存到本地缓存，返回文件名"""
    _IMAGINE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ext = "png" if img_b64.startswith("iVBOR") else "jpg"
    filename = f"imagine-{run_id}-{seq}.{ext}"
    filepath = _IMAGINE_CACHE_DIR / filename
    try:
        raw = base64_module.b64decode(img_b64)
        filepath.write_bytes(raw)
    except Exception as e:
        logger.warning(f"保存 Imagine 图片失败: {e}")
        return ""
    return filename

TEMPLATE_DIR = Path(__file__).parent.parent.parent / "static"


router = APIRouter()

IMAGINE_SESSION_TTL = 600
_IMAGINE_SESSIONS: dict[str, dict] = {}
_IMAGINE_SESSIONS_LOCK = asyncio.Lock()

# 全局冷却锁：所有 COOLING Token 同时刷新时暂停任务
_imagine_cooldown_until: float = 0.0
_imagine_refresh_lock: asyncio.Lock = asyncio.Lock()


async def _refresh_cooling_tokens(token_mgr):
    """只刷新 COOLING 状态的 Token（按需刷新，不刷全部）"""
    global _imagine_cooldown_until

    # 已有 Task 在刷新，其他 Task 等冷却解除即可
    if _imagine_refresh_lock.locked():
        return

    async with _imagine_refresh_lock:
        # 收集所有 COOLING 状态的 Token
        cooling_tokens = []
        for pool in token_mgr.pools.values():
            for token_info in pool.list():
                if token_info.status == TokenStatus.COOLING:
                    t = token_info.token
                    if t.startswith("sso="):
                        t = t[4:]
                    cooling_tokens.append(t)

        if not cooling_tokens:
            _imagine_cooldown_until = 0.0
            return

        total = len(cooling_tokens)
        logger.warning(f"Refreshing {total} COOLING tokens...")

        max_concurrent = int(get_config("performance.usage_max_concurrent", 25))
        batch_size = int(get_config("performance.usage_batch_size", 50))

        async def _refresh_one(t: str):
            return await token_mgr.sync_usage(
                t, "grok-3", consume_on_fail=False, is_usage=False
            )

        ok = 0
        fail = 0

        async def _on_item(item: str, res: dict):
            nonlocal ok, fail
            if res and res.get("ok"):
                ok += 1
            else:
                fail += 1

        await run_in_batches(
            cooling_tokens,
            _refresh_one,
            max_concurrent=max_concurrent,
            batch_size=batch_size,
            on_item=_on_item,
        )

        await token_mgr._save()
        logger.warning(f"Cooling refresh done: {ok} ok, {fail} fail out of {total}")

        # 刷新完毕，解除冷却
        _imagine_cooldown_until = 0.0


async def _cleanup_imagine_sessions(now: float) -> None:
    expired = [
        key
        for key, info in _IMAGINE_SESSIONS.items()
        if now - float(info.get("created_at") or 0) > IMAGINE_SESSION_TTL
    ]
    for key in expired:
        _IMAGINE_SESSIONS.pop(key, None)


async def _create_imagine_session(prompt: str, aspect_ratio: str, dynamic_random: bool = False) -> str:
    task_id = uuid.uuid4().hex
    now = time.time()
    async with _IMAGINE_SESSIONS_LOCK:
        await _cleanup_imagine_sessions(now)
        _IMAGINE_SESSIONS[task_id] = {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "dynamic_random": dynamic_random,
            "created_at": now,
        }
    return task_id


async def _get_imagine_session(task_id: str) -> Optional[dict]:
    if not task_id:
        return None
    now = time.time()
    async with _IMAGINE_SESSIONS_LOCK:
        await _cleanup_imagine_sessions(now)
        info = _IMAGINE_SESSIONS.get(task_id)
        if not info:
            return None
        created_at = float(info.get("created_at") or 0)
        if now - created_at > IMAGINE_SESSION_TTL:
            _IMAGINE_SESSIONS.pop(task_id, None)
            return None
        return dict(info)


async def _delete_imagine_session(task_id: str) -> None:
    if not task_id:
        return
    async with _IMAGINE_SESSIONS_LOCK:
        _IMAGINE_SESSIONS.pop(task_id, None)


async def _delete_imagine_sessions(task_ids: list[str]) -> int:
    if not task_ids:
        return 0
    removed = 0
    async with _IMAGINE_SESSIONS_LOCK:
        for task_id in task_ids:
            if task_id and task_id in _IMAGINE_SESSIONS:
                _IMAGINE_SESSIONS.pop(task_id, None)
                removed += 1
    return removed


def _collect_tokens(data: dict) -> list[str]:
    """从请求数据中收集 token 列表"""
    tokens = []
    if isinstance(data.get("token"), str) and data["token"].strip():
        tokens.append(data["token"].strip())
    if isinstance(data.get("tokens"), list):
        tokens.extend([str(t).strip() for t in data["tokens"] if str(t).strip()])
    return tokens


def _truncate_tokens(
    tokens: list[str], max_tokens: int, operation: str = "operation"
) -> tuple[list[str], bool, int]:
    """去重并截断 token 列表，返回 (unique_tokens, truncated, original_count)"""
    unique_tokens = list(dict.fromkeys(tokens))
    original_count = len(unique_tokens)
    truncated = False

    if len(unique_tokens) > max_tokens:
        unique_tokens = unique_tokens[:max_tokens]
        truncated = True
        logger.warning(
            f"{operation}: truncated from {original_count} to {max_tokens} tokens"
        )

    return unique_tokens, truncated, original_count


def _mask_token(token: str) -> str:
    """掩码 token 显示"""
    return f"{token[:8]}...{token[-8:]}" if len(token) > 20 else token


async def render_template(filename: str, **variables):
    """渲染指定模板，支持 {{KEY}} 变量替换"""
    template_path = TEMPLATE_DIR / filename
    if not template_path.exists():
        return HTMLResponse(f"Template {filename} not found.", status_code=404)

    async with aiofiles.open(template_path, "r", encoding="utf-8") as f:
        content = await f.read()

    # 注入默认模板变量
    site_mode = "public" if is_public_mode() else "private"
    auth_required = "false" if is_public_mode() else "true"
    defaults = {
        "SITE_MODE": site_mode,
        "AUTH_REQUIRED": auth_required,
    }
    defaults.update(variables)

    for key, value in defaults.items():
        content = content.replace("{{" + key + "}}", str(value))

    return HTMLResponse(content)


def _sse_event(payload: dict) -> str:
    return f"data: {orjson.dumps(payload).decode()}\n\n"


def _verify_stream_api_key(request: Request) -> None:
    if is_public_mode():
        return
    api_key = get_admin_api_key()
    if not api_key:
        return
    key = request.query_params.get("api_key")
    if key != api_key:
        raise HTTPException(status_code=401, detail="Invalid authentication token")


@router.get("/api/v1/admin/batch/{task_id}/stream")
async def stream_batch(task_id: str, request: Request):
    _verify_stream_api_key(request)
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    async def event_stream():
        queue = task.attach()
        try:
            yield _sse_event({"type": "snapshot", **task.snapshot()})

            final = task.final_event()
            if final:
                yield _sse_event(final)
                return

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    final = task.final_event()
                    if final:
                        yield _sse_event(final)
                        return
                    continue

                yield _sse_event(event)
                if event.get("type") in ("done", "error", "cancelled"):
                    return
        finally:
            task.detach(queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post(
    "/api/v1/admin/batch/{task_id}/cancel", dependencies=[Depends(verify_api_key)]
)
async def cancel_batch(task_id: str):
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    task.cancel()
    return {"status": "success"}


@router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def admin_login_page():
    """管理后台登录页"""
    return await render_template("login/login.html")


@router.get("/", include_in_schema=False)
async def root_redirect():
    if is_public_mode():
        return RedirectResponse(url="/imagine")
    return RedirectResponse(url="/admin")


@router.get("/admin/config", response_class=HTMLResponse, include_in_schema=False)
async def admin_config_page():
    """配置管理页"""
    return await render_template("config/config.html")


@router.get("/admin/token", response_class=HTMLResponse, include_in_schema=False)
async def admin_token_page():
    """Token 管理页"""
    return await render_template("token/token.html")


@router.get("/admin/voice", response_class=HTMLResponse, include_in_schema=False)
async def admin_voice_page():
    """私有模式直接渲染，公开模式重定向"""
    if is_public_mode():
        return RedirectResponse(url="/voice")
    return await render_template("voice/voice.html")


@router.get("/admin/imagine", response_class=HTMLResponse, include_in_schema=False)
async def admin_imagine_page():
    """私有模式直接渲染，公开模式重定向"""
    if is_public_mode():
        return RedirectResponse(url="/imagine")
    return await render_template("imagine/imagine.html")


@router.get("/admin/video", response_class=HTMLResponse, include_in_schema=False)
async def admin_video_page():
    """私有模式直接渲染，公开模式重定向"""
    if is_public_mode():
        return RedirectResponse(url="/video")
    return await render_template("video/video.html")


# === 公开路径：功能玩法页面 ===

@router.get("/imagine", response_class=HTMLResponse, include_in_schema=False)
async def public_imagine_page():
    return await render_template("imagine/imagine.html")


@router.get("/video", response_class=HTMLResponse, include_in_schema=False)
async def public_video_page():
    return await render_template("video/video.html")


@router.get("/voice", response_class=HTMLResponse, include_in_schema=False)
async def public_voice_page():
    return await render_template("voice/voice.html")


class VoiceTokenResponse(BaseModel):
    token: str
    url: str
    participant_name: str = ""
    room_name: str = ""


@router.get(
    "/api/v1/admin/voice/token",
    dependencies=[Depends(verify_api_key_if_private)],
    response_model=VoiceTokenResponse,
)
async def admin_voice_token(
    voice: str = "ara",
    personality: str = "assistant",
    speed: float = 1.0,
):
    """获取 Grok Voice Mode (LiveKit) Token"""
    token_mgr = await get_token_manager()
    sso_token = None
    for pool_name in ("ssoBasic", "ssoSuper"):
        sso_token = token_mgr.get_token(pool_name)
        if sso_token:
            break

    if not sso_token:
        raise AppException(
            "No available tokens for voice mode",
            code="no_token",
            status_code=503,
        )

    service = VoiceService()
    try:
        data = await service.get_token(
            token=sso_token,
            voice=voice,
            personality=personality,
            speed=speed,
        )
        token = data.get("token")
        if not token:
            raise AppException(
                "Upstream returned no voice token",
                code="upstream_error",
                status_code=502,
            )

        return VoiceTokenResponse(
            token=token,
            url="wss://livekit.grok.com",
            participant_name="",
            room_name="",
        )

    except Exception as e:
        if isinstance(e, AppException):
            raise
        raise AppException(
            f"Voice token error: {str(e)}",
            code="voice_error",
            status_code=500,
        )


async def _verify_imagine_ws_auth(websocket: WebSocket) -> tuple[bool, Optional[str]]:
    # 公开模式直接放行（需放在最前）
    if is_public_mode():
        task_id = websocket.query_params.get("task_id")
        return True, task_id if task_id else None

    task_id = websocket.query_params.get("task_id")
    if task_id:
        info = await _get_imagine_session(task_id)
        if info:
            return True, task_id

    api_key = get_admin_api_key()
    if not api_key:
        return True, None
    key = websocket.query_params.get("api_key")
    return key == api_key, None


@router.websocket("/api/v1/admin/imagine/ws")
async def admin_imagine_ws(websocket: WebSocket):
    ok, session_id = await _verify_imagine_ws_auth(websocket)
    if not ok:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    stop_event = asyncio.Event()
    run_task: Optional[asyncio.Task] = None

    async def _send(payload: dict) -> bool:
        try:
            await websocket.send_text(orjson.dumps(payload).decode())
            return True
        except Exception:
            return False

    async def _stop_run():
        nonlocal run_task
        stop_event.set()
        if run_task and not run_task.done():
            run_task.cancel()
            try:
                await run_task
            except Exception:
                pass
        run_task = None
        stop_event.clear()

    async def _run(prompt: str, aspect_ratio: str, dynamic_random: bool = False):
        model_id = "grok-imagine-1.0"
        model_info = ModelService.get(model_id)
        if not model_info or not model_info.is_image:
            await _send(
                {
                    "type": "error",
                    "message": "Image model is not available.",
                    "code": "model_not_supported",
                }
            )
            return

        token_mgr = await get_token_manager()
        enable_nsfw = bool(get_config("image.image_ws_nsfw", True))
        sequence = 0
        run_id = uuid.uuid4().hex

        await _send(
            {
                "type": "status",
                "status": "running",
                "prompt": prompt,
                "aspect_ratio": aspect_ratio,
                "run_id": run_id,
            }
        )

        while not stop_event.is_set():
            try:
                # 全局冷却检查：有 Task 在刷新 Token 时所有 Task 等待
                global _imagine_cooldown_until
                while _imagine_cooldown_until > time.time() and not stop_event.is_set():
                    await asyncio.sleep(1)

                await token_mgr.reload_if_stale()
                token = None
                for pool_name in ModelService.pool_candidates_for_model(
                    model_info.model_id
                ):
                    token = token_mgr.get_token(pool_name)
                    if token:
                        break

                if not token:
                    # 没有 ACTIVE Token → 设置冷却锁，刷新 COOLING Token
                    _imagine_cooldown_until = time.time() + 600
                    logger.warning("No active tokens → refreshing cooling tokens")
                    await _refresh_cooling_tokens(token_mgr)
                    # 刷新后仍无可用 Token 才通知前端
                    retry_token = None
                    for pn in ModelService.pool_candidates_for_model(model_info.model_id):
                        retry_token = token_mgr.get_token(pn)
                        if retry_token:
                            break
                    if not retry_token:
                        await _send(
                            {
                                "type": "error",
                                "message": "No available tokens. Please try again later.",
                                "code": "rate_limit_exceeded",
                            }
                        )
                        await asyncio.sleep(10)
                    continue

                # 动态随机：每轮生成不同的 prompt
                actual_prompt = randomize_prompt(prompt) if dynamic_random else prompt
                upstream = image_service.stream(
                    token=token,
                    prompt=actual_prompt,
                    aspect_ratio=aspect_ratio,
                    n=6,
                    enable_nsfw=enable_nsfw,
                )

                processor = ImageWSCollectProcessor(
                    model_info.model_id,
                    token,
                    n=6,
                    response_format="b64_json",
                )

                start_at = time.time()
                images = await processor.process(upstream)
                elapsed_ms = int((time.time() - start_at) * 1000)

                if images and all(img and img != "error" for img in images):
                    # 成功生图，重置全局冷却
                    _imagine_cooldown_until = 0.0

                    # 一次发送所有 6 张图片（跳过被打码检测过滤的空图片）
                    saved_filenames = []
                    for img_b64 in images:
                        if not img_b64:
                            continue
                        sequence += 1
                        await _send(
                            {
                                "type": "image",
                                "b64_json": img_b64,
                                "sequence": sequence,
                                "created_at": int(time.time() * 1000),
                                "elapsed_ms": elapsed_ms,
                                "aspect_ratio": aspect_ratio,
                                "run_id": run_id,
                                "prompt": actual_prompt,
                            }
                        )
                        # 保存到本地缓存
                        fname = _save_imagine_b64(img_b64, sequence, run_id)
                        if fname:
                            saved_filenames.append(fname)

                    # 记录元数据
                    if saved_filenames:
                        meta_task_id = f"imagine_{run_id}"
                        record_images(
                            task_id=meta_task_id,
                            prompt=prompt,
                            filenames=saved_filenames,
                            source="imagine",
                            aspect_ratio=aspect_ratio,
                        )

                    # 消耗 token（6 张图片按高成本计算）
                    try:
                        effort = (
                            EffortType.HIGH
                            if (model_info and model_info.cost.value == "high")
                            else EffortType.LOW
                        )
                        await token_mgr.consume(token, effort)
                    except Exception as e:
                        logger.warning(f"Failed to consume token: {e}")
                else:
                    # 空结果（401/限流）→ 标记当前 Token 为 COOLING，换下一个继续
                    await token_mgr.mark_rate_limited(token)
                    logger.warning(f"Imagine empty result → token {token[:16]}... marked COOLING")
                    continue

            except asyncio.CancelledError:
                break
            except Exception as e:
                err_msg = str(e)
                # rate_limit / 429 / 401 类错误 → 标记 COOLING，不弹窗
                if any(kw in err_msg.lower() for kw in ['rate_limit', '429', '401', 'rate limit']):
                    await token_mgr.mark_rate_limited(token)
                    logger.warning(f"Imagine rate limit → token {token[:16]}... marked COOLING")
                else:
                    # 只有非限流错误才通知前端
                    logger.warning(f"Imagine stream error: {err_msg[:100]}")
                    await _send(
                        {
                            "type": "error",
                            "message": err_msg,
                            "code": "internal_error",
                        }
                    )
                await asyncio.sleep(1.5)

        await _send({"type": "status", "status": "stopped", "run_id": run_id})

    async def _run_edit(prompt: str, aspect_ratio: str, image_urls: list, dynamic_random: bool = False, parent_post_id: str = None, upload_token: str = None):
        """图生图持续循环（借鉴 _run 架构）"""
        model_id = "grok-imagine-1.0-edit"
        model_info = ModelService.get(model_id)
        if not model_info:
            await _send({"type": "error", "message": "Edit model not available.", "code": "model_not_supported"})
            return

        token_mgr = await get_token_manager()
        sequence = 0
        run_id = uuid.uuid4().hex

        # 构建编辑 Payload 模板（与原版 /images/edits API 完全一致）
        enable_nsfw = bool(get_config("image.image_ws_nsfw", True))
        model_config_override = {
            "modelMap": {
                "imageEditModel": "imagine",
                "imageEditModelConfig": {
                    "imageReferences": image_urls,
                    "enable_nsfw": enable_nsfw,
                    "is_kids_mode": False,
                },
            }
        }

        # parentPostId 是让 Grok 正确关联参考图的关键字段
        if parent_post_id:
            model_config_override["modelMap"]["imageEditModelConfig"]["parentPostId"] = parent_post_id

        raw_payload_template = {
            "temporary": bool(get_config("chat.temporary")),
            "modelName": model_info.grok_model,
            "message": prompt,
            "enableImageGeneration": True,
            "returnImageBytes": False,
            "returnRawGrokInXaiRequest": False,
            "enableImageStreaming": True,
            "imageGenerationCount": 2,
            "forceConcise": False,
            "toolOverrides": {"imageGen": True},
            "enableSideBySide": True,
            "sendFinalMetadata": True,
            "isReasoning": False,
            "disableTextFollowUps": True,
            "responseMetadata": {"modelConfigOverride": model_config_override},
            "disableMemory": False,
            "forceSideBySide": False,
        }

        await _send({"type": "status", "status": "running", "prompt": prompt, "run_id": run_id})
        logger.info(f"[_run_edit] Starting: image_urls={image_urls}, parent_post_id={parent_post_id}")
        logger.info(f"[_run_edit] modelConfigOverride={model_config_override}")

        # 非动态随机模式下：预先解析一次通配符标签（结果固定不变）
        _edit_resolved_prompt = randomize_prompt(prompt)

        while not stop_event.is_set():
            try:
                # 全局冷却检查
                global _imagine_cooldown_until
                while _imagine_cooldown_until > time.time() and not stop_event.is_set():
                    await asyncio.sleep(1)

                await token_mgr.reload_if_stale()
                # 优先使用上传图片时的 token，保证能访问参考图
                if upload_token:
                    token = upload_token
                else:
                    token = None
                    for pool_name in ModelService.pool_candidates_for_model(model_info.model_id):
                        token = token_mgr.get_token(pool_name)
                        if token:
                            break

                if not token:
                    _imagine_cooldown_until = time.time() + 600
                    logger.warning("No active tokens for edit → refreshing cooling tokens")
                    await _refresh_cooling_tokens(token_mgr)
                    retry_token = None
                    for pn in ModelService.pool_candidates_for_model(model_info.model_id):
                        retry_token = token_mgr.get_token(pn)
                        if retry_token:
                            break
                    if retry_token:
                        token = retry_token
                    else:
                        await _send({"type": "error", "message": "No available tokens.", "code": "no_token"})
                        await asyncio.sleep(10)
                    continue

                # 图生图始终解析通配符标签（无论动态随机是否开启）
                # 动态随机开启时每轮重新随机化，关闭时使用固定解析结果
                resolved = randomize_prompt(prompt) if dynamic_random else _edit_resolved_prompt
                # 引导前缀：强制保持参考图人物脸部（最高权重）、发型、身材一致
                actual_prompt = (
                    "CRITICAL: You MUST preserve the EXACT same face from the reference image - "
                    "this is the highest priority. Also keep the same hairstyle and body shape. "
                    f"Only change the pose/outfit/scene as described: {resolved}"
                )
                logger.info(f"[_run_edit] actual_prompt={actual_prompt[:120]}")
                current_payload = {**raw_payload_template, "message": actual_prompt}
                chat_service = GrokChatService()
                response = await chat_service.chat(
                    token=token,
                    message=actual_prompt,
                    model=model_info.grok_model,
                    mode=None,
                    stream=True,
                    raw_payload=current_payload,
                )
                processor = ImageCollectProcessor(
                    model_info.model_id, token, response_format="b64_json",
                )

                start_at = time.time()
                images = await processor.process(response)
                elapsed_ms = int((time.time() - start_at) * 1000)

                if images and any(img and img != "error" for img in images):
                    _imagine_cooldown_until = 0.0
                    saved_filenames = []
                    for img_b64 in images:
                        if not img_b64 or img_b64 == "error":
                            continue
                        # 处理可能的 data URL 前缀
                        pure_b64 = img_b64
                        if img_b64.startswith("data:"):
                            pure_b64 = img_b64.split(",", 1)[1] if "," in img_b64 else img_b64
                        sequence += 1
                        await _send({
                            "type": "image",
                            "b64_json": pure_b64,
                            "sequence": sequence,
                            "created_at": int(time.time() * 1000),
                            "elapsed_ms": elapsed_ms,
                            "aspect_ratio": aspect_ratio,
                            "run_id": run_id,
                            "prompt": actual_prompt,
                        })
                        fname = _save_imagine_b64(pure_b64, sequence, run_id)
                        if fname:
                            saved_filenames.append(fname)

                    if saved_filenames:
                        record_images(
                            task_id=f"edit_{run_id}",
                            prompt=prompt,
                            filenames=saved_filenames,
                            source="imagine",
                            aspect_ratio=aspect_ratio,
                        )

                    try:
                        effort = EffortType.HIGH if (model_info and model_info.cost.value == "high") else EffortType.LOW
                        await token_mgr.consume(token, effort)
                    except Exception as e:
                        logger.warning(f"Failed to consume token: {e}")
                else:
                    await token_mgr.mark_rate_limited(token)
                    logger.warning(f"Edit empty result → token {token[:16]}... marked COOLING")
                    continue

            except asyncio.CancelledError:
                break
            except Exception as e:
                err_msg = str(e)
                if any(kw in err_msg.lower() for kw in ['rate_limit', '429', '401', 'rate limit']):
                    await token_mgr.mark_rate_limited(token)
                    logger.warning(f"Edit rate limit → token {token[:16]}... marked COOLING")
                else:
                    logger.warning(f"Edit stream error: {err_msg[:100]}")
                    await _send({"type": "error", "message": err_msg, "code": "internal_error"})
                await asyncio.sleep(1.5)

        await _send({"type": "status", "status": "stopped", "run_id": run_id})

    try:
        while True:
            try:
                raw = await websocket.receive_text()
            except (RuntimeError, WebSocketDisconnect):
                # WebSocket already closed or disconnected
                break
            
            try:
                payload = orjson.loads(raw)
            except Exception:
                await _send(
                    {
                        "type": "error",
                        "message": "Invalid message format.",
                        "code": "invalid_payload",
                    }
                )
                continue

            msg_type = payload.get("type")
            if msg_type == "start":
                prompt = str(payload.get("prompt") or "").strip()
                if not prompt:
                    await _send(
                        {
                            "type": "error",
                            "message": "Prompt cannot be empty.",
                            "code": "empty_prompt",
                        }
                    )
                    continue
                ratio = str(payload.get("aspect_ratio") or "2:3").strip()
                if not ratio:
                    ratio = "2:3"
                ratio = resolve_aspect_ratio(ratio)
                dynamic_random = bool(payload.get("dynamic_random", False))
                await _stop_run()
                stop_event.clear()
                run_task = asyncio.create_task(_run(prompt, ratio, dynamic_random))
            elif msg_type == "start_edit":
                prompt = str(payload.get("prompt") or "").strip()
                edit_image_urls = payload.get("image_urls") or []
                if not prompt:
                    await _send({"type": "error", "message": "Prompt cannot be empty.", "code": "empty_prompt"})
                    continue
                if not edit_image_urls:
                    await _send({"type": "error", "message": "Image URLs required.", "code": "missing_images"})
                    continue
                ratio = resolve_aspect_ratio(str(payload.get("aspect_ratio") or "1:1").strip())
                edit_parent_post_id = str(payload.get("parent_post_id") or "").strip() or None
                edit_upload_token = str(payload.get("upload_token") or "").strip() or None
                dynamic_random = bool(payload.get("dynamic_random", False))
                logger.info(f"[Imagine Edit] prompt={prompt[:50]}, image_urls={len(edit_image_urls)}, parent_post_id={edit_parent_post_id}, upload_token={'yes' if edit_upload_token else 'no'}, dynamic_random={dynamic_random}")
                await _stop_run()
                stop_event.clear()
                run_task = asyncio.create_task(_run_edit(prompt, ratio, edit_image_urls, dynamic_random, edit_parent_post_id, edit_upload_token))
            elif msg_type == "stop":
                await _stop_run()
            elif msg_type == "ping":
                await _send({"type": "pong"})
            else:
                await _send(
                    {
                        "type": "error",
                        "message": "Unknown command.",
                        "code": "unknown_command",
                    }
                )
    except WebSocketDisconnect:
        logger.debug("WebSocket disconnected by client")
    except Exception as e:
        logger.warning(f"WebSocket error: {e}")
    finally:
        await _stop_run()

        try:
            from starlette.websockets import WebSocketState
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.close(code=1000, reason="Server closing connection")
        except Exception as e:
            logger.debug(f"WebSocket close ignored: {e}")
        if session_id:
            await _delete_imagine_session(session_id)


class ImagineStartRequest(BaseModel):
    prompt: str
    aspect_ratio: Optional[str] = "2:3"
    dynamic_random: Optional[bool] = False


@router.post("/api/v1/admin/imagine/start", dependencies=[Depends(verify_api_key_if_private)])
async def admin_imagine_start(data: ImagineStartRequest):
    prompt = (data.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")
    ratio = resolve_aspect_ratio(str(data.aspect_ratio or "2:3").strip() or "2:3")
    task_id = await _create_imagine_session(prompt, ratio, bool(data.dynamic_random))
    return {"task_id": task_id, "aspect_ratio": ratio}


class ImagineStopRequest(BaseModel):
    task_ids: list[str]


@router.post("/api/v1/admin/imagine/stop", dependencies=[Depends(verify_api_key_if_private)])
async def admin_imagine_stop(data: ImagineStopRequest):
    removed = await _delete_imagine_sessions(data.task_ids or [])
    return {"status": "success", "removed": removed}


@router.get("/api/v1/admin/imagine/sse")
async def admin_imagine_sse(
    request: Request,
    task_id: str = Query(""),
    prompt: str = Query(""),
    aspect_ratio: str = Query("2:3"),
):
    """Imagine 图片瀑布流（SSE 兜底）"""
    session = None
    if task_id:
        session = await _get_imagine_session(task_id)
        if not session:
            raise HTTPException(status_code=404, detail="Task not found")
    else:
        _verify_stream_api_key(request)

    dynamic_random = False
    if session:
        prompt = str(session.get("prompt") or "").strip()
        ratio = str(session.get("aspect_ratio") or "2:3").strip() or "2:3"
        dynamic_random = bool(session.get("dynamic_random", False))
    else:
        prompt = (prompt or "").strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="Prompt cannot be empty")
        ratio = str(aspect_ratio or "2:3").strip() or "2:3"
        ratio = resolve_aspect_ratio(ratio)

    async def event_stream():
        try:
            model_id = "grok-imagine-1.0"
            model_info = ModelService.get(model_id)
            if not model_info or not model_info.is_image:
                yield _sse_event(
                    {
                        "type": "error",
                        "message": "Image model is not available.",
                        "code": "model_not_supported",
                    }
                )
                return

            token_mgr = await get_token_manager()
            enable_nsfw = bool(get_config("image.image_ws_nsfw", True))
            sequence = 0
            run_id = uuid.uuid4().hex

            yield _sse_event(
                {
                    "type": "status",
                    "status": "running",
                    "prompt": prompt,
                    "aspect_ratio": ratio,
                    "run_id": run_id,
                }
            )

            while True:
                if await request.is_disconnected():
                    break
                if task_id:
                    session_alive = await _get_imagine_session(task_id)
                    if not session_alive:
                        break

                try:
                    await token_mgr.reload_if_stale()
                    token = None
                    for pool_name in ModelService.pool_candidates_for_model(
                        model_info.model_id
                    ):
                        token = token_mgr.get_token(pool_name)
                        if token:
                            break

                    if not token:
                        yield _sse_event(
                            {
                                "type": "error",
                                "message": "No available tokens. Please try again later.",
                                "code": "rate_limit_exceeded",
                            }
                        )
                        await asyncio.sleep(2)
                        continue

                    # 动态随机：每轮生成不同的 prompt
                    actual_prompt = randomize_prompt(prompt) if dynamic_random else prompt
                    upstream = image_service.stream(
                        token=token,
                        prompt=actual_prompt,
                        aspect_ratio=ratio,
                        n=6,
                        enable_nsfw=enable_nsfw,
                    )

                    processor = ImageWSCollectProcessor(
                        model_info.model_id,
                        token,
                        n=6,
                        response_format="b64_json",
                    )

                    start_at = time.time()
                    images = await processor.process(upstream)
                    elapsed_ms = int((time.time() - start_at) * 1000)

                    if images and all(img and img != "error" for img in images):
                        saved_filenames = []
                        for img_b64 in images:
                            if not img_b64:
                                continue
                            sequence += 1
                            yield _sse_event(
                                {
                                    "type": "image",
                                    "b64_json": img_b64,
                                    "sequence": sequence,
                                    "created_at": int(time.time() * 1000),
                                    "elapsed_ms": elapsed_ms,
                                    "aspect_ratio": ratio,
                                    "run_id": run_id,
                                    "prompt": actual_prompt,
                                }
                            )
                            # 保存到本地缓存
                            fname = _save_imagine_b64(img_b64, sequence, run_id)
                            if fname:
                                saved_filenames.append(fname)

                        # 记录元数据
                        if saved_filenames:
                            meta_task_id = f"imagine_{run_id}"
                            record_images(
                                task_id=meta_task_id,
                                prompt=prompt,
                                filenames=saved_filenames,
                                source="imagine",
                                aspect_ratio=ratio,
                            )

                        try:
                            effort = (
                                EffortType.HIGH
                                if (model_info and model_info.cost.value == "high")
                                else EffortType.LOW
                            )
                            await token_mgr.consume(token, effort)
                        except Exception as e:
                            logger.warning(f"Failed to consume token: {e}")
                    else:
                        yield _sse_event(
                            {
                                "type": "error",
                                "message": "Image generation returned empty data.",
                                "code": "empty_image",
                            }
                        )
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.warning(f"Imagine SSE error: {e}")
                    yield _sse_event(
                        {"type": "error", "message": str(e), "code": "internal_error"}
                    )
                    await asyncio.sleep(1.5)

            yield _sse_event({"type": "status", "status": "stopped", "run_id": run_id})
        finally:
            if task_id:
                await _delete_imagine_session(task_id)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@router.get("/api/v1/admin/imagine/wildcards", dependencies=[Depends(verify_api_key_if_private)])
async def get_wildcards_api():
    """获取可用的通配符词库列表"""
    return {"wildcards": get_available_wildcards()}


@router.get("/api/v1/admin/imagine/wildcards/{name}", dependencies=[Depends(verify_api_key_if_private)])
async def get_wildcard_items_api(name: str):
    """获取指定词库的全部内容"""
    items = load_wildcard(name)
    if items is None:
        raise HTTPException(status_code=404, detail=f"词库 '{name}' 不存在")
    return {"name": name, "items": items}


@router.post("/api/v1/admin/login", dependencies=[Depends(verify_app_key)])
async def admin_login_api():
    """管理后台登录验证（使用 app_key）"""
    return {"status": "success", "api_key": get_admin_api_key()}


@router.get("/api/v1/admin/config", dependencies=[Depends(verify_api_key)])
async def get_config_api():
    """获取当前配置"""
    # 暴露原始配置字典
    return config._config


@router.post("/api/v1/admin/config", dependencies=[Depends(verify_api_key)])
async def update_config_api(data: dict):
    """更新配置"""
    try:
        await config.update(data)
        return {"status": "success", "message": "配置已更新"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/v1/admin/storage", dependencies=[Depends(verify_api_key)])
async def get_storage_info():
    """获取当前存储模式"""
    storage_type = os.getenv("SERVER_STORAGE_TYPE", "").lower()
    if not storage_type:
        storage_type = str(get_config("storage.type")).lower()
    if not storage_type:
        storage = get_storage()
        if isinstance(storage, LocalStorage):
            storage_type = "local"
        elif isinstance(storage, RedisStorage):
            storage_type = "redis"
        elif isinstance(storage, SQLStorage):
            storage_type = {
                "mysql": "mysql",
                "mariadb": "mysql",
                "postgres": "pgsql",
                "postgresql": "pgsql",
                "pgsql": "pgsql",
            }.get(storage.dialect, storage.dialect)
    return {"type": storage_type or "local"}


@router.get("/api/v1/admin/tokens", dependencies=[Depends(verify_api_key)])
async def get_tokens_api():
    """获取所有 Token"""
    storage = get_storage()
    tokens = await storage.load_tokens()
    return tokens or {}


@router.post("/api/v1/admin/tokens", dependencies=[Depends(verify_api_key)])
async def update_tokens_api(data: dict):
    """更新 Token 信息"""
    storage = get_storage()
    try:
        from app.services.token.manager import get_token_manager
        from app.services.token.models import TokenInfo

        async with storage.acquire_lock("tokens_save", timeout=10):
            existing = await storage.load_tokens() or {}
            normalized = {}
            allowed_fields = set(TokenInfo.model_fields.keys())
            existing_map = {}
            for pool_name, tokens in existing.items():
                if not isinstance(tokens, list):
                    continue
                pool_map = {}
                for item in tokens:
                    if isinstance(item, str):
                        token_data = {"token": item}
                    elif isinstance(item, dict):
                        token_data = dict(item)
                    else:
                        continue
                    raw_token = token_data.get("token")
                    if isinstance(raw_token, str) and raw_token.startswith("sso="):
                        token_data["token"] = raw_token[4:]
                    token_key = token_data.get("token")
                    if isinstance(token_key, str):
                        pool_map[token_key] = token_data
                existing_map[pool_name] = pool_map
            for pool_name, tokens in (data or {}).items():
                if not isinstance(tokens, list):
                    continue
                pool_list = []
                for item in tokens:
                    if isinstance(item, str):
                        token_data = {"token": item}
                    elif isinstance(item, dict):
                        token_data = dict(item)
                    else:
                        continue

                    raw_token = token_data.get("token")
                    if isinstance(raw_token, str) and raw_token.startswith("sso="):
                        token_data["token"] = raw_token[4:]

                    base = existing_map.get(pool_name, {}).get(
                        token_data.get("token"), {}
                    )
                    merged = dict(base)
                    merged.update(token_data)
                    if merged.get("tags") is None:
                        merged["tags"] = []

                    filtered = {k: v for k, v in merged.items() if k in allowed_fields}
                    try:
                        info = TokenInfo(**filtered)
                        pool_list.append(info.model_dump())
                    except Exception as e:
                        logger.warning(f"Skip invalid token in pool '{pool_name}': {e}")
                        continue
                normalized[pool_name] = pool_list

            await storage.save_tokens(normalized)
            mgr = await get_token_manager()
            await mgr.reload()
        return {"status": "success", "message": "Token 已更新"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/v1/admin/tokens/refresh", dependencies=[Depends(verify_api_key)])
async def refresh_tokens_api(data: dict):
    """刷新 Token 状态"""
    try:
        mgr = await get_token_manager()
        tokens = _collect_tokens(data)

        if not tokens:
            raise HTTPException(status_code=400, detail="No tokens provided")

        # 去重并截断
        max_tokens = int(get_config("performance.usage_max_tokens"))
        unique_tokens, truncated, original_count = _truncate_tokens(
            tokens, max_tokens, "Usage refresh"
        )

        # 批量执行配置
        max_concurrent = get_config("performance.usage_max_concurrent")
        batch_size = get_config("performance.usage_batch_size")

        async def _refresh_one(t):
            return await mgr.sync_usage(
                t, "grok-3", consume_on_fail=False, is_usage=False
            )

        raw_results = await run_in_batches(
            unique_tokens,
            _refresh_one,
            max_concurrent=max_concurrent,
            batch_size=batch_size,
        )

        results = {}
        for token, res in raw_results.items():
            if res.get("ok"):
                results[token] = res.get("data", False)
            else:
                results[token] = False

        response = {"status": "success", "results": results}
        if truncated:
            response["warning"] = (
                f"数量超出限制，仅处理前 {max_tokens} 个（共 {original_count} 个）"
            )
        return response
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/api/v1/admin/tokens/refresh/async", dependencies=[Depends(verify_api_key)]
)
async def refresh_tokens_api_async(data: dict):
    """刷新 Token 状态（异步批量 + SSE 进度）"""
    mgr = await get_token_manager()
    tokens = _collect_tokens(data)

    if not tokens:
        raise HTTPException(status_code=400, detail="No tokens provided")

    # 去重并截断
    max_tokens = int(get_config("performance.usage_max_tokens"))
    unique_tokens, truncated, original_count = _truncate_tokens(
        tokens, max_tokens, "Usage refresh"
    )

    max_concurrent = get_config("performance.usage_max_concurrent")
    batch_size = get_config("performance.usage_batch_size")

    task = create_task(len(unique_tokens))

    async def _run():
        try:

            async def _refresh_one(t: str):
                return await mgr.sync_usage(
                    t, "grok-3", consume_on_fail=False, is_usage=False
                )

            async def _on_item(item: str, res: dict):
                task.record(bool(res.get("ok")))

            raw_results = await run_in_batches(
                unique_tokens,
                _refresh_one,
                max_concurrent=max_concurrent,
                batch_size=batch_size,
                on_item=_on_item,
                should_cancel=lambda: task.cancelled,
            )

            if task.cancelled:
                task.finish_cancelled()
                return

            results: dict[str, bool] = {}
            ok_count = 0
            fail_count = 0
            for token, res in raw_results.items():
                if res.get("ok") and res.get("data") is True:
                    ok_count += 1
                    results[token] = True
                else:
                    fail_count += 1
                    results[token] = False

            await mgr._save()

            result = {
                "status": "success",
                "summary": {
                    "total": len(unique_tokens),
                    "ok": ok_count,
                    "fail": fail_count,
                },
                "results": results,
            }
            warning = None
            if truncated:
                warning = (
                    f"数量超出限制，仅处理前 {max_tokens} 个（共 {original_count} 个）"
                )
            task.finish(result, warning=warning)
        except Exception as e:
            task.fail_task(str(e))
        finally:
            asyncio.create_task(expire_task(task.id, 300))

    asyncio.create_task(_run())

    return {
        "status": "success",
        "task_id": task.id,
        "total": len(unique_tokens),
    }


@router.post("/api/v1/admin/tokens/nsfw/enable", dependencies=[Depends(verify_api_key)])
async def enable_nsfw_api(data: dict):
    """批量开启 NSFW (Unhinged) 模式"""
    from app.services.grok.services.nsfw import NSFWService

    try:
        mgr = await get_token_manager()
        nsfw_service = NSFWService()

        # 收集 token 列表
        tokens = _collect_tokens(data)

        # 若未指定，则使用所有 pool 中的 token
        if not tokens:
            for pool_name, pool in mgr.pools.items():
                for info in pool.list():
                    raw = (
                        info.token[4:] if info.token.startswith("sso=") else info.token
                    )
                    tokens.append(raw)

        if not tokens:
            raise HTTPException(status_code=400, detail="No tokens available")

        # 去重并截断
        max_tokens = int(get_config("performance.nsfw_max_tokens"))
        unique_tokens, truncated, original_count = _truncate_tokens(
            tokens, max_tokens, "NSFW enable"
        )

        # 批量执行配置
        max_concurrent = get_config("performance.nsfw_max_concurrent")
        batch_size = get_config("performance.nsfw_batch_size")

        # 初始化共享连接池
        await nsfw_service.open()

        # 定义 worker
        async def _enable(token: str):
            result = await nsfw_service.enable(token)
            # 成功后添加 nsfw tag
            if result.success:
                await mgr.add_tag(token, "nsfw")
            return {
                "success": result.success,
                "http_status": result.http_status,
                "grpc_status": result.grpc_status,
                "grpc_message": result.grpc_message,
                "error": result.error,
            }

        try:
            # 执行批量操作
            raw_results = await run_in_batches(
                unique_tokens, _enable, max_concurrent=max_concurrent, batch_size=batch_size
            )
        finally:
            await nsfw_service.close()

        # 构造返回结果（mask token）
        results = {}
        ok_count = 0
        fail_count = 0

        for token, res in raw_results.items():
            masked = _mask_token(token)
            if res.get("ok") and res.get("data", {}).get("success"):
                ok_count += 1
                results[masked] = res.get("data", {})
            else:
                fail_count += 1
                results[masked] = res.get("data") or {"error": res.get("error")}

        response = {
            "status": "success",
            "summary": {
                "total": len(unique_tokens),
                "ok": ok_count,
                "fail": fail_count,
            },
            "results": results,
        }

        # 添加截断提示
        if truncated:
            response["warning"] = (
                f"数量超出限制，仅处理前 {max_tokens} 个（共 {original_count} 个）"
            )

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Enable NSFW failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/api/v1/admin/tokens/nsfw/enable/async", dependencies=[Depends(verify_api_key)]
)
async def enable_nsfw_api_async(data: dict):
    """批量开启 NSFW (Unhinged) 模式（异步批量 + SSE 进度）"""
    from app.services.grok.services.nsfw import NSFWService

    mgr = await get_token_manager()
    nsfw_service = NSFWService()

    tokens = _collect_tokens(data)

    if not tokens:
        for pool_name, pool in mgr.pools.items():
            for info in pool.list():
                raw = info.token[4:] if info.token.startswith("sso=") else info.token
                tokens.append(raw)

    if not tokens:
        raise HTTPException(status_code=400, detail="No tokens available")

    # 去重并截断
    max_tokens = int(get_config("performance.nsfw_max_tokens"))
    unique_tokens, truncated, original_count = _truncate_tokens(
        tokens, max_tokens, "NSFW enable"
    )

    max_concurrent = get_config("performance.nsfw_max_concurrent")
    batch_size = get_config("performance.nsfw_batch_size")

    task = create_task(len(unique_tokens))

    async def _run():
        try:
            # 初始化共享连接池
            await nsfw_service.open()

            async def _enable(token: str):
                result = await nsfw_service.enable(token)
                if result.success:
                    await mgr.add_tag(token, "nsfw")
                return {
                    "success": result.success,
                    "http_status": result.http_status,
                    "grpc_status": result.grpc_status,
                    "grpc_message": result.grpc_message,
                    "error": result.error,
                }

            async def _on_item(item: str, res: dict):
                ok = bool(res.get("ok") and res.get("data", {}).get("success"))
                task.record(ok)

            try:
                raw_results = await run_in_batches(
                    unique_tokens,
                    _enable,
                    max_concurrent=max_concurrent,
                    batch_size=batch_size,
                    on_item=_on_item,
                    should_cancel=lambda: task.cancelled,
                )
            finally:
                await nsfw_service.close()

            if task.cancelled:
                task.finish_cancelled()
                return

            results = {}
            ok_count = 0
            fail_count = 0
            for token, res in raw_results.items():
                masked = f"{token[:8]}...{token[-8:]}" if len(token) > 20 else token
                if res.get("ok") and res.get("data", {}).get("success"):
                    ok_count += 1
                    results[masked] = res.get("data", {})
                else:
                    fail_count += 1
                    results[masked] = res.get("data") or {"error": res.get("error")}

            await mgr._save()

            result = {
                "status": "success",
                "summary": {
                    "total": len(unique_tokens),
                    "ok": ok_count,
                    "fail": fail_count,
                },
                "results": results,
            }
            warning = None
            if truncated:
                warning = (
                    f"数量超出限制，仅处理前 {max_tokens} 个（共 {original_count} 个）"
                )
            task.finish(result, warning=warning)
        except Exception as e:
            task.fail_task(str(e))
        finally:
            asyncio.create_task(expire_task(task.id, 300))

    asyncio.create_task(_run())

    return {
        "status": "success",
        "task_id": task.id,
        "total": len(unique_tokens),
    }


@router.get("/admin/cache", response_class=HTMLResponse, include_in_schema=False)
async def admin_cache_page():
    """缓存管理页"""
    return await render_template("cache/cache.html")


@router.get("/api/v1/admin/cache", dependencies=[Depends(verify_api_key)])
async def get_cache_stats_api(request: Request):
    """获取缓存统计"""
    from app.services.grok.services.assets import DownloadService, ListService
    from app.services.token.manager import get_token_manager
    from app.services.grok.utils.batch import run_in_batches

    try:
        dl_service = DownloadService()
        image_stats = dl_service.get_stats("image")
        video_stats = dl_service.get_stats("video")

        mgr = await get_token_manager()
        pools = mgr.pools
        accounts = []
        for pool_name, pool in pools.items():
            for info in pool.list():
                raw_token = (
                    info.token[4:] if info.token.startswith("sso=") else info.token
                )
                masked = (
                    f"{raw_token[:8]}...{raw_token[-16:]}"
                    if len(raw_token) > 24
                    else raw_token
                )
                accounts.append(
                    {
                        "token": raw_token,
                        "token_masked": masked,
                        "pool": pool_name,
                        "status": info.status,
                        "last_asset_clear_at": info.last_asset_clear_at,
                    }
                )

        scope = request.query_params.get("scope")
        selected_token = request.query_params.get("token")
        tokens_param = request.query_params.get("tokens")
        selected_tokens = []
        if tokens_param:
            selected_tokens = [t.strip() for t in tokens_param.split(",") if t.strip()]

        online_stats = {
            "count": 0,
            "status": "unknown",
            "token": None,
            "last_asset_clear_at": None,
        }
        online_details = []
        account_map = {a["token"]: a for a in accounts}
        max_concurrent = max(1, int(get_config("performance.assets_max_concurrent")))
        batch_size = max(1, int(get_config("performance.assets_batch_size")))
        max_tokens = int(get_config("performance.assets_max_tokens"))

        truncated = False
        original_count = 0

        async def _fetch_assets(token: str):
            list_service = ListService()
            try:
                return await list_service.count(token)
            finally:
                await list_service.close()

        async def _fetch_detail(token: str):
            account = account_map.get(token)
            try:
                count = await _fetch_assets(token)
                return {
                    "detail": {
                        "token": token,
                        "token_masked": account["token_masked"] if account else token,
                        "count": count,
                        "status": "ok",
                        "last_asset_clear_at": account["last_asset_clear_at"]
                        if account
                        else None,
                    },
                    "count": count,
                }
            except Exception as e:
                return {
                    "detail": {
                        "token": token,
                        "token_masked": account["token_masked"] if account else token,
                        "count": 0,
                        "status": f"error: {str(e)}",
                        "last_asset_clear_at": account["last_asset_clear_at"]
                        if account
                        else None,
                    },
                    "count": 0,
                }

        if selected_tokens:
            selected_tokens, truncated, original_count = _truncate_tokens(
                selected_tokens, max_tokens, "Assets fetch"
            )
            total = 0
            raw_results = await run_in_batches(
                selected_tokens,
                _fetch_detail,
                max_concurrent=max_concurrent,
                batch_size=batch_size,
            )
            for token, res in raw_results.items():
                if res.get("ok"):
                    data = res.get("data", {})
                    detail = data.get("detail")
                    total += data.get("count", 0)
                else:
                    account = account_map.get(token)
                    detail = {
                        "token": token,
                        "token_masked": account["token_masked"] if account else token,
                        "count": 0,
                        "status": f"error: {res.get('error')}",
                        "last_asset_clear_at": account["last_asset_clear_at"]
                        if account
                        else None,
                    }
                if detail:
                    online_details.append(detail)
            online_stats = {
                "count": total,
                "status": "ok" if selected_tokens else "no_token",
                "token": None,
                "last_asset_clear_at": None,
            }
            scope = "selected"
        elif scope == "all":
            total = 0
            tokens = list(dict.fromkeys([account["token"] for account in accounts]))
            original_count = len(tokens)
            if len(tokens) > max_tokens:
                tokens = tokens[:max_tokens]
                truncated = True
            raw_results = await run_in_batches(
                tokens,
                _fetch_detail,
                max_concurrent=max_concurrent,
                batch_size=batch_size,
            )
            for token, res in raw_results.items():
                if res.get("ok"):
                    data = res.get("data", {})
                    detail = data.get("detail")
                    total += data.get("count", 0)
                else:
                    account = account_map.get(token)
                    detail = {
                        "token": token,
                        "token_masked": account["token_masked"] if account else token,
                        "count": 0,
                        "status": f"error: {res.get('error')}",
                        "last_asset_clear_at": account["last_asset_clear_at"]
                        if account
                        else None,
                    }
                if detail:
                    online_details.append(detail)
            online_stats = {
                "count": total,
                "status": "ok" if accounts else "no_token",
                "token": None,
                "last_asset_clear_at": None,
            }
        else:
            token = selected_token
            if token:
                try:
                    count = await _fetch_assets(token)
                    match = next((a for a in accounts if a["token"] == token), None)
                    online_stats = {
                        "count": count,
                        "status": "ok",
                        "token": token,
                        "token_masked": match["token_masked"] if match else token,
                        "last_asset_clear_at": match["last_asset_clear_at"]
                        if match
                        else None,
                    }
                except Exception as e:
                    match = next((a for a in accounts if a["token"] == token), None)
                    online_stats = {
                        "count": 0,
                        "status": f"error: {str(e)}",
                        "token": token,
                        "token_masked": match["token_masked"] if match else token,
                        "last_asset_clear_at": match["last_asset_clear_at"]
                        if match
                        else None,
                    }
            else:
                online_stats = {
                    "count": 0,
                    "status": "not_loaded",
                    "token": None,
                    "last_asset_clear_at": None,
                }

        response = {
            "local_image": image_stats,
            "local_video": video_stats,
            "online": online_stats,
            "online_accounts": accounts,
            "online_scope": scope or "none",
            "online_details": online_details,
        }
        if truncated:
            response["warning"] = (
                f"数量超出限制，仅处理前 {max_tokens} 个（共 {original_count} 个）"
            )
        return response
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/api/v1/admin/cache/online/load/async", dependencies=[Depends(verify_api_key)]
)
async def load_online_cache_api_async(data: dict):
    """在线资产统计（异步批量 + SSE 进度）"""
    from app.services.grok.services.assets import DownloadService, ListService
    from app.services.token.manager import get_token_manager
    from app.services.grok.utils.batch import run_in_batches

    mgr = await get_token_manager()

    # 账号列表
    accounts = []
    for pool_name, pool in mgr.pools.items():
        for info in pool.list():
            raw_token = info.token[4:] if info.token.startswith("sso=") else info.token
            masked = (
                f"{raw_token[:8]}...{raw_token[-16:]}"
                if len(raw_token) > 24
                else raw_token
            )
            accounts.append(
                {
                    "token": raw_token,
                    "token_masked": masked,
                    "pool": pool_name,
                    "status": info.status,
                    "last_asset_clear_at": info.last_asset_clear_at,
                }
            )

    account_map = {a["token"]: a for a in accounts}

    tokens = data.get("tokens")
    scope = data.get("scope")
    selected_tokens: list[str] = []
    if isinstance(tokens, list):
        selected_tokens = [str(t).strip() for t in tokens if str(t).strip()]

    if not selected_tokens and scope == "all":
        selected_tokens = [account["token"] for account in accounts]
        scope = "all"
    elif selected_tokens:
        scope = "selected"
    else:
        raise HTTPException(status_code=400, detail="No tokens provided")

    max_tokens = int(get_config("performance.assets_max_tokens"))
    selected_tokens, truncated, original_count = _truncate_tokens(
        selected_tokens, max_tokens, "Assets load"
    )

    max_concurrent = get_config("performance.assets_max_concurrent")
    batch_size = get_config("performance.assets_batch_size")

    task = create_task(len(selected_tokens))

    async def _run():
        try:
            dl_service = DownloadService()
            image_stats = dl_service.get_stats("image")
            video_stats = dl_service.get_stats("video")

            async def _fetch_detail(token: str):
                account = account_map.get(token)
                list_service = ListService()
                try:
                    count = await list_service.count(token)
                    detail = {
                        "token": token,
                        "token_masked": account["token_masked"] if account else token,
                        "count": count,
                        "status": "ok",
                        "last_asset_clear_at": account["last_asset_clear_at"]
                        if account
                        else None,
                    }
                    return {"ok": True, "detail": detail, "count": count}
                except Exception as e:
                    detail = {
                        "token": token,
                        "token_masked": account["token_masked"] if account else token,
                        "count": 0,
                        "status": f"error: {str(e)}",
                        "last_asset_clear_at": account["last_asset_clear_at"]
                        if account
                        else None,
                    }
                    return {"ok": False, "detail": detail, "count": 0}
                finally:
                    await list_service.close()

            async def _on_item(item: str, res: dict):
                ok = bool(res.get("data", {}).get("ok"))
                task.record(ok)

            raw_results = await run_in_batches(
                selected_tokens,
                _fetch_detail,
                max_concurrent=max_concurrent,
                batch_size=batch_size,
                on_item=_on_item,
                should_cancel=lambda: task.cancelled,
            )

            if task.cancelled:
                task.finish_cancelled()
                return

            online_details = []
            total = 0
            for token, res in raw_results.items():
                data = res.get("data", {})
                detail = data.get("detail")
                if detail:
                    online_details.append(detail)
                total += data.get("count", 0)

            online_stats = {
                "count": total,
                "status": "ok" if selected_tokens else "no_token",
                "token": None,
                "last_asset_clear_at": None,
            }

            result = {
                "local_image": image_stats,
                "local_video": video_stats,
                "online": online_stats,
                "online_accounts": accounts,
                "online_scope": scope or "none",
                "online_details": online_details,
            }
            warning = None
            if truncated:
                warning = (
                    f"数量超出限制，仅处理前 {max_tokens} 个（共 {original_count} 个）"
                )
            task.finish(result, warning=warning)
        except Exception as e:
            task.fail_task(str(e))
        finally:
            asyncio.create_task(expire_task(task.id, 300))

    asyncio.create_task(_run())

    return {
        "status": "success",
        "task_id": task.id,
        "total": len(selected_tokens),
    }


@router.post("/api/v1/admin/cache/clear", dependencies=[Depends(verify_api_key)])
async def clear_local_cache_api(data: dict):
    """清理本地缓存"""
    from app.services.grok.services.assets import DownloadService
    from app.services.grok.services.image_meta import cleanup_all

    cache_type = data.get("type", "image")

    try:
        dl_service = DownloadService()
        result = dl_service.clear(cache_type)
        # 清空图片缓存时同步清除元数据索引
        if cache_type == "image":
            cleanup_all()
        return {"status": "success", "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/v1/admin/cache/list", dependencies=[Depends(verify_api_key)])
async def list_local_cache_api(
    cache_type: str = "image",
    type_: str = Query(default=None, alias="type"),
    page: int = 1,
    page_size: int = 1000,
):
    """列出本地缓存文件"""
    from app.services.grok.services.assets import DownloadService

    try:
        if type_:
            cache_type = type_
        dl_service = DownloadService()
        result = dl_service.list_files(cache_type, page, page_size)
        return {"status": "success", **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/v1/admin/cache/item/delete", dependencies=[Depends(verify_api_key)])
async def delete_local_cache_item_api(data: dict):
    """删除单个本地缓存文件"""
    from app.services.grok.services.assets import DownloadService
    from app.services.grok.services.image_meta import cleanup_deleted

    cache_type = data.get("type", "image")
    name = data.get("name")
    if not name:
        raise HTTPException(status_code=400, detail="Missing file name")
    try:
        dl_service = DownloadService()
        result = dl_service.delete_file(cache_type, name)
        # 同步清理元数据索引
        if cache_type == "image":
            cleanup_deleted(name)
        return {"status": "success", "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/v1/admin/cache/tasks", dependencies=[Depends(verify_api_key)])
async def list_cache_tasks_api(
    page: int = 1,
    page_size: int = 20,
    source: str = Query(default=None),
):
    """按任务分组返回缓存图片"""
    from app.services.grok.services.image_meta import get_tasks

    try:
        result = get_tasks(page=page, page_size=page_size, source=source)
        return {"status": "success", **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/v1/admin/cache/task/delete", dependencies=[Depends(verify_api_key)])
async def delete_cache_task_api(data: dict):
    """删除整个任务分组及其所有图片"""
    from app.services.grok.services.image_meta import delete_task

    task_id = data.get("task_id")
    if not task_id:
        raise HTTPException(status_code=400, detail="Missing task_id")
    try:
        result = delete_task(task_id)
        return {"status": "success", **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/v1/admin/cache/tasks/merge", dependencies=[Depends(verify_api_key)])
async def merge_cache_tasks_api():
    """合并历史中同 prompt 同 source 且时间相近的重复分组"""
    from app.services.grok.services.image_meta import merge_similar_tasks

    try:
        result = merge_similar_tasks()
        return {"status": "success", **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/v1/admin/cache/uncategorized", dependencies=[Depends(verify_api_key)])
async def list_uncategorized_cache_api(
    page: int = 1,
    page_size: int = 50,
):
    """获取未分类的缓存图片"""
    from app.services.grok.services.image_meta import get_uncategorized_images

    try:
        result = get_uncategorized_images(page=page, page_size=page_size)
        return {"status": "success", **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/v1/admin/cache/online/clear", dependencies=[Depends(verify_api_key)])
async def clear_online_cache_api(data: dict):
    """清理在线缓存"""
    from app.services.grok.services.assets import DeleteService
    from app.services.token.manager import get_token_manager
    from app.services.grok.utils.batch import run_in_batches

    delete_service = None
    try:
        mgr = await get_token_manager()
        tokens = data.get("tokens")
        delete_service = DeleteService()

        if isinstance(tokens, list):
            token_list = [t.strip() for t in tokens if isinstance(t, str) and t.strip()]
            if not token_list:
                raise HTTPException(status_code=400, detail="No tokens provided")

            # 去重并保持顺序
            token_list = list(dict.fromkeys(token_list))

            # 最大数量限制
            max_tokens = int(get_config("performance.assets_max_tokens"))
            token_list, truncated, original_count = _truncate_tokens(
                token_list, max_tokens, "Clear online cache"
            )

            results = {}
            max_concurrent = max(
                1, int(get_config("performance.assets_max_concurrent"))
            )
            batch_size = max(1, int(get_config("performance.assets_batch_size")))

            async def _clear_one(t: str):
                try:
                    result = await delete_service.delete_all(t)
                    await mgr.mark_asset_clear(t)
                    return {"status": "success", "result": result}
                except Exception as e:
                    return {"status": "error", "error": str(e)}

            raw_results = await run_in_batches(
                token_list,
                _clear_one,
                max_concurrent=max_concurrent,
                batch_size=batch_size,
            )
            for token, res in raw_results.items():
                if res.get("ok"):
                    results[token] = res.get("data", {})
                else:
                    results[token] = {"status": "error", "error": res.get("error")}

            response = {"status": "success", "results": results}
            if truncated:
                response["warning"] = (
                    f"数量超出限制，仅处理前 {max_tokens} 个（共 {original_count} 个）"
                )
            return response

        token = data.get("token") or mgr.get_token()
        if not token:
            raise HTTPException(
                status_code=400, detail="No available token to perform cleanup"
            )

        result = await delete_service.delete_all(token)
        await mgr.mark_asset_clear(token)
        return {"status": "success", "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if delete_service:
            await delete_service.close()


@router.post(
    "/api/v1/admin/cache/online/clear/async", dependencies=[Depends(verify_api_key)]
)
async def clear_online_cache_api_async(data: dict):
    """清理在线缓存（异步批量 + SSE 进度）"""
    from app.services.grok.services.assets import DeleteService
    from app.services.token.manager import get_token_manager
    from app.services.grok.utils.batch import run_in_batches

    mgr = await get_token_manager()
    tokens = data.get("tokens")
    if not isinstance(tokens, list):
        raise HTTPException(status_code=400, detail="No tokens provided")

    token_list = [t.strip() for t in tokens if isinstance(t, str) and t.strip()]
    if not token_list:
        raise HTTPException(status_code=400, detail="No tokens provided")

    max_tokens = int(get_config("performance.assets_max_tokens"))
    token_list, truncated, original_count = _truncate_tokens(
        token_list, max_tokens, "Clear online cache async"
    )

    max_concurrent = get_config("performance.assets_max_concurrent")
    batch_size = get_config("performance.assets_batch_size")

    task = create_task(len(token_list))

    async def _run():
        delete_service = DeleteService()
        try:

            async def _clear_one(t: str):
                try:
                    result = await delete_service.delete_all(t)
                    await mgr.mark_asset_clear(t)
                    return {"ok": True, "result": result}
                except Exception as e:
                    return {"ok": False, "error": str(e)}

            async def _on_item(item: str, res: dict):
                ok = bool(res.get("data", {}).get("ok"))
                task.record(ok)

            raw_results = await run_in_batches(
                token_list,
                _clear_one,
                max_concurrent=max_concurrent,
                batch_size=batch_size,
                on_item=_on_item,
                should_cancel=lambda: task.cancelled,
            )

            if task.cancelled:
                task.finish_cancelled()
                return

            results = {}
            ok_count = 0
            fail_count = 0
            for token, res in raw_results.items():
                data = res.get("data", {})
                if data.get("ok"):
                    ok_count += 1
                    results[token] = {"status": "success", "result": data.get("result")}
                else:
                    fail_count += 1
                    results[token] = {"status": "error", "error": data.get("error")}

            result = {
                "status": "success",
                "summary": {
                    "total": len(token_list),
                    "ok": ok_count,
                    "fail": fail_count,
                },
                "results": results,
            }
            warning = None
            if truncated:
                warning = (
                    f"数量超出限制，仅处理前 {max_tokens} 个（共 {original_count} 个）"
                )
            task.finish(result, warning=warning)
        except Exception as e:
            task.fail_task(str(e))
        finally:
            await delete_service.close()
            asyncio.create_task(expire_task(task.id, 300))

    asyncio.create_task(_run())

    return {
        "status": "success",
        "task_id": task.id,
        "total": len(token_list),
    }
