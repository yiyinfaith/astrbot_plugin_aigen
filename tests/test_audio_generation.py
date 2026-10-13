"""Offline regression coverage for audio intent, collection and provider requests."""

import asyncio
import base64
import copy
import io
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import test_video_plugin as behavior
from aiohttp import web
from test_video_plugin import (
    Event,
    Record,
    Video,
    audio_api,
    audio_router,
    image_router,
)

from config_manager import migrate_config, schema_defaults

ROOT = Path(__file__).resolve().parents[1]


def settings():
    return schema_defaults(json.loads((ROOT / "_conf_schema.json").read_text("utf-8")))


def wav_bytes():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"\0\0" * 48)
    return stream.getvalue()


class AudioRoutingTest(unittest.TestCase):
    def setUp(self):
        self.config = settings()["audio_settings"]
        self.config["base_url"] = "https://api.example.com/v1"
        for rule in self.config["routes"]:
            if not rule["model"]:
                rule["model"] = "test-" + rule["__template_key"]

    def test_all_seven_routes_and_explicit_intents(self):
        cases = [
            ("朗读", "你好", [], "", "text_to_speech"),
            ("提示词", "雨滴声", [], "", "text_to_audio"),
            ("朗读", "你好", ["a"], "", "reference_to_speech"),
            ("提示词", "雨滴声", ["a"], "", "reference_to_audio"),
            ("提示词", "去掉噪声", ["a"], "audio_editing", "audio_editing"),
            ("朗读", "替换台词", ["a"], "speech_editing", "speech_editing"),
            ("朗读", "", ["a", "b"], "voice_conversion", "voice_conversion"),
        ]
        for mode, text, audios, explicit, expected in cases:
            with self.subTest(expected):
                options = audio_router.AudioOptions(
                    mode=mode, prompt=text, route=explicit
                )
                route, effective = audio_router.select_audio_route(
                    self.config, options, audios
                )
                self.assertEqual(route, expected)
                self.assertEqual(effective["audio_route"], expected)

    def test_literal_reading_text_and_fragment_count_are_preserved(self):
        result = audio_router.parse_audio_materials(
            ["  第一段 --duration 5 ", "", "类型=音频编辑，原封不动读这句话。"], "朗读"
        )
        self.assertEqual(
            result.prompt, "第一段 --duration 5 类型=音频编辑，原封不动读这句话。"
        )
        self.assertEqual(result.prompt_segments, 2)

    def test_invalid_inputs_fail_before_provider_submission(self):
        for options, audios in [
            (audio_router.AudioOptions(prompt=""), []),
            (audio_router.AudioOptions(prompt="x"), ["a"] * 4),
            (
                audio_router.AudioOptions(prompt="x", route="voice_conversion"),
                ["a", "b"],
            ),
            (audio_router.AudioOptions(prompt="x", route="speech_editing"), ["a", "b"]),
            (
                audio_router.AudioOptions(
                    mode="朗读", prompt="x", route="audio_editing"
                ),
                ["a"],
            ),
        ]:
            with self.assertRaises(audio_router.VideoError):
                audio_router.select_audio_route(self.config, options, audios)
        empty = settings()["audio_settings"]
        with self.assertRaisesRegex(audio_router.VideoError, "配置模型"):
            audio_router.select_audio_route(
                empty, audio_router.AudioOptions(prompt="x"), []
            )
        self.config["routes"].append(copy.deepcopy(self.config["routes"][0]))
        with self.assertRaisesRegex(audio_router.VideoError, "恰好一条"):
            audio_router.select_audio_route(
                self.config, audio_router.AudioOptions(mode="提示词", prompt="x"), []
            )

    def test_index_tts_v2_uses_exact_text_and_reference_audio_without_video_controls(
        self,
    ):
        manager = audio_api.AudioApiManager(self.config, Path("unused"))
        options = audio_router.AudioOptions(
            prompt="第一句。 第二句！", route="reference_to_speech"
        )
        _, routed = manager.prepare(options, ["https://media.example.com/voice.wav"])
        create, query, body, _ = routed.build_request(
            options.prompt, ["https://media.example.com/voice.wav"]
        )
        self.assertEqual(create, "/v2/video_generation")
        self.assertIn("{task_id}", query)
        self.assertEqual(
            body,
            {
                "model": "IndexTTS-2",
                "content": [
                    {"type": "text", "text": options.prompt},
                    {
                        "type": "audio_url",
                        "audio_url": {"url": "https://media.example.com/voice.wav"},
                        "role": "reference_audio",
                    },
                ],
            },
        )
        for count in [0, 3]:
            with self.assertRaises(audio_router.VideoError):
                manager.prepare(options, ["https://media.example.com/a.wav"] * count)

    def test_speech_protocols_never_drop_reference_audio(self):
        for mode in ["openai_speech", "minimax_speech", "dashscope"]:
            conf = dict(self.config, auto_route=False, model="tts", interface_mode=mode)
            manager = audio_api.AudioApiManager(conf, Path("unused"))
            with self.assertRaisesRegex(audio_router.VideoError, "仅支持"):
                manager.prepare(
                    audio_router.AudioOptions(prompt="hello"),
                    ["https://media.example.com/a.wav"],
                )

    def test_all_provider_payloads_and_complete_url_normalization(self):
        for mode, expected in [
            ("openai_speech", "/v1/audio/speech"),
            ("minimax_speech", "/v1/t2a_v2"),
            ("openai_chat", "/v1/chat/completions"),
            ("gemini_official", "/v1beta/models/tts-model:generateContent"),
            ("dashscope", "/api/v1/services/aigc/multimodal-generation/generation"),
        ]:
            with self.subTest(mode):
                config = dict(
                    self.config,
                    auto_route=False,
                    model="tts-model",
                    interface_mode=mode,
                )
                manager = audio_api.AudioApiManager(config, Path("unused"))
                _, manager = manager.prepare(
                    audio_router.AudioOptions(prompt="原文。"), []
                )
                create, _, body, _ = manager.build_request("原文。", [])
                self.assertEqual(create, expected)
                self.assertIn("原文。", json.dumps(body, ensure_ascii=False))
                manager.config["base_url"] = "https://api.example.com/proxy" + expected
                self.assertEqual(
                    manager.audio_endpoint(create),
                    "https://api.example.com/proxy" + expected,
                )
        manager = audio_api.AudioApiManager(
            dict(
                self.config,
                auto_route=False,
                model="IndexTTS-2",
                interface_mode="autodl_native",
            ),
            Path("unused"),
        )
        _, manager = manager.prepare(
            audio_router.AudioOptions(prompt="原文。"),
            ["https://media.example.com/a.wav"],
        )
        body = manager.build_request("原文。", ["https://media.example.com/a.wav"])[2]
        self.assertEqual(body["prompt_text"], "原文。")
        self.assertEqual(body["prompt_simple"], "https://media.example.com/a.wav")

    def test_custom_editing_template_preserves_every_audio(self):
        manager = audio_api.AudioApiManager(
            dict(
                self.config,
                auto_route=False,
                model="editing",
                interface_mode="custom_endpoint",
            ),
            Path("unused"),
        )
        _, manager = manager.prepare(
            audio_router.AudioOptions(prompt="", route="voice_conversion"),
            [
                "https://media.example.com/source.wav",
                "https://media.example.com/target.wav",
            ],
        )
        body = manager.build_request(
            "",
            [
                "https://media.example.com/source.wav",
                "https://media.example.com/target.wav",
            ],
        )[2]
        self.assertEqual(len(body["audios"]), 2)
        manager.config["custom_body_template"] = '{"model":"{model}","audio":"{audio}"}'
        with self.assertRaisesRegex(audio_router.VideoError, "全部音频"):
            manager.build_request(
                "",
                [
                    "https://media.example.com/source.wav",
                    "https://media.example.com/target.wav",
                ],
            )

    def test_image_routes_select_model_protocol_and_preserve_original_configuration(
        self,
    ):
        config = settings()["image_settings"]
        config["routes"][0].update(model="text-model", interface_mode="openai_response")
        config["routes"][1].update(model="edit-model", interface_mode="gemini_official")
        original = copy.deepcopy(config)
        self.assertEqual(
            image_router.select_image_route(config, [], "default")["model"],
            "text-model",
        )
        self.assertEqual(
            image_router.select_image_route(config, [b"image"], "default")[
                "interface_mode"
            ],
            "gemini_official",
        )
        self.assertEqual(config, original)

    def test_upgrade_removes_only_video_legacy_progress_and_preserves_presets(self):
        config = settings()
        config["_config_layout_version"] = 3
        config.pop("audio_settings")
        config["image_settings"].pop("routes")
        config["image_settings"].pop("auto_route")
        config["image_settings"].update(
            api_keys="private-test-value", prompt_list=["mine:all text"]
        )
        config["video_settings"]["generating_msg_template"] = "obsolete"
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(migrate_config(config, Path(tmp)))
            self.assertEqual(len(list(Path(tmp).glob("*.json"))), 1)
        self.assertNotIn("generating_msg_template", config["video_settings"])
        self.assertEqual(config["image_settings"]["prompt_list"], ["mine:all text"])
        self.assertEqual(config["image_settings"]["api_keys"], "private-test-value")
        self.assertIn("audio_settings", config)


class AudioCollectionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        behavior.VideoPluginTest.setUp(self)
        self.plugin.audio_conf = settings()["audio_settings"]
        self.plugin.audio_conf.update(
            base_url="https://api.example.com",
            api_keys="test-key",
            extra_prefix=["配音", "做声音"],
        )
        for rule in self.plugin.audio_conf["routes"]:
            if not rule["model"]:
                rule["model"] = "provider-" + rule["__template_key"]
            if rule["__template_key"] in {
                "audio_editing",
                "speech_editing",
                "voice_conversion",
            }:
                rule["interface_mode"] = "custom_endpoint"
        self.real = audio_api.AudioApiManager(self.plugin.audio_conf, Path("unused"))
        self.plugin.audio_mgr = Mock(_session=None)
        self.plugin.audio_mgr.prepare = Mock(wraps=self.real.prepare)
        self.plugin.audio_mgr.generate = AsyncMock(
            return_value=audio_api.AudioResult(
                Path("generated.wav"), "task-audio", "routed"
            )
        )

    async def asyncTearDown(self):
        await self.plugin.terminate()

    async def receive(self, text="", chain=None, sender="99"):
        event = Event(chain or [], text)
        event._sender_id = sender
        event.unified_msg_origin = "qq:group"
        result = [r async for r in self.plugin.on_preset_request(event)]
        return event, result

    async def test_entry_requires_intent_and_only_one_optional_type(self):
        for text, hint in [
            ("/生音频", "请选择"),
            ("/生音频 错误", "只能"),
            ("/生音频 朗读 错误", "第二个参数"),
            ("/生音频 提示词 音色克隆", "朗读"),
        ]:
            _, result = await self.receive(text)
            self.assertIn(hint, result[0][0].text)
            self.assertFalse(self.plugin._video_collections)

    async def test_text_only_merges_verbatim_and_sends_one_summary_and_only_audio(self):
        start, _ = await self.receive("/生音频 朗读")
        self.assertEqual(
            start.sent[0][0].text,
            "请发送材料，录入阶段发送“/设置”可设置详细参数，最后发送“/生成”开始生成音频，120秒超时（整个过程共用）",
        )
        for text in ["第一句话。", "第二句话！"]:
            event, result = await self.receive(text)
            self.assertEqual((event.sent, result), ([], []))
        event, result = await self.receive("/生成")
        self.assertEqual(result, [])
        options = self.plugin.audio_mgr.generate.await_args.args[0]
        self.assertEqual(options.prompt, "第一句话。 第二句话！")
        self.assertEqual(
            event.sent[0][0].text, "共收到0图片，0音频，0视频，2段文字，生成中"
        )
        self.assertIsInstance(event.sent[1][0], Record)
        self.assertEqual(len(event.sent), 2)

    async def test_audio_without_type_asks_then_bare_answer_is_not_counted_as_text(
        self,
    ):
        await self.receive("/配音 朗读")
        await self.receive("原封不动读这一句。")
        await self.receive(chain=[Record("https://media.example.com/voice.wav")])
        finish, result = await self.receive("/生成")
        self.assertEqual(finish.sent, [])
        self.assertIn("音色克隆", result[0][0].text)
        self.assertIn("语音编辑", result[0][0].text)
        self.plugin.audio_mgr.generate.assert_not_awaited()
        _, bad = await self.receive("随便")
        self.assertIn("不用 /", bad[0][0].text)
        selected, result = await self.receive("音色克隆")
        options = self.plugin.audio_mgr.generate.await_args.args[0]
        self.assertEqual(options.prompt, "原封不动读这一句。")
        self.assertEqual(options.prompt_segments, 1)
        self.assertEqual(options.route, "reference_to_speech")
        self.assertIn("1音频", selected.sent[0][0].text)
        self.assertIsInstance(selected.sent[1][0], Record)
        self.assertEqual(result, [])

    async def test_explicit_type_skips_question(self):
        await self.receive("/生音频 提示词 音频编辑")
        await self.receive("去掉背景噪声")
        await self.receive(chain=[Record("https://media.example.com/original.wav")])
        event, result = await self.receive("/生成")
        self.assertEqual(result, [])
        self.assertEqual(
            self.plugin.audio_mgr.generate.await_args.args[0].route, "audio_editing"
        )
        self.assertEqual(len(event.sent), 2)

    async def test_prompt_with_audio_offers_conditional_generation_and_editing(self):
        await self.receive("/做声音 提示词")
        await self.receive("参考这个旋律生成雨声")
        await self.receive(chain=[Record("https://media.example.com/original.wav")])
        _, result = await self.receive("/生成")
        self.assertIn("参考音频生成", result[0][0].text)
        self.assertIn("音频编辑", result[0][0].text)
        await self.receive("参考音频生成")
        self.assertEqual(
            self.plugin.audio_mgr.generate.await_args.args[0].route,
            "reference_to_audio",
        )

    async def test_voice_conversion_keeps_two_audio_sources_in_order(self):
        await self.receive("/生音频 朗读")
        for name in ["source", "target"]:
            await self.receive(chain=[Record(f"https://media.example.com/{name}.wav")])
        _, result = await self.receive("/生成")
        self.assertIn("音色转换", result[0][0].text)
        event, _ = await self.receive("音色转换")
        options, audios = self.plugin.audio_mgr.generate.await_args.args
        self.assertEqual(options.prompt_segments, 0)
        self.assertEqual(
            audios,
            [
                "https://media.example.com/source.wav",
                "https://media.example.com/target.wav",
            ],
        )
        self.assertIn("0段文字", event.sent[0][0].text)

    async def test_wrong_media_cancel_timeout_and_session_isolation(self):
        await self.receive("/生音频 朗读")
        await self.receive("hello")
        await self.receive(chain=[Video("https://media.example.com/a.mp4")])
        _, result = await self.receive("/生成")
        self.assertIn("不支持图片和视频", result[0][0].text)
        self.plugin.audio_mgr.generate.assert_not_awaited()
        await self.receive("/取消")
        self.assertFalse(self.plugin._video_collections)
        self.plugin.audio_conf["collection_timeout_seconds"] = 1
        await self.receive("/生音频 朗读")
        other, result = await self.receive("其他人说话", sender="other")
        self.assertFalse(other.stopped)
        await asyncio.sleep(1.05)
        self.assertFalse(self.plugin._video_collections)
        self.assertIn(
            "取消本次生音频",
            self.plugin.context.send_message.await_args.args[1][0].text,
        )

    async def test_type_selection_timeout_cancels_without_submitting(self):
        self.plugin.audio_conf["collection_timeout_seconds"] = 1
        await self.receive("/生音频 朗读")
        await self.receive("hello")
        await self.receive(chain=[Record("https://media.example.com/voice.wav")])
        _, result = await self.receive("/生成")
        self.assertIn("音色克隆", result[0][0].text)
        await asyncio.sleep(1.05)
        self.assertFalse(self.plugin._video_collections)
        self.plugin.audio_mgr.generate.assert_not_awaited()

    async def test_image_route_execution_preserves_its_key_pool_between_generations(
        self,
    ):
        self.plugin.conf = settings()["image_settings"]
        self.plugin.conf.update(base_url="https://api.example.com", api_keys="main-key")
        self.plugin.conf["routes"][0].update(
            model="text-image", api_keys="image-key-a\nimage-key-b"
        )
        self.plugin.api_mgr = behavior.image_api.ApiManager(self.plugin.conf)
        self.plugin.img_mgr.optimize_output_image = AsyncMock(
            return_value=b"png-result"
        )
        keys = []

        async def generate(manager, *args, **kwargs):
            keys.append(await manager.get_key())
            return b"png-result"

        with patch.object(behavior.image_api.ApiManager, "call_api", generate):
            for _ in range(2):
                await self.plugin._generate(
                    Event([], "draw"),
                    "a cat",
                    "自定义",
                    "default",
                    [],
                    show_progress=False,
                )
        self.assertEqual(keys, ["image-key-a", "image-key-b"])


