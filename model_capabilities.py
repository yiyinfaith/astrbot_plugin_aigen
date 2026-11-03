"""Administrator-configurable material and output capability ranges."""

from __future__ import annotations

import math
import re

from .video_router import VideoError

CAPABILITY_FIELDS = {
    "input_text": "输入文本",
    "input_images": "输入图片",
    "input_videos": "输入视频",
    "input_audios": "输入音频",
    "output_seconds": "输出秒数",
    "output_resolution": "输出分辨率",
    "output_aspect_ratio": "输出比例",
}
NUMERIC_FIELDS = {
    "input_text",
    "input_images",
    "input_videos",
    "input_audios",
    "output_seconds",
}
_NUMBER = r"(?:\d+(?:\.\d+)?|\.\d+)"
_RANGE = re.compile(rf"^({_NUMBER})\s*-\s*({_NUMBER})$")


def normalize_supported_ranges(value) -> dict[str, str]:
    """Validate and normalize the WebUI's compact range strings.

    Numeric fields accept an inclusive range such as ``1-15`` or a comma
    separated list such as ``1,5,10``. Text fields accept comma separated
    values. Empty fields are omitted so older price rows remain unchanged.
    """
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise VideoError("支持参数范围必须是对象。")
    unknown = set(value) - set(CAPABILITY_FIELDS)
    if unknown:
        raise VideoError("支持参数范围包含未知字段：" + "、".join(sorted(unknown)))
    result = {}
    for key, raw in value.items():
        if raw is None:
            continue
        if not isinstance(raw, str) or len(raw) > 200:
            raise VideoError(f"{CAPABILITY_FIELDS[key]}范围必须是 200 字符以内的文字。")
        text = raw.strip()
        if not text:
            continue
        if any(char in text for char in "\n\r"):
            raise VideoError(f"{CAPABILITY_FIELDS[key]}范围不能包含换行。")
        if key in NUMERIC_FIELDS:
            match = _RANGE.fullmatch(text)
            if match:
                low, high = (float(part) for part in match.groups())
                if (
                    not all(math.isfinite(item) for item in (low, high))
                    or low < (1 if key == "output_seconds" else 0)
                    or low > high
                    or not low.is_integer()
                    or not high.is_integer()
                ):
                    raise VideoError(
                        f"{CAPABILITY_FIELDS[key]}范围必须是递增的非负整数范围。"
                    )
                if key == "output_seconds" and high - low > 199:
                    raise VideoError("输出秒数范围最多可包含 200 个菜单选项。")
                if low.is_integer() and high.is_integer():
                    text = f"{int(low)}-{int(high)}"
                else:
                    text = f"{low:g}-{high:g}"
                result[key] = text
                continue
            parts = [part.strip() for part in text.split(",")]
            if not parts or any(not part for part in parts):
                raise VideoError(
                    f"{CAPABILITY_FIELDS[key]}请使用值1-值2或英文逗号分隔的值。"
                )
            numbers = []
            for part in parts:
                try:
                    number = float(part)
                except (TypeError, ValueError) as exc:
                    raise VideoError(
                        f"{CAPABILITY_FIELDS[key]}包含无效数字：{part}"
                    ) from exc
                minimum = 1 if key == "output_seconds" else 0
                if not math.isfinite(number) or number < minimum:
                    raise VideoError(
                        f"{CAPABILITY_FIELDS[key]}只能填写不小于 {minimum} 的有限数字。"
                    )
                if not number.is_integer():
                    raise VideoError(f"{CAPABILITY_FIELDS[key]}只能填写整数。")
                numbers.append(int(number) if number.is_integer() else number)
            result[key] = ",".join(str(number) for number in dict.fromkeys(numbers))
        else:
            parts = [part.strip() for part in text.split(",")]
            if not parts or any(not part for part in parts):
                raise VideoError(f"{CAPABILITY_FIELDS[key]}请使用英文逗号分隔多个值。")
            result[key] = ",".join(dict.fromkeys(parts))
    return result


def _numeric_values(text: str) -> tuple[float, ...]:
    match = _RANGE.fullmatch(text)
    if match:
        low, high = (float(part) for part in match.groups())
        if low.is_integer() and high.is_integer() and high - low <= 200:
            return tuple(range(int(low), int(high) + 1))
        return ()
    return tuple(
        int(part) if float(part).is_integer() else float(part)
        for part in text.split(",")
    )


def values(config: dict, key: str) -> tuple:
    """Return finite menu values when a configured range can be expanded."""
    raw = (config.get("supported_ranges") or {}).get(key, "")
    if not isinstance(raw, str) or not raw.strip():
        return ()
    if key in NUMERIC_FIELDS:
        return _numeric_values(raw.strip())
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def has_range(config: dict, key: str) -> bool:
    """Return whether the selected model has an explicit setting for a field."""
    ranges = config.get("supported_ranges")
    return (
        isinstance(ranges, dict)
        and isinstance(ranges.get(key), str)
        and bool(ranges[key].strip())
    )


def allows(config: dict, key: str, value) -> bool:
    """Return whether a configured numeric or enum range accepts ``value``."""
    if not has_range(config, key):
        return True
    raw = config["supported_ranges"][key].strip()
    if key in NUMERIC_FIELDS and _RANGE.fullmatch(raw):
        limit = bounds(config, key)
        return bool(limit and limit[0] <= value <= limit[1])
    allowed = values(config, key)
    return value in allowed


def bounds(config: dict, key: str) -> tuple[float, float] | None:
    """Return numeric lower/upper bounds for validation."""
    raw = (config.get("supported_ranges") or {}).get(key, "")
    if not isinstance(raw, str) or not raw.strip() or key not in NUMERIC_FIELDS:
        return None
    match = _RANGE.fullmatch(raw.strip())
    if match:
        return tuple(float(part) for part in match.groups())
    parsed = _numeric_values(raw.strip())
    return (min(parsed), max(parsed)) if parsed else None


def material_bounds(
    config: dict, key: str, fallback: tuple[int, int]
) -> tuple[int, int]:
    """Use a configured integer material range or the adapter fallback."""
    result = bounds(config, key)
    if not result:
        return fallback
    low, high = result
    if low.is_integer() and high.is_integer():
        return int(low), int(high)
    raise VideoError(f"{CAPABILITY_FIELDS[key]}必须使用整数范围。")


def merge_model_ranges(config: dict, model: str) -> dict:
    """Attach the configured model range to an effective route config."""
    mapping = config.get("_model_capabilities", {})
    ranges = mapping.get(model) if isinstance(mapping, dict) else None
    effective = dict(config)
    # Ranges belong to one model; never carry another model's limits forward.
    effective.pop("supported_ranges", None)
    if ranges:
        effective["supported_ranges"] = normalize_supported_ranges(ranges)
    return effective
