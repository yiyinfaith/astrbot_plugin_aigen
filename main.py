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
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import (
    File,
    Image,
    Node,
    Nodes,
    Plain,
    Record,
    Video,
)
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core import AstrBotConfig

from .api_manager import ApiManager, ImageBatch, ImageDownloadError
from .audio_api_manager import AudioApiManager, AudioDownloadError
from .audio_router import (
    AUDIO_TYPES,
    AudioOptions,
    parse_audio_materials,
    select_audio_route,
)
from .billing import BillingError, BillingService, billed_call
from .billing_commands import COMMANDS as BILLING_COMMANDS
from .billing_commands import handle_command
from .config_manager import media_attempts, media_config, migrate_config
from .data_manager import DataManager
from .finance_store import FinanceStore
from .finance_web import FinanceWebUI
from .generation_capabilities import (
    AUDIO,
    IMAGE,
    apply_menu_selection,
    build_menu,
    configured_capabilities,
    supplied,
    validate_options,
)
from .generation_params import (
    detect_aspect_ratio_from_image,
    openai_size_for_generation,
    resolve_image_generation_params,
)
from .image_manager import ImageManager
from .image_router import select_image_route
from .utils import extract_image_urls_from_text, match_keyword_in_text, norm_id
from .video_api_manager import VideoApiManager, VideoDownloadError, video_params
from .video_inputs import collect_media, normalize_media_event, text_media
from .video_router import VideoError, VideoOptions, parse_video_command, select_route


