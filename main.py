"""AstrBot image generation plugin.

The plugin intentionally keeps the small, stable feature set needed in
production: image generation, keyword presets, configurable help text, and
quota billing. Network details remain in ``ApiManager`` so all supported
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
        "画图帮助",
        "发图帮助",
        "手办化帮助",
        "lm帮助",
    }
    _DRAW_COMMANDS: ClassVar[set[str]] = {"画图", "文生图", "生图"}
    _MODE_ALIASES: ClassVar[dict[str, str]] = {
        "image": "openai_image",
        "openai_image": "openai_image",
        "chat": "openai_chat",
        "openai_chat": "openai_chat",
        "response": "openai_response",
        "openai_response": "openai_response",
        "gemini": "gemini_official",
        "gemini_official": "gemini_official",
        "custom": "custom_endpoint",
        "custom_endpoint": "custom_endpoint",
    }

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        data_dir = Path(StarTools.get_data_dir())
        self.data_mgr = DataManager(data_dir, config)
        self.img_mgr = ImageManager(config)
        self.api_mgr = ApiManager(config)
        self._llm_last_call: dict[str, float] = {}

    async def initialize(self):
        """Load persistent quotas and user presets after plugin injection."""
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

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """Return whether the sender is an AstrBot administrator."""
        sender_id = norm_id(event.get_sender_id())
        context_config = self.context.get_config() or {}
        admins = context_config.get("admins_id", []) or []
        if isinstance(admins, str):
            admins = re.split(r"[\r\n,]+", admins)
        return bool(sender_id) and sender_id in {norm_id(item) for item in admins}

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
        """Resolve ``/画图 <custom prompt>`` or an optional preset keyword."""
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
        self, event: AstrMessageEvent, prompt: str
    ) -> list[bytes]:
        """Collect images from the current message, mentions, and URL text."""
        images = await self.img_mgr.extract_images_from_event(
            event,
            ignore_id=self._bot_id(event),
            context=self.context,
            include_at_avatar=True,
        )
        for url in extract_image_urls_from_text(prompt):
            image = await self.img_mgr.load_bytes(url)
            if image:
                images.append(image)
        return images

    def _quota(self, event: AstrMessageEvent, uid: str, gid: str, cost: int) -> dict:
        """Check the optional group balance and return the account to charge.

        Personal balances remain on disk for backwards compatibility, but they
        are no longer a generation gate and are never decremented.  This keeps
        historical billing files readable while removing the personal limit.
        """
        if self._is_admin(event):
            return {"allowed": True, "source": "free"}
        group_balance = self.data_mgr.get_group_count(gid) if gid else 0
        if gid and group_balance >= cost:
            return {"allowed": True, "source": "group"}
        if not gid:
            return {"allowed": True, "source": "free"}
        return {
            "allowed": False,
            "msg": f"次数不足：本次需要 {cost} 次，群组剩余 {group_balance} 次。",
        }

    async def _save_config(self) -> None:
        """Persist command changes through AstrBot's native config object."""
        save = getattr(self.conf, "save", None)
        if callable(save):
            try:
                result = save()
                if hasattr(result, "__await__"):
                    await result
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not save plugin configuration: %s", exc)

    async def _generate(
        self,
        event: AstrMessageEvent,
        prompt: str,
        preset_name: str,
        model: str,
        images: list[bytes],
        show_progress: bool = True,
    ) -> list[Any]:
        """Charge one request, call the selected API mode, and build a reply."""
        uid = norm_id(event.get_sender_id())
        gid = norm_id(event.get_group_id())
        deduction = self._quota(event, uid, gid, 1)
        if not deduction.get("allowed"):
            return [Plain(deduction.get("msg", "暂时无法生成图片。"))]

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

        if deduction.get("source") == "group":
            await self.data_mgr.decrease_group_count(gid)

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
        if preset_name not in {"", "自定义"}:
            await self.data_mgr.save_preset_image(preset_name, result)
        suffix = f" | 预设：{preset_name}" if preset_name not in {"", "自定义"} else ""
        if self.conf.get("show_model_info", False):
            suffix += f" | 模型：{model}"
        return [
            Image.fromBytes(result),
            Plain(f"\n✅ 生成成功（{elapsed:.1f}s）{suffix}"),
        ]

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
        images = await self._extract_images(event, prompt)
        yield event.chain_result(
            await self._generate(event, prompt, preset_name, model, images)
        )

    @filter.llm_tool(name="generate_image")
    async def generate_image_tool(
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
            show_progress=bool(self.conf.get("llm_show_progress", True)),
        )
        yield event.chain_result(result)

    @filter.command("画图", aliases={"文生图", "生图"}, prefix_optional=False)
    async def draw_command(self, event: AstrMessageEvent):
        """Generate an image from ``/画图 <自定义提示词>`` or a preset."""
        prompt, preset_name, model = self._resolve_draw_prompt(
            self._event_message_text(event)
        )
        if not prompt:
            yield event.chain_result([Plain("用法：/画图 <自定义提示词>。")])
            return
        event.stop_event()
        images = await self._extract_images(event, prompt)
        yield event.chain_result(
            await self._generate(event, prompt, preset_name, model, images)
        )

    @filter.command(
        "画图帮助",
        aliases={"发图帮助", "手办化帮助", "lm帮助"},
        prefix_optional=True,
    )
    async def help_command(self, event: AstrMessageEvent):
        """Send the help text configured in the plugin settings."""
        text = str(self.conf.get("help_text", "帮助文档未配置。"))
        yield event.chain_result([Plain(text)])

    @filter.command("lm列表", aliases={"lmlist"}, prefix_optional=True)
    async def preset_list(self, event: AstrMessageEvent):
        """List available preset trigger words without changing their prompts."""
        names = sorted(self.data_mgr.prompt_map)
        yield event.chain_result([Plain("可用预设：\n" + "、".join(names))])

    @filter.command("lm查看", aliases={"lmv", "lm预览"}, prefix_optional=True)
    async def preset_view(self, event: AstrMessageEvent):
        """Display one complete preset prompt."""
        raw = self._clean_message(self._event_message_text(event))
        key = raw.split(maxsplit=1)[1].strip() if " " in raw else ""
        prompt = self.data_mgr.get_prompt(key)
        if not prompt:
            yield event.chain_result([Plain(f"未找到预设：{key or '（未填写）'}")])
            return
        yield event.chain_result([Plain(f"【{key}】\n{prompt}")])

    @filter.command("lm添加", aliases={"lma"}, prefix_optional=True)
    async def preset_add(self, event: AstrMessageEvent):
        """Add a user preset to the data directory without rewriting old presets."""
        raw = self._clean_message(self._event_message_text(event))
        payload = raw.split(maxsplit=1)[1].strip() if " " in raw else ""
        if ":" not in payload:
            yield event.chain_result([Plain("用法：/lm添加 触发词:提示词")])
            return
        key, prompt = (item.strip() for item in payload.split(":", 1))
        if not key or not prompt:
            yield event.chain_result([Plain("触发词和提示词都不能为空。")])
            return
        await self.data_mgr.add_user_prompt(key, prompt)
        yield event.chain_result([Plain(f"✅ 已保存预设：{key}")])

    @filter.command("lm删除", aliases={"lmd", "lm删", "删除预设"}, prefix_optional=True)
    async def preset_delete(self, event: AstrMessageEvent):
        """Delete only a user-owned preset; configured defaults remain untouched."""
        raw = self._clean_message(self._event_message_text(event))
        key = raw.split(maxsplit=1)[1].strip() if " " in raw else ""
        if not key or not await self.data_mgr.remove_user_prompt(key):
            yield event.chain_result([Plain("只能删除已经通过 /lm添加 保存的预设。")])
            return
        yield event.chain_result([Plain(f"✅ 已删除自定义预设：{key}")])

    @filter.command("画图查询次数", aliases={"手办化查询次数"}, prefix_optional=True)
    async def quota_query(self, event: AstrMessageEvent):
        """Show billing status without exposing a personal generation limit."""
        gid = norm_id(event.get_group_id())
        message = "个人次数限制：已取消"
        if gid:
            message += f"\n群组剩余：{self.data_mgr.get_group_count(gid)} 次"
        yield event.chain_result([Plain(message)])

    @filter.command(
        "画图增加用户次数", aliases={"手办化增加用户次数"}, prefix_optional=True
    )
    async def quota_add_user(self, event: AstrMessageEvent):
        """Let an administrator grant personal image credits."""
        if not self._is_admin(event):
            return
        raw = self._clean_message(self._event_message_text(event))
        parts = raw.split()
        if len(parts) < 3:
            yield event.chain_result([Plain("用法：/画图增加用户次数 用户ID 次数")])
            return
        try:
            amount = int(parts[2])
        except ValueError:
            yield event.chain_result([Plain("次数必须是整数。")])
            return
        await self.data_mgr.add_user_count(parts[1], amount)
        yield event.chain_result([Plain(f"✅ 已为 {parts[1]} 增加 {amount} 次。")])

    @filter.command(
        "画图增加群组次数", aliases={"手办化增加群组次数"}, prefix_optional=True
    )
    async def quota_add_group(self, event: AstrMessageEvent):
        """Let an administrator grant group image credits."""
        if not self._is_admin(event):
            return
        raw = self._clean_message(self._event_message_text(event))
        parts = raw.split()
        if len(parts) < 2:
            yield event.chain_result([Plain("用法：/画图增加群组次数 次数")])
            return
        try:
            amount = int(parts[1])
        except ValueError:
            yield event.chain_result([Plain("次数必须是整数。")])
            return
        gid = norm_id(event.get_group_id())
        if not gid:
            yield event.chain_result([Plain("该命令只能在群聊中使用。")])
            return
        await self.data_mgr.add_group_count(gid, amount)
        yield event.chain_result([Plain(f"✅ 已为本群增加 {amount} 次。")])

    @filter.command("切换API模式", prefix_optional=True)
    async def switch_mode(self, event: AstrMessageEvent):
        """Switch among all supported image request modes."""
        if not self._is_admin(event):
            return
        raw = self._clean_message(self._event_message_text(event))
        parts = raw.split(maxsplit=1)
        if len(parts) == 1:
            yield event.chain_result(
                [Plain(f"当前模式：{self.conf.get('interface_mode', 'openai_image')}")]
            )
            return
        mode = self._MODE_ALIASES.get(parts[1].strip().lower())
        if not mode:
            yield event.chain_result(
                [
                    Plain(
                        "支持：openai_image、openai_chat、openai_response、gemini_official、custom_endpoint"
                    )
                ]
            )
            return
        self.conf["interface_mode"] = mode
        await self._save_config()
        yield event.chain_result([Plain(f"✅ API 模式已切换为：{mode}")])

    @filter.command("切换模型", prefix_optional=True)
    async def switch_model(self, event: AstrMessageEvent):
        """View or change the default image model."""
        if not self._is_admin(event):
            return
        raw = self._clean_message(self._event_message_text(event))
        parts = raw.split(maxsplit=1)
        if len(parts) == 1:
            yield event.chain_result(
                [
                    Plain(f"当前模型：{self.conf.get('model')}")
                ]
            )
            return
        self.conf["model"] = parts[1].strip()
        await self._save_config()
        yield event.chain_result([Plain(f"✅ 默认模型已切换为：{self.conf['model']}")])
