"""Native AstrBot config groups and lossless v1.0.0 upgrades."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
        return False
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
    return bool(legacy)


def media_config(config: dict, kind: str = "image") -> dict:
    """Build a runtime view; old fields are never read after migration."""
    return {**config.get("global_settings", {}), **config.get(f"{kind}_settings", {})}
