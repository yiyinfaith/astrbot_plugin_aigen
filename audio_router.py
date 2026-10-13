"""Audio intent parsing and schema-backed routing."""

from __future__ import annotations

from dataclasses import dataclass

from .video_router import VideoError

AUDIO_ROUTES = {
    "text_to_audio": "提示词生音频（TTA）",
    "text_to_speech": "文字生音频（TTS）",
    "reference_to_speech": "文字＋参考音频生音频（音色克隆）",
    "reference_to_audio": "提示词＋参考音频生音频（音频条件 TTA）",
    "audio_editing": "音频编辑",
    "voice_conversion": "音色转换",
    "speech_editing": "语音编辑",
}
AUDIO_MODES = (
    "minimax",
    "openai_speech",
    "openai_chat",
    "minimax_speech",
    "gemini_official",
    "dashscope",
    "autodl_native",
    "custom_endpoint",
)
AUDIO_TYPES = {
    "音色克隆": "reference_to_speech",
    "参考音频生成": "reference_to_audio",
    "音频编辑": "audio_editing",
    "语音编辑": "speech_editing",
    "音色转换": "voice_conversion",
}


@dataclass
class AudioOptions:
    """Keep reading text separate from descriptive audio prompts."""

    mode: str = "朗读"
    prompt: str = ""
    prompt_segments: int = 0
    route: str = ""


def parse_audio_materials(fragments: list[str], mode: str) -> AudioOptions:
    """Merge material messages without interpreting literal reading text.

    Args:
        fragments: Ordered text messages collected from the user.
        mode: The required entry selector, 朗读 or 提示词.

    Returns:
        One merged text and the count of contributing messages.

    Raises:
        VideoError: If the selector is unknown or conflicting.
    """
    if mode not in {"朗读", "提示词"}:
        raise VideoError("生音频只能从“朗读”和“提示词”里面选。")
    options = AudioOptions(mode=mode)
    texts = [str(fragment).strip() for fragment in fragments if str(fragment).strip()]
    options.prompt = " ".join(texts)
    options.prompt_segments = len(texts)
    return options


def select_audio_route(
    config: dict, options: AudioOptions, audios: list
) -> tuple[str, dict]:
    """Validate materials, then choose one configured audio route.

    Args:
        config: Audio settings, including native template-list routes.
        options: Reading or prompting intent and optional explicit operation.
        audios: Ordered reference audio sources.

    Returns:
        The route key and independent effective settings.

    Raises:
        VideoError: If materials or route configuration are invalid.
    """
    route = options.route
    if not route:
        route = (
            ("reference_to_speech" if audios else "text_to_speech")
            if options.mode == "朗读"
            else ("reference_to_audio" if audios else "text_to_audio")
        )
    if route not in AUDIO_ROUTES:
        raise VideoError("不支持的音频类型。")
    count = len(audios)
    if route == "voice_conversion":
        if count != 2 or options.prompt:
            raise VideoError(
                "音色转换需要恰好两段音频：先原语音，后目标音色；不输入正文文字。"
            )
    else:
        if not options.prompt:
            raise VideoError("请发送要朗读的文字或音频提示词后再 /生成。")
        if route == "audio_editing" and options.mode != "提示词":
            raise VideoError("音频编辑请使用 /生音频 提示词，输入编辑指令和原音频。")
        if route == "speech_editing" and options.mode != "朗读":
            raise VideoError("语音编辑请使用 /生音频 朗读，输入修改后的文字和原语音。")
        if route == "reference_to_speech" and options.mode != "朗读":
            raise VideoError("音色克隆请使用 /生音频 朗读。")
        if route == "reference_to_audio" and options.mode != "提示词":
            raise VideoError("参考音频生成请使用 /生音频 提示词。")
        if route in {"reference_to_speech", "reference_to_audio"} and not audios:
            raise VideoError("此音频类型需要至少一段参考音频。")
        if route in {"audio_editing", "speech_editing"} and count != 1:
            raise VideoError("音频编辑和语音编辑需要恰好一段原始音频。")
        if count > 3:
            raise VideoError(f"此类生音频最多接收 3 段参考音频，当前 {count} 段。")
    effective = dict(config)
    if config.get("auto_route", True):
        rules = config.get("routes", [])
        matches = [
            r for r in rules if isinstance(r, dict) and r.get("__template_key") == route
        ]
        if len(matches) != 1:
            raise VideoError(f"请为“{AUDIO_ROUTES[route]}”配置恰好一条路线。")
        rule = matches[0]
        model = str(rule.get("model", "") or "").strip()
        if not model:
            raise VideoError(
                f"请先在“生音频设置 → 自动路由模型 → {AUDIO_ROUTES[route]}”配置模型和接口格式；未提交生成任务。"
            )
        effective["model"] = model
        for key in ("interface_mode", "base_url", "api_keys", "voice"):
            if rule.get(key) and rule[key] != "inherit":
                effective[key] = rule[key]
    effective["audio_route"] = route
    effective["audio_intent"] = options.mode
    return route, effective
