import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_API_VERSION_SEGMENT = re.compile(r"^v\d+(?:(?:alpha|beta)\d*)?$", re.IGNORECASE)
MAX_IMAGE_BYTES = 100 * 1024 * 1024
MAX_API_RESPONSE_BYTES = 150 * 1024 * 1024


async def read_api_response(response) -> str:
    """Drain real response traffic while discarding SSE comment heartbeats.

    Heartbeats must originate at the server/gateway. Reading chunks keeps the
    socket active without accumulating comments or treating them as a result.
    The caller's configured total timeout and cancellation remain authoritative.
    """
    if not callable(getattr(getattr(response, "content", None), "iter_any", None)):
        return await response.text()  # Compatibility with host response wrappers.
    is_sse = "text/event-stream" in response.headers.get("Content-Type", "").lower()
    data = bytearray()
    line_start = True
    comment = False

    def append(chunk):
        if len(data) + len(chunk) > MAX_API_RESPONSE_BYTES:
            raise ValueError("API 响应超过 150MB 上限；已提交的生成请求不会自动重试。")
        data.extend(chunk)

    async for chunk in response.content.iter_any():
        if not is_sse:
            append(chunk)
            continue
        # Split only the new chunk: repeatedly scanning a growing base64 line
        # makes large generated images quadratic in size.
        pieces = chunk.split(b"\n")
        for index, piece in enumerate(pieces):
            piece = piece.rstrip(b"\r")
            if line_start and piece:
                comment = piece.startswith(b":")
                line_start = False
            if not comment and piece:
                append(piece)
            if index < len(pieces) - 1:
                if not line_start and not comment:
                    append(b"\n")
                line_start = True
                comment = False
    return data.decode(response.charset or "utf-8", errors="replace")


async def read_image_response(response) -> bytes:
    """Bound downloads even when Content-Length is absent or incorrect.

    Args:
        response: The HTTP response supplying image bytes.

    Returns:
        Downloaded bytes within the image size limit.

    Raises:
        ValueError: If the image exceeds the size limit.
    """
    if response.content_length and response.content_length > MAX_IMAGE_BYTES:
        raise ValueError("图片超过 100MB 下载上限。")
    data = bytearray()
    async for chunk in response.content.iter_chunked(64 * 1024):
        if len(data) + len(chunk) > MAX_IMAGE_BYTES:
            raise ValueError("图片超过 100MB 下载上限。")
        data.extend(chunk)
    return bytes(data)


def norm_id(raw_id: Any) -> str:
    """标准化 ID 为字符串"""
    if raw_id is None:
        return ""
    return str(raw_id).strip()


def match_keyword_in_text(text: Any, keywords: Any) -> tuple[str, int] | None:
    """Match the longest configured keyword as a whitespace-delimited token.

    This mirrors the local memelite plugin's non-fuzzy matching: a keyword
    must be at the start/end of the message or next to whitespace.  A QQ
    ``@`` mention is a special boundary, because AstrBot may serialize an
    ``At`` component without inserting spaces (for example,
    ``@小明手办化`` or ``手办化@小明``).  Digits and other attached text are
    not boundaries, so ``手办化123`` cannot trigger a preset.
    """

    def _left_boundary(value: str, index: int) -> bool:
        if index <= 0 or value[index - 1].isspace():
            return True
        token_start = index - 1
        while token_start >= 0 and not value[token_start].isspace():
            token_start -= 1
        token = value[token_start + 1 : index]
        return token.startswith("@") and len(token) > 1

    def _right_boundary(value: str, end: int) -> bool:
        if end >= len(value) or value[end].isspace():
            return True
        token_end = end
        while token_end < len(value) and not value[token_end].isspace():
            token_end += 1
        token = value[end:token_end]
        return token.startswith("@") and len(token) > 1

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
        search_from = 0
        while True:
            index = value.find(keyword, search_from)
            if index < 0:
                break
            end = index + len(keyword)
            if _left_boundary(value, index) and _right_boundary(value, end):
                return keyword, index
            search_from = index + 1
    return None


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


def extract_image_urls_from_text(text: str) -> list[str]:
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
