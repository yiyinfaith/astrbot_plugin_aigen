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

from .api_manager import ApiManager
from .config_manager import media_config, migrate_config
from .data_manager import DataManager
from .image_manager import ImageManager
from .utils import extract_image_urls_from_text, match_keyword_in_text, norm_id
from .video_api_manager import VideoApiManager
from .video_inputs import collect_media, resolve_media, strip_media_text, text_media
from .video_router import VideoError, VideoOptions, parse_video_command


@register(
    "astrbot_plugin_aigen",
    "yiyinfaith",
    "统一图片、视频生成与关键词预设插件",
    "1.0.1",
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
        extra_prefix = str(
            conf.get("extra_prefix", default_prefix) or default_prefix
        ).strip()
        if extra_prefix and (
            clean == extra_prefix or clean.startswith(extra_prefix + " ")
        ):
            return clean[len(extra_prefix) :].strip(), "自定义", model

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

        try:
            start = datetime.now(timezone.utc)
            result = await self.api_mgr.call_api(
                images,
                prompt,
                model,
                proxy=self.img_mgr.proxy,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Image generation request failed")
            result = str(exc)

        if not isinstance(result, bytes):
            if self.conf.get("debug_mode", False):
                return [Plain(f"图片生成失败：{result}")]
            return [Plain("图片生成失败，请稍后重试。")]

        result = await self.img_mgr.optimize_output_image(result)
        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        await self.data_mgr.record_usage(uid, gid)
        suffix = f" | 预设：{preset_name}" if preset_name not in {"", "自定义"} else ""
        if self.conf.get("show_model_info", False):
            suffix += f" | 模型：{model}"
        reply: list[Any] = [Image.fromBytes(result)]
        if include_result_text:
            reply.append(Plain(f"\n✅ 生成成功（{elapsed:.1f}s）{suffix}"))
        return reply

    @filter.event_message_type(filter.EventMessageType.ALL, priority=5)
    async def on_preset_request(self, event: AstrMessageEvent, ctx=None):
        """Route image/video prefixes and the longest preset without duplicates."""
        raw_text = self._event_message_text(event)
        text = self._clean_message(raw_text)
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
            try:
                options, images, audios, videos = await self._video_command_inputs(
                    event, prompt, preset_name
                )
                result = await self._generate_video(
                    event,
                    options.prompt,
                    preset_name,
                    images,
                    audios,
                    videos,
                    options=options,
                )
            except (VideoError, OSError, ValueError) as exc:
                result = [Plain(str(exc))]
        else:
            images = await self._extract_images(event, prompt, preset_name)
            result = await self._generate(event, prompt, preset_name, model, images)
        yield event.chain_result(result)

    async def _video_command_inputs(self, event, prompt, preset_name):
        options = parse_video_command(prompt)
        # Explicit image flags are added at their text position by ImageManager;
        # command syntax is stripped only after media extraction.
        sources = list(
            dict.fromkeys([*extract_image_urls_from_text(prompt), *options.images])
        )
        for source in options.images:
            if not self.img_mgr._is_probably_valid_source(source):
                raise VideoError("--image 地址或本地文件无效；未提交生成任务。")
        images = await self._extract_images(
            event, prompt, preset_name, strict=True, extra_sources=sources
        )
        audios, videos = await collect_media(event, self.img_mgr)
        a, v = text_media(prompt)
        for kind, explicit, found, target in (
            ("audio", options.audios, a, audios),
            ("video", options.videos, v, videos),
        ):
            for source in [*explicit, *found]:
                resolved = await resolve_media(source, kind, event)
                if resolved not in target:
                    target.append(resolved)
        options.prompt = strip_media_text(options.prompt)
        for source in extract_image_urls_from_text(options.prompt):
            options.prompt = options.prompt.replace(source, "").strip()
        return options, images, audios, videos

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
            name = "" if preset_name == "自定义" else preset_name
            feedback = str(
                self.video_conf.get(
                    "generating_msg_template", "🎬 正在生成视频 [{preset}]..."
                )
            ).replace("{preset}", name)
            await event.send(event.chain_result([Plain(feedback)]))
        start = monotonic()
        try:
            result = await self.video_mgr.generate(
                prompt, images, audios, videos, duration, options
            )
        except (VideoError, aiohttp.ClientError, OSError, ValueError) as exc:
            logger.warning("Video generation failed: %s", exc)
            # Task IDs and input validation are actionable even outside debug mode.
            return [Plain(str(exc))]
        await self.data_mgr.record_usage(
            event.get_sender_id(), event.get_group_id(), "video"
        )
        reply = [Video.fromFileSystem(str(result.path))]
        if not tool_call:
            suffix = (
                f" | 预设：{preset_name}" if preset_name not in {"", "自定义"} else ""
            )
            if self.video_conf.get("show_model_info", False):
                suffix += f" | 模型：{result.model}"
            reply.append(
                Plain(f"\n✅ 视频生成成功（{monotonic() - start:.1f}s）{suffix}")
            )
        return reply

    @filter.llm_tool(name="generate_video")
    async def generate_video(
        self,
        event: AstrMessageEvent,
        prompt: str = "",
        image_url: str = "",
        audio_url: str = "",
        video_url: str = "",
        duration: int = 0,
        resolution: str = "",
        aspect_ratio: str = "",
        reference_mode: str = "auto",
        seed: int = -2,
        generate_audio: str = "auto",
        use_message_media: bool = True,
    ):
        """按图片、音频、视频的组合自动选择管理员配置的模型生成视频，成功仅发送视频。

        Args:
            prompt(string): 视频提示词；动作迁移/图片音频同步可留空。
            image_url(string): 参考图片 URL、本地路径或 base64:// 数据，多张用空格分隔。留空时可读取消息、引用图片和 @用户头像。
            audio_url(string): 参考音频 URL、标准 Data URL 或本地文件，多段用空格分隔。留空时可读取发送或引用的音频。
            video_url(string): 参考视频 URL、标准 Data URL 或本地文件，多段用空格分隔。留空时可读取发送或引用的视频。
            duration(number): 正整数秒；0 使用提示词或配置时长。10秒以上可自动选择配置的长视频模型。动作迁移跟随参考视频时长。
            resolution(string): 可选分辨率档位，如480p、768p；留空使用所选路线的配置值，不自动降档。
            aspect_ratio(string): 可选16:9、9:16、1:1、4:3、3:4、21:9、adaptive；留空使用配置值。
            reference_mode(string): auto使用配置，reference为普通参考，first_frame需一图，first_last_frame需两图；首尾帧不能带音频或视频。
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
            images = []
            if image_url:
                for source in re.split(r"\s+", image_url.strip()):
                    raw = await self.img_mgr.load_bytes(source)
                    if not raw:
                        raise VideoError("参考图片无法读取；未提交生成任务。")
                    images.append(raw)
            elif use_message_media:
                images = await self._extract_images(event, options.prompt, strict=True)
            audios, videos = (
                await collect_media(
                    event,
                    self.img_mgr,
                    kinds={
                        kind
                        for kind, value in (("audio", audio_url), ("video", video_url))
                        if not value
                    },
                )
                if use_message_media and (not audio_url or not video_url)
                else ([], [])
            )
            for kind, value in (("audio", audio_url), ("video", video_url)):
                if value:
                    sources = [
                        await resolve_media(s, kind, event)
                        for s in re.split(r"\s+", value.strip())
                    ]
                    if kind == "audio":
                        audios = sources
                    else:
                        videos = sources
        except (VideoError, ValueError, TypeError, OSError) as exc:
            yield str(exc)
            return
        self._llm_last_call[uid] = monotonic()
        result = await self._generate_video(
            event,
            options.prompt,
            "自定义",
            images,
            audios,
            videos,
            tool_call=True,
            options=options,
        )
        yield event.chain_result(result)

    @filter.command("生视频", aliases={"生成视频"}, prefix_optional=False)
    async def video_command(self, event: AstrMessageEvent):
        """根据文本、图片、音频和视频自动路由，支持引用、@头像和参数校验。"""
        prompt, preset_name, _ = self._resolve_video_prompt(
            self._event_message_text(event)
        )
        event.stop_event()
        try:
            options, images, audios, videos = await self._video_command_inputs(
                event, prompt, preset_name
            )
            result = await self._generate_video(
                event,
                options.prompt,
                preset_name,
                images,
                audios,
                videos,
                options=options,
            )
        except (VideoError, OSError, ValueError) as exc:
            result = [Plain(str(exc))]
        yield event.chain_result(result)

    @filter.llm_tool(name="generate_image")
    async def generate_image(
        self, event: AstrMessageEvent, prompt: str, image_url: str = ""
    ):
        """使用统一图片生成入口生成或编辑图片。

        Args:
            prompt(string): 图片生成或编辑提示词。
            image_url(string): 可选的参考图片 URL、本地路径或 base64:// 数据。为空时进行文生图，传入后进行图生图。
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
            yield "请提供图片生成或编辑提示词。"
            return

        images: list[bytes] = []
        sources = [
            item.strip()
            for item in re.split(r"[\s,，]+", str(image_url or ""))
            if item.strip()
        ]
        for source in sources:
            image = await self.img_mgr.load_bytes(source)
            if image:
                images.append(image)
        if image_url and not images:
            yield "参考图片无法读取，请提供可访问的图片 URL、本地路径或 base64:// 数据。"
            return

        result = await self._generate(
            event,
            prompt,
            "自定义",
            str(self.conf.get("model", "nano-banana") or "nano-banana"),
            images,
            show_progress=False,
            include_result_text=False,
        )
        yield event.chain_result(result)

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
        yield event.chain_result(
            await self._generate(event, prompt, preset_name, model, images)
        )

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
