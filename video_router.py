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
    "last_frame": ("尾帧生视频", "", ""),
    "motion_transfer": ("动作迁移（图片+视频生视频）", "wan2.2-animate-move", ""),
    "audio_sync": (
        "音频同步（自动对口型，图片+音频生视频）",
        "minimax_h3_image_audio_to_video",
        "",
    ),
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
    "image_video_to_video": ("图片＋视频生视频", "", ""),
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
    prompt_segments: int = 0

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
            "last_frame",
            "first_last_frame",
            "motion_transfer",
            "audio_sync",
        }:
            raise VideoError(
                "图片用途必须是 auto、reference、first_frame、last_frame、first_last_frame、motion_transfer 或 audio_sync。"
            )
        return values


def parse_video_command(text: str | list[str]) -> VideoOptions:
    """Parse flags and count messages that contribute actual prompt text.

    Args:
        text: One command or ordered text messages joined with spaces.

    Returns:
        Parsed options with one merged prompt and its contributing message count.

    Raises:
        VideoError: If an option or explicit route is invalid.
    """
    fragments = text if isinstance(text, list) else [text]
    text = " ".join(fragments)
    result = VideoOptions()
    pattern = re.compile(r"(?<!\S)--([a-z-]+)(?:=|\s+)?")
    cursor, parts = 0, []
    spans = []
    while match := pattern.search(text, cursor):
        parts.append(text[cursor : match.start()])
        spans.append((cursor, match.start()))
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
    spans.append((cursor, len(text)))
    result.prompt = " ".join(part.strip() for part in parts if part.strip())
    directive = re.match(
        r"^(首帧|尾帧|首尾帧|动作迁移|音频同步)(?:\s+|$)", result.prompt
    )
    if directive:
        mode = {
            "首帧": "first_frame",
            "尾帧": "last_frame",
            "首尾帧": "first_last_frame",
            "动作迁移": "motion_transfer",
            "音频同步": "audio_sync",
        }[directive[1]]
        if result.reference_mode not in {"auto", mode}:
            raise VideoError(
                "路由关键词与 --reference-mode/--frames 冲突，请只选择一种模式。"
            )
        result.reference_mode = mode
        result.prompt = result.prompt[directive.end() :].strip()
    # Map the retained prompt back to the original messages. This also handles
    # option values split across messages without counting them as prompt text.
    skip = directive.end() if directive else 0
    kept = []
    for part, (start, end) in zip(parts, spans):
        stripped = part.strip()
        if not stripped:
            continue
        left = start + len(part) - len(part.lstrip())
        right = end - len(part) + len(part.rstrip())
        if skip < len(stripped):
            kept.append((left + skip, right))
        skip = max(0, skip - len(stripped) - 1)
    offset, span_index = 0, 0
    for fragment in fragments:
        end = offset + len(fragment)
        while span_index < len(kept) and kept[span_index][1] <= offset:
            span_index += 1
        for index in range(span_index, len(kept)):
            left, right = kept[index]
            if left >= end:
                break
            if text[max(left, offset) : min(right, end)].strip():
                result.prompt_segments += 1
                break
        offset = end + 1
    result.overrides()
    return result


def select_route(
    config: dict,
    images: list,
    audios: list,
    videos: list,
    seconds: int,
    overrides: dict | None = None,
    prompt: str = "",
) -> tuple[str, dict]:
    overrides = overrides or {}
    reference = overrides.get("reference_mode", "auto")
    if reference == "auto":
        # Image count never implies frame semantics. Only an explicit directive
        # or a tool's explicit reference_mode can select a special route.
        reference = "reference"
    if reference in {"motion_transfer", "audio_sync"}:
        label = ROUTES[reference][0]
        required = (1, 0, 1) if reference == "motion_transfer" else (1, 1, 0)
        if (len(images), len(audios), len(videos)) != required:
            raise VideoError(
                f"{label}需要恰好 {required[0]} 张图片、{required[1]} 段音频、{required[2]} 段视频；"
                f"当前图片 {len(images)}、音频 {len(audios)}、视频 {len(videos)}。"
            )
        if str(prompt or "").strip():
            raise VideoError(f"{label}不支持输入提示词，请只发送路由关键词和所需媒体。")
        route = reference
    elif reference in {"first_frame", "last_frame", "first_last_frame"}:
        count = 2 if reference == "first_last_frame" else 1
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
    if (
        not videos
        and reference not in {"motion_transfer", "audio_sync"}
        and (len(images) > 9 or len(audios) > 3)
    ):
        raise VideoError(
            f"此类生视频最多接收 9 张图片、3 段音频；当前图片 {len(images)}、音频 {len(audios)}。未提交生成任务。"
        )
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
        short_model = str(rule.get("model", default_model) or "").strip()
        configured_long_model = rule.get("long_model", default_long)
        long_model = str(configured_long_model or "").strip()
        # A long interface belongs to the concrete long-model selection.  If
        # no long model is configured, keep using the short model and its
        # interface even when the requested duration exceeds ten seconds.
        is_long_model = seconds > 10 and bool(long_model)
        model = long_model if is_long_model else short_model
        if not model:
            raise VideoError(
                f"检测到{label}（图片 {len(images)}、音频 {len(audios)}、视频 {len(videos)}），请先在“生视频设置 → 自动路由模型 → {label}”配置模型和接口格式；未提交收费任务。"
            )
        effective["model"] = model
        mode_key = "long_interface_mode" if is_long_model else "interface_mode"
        mode = rule.get(mode_key, "inherit" if is_long_model else "minimax")
        if mode == "inherit" and is_long_model:
            mode = rule.get("interface_mode", "inherit")
        if mode != "inherit":
            effective["interface_mode"] = mode
        for key in ("resolution", "aspect_ratio"):
            value = rule.get(
                key,
                "464p" if route == "motion_transfer" and key == "resolution" else "",
            )
            if value:
                effective[key] = value
    effective.update(overrides)
    effective["reference_mode"] = reference
    return route, effective
