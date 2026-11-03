import asyncio
import ipaddress
import json
import re
import socket
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp
import aiohttp.abc

_API_VERSION_SEGMENT = re.compile(r"^v\d+(?:(?:alpha|beta)\d*)?$", re.IGNORECASE)
MAX_IMAGE_BYTES = 100 * 1024 * 1024
MAX_API_RESPONSE_BYTES = 150 * 1024 * 1024


def concise_error_detail(value: Any, limit: int = 220) -> str:
    """Extract a short, credential-safe explanation from an upstream error."""
    if isinstance(value, dict):
        code = value.get("code") or value.get("type") or ""
        message = next(
            (
                value.get(key)
                for key in ("message", "detail", "reason", "status_msg", "error")
                if isinstance(value.get(key), (str, dict)) and value.get(key)
            ),
            "",
        )
        if isinstance(message, dict):
            message = concise_error_detail(message, limit)
        text = f"{code}: {message}" if code and message else str(message or code)
    else:
        text = str(value or "")
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            return concise_error_detail(parsed, limit)
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"(?i)bearer\s+[^\s,;]+", "Bearer [redacted]", text)
    text = re.sub(
        r"(?i)\b(api[_ -]?key|token|secret|password)\s*[:=]\s*[^\s,;]+",
        r"\1=[redacted]",
        text,
    )
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[redacted]", text)
    lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in text.splitlines()
        if line.strip()
        and not re.search(r"(?i)(traceback|file \".*\", line \d+| at .+\(.+\))", line)
    ]
    text = " ".join(lines)
    text = re.sub(r"\s+", " ", text).strip(" \t\r\n{}\"'")
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def friendly_generation_error(kind: str, error: Any, guidance: str) -> str:
    """Keep a useful error detail and append one actionable next step."""
    detail = concise_error_detail(error)
    message = f"{kind}生成未能继续：{detail or '接口未提供具体原因。'}"
    if guidance:
        message += "\n" + guidance
    return message


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


def is_public_ip(value: str) -> bool:
    """Return whether an address is globally routable (not loopback/LAN/etc.)."""
    try:
        address = ipaddress.ip_address(str(value).split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return address.is_global and not address.is_multicast


async def ensure_public_url(url: str) -> None:
    """Reject user-supplied URLs that point at the bot host or a private network.

    Args:
        url: An HTTP(S) URL taken from untrusted message text.

    Raises:
        ValueError: If the scheme is unsupported or any resolved address is private.
    """
    parts = urlsplit(str(url or ""))
    host = parts.hostname or ""
    if parts.scheme not in {"http", "https"} or not host:
        raise ValueError("图片链接必须是 http(s) 公网地址。")
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, parts.port or (443 if parts.scheme == "https" else 80)
            )
        except OSError as exc:
            raise ValueError("图片链接的域名无法解析。") from exc
        addresses = [info[4][0] for info in infos]
    if not addresses or not all(is_public_ip(address) for address in addresses):
        raise ValueError("出于安全考虑，不读取指向本机或内网地址的图片链接。")


class PublicResolver(aiohttp.abc.AbstractResolver):
    """Resolve hostnames but only connect to globally routable addresses."""

    def __init__(self):
        self._resolver = aiohttp.DefaultResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        records = [
            record
            for record in await self._resolver.resolve(host, port, family)
            if is_public_ip(record["host"])
        ]
        if not records:
            raise OSError("出于安全考虑，不读取指向本机或内网地址的图片链接。")
        return records

    async def close(self):
        await self._resolver.close()


def norm_id(raw_id: Any) -> str:
    """标准化 ID 为字符串"""
    if raw_id is None:
        return ""
    return str(raw_id).strip()


def match_keyword_in_text(
    text: Any,
    keywords: Any,
    *,
    require_boundaries: bool = True,
) -> tuple[str, int] | None:
    """Match the longest configured keyword.

    When ``require_boundaries`` is enabled, this mirrors the local memelite
    plugin's non-fuzzy matching: a keyword must be at the start/end of the
    message or next to whitespace.  A QQ ``@`` mention is a special boundary,
    because AstrBot may serialize an ``At`` component without inserting spaces
    (for example, ``@小明手办化`` or ``手办化@小明``).  Digits and other
    attached text are not boundaries, so ``手办化123`` cannot trigger a
    preset.  Callers that intentionally keep fuzzy matching can disable this
    check explicitly.
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
            if not require_boundaries or (
                _left_boundary(value, index) and _right_boundary(value, end)
            ):
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
    """从用户文本中提取公网图片链接。

    用户文本不可信，不提取服务器本地文件路径，避免读取宿主机文件并发送给上游。
    """
    image_urls = []

    # 网络 URL；结尾断言需单独允许字符串末尾（字符集中的 $ 只是普通字符）。
    url_patterns = [
        r'https?://[^\s<>"\'\)]+\.(?:jpg|jpeg|png|gif|bmp|webp)(?:\?[^\s<>"\'\)]*)?(?=[\s<>"\'\)|]|$)',
        r'https?://[^\s<>"\'\)]+/(?:s\d+/|upload/|image/|img/|pic/)[^\s<>"\'\)]+\.(?:jpg|jpeg|png|gif|bmp|webp)(?:\?[^\s<>"\'\)]*)?(?=[\s<>"\'\)|]|$)',
        r'https?://youke\d+\.picui\.cn/[^\s<>"\'\)]+\.(?:jpg|jpeg|png|gif|bmp|webp)(?:\?[^\s<>"\'\)]*)?(?=[\s<>"\'\)|]|$)',
    ]
    for pattern in url_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for match in matches:
            if match and match not in image_urls:
                image_urls.append(match)

    return image_urls
