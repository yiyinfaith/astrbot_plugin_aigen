"""Current/quoted audio and video inputs, with OneBot file-token resolution."""

from __future__ import annotations

import asyncio
import base64
import re
from pathlib import Path
from urllib.parse import unquote

from .video_api_manager import media_sources
from .video_router import VideoError

MEDIA_URL = re.compile(
    r"https?://[^\s<>\"']+\.(mp4|webm|mov|wav|mp3|flac|m4a|ogg)(?:\?[^\s<>\"']*)?",
    re.IGNORECASE,
)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".amr", ".silk"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v"}


def _component_source(segment) -> str:
    """Read a component's URL without touching File.file (which may download)."""
    if type(segment).__name__ == "File":
        values = (getattr(segment, "url", ""), getattr(segment, "file_", ""))
    else:
        values = (
            getattr(segment, "url", ""),
            getattr(segment, "file", ""),
            getattr(segment, "path", ""),
        )
    return next((str(value).strip() for value in values if value), "")


def component_media_kind(segment) -> str:
    """Classify Record/Video/File components using MIME/name/URL metadata."""
    name = type(segment).__name__
    if name == "Record" or name == "Audio":
        return "audio"
    if name == "Video":
        return "video"
    if name != "File":
        return ""
    values = [
        getattr(segment, "name", ""),
        getattr(segment, "content_type", ""),
        getattr(segment, "mime_type", ""),
        _component_source(segment),
    ]
    text = " ".join(str(value or "").lower() for value in values)
    if "audio/" in text or any(ext in text for ext in AUDIO_EXTENSIONS):
        return "audio"
    if "video/" in text or any(ext in text for ext in VIDEO_EXTENSIONS):
        return "video"
    return "image" if "image/" in text or any(ext in text for ext in IMAGE_EXTENSIONS) else ""


def text_media(text: str) -> tuple[list[str], list[str]]:
    audio, video = [], []
    for match in MEDIA_URL.finditer(text):
        (video if match[1].lower() in {"mp4", "webm", "mov"} else audio).append(
            match[0]
        )
    return audio, video


async def resolve_media(source: str, kind: str, event=None) -> str:
    source = str(source or "").strip()
    if not source:
        raise VideoError(f"参考{kind}没有可读取的地址。")
    if source.startswith("data:"):
        media_sources(source, kind)
        return source
    # QQ voice/file tokens are not public media URLs. Ask the adapter to export
    # audio as MP3; never label SILK/AMR bytes as MPEG.
    bot = getattr(event, "bot", None)
    action = getattr(bot, "call_action", None) or getattr(
        getattr(bot, "api", None), "call_action", None
    )
    opaque = (
        not source.startswith(("http://", "https://", "file://", "base64://"))
        and not Path(source).is_file()
    )
    if action and (
        opaque
        or (
            kind == "audio"
            and re.search(r"\.(?:silk|amr)(?:\?|$)", source, re.IGNORECASE)
        )
    ):
        try:
            data = await action(
                "get_record" if kind == "audio" else "get_video",
                file=source,
                **({"out_format": "mp3"} if kind == "audio" else {}),
            )
            data = data.get("data", data)
            source = str(data.get("url") or data.get("file") or "")
        except Exception as exc:
            raise VideoError(
                f"平台无法读取参考{kind}，请使用可下载的 URL 或重新发送文件。"
            ) from exc
    if source.startswith(("http://", "https://")):
        media_sources(source, kind)
        return source
    if source.startswith("base64://"):
        try:
            raw = base64.b64decode(source[9:], validate=True)
        except ValueError as exc:
            raise VideoError("参考媒体 Base64 无效。") from exc
        suffix = ""
    else:
        local = unquote(source[8:]) if source.startswith("file:///") else source
        path = Path(local)
        if not path.is_file():
            raise VideoError(f"无法读取参考{kind}；请提供公开 URL 或本地有效文件。")
        if path.stat().st_size > 20 * 1024 * 1024:
            raise VideoError("内联参考媒体超过 20MB，请提供公开 URL。")
        raw = await asyncio.to_thread(path.read_bytes)
        suffix = path.suffix.lower()
    if len(raw) > 20 * 1024 * 1024:
        raise VideoError("内联参考媒体超过 20MB，请提供公开 URL。")
    if raw.startswith((b"#!AMR", b"#!SILK", b"\x02#!SILK")):
        raise VideoError(
            "平台语音为 AMR/SILK 且未能转成 MP3，请改用 WAV/MP3 音频或可下载的音频 URL。"
        )
    if kind == "video":
        mime = (
            "video/webm"
            if raw.startswith(b"\x1a\x45\xdf\xa3")
            else "video/mp4"
            if b"ftyp" in raw[:64]
            else ""
        )
    else:
        mime = (
            "audio/wav"
            if raw.startswith(b"RIFF")
            else "audio/flac"
            if raw.startswith(b"fLaC")
            else "audio/ogg"
            if raw.startswith(b"OggS")
            else "audio/mp4"
            if b"ftyp" in raw[:64]
            else "audio/mpeg"
            if raw.startswith(b"ID3")
            or (len(raw) > 1 and raw[0] == 255 and raw[1] & 224 == 224)
            else ""
        )
    if not mime:
        raise VideoError(
            f"无法识别参考{kind}格式（{suffix or '无后缀'}），请使用 MP4/WebM 视频或 WAV/MP3/FLAC/OGG/M4A 音频。"
        )
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


async def collect_media(
    event, image_manager, kinds=None, quoted_only=False
) -> tuple[list[str], list[str]]:
    audios, videos = [], []
    kinds = {"audio", "video"} if kinds is None else set(kinds)

    async def collect(chain, quoted=False):
        for seg in chain:
            name = type(seg).__name__
            if name == "Reply":
                chain = getattr(
                    seg, "chain", None
                ) or await image_manager._fetch_reply_components(
                    event, getattr(seg, "id", None)
                )
                await collect(
                    [item for item in chain if type(item).__name__ != "Reply"], True
                )
            elif name in {"Record", "Audio", "Video", "File"}:
                kind = component_media_kind(seg)
                if kind not in kinds:
                    continue
                if quoted_only and not quoted:
                    continue
                source = _component_source(seg)
                source = await resolve_media(source, kind, event)
                target = videos if kind == "video" else audios
                if source not in target:
                    target.append(source)
            elif name == "Plain":
                # Media must come from a message component. Text URLs are not
                # accepted as command/tool media parameters.
                continue

    await collect(image_manager._event_chain(event), False)
    return audios, videos


def strip_media_text(prompt: str) -> str:
    return re.sub(r"\s+", " ", MEDIA_URL.sub("", prompt)).strip()
