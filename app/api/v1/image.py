"""
Image Generation API 路由
"""

import asyncio
import base64
import math
import random
import re
import time
import uuid
from pathlib import Path
from typing import List, Optional, Union

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field, ValidationError

from app.services.grok.services.chat import GrokChatService
from app.services.grok.services.image import image_service
from app.services.grok.services.assets import UploadService
from app.services.grok.services.media import VideoService
from app.services.grok.models.model import ModelService
from app.services.grok.processors import (
    ImageStreamProcessor,
    ImageCollectProcessor,
    ImageWSStreamProcessor,
    ImageWSCollectProcessor,
)
from app.services.token import get_token_manager, EffortType
from app.core.exceptions import (
    AppException,
    ValidationException,
    ErrorType,
)
from app.core.storage import DATA_DIR
from app.services.grok.services.image_meta import record_images
from app.core.config import get_config
from app.core.logger import logger
from app.services.grok.services.image_meta import (
    generate_task_id, set_task_context, clear_task_context,
)


router = APIRouter(tags=["Images"])


class ImageGenerationRequest(BaseModel):
    """图片生成请求 - OpenAI 兼容"""

    prompt: str = Field(..., description="图片描述")
    model: Optional[str] = Field("grok-imagine-1.0", description="模型名称")
    n: Optional[int] = Field(1, ge=1, le=10, description="生成数量 (1-10)")
    size: Optional[str] = Field("1024x1024", description="图片尺寸 (暂不支持)")
    quality: Optional[str] = Field("standard", description="图片质量 (暂不支持)")
    response_format: Optional[str] = Field(None, description="响应格式")
    style: Optional[str] = Field(None, description="风格 (暂不支持)")
    stream: Optional[bool] = Field(False, description="是否流式输出")


class ImageEditRequest(BaseModel):
    """图片编辑请求 - OpenAI 兼容"""

    prompt: str = Field(..., description="编辑描述")
    model: Optional[str] = Field("grok-imagine-1.0-edit", description="模型名称")
    image: Optional[Union[str, List[str]]] = Field(None, description="待编辑图片文件")
    n: Optional[int] = Field(1, ge=1, le=10, description="生成数量 (1-10)")
    size: Optional[str] = Field("1024x1024", description="图片尺寸 (暂不支持)")
    quality: Optional[str] = Field("standard", description="图片质量 (暂不支持)")
    response_format: Optional[str] = Field(None, description="响应格式")
    style: Optional[str] = Field(None, description="风格 (暂不支持)")
    stream: Optional[bool] = Field(False, description="是否流式输出")


def _validate_common_request(
    request: Union[ImageGenerationRequest, ImageEditRequest],
    *,
    allow_ws_stream: bool = False,
):
    """通用参数校验"""
    # 验证 prompt
    if not request.prompt or not request.prompt.strip():
        raise ValidationException(
            message="Prompt cannot be empty", param="prompt", code="empty_prompt"
        )

    # 验证 n 参数范围
    if request.n < 1 or request.n > 10:
        raise ValidationException(
            message="n must be between 1 and 10", param="n", code="invalid_n"
        )

    # 流式只支持 n=1 或 n=2
    if request.stream and request.n not in [1, 2]:
        raise ValidationException(
            message="Streaming is only supported when n=1 or n=2",
            param="stream",
            code="invalid_stream_n",
        )

    if allow_ws_stream:
        # WS 流式仅支持 b64_json (base64 视为同义)
        if (
            request.stream
            and get_config("image.image_ws")
            and request.response_format
            and request.response_format not in {"b64_json", "base64"}
        ):
            raise ValidationException(
                message="Streaming with image_ws only supports response_format=b64_json/base64",
                param="response_format",
                code="invalid_response_format",
            )

    if request.response_format:
        allowed_formats = {"b64_json", "base64", "url"}
        if request.response_format not in allowed_formats:
            raise ValidationException(
                message=f"response_format must be one of {sorted(allowed_formats)}",
                param="response_format",
                code="invalid_response_format",
            )


