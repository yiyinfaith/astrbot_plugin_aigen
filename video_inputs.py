"""Current/quoted audio and video inputs, with OneBot file-token resolution."""

from __future__ import annotations

import asyncio
import base64
import copy
import re
from pathlib import Path
from urllib.parse import urlsplit

from astrbot.api import logger

from .video_api_manager import local_media_path, media_sources
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
        getattr(segment, "file_name", ""),
        _component_source(segment),
    ]
    mime = str(
        getattr(segment, "content_type", "") or getattr(segment, "mime_type", "") or ""
    ).lower()
    suffixes = {
        Path(urlsplit(str(value or "")).path).suffix.lower() for value in values
    }
    if mime.startswith("audio/") or suffixes & AUDIO_EXTENSIONS:
        return "audio"
    if mime.startswith("video/") or suffixes & VIDEO_EXTENSIONS:
        return "video"
    return "image" if mime.startswith("image/") or suffixes & IMAGE_EXTENSIONS else ""


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
        path = local_media_path(source)
        if not path.is_file():
            raise VideoError(f"无法读取参考{kind}；请重新发送并引用媒体文件。")
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
    event, image_manager, kinds=None, quoted_only=False, preserve_duplicates=False
) -> tuple[list[str], list[str]]:
    audios, videos = [], []
    kinds = {"audio", "video"} if kinds is None else set(kinds)

    async def collect(chain, quoted=False):
        for seg in chain:
            name = type(seg).__name__
            if name == "Reply":
                nested_chain = getattr(
                    seg, "chain", None
                ) or await image_manager._fetch_reply_components(
                    event, getattr(seg, "id", None)
                )
                await collect(
                    [item for item in nested_chain if type(item).__name__ != "Reply"],
                    True,
                )
            elif name in {"Record", "Audio", "Video", "File"}:
                kind = component_media_kind(seg)
                if kind not in kinds:
                    continue
                if quoted_only and not quoted:
                    continue
                source = _component_source(seg)
                converter = getattr(seg, "convert_to_file_path", None)
                if name == "Record" and callable(converter):
                    # AstrBot's Record converter downloads and decodes QQ
                    # SILK/AMR into WAV through the host's media utilities.
                    try:
                        source = await asyncio.wait_for(converter(), timeout=120)
                    except Exception as exc:
                        raise VideoError(
                            "QQ 语音读取或转码失败，请重新发送语音，或改发 WAV/MP3 文件。"
                        ) from exc
                source = await resolve_media(source, kind, event)
                target = videos if kind == "video" else audios
                if preserve_duplicates or source not in target:
                    target.append(source)
            elif name == "Plain":
                # Media must come from a message component. Text URLs are not
                # accepted as command/tool media parameters.
                continue

    await collect(image_manager._event_chain(event), False)
    return audios, videos


async def normalize_media_event(event, image_manager):
    """Resolve File tokens and recover files omitted by a OneBot adapter.

    Args:
        event: Original AstrBot event, kept unchanged.
        image_manager: The plugin's image manager for adapter lookups.

    Returns:
        The original event or a shallow event view with normalized components.

    Raises:
        VideoError: If a supplied file cannot be resolved or classified.
    """
    chain = image_manager._event_chain(event)
    message = getattr(event, "message_obj", None)
    raw = getattr(message, "raw_message", None)
    raw_chain = (
        raw.get("message", []) if isinstance(raw, dict) else getattr(raw, "message", [])
    )
    raw_files = (
        [
            item
            for item in raw_chain
            if isinstance(item, dict) and item.get("type") == "file"
        ]
        if isinstance(raw_chain, list)
        else []
    )
    files = [seg for seg in chain if type(seg).__name__ == "File"]
    if len(raw_files) > len(files):
        recovered = await image_manager._fetch_reply_components(
            event, getattr(message, "message_id", None)
        )
        if len([seg for seg in recovered if type(seg).__name__ == "File"]) < len(
            raw_files
        ):
            raise VideoError(
                "QQ 文件消息未能读取完整，请单独重新发送文件；未提交生成任务。"
            )
        chain = recovered
        files = [seg for seg in chain if type(seg).__name__ == "File"]
    for seg in files:
        if not component_media_kind(seg):
            raise VideoError(
                f"不支持的材料文件：{getattr(seg, 'name', '未知文件')}；请发送图片、音频或视频文件。"
            )
        source = _component_source(seg)
        if source.startswith(
            ("http://", "https://", "data:", "base64://", "file://")
        ) or (source and Path(source).is_file()):
            continue
        bot = await image_manager._resolve_bot_for_context(event)
        action = getattr(bot, "call_action", None) or getattr(
            getattr(bot, "api", None), "call_action", None
        )
        file_id = getattr(seg, "file_id", None) or getattr(seg, "id", None) or source
        url = ""
        if action and file_id:
            group = str(event.get_group_id() or "")
            actions = [("get_file", {"file_id": file_id})]
            actions.append(
                (
                    "get_group_file_url",
                    {
                        "group_id": int(group) if group.isdigit() else group,
                        "file_id": file_id,
                    },
                )
                if group
                else ("get_private_file_url", {"file_id": file_id})
            )
            for name, params in actions:
                try:
                    result = await asyncio.wait_for(action(name, **params), timeout=30)
                    result = result.get("data", result)
                    url = str(result.get("url") or result.get("file") or "")
                    if url and (
                        url.startswith(("http://", "https://", "data:", "base64://"))
                        or local_media_path(url).is_file()
                    ):
                        break
                    url = ""
                except Exception as exc:  # noqa: BLE001
                    logger.debug("File lookup action %s failed: %s", name, exc)
                    continue
        if not url:
            raise VideoError("平台无法读取材料文件，请重新发送文件；未提交生成任务。")
        replacement = copy.copy(seg)
        replacement.url = url
        chain[chain.index(seg)] = replacement
    if chain == image_manager._event_chain(event):
        return event
    view = copy.copy(event)
    if message is not None:
        view.message_obj = copy.copy(message)
        view.message_obj.message = chain
    # Only the plugin's collectors use this view; the framework event is intact.
    view.get_messages = lambda: chain
    return view
