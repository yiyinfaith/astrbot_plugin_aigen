"""AstrBot image/video generation plugin.

The plugin intentionally keeps the small, stable feature set needed in
production: image generation, keyword presets, configurable help text, and
usage statistics. Network details remain in ``ApiManager`` so all supported
request modes use one implementation.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any, ClassVar

import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain, Video
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core import AstrBotConfig

from .api_manager import ApiManager, ImageDownloadError
from .config_manager import media_config, migrate_config
from .data_manager import DataManager
from .image_manager import ImageManager
from .utils import extract_image_urls_from_text, match_keyword_in_text, norm_id
from .video_api_manager import VideoApiManager, VideoDownloadError
from .video_inputs import collect_media, normalize_media_event, text_media
from .video_router import VideoError, VideoOptions, parse_video_command


@register(
    "astrbot_plugin_aigen",
    "yiyinfaith",
    "统一图片、视频生成与关键词预设插件",
    "1.1.0",
    "https://github.com/yiyinfaith/astrbot_plugin_aigen",
)
class ImageGeneratorPlugin(Star):
    """Generate images through configured OpenAI-compatible or Gemini APIs."""

    _HELP_COMMANDS: ClassVar[set[str]] = {
        "ai生成帮助",
        "画图帮助",
        "生图帮助",
        "画图菜单",
        "生图菜单",
        "生视频帮助",
        "生视频菜单",
    }
    _DRAW_COMMANDS: ClassVar[set[str]] = {"画图", "生图"}
    _VIDEO_COMMANDS: ClassVar[set[str]] = {"生视频", "生成视频"}

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        data_dir = Path(StarTools.get_data_dir())
        migrate_config(config, data_dir / "config_backups")
        self.root_conf = config
        self.conf = media_config(config, "image")
        self.video_conf = media_config(config, "video")
        self.data_mgr = DataManager(data_dir, self.conf, self.video_conf)
        self.img_mgr = ImageManager(self.conf)
        self.api_mgr = ApiManager(self.conf)
        self.video_mgr = VideoApiManager(self.video_conf, data_dir)
        self._cleanup_task = None
        self._llm_last_call: dict[str, float] = {}
        self._video_collections: dict[tuple[str, str, str], dict] = {}
        self._video_busy: set[tuple[str, str, str]] = set()
        self._video_generation_tasks: set[asyncio.Task] = set()
        self._terminating = False

    async def initialize(self):
        """Load usage statistics and configured presets after plugin injection."""
        await self.data_mgr.initialize()
        logger.info(
            "Image generator loaded with %d presets and interface mode %s",
            len(self.data_mgr.prompt_map),
            self.conf.get("interface_mode", "openai_image"),
        )
        if not self.api_mgr._normalize_keys(self.conf.get("api_keys")):
            logger.warning("No API key is configured for the image generator")
        self._cleanup_task = asyncio.create_task(self._cleanup_video_cache())

    async def terminate(self):
        """Close the reusable HTTP session during hot reload."""
        self._terminating = True
        self._video_collections.clear()
        tasks = list(self._video_generation_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        for manager in (self.api_mgr, self.video_mgr):
            session = getattr(manager, "_session", None)
            if session is not None and not session.closed:
                await session.close()

    async def _cleanup_video_cache(self):
        """Keep completed MP4s for platform delivery; never touch user files."""
        while True:
            await asyncio.sleep(3600)
            for key, collection in list(self._video_collections.items()):
                if monotonic() - collection["updated"] > 1800:
                    self._video_collections.pop(key, None)
            cutoff = (
                datetime.now(timezone.utc).timestamp()
                - max(1, int(self.video_conf.get("cache_hours", 24))) * 3600
            )

            def cleanup(cutoff=cutoff):
                for path in self.video_mgr.data_dir.glob("*.mp4"):
                    if path.stat().st_mtime < cutoff:
                        path.unlink(missing_ok=True)

            try:
                await asyncio.to_thread(cleanup)
            except OSError as exc:
                logger.warning("Could not clean video cache: %s", exc)

    def _bot_id(self, event: AstrMessageEvent) -> str:
        """Resolve the current bot ID for image extraction filtering."""
        for attr in ("self_id", "get_self_id"):
            value = getattr(event, attr, None)
            if callable(value):
                try:
                    value = value()
                except (AttributeError, TypeError, RuntimeError, ValueError):
                    value = None
            if value:
                return str(value)
        getter = getattr(self.context, "get_self_id", None)
        if callable(getter):
            try:
                value = getter()
                if value:
                    return str(value)
            except (AttributeError, TypeError, RuntimeError, ValueError) as exc:
                logger.debug("Could not resolve bot ID from context: %s", exc)
        return ""

    @staticmethod
    def _event_message_text(event: AstrMessageEvent) -> str:
        """Return AstrBot's normalized text representation for an event.

        Current AstrBot platforms expose ``get_message_str()`` as the stable
        API.  ``message_str`` is retained as a fallback for older adapters and
        for the small fake events used by downstream tests.
        """
        getter = getattr(event, "get_message_str", None)
        if callable(getter):
            try:
                value = getter()
                if value is not None:
                    return str(value)
            except (AttributeError, TypeError, RuntimeError, ValueError) as exc:
                logger.debug("Could not read normalized event text: %s", exc)
        return str(getattr(event, "message_str", "") or "")

    @staticmethod
    def _clean_message(text: str) -> str:
        """Remove one command prefix while retaining the user's prompt."""
        return re.sub(r"^(?:[/#！!])\s*", "", (text or "").strip()).strip()

    @staticmethod
    def _has_astrbot_prefix(event: AstrMessageEvent, text: str) -> bool:
        """Return whether an event has AstrBot's command prefix or bot wake."""
        if str(text or "").lstrip().startswith(("/", "#", "!", "！")):
            return True
        return bool(getattr(event, "is_at_or_wake_command", False))

    @staticmethod
    def _remove_mention_prefix(text: str) -> str:
        """Drop textual @mentions that precede a matched preset keyword.

        A platform may serialize an ``At`` component as ``@123`` or as a
        display name such as ``@小明``.  The component itself is still used by
        ``ImageManager`` to fetch the avatar; this helper only keeps that
        serialization from becoming part of the image prompt.
        """
        return re.sub(r"(?:^|\s)@[^\s]+\s*", " ", text).strip()

    def _resolve_preset_prompt(
        self, text: str, kind: str = "image"
    ) -> tuple[str, str, str] | None:
        """Resolve a keyword preset anywhere in the message.

        The matching rule uses fuzzy matching (``keyword in text``) instead of
        requiring the keyword to be the first token.  This allows
        messages such as ``@小明手办化`` and ``请手办化`` to trigger while the
        image collector still obtains the actual ``At`` component/avatar.
        """
        clean = self._clean_message(text)
        if not clean:
            return None
        conf = self.video_conf if kind == "video" else self.conf
        prompt_map = (
            self.data_mgr.video_prompt_map
            if kind == "video"
            else self.data_mgr.prompt_map
        )
        model = str(conf.get("model", "nano-banana") or "nano-banana")
        default_prefix = "生视频" if kind == "video" else "生图"
        prefixes = conf.get("extra_prefix", [default_prefix])
        if isinstance(prefixes, str):
            prefixes = [prefixes]
        for prefix in sorted(
            {
                str(item).strip().lstrip("/")
                for item in (prefixes or [])
                if str(item).strip()
            },
            key=len,
            reverse=True,
        ):
            if prefix and (clean == prefix or clean.startswith(prefix + " ")):
                return clean[len(prefix) :].strip(), "自定义", model

        matched = match_keyword_in_text(clean, prompt_map)
        if matched:
            key, match_at = matched
            prompt = prompt_map.get(key)
            if prompt and prompt != "[内置预设]":
                before = self._remove_mention_prefix(clean[:match_at])
                after = clean[match_at + len(key) :].strip()
                suffix = " ".join(part for part in (before, after) if part).strip()
                return f"{prompt} {suffix}".strip(), key, model
        return None

    def _resolve_video_prompt(self, raw: str) -> tuple[str, str, str]:
        clean = self._clean_message(raw)
        for command in self._VIDEO_COMMANDS:
            if clean == command:
                return "", "自定义", str(self.video_conf.get("model", ""))
            if clean.startswith(command + " "):
                clean = clean[len(command) :].strip()
                break
        return self._resolve_preset_prompt(clean, "video") or (
            clean,
            "自定义",
            str(self.video_conf.get("model", "")),
        )

    def _resolve_draw_prompt(self, raw: str) -> tuple[str, str, str]:
        """Resolve ``/生图 <custom prompt>`` or an optional preset keyword."""
        clean = self._clean_message(raw)
        for command in self._DRAW_COMMANDS:
            if clean == command:
                return "", "自定义", str(self.conf.get("model", "nano-banana"))
            if clean.startswith(command + " "):
                clean = clean[len(command) :].strip()
                break
        resolved = self._resolve_preset_prompt(clean)
        if resolved is not None:
            return resolved
        return clean, "自定义", str(self.conf.get("model", "nano-banana"))

    async def _extract_images(
        self,
        event: AstrMessageEvent,
        prompt: str,
        preset_name: str = "自定义",
        *,
        strict=False,
        extra_sources=None,
    ) -> list[bytes]:
        """Collect image parameters using the preset or custom-prompt rules."""
        is_preset = preset_name not in {"", "自定义"}
        return await self.img_mgr.extract_images_from_event(
            event,
            ignore_id=self._bot_id(event),
            context=event,
            include_at_avatar=True,
            max_images=1 if is_preset else None,
            include_sender_avatar=is_preset,
            extra_sources=extra_sources
            if extra_sources is not None
            else extract_image_urls_from_text(prompt),
            strict=strict,
        )

    async def _generate(
        self,
        event: AstrMessageEvent,
        prompt: str,
        preset_name: str,
        model: str,
        images: list[bytes],
        show_progress: bool = True,
        include_result_text: bool = True,
        image_options: dict[str, object] | None = None,
        dispatch_result: bool = False,
        tool_call: bool = False,
    ) -> list[Any]:
        """Call the selected API mode, record usage, and build a reply.

        LLM function-tool calls can request an image-only response by setting
        ``include_result_text`` to ``False`` and disabling progress output.
        Normal commands keep the configured success text.
        """
        uid = norm_id(event.get_sender_id())
        gid = norm_id(event.get_group_id())

        display_name = "" if preset_name in {"", "自定义"} else preset_name
        template = str(
            self.conf.get(
                "generating_msg_template", "🎨 收到请求，正在生成 [{preset}]..."
            )
        )
        feedback = template.replace("{preset}", display_name)
        if not display_name:
            feedback = feedback.replace(" [{preset}]", "").replace("[{preset}]", "")
        if show_progress:
            await event.send(event.chain_result([Plain(feedback)]))

        source_url = ""
        source_expiry = ""
        try:
            start = datetime.now(timezone.utc)
            result = await self.api_mgr.call_api(
                images,
                prompt,
                model,
                proxy=self.img_mgr.proxy,
                image_options=image_options,
            )
        except ImageDownloadError as exc:
            logger.warning("图片本地下载失败，准备使用公网链接回退：%s", exc)
            if dispatch_result:
                return await self._deliver_image_fallback(
                    event,
                    exc.url,
                    exc.url_expires_at,
                    send_message=not tool_call,
                )
            result = str(exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Image generation request failed")
            result = str(exc)

        if not isinstance(result, bytes):
            if self.conf.get("debug_mode", False):
                return [Plain(f"图片生成失败：{result}")]
            return [Plain("图片生成失败，请稍后重试。")]

        source_url = str(getattr(result, "url", "") or "")
        source_expiry = str(getattr(result, "url_expires_at", "") or "")
        result = await self.img_mgr.optimize_output_image(result)
        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        await self.data_mgr.record_usage(uid, gid)
        suffix = f" | 预设：{preset_name}" if preset_name not in {"", "自定义"} else ""
        if self.conf.get("show_model_info", False):
            suffix += f" | 模型：{model}"
        reply: list[Any] = [Image.fromBytes(result)]
        if include_result_text:
            reply.append(Plain(f"\n✅ 生成成功（{elapsed:.1f}s）{suffix}"))
        if dispatch_result:
            return await self._deliver_image_result(
                event,
                result,
                source_url,
                source_expiry,
                include_result_text,
                suffix,
                f"\n✅ 生成成功（{elapsed:.1f}s）{suffix}",
                send_fallback_message=not tool_call,
            )
        return reply

    @staticmethod
    def _image_expiry_note(expiry: str = "") -> str:
        expiry = str(expiry or "").strip()
        if expiry:
            return f"链接失效时间：{expiry}"
        return "链接失效时间：上游未返回明确时间，请以上游链接有效期为准"

    async def _deliver_image_fallback(
        self,
        event: AstrMessageEvent,
        url: str,
        expiry: str = "",
        *,
        send_message: bool = True,
    ) -> list[Any]:
        """Deliver or return one concise public-link fallback message."""
        url = str(url or "").strip()
        if not url:
            return [Plain("图片已生成，但发送失败，接口未提供公网链接。")]
        fallback_chain = [
            Plain(
                "图片发送失败，使用公网链接：\n"
                f"{url}\n"
                f"{self._image_expiry_note(expiry)}"
            )
        ]
        if not send_message:
            return fallback_chain
        try:
            await event.send(event.chain_result(fallback_chain))
            return []
        except Exception as exc:  # noqa: BLE001
            logger.exception("公网图片链接回退发送失败：%s", exc)
            return fallback_chain

    async def _deliver_image_result(
        self,
        event: AstrMessageEvent,
        result: bytes,
        source_url: str = "",
        source_expiry: str = "",
        include_result_text: bool = True,
        suffix: str = "",
        success_text: str = "",
        send_fallback_message: bool = True,
    ) -> list[Any]:
        """Retry local image delivery before falling back to the public URL."""
        chain: list[Any] = [Image.fromBytes(result)]
        if include_result_text:
            chain.append(Plain(success_text or f"\n✅ 生成成功{suffix}"))
        attempts = max(
            1,
            min(
                5,
                int(
                    self.conf.get(
                        "image_delivery_retries",
                        self.conf.get("result_image_download_retries", 2),
                    )
                    or 2
                ),
            ),
        )
        for attempt in range(attempts):
            try:
                await event.send(event.chain_result(chain))
                return []
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "本地图片消息发送失败（第 %d/%d 次）：%s",
                    attempt + 1,
                    attempts,
                    exc,
                )
                if attempt + 1 < attempts:
                    await asyncio.sleep(min(1.0 * (attempt + 1), 3.0))
        return await self._deliver_image_fallback(
            event,
            source_url,
            source_expiry,
            send_message=send_fallback_message,
        )

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_preset_request(self, event: AstrMessageEvent, ctx=None):
        """Route image/video prefixes and the longest preset without duplicates."""
        raw_text = self._event_message_text(event)
        text = self._clean_message(raw_text)
        key = self._video_session_key(event)
        if self._terminating:
            return
        collection = self._video_collections.get(key)
        if collection and monotonic() - collection["updated"] > 1800:
            self._video_collections.pop(key, None)
            collection = None
        control = text in {"生成", "取消"} and self._has_astrbot_prefix(event, raw_text)
        if collection or control:
            event.stop_event()
            restart = self._resolve_preset_prompt(raw_text, "video")
            is_start = any(
                text == cmd or text.startswith(cmd + " ")
                for cmd in self._VIDEO_COMMANDS
            ) or (restart is not None and restart[1] == "自定义")
            result = await self._collect_video_request(
                event,
                self._resolve_video_prompt(raw_text)[0]
                if is_start and not control
                else None,
                start=is_start and not control,
            )
            if result:
                yield event.chain_result(result)
            return
        if not text:
            return
        if any(
            text == command or text.startswith(command + " ")
            for command in self._HELP_COMMANDS
        ):
            return
        # Registered commands handle prefixed/woken events. Bare commands are
        # handled here only when the corresponding prefix switch is disabled.
        kind = "image"
        command_kind = next(
            (
                k
                for k, commands in (
                    ("image", self._DRAW_COMMANDS),
                    ("video", self._VIDEO_COMMANDS),
                )
                if any(text == cmd or text.startswith(cmd + " ") for cmd in commands)
            ),
            None,
        )
        if command_kind:
            if self._has_astrbot_prefix(event, raw_text):
                return
            kind = command_kind
            conf = self.video_conf if kind == "video" else self.conf
            if conf.get("custom_prompt_need_prefix", True):
                return
            resolved = (
                self._resolve_video_prompt(text)
                if kind == "video"
                else self._resolve_draw_prompt(text)
            )
        else:
            candidates = [
                (k, self._resolve_preset_prompt(text, k)) for k in ("image", "video")
            ]
            # Explicit custom prefixes beat keyword matches; for overlapping
            # keywords use the longest, preserving image precedence on ties.
            candidates = [(k, r) for k, r in candidates if r is not None]
            if not candidates:
                return
            kind, resolved = max(
                candidates, key=lambda item: (item[1][1] == "自定义", len(item[1][1]))
            )
        if resolved is None or (not resolved[0] and kind != "video"):
            return
        prompt, preset_name, model = resolved
        need_prefix_key = (
            "custom_prompt_need_prefix"
            if preset_name == "自定义"
            else "preset_need_prefix"
        )
        default_need_prefix = preset_name == "自定义"
        conf = self.video_conf if kind == "video" else self.conf
        if conf.get(
            need_prefix_key, default_need_prefix
        ) and not self._has_astrbot_prefix(event, raw_text):
            return
        event.stop_event()
        if kind == "video":
            result = await self._collect_video_request(
                event, prompt, preset_name, start=True
            )
        else:
            images = await self._extract_images(event, prompt, preset_name)
            result = await self._generate(
                event,
                prompt,
                preset_name,
                model,
                images,
                dispatch_result=True,
            )
        if result:
            yield event.chain_result(result)

    async def _video_command_inputs(self, event, prompt, preset_name):
        options = parse_video_command(prompt)
        if (
            options.images
            or options.audios
            or options.videos
            or extract_image_urls_from_text(prompt)
            or text_media(prompt)[0]
            or text_media(prompt)[1]
        ):
            raise VideoError(
                "生视频不接受媒体 URL 参数；请直接发送图片/文件，或引用已发送的图片、音频、视频。"
            )
        media_event = await normalize_media_event(event, self.img_mgr)
        images = await self.img_mgr.extract_image_sources_from_event(
            media_event,
            ignore_id=self._bot_id(event),
            context=event,
            include_at_avatar=False,
            preserve_duplicates=True,
            strict=True,
        )
        audios, videos = await collect_media(
            media_event, self.img_mgr, preserve_duplicates=True
        )
        return options, images, audios, videos

    @staticmethod
    def _video_session_key(event) -> tuple[str, str, str]:
        """Identify one sender in one chat on one platform.

        Args:
            event: Incoming AstrBot message event.

        Returns:
            A key that isolates groups, private chats and platform instances.
        """
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        return origin, str(event.get_group_id() or ""), norm_id(event.get_sender_id())

    async def _collect_video_request(
        self, event, prompt=None, preset_name="自定义", *, start=False
    ) -> list[Any]:
        """Collect ordered messages and submit once after the finish command.

        Args:
            event: Incoming AstrBot message event.
            prompt: Initial prompt with its command prefix removed.
            preset_name: Name of the initial video preset, if any.
            start: Whether this message begins a new collection.

        Returns:
            An optional error or video-delivery fallback message chain.
        """
        key = self._video_session_key(event)
        if self._terminating:
            return []
        if key in self._video_busy:
            return [Plain("已有视频任务正在生成，请等待结果，勿重复提交。")]
        raw = self._event_message_text(event)
        control = (
            self._clean_message(raw) if self._has_astrbot_prefix(event, raw) else ""
        )
        if not self.video_conf.get("enabled", True):
            return [Plain("视频生成功能已关闭。")]
        collection = self._video_collections.get(key)
        if collection is None:
            if not start:
                return [
                    Plain(
                        "没有正在收集的材料，或收集已超过 30 分钟；请先发送 /生视频。"
                    )
                ]
            collection = {
                "lock": asyncio.Lock(),
                "text": [],
                "images": [],
                "audios": [],
                "videos": [],
                "errors": [],
                "size": 0,
                "updated": monotonic(),
                "preset": preset_name,
            }
            self._video_collections[key] = collection
        async with collection["lock"]:
            # A finish command may have been queued while an earlier upload was
            # resolving. Verify ownership again before accepting/submitting it.
            if self._video_collections.get(key) is not collection:
                return [Plain("材料已提交或取消，请等待结果或重新发送 /生视频。")]
            if control == "取消" and not start:
                self._video_collections.pop(key, None)
                return [Plain("已取消本次材料收集。")]
            if start:
                for field in ("text", "images", "audios", "videos", "errors"):
                    collection[field].clear()
                collection.update(size=0, preset=preset_name)
            collection["updated"] = monotonic()
            finish = control == "生成" and not start
            text = str(prompt or "") if start else "" if finish else raw.strip()
            if text:
                if sum(map(len, collection["text"])) + len(text) > 2_000_000:
                    collection["errors"].append(
                        "文字材料超过 200 万字符，请 /取消 后减少材料。"
                    )
                else:
                    collection["text"].append(text)
            try:
                _, images, audios, videos = await self._video_command_inputs(
                    event, "", "自定义"
                )
                added_size = sum(
                    len(src) for bucket in (images, audios, videos) for src in bucket
                )
                if collection["size"] + added_size > 100 * 1024 * 1024:
                    raise VideoError(
                        "材料总量超过 100MB，请 /取消 后减少材料或使用较小的文件。"
                    )
                for field, sources in (
                    ("images", images),
                    ("audios", audios),
                    ("videos", videos),
                ):
                    collection[field].extend(sources)
                collection["size"] += added_size
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not collect video input: %s", exc)
                if len(collection["errors"]) < 5:
                    collection["errors"].append(
                        str(exc)[:500] or "有材料无法读取，请重新发送。"
                    )
            if self._terminating or self._video_collections.get(key) is not collection:
                return []
            if start:
                await event.send(
                    event.chain_result(
                        [Plain("请发送材料，最后发送“/生成”开始生成视频")]
                    )
                )
            if not finish:
                return []
            if collection["errors"]:
                return [
                    Plain(
                        "材料读取失败，未提交生成任务：\n"
                        + "\n".join(collection["errors"])
                        + "\n请 /取消 后重新发送材料。"
                    )
                ]
            try:
                merged = " ".join(collection["text"])
                options = parse_video_command(merged)
                if (
                    options.images
                    or options.audios
                    or options.videos
                    or extract_image_urls_from_text(merged)
                    or any(text_media(merged))
                ):
                    raise VideoError(
                        "请直接发送图片、QQ 语音、视频或媒体文件，不接受媒体 URL 参数。"
                    )
                self.video_mgr.prepare(
                    options.prompt,
                    collection["images"],
                    collection["audios"],
                    collection["videos"],
                    options=options,
                )
            except (VideoError, OSError, ValueError, TypeError) as exc:
                return [
                    Plain(
                        f"{exc}\n材料仍保留，可补充缺少的材料后再次 /生成；如需删除材料，请 /取消 后重新开始。"
                    )
                ]
            self._video_collections.pop(key, None)
            self._video_busy.add(key)
        task = asyncio.current_task()
        self._video_generation_tasks.add(task)
        try:
            return await self._generate_video(
                event,
                options.prompt,
                collection["preset"],
                collection["images"],
                collection["audios"],
                collection["videos"],
                dispatch_result=True,
                options=options,
            )
        finally:
            self._video_busy.discard(key)
            self._video_generation_tasks.discard(task)

    async def _generate_video(
        self,
        event,
        prompt,
        preset_name,
        images,
        audios=None,
        videos=None,
        *,
        tool_call=False,
        dispatch_result=False,
        duration=0,
        options=None,
    ) -> list[Any]:
        if not self.video_conf.get("enabled", True):
            return [Plain("视频生成功能已关闭。")]
        try:
            self.video_mgr.prepare(
                prompt, images, audios or [], videos or [], duration, options
            )
        except (VideoError, ValueError, TypeError) as exc:
            return [Plain(str(exc))]
        if not tool_call:
            feedback = f"共收到{len(images)}图片，{len(audios or [])}音频，{len(videos or [])}视频，生成中"
            await event.send(event.chain_result([Plain(feedback)]))
        try:
            result = await self.video_mgr.generate(
                prompt, images, audios, videos, duration, options
            )
        except VideoDownloadError as exc:
            logger.warning("视频本地下载失败，准备使用公网链接回退：%s", exc)
            if dispatch_result:
                return await self._deliver_video_fallback(
                    event,
                    exc.url,
                    exc.url_expires_at,
                    send_message=not tool_call,
                )
            return [Plain(str(exc))]
        except (VideoError, aiohttp.ClientError, OSError, ValueError) as exc:
            logger.warning("Video generation failed: %s", exc)
            # Task IDs and input validation are actionable even outside debug mode.
            return [Plain(str(exc))]
        except Exception:  # noqa: BLE001
            logger.exception("Unexpected video generation failure")
            return [
                Plain(
                    "视频生成发生内部错误，请联系管理员检查日志；勿立即重复提交，以免重复付费。"
                )
            ]
        try:
            await self.data_mgr.record_usage(
                event.get_sender_id(), event.get_group_id(), "video"
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Could not record video usage; delivering the completed video"
            )
        # A successful delivery is one video message.  Progress, if enabled,
        # was already sent before the paid request started.
        reply = [Video.fromFileSystem(str(result.path))]
        if dispatch_result:
            return await self._deliver_video_result(
                event,
                result,
                reply,
                send_fallback_message=not tool_call,
            )
        return reply

    @staticmethod
    def _video_expiry_note(expiry: str = "") -> str:
        expiry = str(expiry or "").strip()
        if expiry:
            return f"链接失效时间：{expiry}"
        return "链接失效时间：AutoDL 未返回明确时间，请以 AutoDL 上游链接有效期为准"

    async def _deliver_video_fallback(
        self,
        event: AstrMessageEvent,
        url: str,
        expiry: str = "",
        *,
        send_message: bool = True,
    ) -> list[Any]:
        """Deliver or return one concise public-link fallback message."""
        url = str(url or "").strip()
        if not url:
            return [Plain("视频已生成，但发送失败，接口未提供公网链接。")]
        fallback_chain = [
            Plain(
                "视频发送失败，使用公网链接：\n"
                f"{url}\n"
                f"{self._video_expiry_note(expiry)}"
            )
        ]
        if not send_message:
            return fallback_chain
        try:
            await event.send(event.chain_result(fallback_chain))
            return []
        except Exception as exc:  # noqa: BLE001
            logger.exception("公网视频链接回退发送失败：%s", exc)
            # Let AstrBot's normal result stage make one last delivery attempt.
            return fallback_chain

    async def _deliver_video_result(
        self,
        event: AstrMessageEvent,
        result,
        local_chain: list[Any],
        *,
        send_fallback_message: bool = True,
    ) -> list[Any]:
        """Retry a real video delivery before falling back to its public URL."""
        attempts = max(
            1,
            min(
                5,
                int(
                    self.video_conf.get(
                        "video_delivery_retries",
                        self.video_conf.get("result_video_download_retries", 2),
                    )
                    or 2
                ),
            ),
        )
        for attempt in range(attempts):
            try:
                # Keep the video send isolated: a later success-text failure
                # must never be mistaken for a failed video delivery.
                await event.send(event.chain_result(local_chain[:1]))
                return []
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "本地视频消息发送失败（第 %d/%d 次）：%s",
                    attempt + 1,
                    attempts,
                    exc,
                )
                if attempt + 1 < attempts:
                    await asyncio.sleep(min(1.0 * (attempt + 1), 3.0))

        return await self._deliver_video_fallback(
            event,
            getattr(result, "url", ""),
            getattr(result, "url_expires_at", ""),
            send_message=send_fallback_message,
        )

    @filter.llm_tool(name="generate_video")
    async def generate_video(
        self,
        event: AstrMessageEvent,
        prompt: str = "",
        duration: int = 0,
        resolution: str = "",
        aspect_ratio: str = "",
        reference_mode: str = "auto",
        seed: int = -2,
        generate_audio: str = "auto",
        use_message_media: bool = True,
    ):
        """按图片、音频、视频的组合自动选择管理员配置的模型生成视频，成功仅发送视频。

        媒体不需要填写 URL；工具会读取当前消息和引用中的 QQ 图片、文件、语音和视频。
        用户说“图生视频”“让图中人物动起来”或“跳舞”，应选 reference（或省略），一图和多图都一样。
        first_frame 表示把图片固定为视频第一帧，first_last_frame 表示固定第一帧和最后一帧；
        不要仅因图片数量选择这两种模式。根据用户意图合理选择，未配置的路线可改用普通 reference。

        Args:
            prompt(string): 常规视频需要提示词；仅动作迁移、音频同步特殊模式留空。
            duration(number): 正整数秒；0 使用提示词或配置时长。10秒以上可自动选择配置的长视频模型。动作迁移仅声明预扣时长，不裁剪视频。
            resolution(string): 可选分辨率档位，如480p、768p；留空使用所选路线的配置值，不自动降档。
            aspect_ratio(string): 可选16:9、9:16、1:1、4:3、3:4、21:9、adaptive；留空使用配置值。
            reference_mode(string): 可选，默认auto。普通图生视频、人物运动/跳舞用reference或省略；任何图片数量都不自动触发帧模式。明确首帧/尾帧用first_frame/last_frame（恰好一图），首尾帧用first_last_frame（恰好两图），都不能混音频/视频。动作迁移用motion_transfer（恰好一图一视频），自动对口型用audio_sync（恰好一图一音频）；后两种必须留空prompt。
            seed(number): -2使用配置，-1不发送种子，AutoDL最大999999999999999，Seedance最大2147483647；最小值由模型决定。
            generate_audio(string): auto使用配置，true/false控制Seedance同时生成音频。
            use_message_media(boolean): 默认true，缺少对应显式媒体时读取当前/引用消息和@头像；纯文生且不希望使用附带媒体时设false。
        """
        if not self.video_conf.get("enable_llm_tool", True):
            yield "视频生成函数工具已停用。"
            return
        uid = "video:" + norm_id(event.get_sender_id())
        cooldown = max(0, int(self.video_conf.get("llm_cooldown_seconds", 60)))
        elapsed = monotonic() - self._llm_last_call.get(uid, 0)
        if elapsed < cooldown:
            yield f"视频工具冷却中，请 {int(cooldown - elapsed) + 1} 秒后再试。"
            return
        try:
            options = VideoOptions(
                prompt=str(prompt or ""),
                duration=duration,
                resolution=resolution,
                aspect_ratio=aspect_ratio,
                reference_mode=reference_mode,
                seed=seed,
                generate_audio=generate_audio,
            )
            options.overrides()
            if use_message_media:
                media_event = await normalize_media_event(event, self.img_mgr)
                images = await self.img_mgr.extract_image_sources_from_event(
                    media_event,
                    ignore_id=self._bot_id(event),
                    context=event,
                    include_at_avatar=True,
                    strict=True,
                )
                audios, videos = await collect_media(media_event, self.img_mgr)
            else:
                images, audios, videos = [], [], []
        except (VideoError, ValueError, TypeError, OSError) as exc:
            yield str(exc)
            return
        self._llm_last_call[uid] = monotonic()
        task = asyncio.current_task()
        self._video_generation_tasks.add(task)
        try:
            result = await self._generate_video(
                event,
                options.prompt,
                "自定义",
                images,
                audios,
                videos,
                tool_call=True,
                dispatch_result=True,
                options=options,
            )
        finally:
            self._video_generation_tasks.discard(task)
        if result:
            # AstrBot sends MessageEventResult directly to the chat. A string
            # is returned to the LLM instead, including failures and URL fallbacks.
            yield "\n".join(part.text for part in result if isinstance(part, Plain))

    @filter.command("生视频", aliases={"生成视频"}, prefix_optional=False)
    async def video_command(self, event: AstrMessageEvent):
        """根据文本、图片、音频和视频自动路由，支持引用、@头像和参数校验。"""
        prompt, preset_name, _ = self._resolve_video_prompt(
            self._event_message_text(event)
        )
        event.stop_event()
        result = await self._collect_video_request(
            event, prompt, preset_name, start=True
        )
        if result:
            yield event.chain_result(result)

    @filter.llm_tool(name="generate_image")
    async def generate_image(
        self,
        event: AstrMessageEvent,
        prompt: str = "",
        resolution: str = "",
        aspect_ratio: str = "",
        size: str = "",
        quality: str = "",
        background: str = "",
        output_format: str = "",
        output_compression: int = 0,
        moderation: str = "",
        style: str = "",
        n: int = 0,
        max_num_results: int = 0,
        input_fidelity: str = "",
        partial_images: int = 0,
        action: str = "",
    ):
        """使用统一图片生成入口生成或编辑图片。

        媒体不需要填写 URL；工具会读取当前消息和引用中的图片或图片文件。

        Args:
            prompt(string): 可选的图片生成或编辑提示词。省略时优先使用当前消息文本；如果只有参考图，则使用通用编辑提示。
            resolution(string): 可选插件画质档位：1K、2K、4K；留空使用配置或提示词。
            aspect_ratio(string): 可选比例，如 1:1、16:9、9:16、4:3、3:4、2:3、3:2、4:5、5:4、21:9；留空使用配置或输入图片比例。
            size(string): 可选 OpenAI Images 尺寸：auto、256x256、512x512、1024x1024、1536x1024、1024x1536、1792x1024、1024x1792，或符合模型约束的自定义 WIDTHxHEIGHT；填写后优先于 resolution/aspect_ratio 的 OpenAI size 映射。
            quality(string): 可选 OpenAI 画质：auto、low、medium、high、xhigh、max；旧版 DALL-E 兼容接口可填写 standard、hd。
            background(string): 可选背景：auto、transparent、opaque。
            output_format(string): 可选输出格式：png、jpeg、webp。
            output_compression(number): 可选输出压缩率 0-100；0 表示不发送此字段。
            moderation(string): 可选内容审核级别：auto、low。
            style(string): 可选风格：vivid、natural；主要适用于 DALL-E 3 兼容模型。
            n(number): 可选生成数量；0 表示使用插件默认值 1，插件回复第一张图片。
            max_num_results(number): 可选 Responses 图片工具生成数量，1-50；未填写时使用接口默认值。
            input_fidelity(string): 可选编辑参考图保真度：low、high；没有输入图片时忽略。
            partial_images(number): 可选流式中间图片数量：0-3；仅由支持该字段的 OpenAI 图片接口处理。
            action(string): 可选 Responses 图片工具动作：auto、generate、edit；留空由接口自动判断。
        """
        if not self.conf.get("enable_llm_tool", True):
            yield "图片生成函数工具当前已在插件配置中停用。"
            return

        uid = norm_id(event.get_sender_id())
        cooldown = max(0, int(self.conf.get("llm_cooldown_seconds", 60) or 0))
        now = monotonic()
        if cooldown:
            elapsed = now - self._llm_last_call.get(uid, 0.0)
            if elapsed < cooldown:
                yield f"图片生成工具冷却中，请 {int(cooldown - elapsed) + 1} 秒后再试。"
                return
        self._llm_last_call[uid] = now

        prompt = str(prompt or "").strip()
        if not prompt:
            # LLM 调用有时只传媒体而省略 prompt。先尝试使用当前消息的
            # 文本，最后再由参考图编辑走一个明确但宽松的默认提示。
            prompt = self._clean_message(self._event_message_text(event))

        images: list[bytes] = []
        images = await self._extract_images(
            event, prompt, "自定义", strict=True, extra_sources=[]
        )
        if not prompt and images:
            prompt = "请根据参考图片生成一张高质量图片，保留主体特征并自然完成画面。"
        if not prompt:
            yield "请提供图片生成或编辑提示词，或在消息中附带参考图片。"
            return

        image_options = {
            "resolution": str(resolution or "").strip(),
            "aspect_ratio": str(aspect_ratio or "").strip(),
            "size": str(size or "").strip(),
            "quality": str(quality or "").strip(),
            "background": str(background or "").strip(),
            "output_format": str(output_format or "").strip(),
            "output_compression": output_compression,
            "moderation": str(moderation or "").strip(),
            "style": str(style or "").strip(),
            "n": n,
            "max_num_results": max_num_results,
            "input_fidelity": str(input_fidelity or "").strip(),
            "partial_images": partial_images,
            "action": str(action or "").strip(),
        }

        result = await self._generate(
            event,
            prompt,
            "自定义",
            str(self.conf.get("model", "nano-banana") or "nano-banana"),
            images,
            show_progress=False,
            include_result_text=False,
            image_options=image_options,
            dispatch_result=True,
            tool_call=True,
        )
        if result:
            yield "\n".join(part.text for part in result if isinstance(part, Plain))

    @filter.command("生图", aliases={"画图"}, prefix_optional=False)
    async def draw_command(self, event: AstrMessageEvent):
        """Generate an image from ``/生图 <自定义提示词>`` or a preset."""
        prompt, preset_name, model = self._resolve_draw_prompt(
            self._event_message_text(event)
        )
        if not prompt:
            yield event.chain_result([Plain("用法：/画图 <自定义提示词>。")])
            return
        event.stop_event()
        images = await self._extract_images(event, prompt, preset_name)
        result = await self._generate(
            event,
            prompt,
            preset_name,
            model,
            images,
            dispatch_result=True,
        )
        if result:
            yield event.chain_result(result)

    @filter.command(
        "ai生成帮助",
        aliases={
            "生图帮助",
            "画图帮助",
            "生图菜单",
            "画图菜单",
            "生视频帮助",
            "生视频菜单",
        },
        prefix_optional=True,
    )
    async def help_command(self, event: AstrMessageEvent):
        """Send the help text configured in the plugin settings."""
        text = str(self.conf.get("help_text", "帮助文档未配置。"))
        yield event.chain_result([Plain(text)])