class AudioHTTPTest(unittest.IsolatedAsyncioTestCase):
    async def test_route_specific_key_pool_keeps_rotating_across_requests(self):
        config = settings()["audio_settings"]
        config.update(base_url="https://api.example.com", api_keys="main-key")
        rule = next(
            r for r in config["routes"] if r["__template_key"] == "text_to_speech"
        )
        rule.update(
            model="tts",
            interface_mode="openai_speech",
            api_keys="route-key-a\nroute-key-b",
        )
        manager = audio_api.AudioApiManager(config, Path("unused"))
        options = audio_router.AudioOptions(prompt="hello")
        _, first = manager.prepare(options, [])
        self.assertEqual(await first.get_key(), "route-key-a")
        _, second = manager.prepare(options, [])
        self.assertIsNot(first, second)
        self.assertEqual(await second.get_key(), "route-key-b")

    async def test_sync_hex_base64_pcm_and_custom_audio_decoding(self):
        content = wav_bytes()
        responses = {
            "minimax_speech": {
                "base_resp": {"status_code": 0},
                "data": {"audio": content.hex()},
            },
            "openai_chat": {
                "choices": [
                    {"message": {"audio": {"data": base64.b64encode(content).decode()}}}
                ]
            },
            "gemini_official": {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "inlineData": {
                                        "mimeType": "audio/L16;codec=pcm;rate=24000",
                                        "data": base64.b64encode(b"\0\0" * 48).decode(),
                                    }
                                }
                            ]
                        }
                    }
                ]
            },
            "custom_endpoint": {"audio_base64": base64.b64encode(content).decode()},
        }
        with tempfile.TemporaryDirectory() as tmp:
            for mode, response in responses.items():
                with self.subTest(mode):
                    config = dict(
                        settings()["audio_settings"],
                        base_url="https://api.example.com",
                        api_keys="offline-key",
                        model="tts",
                        auto_route=False,
                        interface_mode=mode,
                    )
                    manager = audio_api.AudioApiManager(config, Path(tmp))
                    manager._json_request = AsyncMock(return_value=response)
                    result = await manager._generate_audio("原文。", [])
                    self.assertEqual(result.path.read_bytes(), content)
                    manager._json_request.assert_awaited_once()

    async def test_submission_and_terminal_errors_never_resubmit(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = dict(
                settings()["audio_settings"],
                base_url="https://api.example.com",
                api_keys="offline-key",
                model="provider",
                auto_route=False,
            )
            for response in [
                {
                    "task": {
                        "id": "task-failed",
                        "status": "failed",
                        "error": {"message": "upstream failure"},
                    }
                },
                {
                    "task": {
                        "id": "task-no-output",
                        "status": "succeeded",
                        "modality": "audio",
                    }
                },
            ]:
                manager = audio_api.AudioApiManager(config, Path(tmp))
                manager._json_request = AsyncMock(return_value=response)
                with self.assertRaises(audio_router.VideoError):
                    await manager._generate_audio("x", [])
                manager._json_request.assert_awaited_once()
            manager._json_request = AsyncMock(
                side_effect=audio_api.VideoHttpError("denied", 401)
            )
            with self.assertRaisesRegex(audio_router.VideoError, "denied"):
                await manager._generate_audio("x", [])
            manager._json_request.assert_awaited_once()

    async def test_invalid_inline_audio_is_not_delivered_as_a_wav(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = dict(
                settings()["audio_settings"],
                base_url="https://api.example.com",
                api_keys="offline-key",
                model="tts",
                auto_route=False,
                interface_mode="minimax_speech",
            )
            manager = audio_api.AudioApiManager(config, Path(tmp))
            manager._json_request = AsyncMock(
                return_value={"data": {"audio": b"<html>error</html>".hex()}}
            )
            with self.assertRaisesRegex(audio_router.VideoError, "可识别"):
                await manager._generate_audio("x", [])
            self.assertFalse(list(manager.data_dir.glob("*.wav")))

    async def test_v2_async_audio_and_speech_binary_do_not_resubmit_or_leak_credentials(
        self,
    ):
        calls = []
        content = wav_bytes()

        async def submit(request):
            calls.append((request.method, request.path, await request.json()))
            return web.json_response({"task_id": "created-task"})

        async def poll(request):
            return web.json_response(
                {
                    "task": {
                        "id": "created-task",
                        "status": "succeeded",
                        "modality": "audio",
                        "content": {"url": base + "/audio.wav"},
                    }
                }
            )

        async def artifact(request):
            self.assertNotIn("Authorization", request.headers)
            return web.Response(body=content, content_type="audio/wav")

        async def speech(request):
            body = await request.json()
            calls.append((request.method, request.path, body))
            self.assertEqual(body["input"], "原文。")
            return web.Response(body=content, content_type="audio/wav")

        app = web.Application()
        app.router.add_post("/v2/video_generation", submit)
        app.router.add_get("/v2/query/video_generation/created-task", poll)
        app.router.add_get("/audio.wav", artifact)
        app.router.add_post("/v1/audio/speech", speech)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        base = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
        with tempfile.TemporaryDirectory() as tmp:
            config = dict(
                settings()["audio_settings"],
                base_url=base,
                api_keys="offline-key",
                auto_route=False,
                model="IndexTTS-2",
                poll_interval=0.1,
            )
            manager = audio_api.AudioApiManager(config, Path(tmp))
            try:
                result = await manager.generate(
                    audio_router.AudioOptions(prompt="原文。"), [base + "/audio.wav"]
                )
                self.assertEqual(result.path.read_bytes(), content)
                manager.config.update(model="tts-model", interface_mode="openai_speech")
                result = await manager.generate(
                    audio_router.AudioOptions(prompt="原文。"), []
                )
                self.assertEqual(result.path.read_bytes(), content)
                self.assertEqual(len(calls), 2)
                records = list((Path(tmp) / "audio").glob("*.json"))
                self.assertEqual(len(records), 2)
                self.assertTrue(
                    all(
                        "offline-key" not in p.read_text()
                        and "原文" not in p.read_text()
                        for p in records
                    )
                )
            finally:
                if manager._session:
                    await manager._session.close()
        await runner.cleanup()


if __name__ == "__main__":
    unittest.main()
