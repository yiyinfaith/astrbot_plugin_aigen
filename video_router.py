"""Shared, deterministic media routing and command options (no network calls)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


class VideoError(RuntimeError):
    pass


ROUTES = {
    "text_to_video": ("文生视频", "minimax_h3_lightx2v_no_pic", ""),
    "image_to_video": (
        "图生视频",
        "minimax_h3_lightx2v_v5",
        "minimax_h3_lightx2v_v5_15s",
    ),
    "first_last_frame": ("首尾帧生视频", "minimax_h3_lightx2v", ""),
    "first_frame": ("首帧生视频", "", ""),
    "audio_to_video": (
        "音频生视频",
        "minimax_h3_image_audio_to_video_v2",
        "minimax_h3_image_audio_to_video_v2_15s",
    ),
    "image_audio_to_video": (
        "图片＋音频生视频",
        "minimax_h3_image_audio_to_video_v2",
        "minimax_h3_image_audio_to_video_v2_15s",
    ),
    "video_to_video": ("视频生视频", "", ""),
    "image_video_to_video": ("图片＋视频生视频", "wan2.2-animate-move", ""),
    "audio_video_to_video": ("音频＋视频生视频", "", ""),
    "image_audio_video_to_video": ("图片＋音频＋视频生视频", "", ""),
}


def integer(value, label: str, minimum: int, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"-?\d+", str(value)):
        raise VideoError(f"{label}必须是整数。")
    result = int(value)
    if result < minimum or (maximum is not None and result > maximum):
        suffix = f"{minimum}–{maximum}" if maximum is not None else f"至少 {minimum}"
        raise VideoError(f"{label}必须为 {suffix}。")
    return result


@dataclass
class VideoOptions:
    prompt: str = ""
    images: list[str] = field(default_factory=list)
    audios: list[str] = field(default_factory=list)
    videos: list[str] = field(default_factory=list)
    duration: int = 0
    resolution: str = ""
    aspect_ratio: str = ""
    reference_mode: str = "auto"
    seed: int = -2
    generate_audio: str = "auto"

    def overrides(self) -> dict:
        values = {}
        if self.duration:
            values["duration"] = self.duration
        if self.resolution:
            values["resolution"] = self.resolution.lower()
        if self.aspect_ratio:
            values["aspect_ratio"] = self.aspect_ratio
        if self.reference_mode != "auto":
            values["reference_mode"] = self.reference_mode
        if self.seed != -2:
            values["seed"] = self.seed
        if self.generate_audio != "auto":
            if self.generate_audio not in {"true", "false"}:
                raise VideoError("generate_audio 只能是 auto、true 或 false。")
            values["generate_audio"] = self.generate_audio == "true"
        integer(self.duration, "时长", 0)
        integer(self.seed, "随机种子", -2, 999999999999999)
        if self.reference_mode not in {
            "auto",
            "reference",
            "first_frame",
            "first_last_frame",
        }:
            raise VideoError(
                "图片用途必须是 auto、reference、first_frame 或 first_last_frame。"
            )
        return values


def parse_video_command(text: str) -> VideoOptions:
    """Parse flags without mangling natural language, URL queries or quotes."""
    result = VideoOptions()
    pattern = re.compile(r"(?<!\S)--([a-z-]+)(?:=|\s+)?")
    cursor, parts = 0, []
    while match := pattern.search(text, cursor):
        parts.append(text[cursor : match.start()])
        name = match.group(1)
        start = match.end()
        if name == "frames":
            result.reference_mode = "first_last_frame"
            cursor = start
            continue
        if name not in {
            "image",
            "audio",
            "video",
            "duration",
            "resolution",
            "ratio",
            "seed",
            "reference-mode",
            "generate-audio",
        }:
            raise VideoError(
                f"不支持参数 --{name}；可用 --image/--audio/--video/--duration/--resolution/--ratio/--frames/--reference-mode/--seed/--generate-audio。"
            )
        value_match = re.match(
            r'"([^"\n]+)"|\x27([^\x27\n]+)\x27|([^\s]+)', text[start:]
        )
        if not value_match:
            raise VideoError(f"--{name} 缺少参数值。")
        value = next(v for v in value_match.groups() if v is not None)
        if value.startswith("--"):
            raise VideoError(f"--{name} 缺少参数值。")
        cursor = start + value_match.end()
        if name in {"image", "audio", "video"}:
            getattr(result, name + "s" if name != "audio" else "audios").append(value)
        elif name in {"duration", "seed"}:
            setattr(result, name, integer(value, name, 1 if name == "duration" else -1))
        else:
            setattr(
                result,
                {
                    "ratio": "aspect_ratio",
                    "reference-mode": "reference_mode",
                    "generate-audio": "generate_audio",
                }.get(name, name),
                value,
            )
    parts.append(text[cursor:])
    result.prompt = " ".join(part.strip() for part in parts if part.strip())
    result.overrides()
    return result


def select_route(
    config: dict,
    images: list,
    audios: list,
    videos: list,
    seconds: int,
    overrides: dict | None = None,
) -> tuple[str, dict]:
    overrides = overrides or {}
    reference = overrides.get(
        "reference_mode", config.get("reference_mode", "reference")
    )
    if reference in {"first_frame", "first_last_frame"}:
        count = 1 if reference == "first_frame" else 2
        if len(images) != count or audios or videos:
            raise VideoError(
                f"{reference} 需要恰好 {count} 张图片，不能混用音频或视频；当前图片 {len(images)}、音频 {len(audios)}、视频 {len(videos)}。"
            )
        route = reference
    elif videos:
        route = (
            "image_audio_video_to_video"
            if images and audios
            else "image_video_to_video"
            if images
            else "audio_video_to_video"
            if audios
            else "video_to_video"
        )
    elif audios:
        route = "image_audio_to_video" if images else "audio_to_video"
    else:
        route = "image_to_video" if images else "text_to_video"
    effective = dict(config)
    if config.get("auto_route", True):
        label, default_model, default_long = ROUTES[route]
        rules = config.get("routes", {})
        if isinstance(rules, list):
            matches = [
                item
                for item in rules
                if isinstance(item, dict) and item.get("__template_key") == route
            ]
            if len(matches) > 1:
                raise VideoError(f"{label}配置了多条路线，请保留一条；未提交收费任务。")
            if not matches:
                raise VideoError(f"请在自动路由模型中添加{label}路线；未提交收费任务。")
            rule = matches[0]
        else:
            rule = rules.get(route, {})
        model = str(rule.get("model", default_model) or "").strip()
        if seconds > 10:
            model = str(rule.get("long_model", default_long) or model).strip()
        if not model:
            raise VideoError(
                f"检测到{label}（图片 {len(images)}、音频 {len(audios)}、视频 {len(videos)}），请先在“生视频设置 → 自动路由模型 → {label}”配置模型和接口格式；未提交收费任务。"
            )
        effective["model"] = model
        mode = rule.get("interface_mode", "inherit")
        if mode != "inherit":
            effective["interface_mode"] = mode
        for key in ("resolution", "aspect_ratio"):
            value = rule.get(
                key,
                "464p"
                if route == "image_video_to_video" and key == "resolution"
                else "",
            )
            if value:
                effective[key] = value
    effective.update(overrides)
    return route, effective
