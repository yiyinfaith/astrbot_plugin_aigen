"""Shared parameter contracts for adapters, tools and collection menus."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace

from .video_router import VideoError


@dataclass(frozen=True)
class Parameter:
    """Describe one optional generation control without private configuration."""

    label: str
    kind: str = "string"
    choices: tuple = ()
    minimum: float | None = None
    maximum: float | None = None
    menu: tuple = ()


def enum(label, *choices):
    kind = (
        "integer"
        if choices
        and all(
            isinstance(value, int) and not isinstance(value, bool) for value in choices
        )
        else "string"
    )
    return Parameter(label, kind=kind, choices=choices, menu=choices)


def number(label, minimum, maximum, menu=(), *, integer=False):
    return Parameter(
        label, "integer" if integer else "number", (), minimum, maximum, menu
    )


BOOL = Parameter("开关", "boolean", menu=(False, True))
IMAGE = {
    "resolution": enum("分辨率", "1K", "2K", "4K"),
    "aspect_ratio": enum(
        "比例", "1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "5:4", "4:5", "21:9"
    ),
    "size": Parameter("精确尺寸"),
    "quality": enum(
        "画质", "auto", "low", "medium", "high", "xhigh", "max", "standard", "hd"
    ),
    "background": enum("背景", "auto", "transparent", "opaque"),
    "output_format": enum("图片格式", "png", "jpeg", "webp"),
    "output_compression": number("压缩率", 0, 100, integer=True),
    "moderation": enum("审核级别", "auto", "low"),
    "style": enum("风格", "vivid", "natural"),
    "n": number("生成数量", 1, 10, integer=True),
    "max_num_results": number("结果数量", 1, 50, integer=True),
    "input_fidelity": enum("参考保真度", "low", "high"),
    "partial_images": number("中间图片数量", 0, 3, integer=True),
    "action": enum("动作", "auto", "generate", "edit"),
    "temperature": number("随机性", 0, 2),
    "top_p": number("采样概率", 0, 1),
    "top_k": number("候选 token 数", 1, 100, integer=True),
    "seed": number("随机种子", 0, 2147483647, integer=True),
    "max_tokens": number("最大输出 token", 1, 65536, integer=True),
}
VIDEO = {
    "duration": number("时长（秒）", 1, 3600, (5, 10, 15), integer=True),
    "resolution": Parameter("分辨率", menu=("480p", "720p", "768p", "1080p")),
    "aspect_ratio": enum(
        "比例", "16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "adaptive"
    ),
    "size": Parameter("精确尺寸"),
    "seed": number("随机种子", -1, 999999999999999, (-1, 1, 42), integer=True),
    "generate_audio": replace(BOOL, label="同时生成声音"),
    "watermark": replace(BOOL, label="水印"),
    "negative_prompt": Parameter("负面提示词"),
    "prompt_extend": replace(BOOL, label="智能扩写"),
    "camera_fixed": replace(BOOL, label="固定镜头"),
    "return_last_frame": replace(BOOL, label="返回尾帧"),
    "service_tier": enum("服务档位", "default", "flex"),
    "draft": replace(BOOL, label="草稿模式"),
}
AUDIO = {
    "voice": Parameter(
        "音色",
        menu=(
            "alloy",
            "ash",
            "coral",
            "echo",
            "fable",
            "nova",
            "onyx",
            "sage",
            "shimmer",
        ),
    ),
    "speed": number("语速", 0.25, 4, (0.75, 1, 1.25, 1.5, 2)),
    "response_format": enum("音频格式", "wav", "mp3", "opus", "aac", "flac", "pcm"),
    "instructions": Parameter("朗读风格指令"),
    "language": Parameter(
        "语言", menu=("Auto", "Chinese", "English", "Japanese", "Korean")
    ),
    "volume": number("音量", 0, 10, (0.5, 1, 2)),
    "pitch": number("音调", -12, 12, (-4, 0, 4), integer=True),
    "sample_rate": enum("采样率", 8000, 16000, 22050, 24000, 32000, 44100),
    "bitrate": enum("比特率", 32000, 64000, 128000, 256000),
    "channels": enum("声道", 1, 2),
    "emotion": enum(
        "情绪",
        "happy",
        "sad",
        "angry",
        "fearful",
        "disgusted",
        "surprised",
        "calm",
        "fluent",
        "whisper",
    ),
    "temperature": number("随机性", 0, 2, (0, 0.5, 1)),
    "top_p": number("采样概率", 0, 1, (0.5, 0.9, 1)),
    "max_tokens": number("最大输出 token", 1, 32768, integer=True),
    "emo_control_method": enum(
        "情感来源", "与音色参考音频相同", "使用情感参考音频", "使用情感向量控制"
    ),
    "emo_random": replace(BOOL, label="随机情感"),
    "emo_surprised": enum("惊讶情感", "0"),
    **{
        f"emo_{key}": number(label, 0, 1.4, (0, 0.3, 0.7, 1, 1.4))
        for key, label in {
            "afraid": "恐惧",
            "angry": "愤怒",
            "calm": "平静",
            "disgusted": "厌恶",
            "happy": "开心",
            "melancholic": "忧郁",
            "sad": "悲伤",
        }.items()
    },
}


def supplied(values: dict) -> dict:
    """Keep explicit zero/false and drop only omitted or empty values."""
    return {
        key: value for key, value in values.items() if value is not None and value != ""
    }


def parameter_defaults(config: dict, *, include_adapter: bool = False) -> dict:
    """Read optional administrator defaults while retaining zero and false."""
    raw = config.get("parameter_defaults", "")
    if not raw:
        return {}
    try:
        values = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError) as exc:
        raise VideoError("生成参数默认值必须是合法 JSON 对象。") from exc
    if not isinstance(values, dict):
        raise VideoError("生成参数默认值必须是 JSON 对象。")
    values = dict(values)
    adapter = values.pop("adapter", {})
    from .config_manager import AUDIO_ADAPTER_DEFAULTS

    if not isinstance(adapter, dict) or any(
        "custom_" + key not in AUDIO_ADAPTER_DEFAULTS for key in adapter
    ):
        raise VideoError(
            "生成参数默认值的 adapter 必须是对象，且只能包含文档列出的接口字段。"
        )
    transport = {}
    for key, value in adapter.items():
        if key in {"body_template", "headers"}:
            try:
                value = json.loads(value) if isinstance(value, str) else value
            except ValueError as exc:
                raise VideoError(f"adapter.{key} 必须是合法 JSON 对象。") from exc
            if not isinstance(value, dict):
                raise VideoError(f"adapter.{key} 必须是 JSON 对象。")
            value = json.dumps(value, ensure_ascii=False)
        elif not isinstance(value, str):
            raise VideoError(f"adapter.{key} 必须是字符串。")
        transport["custom_" + key] = value
    allowed = IMAGE.keys() | VIDEO.keys() | AUDIO.keys()
    if any(key not in allowed for key in values):
        raise VideoError("生成参数默认值包含未知字段，请先查询能力工具支持的字段。")
    return {**supplied(values), **(transport if include_adapter else {})}


def prune_template_parameters(template, kind: str, values: dict):
    """Omit unprovided optional template fields instead of sending placeholders."""
    specs = VIDEO if kind == "video" else AUDIO
    aliases = {"aspect_ratio": "ratio", "response_format": "format"}
    optional = {"{" + key + "}" for key in specs} | {
        "{" + aliases.get(key, key) + "}" for key in specs
    }
    missing = optional - values.keys()
    omitted = object()

    def prune(value):
        if isinstance(value, str):
            if value in missing:
                return omitted
            if any(token in value for token in missing):
                raise VideoError(
                    "自定义模板的组合文字包含未填写的参数，请设置该参数或从模板移除。"
                )
            return value
        if isinstance(value, dict):
            result = {key: prune(child) for key, child in value.items()}
            return {key: child for key, child in result.items() if child is not omitted}
        if isinstance(value, list):
            return [child for child in map(prune, value) if child is not omitted]
        return value

    return prune(template)


def parameter_specs(
    kind: str, config: dict, *, has_reference=False
) -> dict[str, Parameter]:
    """Return actual adapter fields, narrowed by known model constraints."""
    mode = config.get(
        "interface_mode", "openai_image" if kind == "image" else "minimax"
    )
    model = str(config.get("model", ""))
    if kind == "image":
        if mode == "custom_endpoint":
            url = str(config.get("base_url", "")).lower()
            mode = (
                "gemini_official"
                if "generatecontent" in url
                else "openai_chat"
                if "chat/completions" in url
                else "openai_image"
            )
        fields = {"resolution", "aspect_ratio"}
        if mode in {"gemini_official", "openai_chat"}:
            fields |= {"temperature", "top_p", "seed", "max_tokens"}
            if mode == "gemini_official":
                fields.add("top_k")
            else:
                fields.discard("aspect_ratio")
        lower = model.lower()
        if mode in {"openai_image", "openai_response"}:
            fields |= {"size", "n"}
            if lower.startswith("gpt-image") or mode == "openai_response":
                fields |= {
                    "quality",
                    "background",
                    "output_format",
                    "output_compression",
                    "moderation",
                }
                if config.get("use_stream"):
                    fields.add("partial_images")
                if has_reference and lower.startswith("gpt-image-1"):
                    fields.add("input_fidelity")
            elif lower.startswith(("dall-e", "dalle")):
                fields |= {"quality", "style"}
            if mode == "openai_response":
                fields |= {"max_num_results", "action"}
        specs = {key: IMAGE[key] for key in IMAGE if key in fields}
        if "quality" in specs:
            choices = (
                ("standard", "hd")
                if lower.startswith(("dall-e", "dalle"))
                else ("auto", "low", "medium", "high", "xhigh", "max")
            )
            specs["quality"] = replace(specs["quality"], choices=choices, menu=choices)
        if "gpt-image-1-mini" in lower and "input_fidelity" in specs:
            specs["input_fidelity"] = enum("参考保真度", "low")
        return specs
    if kind == "video":
        fields = {"duration", "resolution", "aspect_ratio", "seed"}
        if mode == "openai_video":
            fields.add("size")
        if mode == "seedance":
            fields |= {
                "generate_audio",
                "watermark",
                "camera_fixed",
                "return_last_frame",
                "service_tier",
                "draft",
            }
        if mode == "dashscope":
            fields |= {"watermark", "negative_prompt", "prompt_extend"}
            if model == "wan2.2-animate-move":
                fields = {"watermark"}
        specs = {key: VIDEO[key] for key in VIDEO if key in fields}
        from .video_api_manager import NO_SEED_MODELS, RESOLUTIONS, WORKFLOWS

        caps = WORKFLOWS.get(
            "wan2.2animate-v4-motion_retargeting"
            if model == "wan2.2-animate-move"
            else model
        )
        if caps and mode in {"openai_video", "minimax", "autodl_native"}:
            group = caps[6]
            resolutions = RESOLUTIONS[group]
            specs["resolution"] = enum("分辨率", *resolutions)
            limit = caps[5] or 3600
            if group == "E" and mode == "autodl_native":
                specs.pop("duration", None)
            else:
                specs["duration"] = number(
                    "时长（秒）",
                    1,
                    limit,
                    tuple(n for n in (1, 5, 10, 12, 15) if n <= limit),
                    integer=group != "E",
                )
            ratios = (
                ("16:9", "9:16", "adaptive")
                if group not in {"C", "D", "G", "I"}
                else ("16:9", "9:16", "1:1", "adaptive")
            )
            if model in {"minimax_h3_lightx2v", "minimax_h3_b99_002"}:
                specs.pop("aspect_ratio", None)
            else:
                specs["aspect_ratio"] = enum("比例", *ratios)
            if group in {"C", "D", "E"}:
                specs.pop("size", None)
            if model in NO_SEED_MODELS:
                specs.pop("seed", None)
            elif "seed" in specs:
                minimum = 0 if group == "C" else 1
                # -1 explicitly requests provider defaults, not seed zero.
                specs["seed"] = replace(specs["seed"], menu=(-1, minimum, 42))
        if mode == "seedance":
            specs["seed"] = replace(specs["seed"], maximum=2147483647)
        if not has_reference and "aspect_ratio" in specs:
            spec = specs["aspect_ratio"]
            specs["aspect_ratio"] = replace(
                spec,
                choices=tuple(x for x in spec.choices if x != "adaptive"),
                menu=tuple(x for x in spec.menu if x != "adaptive"),
            )
        all_specs = VIDEO
    elif kind == "audio":
        fields = {
            "openai_speech": {"voice", "speed", "response_format", "instructions"},
            "minimax_speech": {
                "voice",
                "speed",
                "response_format",
                "volume",
                "pitch",
                "sample_rate",
                "bitrate",
                "channels",
                "emotion",
                "language",
            },
            "openai_chat": {
                "voice",
                "response_format",
                "temperature",
                "top_p",
                "max_tokens",
            },
            "gemini_official": {"voice", "temperature", "top_p", "max_tokens"},
            "dashscope": {"voice", "language"},
            "minimax": set(),
            "autodl_native": set(),
        }.get(mode, set())
        if mode in {"minimax", "autodl_native"} and model in {
            "IndexTTS-2",
            "indextts2-v1",
        }:
            fields |= {key for key in AUDIO if key.startswith("emo_")}
        specs = {key: AUDIO[key] for key in AUDIO if key in fields}
        if mode == "minimax_speech":
            specs["speed"] = replace(specs["speed"], minimum=0.5, maximum=2)
            specs["response_format"] = enum("音频格式", "wav", "mp3", "pcm", "flac")
            specs["voice"] = Parameter(
                "音色",
                menu=(
                    config.get("minimax_voice", "male-qn-qingse"),
                    "female-shaonv",
                    "male-qn-jingying",
                ),
            )
        elif mode == "openai_chat":
            specs["response_format"] = enum(
                "音频格式", "wav", "mp3", "opus", "flac", "pcm"
            )
        elif mode == "gemini_official":
            specs["voice"] = Parameter(
                "音色", menu=("Kore", "Puck", "Charon", "Fenrir", "Aoede")
            )
        elif mode == "dashscope":
            specs["voice"] = Parameter(
                "音色", menu=("Cherry", "Serena", "Ethan", "Chelsie")
            )
        if mode == "openai_speech" and model.startswith(("tts-1", "tts-1-hd")):
            specs.pop("instructions", None)
        all_specs = AUDIO
    else:
        raise VideoError("媒体类型必须为 image、video 或 audio。")
    if mode == "custom_endpoint":
        template = str(config.get("custom_body_template", ""))
        aliases = {"aspect_ratio": "ratio", "response_format": "format"}
        specs = {
            key: spec
            for key, spec in all_specs.items()
            if "{" + aliases.get(key, key) + "}" in template
            or "{" + key + "}" in template
        }
    return specs


def validate_options(
    kind: str, config: dict, values: dict, *, has_reference=False
) -> dict:
    """Reject explicit unsupported fields and invalid values before network I/O."""
    specs = parameter_specs(kind, config, has_reference=has_reference)
    result = supplied(values)
    for key, value in result.items():
        if key not in specs:
            raise VideoError(
                f"当前模型 {config.get('model', '')} / {config.get('interface_mode', '')} 不支持参数 {key}；请清除该设置或换用支持的路线。"
            )
        spec = specs[key]
        if spec.kind == "boolean":
            if not isinstance(value, bool):
                raise VideoError(f"{spec.label}必须是 true 或 false。")
        elif spec.kind in {"integer", "number"}:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise VideoError(f"{spec.label}必须是有效数字。")
            if spec.kind == "integer" and int(value) != value:
                raise VideoError(f"{spec.label}必须是整数。")
            if spec.kind == "integer":
                result[key] = int(value)
            if (spec.minimum is not None and value < spec.minimum) or (
                spec.maximum is not None and value > spec.maximum
            ):
                raise VideoError(f"{spec.label}范围为 {spec.minimum}–{spec.maximum}。")
        elif not spec.choices and not isinstance(value, str):
            raise VideoError(f"{spec.label}必须是文字。")
        if spec.choices and value not in spec.choices:
            raise VideoError(f"{spec.label}可选：{'、'.join(map(str, spec.choices))}。")
    if kind == "image" and "size" in result:
        from .generation_params import normalize_openai_image_size

        if not normalize_openai_image_size(result["size"]):
            raise VideoError("图片 size 不符合支持的精确尺寸规则。")
    if (
        kind == "image"
        and config.get("model", "").lower().startswith(("dall-e-3", "dalle-3"))
        and result.get("n", 1) != 1
    ):
        raise VideoError("DALL-E 3 只支持一次生成一张。")
    return result


def effective_defaults(kind: str, config: dict) -> dict:
    """Return allowlisted parameter defaults, never connection information."""
    defaults = {}
    for key in parameter_specs(kind, config, has_reference=True):
        source = "image_resolution" if kind == "image" and key == "resolution" else key
        if kind == "audio" and key == "voice":
            source = {
                "minimax_speech": "minimax_voice",
                "gemini_official": "gemini_voice",
                "dashscope": "dashscope_voice",
            }.get(config.get("interface_mode"), key)
        defaults[key] = (
            config.get(key, config.get(source, "上游默认"))
            if kind == "image"
            else config.get(source, "上游默认")
        )
    return defaults


def describe_capabilities(kind: str, config: dict) -> dict:
    """Build a credential-free capability document for the language model."""
    specs = parameter_specs(kind, config, has_reference=True)
    known = str(config.get("model", "")).startswith(
        ("gpt-image", "dall-e", "tts-1")
    ) or config.get("model") in {"IndexTTS-2", "indextts2-v1"}
    info = {
        "media_type": kind,
        "model": config.get("model", ""),
        "interface_mode": config.get(
            "interface_mode", "openai_image" if kind == "image" else "minimax"
        ),
        "model_constraints_known": known,
        "parameters": {
            key: {
                "label": spec.label,
                "type": spec.kind,
                "choices": spec.choices,
                "minimum": spec.minimum,
                "maximum": spec.maximum,
            }
            for key, spec in specs.items()
        },
        "effective_defaults": effective_defaults(kind, config),
        "precedence": "显式工具参数/会话设置 > 路线配置 > 板块默认 > 上游默认；未知模型只声明适配器可传字段，实际模型能力以服务商为准。",
    }
    if kind == "video":
        from .video_api_manager import WORKFLOWS

        caps = WORKFLOWS.get(
            "wan2.2animate-v4-motion_retargeting"
            if config.get("model") == "wan2.2-animate-move"
            else config.get("model")
        )
        if caps:
            info["model_constraints_known"] = True
            info["materials"] = {
                "images": caps[:2],
                "audios": caps[2:4],
                "videos": caps[4],
                "max_seconds": caps[5] or None,
            }
    elif kind == "audio":
        mode = info["interface_mode"]
        info["protocol_operations"] = (
            ["text_to_speech"]
            if mode in {"openai_speech", "minimax_speech", "dashscope"}
            else ["reference_to_speech"]
            if mode == "autodl_native"
            else [
                "text_to_audio",
                "text_to_speech",
                "reference_to_audio",
                "reference_to_speech",
            ]
            if mode == "minimax"
            else [
                "text_to_audio",
                "text_to_speech",
                "reference_to_audio",
                "reference_to_speech",
                "audio_editing",
                "speech_editing",
                "voice_conversion",
            ]
        )
        if config.get("model") in {"IndexTTS-2", "indextts2-v1"}:
            info["materials"] = {"audios": [1, 2], "text_characters": [1, 2048]}
            info["protocol_operations"] = ["reference_to_speech"]
        if mode in {"openai_chat", "gemini_official"}:
            info["reference_note"] = (
                "协议可以编码参考音频；未知模型的音色克隆或编辑能力尚未确认，固定音色模型不能保证还原参考人声。"
            )
    return info


def build_menu(
    kind: str, config: dict, current: dict, *, has_reference=False
) -> tuple[list[str], dict]:
    """Assign unique IDs to a snapshot of the current route's finite choices."""
    sections = [
        "详细参数设置\n请查看本条合并转发里的全部选项，再在聊天中一次性发送要修改的选项编号，不加 /，多个编号之间用空格。\n例如：{example}。编号是各个选项前的数字，不是参数的顺序号；只发送需要修改的选项，未选参数保持原值。每个参数只能选一项，请勿选择同一参数的多个不同值。\n保存后可继续发送材料，最后发送 /生成；只需修改一项时，直接发送该选项编号即可。\n发送 0 退出设置并保留已保存的值；/设置重置 清除本次自定义参数；/生成 直接使用已保存设置开始生成。\n计时从启动起共用，不延长。"
    ]
    mapping = {}
    defaults = effective_defaults(kind, config)
    specs = parameter_specs(kind, config, has_reference=has_reference)
    if kind == "video" and config.get("_long_model") and "duration" in specs:
        long_config = {**config, "model": config["_long_model"]}
        if config.get("_long_interface_mode") not in {None, "", "inherit"}:
            long_config["interface_mode"] = config["_long_interface_mode"]
        long_spec = parameter_specs(kind, long_config, has_reference=has_reference).get(
            "duration"
        )
        if long_spec:
            specs["duration"] = replace(
                specs["duration"],
                menu=tuple(sorted(set(specs["duration"].menu + long_spec.menu))),
            )
    for key, spec in specs.items():
        if not spec.menu:
            continue
        lines = [
            f"{spec.label}（当前：{current.get(key, defaults.get(key, '上游默认'))}）"
        ]
        for value in dict.fromkeys(spec.menu):
            index = str(len(mapping) + 1)
            mapping[index] = (key, value)
            label = "开" if value is True else "关" if value is False else str(value)
            lines.append(f"{index}：{label}")
        sections.append("\n".join(lines))
    example, parameters = [], set()
    for index, (key, _value) in mapping.items():
        if key not in parameters and len(example) < 3:
            example.append(index)
            parameters.add(key)
    sections[0] = sections[0].format(example=" ".join(example) or "0")
    return sections, mapping