def validate_generation_request(request: ImageGenerationRequest):
    """验证图片生成请求参数"""
    if request.model != "grok-imagine-1.0":
        raise ValidationException(
            message="The model `grok-imagine-1.0` is required for image generation.",
            param="model",
            code="model_not_supported",
        )
    # 验证模型 - 通过 is_image 检查
    model_info = ModelService.get(request.model)
    if not model_info or not model_info.is_image:
        # 获取支持的图片模型列表
        image_models = [m.model_id for m in ModelService.MODELS if m.is_image]
        raise ValidationException(
            message=(
                f"The model `{request.model}` is not supported for image generation. "
                f"Supported: {image_models}"
            ),
            param="model",
            code="model_not_supported",
        )
    _validate_common_request(request, allow_ws_stream=True)


def resolve_response_format(response_format: Optional[str]) -> str:
    """解析响应格式"""
    fmt = response_format or get_config("app.image_format")
    if isinstance(fmt, str):
        fmt = fmt.lower()
    if fmt in ("b64_json", "base64", "url"):
        return fmt
    raise ValidationException(
        message="response_format must be one of b64_json, base64, url",
        param="response_format",
        code="invalid_response_format",
    )


def response_field_name(response_format: str) -> str:
    """获取响应字段名"""
    return {"url": "url", "base64": "base64"}.get(response_format, "b64_json")


def resolve_aspect_ratio(size: str) -> str:
    """Map OpenAI size to Grok Imagine aspect ratio."""
    size = (size or "").lower()
    if size in {"16:9", "9:16", "1:1", "2:3", "3:2"}:
        return size
    mapping = {
        "1024x1024": "1:1",
        "512x512": "1:1",
        "1024x576": "16:9",
        "1280x720": "16:9",
        "1536x864": "16:9",
        "576x1024": "9:16",
        "720x1280": "9:16",
        "864x1536": "9:16",
        "1024x1536": "2:3",
        "512x768": "2:3",
        "768x1024": "2:3",
        "1536x1024": "3:2",
        "768x512": "3:2",
        "1024x768": "3:2",
    }
    return mapping.get(size) or "2:3"


def validate_edit_request(request: ImageEditRequest, images: List[UploadFile]):
    """验证图片编辑请求参数"""
    if request.model != "grok-imagine-1.0-edit":
        raise ValidationException(
            message=("The model `grok-imagine-1.0-edit` is required for image edits."),
            param="model",
            code="model_not_supported",
        )
    _validate_common_request(request, allow_ws_stream=False)
    if not images:
        raise ValidationException(
            message="Image is required",
            param="image",
            code="missing_image",
        )
    if len(images) > 16:
        raise ValidationException(
            message="Too many images. Maximum is 16.",
            param="image",
            code="invalid_image_count",
        )


def _get_effort(model_info) -> EffortType:
    """获取模型消耗级别"""
    return (
        EffortType.HIGH
        if (model_info and model_info.cost.value == "high")
        else EffortType.LOW
    )


async def _wrap_stream_with_usage(stream, token_mgr, token, model_info):
    """包装流式响应，成功完成时记录使用"""
    success = False
    try:
        async for chunk in stream:
            yield chunk
        success = True
    finally:
        if success:
            try:
                await token_mgr.consume(token, _get_effort(model_info))
            except Exception as e:
                logger.warning(f"Failed to consume token: {e}")


async def _get_token(model: str):
    """获取可用 token"""
    token_mgr = await get_token_manager()
    await token_mgr.reload_if_stale()

    # 读取 NSFW Token 优先开关
    require_tags = ["nsfw"] if get_config("token.prefer_nsfw_token") else None

    token = None
    for pool_name in ModelService.pool_candidates_for_model(model):
        token = token_mgr.get_token(pool_name, require_tags=require_tags)
        if token:
            break

    if not token:
        raise AppException(
            message="No available tokens. Please try again later.",
            error_type=ErrorType.RATE_LIMIT.value,
            code="rate_limit_exceeded",
            status_code=429,
        )

    return token_mgr, token