@register(
    "astrbot_plugin_aigen",
    "yiyinfaith",
    "统一图片、视频、音频生成与关键词预设插件",
    "1.4.22",
    "https://github.com/yiyinfaith/astrbot_plugin_aigen",
)
class ImageGeneratorPlugin(Star):
    """Generate images through configured OpenAI-compatible or Gemini APIs."""

    _HELP_COMMANDS: ClassVar[set[str]] = {
        "AI帮助",
        "ai帮助",
        "AI菜单",
        "ai菜单",
    }
    _DRAW_COMMANDS: ClassVar[set[str]] = {"画图", "生图"}
    _VIDEO_COMMANDS: ClassVar[set[str]] = {"生视频", "生成视频", "视频"}

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        data_dir = Path(StarTools.get_data_dir())
        migrate_config(config, data_dir / "config_backups")
        self.root_conf = config
        self.billing = BillingService(
            data_dir / "billing.sqlite3", config.get("global_settings", {})
        )
        self.finance = FinanceStore(
            self.billing.store,
            config.get("global_settings", {}).get("billing_prices", []),
        )
        self.billing.finance = self.finance
        self._finance_api = FinanceWebUI(context, self.finance)
        self.conf = media_config(config, "image")
        self.video_conf = media_config(config, "video")
        self.audio_conf = media_config(config, "audio")
        for name in ("base_url", "api_keys"):
            if not self.audio_conf.get(name):
                self.audio_conf[name] = self.video_conf.get(name, "")
        self.data_mgr = DataManager(
            data_dir, self.conf, self.video_conf, self.audio_conf
        )
        self.img_mgr = ImageManager(self.conf)
        self.api_mgr = ApiManager(self.conf)
        self._image_route_managers = {}
        self.video_mgr = VideoApiManager(self.video_conf, data_dir)
        self.audio_mgr = AudioApiManager(self.audio_conf, data_dir)
        self._cleanup_task = None
        self._llm_last_call: dict[tuple[str, str, str], float] = {}
        self._video_collections: dict[tuple[str, str, str], dict] = {}
        self._video_busy: set[tuple[str, str, str]] = set()
        self._video_generation_tasks: set[asyncio.Task] = set()
        self._video_collection_tasks: set[asyncio.Task] = set()
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
        if getattr(self, "_finance_api", None):
            await self._finance_api.close()
        if getattr(self, "billing", None):
            self.billing.closed = True
        self._video_collections.clear()
        tasks = list(
            self._video_generation_tasks
            | self._video_collection_tasks
            | getattr(getattr(self, "billing", None), "tasks", set())
        )
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
        for manager in (self.api_mgr, self.video_mgr, getattr(self, "audio_mgr", None)):
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
            audio_mgr = getattr(self, "audio_mgr", None)
            if audio_mgr is not None:
                audio_cutoff = (
                    datetime.now(timezone.utc).timestamp()
                    - max(1, int(self.audio_conf.get("cache_hours", 24))) * 3600
                )

                def cleanup_audio(cutoff=audio_cutoff, manager=audio_mgr):
                    for path in manager.data_dir.glob("*"):
                        if path.is_file() and path.stat().st_mtime < cutoff:
                            path.unlink(missing_ok=True)

                try:
                    await asyncio.to_thread(cleanup_audio)
                except OSError as exc:
                    logger.warning("Could not clean audio cache: %s", exc)

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
    def _set_completed_media_units(event: Any, units: int) -> None:
        """Expose actual generated units to cooperating plugins.

        The event extra is optional and does not replace AIGen's authoritative
        monetary billing.  Decision uses it only to reconcile its separate
        per-session safety quota when a batch contains partial failures.
        """

        setter = getattr(event, "set_extra", None)
        if callable(setter):
            try:
                setter("aigen_completed_media_units", max(0, int(units)))
            except (AttributeError, TypeError, ValueError, RuntimeError):
                pass

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
        conf = (
            self.audio_conf
            if kind == "audio"
            else self.video_conf
            if kind == "video"
            else self.conf
        )
        prompt_map = (
            getattr(self.data_mgr, "audio_prompt_map", {})
            if kind == "audio"
            else self.data_mgr.video_prompt_map
            if kind == "video"
            else self.data_mgr.prompt_map
        )
        default_model = "nano-banana" if kind == "image" else ""
        model = str(conf.get("model", default_model) or default_model)
        default_prefix = {"image": "生图", "video": "生视频", "audio": "生音频"}[kind]
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

        matched = match_keyword_in_text(
            clean,
            prompt_map,
            require_boundaries=kind in {"image", "video"},
        )
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
        strict=True,
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
            strict=strict and not is_preset,
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
        self._set_completed_media_units(event, 0)
        uid = norm_id(event.get_sender_id())
        gid = norm_id(event.get_group_id())
        try:
            effective = select_image_route(self.conf, images, model)
            image_options = supplied(image_options or {})
            inherited = supplied(
                {key: effective[key] for key in IMAGE if key in effective}
            )
            image_options = validate_options(
                "image",
                effective,
                {**inherited, **image_options},
                has_reference=bool(images),
            )
            image_options["_infer_prompt_parameters"] = False
            image_options.setdefault(
                "resolution", effective.get("image_resolution", "1K")
            )
        except VideoError as exc:
            return [Plain(str(exc))]
        model = effective["model"]
        manager = self.api_mgr
        if any(
            effective.get(key) != self.conf.get(key)
            for key in ("interface_mode", "base_url", "api_keys")
        ):
            cache_key = (
                bool(images),
                model,
                effective.get("interface_mode"),
                effective.get("base_url"),
                tuple(ApiManager._normalize_keys(effective.get("api_keys"))),
            )
            if not hasattr(self, "_image_route_managers"):
                self._image_route_managers = {}
            manager = self._image_route_managers.get(cache_key)
            if manager is None:
                manager = ApiManager(effective)
                manager._get_session = self.api_mgr._get_session
                if effective.get("api_keys") == self.conf.get("api_keys"):
                    manager.get_key = self.api_mgr.get_key
                self._image_route_managers[cache_key] = manager

        display_name = "" if preset_name in {"", "自定义"} else preset_name
        template = str(
            self.conf.get(
                "generating_msg_template", "🎨 收到请求，正在生成 [{preset}]..."
            )
        )
        feedback = template.replace("{preset}", display_name)
        if not display_name:
            feedback = feedback.replace(" [{preset}]", "").replace("[{preset}]", "")

        source_url = ""
        source_expiry = ""
        try:
            start = datetime.now(timezone.utc)
            billing_parameters = resolve_image_generation_params(
                "",
                effective.get("image_resolution", "1K"),
                detect_aspect_ratio_from_image(images[0], "4:3") if images else "4:3",
                resolution=image_options.get("resolution"),
                aspect_ratio=image_options.get("aspect_ratio"),
            )
            if effective.get("interface_mode") in {"openai_image", "openai_response"}:
                billing_parameters["size"] = openai_size_for_generation(
                    billing_parameters, image_options.get("size")
                )
            result = await billed_call(
                getattr(self, "billing", None),
                event,
                "image",
                {
                    **effective,
                    **billing_parameters,
                    **image_options,
                    "size": billing_parameters["size"],
                },
                {
                    "characters": len(prompt),
                    "images": len(images),
                    "audios": 0,
                    "videos": 0,
                },
                lambda: manager.call_api(
                    images,
                    prompt,
                    model,
                    proxy=self.img_mgr.proxy,
                    image_options=image_options,
                ),
                before_submit=(
                    lambda: event.send(event.chain_result([Plain(feedback)]))
                )
                if show_progress
                else None,
            )
        except BillingError as exc:
            return [Plain(str(exc))]
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

        if not isinstance(result, (bytes, ImageBatch)):
            if self.conf.get("debug_mode", False):
                return [Plain(f"图片生成失败：{result}")]
            return [Plain("图片生成失败，请稍后重试。")]

        artifacts = result.images if isinstance(result, ImageBatch) else (result,)
        prepared = []
        for artifact in artifacts:
            if isinstance(artifact, bytes):
                prepared.append((
                    await self.img_mgr.optimize_output_image(artifact),
                    str(getattr(artifact, "url", "") or ""),
                    str(getattr(artifact, "url_expires_at", "") or ""),
                ))
            else:
                prepared.append(artifact)
        self._set_completed_media_units(
            event,
            sum(
                isinstance(artifact, (tuple, ImageDownloadError))
                for artifact in prepared
            ),
        )
        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        if any(isinstance(artifact, (tuple, ImageDownloadError)) for artifact in prepared):
            try:
                await self.data_mgr.record_usage(uid, gid)
            except Exception:  # noqa: BLE001
                logger.exception("Could not record completed image usage")
        suffix = f" | 预设：{preset_name}" if preset_name not in {"", "自定义"} else ""
        if self.conf.get("show_model_info", False):
            suffix += f" | 模型：{model}"
        reply: list[Any] = []
        final_image = max(
            (index for index, artifact in enumerate(prepared) if isinstance(artifact, tuple)),
            default=-1,
        )
        for index, artifact in enumerate(prepared):
            if isinstance(artifact, tuple):
                raw, source_url, source_expiry = artifact
                caption = include_result_text and index == final_image
                if dispatch_result:
                    reply.extend(await self._deliver_image_result(
                        event, raw, source_url, source_expiry, caption, suffix,
                        f"\n✅ 生成成功（{elapsed:.1f}s）{suffix}",
                        send_fallback_message=not tool_call,
                    ))
                else:
                    reply.append(Image.fromBytes(raw))
                    if caption:
                        reply.append(Plain(f"\n✅ 生成成功（{elapsed:.1f}s）{suffix}"))
            elif isinstance(artifact, ImageDownloadError):
                reply.extend(await self._deliver_image_fallback(
                    event, artifact.url, artifact.url_expires_at,
                    send_message=dispatch_result and not tool_call,
                ))
            else:
                reply.append(Plain(f"第 {index + 1} 张图片处理失败：{artifact}"))
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
        attempts = media_attempts(self.conf, "image")
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

    def _register_user(self, event: AstrMessageEvent) -> str:
        """Register only this plugin's caller, returning a user-facing error."""
        service = getattr(self, "billing", None)
        if service is None:
            return ""
        try:
            service.register_user(event)
        except BillingError as exc:
            return str(exc)
        return ""

    @staticmethod
    def _tool_cooldown_key(event: AstrMessageEvent, kind: str) -> tuple[str, str, str]:
        """Isolate tools and platform accounts while sharing a user's chats."""
        platform = str(getattr(event, "get_platform_name", lambda: "")() or "").strip()
        return kind, platform, norm_id(event.get_sender_id())

    async def _dispatch_billing_command(self, event: AstrMessageEvent, text: str):
        """Handle one financial message once across listener and command routes."""
        if self._terminating or event.get_extra("aigen_billing_command_handled", False):
            return None
        event.set_extra("aigen_billing_command_handled", True)
        event.stop_event()
        response = handle_command(self.billing, event, text)
        if len(response) > 1000:
            await self._send_settings_menu(event, [response], title="余额与卡密")
            return None
        return event.chain_result([Plain(response)])

    async def _registered_billing_command(
        self, event: AstrMessageEvent, command: str, arguments: str = ""
    ):
        """Preserve raw validation, with parsed arguments for custom wake prefixes."""
        text = self._clean_message(self._event_message_text(event))
        first = text.split(maxsplit=1)[0] if text else ""
        if BILLING_COMMANDS.get(first) != command:
            text = (command + " " + arguments).strip()
        return await self._dispatch_billing_command(event, text)

    @filter.command("兑换AI卡密", alias={"兑换ai卡密"}, prefix_optional=False)
    async def redeem_card_command(self, event: AstrMessageEvent, card: str = ""):
        """兑换 AIGen 卡密并充值当前用户余额。用法：/兑换AI卡密 <卡密>，建议私聊兑换。"""
        result = await self._registered_billing_command(event, "兑换AI卡密", card)
        if result is not None:
            yield result

    @filter.command("AI余额", alias={"ai余额"}, prefix_optional=False)
    async def balance_command(self, event: AstrMessageEvent):
        """查询当前用户的 AIGen 余额和 AstrBot 用户 ID。用法：/AI余额。"""
        result = await self._registered_billing_command(event, "AI余额")
        if result is not None:
            yield result

    @filter.command("AI账单", alias={"ai账单"}, prefix_optional=False)
    async def my_orders_command(self, event: AstrMessageEvent, page: int = 1):
        """查询当前用户的 AIGen 生成账单。用法：/AI账单 [页码]，默认第 1 页。"""
        result = await self._registered_billing_command(event, "AI账单", str(page))
        if result is not None:
            yield result

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_preset_request(self, event: AstrMessageEvent, ctx=None):
        """Route image/video prefixes and the longest preset without duplicates."""
        raw_text = self._event_message_text(event)
        text = self._clean_message(raw_text)
        key = self._video_session_key(event)
        if self._terminating:
            return
        if any(
            text == command or text.startswith(command + " ")
            for command in self._HELP_COMMANDS
        ):
            return
        if (
            text.split(maxsplit=1)[:1]
            and text.split(maxsplit=1)[0] in BILLING_COMMANDS
            and self._has_astrbot_prefix(event, raw_text)
        ):
            result = await self._dispatch_billing_command(event, text)
            if result is not None:
                yield result
            return
        audio_argument = self._resolve_audio_command(raw_text)
        if audio_argument is not None and (
            self._has_astrbot_prefix(event, raw_text)
            or not getattr(self, "audio_conf", {}).get(
                "custom_prompt_need_prefix", True
            )
        ):
            event.stop_event()
            result = await self._start_audio_request(event, audio_argument)
            if result:
                yield event.chain_result(result)
            return
        collection = self._video_collections.get(key)
        image_start = self._resolve_preset_prompt(raw_text, "image")
        if self._has_astrbot_prefix(event, raw_text) and (
            any(
                text == cmd or text.startswith(cmd + " ") for cmd in self._DRAW_COMMANDS
            )
            or (image_start is not None and image_start[1] == "自定义")
        ):
            collection = None
        control = text in {
            "生成",
            "取消",
            "设置",
            "设置重置",
            "清除失败项",
        } and self._has_astrbot_prefix(event, raw_text)
        if collection or control:
            event.stop_event()
            restart = self._resolve_preset_prompt(raw_text, "video")
            is_start = any(
                text == cmd or text.startswith(cmd + " ")
                for cmd in self._VIDEO_COMMANDS
            ) and self._has_astrbot_prefix(event, raw_text)
            if restart is not None and restart[1] == "自定义":
                is_start = is_start or (
                    self._has_astrbot_prefix(event, raw_text)
                    or not self.video_conf.get("custom_prompt_need_prefix", True)
                )
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
                (k, self._resolve_preset_prompt(text, k))
                for k in ("image", "video", "audio")
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
        conf = (
            self.audio_conf
            if kind == "audio"
            else self.video_conf
            if kind == "video"
            else self.conf
        )
        if conf.get(
            need_prefix_key, default_need_prefix
        ) and not self._has_astrbot_prefix(event, raw_text):
            return
        event.stop_event()
        if kind == "audio":
            result = await self._collect_video_request(
                event,
                text,
                preset_name,
                start=True,
                kind="audio",
                audio_mode="提示词",
                audio_type="reference_to_audio",
            )
        elif kind == "video":
            result = await self._collect_video_request(
                event, prompt, preset_name, start=True
            )
        else:
            if error := self._register_user(event):
                yield event.chain_result([Plain(error)])
                return
            try:
                images = await self._extract_images(event, prompt, preset_name)
            except (ValueError, OSError) as exc:
                yield event.chain_result([Plain(str(exc))])
                return
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

    def _resolve_audio_command(self, raw: str) -> str | None:
        """Match one audio prefix on a token boundary.

        Args:
            raw: Text from the incoming message.

        Returns:
            The command arguments, or None when this is not an audio command.
        """
        text = self._clean_message(raw)
        prefixes = getattr(self, "audio_conf", {}).get("extra_prefix", ["生音频"])
        if isinstance(prefixes, str):
            prefixes = [prefixes]
        for prefix in sorted(
            {"生音频", "音频", *(str(p).strip().lstrip("/") for p in prefixes or [])},
            key=len,
            reverse=True,
        ):
            if prefix and (text == prefix or text.startswith(prefix + " ")):
                return text[len(prefix) :].strip()
        return None

    async def _start_audio_request(self, event, argument: str) -> list[Any]:
        """Require an intent and accept one optional audio type.

        Args:
            event: Incoming AstrBot message event.
            argument: One required intent and an optional type, separated by spaces.

        Returns:
            A selection hint, validation error, or collection start result.
        """
        if error := self._register_user(event):
            return [Plain(error)]
        parts = argument.split()
        if not parts:
            return [
                Plain(
                    "请选择一个参数：/生音频 朗读 或 /生音频 提示词。第二个参数可选音频类型，参数之间用空格。"
                )
            ]
        if parts[0] not in {"朗读", "提示词"}:
            return [Plain("生音频第一个参数只能从“朗读”和“提示词”里面选。")]
        if len(parts) > 2 or (len(parts) == 2 and parts[1] not in AUDIO_TYPES):
            return [
                Plain(
                    "第二个参数可选：音色克隆、参考音频生成、音频编辑、语音编辑、音色转换。启动后再发送正文，参数之间用空格。"
                )
            ]
        requested = AUDIO_TYPES[parts[1]] if len(parts) == 2 else ""
        if (
            requested in {"reference_to_speech", "speech_editing"}
            and parts[0] != "朗读"
        ):
            return [Plain("音色克隆、语音编辑请使用 /生音频 朗读。")]
        if (
            requested in {"reference_to_audio", "audio_editing"}
            and parts[0] != "提示词"
        ):
            return [Plain("参考音频生成、音频编辑请使用 /生音频 提示词。")]
        return await self._collect_video_request(
            event, start=True, kind="audio", audio_mode=parts[0], audio_type=requested
        )

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

    async def _expire_video_collection(self, key, collection):
        """Cancel an overdue collection and proactively notify its chat.

        Args:
            key: Platform, chat and sender identity of this collection.
            collection: The exact collection owned by this timer.
        """
        await asyncio.sleep(max(0, collection["deadline"] - monotonic()))
        # A finish received before the deadline may be waiting for an upload.
        # Let that validation finish; invalid submissions keep the same deadline.
        await collection["finish_idle"].wait()
        if self._terminating or self._video_collections.get(key) is not collection:
            return
        self._video_collections.pop(key, None)
        if collection["timer"] is not asyncio.current_task():
            collection["timer"].cancel()
        label = "音频" if collection.get("kind") == "audio" else "视频"
        try:
            await self.context.send_message(
                key[0],
                MessageChain().message(
                    f"材料收集已超时（{collection['timeout']}秒），已取消本次生{label}。请重新发送 /生{label} 开始。"
                ),
            )
        except Exception:  # noqa: BLE001
            logger.exception("Could not send video collection timeout notice")

    def _collection_settings_config(
        self, collection: dict, parameters: dict | None = None
    ) -> dict:
        """Resolve a menu route before all required materials have arrived."""
        if collection["kind"] == "audio":
            options = parse_audio_materials(
                collection["text"], collection["audio_mode"]
            )
            options.route = collection["audio_type"]
            options.parameters = (
                collection["parameters"] if parameters is None else parameters
            )
            _, effective = select_audio_route(
                self.audio_conf, options, collection["audios"], validate_materials=False
            )
        else:
            options = parse_video_command(collection["text"])
            overrides = {
                **options.overrides(),
                **(collection["parameters"] if parameters is None else parameters),
            }
            _, effective = select_route(
                self.video_conf,
                collection["images"],
                collection["audios"],
                collection["videos"],
                overrides.get("duration", self.video_conf.get("duration", 5)),
                overrides,
                options.prompt,
                validate_materials=False,
            )
        return effective

    async def _send_settings_menu(
        self, event, sections: list[str], title="生成参数设置"
    ):
        """Send one QQ merged forward or one complete plain fallback message."""
        platform = getattr(event, "get_platform_name", lambda: "")()
        if platform == "aiocqhttp":
            try:
                nodes = [
                    Node(
                        uin=self._bot_id(event),
                        name=title,
                        content=[Plain(section)],
                    )
                    for section in sections
                ]
                await event.send(event.chain_result([Nodes(nodes=nodes)]))
                return
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Merged settings forward failed; using one plain message"
                )
        await event.send(event.chain_result([Plain("\n\n".join(sections))]))

    async def _collect_video_request(
        self,
        event,
        prompt=None,
        preset_name="自定义",
        *,
        start=False,
        kind="video",
        audio_mode="朗读",
        audio_type="",
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
        if error := self._register_user(event):
            return [Plain(error)]
        if key in self._video_busy:
            return [Plain("已有视频任务正在生成，请等待结果，勿重复提交。")]
        raw = self._event_message_text(event)
        control = (
            self._clean_message(raw) if self._has_astrbot_prefix(event, raw) else ""
        )
        collection = self._video_collections.get(key)
        if not start and collection is not None:
            kind = collection.get("kind", "video")
        conf = self.audio_conf if kind == "audio" else self.video_conf
        label = "音频" if kind == "audio" else "视频"
        if not conf.get("enabled", True):
            return [Plain(f"{label}生成功能已关闭。")]
        finish = control == "生成" and not start
        if start:
            # Reserving the collection below happens before the first await.
            # A group therefore has one material owner across video and audio.
            for owner, active in self._video_collections.items():
                # AstrBot may give each member a different conversation ID.
                # Group ownership uses the platform instance plus group ID.
                same_chat = (
                    owner[1] == key[1]
                    and owner[0].split(":", 1)[0] == key[0].split(":", 1)[0]
                    if key[1]
                    else owner == key
                )
                if same_chat and owner != key:
                    remaining = max(0, int(active["deadline"] - monotonic()))
                    return [
                        Plain(
                            f"本群已有用户正在录入材料，请等待其提交、取消或超时后再开始（剩余约{remaining}秒）。"
                        )
                    ]
            if collection is not None:
                collection["timer"].cancel()
            try:
                timeout = max(1, int(conf.get("collection_timeout_seconds", 120)))
            except (ValueError, TypeError, OverflowError):
                timeout = 120
            collection = {
                "lock": asyncio.Lock(),
                "text": [],
                "images": [],
                "audios": [],
                "videos": [],
                "errors": [],
                "size": 0,
                "timeout": timeout,
                "deadline": monotonic() + timeout,
                "pending_finishes": 0,
                "finish_idle": asyncio.Event(),
                "preset": preset_name,
                "kind": kind,
                "audio_mode": audio_mode,
                "audio_type": audio_type,
                "type_choices": [],
                "parameters": {},
                "settings_menu": None,
            }
            collection["finish_idle"].set()
            self._video_collections[key] = collection
            timer = asyncio.create_task(self._expire_video_collection(key, collection))
            collection["timer"] = timer
            self._video_collection_tasks.add(timer)
            timer.add_done_callback(self._video_collection_tasks.discard)
        elif collection is None:
            return [
                Plain(
                    f"没有正在收集的材料，或本次收集已超时取消；请先发送 /生{label}。"
                )
            ]
        elif monotonic() >= collection["deadline"]:
            if not collection["pending_finishes"]:
                await self._expire_video_collection(key, collection)
            return [Plain(f"材料收集已超时，请重新发送 /生{label}。")]
        if finish:
            collection["pending_finishes"] += 1
            collection["finish_idle"].clear()
        try:
            async with collection["lock"]:
                # A finish command may have been queued while an earlier upload was
                # resolving. Verify ownership again before accepting/submitting it.
                if self._video_collections.get(key) is not collection:
                    return [Plain("材料已提交或取消，请等待结果或重新发送 /生视频。")]
                if start:
                    try:
                        await event.send(
                            event.chain_result(
                                [
                                    Plain(
                                        f"请发送材料，录入阶段发送“/设置”可设置详细参数，最后发送“/生成”开始生成{label}，{collection['timeout']}秒超时"
                                    )
                                ]
                            )
                        )
                    except BaseException:
                        if self._video_collections.get(key) is collection:
                            self._video_collections.pop(key, None)
                        timer.cancel()
                        raise
                if control == "取消" and not start:
                    self._video_collections.pop(key, None)
                    collection["timer"].cancel()
                    return [Plain("已取消本次材料收集。")]
                if control == "清除失败项" and not start:
                    count = len(collection["errors"])
                    collection["errors"].clear()
                    return [Plain(
                        f"已清除 {count} 项失败记录，已成功录入的材料和参数均保留。"
                        "请重新发送失败的材料，最后发送 /生成；录入时限不变。"
                        if count else "没有需要清除的失败项，已录入材料保持不变，可继续发送材料或 /生成。"
                    )]
                if not start and control in {"设置", "设置重置"}:
                    if control == "设置重置":
                        collection["parameters"] = {}
                        collection["settings_menu"] = None
                        return [
                            Plain("已清除本次自定义参数，继续发送材料，最后 /生成。")
                        ]
                    try:
                        effective = self._collection_settings_config(collection)
                        sections, mapping = build_menu(
                            kind,
                            effective,
                            collection["parameters"],
                            has_reference=bool(
                                collection["images"]
                                or collection["audios"]
                                or collection["videos"]
                            ),
                        )
                        collection["settings_menu"] = mapping
                        await self._send_settings_menu(event, sections)
                        return []
                    except (VideoError, ValueError, TypeError) as exc:
                        return [Plain(str(exc))]
                if collection["settings_menu"] is not None and not start:
                    if finish:
                        collection["settings_menu"] = None
                    else:
                        try:
                            chosen = apply_menu_selection(
                                raw.strip(),
                                collection["settings_menu"],
                                collection["parameters"],
                            )
                            if chosen is not None:
                                effective = self._collection_settings_config(
                                    collection, chosen
                                )
                                validate_options(
                                    kind,
                                    effective,
                                    chosen,
                                    has_reference=bool(
                                        collection["images"]
                                        or collection["audios"]
                                        or collection["videos"]
                                    ),
                                )
                                collection["parameters"] = chosen
                            collection["settings_menu"] = None
                            return [
                                Plain(
                                    "已退出设置，保留原设置。继续发送材料，最后 /生成。"
                                    if chosen is None
                                    else "参数已保存。继续发送材料，最后 /生成；需要调整可再次 /设置。"
                                )
                            ]
                        except (VideoError, ValueError, TypeError) as exc:
                            return [Plain(str(exc))]
                selecting_type = kind == "audio" and bool(collection["type_choices"])
                if selecting_type:
                    choice = raw.strip()
                    if choice not in collection["type_choices"]:
                        return [
                            Plain(
                                "请直接回复音频类型（不用 /）："
                                + "、".join(collection["type_choices"])
                                + "；也可 /取消。"
                            )
                        ]
                    collection["audio_type"] = AUDIO_TYPES[choice]
                    collection["type_choices"] = []
                text = str(prompt or "") if start else "" if finish else raw.strip()
                if selecting_type:
                    text = ""
                if text:
                    if sum(map(len, collection["text"])) + len(text) > 2_000_000:
                        collection["errors"].append(
                            "文字材料超过 200 万字符，本条文字未录入，请减少文字后重新发送。"
                        )
                    else:
                        collection["text"].append(text)
                try:
                    if selecting_type:
                        images, audios, videos = [], [], []
                    else:
                        _, images, audios, videos = await self._video_command_inputs(
                            event, "", "自定义"
                        )
                    if kind == "audio" and (images or videos):
                        raise VideoError(
                            "生音频只接收文字、QQ 语音或音频文件，不支持图片和视频。"
                        )
                    added_size = sum(
                        len(src)
                        for bucket in (images, audios, videos)
                        for src in bucket
                    )
                    if collection["size"] + added_size > 100 * 1024 * 1024:
                        raise VideoError(
                            "材料总量超过 100MB，本条媒体未录入，请使用较小的文件，或 /取消 后减少材料。"
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
                if (
                    self._terminating
                    or self._video_collections.get(key) is not collection
                ):
                    return []
                if not finish and not selecting_type:
                    return []
                if collection["errors"]:
                    return [
                        Plain(
                            "材料读取失败，未提交生成任务：\n"
                            + "\n".join(collection["errors"])
                            + "\n发送 /清除失败项 可清除失败记录，已成功录入的材料和参数会保留；随后重新发送失败的材料，再 /生成。"
                            + "\n如需全部重来，请发送 /取消。"
                        )
                    ]
                try:
                    merged = " ".join(collection["text"])
                    if kind == "audio":
                        options = parse_audio_materials(
                            collection["text"], collection["audio_mode"]
                        )
                        options.route = collection["audio_type"]
                        options.parameters = dict(collection["parameters"])
                        if not options.route and collection["audios"]:
                            count = len(collection["audios"])
                            if not options.prompt and count == 2:
                                choices = ["音色转换"]
                            elif options.prompt and count <= 3:
                                choices = (
                                    ["音色克隆"]
                                    if options.mode == "朗读"
                                    else ["参考音频生成"]
                                )
                                if count == 1:
                                    choices.append(
                                        "语音编辑"
                                        if options.mode == "朗读"
                                        else "音频编辑"
                                    )
                            else:
                                raise VideoError(
                                    "请提供文字及 1–3 段参考音频；音色转换只输入两段音频。"
                                )
                            collection["type_choices"] = choices
                            return [
                                Plain(
                                    "请选择音频类型（直接回复名称，不用 /）："
                                    + "、".join(choices)
                                    + f"。剩余约{max(0, int(collection['deadline'] - monotonic()))}秒，/取消 可取消。"
                                )
                            ]
                        self.audio_mgr.prepare(options, collection["audios"])
                    else:
                        options = parse_video_command(collection["text"])
                        options.parameters = dict(collection["parameters"])
                    if (
                        kind == "video"
                        and options.reference_mode
                        in {
                            "first_frame",
                            "last_frame",
                            "first_last_frame",
                            "motion_transfer",
                            "audio_sync",
                        }
                        and not re.match(
                            r"^(首帧|尾帧|首尾帧|动作迁移|音频同步)(?:\s+|$)",
                            merged.strip(),
                        )
                    ):
                        raise VideoError(
                            "特殊类别请在文字材料开头明确填写“首尾帧”“首帧”“尾帧”“动作迁移”或“音频同步”关键词。"
                        )
                    if kind == "video" and (
                        options.images
                        or options.audios
                        or options.videos
                        or extract_image_urls_from_text(merged)
                        or any(text_media(merged))
                    ):
                        raise VideoError(
                            "请直接发送图片、QQ 语音、视频或媒体文件，不接受媒体 URL 参数。"
                        )
                    if kind == "video":
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
                collection["timer"].cancel()
                self._video_busy.add(key)
        finally:
            if finish:
                collection["pending_finishes"] -= 1
                if not collection["pending_finishes"]:
                    collection["finish_idle"].set()
        task = asyncio.current_task()
        self._video_generation_tasks.add(task)
        try:
            if kind == "audio":
                return await self._generate_audio(event, options, collection["audios"])
            return await self._generate_video(
                event,
                options.prompt,
                collection["preset"],
                collection["images"],
                collection["audios"],
                collection["videos"],
                dispatch_result=True,
                options=options,
                prompt_segments=options.prompt_segments,
            )
        finally:
            self._video_busy.discard(key)
            self._video_generation_tasks.discard(task)

    async def _generate_audio(
        self, event, options: AudioOptions, audios: list[str], *, tool_call=False
    ) -> list[Any]:
        """Generate once, then deliver audio without a success caption.

        Args:
            event: The event receiving progress and the final artifact.
            options: Parsed audio intent and exact merged text.
            audios: Ordered audio materials.
            tool_call: Whether the LLM owns progress/error narration.

        Returns:
            An error chain, or no elements after successful delivery.
        """
        self._set_completed_media_units(event, 0)
        try:
            prepared = self.audio_mgr.prepare(options, audios)
            label = "文字" if options.mode == "朗读" else "提示词"
            feedback = f"共收到0图片，{len(audios)}音频，0视频，{options.prompt_segments}段{label}，生成中"
            effective = prepared[1].config if prepared else {}
            result = await billed_call(
                getattr(self, "billing", None),
                event,
                "audio",
                effective,
                {
                    "characters": len(effective.get("_audio_prompt", options.prompt)),
                    "images": 0,
                    "audios": len(audios),
                    "videos": 0,
                },
                lambda: self.audio_mgr.generate(options, audios),
                before_submit=(
                    lambda: event.send(event.chain_result([Plain(feedback)]))
                )
                if not tool_call
                else None,
            )
        except AudioDownloadError as exc:
            return self._audio_fallback(exc.url)
        except (VideoError, aiohttp.ClientError, OSError, ValueError) as exc:
            logger.warning("Audio generation failed: %s", exc)
            return [Plain(str(exc))]
        except Exception:  # noqa: BLE001
            logger.exception("Unexpected audio generation failure")
            return [
                Plain(
                    "音频生成发生内部错误，请联系管理员；勿立即重复提交，以免重复付费。"
                )
            ]
        try:
            await self.data_mgr.record_usage(
                event.get_sender_id(), event.get_group_id(), "audio"
            )
        except Exception:  # noqa: BLE001
            logger.exception("Could not record audio usage; delivering completed audio")
        self._set_completed_media_units(event, 1)
        path = result.path
        if path.suffix.lower() != ".wav":
            try:
                from astrbot.core.utils.media_utils import convert_audio_format

                converted = path.with_name(path.stem + "_delivery.wav")
                path = Path(
                    await convert_audio_format(str(path), "wav", str(converted))
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Could not convert completed audio; delivering its original file"
                )
                try:
                    await event.send(
                        event.chain_result(
                            [File(file=str(result.path), name=result.path.name)]
                        )
                    )
                    return []
                except Exception:  # noqa: BLE001
                    return self._audio_fallback(result.url)
        attempts = media_attempts(self.audio_conf, "audio")
        for attempt in range(attempts):
            try:
                await event.send(
                    event.chain_result([Record(file=str(path), url=str(path))])
                )
                return []
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Completed audio delivery attempt %d failed", attempt + 1
                )
        try:
            await event.send(
                event.chain_result([File(file=str(result.path), name=result.path.name)])
            )
            return []
        except Exception:  # noqa: BLE001
            return self._audio_fallback(result.url)

    @staticmethod
    def _audio_fallback(url: str) -> list[Any]:
        """Return the original artifact link after download or delivery fails."""
        if not url:
            return [
                Plain(
                    "音频已生成，但下载或发送失败，接口未提供公网链接；请联系管理员获取原产物，勿重复生成。"
                )
            ]
        return [
            Plain(
                f"音频下载或发送失败，使用公网链接：\n{url}\n链接有效期以上游为准，请下载原结果，勿重复生成。"
            )
        ]

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
        prompt_segments=None,
    ) -> list[Any]:
        self._set_completed_media_units(event, 0)
        if not self.video_conf.get("enabled", True):
            return [Plain("视频生成功能已关闭。")]
        try:
            prepared = self.video_mgr.prepare(
                prompt, images, audios or [], videos or [], duration, options
            )
        except (VideoError, ValueError, TypeError) as exc:
            return [Plain(str(exc))]
        if not tool_call:
            if prompt_segments is None:
                prompt_segments = int(bool(str(prompt or "").strip()))
            feedback = (
                f"共收到{len(images)}图片，{len(audios or [])}音频，"
                f"{len(videos or [])}视频，{prompt_segments}段提示词，生成中"
            )
        try:
            effective = prepared[1].config if prepared else {}
            if prepared:
                parameters = video_params(
                    "", effective, options.duration if options else duration
                )
                effective = {
                    **effective,
                    **parameters,
                    "aspect_ratio": parameters["ratio"],
                }
            result = await billed_call(
                getattr(self, "billing", None),
                event,
                "video",
                effective,
                {
                    "characters": len(prompt),
                    "images": len(images),
                    "audios": len(audios or []),
                    "videos": len(videos or []),
                },
                lambda: self.video_mgr.generate(
                    prompt, images, audios, videos, duration, options
                ),
                before_submit=(
                    lambda: event.send(event.chain_result([Plain(feedback)]))
                )
                if not tool_call
                else None,
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
        self._set_completed_media_units(event, 1)
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
        attempts = media_attempts(self.video_conf, "video")
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

    @filter.llm_tool(name="get_generation_capabilities")
    async def get_generation_capabilities(
        self, event: AstrMessageEvent, media_type: str = "all"
    ):
        """查询 AIGen（AI多模态生成器，astrbot_plugin_aigen）插件的 generate_image、generate_video、generate_audio 三个工具的用法、参数与模型能力。

        image 对应本插件的 generate_image（生图），video 对应 generate_video（生视频），
        audio 对应 generate_audio（生音频），all 查询这三个工具。返回各工具适用的媒体路线、
        已配置模型、可选参数、取值范围及有效默认值。仅供上述三个工具使用，不代表其他插件的
        同类工具或 AstrBot 全部模型的能力；不创建生成任务、不扣费。不确定参数或模型能力时先查询，
        再把查询到的参数传给对应的生成工具。未知模型仅返回协议支持范围，不保证模型本身支持。

        Args:
            media_type(string): image 查询 generate_image；video 查询 generate_video；audio 查询 generate_audio；all 查询 AIGen 的这三个工具，默认 all。
        """
        if error := self._register_user(event):
            yield error
            return
        if media_type not in {"image", "video", "audio", "all"}:
            yield "媒体类型必须为 image、video、audio 或 all。"
            return
        configs = {
            "image": self.conf,
            "video": self.video_conf,
            "audio": self.audio_conf,
        }
        try:
            yield (
                "AIGen（AI多模态生成器，astrbot_plugin_aigen）工具能力：\n"
                + "\n".join(
                    f"适用工具：generate_{kind}\n" + configured_capabilities(kind, conf)
                    for kind, conf in configs.items()
                    if media_type in {kind, "all"}
                )
            )
        except (VideoError, ValueError, TypeError) as exc:
            yield str(exc)

    @filter.llm_tool(name="generate_video")
    async def generate_video(
        self,
        event: AstrMessageEvent,
        prompt: str = "",
        duration: float = 0,
        resolution: str = "",
        aspect_ratio: str = "",
        reference_mode: str = "auto",
        seed: int = -2,
        generate_audio: bool | None = None,
        use_message_media: bool = True,
        size: str = "",
        watermark: bool | None = None,
        negative_prompt: str = "",
        prompt_extend: bool | None = None,
        camera_fixed: bool | None = None,
        return_last_frame: bool | None = None,
        service_tier: str = "",
        draft: bool | None = None,
    ):
        """按图片、音频、视频的组合自动选择管理员配置的模型生成视频。

        视频由插件直接发送，实际结果返回 Agent，保留后续对话流程。

        媒体不需要填写 URL；工具会读取当前消息和引用中的 QQ 图片、文件、语音和视频。
        用户说“图生视频”“让图中人物动起来”或“跳舞”，应选 reference（或省略），一图和多图都一样。
        first_frame 表示把图片固定为视频第一帧，first_last_frame 表示固定第一帧和最后一帧；
        不要仅因图片数量选择这两种模式。根据用户意图合理选择，未配置的路线可改用普通 reference。

        Args:
            prompt(string): 常规视频需要提示词；仅动作迁移、音频同步特殊模式留空。
            duration(number): 通常为正整数秒；0 使用配置时长。10秒以上可选择长视频模型。动作迁移支持1–3600的小数秒，仅声明预扣时长，不裁剪视频。
            resolution(string): 可选分辨率档位，如480p、768p；留空使用所选路线的配置值，不自动降档。
            aspect_ratio(string): 可选16:9、9:16、1:1、4:3、3:4、21:9、adaptive；留空使用配置值。
            reference_mode(string): 可选，默认auto。普通图生视频、人物运动/跳舞用reference或省略；任何图片数量都不自动触发帧模式。明确首帧/尾帧用first_frame/last_frame（恰好一图），首尾帧用first_last_frame（恰好两图），都不能混音频/视频。动作迁移用motion_transfer（恰好一图一视频），自动对口型用audio_sync（恰好一图一音频）；后两种必须留空prompt。
            seed(number): -2使用配置，-1不发送种子，AutoDL最大999999999999999，Seedance最大2147483647；最小值由模型决定。
            generate_audio(boolean): 省略使用配置；true/false控制Seedance同时生成音频。
            use_message_media(boolean): 默认true，缺少对应显式媒体时读取当前/引用消息和@头像；纯文生且不希望使用附带媒体时设false。
            size(string): OpenAI 视频精确尺寸，可用值请先查询能力工具；不支持的模型会报错。
            watermark(boolean): 可选水印开关，省略继承配置。
            negative_prompt(string): DashScope 或自定义模板的负面提示词。
            prompt_extend(boolean): DashScope 智能扩写开关。
            camera_fixed(boolean): Seedance 固定镜头开关。
            return_last_frame(boolean): Seedance 返回尾帧开关，插件仍仅发送视频。
            service_tier(string): Seedance 服务档位 default 或 flex。
            draft(boolean): Seedance 草稿模式开关。
        """
        self._set_completed_media_units(event, 0)
        if error := self._register_user(event):
            yield error
            return
        if not self.video_conf.get("enable_llm_tool", True):
            yield "视频生成函数工具已停用。"
            return
        if self._terminating:
            yield "视频插件正在重载，请稍后再试。"
            return
        uid = self._tool_cooldown_key(event, "video")
        cooldown = max(0, int(self.video_conf.get("llm_cooldown_seconds", 60)))
        claimed_at = monotonic()
        elapsed = claimed_at - self._llm_last_call.get(uid, float("-inf"))
        if elapsed < cooldown:
            yield f"视频工具冷却中，请 {int(cooldown - elapsed) + 1} 秒后再试。"
            return
        # Reserve before any media I/O so concurrent calls cannot both pass.
        self._llm_last_call[uid] = claimed_at
        task = asyncio.current_task()
        self._video_generation_tasks.add(task)
        try:
            try:
                options = VideoOptions(
                    prompt=str(prompt or ""),
                    duration=duration,
                    resolution=resolution,
                    aspect_ratio=aspect_ratio,
                    reference_mode=reference_mode,
                    seed=seed,
                    generate_audio=generate_audio,
                    parameters=supplied(
                        {
                            "size": size,
                            "watermark": watermark,
                            "negative_prompt": negative_prompt,
                            "prompt_extend": prompt_extend,
                            "camera_fixed": camera_fixed,
                            "return_last_frame": return_last_frame,
                            "service_tier": service_tier,
                            "draft": draft,
                        }
                    ),
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
                if self._llm_last_call.get(uid) == claimed_at:
                    self._llm_last_call.pop(uid, None)
                yield str(exc)
                return
            if self._terminating:
                yield "视频插件正在重载，请稍后再试。"
                return
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
            if result:
                # Strings go back to the LLM; media is delivered separately.
                yield "\n".join(part.text for part in result if isinstance(part, Plain))
            else:
                yield '{"status":"completed","media_type":"video","delivery":"sent"}'
        finally:
            self._video_generation_tasks.discard(task)

    @filter.command("生视频", alias={"生成视频", "视频"}, prefix_optional=False)
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

    @filter.command("生音频", alias={"音频"}, prefix_optional=False)
    async def audio_command(self, event: AstrMessageEvent):
        """Collect audio materials after a required reading/prompting selector.

        Args:
            event: Incoming AstrBot message event.
        """
        event.stop_event()
        argument = self._resolve_audio_command(self._event_message_text(event))
        result = await self._start_audio_request(event, argument or "")
        if result:
            yield event.chain_result(result)

    @filter.command("清除失败项", prefix_optional=False)
    async def clear_failed_items_command(self, event: AstrMessageEvent):
        """清除本人本次视频或音频录入中的失败记录。用法：/清除失败项，保留成功材料和参数。"""
        event.stop_event()
        result = await self._collect_video_request(event)
        if result:
            yield event.chain_result(result)

    @filter.llm_tool(name="generate_audio")
    async def generate_audio(
        self,
        event: AstrMessageEvent,
        text: str,
        mode: str = "朗读",
        audio_type: str = "",
        voice: str = "",
        speed: float | None = None,
        response_format: str = "",
        instructions: str = "",
        language: str = "",
        volume: float | None = None,
        pitch: int | None = None,
        sample_rate: int | None = None,
        bitrate: int | None = None,
        channels: int | None = None,
        emotion: str = "",
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        emo_control_method: str = "",
        emo_afraid: float | None = None,
        emo_angry: float | None = None,
        emo_calm: float | None = None,
        emo_disgusted: float | None = None,
        emo_happy: float | None = None,
        emo_melancholic: float | None = None,
        emo_sad: float | None = None,
        emo_random: bool | None = None,
        emo_surprised: str = "",
        use_message_media: bool = True,
    ):
        """Generate speech or descriptive audio from text and attached audio.

        Audio is sent directly; the actual result is returned to the Agent
        without ending its conversation loop.

        Args:
            text(string): Exact reading text, or the audio generation prompt.
            mode(string): 朗读 for TTS; 提示词 for descriptive TTA.
            audio_type(string): Optional 音色克隆/参考音频生成/音频编辑/语音编辑/音色转换.
            voice(string): 音色 ID，各接口统一此参数，省略继承对应接口配置；可先查询能力。
            speed(number): 语速，OpenAI 0.25–4，MiniMax 0.5–2。
            response_format(string): 输出音频格式；受接口限制，使用能力查询返回的可选值。
            instructions(string): OpenAI 新版 TTS 的朗读风格指令，不改变要朗读的正文。
            language(string): MiniMax language_boost 或 DashScope language_type。
            volume(number): MiniMax 音量 0–10，0 也会发送。
            pitch(number): MiniMax 音调，整数 -12–12。
            sample_rate(number): MiniMax 采样率 8000/16000/22050/24000/32000/44100。
            bitrate(number): MiniMax 比特率 32000/64000/128000/256000。
            channels(number): MiniMax 声道数 1 或 2。
            emotion(string): MiniMax 情绪，可用值见能力查询。
            temperature(number): Chat/Gemini 采样随机性，0–2。
            top_p(number): Chat/Gemini 采样概率，0–1。
            max_tokens(number): Chat/Gemini 输出 token 上限。
            emo_control_method(string): IndexTTS-2 情感模式：与音色参考音频相同、使用情感参考音频、使用情感向量控制。
            emo_afraid(number): IndexTTS-2 恐惧强度 0–1.4，需向量控制模式。
            emo_angry(number): IndexTTS-2 愤怒强度 0–1.4。
            emo_calm(number): IndexTTS-2 平静强度 0–1.4。
            emo_disgusted(number): IndexTTS-2 厌恶强度 0–1.4。
            emo_happy(number): IndexTTS-2 开心强度 0–1.4。
            emo_melancholic(number): IndexTTS-2 忧郁强度 0–1.4。
            emo_sad(number): IndexTTS-2 悲伤强度 0–1.4。
            emo_random(boolean): IndexTTS-2 随机情感开关，false 也会发送。
            emo_surprised(string): IndexTTS-2 当前只支持字符串 0。
            use_message_media(boolean): 是否读取当前和引用消息中的音频，默认 true。
        """
        self._set_completed_media_units(event, 0)
        if error := self._register_user(event):
            yield error
            return
        parameters = supplied(
            {key: value for key, value in locals().items() if key in AUDIO}
        )
        if not self.audio_conf.get("enabled", True) or not self.audio_conf.get(
            "enable_llm_tool", True
        ):
            yield "生音频工具已关闭。"
            return
        if mode not in {"朗读", "提示词"} or (
            audio_type and audio_type not in AUDIO_TYPES
        ):
            yield "音频模式或类型无效。"
            return
        cooldown_key = self._tool_cooldown_key(event, "audio")
        cooldown = max(0, int(self.audio_conf.get("llm_cooldown_seconds", 60)))
        if (
            monotonic() - self._llm_last_call.get(cooldown_key, float("-inf"))
            < cooldown
        ):
            yield "生音频工具冷却中，请稍后再试。"
            return
        key = self._video_session_key(event)
        if key in self._video_busy:
            yield "已有生成任务正在运行，请等待结果。"
            return
        self._llm_last_call[cooldown_key] = monotonic()
        self._video_busy.add(key)
        task = asyncio.current_task()
        self._video_generation_tasks.add(task)
        try:
            if use_message_media:
                _, images, audios, videos = await self._video_command_inputs(
                    event, "", "自定义"
                )
            else:
                images, audios, videos = [], [], []
            if images or videos:
                yield "生音频不接受图片或视频。"
                return
            options = parse_audio_materials([text], mode)
            options.route = AUDIO_TYPES.get(audio_type, "")
            options.parameters = parameters
            result = await self._generate_audio(
                event, options, audios, tool_call=True
            )
            if result:
                yield "\n".join(part.text for part in result if isinstance(part, Plain))
            else:
                yield '{"status":"completed","media_type":"audio","delivery":"sent"}'
        finally:
            self._video_busy.discard(key)
            self._video_generation_tasks.discard(task)

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
        output_compression: int | None = None,
        moderation: str = "",
        style: str = "",
        n: int | None = None,
        max_num_results: int | None = None,
        input_fidelity: str = "",
        partial_images: int | None = None,
        action: str = "",
        temperature: float | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
        seed: int | None = None,
        max_tokens: int | None = None,
        use_message_media: bool = True,
    ):
        """使用统一图片生成入口生成或编辑图片。

        媒体不需要填写 URL；工具会读取当前消息和引用中的图片或图片文件。
        图片由插件直接发送，实际结果返回 Agent，保留后续对话流程。

        Args:
            prompt(string): 可选的图片生成或编辑提示词。省略时优先使用当前消息文本；如果只有参考图，则使用通用编辑提示。
            resolution(string): 可选插件画质档位：1K、2K、4K；留空使用配置或提示词。
            aspect_ratio(string): 可选比例，如 1:1、16:9、9:16、4:3、3:4、2:3、3:2、4:5、5:4、21:9；留空使用配置或输入图片比例。
            size(string): 可选 OpenAI Images 尺寸：auto、256x256、512x512、1024x1024、1536x1024、1024x1536、1792x1024、1024x1792，或符合模型约束的自定义 WIDTHxHEIGHT；填写后优先于 resolution/aspect_ratio 的 OpenAI size 映射。
            quality(string): 可选 OpenAI 画质：auto、low、medium、high、xhigh、max；旧版 DALL-E 兼容接口可填写 standard、hd。
            background(string): 可选背景：auto、transparent、opaque。
            output_format(string): 可选输出格式：png、jpeg、webp。
            output_compression(number): 可选输出压缩率 0-100；0 会原样发送，省略使用默认。
            moderation(string): 可选内容审核级别：auto、low。
            style(string): 可选风格：vivid、natural；主要适用于 DALL-E 3 兼容模型。
            n(number): 可选生成数量 1–10，省略使用默认，插件按顺序发送上游实际返回的全部图片。
            max_num_results(number): 可选 Responses 图片工具生成数量，1-50；未填写时使用接口默认值，实际返回的全部图片都会发送。
            input_fidelity(string): 可选编辑参考图保真度：low、high；没有输入图片时忽略。
            partial_images(number): 可选流式中间图片数量：0-3；仅由支持该字段的 OpenAI 图片接口处理。
            action(string): 可选 Responses 图片工具动作：auto、generate、edit；留空由接口自动判断。
            temperature(number): Gemini/Chat 采样随机性，0–2。
            top_p(number): Gemini/Chat 采样概率，0–1。
            top_k(number): Gemini 候选 token 数，正整数。
            seed(number): Gemini/Chat 随机种子，非负整数。
            max_tokens(number): Gemini/Chat 最大输出 token 数。
            use_message_media(boolean): 默认 true 读取当前或引用图片，纯文生时可设 false。
        """
        self._set_completed_media_units(event, 0)
        if error := self._register_user(event):
            yield error
            return
        if not self.conf.get("enable_llm_tool", True):
            yield "图片生成函数工具当前已在插件配置中停用。"
            return

        uid = self._tool_cooldown_key(event, "image")
        cooldown = max(0, int(self.conf.get("llm_cooldown_seconds", 60) or 0))
        now = monotonic()
        if cooldown:
            elapsed = now - self._llm_last_call.get(uid, float("-inf"))
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
        if use_message_media:
            try:
                images = await self._extract_images(
                    event, prompt, "自定义", strict=True, extra_sources=[]
                )
            except (ValueError, OSError) as exc:
                if self._llm_last_call.get(uid) == now:
                    self._llm_last_call.pop(uid, None)
                yield str(exc)
                return
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
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k,
            "seed": seed,
            "max_tokens": max_tokens,
        }

        # ``n`` is the tool's requested image count. Protocols such as Gemini
        # have no upstream n field, even for n=1. Keep their requests valid and
        # execute the logical batch behind this one cooldown check. Every item
        # still goes through _generate -> billed_call with the original rules.
        calls = 1
        split_count = False
        if type(n) is int and 1 <= n <= 10:
            try:
                effective = select_image_route(self.conf, images, str(self.conf.get("model") or "nano-banana"))
            except VideoError as exc:
                yield str(exc)
                return
            try:
                validate_options("image", effective, {"n": n}, has_reference=bool(images))
            except VideoError:
                calls, split_count = n, True
                image_options["n"] = None
        completed = 0
        result = []
        try:
            for _ in range(calls):
                returned = False
                try:
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
                    returned = True
                finally:
                    if split_count:
                        getter = getattr(event, "get_extra", None)
                        units = getter("aigen_completed_media_units", None) if callable(getter) else None
                        completed += max(0, units) if type(units) is int else int(returned and not result)
                if result:
                    break
        finally:
            if split_count:
                self._set_completed_media_units(event, completed)
        if result:
            yield "\n".join(part.text for part in result if isinstance(part, Plain))
        else:
            yield '{"status":"completed","media_type":"image","delivery":"sent"}'

    @filter.command("生图", alias={"画图"}, prefix_optional=False)
    async def draw_command(self, event: AstrMessageEvent):
        """根据提示词、图片或预设生成图片。用法：/生图 <提示词>，可附带或引用图片。"""
        if error := self._register_user(event):
            yield event.chain_result([Plain(error)])
            return
        prompt, preset_name, model = self._resolve_draw_prompt(
            self._event_message_text(event)
        )
        if not prompt:
            yield event.chain_result([Plain("用法：/画图 <自定义提示词>。")])
            return
        event.stop_event()
        try:
            images = await self._extract_images(event, prompt, preset_name)
        except (ValueError, OSError) as exc:
            yield event.chain_result([Plain(str(exc))])
            return
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
        "AI帮助",
        alias={"ai帮助", "AI菜单", "ai菜单"},
        prefix_optional=True,
    )
    async def help_command(self, event: AstrMessageEvent):
        """查看 AIGen 的生成、余额与卡密使用说明。用法：/AI帮助，以合并转发消息发送。"""
        if error := self._register_user(event):
            await event.send(event.chain_result([Plain(error)]))
            return
        text = str(self.conf.get("help_text", "帮助文档未配置。"))
        event.stop_event()
        await self._send_settings_menu(event, [text], title="AI帮助")
