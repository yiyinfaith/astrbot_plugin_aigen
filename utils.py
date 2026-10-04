import re
from typing import Any, List
from urllib.parse import urlsplit, urlunsplit


_API_VERSION_SEGMENT = re.compile(r"^v\d+(?:(?:alpha|beta)\d*)?$", re.IGNORECASE)


def norm_id(raw_id: Any) -> str:
    """标准化 ID 为字符串"""
    if raw_id is None:
        return ""
    return str(raw_id).strip()


def match_keyword_in_text(text: Any, keywords: Any) -> tuple[str, int] | None:
    """Match the longest configured keyword anywhere in ``text``.

    This is the fuzzy matching rule used by AstrBot's memelite plugin.  The
    longest-first order makes overlapping presets deterministic (for example,
    ``手办化2`` wins over ``手办化``).
    """
    value = str(text or "")
    candidates_set: set[str] = set()
    for raw_keyword in keywords or []:
        if raw_keyword is None:
            continue
        keyword = str(raw_keyword).strip()
        if keyword:
            candidates_set.add(keyword)
    candidates = sorted(candidates_set, key=lambda item: (-len(item), item))
    for keyword in candidates:
        index = value.find(keyword)
        if index >= 0:
            return keyword, index
    return None


def normalize_model_list(raw_models: Any) -> List[str]:
    """兼容字符串列表和旧版字典模型配置，并跳过无效项。"""
    if not isinstance(raw_models, (list, tuple, set)):
        return []

    models = []
    for item in raw_models:
        value = ""
        if isinstance(item, str):
            value = item
        elif isinstance(item, dict):
            value = item.get("id") or item.get("model") or item.get("name") or ""

        value = str(value).strip()
        if value and value not in models:
            models.append(value)
    return models


def normalize_api_root(raw_url: Any) -> str:
    """Extract an API root while removing version and endpoint suffixes.

    Args:
        raw_url: Configured base URL or complete API endpoint URL.

    Returns:
        The normalized API root without query parameters or endpoint suffixes.
    """
    url = str(raw_url or "").strip().rstrip("/")
    if not url:
        return ""

    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        # 配置项正常应为绝对 URL；这里仍对异常输入做保守的字符串清理。
        clean = re.split(r"[?#]", url, maxsplit=1)[0].rstrip("/")
        clean = re.sub(
            r"/(?:v\d+(?:(?:alpha|beta)\d*)?)(?:/.*)?$",
            "",
            clean,
            flags=re.IGNORECASE,
        )
        clean = re.sub(
            r"/(?:chat/completions|images/(?:generations|edits)|responses?|models(?:/.*)?)$",
            "",
            clean,
            flags=re.IGNORECASE,
        )
        return clean.rstrip("/")

    segments = [segment for segment in parsed.path.split("/") if segment]
    cut_at = len(segments)

    for index, segment in enumerate(segments):
        lower_segment = segment.lower()
        if _API_VERSION_SEGMENT.fullmatch(segment):
            cut_at = index
            break
        if lower_segment == "models":
            cut_at = index
            break
        if lower_segment in {"response", "responses"}:
            cut_at = index
            break
        if (
            lower_segment == "chat"
            and index + 1 < len(segments)
            and segments[index + 1].lower() == "completions"
        ):
            cut_at = index
            break
        if (
            lower_segment == "images"
            and index + 1 < len(segments)
            and segments[index + 1].lower() in {"generations", "edits"}
        ):
            cut_at = index
            break

    root_path = "/" + "/".join(segments[:cut_at]) if cut_at else ""
    return urlunsplit(
        (parsed.scheme, parsed.netloc, root_path.rstrip("/"), "", "")
    ).rstrip("/")


def extract_image_urls_from_text(text: str) -> List[str]:
    """从文本中提取图片链接和本地文件路径"""
    image_urls = []

    # 本地文件路径 (Windows)
    local_patterns = [r"[a-zA-Z]:\\[^\s,，。！？\n]+\.(?:jpg|jpeg|png|gif|bmp|webp)"]
    for pattern in local_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for match in matches:
            if match and match not in image_urls:
                image_urls.append(match)

    # 网络 URL
    url_patterns = [
        r'https?://[^\s<>"\'\)]+\.(?:jpg|jpeg|png|gif|bmp|webp)(?:\?[^\s<>"\'\)]*)?(?=[\s<>"\'\)|$])',
        r'https?://[^\s<>"\'\)]+/(?:s\d+/|upload/|image/|img/|pic/)[^\s<>"\'\)]+\.(?:jpg|jpeg|png|gif|bmp|webp)(?:\?[^\s<>"\'\)]*)?(?=[\s<>"\'\)|$])',
        r'https?://youke\d+\.picui\.cn/[^\s<>"\'\)]+\.(?:jpg|jpeg|png|gif|bmp|webp)(?:\?[^\s<>"\'\)]*)?(?=[\s<>"\'\)|$])',
    ]
    for pattern in url_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for match in matches:
            if match and match not in image_urls:
                image_urls.append(match)

    return image_urls