def apply_menu_selection(text: str, mapping: dict, current: dict) -> dict | None:
    """Apply a sparse selection atomically; None exits without changing values."""
    if text.strip() == "0":
        return None
    tokens = text.split()
    if not tokens or any(token not in mapping for token in tokens):
        raise VideoError("编号无效，请只发送菜单中的数字，用空格分隔；0 退出设置。")
    changes = {}
    for token in tokens:
        key, value = mapping[token]
        if key in changes and changes[key] != value:
            raise VideoError("同一个参数选择了多个不同值，本次设置未保存，请重新选择。")
        changes[key] = value
    return {**current, **changes}


def configured_capabilities(kind: str, config: dict) -> str:
    """Describe configured short/long routes without copying private fields."""
    config = {**config, **parameter_defaults(config, include_adapter=kind == "audio")}
    voice_field = {
        "minimax_speech": "minimax_voice",
        "gemini_official": "gemini_voice",
        "dashscope": "dashscope_voice",
    }
    if kind == "audio" and "voice" in parameter_defaults(config):
        config[voice_field.get(config.get("interface_mode"), "voice")] = config["voice"]
    result = [describe_capabilities(kind, config)]
    if config.get("auto_route", True):
        rules = config.get("routes", [])
        if isinstance(rules, dict):
            rules = [{"__template_key": key, **rule} for key, rule in rules.items()]
        for rule in rules:
            effective = dict(config)
            for key, value in rule.items():
                if value is not None and value != "" and value != "inherit":
                    effective[key] = value
            effective.update(parameter_defaults(rule, include_adapter=kind == "audio"))
            if kind == "audio" and (
                rule.get("voice") or "voice" in parameter_defaults(rule)
            ):
                effective[voice_field.get(effective.get("interface_mode"), "voice")] = (
                    effective["voice"]
                )
            info = describe_capabilities(kind, effective)
            info["route"] = rule.get("__template_key")
            if kind == "audio":
                info["configured_route_supported_by_protocol"] = (
                    info["route"] in info["protocol_operations"]
                )
            result.append(info)
            if kind == "video" and rule.get("long_model"):
                effective["model"] = rule["long_model"]
                if rule.get("long_interface_mode") not in {None, "", "inherit"}:
                    effective["interface_mode"] = rule["long_interface_mode"]
                info = describe_capabilities(kind, effective)
                info.update(route=rule.get("__template_key"), duration_branch=">10s")
                result.append(info)
    return json.dumps(result, ensure_ascii=False)
