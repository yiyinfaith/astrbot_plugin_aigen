import io
import asyncio
import aiohttp
import base64
import ssl
import re
import inspect
from typing import List, Optional
from pathlib import Path
from PIL import Image as PILImage
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, Image, Reply
from astrbot.api import logger


class ImageManager:
    def __init__(self, config: dict):
        self.proxy = config.get("proxy_url") if config.get("use_proxy") else None
        self.max_retries = config.get("download_retries", 3)
        self.timeout = config.get("timeout", 60)

    async def _download_image(
        self, url: str, timeout: Optional[int] = None
    ) -> bytes | None:
        """通用下载逻辑"""
        for i in range(self.max_retries + 1):
            try:
                ssl_ctx = ssl.create_default_context()
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE

                async with aiohttp.ClientSession(
                    connector=aiohttp.TCPConnector(ssl=ssl_ctx)
                ) as session:
                    async with session.get(
                        url, proxy=self.proxy, timeout=timeout or self.timeout
                    ) as resp:
                        resp.raise_for_status()
                        return await resp.read()
            except Exception as e:
                if i < self.max_retries:
                    await asyncio.sleep(1)
        return None

    def _optimize_output_image_sync(self, raw: bytes) -> bytes:
        """对结果图做更积极的发送前压缩，降低平台上传耗时。"""
        if not raw or len(raw) < 700_000:
            return raw

        try:
            with PILImage.open(io.BytesIO(raw)) as img:
                working = img.copy()
                width, height = working.size

                # 更积极地下采样，优先缩短上传时间
                max_side = 1600 if len(raw) < 3_000_000 else 1280
                if width > max_side or height > max_side:
                    working.thumbnail((max_side, max_side), PILImage.Resampling.LANCZOS)

                # 判断是否真的需要透明通道
                has_alpha_band = "A" in working.getbands()
                needs_alpha = False
                if has_alpha_band:
                    try:
                        alpha = working.getchannel("A")
                        alpha_min, alpha_max = alpha.getextrema()
                        needs_alpha = alpha_min < 255 or alpha_max < 255
                    except Exception:
                        needs_alpha = True

                candidates = []

                # 候选1：如果不需要透明，优先转 JPEG
                if not needs_alpha:
                    rgb = working.convert("RGB")
                    for quality in (85, 78, 72):
                        out = io.BytesIO()
                        rgb.save(
                            out,
                            format="JPEG",
                            quality=quality,
                            optimize=True,
                            progressive=True,
                            subsampling=1 if quality >= 80 else 2,
                        )
                        candidates.append(("JPEG", quality, out.getvalue()))
                else:
                    # 候选2：保留透明时，尽量压缩 PNG
                    rgba = working.convert("RGBA")
                    for compress_level in (9,):
                        out = io.BytesIO()
                        rgba.save(
                            out,
                            format="PNG",
                            optimize=True,
                            compress_level=compress_level,
                        )
                        candidates.append(("PNG", compress_level, out.getvalue()))

                if not candidates:
                    return raw

                # 先取最小候选
                best_format, best_level, best_data = min(
                    candidates, key=lambda x: len(x[2])
                )

                # 如果仍然偏大，再做一次更强缩放兜底
                target_limit = 900_000
                if len(best_data) > target_limit:
                    fallback = working.copy()
                    fallback.thumbnail((1280, 1280), PILImage.Resampling.LANCZOS)

                    if not needs_alpha:
                        fallback = fallback.convert("RGB")
                        out = io.BytesIO()
                        fallback.save(
                            out,
                            format="JPEG",
                            quality=68,
                            optimize=True,
                            progressive=True,
                            subsampling=2,
                        )
                        best_format, best_level, best_data = "JPEG", 68, out.getvalue()
                    else:
                        out = io.BytesIO()
                        fallback.convert("RGBA").save(
                            out,
                            format="PNG",
                            optimize=True,
                            compress_level=9,
                        )
                        best_format, best_level, best_data = "PNG", 9, out.getvalue()

                if best_data and len(best_data) < len(raw):
                    logger.info(
                        f"结果图发送前已压缩: {len(raw) / 1024:.1f}KB -> {len(best_data) / 1024:.1f}KB "
                        f"| format={best_format} level={best_level}"
                    )
                    return best_data
        except Exception as e:
            logger.warning(f"Output image optimize failed: {e}")

        return raw

    async def optimize_output_image(self, raw: bytes) -> bytes:
        return await asyncio.to_thread(self._optimize_output_image_sync, raw)

    def _extract_first_frame_sync(self, raw: bytes) -> bytes:
        """(同步) 提取第一帧、转PNG并压缩"""
        try:
            with PILImage.open(io.BytesIO(raw)) as img:
                # 统一转换为 RGBA PNG，解决很多兼容性问题
                if getattr(img, "is_animated", False):
                    img.seek(0)
                img_conv = img.convert("RGBA")

                # [新增] 限制最大边长为 1568 (Gemini/OpenAI Pro Vision 推荐值)
                # 超过此尺寸只会增加 Tokens 消耗和延迟，对生成质量帮助不大
                max_side = 1568
                w, h = img_conv.size
                if w > max_side or h > max_side:
                    ratio = min(max_side / w, max_side / h)
                    new_size = (int(w * ratio), int(h * ratio))
                    img_conv = img_conv.resize(new_size, PILImage.Resampling.LANCZOS)

                out = io.BytesIO()
                # 使用 PNG 压缩优化 (optimize=True 比较耗时，为了速度改为 False，仅依靠Resize减小体积)
                img_conv.save(out, format="PNG")
                return out.getvalue()
        except Exception as e:
            logger.warning(f"Image conversion/resize failed: {e}")
            return raw

    def _is_avatar_source(self, src: str) -> bool:
        """判断一个来源是否明显是平台头像，而不是用户主动发送的图片"""
        if src is None:
            return False

        try:
            text = str(src).strip().lower()
        except Exception:
            return False

        if not text:
            return False

        avatar_keywords = [
            "qlogo.cn",
            "q1.qlogo.cn",
            "q2.qlogo.cn",
            "q3.qlogo.cn",
            "q4.qlogo.cn",
            "thirdqq.qlogo.cn",
            "headimg",
            "/avatar",
            "useravatar",
            "getavatar",
        ]
        return any(keyword in text for keyword in avatar_keywords)

    def _is_probably_valid_source(self, src: str) -> bool:
        """判断一个图片来源字符串是否值得继续尝试加载"""
        if src is None:
            return False

        try:
            src = str(src).strip()
        except Exception:
            return False

        if not src:
            return False

        if self._is_avatar_source(src):
            return False

        if (
            src.startswith("http://")
            or src.startswith("https://")
            or src.startswith("base64://")
        ):
            return True

        if len(src) < 512:
            try:
                return Path(src).is_file()
            except Exception:
                return False

        return False

    async def _resolve_bot_for_context(self, context):
        """兼容不同 AstrBot 版本的 Context/Bot 获取方式"""
        if not context:
            return None

        candidate_attrs = [
            "get_bot",
            "get_robot",
            "get_adapter",
            "get_client",
            "bot",
            "robot",
            "adapter",
            "client",
        ]

        for attr_name in candidate_attrs:
            if not hasattr(context, attr_name):
                continue

            try:
                target = getattr(context, attr_name)
                value = target() if callable(target) else target
                if inspect.isawaitable(value):
                    value = await value
                if value:
                    return value
            except Exception:
                continue

        return None

    async def _fetch_reply_components(self, context, reply_id) -> list:
        """尝试从上下文/机器人中获取被回复消息的组件列表"""
        if not context or not reply_id:
            return []

        bot = await self._resolve_bot_for_context(context)
        if not bot:
            return []

        for method_name in (
            "get_message",
            "fetch_message",
            "get_msg",
            "get_reply_message",
        ):
            if not hasattr(bot, method_name):
                continue

            try:
                method = getattr(bot, method_name)
                result = method(reply_id)
                if inspect.isawaitable(result):
                    result = await result

                if not result:
                    continue

                if hasattr(result, "message_obj") and hasattr(
                    result.message_obj, "message"
                ):
                    return list(result.message_obj.message)
                if hasattr(result, "message"):
                    return list(result.message)
                if isinstance(result, list):
                    return result
            except Exception:
                continue

        return []

    @staticmethod
    def _event_chain(event: AstrMessageEvent) -> list:
        """Read the message chain through the current AstrBot event API."""
        getter = getattr(event, "get_messages", None)
        if callable(getter):
            try:
                messages = getter()
                if messages is not None:
                    return list(messages)
            except (AttributeError, TypeError, RuntimeError, ValueError) as exc:
                logger.debug("Could not read event message chain: %s", exc)
        message_obj = getattr(event, "message_obj", None)
        messages = getattr(message_obj, "message", None)
        return list(messages or [])

    async def _load_quoted_image_refs(
        self, event: AstrMessageEvent, reply: Reply
    ) -> list[bytes]:
        """Resolve quoted-message image references using AstrBot's utilities.

        Newer AstrBot adapters intentionally leave ``Reply.chain`` empty.  The
        quoted-message parser and ``MediaResolver`` are the supported fallback;
        older installations simply return no references and use the existing
        context/bot fetch path below.
        """
        try:
            from astrbot.core.utils.media_utils import MediaResolver
            from astrbot.core.utils.quoted_message.image_resolver import ImageResolver
            from astrbot.core.utils.quoted_message_parser import (
                extract_quoted_message_images,
            )
        except (ImportError, ModuleNotFoundError):
            return []

        try:
            refs = list(await extract_quoted_message_images(event, reply))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Quoted image parser unavailable: %s", exc)
            return []
        if not refs:
            return []

        # OneBot may expose an opaque file token first and a downloadable URL
        # only after ImageResolver talks to the adapter.
        if getattr(event, "get_platform_name", lambda: "")() == "aiocqhttp":
            try:
                refs.extend(await ImageResolver(event).resolve_for_llm(refs))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Quoted image URL resolution failed: %s", exc)

        result: list[bytes] = []
        seen: set[str] = set()
        for ref in refs:
            if not ref or str(ref) in seen:
                continue
            seen.add(str(ref))
            try:
                raw = await MediaResolver(ref, media_type="image").to_bytes()
                if raw:
                    image = await asyncio.get_running_loop().run_in_executor(
                        None, self._extract_first_frame_sync, raw
                    )
                    if image:
                        result.append(image)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Quoted image reference could not be read: %s", exc)
                fallback = await self.load_bytes(str(ref))
                if fallback:
                    result.append(fallback)
        return result

    async def load_raw_bytes(self, src: str) -> bytes | None:
        """加载原始字节（本地/URL/Base64），不做图片解析或格式转换"""
        raw = None
        loop = asyncio.get_running_loop()

        try:
            src = str(src).strip()
            if not src:
                return None

            is_local_file = False
            if (
                len(src) < 512
                and not src.startswith("http")
                and not src.startswith("base64://")
            ):
                try:
                    if Path(src).is_file():
                        is_local_file = True
                except:
                    pass

            if src.startswith("http"):
                raw = await self._download_image(src)
            elif src.startswith("base64://"):
                raw = await loop.run_in_executor(None, base64.b64decode, src[9:])
            elif is_local_file:
                raw = await loop.run_in_executor(None, Path(src).read_bytes)
            else:
                logger.debug(f"跳过无效图片来源: {src[:80]}")
                return None

            return raw
        except Exception as e:
            logger.error(f"Failed to load raw bytes from {src[:50]}...: {e}")
            return None

    async def load_bytes(self, src: str) -> bytes | None:
        """加载图片数据（本地/URL/Base64）- 纯异步封装"""
        loop = asyncio.get_running_loop()

        try:
            raw = await self.load_raw_bytes(src)
            if raw:
                # 图片处理(PIL)放入线程池，防止阻塞
                return await loop.run_in_executor(
                    None, self._extract_first_frame_sync, raw
                )
        except Exception as e:
            logger.error(f"Failed to load bytes from {src[:50]}...: {e}")

        return None

    async def get_avatar(self, user_id: str) -> bytes | None:
        if not user_id.isdigit():
            return None
        return await self._download_image(
            f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
        )

    async def extract_images_from_event(
        self,
        event: AstrMessageEvent,
        ignore_id: str = None,
        context=None,
        include_at_avatar: bool = True,
        max_images: int | None = None,
        include_sender_avatar: bool = False,
        extra_sources: List[str] | None = None,
    ) -> List[bytes]:
        """按请求类型收集图片，并保留消息中的参数顺序。

        ``max_images=1`` 用于预设关键词：引用图片、消息中发送的图片、
        @用户头像、发送者头像依次作为回退来源。多图请求不设置
        ``max_images``，引用图片、发送图片和 @头像按照组件在消息中的顺序
        收集，且不会自动加入发送者头像。
        """
        quoted_tasks = []
        message_tasks = []
        at_tasks = []
        ordered_tasks = []
        at_users: set[str] = set()
        pending_extra_sources = list(extra_sources or [])

        # 1. 规范化 ignore_id，确保是字符串且去除空白
        if ignore_id:
            ignore_id = str(ignore_id).strip()

        logger.debug(f"extract_images_from_event: ignore_id={ignore_id}")

        chain = self._event_chain(event)

        def add_image_task(source: str, bucket: list):
            if not self._is_probably_valid_source(source):
                return False
            # Keep the loader lazy so lower-priority sources are not opened
            # when a higher-priority source already satisfied a single-image
            # request.
            task = lambda source=source: self.load_bytes(source)
            bucket.append(task)
            ordered_tasks.append(task)
            return True

        def add_image_bytes(image: bytes, bucket: list):
            if not isinstance(image, bytes):
                return
            task = lambda image=image: asyncio.sleep(0, result=image)
            bucket.append(task)
            ordered_tasks.append(task)

        def add_at_user(user_id: str):
            qq = str(user_id or "").strip()
            if not qq or (ignore_id and qq == ignore_id) or qq in at_users:
                return
            at_users.add(qq)
            if not include_at_avatar:
                return
            task = lambda qq=qq: self.get_avatar(qq)
            at_tasks.append(task)
            ordered_tasks.append(task)

        def add_text_mentions(value):
            if value is None:
                return
            for match in re.finditer(r"@(\d+)", str(value)):
                add_at_user(match.group(1))

        def add_text_content(value):
            """Read textual @mentions and image URLs at their chain position."""
            if value is None:
                return
            text = str(value)
            add_text_mentions(text)
            for source in list(pending_extra_sources):
                if source and source in text:
                    add_image_task(source, message_tasks)
                    pending_extra_sources.remove(source)

        def image_source(segment) -> str | None:
            for attr in ("url", "file", "path"):
                source = getattr(segment, attr, None)
                if source and self._is_probably_valid_source(source):
                    return str(source)
            return None

        # 2. 收集引用图片、当前消息图片和 @头像。ordered_tasks 保留多图
        # 请求的真实组件顺序；单图请求稍后按来源类别选择。
        for seg in chain:
            if isinstance(seg, Reply):
                found_in_chain = False
                if seg.chain:
                    for s_chain in seg.chain:
                        if isinstance(s_chain, Image):
                            source = image_source(s_chain)
                            if source and add_image_task(source, quoted_tasks):
                                found_in_chain = True
                        elif isinstance(s_chain, At):
                            add_at_user(
                                getattr(s_chain, "qq", getattr(s_chain, "user_id", ""))
                            )
                        else:
                            add_text_content(getattr(s_chain, "text", None))

                if not found_in_chain:
                    quoted_images = await self._load_quoted_image_refs(event, seg)
                    if quoted_images:
                        found_in_chain = True
                        for image in quoted_images:
                            add_image_bytes(image, quoted_tasks)

                if not found_in_chain and context and hasattr(seg, "id") and seg.id:
                    try:
                        logger.debug(
                            f"Reply chain empty/no-image, fetching message_id: {seg.id}"
                        )
                        components = await self._fetch_reply_components(context, seg.id)

                        for comp in components:
                            if isinstance(comp, Image):
                                source = image_source(comp)
                                if source:
                                    add_image_task(source, quoted_tasks)
                    except Exception as e:
                        logger.warning(f"Failed to fetch reply message {seg.id}: {e}")

            elif isinstance(seg, Image):
                source = image_source(seg)
                if source:
                    add_image_task(source, message_tasks)
            elif isinstance(seg, At):
                add_at_user(getattr(seg, "qq", getattr(seg, "user_id", "")))
            else:
                add_text_content(getattr(seg, "text", None))

        # 3. 某些平台不会把 @ 序列化为 At 组件，补充规范化文本中的数字 QQ。
        # 已由组件读到的 ID 会去重；无法得知组件位置时按消息末尾处理。
        getter = getattr(event, "get_message_str", None)
        try:
            raw_event_text = (
                getter() if callable(getter) else getattr(event, "message_str", "")
            )
            event_text = str(raw_event_text or "")
        except (AttributeError, TypeError, RuntimeError, ValueError):
            event_text = str(getattr(event, "message_str", "") or "")
        add_text_content(event_text)
        if include_at_avatar and at_users:
            logger.debug(f"At users to fetch avatars: {at_users}")

        # 文本中的图片 URL 属于发送者主动提供的图片，加入“发送图片”来源。
        for source in pending_extra_sources:
            add_image_task(source, message_tasks)

        async def resolve(tasks: list) -> list[bytes]:
            if not tasks:
                return []
            coroutines = [task() if callable(task) else task for task in tasks]
            results = await asyncio.gather(*coroutines, return_exceptions=True)
            images = []
            for result in results:
                if isinstance(result, bytes):
                    images.append(result)
                elif isinstance(result, Exception):
                    logger.warning(f"Image extraction error: {result}")
            return images

        if max_images == 1:
            # 预设关键词只接收一张图，并严格按：引用 > 发送图片 > @头像
            # > 发送者头像 选择第一个成功读取的来源。
            for tasks in (quoted_tasks, message_tasks, at_tasks):
                images = await resolve(tasks)
                if images:
                    return images[:1]
            if include_sender_avatar:
                sender_id = str(event.get_sender_id() or "").strip()
                sender_avatar = await self.get_avatar(sender_id)
                if isinstance(sender_avatar, bytes):
                    return [sender_avatar]
            return []

        images = await resolve(ordered_tasks)
        if max_images is not None and max_images > 0:
            return images[:max_images]
        return images
