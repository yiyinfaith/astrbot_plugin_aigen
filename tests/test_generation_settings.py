"""Request contracts, sparse settings, deadlines and group ownership."""

import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import test_audio_generation as audio_tests
import test_video_collection as collection_tests
import test_video_plugin as behavior

from config_manager import migrate_config

caps = behavior.capabilities
router = behavior.router
api = behavior.api
audio_api = behavior.audio_api
audio_router = behavior.audio_router


class ParameterContractTest(unittest.IsolatedAsyncioTestCase):
    def test_sparse_choices_are_atomic_and_preserve_unselected_fields(self):
        mapping = {"1": ("speed", 1), "2": ("speed", 2), "3": ("volume", 0)}
        current = {"voice": "alloy", "speed": 0.5}
        self.assertEqual(
            caps.apply_menu_selection("3", mapping, current), {**current, "volume": 0}
        )
        for invalid in ("1 2", "1 999", "0 1", "hello"):
            with self.assertRaises(router.VideoError):
                caps.apply_menu_selection(invalid, mapping, current)
        self.assertEqual(current, {"voice": "alloy", "speed": 0.5})
        self.assertIsNone(caps.apply_menu_selection("0", mapping, current))

    async def test_audio_explicit_options_override_route_and_pane_without_cache_leaks(
        self,
    ):
        config = audio_tests.settings()["audio_settings"]
        config.update(
            base_url="https://api.example.com", parameter_defaults='{"speed": 0.8}'
        )
        rule = next(
            r for r in config["routes"] if r["__template_key"] == "text_to_speech"
        )
        rule.update(
            model="speech-02-hd",
            interface_mode="minimax_speech",
            parameter_defaults='{"speed": 1.2, "volume": 2}',
            api_keys="a\nb",
        )
        manager = audio_api.AudioApiManager(config, Path("unused"))
        options = audio_router.AudioOptions(
            prompt="hello",
            parameters={"speed": 1.5, "volume": 0, "pitch": 0, "voice": "voice-custom"},
        )
        _, first = manager.prepare(options, [])
        _, second = manager.prepare(audio_router.AudioOptions(prompt="hello"), [])
        body = first.build_request("hello", [])[2]
        self.assertEqual(
            body["voice_setting"],
            {"voice_id": "voice-custom", "speed": 1.5, "vol": 0, "pitch": 0},
        )
        self.assertEqual(
            second.build_request("hello", [])[2]["voice_setting"]["speed"], 1.2
        )
        self.assertEqual(await first.get_key(), "a")
        self.assertEqual(await second.get_key(), "b")
        self.assertEqual(config["parameter_defaults"], '{"speed": 0.8}')

    def test_indextts_vector_controls_and_false_reach_body(self):
        config = {
            "model": "IndexTTS-2",
            "interface_mode": "minimax",
            "auto_route": False,
            "base_url": "https://api.example.com",
        }
        options = audio_router.AudioOptions(
            prompt="你好",
            parameters={
                "emo_control_method": "使用情感向量控制",
                "emo_calm": 0.3,
                "emo_angry": 0,
                "emo_random": False,
            },
        )
        _, manager = audio_api.AudioApiManager(config, Path("unused")).prepare(
            options, ["https://m.test/a.wav"]
        )
        body = manager.build_request("你好", ["https://m.test/a.wav"])[2]
        self.assertEqual(
            {key: body[key] for key in options.parameters}, options.parameters
        )
        options.parameters = {"speed": 1.2}
        with self.assertRaisesRegex(router.VideoError, "不支持参数 speed"):
            audio_api.AudioApiManager(config, Path("unused")).prepare(
                options, ["https://m.test/a.wav"]
            )

    def test_video_explicit_false_zero_and_adapter_extensions(self):
        config = {
            "model": "seedance-model",
            "interface_mode": "seedance",
            "auto_route": False,
            "generate_audio": True,
            "watermark": True,
        }
        options = router.VideoOptions(
            seed=0,
            generate_audio="false",
            parameters={
                "watermark": False,
                "camera_fixed": False,
                "return_last_frame": True,
                "service_tier": "flex",
                "draft": False,
            },
        )
        _, manager = api.VideoApiManager(config, Path("unused")).prepare(
            "猫", [], [], [], options=options
        )
        body = manager.build_request("猫", [], [], [])[2]
        self.assertEqual(body["seed"], 0)
        self.assertIs(body["generate_audio"], False)
        for key, value in options.parameters.items():
            self.assertEqual(body[key], value)
        config["interface_mode"] = "minimax"
        with self.assertRaisesRegex(router.VideoError, "不支持参数"):
            api.VideoApiManager(config, Path("unused")).prepare(
                "猫", [], [], [], options=options
            )

    def test_custom_template_only_advertises_and_sends_declared_controls(self):
        config = {
            "model": "custom",
            "interface_mode": "custom_endpoint",
            "auto_route": False,
            "custom_body_template": '{"text":"{prompt}","watermark":"{watermark}","seed":"{seed}"}',
        }
        self.assertEqual(
            set(caps.parameter_specs("video", config)), {"watermark", "seed"}
        )
        _, manager = api.VideoApiManager(config, Path("unused")).prepare(
            "cat",
            [],
            [],
            [],
            options=router.VideoOptions(parameters={"watermark": False, "seed": 0}),
        )
        self.assertEqual(
            manager.build_request("cat", [], [], [])[2],
            {"text": "cat", "watermark": False, "seed": 0},
        )

    def test_fractional_duration_only_reaches_motion_transfer_models(self):
        config = audio_tests.settings()["video_settings"]
        options = router.VideoOptions(duration=5.5, reference_mode="motion_transfer")
        _, manager = api.VideoApiManager(config, Path("unused")).prepare(
            "",
            ["https://m.test/face.png"],
            [],
            ["https://m.test/motion.mp4"],
            options=options,
        )
        self.assertEqual(
            manager.build_request(
                "", ["https://m.test/face.png"], [], ["https://m.test/motion.mp4"], 5.5
            )[2]["duration"],
            5.5,
        )
        options.reference_mode = "auto"
        with self.assertRaises(router.VideoError):
            api.VideoApiManager(config, Path("unused")).prepare(
                "cat", [], [], [], options=options
            )

    def test_capability_query_filters_secrets_and_describes_known_limits(self):
        config = audio_tests.settings()["video_settings"]
        config.update(
            api_keys="secret-value",
            base_url="https://secret-host",
            custom_headers='{"Authorization":"private"}',
        )
        text = caps.configured_capabilities("video", config)
        for secret in ("secret-value", "secret-host", "Authorization", "private"):
            self.assertNotIn(secret, text)
        data = json.loads(text)
        known = next(row for row in data if row.get("route") == "text_to_video")
        self.assertTrue(known["model_constraints_known"])
        self.assertNotIn("seed", known["parameters"])
        self.assertEqual(known["materials"]["max_seconds"], 15)

    async def test_image_command_uses_defaults_without_parsing_prompt_controls(self):
        manager = behavior.image_api.ApiManager(
            {
                "interface_mode": "openai_image",
                "base_url": "https://api.example.com",
                "api_keys": "test",
                "image_resolution": "1K",
            }
        )
        manager.call_images_api = AsyncMock(return_value=b"image")
        await manager.call_api(
            [],
            "4K 16:9 cat",
            "gpt-image-1",
            image_options={"_infer_prompt_parameters": False},
        )
        params = manager.call_images_api.await_args.kwargs["generation_params"]
        self.assertEqual((params["resolution"], params["aspect_ratio"]), ("1K", "4:3"))
        await manager.call_api(
            [],
            "cat",
            "gpt-image-1",
            image_options={
                "resolution": "2K",
                "aspect_ratio": "9:16",
                "output_compression": 0,
            },
        )
        params = manager.call_images_api.await_args.kwargs["generation_params"]
        self.assertEqual((params["resolution"], params["aspect_ratio"]), ("2K", "9:16"))
        body = {}
        manager._add_openai_image_options(
            body,
            {"output_compression": 0, "partial_images": 0},
            include_partial_images=True,
        )
        self.assertEqual(body, {"output_compression": 0, "partial_images": 0})

    def test_known_unsupported_and_invalid_values_fail_locally(self):
        cases = [
            (
                "audio",
                {"interface_mode": "openai_speech", "model": "tts-1"},
                {"instructions": "slow"},
            ),
            (
                "audio",
                {"interface_mode": "minimax_speech", "model": "speech"},
                {"speed": 3},
            ),
            (
                "video",
                {"interface_mode": "seedance", "model": "seed"},
                {"watermark": "false"},
            ),
            (
                "image",
                {"interface_mode": "gemini_official", "model": "gemini"},
                {"quality": "hd"},
            ),
            (
                "image",
                {"interface_mode": "openai_image", "model": "gpt-image-1"},
                {"n": 0},
            ),
        ]
        for kind, config, options in cases:
            with self.subTest(options), self.assertRaises(router.VideoError):
                caps.validate_options(kind, config, options)

    def test_timeout_migration_preserves_custom_values_and_backs_up(self):
        config = audio_tests.settings()
        config["_config_layout_version"] = 4
        config["video_settings"]["collection_timeout_seconds"] = 60
        config["audio_settings"]["collection_timeout_seconds"] = 240
        original = copy.deepcopy(config)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(migrate_config(config, Path(tmp)))
            self.assertEqual(
                config["video_settings"]["collection_timeout_seconds"], 120
            )
            self.assertEqual(
                config["audio_settings"]["collection_timeout_seconds"], 240
            )
            saved = next(Path(tmp).glob("config_before_v1.3.0_*.json"))
            self.assertEqual(json.loads(saved.read_text("utf-8")), original)
            self.assertFalse(migrate_config(config, Path(tmp)))

    def test_custom_optional_fields_are_omitted_when_not_provided(self):
        template = {
            "text": "{prompt}",
            "parameters": {"pitch": "{pitch}", "volume": "{volume}"},
        }
        values = {"{prompt}": "hello", "{volume}": 0}
        pruned = caps.prune_template_parameters(template, "audio", values)
        self.assertEqual(
            api.render_template(pruned, values),
            {"text": "hello", "parameters": {"volume": 0}},
        )
        with self.assertRaises(router.VideoError):
            caps.prune_template_parameters(
                {"instruction": "pitch={pitch}"}, "audio", {}
            )

    def test_integer_enum_rejects_booleans_and_capability_voice_matches_route(self):
        with self.assertRaises(router.VideoError):
            caps.validate_options(
                "audio", {"interface_mode": "minimax_speech"}, {"channels": True}
            )
        config = audio_tests.settings()["audio_settings"]
        rule = next(
            r for r in config["routes"] if r["__template_key"] == "text_to_speech"
        )
        rule.update(
            model="speech-02-hd", interface_mode="minimax_speech", voice="route-voice"
        )
        data = json.loads(caps.configured_capabilities("audio", config))
        row = next(r for r in data if r.get("route") == "text_to_speech")
        self.assertEqual(row["effective_defaults"]["voice"], "route-voice")
        self.assertTrue(row["configured_route_supported_by_protocol"])


