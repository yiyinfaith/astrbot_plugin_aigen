"""Native AstrBot config groups and lossless v1.0.0 upgrades."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LEGACY_V120_HELP_TEXT = "🎨 图片：/生图 <提示词>，可附带或引用图片。\n🎬 视频：先 /生视频，然后依次发送文字、图片、QQ语音、视频或媒体文件；多段文字按顺序合并，最后 /生成。收集时不逐条回复；/取消 可清空重来。启动后请在配置的收集时限内发送 /生成，默认60秒；超时会提示取消。\n首帧、尾帧：关键词 + 提示词 + 一图。首尾帧：关键词 + 提示词 + 两图。普通生视频1–9图均为参考图，不自动选择帧模式。音频最多3段。\n动作迁移：关键词 + 一图一视频，不发提示词。音频同步：关键词 + 一图一音频，不发提示词。其他多媒体组合需提示词和管理员配置模型。\n可用 --duration 1 --resolution 480p --ratio 16:9。\n🎵 音频：/生音频 朗读 或 /生音频 提示词，第二个参数可选音色克隆、参考音频生成、音频编辑、语音编辑、音色转换。随后发送文字、QQ语音或音频文件，最后 /生成；多段文字合并。未指定类型且有音频时，回复类型名称后生成。/取消 可取消，默认60秒超时。"

LEGACY_HELP_TEXT = "🎨 图片：/生图 <提示词>、/画图 <提示词>，可附带或引用图片、@他人取头像。预设关键词按配置触发。\n🎬 视频：/生视频 <提示词>，根据图片/音频/视频自动选择模型，可直接发送或引用媒体、@他人取头像。\n参数：--duration 1 --resolution 480p --ratio 16:9；--frames 表示明确使用两张图作为首尾帧。图片、音频、视频请直接发送或引用，不要填写媒体 URL。\n例如：/生视频 一只猫在雪地奔跑 --duration 1\n模型和所需媒体以插件配置为准；任何人都可使用，不设次数限制。"

LEGACY_COLLECTION_HELP_TEXT = "🎨 图片：/生图 <提示词>，可附带或引用图片。\n🎬 视频：先 /生视频，然后依次发送文字、图片、QQ语音、视频或媒体文件；多段文字按顺序合并，最后 /生成。收集时不逐条回复；/取消 可清空重来。30分钟未发材料会过期。\n首帧、尾帧：关键词 + 提示词 + 一图。首尾帧：关键词 + 提示词 + 两图。普通生视频1–9图均为参考图，不自动选择帧模式。音频最多3段。\n动作迁移：关键词 + 一图一视频，不发提示词。音频同步：关键词 + 一图一音频，不发提示词。其他多媒体组合需提示词和管理员配置模型。\n可用 --duration 1 --resolution 480p --ratio 16:9。"

GLOBAL_KEYS = {
    "show_model_info",
    "debug_mode",
    "use_proxy",
    "proxy_url",
    "timeout",
    "use_stream",
    "help_text",
    "llm_cooldown_seconds",
}

AUDIO_ADAPTER_DEFAULTS = {
    "custom_create_path": "/v2/video_generation",
    "custom_query_path": "/v2/query/video_generation/{task_id}",
    "custom_body_template": '{"model":"{model}","prompt":"{prompt}","audios":"{audios}"}',
    "custom_headers": "{}",
    "custom_id_path": "id",
    "custom_status_path": "status",
    "custom_url_path": "url",
    "custom_error_path": "error.message",
    "custom_base64_path": "audio_base64",
    "custom_success_status": "",
    "custom_failure_status": "",
}


def media_attempts(config: dict, kind: str) -> int:
    """Return the shared total-attempt limit for download and delivery stages."""
    return max(1, min(5, int(config.get(f"{kind}_delivery_retries", 2) or 2)))


def schema_defaults(schema: dict) -> dict:
    return {
        key: schema_defaults(node["items"])
        if node["type"] == "object"
        else copy.deepcopy(node.get("default"))
        for key, node in schema.items()
    }


def migrate_config(config: Any, backup_dir: Path) -> bool:
    """Keep flat fields in the schema until AstrBot has loaded and backed them up.

    AstrBot normalizes configuration *before* constructing a plugin. Hidden
    legacy nodes are therefore necessary to preserve user data during loading.
    Only this one-time upgrade reads them; grouped settings take over afterward.
    """
    if config.get("_config_layout_version", 0) >= 1:
        changed = migrate_video_settings(config, backup_dir)
        changed = (
            migrate_generation_settings(config, backup_dir, backup=not changed)
            or changed
        )
        return (
            migrate_compact_settings(config, backup_dir, backup=not changed) or changed
        )
    schema = json.loads(
        Path(__file__).with_name("_conf_schema.json").read_text("utf-8")
    )
    defaults = schema_defaults(schema)
    original = copy.deepcopy(dict(config))
    upgraded = copy.deepcopy(original)
    for name in ("global_settings", "image_settings", "video_settings"):
        upgraded.setdefault(name, copy.deepcopy(defaults[name]))
    legacy = {
        key: value
        for key, value in original.items()
        if key
        not in {
            "global_settings",
            "image_settings",
            "video_settings",
            "audio_settings",
            "_config_layout_version",
        }
        and value is not None
    }
    if legacy:
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = backup_dir / f"config_before_v1.0.1_{stamp}.json"
        with backup.open("x", encoding="utf-8") as stream:
            json.dump(original, stream, ensure_ascii=False, indent=2)
        backup.chmod(0o600)
        migrated = {}
        for key, value in legacy.items():
            group = "global_settings" if key in GLOBAL_KEYS else "image_settings"
            if key in defaults[group]:
                # Preserve already edited new settings; otherwise migrate every
                # old value, including empty lists, False, zero and full prompts.
                if upgraded[group].get(key) == defaults[group].get(key):
                    upgraded[group][key] = copy.deepcopy(value)
                    migrated[(group, key)] = value
                elif key == "prompt_list" and upgraded[group][key] != value:
                    raise ValueError(
                        "新旧预设列表不同；旧配置已备份，停止自动迁移以保护两份预设。"
                    )
        if any(
            upgraded[group][key] != value for (group, key), value in migrated.items()
        ):
            raise ValueError("配置迁移校验失败；旧配置已备份，停止加载。")
    upgraded["_config_layout_version"] = 1
    config.clear()
    config.update(upgraded)
    try:
        save = getattr(config, "save_config", None)
        if callable(save):
            save()
    except Exception:
        config.clear()
        config.update(original)
        raise
    migrate_video_settings(config, backup_dir, backup=False)
    migrate_generation_settings(config, backup_dir, backup=False)
    migrate_compact_settings(config, backup_dir, backup=False)
    return bool(legacy)


def migrate_video_settings(config: Any, backup_dir: Path, *, backup=True) -> bool:
    """Upgrade video collection defaults and older routes losslessly.

    Args:
        config: AstrBot configuration, including grouped settings.
        backup_dir: Directory for the original configuration snapshot.
        backup: Whether the older layout has not already been backed up.

    Returns:
        Whether an existing layout was upgraded.

    Raises:
        OSError: If backing up or saving fails; in-memory values are restored.
    """
    if config.get("_config_layout_version", 0) >= 3:
        return False
    original = copy.deepcopy(dict(config))
    upgraded = copy.deepcopy(original)
    if backup:
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        version = "v1.1.1" if config.get("_config_layout_version", 0) >= 2 else "v1.1.0"
        snapshot = backup_dir / f"config_before_{version}_{stamp}.json"
        with snapshot.open("x", encoding="utf-8") as stream:
            json.dump(original, stream, ensure_ascii=False, indent=2)
        snapshot.chmod(0o600)
    schema = json.loads(
        Path(__file__).with_name("_conf_schema.json").read_text("utf-8")
    )
    defaults = schema_defaults(schema)
    global_settings = upgraded.get("global_settings", {})
    if global_settings.get("help_text") in {
        LEGACY_HELP_TEXT,
        LEGACY_COLLECTION_HELP_TEXT,
    }:
        global_settings["help_text"] = defaults["global_settings"]["help_text"]
    if config.get("_config_layout_version", 0) < 2:
        for name in ("image_settings", "video_settings"):
            group = upgraded.setdefault(name, copy.deepcopy(defaults[name]))
            prefix = group.get("extra_prefix", defaults[name]["extra_prefix"])
            if isinstance(prefix, str):
                group["extra_prefix"] = [prefix] if prefix.strip() else []
        video = upgraded["video_settings"]
        rules = video.setdefault(
            "routes", copy.deepcopy(defaults["video_settings"]["routes"])
        )
        if isinstance(rules, dict):
            rules = [{"__template_key": key, **value} for key, value in rules.items()]
            video["routes"] = rules
        if isinstance(rules, list):
            motion = next(
                (
                    r
                    for r in rules
                    if isinstance(r, dict)
                    and r.get("__template_key") == "motion_transfer"
                ),
                None,
            )
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                if (
                    rule.get("__template_key") == "image_video_to_video"
                    and rule.get("model") == "wan2.2-animate-move"
                    and motion is None
                ):
                    motion = {
                        **copy.deepcopy(rule),
                        "__template_key": "motion_transfer",
                        "interface_mode": "minimax",
                    }
                    rule.update(
                        model="",
                        long_model="",
                        interface_mode="minimax",
                        resolution="",
                        aspect_ratio="",
                    )
                if rule.get("interface_mode", "inherit") == "inherit":
                    rule["interface_mode"] = "minimax"
            if motion is not None and motion not in rules:
                rules.append(motion)
            present = {r.get("__template_key") for r in rules if isinstance(r, dict)}
            for rule in defaults["video_settings"]["routes"]:
                if rule["__template_key"] not in present:
                    rules.append(copy.deepcopy(rule))
    upgraded.setdefault("video_settings", {}).setdefault(
        "collection_timeout_seconds",
        defaults["video_settings"]["collection_timeout_seconds"],
    )
    upgraded["_config_layout_version"] = 3
    config.clear()
    config.update(upgraded)
    try:
        save = getattr(config, "save_config", None)
        if callable(save):
            save()
    except Exception:
        config.clear()
        config.update(original)
        raise
    return backup


def migrate_generation_settings(config: Any, backup_dir: Path, *, backup=True) -> bool:
    """Add image/audio routes and remove the unused video progress field.

    Args:
        config: Current grouped AstrBot configuration.
        backup_dir: Directory for the original configuration snapshot.
        backup: Whether an earlier upgrade has not already backed up this load.

    Returns:
        Whether the current settings layout was upgraded.

    Raises:
        OSError: If backing up or saving fails.
    """
    if config.get("_config_layout_version", 0) >= 5:
        return False
    original = copy.deepcopy(dict(config))
    upgraded = copy.deepcopy(original)
    if backup:
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        snapshot = backup_dir / f"config_before_v1.3.0_{stamp}.json"
        snapshot.write_text(
            json.dumps(original, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        snapshot.chmod(0o600)
    defaults = schema_defaults(
        json.loads(Path(__file__).with_name("_conf_schema.json").read_text("utf-8"))
    )
    image = upgraded.setdefault("image_settings", {})
    for field in ("auto_route", "routes"):
        image.setdefault(field, copy.deepcopy(defaults["image_settings"][field]))
    upgraded.setdefault("audio_settings", copy.deepcopy(defaults["audio_settings"]))
    upgraded.setdefault("video_settings", {}).pop("generating_msg_template", None)
    for group in ("video_settings", "audio_settings"):
        settings = upgraded[group]
        if settings.get("collection_timeout_seconds", 60) == 60:
            settings["collection_timeout_seconds"] = 120
    if upgraded.get("global_settings", {}).get("help_text") == LEGACY_V120_HELP_TEXT:
        upgraded["global_settings"]["help_text"] = defaults["global_settings"][
            "help_text"
        ]
    upgraded["_config_layout_version"] = 5
    config.clear()
    config.update(upgraded)
    try:
        save = getattr(config, "save_config", None)
        if callable(save):
            save()
    except Exception:
        config.clear()
        config.update(original)
        raise
    return True


def migrate_compact_settings(config: Any, backup_dir: Path, *, backup=True) -> bool:
    """Compact legacy fields and fill optional per-route generation settings.

    Args:
        config: Configuration after the host's schema normalization.
        backup_dir: Directory for a recoverable original snapshot.
        backup: Whether no earlier migration backed up this load.

    Returns:
        Whether configuration was upgraded.

    Raises:
        ValueError: If a populated JSON field cannot be merged losslessly.
        OSError: If the backup or configuration cannot be saved.
    """
    if config.get("_config_layout_version", 0) >= 7:
        return False
    original = copy.deepcopy(dict(config))
    upgraded = copy.deepcopy(original)
    audio = upgraded.setdefault("audio_settings", {})
    legacy = {
        key: audio[key]
        for key, default in AUDIO_ADAPTER_DEFAULTS.items()
        if audio.get(key) is not None and audio[key] != default
    }
    if legacy:
        raw = audio.get("parameter_defaults") or "{}"
        values = json.loads(raw) if isinstance(raw, str) else copy.deepcopy(raw)
        if not isinstance(values, dict) or not isinstance(
            values.get("adapter", {}), dict
        ):
            raise ValueError(
                "音频生成参数默认值必须是 JSON 对象，adapter 必须是对象；旧配置未修改。"
            )
        adapter = values.setdefault("adapter", {})
        for key, value in legacy.items():
            name = key.removeprefix("custom_")
            if name not in adapter:
                # Keep malformed legacy JSON as text so submission validation
                # still rejects it instead of silently substituting defaults.
                if name in {"body_template", "headers"} and isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except ValueError:
                        pass
                adapter[name] = value
        audio["parameter_defaults"] = json.dumps(values, ensure_ascii=False, indent=2)
    for key in AUDIO_ADAPTER_DEFAULTS:
        audio.pop(key, None)
    image = upgraded.setdefault("image_settings", {})
    if not image.get("image_delivery_retries"):
        image["image_delivery_retries"] = (
            image.get("result_image_download_retries") or 2
        )
    image.pop("result_image_download_retries", None)
    image.pop("download_retries", None)
    video = upgraded.setdefault("video_settings", {})
    if not video.get("video_delivery_retries"):
        video["video_delivery_retries"] = (
            video.get("result_video_download_retries") or 2
        )
    video.pop("result_video_download_retries", None)
    video.pop("reference_mode", None)
    rules = video.get("routes", [])
    rules = (
        rules.values()
        if isinstance(rules, dict)
        else rules
        if isinstance(rules, list)
        else []
    )
    for rule in rules:
        if isinstance(rule, dict):
            rule.setdefault("base_url", "")
            rule.setdefault("api_keys", "")
    audio.setdefault("prompt_list", [])
    audio.setdefault("preset_need_prefix", True)
    upgraded.setdefault("global_settings", {}).pop("billing_timezone", None)
    upgraded["_config_layout_version"] = 7
    if backup:
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        snapshot = backup_dir / f"config_before_v1.4.7_{stamp}.json"
        snapshot.write_text(
            json.dumps(original, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        snapshot.chmod(0o600)
    config.clear()
    config.update(upgraded)
    try:
        save = getattr(config, "save_config", None)
        if callable(save):
            save()
    except Exception:
        config.clear()
        config.update(original)
        raise
    return True


def media_config(config: dict, kind: str = "image") -> dict:
    """Build a runtime view; old fields are never read after migration."""
    return {**config.get("global_settings", {}), **config.get(f"{kind}_settings", {})}
