"""
Grok Imagine WebSocket image service.
"""

import asyncio
import certifi
import json
import re
import ssl
import time
import uuid
from typing import AsyncGenerator, Dict, Optional
from urllib.parse import urlparse

import aiohttp
from aiohttp_socks import ProxyConnector

from app.core.config import get_config
from app.core.logger import logger
from app.services.grok.utils.headers import build_sso_cookie

WS_URL = "wss://grok.com/ws/imagine/listen"


class _BlockedError(Exception):
    pass


class _AuthError(Exception):
    """401 认证失败，用于内部控制流，不传播到 processor"""
    pass


class ImageService:
    """Grok Imagine WebSocket image service."""

    # 共享 Session 及代理信息（类级别，所有并发请求复用同一连接池）
    _shared_session: Optional[aiohttp.ClientSession] = None
    _shared_proxy: Optional[str] = None
    _session_lock: asyncio.Lock = None  # lazy init，避免事件循环问题

    def __init__(self):
        self._ssl_context = ssl.create_default_context()
        self._ssl_context.load_verify_locations(certifi.where())
        self._url_pattern = re.compile(r"/images/([a-f0-9-]+)\.(png|jpg|jpeg)")

    def _resolve_proxy(self) -> tuple[aiohttp.BaseConnector, Optional[str]]:
        proxy_url = get_config("network.base_proxy_url")
        if not proxy_url:
            return aiohttp.TCPConnector(ssl=self._ssl_context), None

        scheme = urlparse(proxy_url).scheme.lower()
        if scheme.startswith("socks"):
            logger.info(f"Using SOCKS proxy: {proxy_url}")
            return ProxyConnector.from_url(proxy_url, ssl=self._ssl_context), None

        logger.info(f"Using HTTP proxy: {proxy_url}")
        return aiohttp.TCPConnector(ssl=self._ssl_context), proxy_url

    async def _get_shared_session(self) -> tuple[aiohttp.ClientSession, Optional[str]]:
        """获取共享 aiohttp Session，所有并发 WS 请求复用同一连接池"""
        if ImageService._session_lock is None:
            ImageService._session_lock = asyncio.Lock()

        async with ImageService._session_lock:
            if ImageService._shared_session is None or ImageService._shared_session.closed:
                connector, proxy = self._resolve_proxy()
                ImageService._shared_session = aiohttp.ClientSession(connector=connector)
                ImageService._shared_proxy = proxy
                logger.info("创建共享 aiohttp Session（连接池复用）")
            return ImageService._shared_session, ImageService._shared_proxy

    @classmethod
    async def close(cls):
        """关闭共享 Session，释放连接池资源"""
        if cls._shared_session and not cls._shared_session.closed:
            await cls._shared_session.close()
            cls._shared_session = None
            cls._shared_proxy = None
            logger.info("共享 aiohttp Session 已关闭")

    def _get_ws_headers(self, token: str) -> Dict[str, str]:
        cookie = build_sso_cookie(token, include_rw=True)
        user_agent = get_config("security.user_agent")
        return {
            "Cookie": cookie,
            "Origin": "https://grok.com",
            "User-Agent": user_agent,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }

    def _extract_image_id(self, url: str) -> Optional[str]:
        match = self._url_pattern.search(url or "")
        return match.group(1) if match else None

    def _is_final_image(self, url: str, blob_size: int) -> bool:
        return (url or "").lower().endswith(
            (".jpg", ".jpeg")
        ) and blob_size > get_config("image.image_ws_final_min_bytes")

    def _classify_image(self, url: str, blob: str) -> Optional[Dict[str, object]]:
        if not url or not blob:
            return None

        image_id = self._extract_image_id(url) or uuid.uuid4().hex
        blob_size = len(blob)
        is_final = self._is_final_image(url, blob_size)

        stage = (
            "final"
            if is_final
            else (
                "medium"
                if blob_size > get_config("image.image_ws_medium_min_bytes")
                else "preview"
            )
        )

        return {
            "type": "image",
            "image_id": image_id,
            "stage": stage,
            "blob": blob,
            "blob_size": blob_size,
            "url": url,
            "is_final": is_final,
        }

    async def stream(
        self,
        token: str,
        prompt: str,
        aspect_ratio: str = "2:3",
        n: int = 1,
        enable_nsfw: bool = True,
        max_retries: int = None,
    ) -> AsyncGenerator[Dict[str, object], None]:
        retries = max(1, max_retries if max_retries is not None else 1)
        logger.info(
            f"Image generation: prompt='{prompt[:50]}...', n={n}, ratio={aspect_ratio}, nsfw={enable_nsfw}"
        )

        for attempt in range(retries):
            try:
                yielded_any = False
                async for item in self._stream_once(
                    token, prompt, aspect_ratio, n, enable_nsfw
                ):
                    yielded_any = True
                    yield item
                return
            except _BlockedError:
                if yielded_any or attempt + 1 >= retries:
                    if not yielded_any:
                        yield {
                            "type": "error",
                            "error_code": "blocked",
                            "error": "blocked_no_final_image",
                        }
                    return
                logger.warning(f"WebSocket blocked, retry {attempt + 1}/{retries}")
            except _AuthError as e:
                # 401 不传播给 processor，直接结束让调用方换 Token
                logger.warning(f"WebSocket 401, stream ends silently: {e}")
                return
            except Exception as e:
                logger.error(f"WebSocket stream failed: {str(e)[:100]}")
                return

    async def _stream_once(
        self,
        token: str,
        prompt: str,
        aspect_ratio: str,
        n: int,
        enable_nsfw: bool,
    ) -> AsyncGenerator[Dict[str, object], None]:
        request_id = str(uuid.uuid4())
        headers = self._get_ws_headers(token)
        timeout = float(get_config("network.timeout"))
        blocked_seconds = float(get_config("image.image_ws_blocked_seconds"))

        try:
            session, proxy = await self._get_shared_session()
        except Exception as e:
            logger.error(f"WebSocket session setup failed: {e}")
            return

        try:
            async with session.ws_connect(
                WS_URL,
                headers=headers,
                heartbeat=20,
                receive_timeout=timeout,
                proxy=proxy,
            ) as ws:
                message = {
                    "type": "conversation.item.create",
                    "timestamp": int(time.time() * 1000),
                    "item": {
                        "type": "message",
                        "content": [
                            {
                                "requestId": request_id,
                                "text": prompt,
                                "type": "input_text",
                                "properties": {
                                    "section_count": 0,
                                    "is_kids_mode": False,
                                    "enable_nsfw": enable_nsfw,
                                    "skip_upsampler": False,
                                    "is_initial": False,
                                    "aspect_ratio": aspect_ratio,
                                },
                            }
                        ],
                    },
                }

                await ws.send_json(message)
                logger.info(f"WebSocket request sent: {prompt[:80]}...")

                images = {}
                completed = 0
                start_time = last_activity = time.time()
                medium_received_time = None

                while time.time() - start_time < timeout:
                    try:
                        ws_msg = await asyncio.wait_for(ws.receive(), timeout=5.0)
                    except asyncio.TimeoutError:
                        if (
                            medium_received_time
                            and completed == 0
                            and time.time() - medium_received_time
                            > min(10, blocked_seconds)
                        ):
                            raise _BlockedError()
                        if completed > 0 and time.time() - last_activity > 10:
                            logger.info(
                                f"WebSocket idle timeout, collected {completed} images"
                            )
                            break
                        continue

                    if ws_msg.type == aiohttp.WSMsgType.TEXT:
                        last_activity = time.time()
                        msg = json.loads(ws_msg.data)
                        msg_type = msg.get("type")

                        if msg_type == "image":
                            info = self._classify_image(
                                msg.get("url", ""), msg.get("blob", "")
                            )
                            if not info:
                                continue

                            image_id = info["image_id"]
                            existing = images.get(image_id, {})

                            if (
                                info["stage"] == "medium"
                                and medium_received_time is None
                            ):
                                medium_received_time = time.time()

                            if info["is_final"] and not existing.get("is_final"):
                                completed += 1
                                logger.debug(
                                    f"Final image received: id={image_id}, size={info['blob_size']}"
                                )

                            images[image_id] = {
                                "is_final": info["is_final"]
                                or existing.get("is_final")
                            }
                            yield info

                        elif msg_type == "error":
                            err_code = msg.get('err_code', '')
                            err_msg = msg.get('err_msg', '')
                            logger.warning(
                                f"WebSocket error: {err_code} - {err_msg}"
                            )
                            # rate_limit 类错误和 401 一样处理：raise 让 stream 静默结束
                            if 'rate_limit' in str(err_code) or '429' in str(err_code):
                                raise _AuthError(f"{err_code}: {err_msg}")
                            yield {
                                "type": "error",
                                "error_code": err_code,
                                "error": err_msg,
                            }
                            return

                        if completed >= n:
                            logger.info(
                                f"WebSocket collected {completed} final images"
                            )
                            break

                        if (
                            medium_received_time
                            and completed == 0
                            and time.time() - medium_received_time > blocked_seconds
                        ):
                            raise _BlockedError()

                    elif ws_msg.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        logger.warning(f"WebSocket closed/error: {ws_msg.type}")
                        yield {
                            "type": "error",
                            "error_code": "ws_closed",
                            "error": f"websocket closed: {ws_msg.type}",
                        }
                        break

        except aiohttp.ClientError as e:
            err_str = str(e)
            # 401/429 都 raise _AuthError，让 stream 静默结束
            if "401" in err_str or "429" in err_str:
                raise _AuthError(err_str[:100])
            else:
                logger.error(f"WebSocket connection error: {err_str[:100]}")
                yield {"type": "error", "error_code": "connection_failed", "error": err_str[:100]}


image_service = ImageService()

__all__ = ["image_service", "ImageService"]
