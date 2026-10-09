"""Six explicit video protocols with one paid submission and bounded polling."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp
from PIL import Image

from .api_manager import ApiManager
from .video_router import VideoError, VideoOptions, integer, select_route

VIDEO_MODES = (
    "openai_video",
    "minimax",
    "autodl_native",
    "dashscope",
    "seedance",
    "custom_endpoint",
)
CREATE_PATHS = {
    "openai_video": "/v1/videos",
    "minimax": "/v2/video_generation",
    "autodl_native": "/api/v1/comfyui/comfyui_workflow/{model}",
    "dashscope": "/api/v1/services/aigc/image2video/video-synthesis",
    "seedance": "/api/v3/contents/generations/tasks",
}
QUERY_PATHS = {
    "openai_video": "/v1/videos/{task_id}",
    "minimax": "/v2/query/video_generation/{task_id}",
    "autodl_native": "/api/v1/comfyui/comfyui_workflow/result/{task_id}",
    "dashscope": "/api/v1/tasks/{task_id}",
    "seedance": "/api/v3/contents/generations/tasks/{task_id}",
}

# image min/max, audio min/max, video count, max seconds, resolution group
WORKFLOWS = {
    "minimax_h3_z0903": (1, 6, 1, 3, 0, 15, "A"),
    "minimax_h3_z0902": (1, 6, 0, 0, 0, 15, "A"),
    "minimax_h3_z0901": (0, 0, 0, 0, 0, 15, "B"),
    "minimax_h3_zm_u24": (1, 9, 0, 3, 0, 15, "C"),
    "minimax_h3_zm_u08": (1, 9, 0, 3, 0, 15, "C"),
    "minimax_h3_b99_002": (2, 2, 0, 0, 0, 15, "D"),
    "minimax_h3_b99_001": (0, 0, 0, 0, 0, 15, "D"),
    "minimax_h3_b99_003_12s": (1, 9, 0, 0, 0, 12, "D"),
    "wan2.2animate-v4-motion_retargeting": (1, 1, 0, 0, 1, 0, "E"),
    "minimax_h3_image_audio_to_video_v2_15s": (0, 9, 0, 3, 0, 15, "F"),
    "minimax_h3_lightx2v_v5_15s": (1, 9, 0, 0, 0, 15, "G"),
    "minimax_h3_image_audio_to_video_v2": (0, 9, 0, 3, 0, 10, "H"),
    "minimax_h3_image_audio_to_video": (1, 1, 1, 1, 0, 15, "H"),
    "minimax_h3_lightx2v_v5": (1, 9, 0, 0, 0, 10, "I"),
    "minimax_h3_lightx2v_no_pic": (0, 0, 0, 0, 0, 15, "G"),
    "minimax_h3_lightx2v": (2, 2, 0, 0, 0, 15, "G"),
}
RESOLUTIONS = {
    "A": ("480p", "768p", "1088p", "1440p"),
    "B": ("480p", "768p", "1088p", "1440p"),
    "C": ("480p", "768p"),
    "D": ("736p",),
    "E": ("832p", "464p"),
    "F": ("480p", "768p"),
    "G": ("480p", "768p"),
    "H": ("480p", "768p", "1080p"),
    "I": ("480p", "768p", "1080p"),
}
FRAME_MODELS = {"minimax_h3_lightx2v", "minimax_h3_b99_002"}
NO_TEXT_MODELS = {
    "wan2.2animate-v4-motion_retargeting",
    "minimax_h3_image_audio_to_video",
}
NO_SEED_MODELS = {"minimax_h3_lightx2v_no_pic", "minimax_h3_image_audio_to_video"}


@dataclass
class VideoResult:
    path: Path
    task_id: str
    model: str


def json_path(value, path: str):
    for part in str(path).split("."):
        if isinstance(value, list) and part.isdigit():
            value = value[int(part)] if int(part) < len(value) else None
        elif isinstance(value, dict):
            value = value.get(part)
        else:
            return None
    return value


def endpoint(base: str, path: str) -> str:
    if not base.strip():
        raise VideoError("请先配置生视频接口地址。")
    parsed = urlsplit(base.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise VideoError("生视频接口地址必须是完整 HTTP(S) 地址。")
    if urlsplit(path).netloc:
        raise VideoError("接口路径请填写相对路径；域名统一在接口地址中配置。")
    root = parsed.path.rstrip("/")
    # Accept a root, version prefix, or known complete endpoint, preserving
    # reverse-proxy prefixes. No heuristic removes arbitrary custom paths.
    for suffix in sorted(CREATE_PATHS.values(), key=len, reverse=True):
        pattern = re.escape(suffix).replace(re.escape("{model}"), "[^/]+")
        root = re.sub(pattern + r"$", "", root)
    root = re.sub(r"/(?:api/)?v[123]$", "", root)
    return urlunsplit(
        (parsed.scheme, parsed.netloc, root + "/" + path.lstrip("/"), "", "")
    )


def image_data_url(raw: bytes) -> str:
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.verify()
            mime = Image.MIME.get(image.format)
    except Exception as exc:
        raise VideoError("参考图片格式无法识别。") from exc
    if mime not in {"image/png", "image/jpeg", "image/webp"}:
        with Image.open(io.BytesIO(raw)) as image:
            out = io.BytesIO()
            image.convert("RGB").save(out, format="PNG")
            raw, mime = out.getvalue(), "image/png"
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def media_sources(value: str, kind: str = "") -> list[str]:
    # Commas are part of Data URLs: split on newlines/whitespace only.
    sources = [part for part in re.split(r"\s+", str(value or "").strip()) if part]
    for source in sources:
        if not (
            source.startswith(("https://", "http://"))
            or re.fullmatch(
                r"data:(?:audio|video)/[\w.+-]+;base64,[A-Za-z0-9+/]+=*", source
            )
        ):
            raise VideoError("参考音频/视频请提供 HTTP(S) URL 或标准 Data URL。")
        if source.startswith(("http://", "https://")) and not urlsplit(source).hostname:
            raise VideoError("参考媒体 URL 缺少有效域名。")
        if source.startswith("data:"):
            try:
                raw = base64.b64decode(source.split(",", 1)[1], validate=True)
            except ValueError as exc:
                raise VideoError("参考媒体 Data URL 的 Base64 数据无效。") from exc
            if not raw or len(raw) > 20 * 1024 * 1024:
                raise VideoError("参考媒体 Data URL 为空或超过 20MB，请使用公开 URL。")
        if (
            kind
            and source.startswith("data:")
            and not source.startswith("data:" + kind + "/")
        ):
            raise VideoError(f"参考{kind}的 Data URL 类型不匹配。")
    return sources


def video_params(prompt: str, config: dict, duration: int = 0) -> dict:
    seconds = integer(duration or config.get("duration", 5), "视频时长", 1)
    match = re.search(
        r"(?<![\d.])(-?\d+(?:\.\d+)?)\s*(?:秒|seconds?\b|s\b)", prompt, re.IGNORECASE
    )
    if not duration and match:
        seconds = integer(match.group(1), "视频时长", 1)
    resolution = str(config.get("resolution", "480p")).lower()
    match = re.search(r"(?<!\w)(\d{3,4})[pP](?!\w)", prompt)
    if match and not config.get("_resolution_explicit"):
        resolution = match.group(1) + "p"
    if not re.fullmatch(r"\d{3,4}p", resolution):
        raise VideoError("视频分辨率请填写 480p、768p 等档位。")
    ratio = str(config.get("aspect_ratio", "16:9"))
    match = re.search(r"(?<!\d)(\d{1,3})[:：](\d{1,3})(?!\d)", prompt)
    if match and not config.get("_ratio_explicit"):
        ratio = match.group(1) + ":" + match.group(2)
    if ratio not in {"adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"}:
        raise VideoError("视频比例必须是 adaptive、21:9、16:9、4:3、1:1、3:4 或 9:16。")
    if ratio == "adaptive" and not config.get("_has_reference", True):
        raise VideoError("文生视频必须选择具体比例，不能使用 adaptive。")
    return {
        "duration": seconds,
        "resolution": resolution,
        "ratio": ratio,
        "size": str(config.get("size", "") or "").strip(),
    }


def orientation(ratio: str) -> str:
    return (
        "square"
        if ratio == "1:1"
        else "portrait"
        if ratio in {"9:16", "3:4", "adaptive"}
        else "landscape"
    )


def native_resolution(group: str, resolution: str, ratio: str) -> str:
    direction = orientation(ratio)
    if resolution not in RESOLUTIONS[group]:
        raise VideoError(
            f"该工作流不支持 {resolution}，可用档位：{', '.join(RESOLUTIONS[group])}"
        )
    if direction == "square" and group not in {"C", "D", "G", "I"}:
        raise VideoError("该工作流不支持方形视频，请选择横向或竖向比例。")
    if group == "E":
        if (resolution, direction) not in {("832p", "portrait"), ("464p", "landscape")}:
            raise VideoError("动作迁移仅支持 832p 竖向或 464p 横向。")
        return "464*832px(竖版)" if direction == "portrait" else "832*464px(横版)"
    if group in {"A", "B"}:
        width = int(resolution[:-1])
        height = {
            480: 864,
            768: 1376 if group == "A" else 1344,
            1088: 1920,
            1440: 2560,
        }[width]
        return (
            f"{resolution}竖({width}*{height})"
            if direction == "portrait"
            else f"{resolution}横({height}*{width})"
        )
    return (
        resolution + {"portrait": "竖", "landscape": "横", "square": "(1:1)"}[direction]
    )


def render_template(value, values: dict):
    """Replace exact tokens as typed JSON values, embedded tokens as text."""
    if isinstance(value, str):
        if value in values:
            return values[value]
        for token, replacement in values.items():
            if isinstance(replacement, (str, int, float, bool)):
                value = value.replace(token, str(replacement))
        return value
    if isinstance(value, list):
        return [render_template(item, values) for item in value]
    if isinstance(value, dict):
        return {key: render_template(item, values) for key, item in value.items()}
    return value


class VideoApiManager(ApiManager):
    def __init__(self, config: dict, data_dir: Path):
        super().__init__(config)
        self.data_dir = Path(data_dir) / "video"

    def prepare(self, prompt, images, audios, videos, duration=0, options=None):
        for kind, sources in (("audio", audios), ("video", videos)):
            for source in sources:
                media_sources(source, kind)
        options = options or VideoOptions(duration=duration)
        overrides = options.overrides()
        p = video_params(prompt, {**self.config, **overrides}, options.duration)
        route, effective = select_route(
            self.config, images, audios, videos, p["duration"], overrides
        )
        effective["_has_reference"] = bool(images or audios or videos)
        effective["_resolution_explicit"] = bool(options.resolution)
        effective["_ratio_explicit"] = bool(options.aspect_ratio)
        manager = VideoApiManager(effective, self.data_dir.parent)
        # Requests share the session and key pool while per-task configuration
        # remains independent under concurrent image/audio/video calls.
        manager._get_session = self._get_session
        manager.get_key = self.get_key
        manager.build_request(
            prompt,
            [image_data_url(i) for i in images],
            audios,
            videos,
            options.duration,
        )
        return route, manager

    def build_request(
        self,
        prompt: str,
        images: list[str],
        audios: list[str],
        videos: list[str],
        duration: int = 0,
    ) -> tuple[str, str, dict, dict]:
        c = self.config
        mode = c.get("interface_mode", "openai_video")
        if mode not in VIDEO_MODES:
            raise VideoError("生视频接口模式无效。")
        model = str(c.get("model", "") or "").strip()
        if not model:
            raise VideoError("请配置生视频模型 ID。")
        if c.get("reference_mode") == "first_frame" and mode not in {
            "seedance",
            "custom_endpoint",
        }:
            raise VideoError(
                "首帧图片用途需选择 Seedance 或支持此用途的自定义接口；普通单图请使用 reference。"
            )
        p = video_params(prompt, c, duration)
        caps = WORKFLOWS.get(model)
        if model == "wan2.2-animate-move":
            caps = WORKFLOWS["wan2.2animate-v4-motion_retargeting"]
        if caps and mode in {"openai_video", "minimax", "autodl_native"}:
            imin, imax, amin, amax, vcount, max_seconds, group = caps
            if (
                not imin <= len(images) <= imax
                or not amin <= len(audios) <= amax
                or len(videos) != vcount
            ):
                raise VideoError(
                    f"模型 {model} 需要图片 {imin}–{imax} 张、音频 {amin}–{amax} 段、视频 {vcount} 段；当前输入不符合要求。"
                )
            if max_seconds and p["duration"] > max_seconds:
                raise VideoError(
                    f"模型 {model} 最多支持 {max_seconds} 秒；未提交付费任务。"
                )
            if model not in NO_TEXT_MODELS and not prompt.strip():
                raise VideoError("请提供视频生成提示词。")
            if max_seconds or group == "E":
                native_resolution(group, p["resolution"], p["ratio"])
            if p["size"] and mode == "openai_video":
                if group in {"C", "D", "E"}:
                    raise VideoError(
                        "该工作流不支持精确 size，请留空并使用分辨率/比例。"
                    )
                sizes = set()
                for resolution in RESOLUTIONS[group]:
                    width = int(resolution[:-1])
                    height = (
                        864
                        if width == 480
                        else (1376 if group == "A" else 1344)
                        if width == 768
                        else 1920
                        if width in {1080, 1088}
                        else 2560
                    )
                    sizes.update({f"{width}x{height}", f"{height}x{width}"})
                    if group in {"G", "I"}:
                        sizes.add(f"{width}x{width}")
                if p["size"] not in sizes:
                    raise VideoError(
                        f"模型 {model} 不支持精确尺寸 {p['size']}；请留空或选择文档支持的尺寸。"
                    )
            text_limit = (
                2000000
                if model == "minimax_h3_lightx2v"
                else 500000
                if model in {"minimax_h3_lightx2v_v5", "minimax_h3_lightx2v_v5_15s"}
                else 200000
                if model == "minimax_h3_lightx2v_no_pic"
                else 10000
            )
            if model not in NO_TEXT_MODELS and len(prompt) > text_limit:
                raise VideoError(
                    f"模型 {model} 提示词最多 {text_limit} 字符，当前 {len(prompt)}；未提交收费任务。"
                )
        elif not prompt.strip() and mode != "dashscope":
            raise VideoError("请提供视频生成提示词。")
        headers = {
            "Accept": "application/json, text/event-stream"
            if c.get("use_stream")
            else "application/json"
        }
        seed = integer(
            c.get("seed", -1),
            "随机种子",
            -1,
            2147483647 if mode == "seedance" else 999999999999999,
        )
        if (
            seed == 0
            and caps
            and model not in {"minimax_h3_zm_u24", "minimax_h3_zm_u08"}
            and model not in NO_SEED_MODELS
        ):
            raise VideoError("该工作流随机种子最小为 1；-1 表示不发送种子。")
        if mode == "minimax" and model in {"MiniMax-H3", "MiniMax-H3-Max"}:
            allowed = {"768p", "1440p"} if model == "MiniMax-H3" else {"480p", "768p"}
            minimum = 4 if model == "MiniMax-H3" else 5
            if p["resolution"] not in allowed or not minimum <= p["duration"] <= 15:
                raise VideoError(
                    f"{model} 需要 {minimum}–15 秒和分辨率 {', '.join(sorted(allowed))}。"
                )
            if len(images) > 9 or len(audios) > 3 or videos or (audios and not images):
                raise VideoError(
                    f"{model} 最多 9 图、3 音频；不支持单独音频或参考视频。"
                )
            frames = c.get("reference_mode") == "first_last_frame"
            if frames:
                target = "minimax_h3_lightx2v"
            elif not images:
                target = (
                    "minimax_h3_z0901"
                    if model == "MiniMax-H3"
                    else "minimax_h3_lightx2v_no_pic"
                )
            elif model == "MiniMax-H3":
                target = (
                    "minimax_h3_zm_u24"
                    if len(images) > 6 or p["ratio"] == "1:1"
                    else "minimax_h3_z0903"
                    if audios
                    else "minimax_h3_z0902"
                )
            elif audios:
                target = (
                    "minimax_h3_zm_u08"
                    if p["ratio"] == "1:1"
                    else "minimax_h3_image_audio_to_video_v2_15s"
                    if p["duration"] > 10
                    else "minimax_h3_image_audio_to_video_v2"
                )
            else:
                target = (
                    "minimax_h3_lightx2v_v5_15s"
                    if p["duration"] > 10
                    else "minimax_h3_lightx2v_v5"
                )
            native_resolution(
                WORKFLOWS[target][6],
                p["resolution"],
                "adaptive" if frames else p["ratio"],
            )
        body = {}
        if mode == "openai_video":
            body = {"model": model}
            if model not in NO_TEXT_MODELS and model != "wan2.2-animate-move":
                body.update(prompt=prompt, seconds=p["duration"])
            elif model == "minimax_h3_image_audio_to_video":
                body["seconds"] = p["duration"]
            if not caps or caps[5]:
                if p["size"]:
                    body["size"] = p["size"]
                else:
                    body.update(
                        resolution=p["resolution"], orientation=orientation(p["ratio"])
                    )
            if images:
                refs = [{"image_url": image} for image in images]
                body["input_reference"] = refs[0] if len(refs) == 1 else refs
            if audios:
                body["audios"] = audios
            if videos:
                body["videos"] = videos
        elif mode in {"minimax", "seedance"}:
            content = []
            if model not in NO_TEXT_MODELS and model != "wan2.2-animate-move":
                content.append({"type": "text", "text": prompt})
            frames = (
                model in FRAME_MODELS or c.get("reference_mode") == "first_last_frame"
            )
            if (
                mode == "seedance"
                and c.get("reference_mode", "reference") == "first_frame"
            ):
                if len(images) != 1 or audios or videos:
                    raise VideoError(
                        "Seedance 首帧模式需要恰好一张图片，不混用音频或视频。"
                    )
                frames = True
            elif frames and (len(images) != 2 or audios or videos):
                raise VideoError("首尾帧模式需要恰好两张图片，不混用音频或视频。")
            for i, image in enumerate(images):
                role = (
                    ("first_frame" if i == 0 else "last_frame")
                    if frames
                    else "reference_image"
                )
                content.append(
                    {"type": "image_url", "role": role, "image_url": {"url": image}}
                )
            for kind, sources in (("audio", audios), ("video", videos)):
                content.extend(
                    {
                        "type": kind + "_url",
                        "role": "reference_" + kind,
                        kind + "_url": {"url": source},
                    }
                    for source in sources
                )
            body = {"model": model, "content": content}
            if mode == "seedance":
                body.update(
                    duration=p["duration"],
                    resolution=p["resolution"],
                    ratio="adaptive" if frames else p["ratio"],
                    watermark=bool(c.get("watermark", False)),
                    generate_audio=bool(c.get("generate_audio", False)),
                )
            elif not caps or caps[5] or caps[6] == "E":
                body.update(
                    resolution={"480p": "480P", "768p": "768P", "1440p": "2K"}.get(
                        p["resolution"], p["resolution"]
                    ),
                    ratio="adaptive" if frames else p["ratio"],
                )
                if not caps or caps[5]:
                    body[
                        "audio_duration"
                        if model == "minimax_h3_image_audio_to_video"
                        else "duration"
                    ] = p["duration"]
        elif mode == "autodl_native":
            if not caps:
                raise VideoError(
                    "原生模式需要文档中的工作流 ID；其他模型请使用自定义路径模式。"
                )
            actual_model = (
                "wan2.2animate-v4-motion_retargeting"
                if model == "wan2.2-animate-move"
                else model
            )
            model = actual_model
            if model not in NO_TEXT_MODELS:
                body["prompt"] = prompt
            if caps[5]:
                body[
                    "audio_duration"
                    if model == "minimax_h3_image_audio_to_video"
                    else "duration"
                ] = p["duration"]
                body["resolution"] = native_resolution(
                    caps[6], p["resolution"], p["ratio"]
                )
            elif caps[6] == "E":
                body["resolution"] = native_resolution("E", p["resolution"], p["ratio"])
            if model in FRAME_MODELS:
                body.update(first_frame=images[0], last_frame=images[1])
            elif caps[4]:
                body.update(ref_image=images[0], ref_video=videos[0])
            else:
                body.update({f"ref_image_{i}": value for i, value in enumerate(images)})
                body.update({f"ref_audio_{i}": value for i, value in enumerate(audios)})
        elif mode == "dashscope":
            if (
                model != "wan2.2-animate-move"
                or len(images) != 1
                or len(videos) != 1
                or audios
            ):
                raise VideoError(
                    "DashScope 动作迁移需要 wan2.2-animate-move、恰好一张图片和一个参考视频。"
                )
            headers["X-DashScope-Async"] = "enable"
            body = {
                "model": model,
                "input": {
                    "image_url": images[0],
                    "video_url": videos[0],
                    "watermark": False,
                },
                "parameters": {"mode": "wan-std", "check_image": True},
            }
        else:
            try:
                template = json.loads(c.get("custom_body_template", "{}"))
                extra_headers = json.loads(c.get("custom_headers", "{}"))
            except (ValueError, TypeError) as exc:
                raise VideoError(
                    "自定义请求模板和请求头必须是合法 JSON 对象。"
                ) from exc
            if not isinstance(template, dict) or not isinstance(extra_headers, dict):
                raise VideoError("自定义请求模板和请求头必须是 JSON 对象。")
            body = render_template(
                template,
                {
                    "{model}": model,
                    "{prompt}": prompt,
                    "{duration}": p["duration"],
                    "{resolution}": p["resolution"],
                    "{ratio}": p["ratio"],
                    "{size}": p["size"],
                    "{images}": images,
                    "{image}": images[0] if images else "",
                    "{audios}": audios,
                    "{videos}": videos,
                    "{stream}": bool(c.get("use_stream")),
                },
            )
            headers.update({str(k): str(v) for k, v in extra_headers.items()})
        if (
            seed >= 0
            and mode not in {"dashscope", "custom_endpoint"}
            and model not in NO_SEED_MODELS
        ):
            body["seed"] = seed
        create = (
            c.get("custom_create_path", "/v1/videos")
            if mode == "custom_endpoint"
            else CREATE_PATHS[mode]
        )
        query = (
            c.get("custom_query_path", "/v1/videos/{task_id}")
            if mode == "custom_endpoint"
            else QUERY_PATHS[mode]
        )
        return create.replace("{model}", quote(model, safe="")), query, body, headers

    def parse_task(self, data: dict) -> tuple[str, str, str, str]:
        mode = self.config.get("interface_mode", "openai_video")
        if mode == "custom_endpoint":
            paths = {
                name: self.config.get("custom_" + name + "_path", default)
                for name, default in (
                    ("id", "id"),
                    ("status", "status"),
                    ("url", "url"),
                    ("error", "error.message"),
                )
            }
            return tuple(
                str(json_path(data, paths[name]) or "")
                for name in ("id", "status", "url", "error")
            )
        node = (
            data.get("task", data)
            if mode == "minimax"
            else data.get("output", data)
            if mode == "dashscope"
            else data
        )
        task_id = node.get("id") or node.get("task_id") or ""
        status = node.get("status") or node.get("task_status") or ""
        url = node.get("url") or node.get("video_url") or ""
        content = node.get("content") or {}
        if isinstance(content, dict):
            url = url or content.get("video_url") or content.get("url") or ""
        results = node.get("results") or []
        if isinstance(results, dict):
            url = url or results.get("video_url") or results.get("url") or ""
        elif isinstance(results, list):
            url = url or next(
                (
                    item.get("url", "")
                    for item in results
                    if isinstance(item, dict) and item.get("type", "video") == "video"
                ),
                "",
            )
        error = (
            node.get("error") or node.get("fail_reason") or node.get("message") or ""
        )
        if isinstance(error, dict):
            error = error.get("message") or error.get("code") or "视频任务失败"
        return str(task_id), str(status), str(url), str(error)

    async def _json_request(
        self, method: str, url: str, key: str, headers: dict, body=None
    ) -> dict:
        session = await self._get_session()
        request_headers = {**headers, "Authorization": "Bearer " + key}
        timeout = aiohttp.ClientTimeout(
            total=max(1, int(self.config.get("timeout", 120)))
        )
        async with session.request(
            method,
            url,
            json=body,
            headers=request_headers,
            timeout=timeout,
            proxy=self._get_request_proxy(
                url,
                self.config.get("proxy_url") if self.config.get("use_proxy") else None,
            ),
        ) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise VideoError(
                    f"视频接口 HTTP {resp.status}：{text[:800].replace(key, '[redacted]')}"
                )
            try:
                if "text/event-stream" in resp.headers.get("Content-Type", ""):
                    messages = [
                        json.loads(line[5:].strip())
                        for line in text.splitlines()
                        if line.startswith("data:")
                        and line[5:].strip() not in {"", "[DONE]"}
                    ]
                    # A heartbeat may follow the useful task object.
                    data = next(
                        (
                            item
                            for item in reversed(messages)
                            if isinstance(item, dict) and any(self.parse_task(item)[:3])
                        ),
                        None,
                    )
                else:
                    data = json.loads(text)
            except (ValueError, TypeError) as exc:
                raise VideoError("视频接口返回了无法解析的 JSON/SSE。") from exc
            if not isinstance(data, dict):
                raise VideoError("视频接口没有返回任务对象。")
            return data

    async def generate(
        self,
        prompt: str,
        images: list[bytes],
        audios: list[str] | None = None,
        videos: list[str] | None = None,
        duration: int = 0,
        options=None,
    ) -> VideoResult:
        _, manager = self.prepare(
            prompt, images, audios or [], videos or [], duration, options
        )
        return await manager._generate(
            prompt,
            images,
            audios or [],
            videos or [],
            (options.duration if options else duration),
        )

    async def _generate(self, prompt, images, audios, videos, duration) -> VideoResult:
        create, query, body, headers = self.build_request(
            prompt,
            [image_data_url(i) for i in images],
            audios or [],
            videos or [],
            duration,
        )
        base = str(self.config.get("base_url", "") or "")
        create_url = endpoint(base, create)
        # Check the query template before spending money.
        if "{task_id}" not in query:
            raise VideoError("视频查询路径必须包含 {task_id}。")
        endpoint(base, query.replace("{task_id}", "check"))
        key = await self.get_key()
        if not key:
            raise VideoError("请配置生视频 API Key。")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        job = self.data_dir / (uuid.uuid4().hex + ".json")
        # No retries for POST: network ambiguity must not cause a second charge.
        try:
            data = await self._json_request("POST", create_url, key, headers, body)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise VideoError(
                "视频提交连接中断，任务可能已创建；请先核对上游任务记录，勿立即重复提交。"
            ) from exc
        task_id, status, url, error = self.parse_task(data)
        if not task_id and not url:
            raise VideoError("接口没有返回任务 ID 或视频地址；请核对任务记录后再提交。")
        info = {
            "task_id": task_id,
            "mode": self.config.get("interface_mode"),
            "model": self.config.get("model"),
            "created_at": time.time(),
            "status": status,
        }
        await asyncio.to_thread(
            job.write_text, json.dumps(info, ensure_ascii=False), "utf-8"
        )
        deadline = time.monotonic() + max(1, int(self.config.get("poll_timeout", 900)))
        interval = max(1.0, float(self.config.get("poll_interval", 5)))
        success = {"completed", "succeeded", "success"}
        failure = {"failed", "failure", "cancelled", "canceled", "expired"}
        if self.config.get("interface_mode") == "custom_endpoint":
            success.update(
                s.strip().lower()
                for s in str(self.config.get("custom_success_status", "")).split(",")
                if s.strip()
            )
            failure.update(
                s.strip().lower()
                for s in str(self.config.get("custom_failure_status", "")).split(",")
                if s.strip()
            )
        transient_errors = 0
        while True:
            normalized = status.strip().lower()
            if normalized in failure:
                info.update(status=status)
                await asyncio.to_thread(
                    job.write_text, json.dumps(info, ensure_ascii=False), "utf-8"
                )
                raise VideoError(f"视频任务失败（ID：{task_id}）：{error or status}")
            if normalized in success or (url and not status):
                if not url:
                    raise VideoError(f"任务 {task_id} 已完成，但接口未返回视频地址。")
                break
            if time.monotonic() >= deadline:
                raise VideoError(
                    f"视频等待超时；任务 ID：{task_id}。任务记录已保留，请查询上游结果，勿重复提交。"
                )
            await asyncio.sleep(min(interval, max(0.0, deadline - time.monotonic())))
            try:
                poll_url = endpoint(
                    base, query.replace("{task_id}", quote(task_id, safe=""))
                )
                remaining = max(0.01, deadline - time.monotonic())
                data = await asyncio.wait_for(
                    self._json_request("GET", poll_url, key, headers), remaining
                )
                _, status, url, error = self.parse_task(data)
                transient_errors = 0
            except (aiohttp.ClientError, asyncio.TimeoutError, VideoError) as exc:
                transient_errors += 1
                if transient_errors > 3 or time.monotonic() >= deadline:
                    raise VideoError(
                        f"视频查询中断；任务 ID：{task_id}。任务仍可在上游查询，请勿重复提交。"
                    ) from exc
        info.update(status="completed", url=url)
        await asyncio.to_thread(
            job.write_text, json.dumps(info, ensure_ascii=False), "utf-8"
        )
        try:
            path = await self.download_video(url, key, base)
        except (VideoError, OSError) as exc:
            raise VideoError(
                f"视频已完成但下载失败（ID：{task_id}）：{exc}。任务记录包含下载地址，请勿重复生成。"
            ) from exc
        info.update(status="completed", path=str(path))
        await asyncio.to_thread(
            job.write_text, json.dumps(info, ensure_ascii=False), "utf-8"
        )
        return VideoResult(path, task_id, str(self.config.get("model")))

    async def download_video(self, url: str, key: str, base: str) -> Path:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"}:
            raise VideoError("视频结果必须是 HTTP(S) 下载地址。")
        session = await self._get_session()
        path = self.data_dir / (uuid.uuid4().hex + ".mp4")
        timeout = int(self.config.get("result_video_download_timeout", 300)) or int(
            self.config.get("timeout", 120)
        )
        retries = max(
            0, min(5, int(self.config.get("result_video_download_retries", 2)))
        )
        max_size = max(1, int(self.config.get("max_video_size_mb", 100))) * 1024 * 1024
        # Public artifact URLs never receive the API key, even on the same host.
        # Authorization is reserved for the OpenAI ownership-checked /content URL.
        b = urlsplit(base)
        protected = (parsed.scheme, parsed.netloc) == (
            b.scheme,
            b.netloc,
        ) and re.search(r"/v1/videos/[^/]+/content$", parsed.path)
        headers = {"Authorization": "Bearer " + key} if protected else {}
        for attempt in range(retries + 1):
            try:
                async with session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                    proxy=self._get_request_proxy(
                        url,
                        self.config.get("proxy_url")
                        if self.config.get("use_proxy")
                        else None,
                    ),
                ) as resp:
                    resp.raise_for_status()
                    if resp.content_length and resp.content_length > max_size:
                        raise VideoError(
                            "视频超过下载大小上限，请在配置中调整最大视频大小。"
                        )
                    total = 0
                    with path.open("wb") as output:
                        async for chunk in resp.content.iter_chunked(64 * 1024):
                            total += len(chunk)
                            if total > max_size:
                                raise VideoError("视频超过下载大小上限。")
                            output.write(chunk)
                with path.open("rb") as check:
                    head = check.read(64)
                if total < 12 or b"ftyp" not in head:
                    raise VideoError("下载结果不是有效 MP4 视频。")
                return path
            except (aiohttp.ClientError, asyncio.TimeoutError):
                path.unlink(missing_ok=True)
                if attempt == retries:
                    raise VideoError(
                        "视频已生成，但下载失败；任务记录已保留，可从上游取回视频。"
                    )
                await asyncio.sleep(1)
            except BaseException:
                path.unlink(missing_ok=True)
                raise
        raise VideoError("视频下载失败。")