class CollectionSettingsTest(unittest.IsolatedAsyncioTestCase):
    setUp = collection_tests.VideoCollectionTest.setUp
    asyncTearDown = collection_tests.VideoCollectionTest.asyncTearDown
    receive = collection_tests.VideoCollectionTest.receive

    def collection(self):
        return next(iter(self.plugin._video_collections.values()))

    def choice(self, field, value):
        return next(
            key
            for key, pair in self.collection()["settings_menu"].items()
            if pair == (field, value)
        )

    async def test_settings_numbers_never_become_prompt_and_deadline_is_fixed(self):
        await self.receive("/生视频 猫")
        collection = self.collection()
        deadline = collection["deadline"]
        event, _ = await self.receive("/设置")
        self.assertEqual(len(event.sent), 1)
        _, result = await self.receive(
            self.choice("duration", 5) + " " + self.choice("resolution", "768p")
        )
        self.assertIn("参数已保存", result[0][0].text)
        self.assertEqual(collection["deadline"], deadline)
        self.assertEqual(collection["text"], ["猫"])
        finish, _ = await self.receive("/生成")
        self.assertIn("1段提示词", finish.sent[0][0].text)
        options = self.plugin.video_mgr.generate.await_args.args[5]
        self.assertEqual(options.parameters, {"duration": 5, "resolution": "768p"})

    async def test_invalid_selection_is_atomic_and_zero_keeps_previous_settings(self):
        await self.receive("/生视频 猫")
        await self.receive("/设置")
        _, result = await self.receive(
            self.choice("duration", 5) + " " + self.choice("duration", 10)
        )
        self.assertIn("未保存", result[0][0].text)
        self.assertEqual(self.collection()["parameters"], {})
        await self.receive(self.choice("duration", 5))
        await self.receive("/设置")
        await self.receive("0")
        self.assertEqual(self.collection()["parameters"], {"duration": 5})
        await self.receive("/设置")
        await self.receive("/生成")
        self.plugin.video_mgr.generate.assert_awaited_once()

    async def test_other_users_are_not_collected_and_group_slot_releases_on_submission(
        self,
    ):
        await self.receive("/生视频 猫")
        deadline = self.collection()["deadline"]
        outsider, _ = await self.receive(
            "其他人的聊天", [behavior.Image("https://m.test/other.png")], sender="100"
        )
        self.assertFalse(outsider.stopped)
        _, result = await self.receive("/生视频 他人", sender="100")
        self.assertIn("已有用户正在录入", result[0][0].text)
        _, result = await self.receive("/生音频 朗读", sender="100")
        self.assertIn("已有用户正在录入", result[0][0].text)
        self.assertEqual(self.collection()["deadline"], deadline)
        self.assertEqual(self.collection()["text"], ["猫"])
        self.assertEqual(self.collection()["images"], [])
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.plugin.video_mgr.generate

        async def slow(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        self.plugin.video_mgr.generate = slow
        task = asyncio.create_task(self.receive("/生成"))
        await entered.wait()
        try:
            await self.receive("/生音频 朗读", sender="100")
            self.assertEqual(self.collection()["kind"], "audio")
            self.assertEqual(len(self.plugin._video_collections), 1)
        finally:
            release.set()
            await task

    async def test_image_command_bypasses_material_collection(self):
        await self.receive("/生视频 猫")
        event, _ = await self.receive("/生图 花")
        self.assertFalse(event.stopped)
        self.assertEqual(self.collection()["text"], ["猫"])

    async def test_qq_forward_and_single_plain_fallback(self):
        event = behavior.Event([])
        event.get_platform_name = lambda: "aiocqhttp"
        await self.plugin._send_settings_menu(event, ["说明", "时长选项", "比例选项"])
        self.assertEqual(len(event.sent), 1)
        self.assertEqual(len(event.sent[0]), 1)
        self.assertIsInstance(event.sent[0][0], behavior.Nodes)
        self.assertEqual(len(event.sent[0][0].nodes), 3)
        self.assertTrue(
            all(isinstance(node, behavior.Node) for node in event.sent[0][0].nodes)
        )
        original = event.send

        async def reject_nodes(result):
            if isinstance(result[0], behavior.Nodes):
                raise TypeError("not supported")
            await original(result)

        event.sent.clear()
        event.send = reject_nodes
        await self.plugin._send_settings_menu(event, ["说明", "时长选项"])
        self.assertEqual(len(event.sent), 1)
        self.assertEqual(event.sent[0][0].text, "说明\n\n时长选项")

    async def test_settings_revalidate_when_materials_change_route(self):
        rule = next(
            r for r in self.config["routes"] if r["__template_key"] == "text_to_video"
        )
        rule.update(model="seedance-model", interface_mode="seedance")
        await self.receive("/生视频 猫")
        await self.receive("/设置")
        await self.receive(self.choice("watermark", False))
        await self.receive(chain=[behavior.Image("https://m.test/face.png")])
        _, result = await self.receive("/生成")
        self.assertIn("不支持参数 watermark", result[0][0].text)
        self.plugin.video_mgr.generate.assert_not_awaited()
        await self.receive("/设置重置")
        await self.receive("/生成")
        self.plugin.video_mgr.generate.assert_awaited_once()

    async def test_menu_can_select_long_video_model(self):
        await self.receive("/生视频 猫", [behavior.Image("https://m.test/face.png")])
        await self.receive("/设置")
        _, result = await self.receive(self.choice("duration", 15))
        self.assertIn("参数已保存", result[0][0].text)
        await self.receive("/生成")
        self.plugin.video_mgr.generate.assert_awaited_once()


class AudioSettingsTest(unittest.IsolatedAsyncioTestCase):
    setUp = audio_tests.AudioCollectionTest.setUp
    asyncTearDown = audio_tests.AudioCollectionTest.asyncTearDown
    receive = audio_tests.AudioCollectionTest.receive

    async def test_type_question_and_menu_share_original_deadline(self):
        await self.receive("/生音频 朗读")
        collection = next(iter(self.plugin._video_collections.values()))
        deadline, timer = collection["deadline"], collection["timer"]
        await self.receive("你好")
        await self.receive(chain=[behavior.Record("https://m.test/voice.wav")])
        await self.receive("/设置")
        mapping = collection["settings_menu"]
        selection = next(
            key for key, pair in mapping.items() if pair == ("emo_random", False)
        )
        await self.receive(selection)
        await self.receive("/生成")
        self.assertTrue(collection["type_choices"])
        self.assertEqual(collection["deadline"], deadline)
        self.assertIs(collection["timer"], timer)
        event, _ = await self.receive("音色克隆")
        self.assertIn("1段文字", event.sent[0][0].text)
        options = self.plugin.audio_mgr.generate.await_args.args[0]
        self.assertEqual(options.parameters, {"emo_random": False})
