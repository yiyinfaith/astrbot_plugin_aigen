"""Helpers for the current grouped AstrBot configuration layout."""

from __future__ import annotations

import copy
import json
from pathlib import Path

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
    """Build an independent configuration tree from the current schema."""
    return {
        key: schema_defaults(node["items"])
        if node["type"] == "object"
        else copy.deepcopy(node.get("default"))
        for key, node in schema.items()
    }


def media_config(config: dict, kind: str = "image") -> dict:
    """Build a runtime view from the only supported grouped layout."""
    return {**config.get("global_settings", {}), **config.get(f"{kind}_settings", {})}


def load_schema_defaults() -> dict:
    """Load the plugin schema defaults for tooling and offline checks."""
    return schema_defaults(
        json.loads(Path(__file__).with_name("_conf_schema.json").read_text("utf-8"))
    )