async def call_grok(
    token_mgr,
    token: str,
    prompt: str,
    model_info,
    file_attachments: Optional[List[str]] = None,
    response_format: str = "b64_json",
) -> List[str]:
    """调用 Grok 获取图片，返回 base64 列表"""
    chat_service = GrokChatService()
    success = False

    try:
        response = await chat_service.chat(
            token=token,
            message=prompt,
            model=model_info.grok_model,
            mode=model_info.model_mode,
            stream=True,
            file_attachments=file_attachments,
        )

        processor = ImageCollectProcessor(
            model_info.model_id, token, response_format=response_format
        )
        images = await processor.process(response)
        success = True
        return images

    except Exception as e:
        logger.error(f"Grok image call failed: {e}")
        return []
    finally:
        if success:
            try:
                await token_mgr.consume(token, _get_effort(model_info))
            except Exception as e:
                logger.warning(f"Failed to consume token: {e}")


@router.post("/images/generations")
async def create_image(request: ImageGenerationRequest):
    """
    Image Generation API

    流式响应格式:
    - event: image_generation.partial_image
    - event: image_generation.completed

    非流式响应格式:
    - {"created": ..., "data": [{"b64_json": "..."}], "usage": {...}}
    """
    # stream 默认为 false
    if request.stream is None:
        request.stream = False

    if request.response_format is None:
        request.response_format = resolve_response_format(None)

    # 参数验证
    validate_generation_request(request)

    # 兼容 base64/b64_json
    if request.response_format == "base64":
        request.response_format = "b64_json"

    response_format = resolve_response_format(request.response_format)
    response_field = response_field_name(response_format)

    # 获取 token 和模型信息
    token_mgr, token = await _get_token(request.model)
    model_info = ModelService.get(request.model)
    use_ws = bool(get_config("image.image_ws"))

    # 流式模式
    if request.stream:
        if use_ws:
            aspect_ratio = resolve_aspect_ratio(request.size)
            enable_nsfw = bool(get_config("image.image_ws_nsfw"))
            upstream = image_service.stream(
                token=token,
                prompt=request.prompt,
                aspect_ratio=aspect_ratio,
                n=request.n,
                enable_nsfw=enable_nsfw,
            )
            processor = ImageWSStreamProcessor(
                model_info.model_id,
                token,
                n=request.n,
                response_format=response_format,
                size=request.size,
            )

            return StreamingResponse(
                _wrap_stream_with_usage(
                    processor.process(upstream), token_mgr, token, model_info
                ),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
            )

        chat_service = GrokChatService()
        response = await chat_service.chat(
            token=token,
            message=f"Image Generation: {request.prompt}",
            model=model_info.grok_model,
            mode=model_info.model_mode,
            stream=True,
        )

        processor = ImageStreamProcessor(
            model_info.model_id, token, n=request.n, response_format=response_format
        )

        return StreamingResponse(
            _wrap_stream_with_usage(
                processor.process(response), token_mgr, token, model_info
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    # 非流式模式
    n = request.n

    # 设置元数据上下文，让 process_url() 下载图片时自动记录
    meta_task_id = generate_task_id()
    aspect_ratio = resolve_aspect_ratio(request.size)
    set_task_context(
        task_id=meta_task_id,
        prompt=request.prompt,
        source="api",
        aspect_ratio=aspect_ratio,
    )

    usage_override = None
    if use_ws:
        aspect_ratio = resolve_aspect_ratio(request.size)
        enable_nsfw = bool(get_config("image.image_ws_nsfw"))
        all_images = []
        seen = set()
        expected_per_call = 6
        calls_needed = max(1, math.ceil(n / expected_per_call))
        calls_needed = min(calls_needed, n)

        async def _fetch_batch(call_target: int):
            upstream = image_service.stream(
                token=token,
                prompt=request.prompt,
                aspect_ratio=aspect_ratio,
                n=call_target,
                enable_nsfw=enable_nsfw,
            )
            processor = ImageWSCollectProcessor(
                model_info.model_id,
                token,
                n=call_target,
                response_format=response_format,
            )
            return await processor.process(upstream)

        tasks = []
        for i in range(calls_needed):
            remaining = n - (i * expected_per_call)
            call_target = min(expected_per_call, remaining)
            tasks.append(_fetch_batch(call_target))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for batch in results:
            if isinstance(batch, Exception):
                logger.warning(f"WS batch failed: {batch}")
                continue
            for img in batch:
                if img not in seen:
                    seen.add(img)
                    all_images.append(img)
                if len(all_images) >= n:
                    break
            if len(all_images) >= n:
                break
        try:
            await token_mgr.consume(token, _get_effort(model_info))
        except Exception as e:
            logger.warning(f"Failed to consume token: {e}")
        usage_override = {
            "total_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "input_tokens_details": {"text_tokens": 0, "image_tokens": 0},
        }
    else:
        calls_needed = (n + 1) // 2

        if calls_needed == 1:
            # 单次调用
            all_images = await call_grok(
                token_mgr,
                token,
                f"Image Generation: {request.prompt}",
                model_info,
                response_format=response_format,
            )
        else:
            # 并发调用
            tasks = [
                call_grok(
                    token_mgr,
                    token,
                    f"Image Generation: {request.prompt}",
                    model_info,
                    response_format=response_format,
                )
                for _ in range(calls_needed)
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # 收集成功的图片
            all_images = []
            for result in results:
                if isinstance(result, Exception):
                    logger.error(f"Concurrent call failed: {result}")
                elif isinstance(result, list):
                    all_images.extend(result)

    # 随机选取 n 张图片
    if len(all_images) >= n:
        selected_images = random.sample(all_images, n)
    else:
        # 全部返回，error 填充缺失
        selected_images = all_images.copy()
        while len(selected_images) < n:
            selected_images.append("error")

    # 构建响应
    data = [{response_field: img} for img in selected_images]
    usage = usage_override or {
        "total_tokens": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "input_tokens_details": {"text_tokens": 0, "image_tokens": 0},
    }

    # 清除元数据上下文
    clear_task_context()

    return JSONResponse(
        content={
            "created": int(time.time()),
            "data": data,
            "usage": usage,
        }
    )


@router.post("/images/upload")
async def upload_image_for_edit(
    image: List[UploadFile] = File(...),
):
    """上传参考图片到 Grok，返回 image_urls 供 WebSocket 编辑循环使用"""
    import traceback
    try:
        from app.services.grok.services.assets import UploadService
        from app.services.grok.services.media import VideoService

        model_id = "grok-imagine-1.0-edit"

        # 读取图片并转为 base64 data URL
        allowed_types = {"image/png", "image/jpeg", "image/webp", "image/jpg"}
        images: List[str] = []
        for item in image:
            content = await item.read()
            await item.close()
            if not content:
                continue
            mime = (item.content_type or "").lower()
            if mime == "image/jpg":
                mime = "image/jpeg"
            ext = Path(item.filename or "").suffix.lower()
            if mime not in allowed_types:
                if ext in (".jpg", ".jpeg"):
                    mime = "image/jpeg"
                elif ext == ".png":
                    mime = "image/png"
                elif ext == ".webp":
                    mime = "image/webp"
                else:
                    mime = "image/jpeg"
            b64 = base64.b64encode(content).decode()
            images.append(f"data:{mime};base64,{b64}")

        if not images:
            raise AppException(
                message="No valid images uploaded",
                error_type=ErrorType.SERVER.value,
                code="no_images",
            )

        # 获取 token
        _, token = await _get_token(model_id)
        logger.info(f"[upload] Got token: {token[:16]}...")

        # 上传到 Grok
        image_urls: List[str] = []
        upload_service = UploadService()
        try:
            for img_data in images:
                logger.info(f"[upload] Uploading image, data length: {len(img_data)}")
                file_id, file_uri = await upload_service.upload(img_data, token)
                logger.info(f"[upload] Upload result: file_id={file_id}, file_uri={file_uri}")
                if file_uri:
                    if file_uri.startswith("http"):
                        image_urls.append(file_uri)
                    else:
                        image_urls.append(f"https://assets.grok.com/{file_uri.lstrip('/')}")
        finally:
            await upload_service.close()

        if not image_urls:
            raise AppException(
                message="Image upload failed",
                error_type=ErrorType.SERVER.value,
                code="upload_failed",
            )

        # 尝试创建 parent post（可选，失败不影响主流程）
        parent_post_id = None
        try:
            media_service = VideoService()
            parent_post_id = await media_service.create_image_post(token, image_urls[0])
        except Exception as e:
            logger.warning(f"Create image post failed: {e}")

        if not parent_post_id:
            for url in image_urls:
                match = re.search(r"/generated/([a-f0-9-]+)/", url)
                if match:
                    parent_post_id = match.group(1)
                    break
                match = re.search(r"/users/[^/]+/([a-f0-9-]+)/content", url)
                if match:
                    parent_post_id = match.group(1)
                    break

        return JSONResponse(content={
            "image_urls": image_urls,
            "parent_post_id": parent_post_id,
            "upload_token": token,
        })
    except AppException:
        raise
    except Exception as e:
        logger.error(f"[upload] Unexpected error: {traceback.format_exc()}")
        raise


@router.post("/images/edits")
async def edit_image(
    prompt: str = Form(...),
    image: List[UploadFile] = File(...),
    model: Optional[str] = Form("grok-imagine-1.0-edit"),
    n: int = Form(1),
    size: str = Form("1024x1024"),
    quality: str = Form("standard"),
    response_format: Optional[str] = Form(None),
    style: Optional[str] = Form(None),
    stream: Optional[bool] = Form(False),
):
    """
    Image Edits API

    同官方 API 格式，仅支持 multipart/form-data 文件上传
    """
    if response_format is None:
        response_format = resolve_response_format(None)

    try:
        edit_request = ImageEditRequest(
            prompt=prompt,
            model=model,
            n=n,
            size=size,
            quality=quality,
            response_format=response_format,
            style=style,
            stream=stream,
        )
    except ValidationError as exc:
        errors = exc.errors()
        if errors:
            first = errors[0]
            loc = first.get("loc", [])
            msg = first.get("msg", "Invalid request")
            code = first.get("type", "invalid_value")
            param_parts = [
                str(x) for x in loc if not (isinstance(x, int) or str(x).isdigit())
            ]
            param = ".".join(param_parts) if param_parts else None
            raise ValidationException(message=msg, param=param, code=code)
        raise ValidationException(message="Invalid request", code="invalid_value")

    if edit_request.stream is None:
        edit_request.stream = False

    response_format = resolve_response_format(edit_request.response_format)
    edit_request.response_format = response_format
    response_field = response_field_name(response_format)

    # 参数验证
    validate_edit_request(edit_request, image)

    max_image_bytes = 50 * 1024 * 1024
    allowed_types = {"image/png", "image/jpeg", "image/webp", "image/jpg"}

    images: List[str] = []
    for item in image:
        content = await item.read()
        await item.close()
        if not content:
            raise ValidationException(
                message="File content is empty",
                param="image",
                code="empty_file",
            )
        if len(content) > max_image_bytes:
            raise ValidationException(
                message="Image file too large. Maximum is 50MB.",
                param="image",
                code="file_too_large",
            )
        mime = (item.content_type or "").lower()
        if mime == "image/jpg":
            mime = "image/jpeg"
        ext = Path(item.filename or "").suffix.lower()
        if mime not in allowed_types:
            if ext in (".jpg", ".jpeg"):
                mime = "image/jpeg"
            elif ext == ".png":
                mime = "image/png"
            elif ext == ".webp":
                mime = "image/webp"
            else:
                raise ValidationException(
                    message="Unsupported image type. Supported: png, jpg, webp.",
                    param="image",
                    code="invalid_image_type",
                )
        b64 = base64.b64encode(content).decode()
        images.append(f"data:{mime};base64,{b64}")

    # 获取 token 和模型信息
    token_mgr, token = await _get_token(edit_request.model)
    model_info = ModelService.get(edit_request.model)

    # 上传图片
    image_urls: List[str] = []
    upload_service = UploadService()
    try:
        for image in images:
            file_id, file_uri = await upload_service.upload(image, token)
            if file_uri:
                if file_uri.startswith("http"):
                    image_urls.append(file_uri)
                else:
                    image_urls.append(f"https://assets.grok.com/{file_uri.lstrip('/')}")
    finally:
        await upload_service.close()

    if not image_urls:
        raise AppException(
            message="Image upload failed",
            error_type=ErrorType.SERVER.value,
            code="upload_failed",
        )

    parent_post_id = None
    try:
        media_service = VideoService()
        parent_post_id = await media_service.create_image_post(token, image_urls[0])
        logger.debug(f"Parent post ID: {parent_post_id}")
    except Exception as e:
        logger.warning(f"Create image post failed: {e}")

    if not parent_post_id:
        for url in image_urls:
            match = re.search(r"/generated/([a-f0-9-]+)/", url)
            if match:
                parent_post_id = match.group(1)
                logger.debug(f"Parent post ID: {parent_post_id}")
                break
            match = re.search(r"/users/[^/]+/([a-f0-9-]+)/content", url)
            if match:
                parent_post_id = match.group(1)
                logger.debug(f"Parent post ID: {parent_post_id}")
                break


    model_config_override = {
        "modelMap": {
            "imageEditModel": "imagine",
            "imageEditModelConfig": {
                "imageReferences": image_urls,
            },
        }
    }

    if parent_post_id:
        model_config_override["modelMap"]["imageEditModelConfig"]["parentPostId"] = (
            parent_post_id
        )

    raw_payload = {
        "temporary": bool(get_config("chat.temporary")),
        "modelName": model_info.grok_model,
        "message": edit_request.prompt,
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

    # 流式模式
    if edit_request.stream:
        chat_service = GrokChatService()
        response = await chat_service.chat(
            token=token,
            message=edit_request.prompt,
            model=model_info.grok_model,
            mode=None,
            stream=True,
            raw_payload=raw_payload,
        )

        processor = ImageStreamProcessor(
            model_info.model_id,
            token,
            n=edit_request.n,
            response_format=response_format,
        )

        return StreamingResponse(
            _wrap_stream_with_usage(
                processor.process(response), token_mgr, token, model_info
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    # 非流式模式
    n = edit_request.n
    calls_needed = (n + 1) // 2

    async def _call_edit():
        chat_service = GrokChatService()
        response = await chat_service.chat(
            token=token,
            message=edit_request.prompt,
            model=model_info.grok_model,
            mode=None,
            stream=True,
            raw_payload=raw_payload,
        )
        processor = ImageCollectProcessor(
            model_info.model_id, token, response_format=response_format
        )
        return await processor.process(response)

    if calls_needed == 1:
        all_images = await _call_edit()
    else:
        tasks = [_call_edit() for _ in range(calls_needed)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_images = []
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Concurrent call failed: {result}")
            elif isinstance(result, list):
                all_images.extend(result)

    # 选择图片
    if len(all_images) >= n:
        selected_images = random.sample(all_images, n)
    else:
        selected_images = all_images.copy()
        while len(selected_images) < n:
            selected_images.append("error")

    data = [{response_field: img} for img in selected_images]

    # 保存图片到缓存并记录元数据
    try:
        saved_filenames = []
        cache_dir = DATA_DIR / "tmp" / "image"
        cache_dir.mkdir(parents=True, exist_ok=True)
        run_id = uuid.uuid4().hex

        for i, img_b64 in enumerate(selected_images):
            if not img_b64 or img_b64 == "error":
                continue
            
            ext = "png"
            if img_b64.startswith("data:"):
                header, b64_data = img_b64.split(",", 1)
                if "jpeg" in header or "jpg" in header:
                    ext = "jpg"
                img_data = base64.b64decode(b64_data)
            else:
                if not img_b64.startswith("iVBOR"):
                    ext = "jpg"
                img_data = base64.b64decode(img_b64)
            
            filename = f"imagine-edit-{run_id}-{i}.{ext}"
            filepath = cache_dir / filename
            filepath.write_bytes(img_data)
            saved_filenames.append(filename)
        
        if saved_filenames:
            record_images(
                task_id=f"edit_{run_id}",
                prompt=edit_request.prompt,
                filenames=saved_filenames,
                source="imagine",
                aspect_ratio="1:1", # 编辑模式通常是正方形或原图比例，暂时默认 1:1 或需要解析
            )
    except Exception as e:
        logger.warning(f"Failed to save edit images to cache: {e}")

    return JSONResponse(
        content={
            "created": int(time.time()),
            "data": data,
            "usage": {
                "total_tokens": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "input_tokens_details": {"text_tokens": 0, "image_tokens": 0},
            },
        }
    )


__all__ = ["router"]
