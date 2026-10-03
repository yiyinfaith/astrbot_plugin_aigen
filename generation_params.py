import io
import re
from typing import Dict, Optional

from PIL import Image, ImageOps


IMAGE_SIZE_MAP = {
    "1K": {
        "1:1": "1024x1024",
        "16:9": "1280x720",
        "9:16": "720x1280",
        "5:4": "1040x832",
        "4:5": "832x1040",
        "4:3": "1024x768",
        "3:4": "768x1024",
        "3:2": "1008x672",
        "2:3": "672x1008",
        "21:9": "1344x576",
    },
    "2K": {
        "1:1": "2048x2048",
        "16:9": "2048x1152",
        "9:16": "1152x2048",
        "5:4": "2080x1664",
        "4:5": "1664x2080",
        "4:3": "2048x1536",
        "3:4": "1536x2048",
        "3:2": "2064x1376",
        "2:3": "1376x2064",
        "21:9": "2016x864",
    },
    "4K": {
        "1:1": "2880x2880",
        "16:9": "3840x2160",
        "9:16": "2160x3840",
        "5:4": "3200x2560",
        "4:5": "2560x3200",
        "4:3": "3264x2448",
        "3:4": "2448x3264",
        "3:2": "3504x2336",
        "2:3": "2336x3504",
        "21:9": "3808x1632",
    },
}

SUPPORTED_ASPECT_RATIOS = tuple(IMAGE_SIZE_MAP["1K"].keys())
_SIZE_TO_PARAMS = {
    size.lower(): (resolution, aspect_ratio)
    for resolution, ratios in IMAGE_SIZE_MAP.items()
    for aspect_ratio, size in ratios.items()
}


def normalize_resolution(value: Optional[str], default: str = "1K") -> str:
    text = str(value or "").strip().upper().replace(" ", "")
    match = re.fullmatch(r"([124])K", text)
    if match:
        return f"{match.group(1)}K"
    normalized_default = str(default or "1K").strip().upper().replace(" ", "")
    return normalized_default if normalized_default in IMAGE_SIZE_MAP else "1K"


def normalize_aspect_ratio(value: Optional[str], default: str = "1:1") -> str:
    text = str(value or "").strip()
    for separator in ("：", "/", "x", "X", "×", "*"):
        text = text.replace(separator, ":")
    text = re.sub(r"\s+", "", text)
    return text if text in SUPPORTED_ASPECT_RATIOS else default


def detect_aspect_ratio_from_image(image_data: bytes, default: str = "1:1") -> str:
    """读取图片尺寸，并吸附到最接近的常规宽高比。"""
    if not image_data:
        return normalize_aspect_ratio(default)
    try:
        with Image.open(io.BytesIO(image_data)) as image:
            width, height = ImageOps.exif_transpose(image).size
        if width <= 0 or height <= 0:
            return normalize_aspect_ratio(default)

        actual_ratio = width / height
        return min(
            SUPPORTED_ASPECT_RATIOS,
            key=lambda item: abs(
                actual_ratio - (int(item.split(":")[0]) / int(item.split(":")[1]))
            ),
        )
    except Exception:
        return normalize_aspect_ratio(default)


def resolve_image_generation_params(
    prompt: str,
    default_resolution: str = "1K",
    default_aspect_ratio: str = "1:1",
    resolution: Optional[str] = None,
    aspect_ratio: Optional[str] = None,
) -> Dict[str, str]:
    """从提示词和显式参数中解析图片分辨率、比例及 OpenAI size。

    显式参数优先于提示词；提示词优先于配置默认值。若提示词包含映射表中的
    精确像素尺寸（如 3840x2160），会同时反推出 4K 与 16:9。
    """
    text = str(prompt or "")
    detected_resolution = None
    detected_ratio = None

    dimension_match = re.search(r"(?<!\d)(\d{3,4})\s*[xX×*]\s*(\d{3,4})(?!\d)", text)
    if dimension_match:
        dimension = f"{dimension_match.group(1)}x{dimension_match.group(2)}".lower()
        if dimension in _SIZE_TO_PARAMS:
            detected_resolution, detected_ratio = _SIZE_TO_PARAMS[dimension]

    ratio_match = re.search(
        r"(?<!\d)(21|16|9|5|4|3|2|1)\s*[:：/xX×*]\s*(16|9|5|4|3|2|1)(?!\d)",
        text,
    )
    if ratio_match:
        candidate = f"{ratio_match.group(1)}:{ratio_match.group(2)}"
        if candidate in SUPPORTED_ASPECT_RATIOS:
            detected_ratio = candidate

    resolution_matches = re.findall(
        r"(?<![A-Za-z0-9])([124])\s*[kK](?![A-Za-z0-9])", text
    )
    if resolution_matches:
        detected_resolution = f"{resolution_matches[-1]}K"

    final_resolution = normalize_resolution(
        resolution, detected_resolution or default_resolution
    )
    if not resolution:
        final_resolution = normalize_resolution(detected_resolution, default_resolution)

    final_ratio = normalize_aspect_ratio(
        aspect_ratio, detected_ratio or default_aspect_ratio
    )
    if not aspect_ratio:
        final_ratio = normalize_aspect_ratio(detected_ratio, default_aspect_ratio)

    return {
        "resolution": final_resolution,
        "aspect_ratio": final_ratio,
        "size": IMAGE_SIZE_MAP[final_resolution][final_ratio],
    }
