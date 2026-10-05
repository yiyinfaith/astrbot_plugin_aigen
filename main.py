"""AstrBot image generation plugin.

The plugin intentionally keeps the small, stable feature set needed in
production: image generation, keyword presets, configurable help text, and
usage statistics. Network details remain in ``ApiManager`` so all supported
request modes use one implementation.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any, ClassVar

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core import AstrBotConfig

from .api_manager import ApiManager
from .data_manager import DataManager
from .image_manager import ImageManager
from .utils import extract_image_urls_from_text, match_keyword_in_text, norm_id


@register(
    "astrbot_plugin_aigen",
    "yiyinfaith",
    "统一图片生成、图生图与关键词预设插件",
    "1.0.0",
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
    }
    _DRAW_COMMANDS: ClassVar[set[str]] = {"画图", "生图"}

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        data_dir = Path(StarTools.get_data_dir())
        self.data_mgr = DataManager(data_dir, config)
        self.img_mgr = ImageManager(config)
        self.api_mgr = ApiManager(config)
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

    async def terminate(self):
        """Close the reusable HTTP session during hot reload."""
        session = getattr(self.api_mgr, "_session", None)
        if session is not None and not session.closed:
            await session.close()

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

    def _model_for_request(self, text: str) -> tuple[str, str]:
        """Resolve the configured default model for a request."""
        model = str(self.conf.get("model", "nano-banana") or "nano-banana")
        return text.strip(), model

    def _resolve_preset_prompt(self, text: str) -> tuple[str, str, str] | None:
        """Resolve a keyword preset anywhere in the message.

        The matching rule uses fuzzy matching (``keyword in text``) instead of
        requiring the keyword to be the first token.  This allows
        messages such as ``@小明手办化`` and ``请手办化`` to trigger while the
        image collector still obtains the actual ``At`` component/avatar.
        """
        clean = self._clean_message(text)
        if not clean:
            return None
        clean, model = self._model_for_request(clean)
        extra_prefix = str(self.conf.get("extra_prefix", "生图") or "生图").strip()
        if extra_prefix and (
            clean == extra_prefix or clean.startswith(extra_prefix + " ")
        ):
            return clean[len(extra_prefix) :].strip(), "自定义", model

        matched = match_keyword_in_text(clean, self.data_mgr.prompt_map)
        if matched:
            key, match_at = matched
            prompt = self.data_mgr.get_prompt(key)
            if prompt and prompt != "[内置预设]":
                before = self._remove_mention_prefix(clean[:match_at])
                after = clean[match_at + len(key) :].strip()
                suffix = " ".join(part for part in (before, after) if part).strip()
                return f"{prompt} {suffix}".strip(), key, model
        return None

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
        self, event: AstrMessageEvent, prompt: str, preset_name: str = "自定义"
    ) -> list[bytes]:
        """Collect image parameters using the preset or custom-prompt rules."""
        is_preset = preset_name not in {"", "自定义"}
        return await self.img_mgr.extract_images_from_event(
            event,
            ignore_id=self._bot_id(event),
            context=self.context,
            include_at_avatar=True,
            max_images=1 if is_preset else None,
            include_sender_avatar=is_preset,
            extra_sources=extract_image_urls_from_text(prompt),
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
        """Trigger configured presets and custom prompts from normal messages."""
        raw_text = self._event_message_text(event)
        text = self._clean_message(raw_text)
        if not text:
            return
        if any(
            text == command or text.startswith(command + " ")
            for command in self._HELP_COMMANDS | self._DRAW_COMMANDS
        ):
            return
        resolved = self._resolve_preset_prompt(text)
        if resolved is None or not resolved[0]:
            return
        prompt, preset_name, model = resolved
        need_prefix_key = (
            "custom_prompt_need_prefix"
            if preset_name == "自定义"
            else "preset_need_prefix"
        )
        default_need_prefix = preset_name == "自定义"
        if self.conf.get(need_prefix_key, default_need_prefix) and not self._has_astrbot_prefix(
            event, raw_text
        ):
            return
        event.stop_event()
        images = await self._extract_images(event, prompt, preset_name)
        yield event.chain_result(
            await self._generate(event, prompt, preset_name, model, images)
        )

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
        aliases={"生图帮助", "画图帮助", "生图菜单", "画图菜单"},
        prefix_optional=True,
    )
    async def help_command(self, event: AstrMessageEvent):
        """Send the help text configured in the plugin settings."""
        text = str(self.conf.get("help_text", "帮助文档未配置。"))
        yield event.chain_result([Plain(text)])
