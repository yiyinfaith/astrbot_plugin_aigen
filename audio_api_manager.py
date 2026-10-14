"""Audio adapters with one submission, bounded polling and artifact delivery."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import math
import re
import time
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp

from .audio_router import AUDIO_MODES, AudioOptions, select_audio_route
from .billing import track
from .generation_capabilities import AUDIO, prune_template_parameters, validate_options
from .video_api_manager import (
    VideoApiManager,
    VideoDownloadError,
    VideoHttpError,
    json_path,
    media_sources,
    render_template,
)
from .video_router import InvalidMediaError, VideoError


@dataclass
class AudioResult:
    """Keep a completed audio artifact and its recoverable task identity."""

    path: Path
    task_id: str
    model: str
    url: str = ""


class AudioDownloadError(VideoDownloadError):
    """A task completed but its audio could not be downloaded."""


class AudioApiManager(VideoApiManager):
    """Share HTTP/key handling while keeping audio payloads and artifacts separate."""

    def __init__(self, config: dict, data_dir: Path):
        super().__init__(config, data_dir)
        self.data_dir = Path(data_dir) / "audio"
        self._route_managers = {}

    def prepare(self, options: AudioOptions, audios: list[str]):
        """Validate the route and payload before submitting a chargeable request.

        Args:
            options: Literal reading text or descriptive audio prompt.
            audios: Ordered audio URLs or standard audio Data URLs.

        Returns:
            The selected route and a per-request adapter.

        Raises:
            VideoError: If the model, protocol or materials are unsupported.
        """
        for source in audios:
            media_sources(source, "audio")
        route, effective = select_audio_route(self.config, options, audios)
        cache_key = (
            route,
            effective.get("model"),
            effective.get("interface_mode"),
            effective.get("base_url"),
            tuple(self._normalize_keys(effective.get("api_keys"))),
            effective.get("audio_intent"),
        )
        manager = self._route_managers.get(cache_key)
        if manager is None:
            manager = AudioApiManager(effective, self.data_dir.parent)
            manager._get_session = self._get_session
            if effective.get("api_keys") == self.config.get("api_keys"):
                manager.get_key = self.get_key
            self._route_managers[cache_key] = manager
        request_manager = AudioApiManager(effective, self.data_dir.parent)
        request_manager._get_session = self._get_session
        request_manager.get_key = manager.get_key
        request_manager.build_request(options.prompt, audios)
        return route, request_manager

    def build_request(self, text: str, audios: list[str]):
        """Encode the configured audio protocol without discarding any reference.

        Args:
            text: Exact merged reading text or the merged prompt.
            audios: Ordered references.

        Returns:
            Create path, optional query path, JSON body and headers.

        Raises:
            VideoError: If the selected protocol cannot represent the request.
        """
        c = self.config
        mode = c.get("interface_mode", "minimax")
        model = str(c.get("model", "") or "").strip()
        route = c.get("audio_route", "text_to_speech")
        if mode not in AUDIO_MODES or not model:
            raise VideoError("请配置生音频模型及支持的接口格式。")
        fmt = str(c.get("response_format", "wav"))
        if fmt not in {"wav", "mp3", "opus", "aac", "flac", "pcm"}:
            raise VideoError("音频格式必须为 wav、mp3、opus、aac、flac 或 pcm。")
        try:
            speed = float(c.get("speed", 1))
        except (ValueError, TypeError) as exc:
            raise VideoError("音频语速必须是有效数字。") from exc
        if not math.isfinite(speed) or not 0.25 <= speed <= 4:
            raise VideoError("音频语速必须在 0.25–4 之间。")
        headers = {"Content-Type": "application/json"}
        query = ""
        if mode in {"openai_speech", "minimax_speech", "dashscope"} and (
            route != "text_to_speech" or audios
        ):
            raise VideoError(
                f"{mode} 仅支持无参考音频的文字朗读；此方向请使用工作流 V2、Chat 音频或自定义接口。"
            )
        if mode == "openai_speech":
            body = {
                "model": model,
                "input": text,
                "voice": str(c.get("voice", "alloy")),
                "response_format": fmt,
                "speed": speed,
            }
            if c.get("instructions"):
                body["instructions"] = c["instructions"]
            create = "/v1/audio/speech"
        elif mode == "minimax_speech":
            if not 0.5 <= speed <= 2 or fmt not in {"wav", "mp3", "pcm", "flac"}:
                raise VideoError(
                    "MiniMax 官方语音接口要求语速 0.5–2，格式 wav/mp3/pcm/flac。"
                )
            body = {
                "model": model,
                "text": text,
                "stream": False,
                "voice_setting": {
                    "voice_id": str(c.get("minimax_voice", "male-qn-qingse")),
                    "speed": speed,
                },
                "audio_setting": {"sample_rate": 32000, "format": fmt, "channel": 1},
            }
            for source, target in (
                ("volume", "vol"),
                ("pitch", "pitch"),
                ("emotion", "emotion"),
            ):
                if source in c:
                    body["voice_setting"][target] = c[source]
            for source, target in (
                ("sample_rate", "sample_rate"),
                ("bitrate", "bitrate"),
                ("channels", "channel"),
            ):
                if source in c:
                    body["audio_setting"][target] = c[source]
            if c.get("language"):
                body["language_boost"] = c["language"]
            create = "/v1/t2a_v2"
        elif mode in {"minimax", "autodl_native"}:
            if model in {"IndexTTS-2", "indextts2-v1"}:
                if route != "reference_to_speech" or not 1 <= len(audios) <= 2:
                    raise VideoError(
                        "IndexTTS-2 用于文字＋参考音频朗读，需要 1–2 段音频（第 1 段音色，第 2 段可选情感）。"
                    )
                if not 1 <= len(text) <= 2048:
                    raise VideoError("IndexTTS-2 朗读文字长度须为 1–2048 字符。")
            if mode == "minimax":
                content = ([{"type": "text", "text": text}] if text else []) + [
                    {
                        "type": "audio_url",
                        "audio_url": {"url": source},
                        "role": "reference_audio",
                    }
                    for source in audios
                ]
                body = {"model": model, "content": content}
                if route in {"audio_editing", "speech_editing", "voice_conversion"}:
                    raise VideoError(
                        "MiniMax 工作流 V2 没有标准编辑/音色转换角色；请为此路线配置自定义接口，避免误用参考音频。"
                    )
                create, query = (
                    "/v2/video_generation",
                    "/v2/query/video_generation/{task_id}",
                )
            else:
                if model not in {"IndexTTS-2", "indextts2-v1"}:
                    raise VideoError(
                        "AutoDL 原生音频格式仅接入 IndexTTS-2；其他工作流请配置自定义接口。"
                    )
                body = {
                    "prompt_text": text,
                    "prompt_simple": audios[0],
                    "emo_control_method": "与音色参考音频相同",
                }
                if len(audios) == 2:
                    body.update(
                        emo_ref_audio=audios[1], emo_control_method="使用情感参考音频"
                    )
                create, query = (
                    "/api/v1/comfyui/comfyui_workflow/indextts2-v1",
                    "/api/v1/comfyui/comfyui_workflow/result/{task_id}",
                )
            if model in {"IndexTTS-2", "indextts2-v1"}:
                controls = {
                    key: c[key] for key in AUDIO if key.startswith("emo_") and key in c
                }
                validate_options("audio", c, controls, has_reference=bool(audios))
                method = controls.get(
                    "emo_control_method",
                    "使用情感参考音频" if len(audios) == 2 else "与音色参考音频相同",
                )
                if method == "使用情感参考音频" and len(audios) != 2:
                    raise VideoError(
                        "情感参考模式需要两段音频：音色参考在前，情感参考在后。"
                    )
                if len(audios) == 2 and method != "使用情感参考音频":
                    raise VideoError(
                        "两段参考音频必须使用情感参考模式，避免忽略第二段音频。"
                    )
                if (
                    any(
                        key.startswith("emo_")
                        and key not in {"emo_control_method", "emo_random"}
                        for key in controls
                    )
                    and method != "使用情感向量控制"
                ):
                    raise VideoError("设置情感向量时请选择“使用情感向量控制”。")
                body.update(controls)
                if controls or len(audios) == 2 or mode == "autodl_native":
                    body["emo_control_method"] = method
        elif mode == "openai_chat":
            if fmt not in {"wav", "mp3", "opus", "flac", "pcm"}:
                raise VideoError("OpenAI Chat 音频输出不支持此格式。")
            content = ([{"type": "text", "text": text}] if text else []) + [
                {
                    "type": "input_audio",
                    "input_audio": {"data": source, "format": "wav"},
                }
                for source in audios
            ]
            body = {
                "model": model,
                "modalities": ["text", "audio"],
                "audio": {
                    "voice": str(c.get("voice", "alloy")),
                    "format": "pcm16" if fmt == "pcm" else fmt,
                },
                "messages": [{"role": "user", "content": content}],
                "stream": False,
            }
            for name in ("temperature", "top_p", "max_tokens"):
                if name in c:
                    body[name] = c[name]
            if route == "voice_conversion":
                body["messages"].insert(
                    0,
                    {
                        "role": "system",
                        "content": "Convert the speech in the first audio to the target speaker timbre demonstrated by the second audio. Preserve the exact words in the first audio. Do not read the second audio's transcript.",
                    },
                )
            elif c.get("audio_intent") == "朗读":
                body["messages"].insert(
                    0,
                    {
                        "role": "system",
                        "content": "Read the provided text verbatim. Do not paraphrase, answer it, add words or omit words. Use supplied audio only as reference for the requested operation.",
                    },
                )
            create = "/v1/chat/completions"
        elif mode == "gemini_official":
            parts = ([{"text": text}] if text else []) + [
                {"inlineData": {"mimeType": "audio/wav", "data": source}}
                for source in audios
            ]
            body = {
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {
                        "voiceConfig": {
                            "prebuiltVoiceConfig": {
                                "voiceName": str(c.get("gemini_voice", "Kore"))
                            }
                        }
                    },
                },
            }
            for source, target in (
                ("temperature", "temperature"),
                ("top_p", "topP"),
                ("max_tokens", "maxOutputTokens"),
            ):
                if source in c:
                    body["generationConfig"][target] = c[source]
            if route == "voice_conversion":
                body["systemInstruction"] = {
                    "parts": [
                        {
                            "text": "Convert the speech in the first audio to the target speaker timbre in the second audio. Preserve the exact words from the first audio; the second audio is only a speaker reference."
                        }
                    ]
                }
            elif c.get("audio_intent") == "朗读":
                body["systemInstruction"] = {
                    "parts": [
                        {
                            "text": "Read the input text verbatim, without additions, omissions or paraphrasing."
                        }
                    ]
                }
            create = f"/v1beta/models/{quote(model, safe='')}:generateContent"
        elif mode == "dashscope":
            body = {
                "model": model,
                "input": {
                    "text": text,
                    "voice": str(c.get("dashscope_voice", "Cherry")),
                    "language_type": c.get("language", "Auto"),
                },
            }
            create = "/api/v1/services/aigc/multimodal-generation/generation"
        else:
            try:
                template = json.loads(c.get("custom_body_template", "{}"))
                extra = json.loads(c.get("custom_headers", "{}"))
            except (ValueError, TypeError) as exc:
                raise VideoError(
                    "音频自定义请求体和请求头必须为合法 JSON 对象。"
                ) from exc
            if (
                not isinstance(template, dict)
                or not template
                or not isinstance(extra, dict)
            ):
                raise VideoError(
                    "音频自定义请求体必须是非空 JSON 对象，请求头必须是 JSON 对象。"
                )
            serialized = json.dumps(template)
            if text and not any(
                token in serialized for token in ("{text}", "{prompt}")
            ):
                raise VideoError(
                    "音频自定义模板必须包含 {text} 或 {prompt}，不能丢弃输入文字。"
                )
            keeps_all_audio = (
                "{audios}" in serialized
                or (
                    len(audios) == 1
                    and any(
                        token in serialized for token in ("{audio}", "{source_audio}")
                    )
                )
                or (
                    route == "voice_conversion"
                    and all(
                        token in serialized
                        for token in ("{source_audio}", "{target_audio}")
                    )
                )
            )
            if audios and not keeps_all_audio:
                raise VideoError(
                    "音频自定义模板必须传递全部音频；多音频请用 {audios}，音色转换可用 {source_audio} 和 {target_audio}。"
                )
            template_values = {
                "{model}": model,
                "{text}": text,
                "{prompt}": text,
                "{audios}": audios,
                "{audio}": audios[0] if audios else "",
                "{source_audio}": audios[0] if audios else "",
                "{target_audio}": audios[1] if len(audios) > 1 else "",
                "{route}": route,
                "{voice}": c.get("voice", "alloy"),
                "{format}": fmt,
                "{speed}": speed,
                "{stream}": False,
                **{"{" + key + "}": c[key] for key in AUDIO if key in c},
            }
            body = render_template(
                prune_template_parameters(template, "audio", template_values),
                template_values,
            )
            headers.update({str(k): str(v) for k, v in extra.items()})
            create, query = (
                str(c.get("custom_create_path", "")),
                str(c.get("custom_query_path", "")),
            )
            if not create:
                raise VideoError("请配置音频自定义提交路径。")
        self.audio_endpoint(create)
        if query:
            if "{task_id}" not in query:
                raise VideoError("音频查询路径必须包含 {task_id}。")
            self.audio_endpoint(query.replace("{task_id}", "check"))
        return create, query, body, headers

    def audio_endpoint(self, path: str) -> str:
        """Build a provider URL while retaining reverse-proxy prefixes.

        Args:
            path: Relative endpoint path.

        Returns:
            The complete HTTP URL.

        Raises:
            VideoError: If either URL is invalid.
        """
        base = str(self.config.get("base_url", "") or "").strip()
        if not base and self.config.get("interface_mode") == "gemini_official":
            base = "https://generativelanguage.googleapis.com"
        parsed = urlsplit(base)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
        ):
            raise VideoError("请配置完整的生音频 HTTP(S) 接口地址。")
        if urlsplit(path).scheme or urlsplit(path).netloc:
            raise VideoError("音频接口路径请填写相对路径，域名在接口地址中配置。")
        root = parsed.path.rstrip("/")
        root = re.sub(
            r"/(?:v1/audio/speech|v1/chat/completions|v1/t2a_v2|v2/video_generation|v1beta/models/[^/]+:generateContent|api/v1/services/aigc/multimodal-generation/generation|api/v1/comfyui/comfyui_workflow/[^/]+)$",
            "",
            root,
        )
        root = re.sub(r"/(?:api/)?v[12](?:beta)?$", "", root)
        return urlunsplit(
            (parsed.scheme, parsed.netloc, root + "/" + path.lstrip("/"), "", "")
        )

    def parse_task(self, data: dict):
        """Read task status and audio URLs for asynchronous protocols.

        Args:
            data: Decoded provider response.

        Returns:
            Task ID, status, audio URL and failure reason.

        Raises:
            VideoError: If a video artifact is incorrectly configured as audio.
        """
        if self.config.get("interface_mode") == "custom_endpoint":
            return tuple(
                str(
                    json_path(
                        data, self.config.get("custom_" + name + "_path", default)
                    )
                    or ""
                )
                for name, default in [
                    ("id", "id"),
                    ("status", "status"),
                    ("url", "url"),
                    ("error", "error.message"),
                ]
            )
        node = data.get("task", data.get("output", data.get("data", data)))
        if not isinstance(node, dict):
            node = data
        if node.get("modality") == "video":
            raise VideoError("生音频接口返回了视频产物，请检查路线模型。")
        task_id = node.get("id") or node.get("task_id") or data.get("task_id") or ""
        status = node.get("status") or node.get("task_status") or ""
        content = node.get("content") or node.get("audio") or {}
        url = node.get("url") or node.get("audio_url") or ""
        if isinstance(content, dict):
            url = url or content.get("url") or content.get("audio_url") or ""
        for result in (
            node.get("results", []) if isinstance(node.get("results"), list) else []
        ):
            if isinstance(result, dict) and result.get("type") == "audio":
                url = url or result.get("url", "")
        error = (
            node.get("error") or node.get("fail_reason") or node.get("message") or ""
        )
        if isinstance(error, dict):
            error = error.get("message") or error.get("code") or "音频生成失败"
        return str(task_id), str(status), str(url), str(error)

    async def generate(self, options: AudioOptions, audios: list[str]) -> AudioResult:
        """Generate audio using one paid POST and read-only task polling.

        Args:
            options: Parsed audio intent.
            audios: Ordered reference sources.

        Returns:
            A downloaded or decoded audio artifact.

        Raises:
            VideoError: If submission, polling or response decoding fails.
            AudioDownloadError: If a completed artifact cannot be downloaded.
        """
        _, manager = self.prepare(options, audios)
        return await manager._generate_audio(options.prompt, audios)

    async def _generate_audio(self, text: str, audios: list[str]) -> AudioResult:
        """Submit, poll and materialize one audio result.

        Args:
            text: Merged user text.
            audios: References already validated for this route.

        Returns:
            The completed local audio artifact.

        Raises:
            VideoError: If generation cannot complete safely.
        """
        create, query, body, headers = self.build_request(text, audios)
        key = await self.get_key()
        if not key:
            raise VideoError("请配置生音频 API Key。")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        mode = self.config.get("interface_mode")
        # Chat/Gemini require inline bytes, not URL strings pretending to be Base64.
        if mode in {"openai_chat", "gemini_official"}:
            for index, source in enumerate(audios):
                if source.startswith("data:"):
                    mime = source[5:].split(";", 1)[0]
                    raw = base64.b64decode(source.split(",", 1)[1], validate=True)
                else:
                    path = await self.download_audio(source)
                    try:
                        raw = await asyncio.to_thread(path.read_bytes)
                    finally:
                        path.unlink(missing_ok=True)
                    mime = {
                        ".wav": "audio/wav",
                        ".mp3": "audio/mpeg",
                        ".flac": "audio/flac",
                        ".ogg": "audio/ogg",
                        ".m4a": "audio/mp4",
                        ".aac": "audio/aac",
                        ".amr": "audio/amr",
                        ".silk": "audio/silk",
                    }.get(path.suffix.lower(), "application/octet-stream")
                if mode == "openai_chat":
                    if not (
                        (raw[:4] == b"RIFF" and raw[8:12] == b"WAVE")
                        or raw[:3] == b"ID3"
                        or (
                            len(raw) > 1
                            and raw[0] == 255
                            and raw[1] & 224 == 224
                            and raw[1] & 0xF6 != 0xF0
                        )
                    ):
                        raise VideoError(
                            "OpenAI Chat 参考音频需 WAV 或 MP3，请使用对应音频文件。"
                        )
                    item = body["messages"][-1]["content"][-len(audios) + index][
                        "input_audio"
                    ]
                    item.update(
                        data=base64.b64encode(raw).decode(),
                        format="wav" if raw[:4] == b"RIFF" else "mp3",
                    )
                else:
                    body["contents"][0]["parts"][-len(audios) + index]["inlineData"] = {
                        "mimeType": mime,
                        "data": base64.b64encode(raw).decode(),
                    }
        url = self.audio_endpoint(create)
        try:
            if mode == "openai_speech":
                session = await self._get_session()
                track(state="submitted")
                async with session.post(
                    url,
                    json=body,
                    headers={**headers, "Authorization": "Bearer " + key},
                    timeout=aiohttp.ClientTimeout(
                        total=max(1, int(self.config.get("timeout", 120)))
                    ),
                    proxy=self._get_request_proxy(
                        url,
                        self.config.get("proxy_url")
                        if self.config.get("use_proxy")
                        else None,
                    ),
                ) as response:
                    track(status=response.status)
                    if response.status >= 400:
                        detail = (await response.text()).replace(key, "[redacted]")[
                            :400
                        ]
                        raise VideoHttpError(
                            f"音频接口错误（HTTP {response.status}）：{detail}",
                            response.status,
                        )
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        raw.extend(chunk)
                        if (
                            len(raw)
                            > int(self.config.get("max_audio_size_mb", 50))
                            * 1024
                            * 1024
                        ):
                            raise VideoError("音频输出超过下载大小上限。")
                    data = {"_raw": bytes(raw)}
            else:
                data = await self._json_request("POST", url, key, headers, body)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise VideoError(
                "音频提交连接中断，任务可能已创建；请核对上游记录，勿立即重复提交。"
            ) from exc
        except VideoError as exc:
            raise VideoError(str(exc).replace("视频", "音频")) from exc
        raw = data.get("_raw")
        mime = ""
        if mode == "minimax_speech":
            meta = data.get("base_resp") or {}
            if meta.get("status_code", 0) != 0:
                track(state="failed")
                raise VideoError(
                    "MiniMax 语音生成失败："
                    + str(meta.get("status_msg", "未知原因"))[:400].replace(
                        key, "[redacted]"
                    )
                )
            encoded = json_path(data, "data.audio")
            if encoded:
                try:
                    raw = bytes.fromhex(encoded)
                except (ValueError, TypeError) as exc:
                    raise VideoError("MiniMax 返回了无效的十六进制音频。") from exc
        elif mode == "openai_chat":
            encoded = json_path(data, "choices.0.message.audio.data")
            if encoded:
                try:
                    raw = base64.b64decode(encoded, validate=True)
                except (ValueError, TypeError) as exc:
                    raise VideoError("OpenAI Chat 返回了无效的 Base64 音频。") from exc
        elif mode == "gemini_official":
            parts = json_path(data, "candidates.0.content.parts") or []
            for part in parts:
                inline = part.get("inlineData", {}) if isinstance(part, dict) else {}
                if inline.get("mimeType", "").startswith("audio/") and inline.get(
                    "data"
                ):
                    try:
                        raw = base64.b64decode(inline["data"], validate=True)
                    except (ValueError, TypeError) as exc:
                        raise VideoError("Gemini 返回了无效的 Base64 音频。") from exc
                    mime = inline["mimeType"]
                    break
        elif mode == "custom_endpoint":
            encoded = json_path(
                data, self.config.get("custom_base64_path", "audio_base64")
            )
            if encoded:
                try:
                    raw = base64.b64decode(encoded, validate=True)
                except (ValueError, TypeError) as exc:
                    raise VideoError("自定义接口返回了无效的 Base64 音频。") from exc
        task_id, status, artifact_url, error = self.parse_task(data)
        error = error.replace(key, "[redacted]")
        failure = {
            "failed",
            "failure",
            "cancelled",
            "canceled",
            "expired",
            "error",
            "timeout",
        }
        success = {"success", "succeeded", "completed"}
        if mode == "custom_endpoint":
            failure.update(
                s.strip().lower()
                for s in str(self.config.get("custom_failure_status", "")).split(",")
                if s.strip()
            )
            success.update(
                s.strip().lower()
                for s in str(self.config.get("custom_success_status", "")).split(",")
                if s.strip()
            )
        record = self.data_dir / (uuid.uuid4().hex + ".json")
        info = {
            "task_id": task_id,
            "model": self.config["model"],
            "status": status,
            "created_at": time.time(),
        }
        await self._save_job_record(record, info)
        deadline = time.monotonic() + max(1, int(self.config.get("poll_timeout", 900)))
        errors = 0
        while raw is None:
            normalized = status.strip().lower()
            if normalized in failure:
                track(state="failed")
                raise VideoError(f"音频任务失败（ID：{task_id}）：{error or status}")
            if artifact_url and (not status or normalized in success):
                break
            if normalized in success:
                raise VideoError(f"音频任务 {task_id} 已完成，但没有返回音频产物。")
            if not task_id or not query:
                raise VideoError(
                    "接口没有返回音频产物或可查询的任务 ID："
                    + (error or "请核对上游任务记录，勿重复提交。")
                )
            if time.monotonic() >= deadline:
                raise VideoError(
                    f"音频等待超时，任务 ID：{task_id}；请查询原任务，勿重复提交。"
                )
            await asyncio.sleep(
                min(
                    max(0.1, float(self.config.get("poll_interval", 5))),
                    max(0, deadline - time.monotonic()),
                )
            )
            try:
                poll = self.audio_endpoint(
                    query.replace("{task_id}", quote(task_id, safe=""))
                )
                data = await asyncio.wait_for(
                    self._json_request("GET", poll, key, headers),
                    max(0.01, deadline - time.monotonic()),
                )
                _, status, artifact_url, error = self.parse_task(data)
                error = error.replace(key, "[redacted]")
                errors = 0
            except (aiohttp.ClientError, asyncio.TimeoutError, VideoError) as exc:
                errors += 1
                if errors > 3 or (
                    isinstance(exc, VideoHttpError)
                    and exc.status < 500
                    and exc.status not in {408, 429}
                ):
                    raise VideoError(
                        f"音频查询中断（ID：{task_id}）：{exc}；勿重复提交。"
                    ) from exc
        if status.strip().lower() in failure:
            track(state="failed")
            raise VideoError(f"音频任务失败（ID：{task_id}）：{error or status}")
        if raw is not None:
            fmt = str(self.config.get("response_format", "wav"))
            if mime.lower().startswith(("audio/l16", "audio/pcm")) or fmt == "pcm":
                rate_match = re.search(r"rate=(\d+)", mime)
                rate = (
                    int(rate_match[1])
                    if rate_match
                    else int(self.config.get("sample_rate", 32000))
                    if mode == "minimax_speech"
                    else 24000
                )
                output = io.BytesIO()
                with wave.open(output, "wb") as wav:
                    wav.setnchannels(
                        int(self.config.get("channels", 1))
                        if mode == "minimax_speech"
                        else 1
                    )
                    wav.setsampwidth(2)
                    wav.setframerate(rate)
                    wav.writeframes(raw)
                raw = output.getvalue()
            head = raw[:32]
            if not (
                (head[:4] == b"RIFF" and head[8:12] == b"WAVE")
                or head.startswith(
                    (
                        b"ID3",
                        b"fLaC",
                        b"OggS",
                        b"#!AMR",
                        b"#!SILK",
                        b"\x02#!SILK",
                    )
                )
                or (len(head) > 1 and head[0] == 255 and head[1] & 224 == 224)
                or (len(head) > 8 and head[4:8] == b"ftyp")
            ):
                track(state="failed")
                raise InvalidMediaError(
                    "接口未返回可识别的音频数据，请核对原任务，勿重复提交。"
                )
            path = self.data_dir / (
                uuid.uuid4().hex
                + "."
                + (
                    "wav"
                    if head[:4] == b"RIFF"
                    else "flac"
                    if head.startswith(b"fLaC")
                    else "ogg"
                    if head.startswith(b"OggS")
                    else "amr"
                    if head.startswith(b"#!AMR")
                    else "silk"
                    if head.startswith((b"#!SILK", b"\x02#!SILK"))
                    else "m4a"
                    if head[4:8] == b"ftyp"
                    else "aac"
                    if len(head) > 1 and head[0] == 255 and head[1] & 0xF6 == 0xF0
                    else "mp3"
                )
            )
            if (
                not raw
                or len(raw)
                > int(self.config.get("max_audio_size_mb", 50)) * 1024 * 1024
            ):
                raise VideoError("音频输出为空或超过大小上限。")
            track(state="completed")
            await asyncio.to_thread(path.write_bytes, raw)
        else:
            track(state="completed")
            try:
                path = await self.download_audio(artifact_url)
            except InvalidMediaError:
                raise
            except (
                VideoError,
                OSError,
                aiohttp.ClientError,
                asyncio.TimeoutError,
            ) as exc:
                raise AudioDownloadError(
                    f"音频已生成但下载失败（ID：{task_id}）：{exc}", artifact_url
                ) from exc
        info.update(status="completed", path=str(path))
        await self._save_job_record(record, info)
        return AudioResult(path, task_id, str(self.config["model"]), artifact_url)

    async def download_audio(self, url: str) -> Path:
        """Download bounded public audio without leaking provider credentials.

        Args:
            url: Public audio artifact or reference URL.

        Returns:
            A local file identified from its audio signature.

        Raises:
            VideoError: If the URL, response or size is invalid.
        """
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
        ):
            raise VideoError("音频地址必须是有效 HTTP(S) URL。")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.data_dir / (uuid.uuid4().hex + ".part")
        maximum = max(1, int(self.config.get("max_audio_size_mb", 50))) * 1024 * 1024
        session = await self._get_session()
        try:
            async with session.get(
                url,
                headers={},
                timeout=aiohttp.ClientTimeout(
                    total=max(
                        1, int(self.config.get("result_audio_download_timeout", 300))
                    )
                ),
                proxy=self._get_request_proxy(
                    url,
                    self.config.get("proxy_url")
                    if self.config.get("use_proxy")
                    else None,
                ),
            ) as response:
                response.raise_for_status()
                total = 0
                with path.open("wb") as out:
                    async for chunk in response.content.iter_chunked(65536):
                        total += len(chunk)
                        if total > maximum:
                            raise VideoError("音频超过下载大小上限。")
                        out.write(chunk)
            with path.open("rb") as check:
                head = check.read(32)
            if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
                suffix = ".wav"
            elif head.startswith(b"fLaC"):
                suffix = ".flac"
            elif head.startswith(b"OggS"):
                suffix = ".ogg"
            elif len(head) > 1 and head[0] == 255 and head[1] & 0xF6 == 0xF0:
                suffix = ".aac"
            elif head.startswith(b"ID3") or (
                len(head) > 1 and head[0] == 255 and head[1] & 224 == 224
            ):
                suffix = ".mp3"
            elif len(head) > 8 and head[4:8] == b"ftyp":
                suffix = ".m4a"
            elif head.startswith((b"#!AMR", b"#!SILK", b"\x02#!SILK")):
                suffix = ".amr" if head.startswith(b"#!AMR") else ".silk"
            else:
                track(state="failed")
                raise InvalidMediaError(
                    "下载结果不是可识别的音频文件，可能是接口错误页面。"
                )
            completed = path.with_suffix(suffix)
            path.replace(completed)
            return completed
        finally:
            path.unlink(missing_ok=True)
