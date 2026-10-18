"""Route credentials and reference-conditioned audio preset regressions."""

import copy
import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_audio_generation as audio_fixture
import test_video_plugin as behavior
from aiohttp import web
from test_api_manager_urls import PACKAGE
from test_config_migration import loader_class
from test_video_api import MP4

from config_manager import migrate_config, schema_defaults

ROOT = Path(__file__).resolve().parents[1]
data_module = importlib.import_module(PACKAGE + ".data_manager")


class PresetAndSchemaTest(unittest.TestCase):
    def setUp(self):
        self.schema = json.loads((ROOT / "_conf_schema.json").read_text("utf-8"))
        self.defaults = schema_defaults(self.schema)

    def test_all_video_route_templates_expose_optional_credentials(self):
        for template in self.schema["video_settings"]["items"]["routes"][
            "templates"
        ].values():
            self.assertEqual(template["items"]["base_url"]["default"], "")
            self.assertEqual(template["items"]["api_keys"]["default"], "")
            self.assertTrue(template["items"]["api_keys"]["secret"])

    def test_host_normalization_preserves_presets_routes_and_new_route_credentials(
        self,
    ):
        config = copy.deepcopy(self.defaults)
        config["_config_layout_version"] = 6
        config["image_settings"]["prompt_list"] = ["image:precious original"]
        config["audio_settings"]["prompt_list"] = ["soft:complete audio prompt"]
        for rule in config["video_settings"]["routes"]:
            rule.pop("base_url", None)
            rule.pop("api_keys", None)
        config["video_settings"]["routes"][0].update(
            base_url="https://route.test", api_keys="route-key-a\nroute-key-b"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            loaded = loader_class()(str(path), schema=self.schema)
            self.assertTrue(migrate_config(loaded, Path(directory) / "backups"))
        self.assertEqual(
            loaded["image_settings"]["prompt_list"],
            config["image_settings"]["prompt_list"],
        )
        self.assertEqual(
            loaded["audio_settings"]["prompt_list"],
            config["audio_settings"]["prompt_list"],
        )
        for old, new in zip(
            config["video_settings"]["routes"], loaded["video_settings"]["routes"]
        ):
            self.assertEqual(new, {"base_url": "", "api_keys": "", **old})
        self.assertEqual(loaded["_config_layout_version"], 7)

    def test_three_preset_maps_load_independently(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = data_module.DataManager(
                Path(directory),
                {"prompt_list": ["same:image"]},
                {"prompt_list": ["same:video"]},
                {"prompt_list": ["same:audio:complete", "bad"]},
            )
            manager.reload_prompts()
        self.assertEqual(manager.prompt_map, {"same": "image"})
        self.assertEqual(manager.video_prompt_map, {"same": "video"})
        self.assertEqual(manager.audio_prompt_map, {"same": "audio:complete"})

    def test_presets_apply_only_to_conditional_tta_and_keep_original_options(self):
        config = {"prompt_list": ["轻声:slow", "轻声讲述:Speak softly with pauses"]}
        for mode, route, audios, expected in [
            ("提示词", "", ["a"], "Speak softly with pauses near microphone"),
            (
                "提示词",
                "reference_to_audio",
                ["a"],
                "Speak softly with pauses near microphone",
            ),
            ("提示词", "", [], "轻声讲述 near microphone"),
            ("提示词", "audio_editing", ["a"], "轻声讲述 near microphone"),
            ("朗读", "reference_to_speech", ["a"], "轻声讲述 near microphone"),
            ("朗读", "speech_editing", ["a"], "轻声讲述 near microphone"),
        ]:
            with self.subTest(mode=mode, route=route):
                options = behavior.audio_router.AudioOptions(
                    mode=mode, route=route, prompt="轻声讲述 near microphone"
                )
                self.assertEqual(
                    behavior.audio_router.resolve_audio_preset(config, options, audios),
                    expected,
                )
                self.assertEqual(options.prompt, "轻声讲述 near microphone")

    def test_preflight_and_request_use_same_single_expansion(self):
        config = dict(
            self.defaults["audio_settings"],
            auto_route=False,
            model="custom-tta",
            interface_mode="custom_endpoint",
            base_url="https://audio.test",
            prompt_list=["轻声:轻声 spoken description"],
        )
        manager = behavior.audio_api.AudioApiManager(config, Path("unused"))
        options = behavior.audio_router.AudioOptions(
            mode="提示词", prompt="轻声", prompt_segments=1
        )
        for _ in range(3):
            _, prepared = manager.prepare(options, ["https://media.test/ref.wav"])
            self.assertEqual(
                prepared.config["_audio_prompt"], "轻声 spoken description"
            )
            body = prepared.build_request(
                prepared.config["_audio_prompt"], ["https://media.test/ref.wav"]
            )[2]
            self.assertEqual(body["prompt"], "轻声 spoken description")
        self.assertEqual(options.prompt, "轻声")

    def test_video_route_inheritance_and_long_model_use_same_route_credentials(self):
        config = self.defaults["video_settings"]
        config.update(base_url="https://main.test", api_keys="main-key")
        config["routes"][0].update(
            model="short-model",
            long_model="long-model",
            base_url=" https://route.test ",
            api_keys=" route-key ",
        )
        original = copy.deepcopy(config)
        for seconds, model in [(5, "short-model"), (15, "long-model")]:
            _, effective = behavior.router.select_route(config, [], [], [], seconds)
            self.assertEqual(
                (effective["model"], effective["base_url"], effective["api_keys"]),
                (model, "https://route.test", "route-key"),
            )
        self.assertEqual(config, original)
        config["routes"][0].update(base_url=" \n ", api_keys=" \n ")
        _, inherited = behavior.router.select_route(config, [], [], [], 5)
        self.assertEqual(
            (inherited["base_url"], inherited["api_keys"]),
            ("https://main.test", "main-key"),
        )


class AudioPresetCollectionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        audio_fixture.AudioCollectionTest.setUp(self)
        self.plugin.audio_conf["prompt_list"] = [
            "轻声:Speak softly",
            "轻声讲述:Use reference voice to describe a quiet evening",
        ]
        self.real.config["prompt_list"] = self.plugin.audio_conf["prompt_list"]
        self.plugin.data_mgr.audio_prompt_map = {
            item.split(":", 1)[0]: item.split(":", 1)[1]
            for item in self.plugin.audio_conf["prompt_list"]
        }

    async def asyncTearDown(self):
        await self.plugin.terminate()

    async def receive(self, text="", chain=None, sender="99"):
        return await audio_fixture.AudioCollectionTest.receive(
            self, text, chain, sender
        )

    async def test_audio_keyword_starts_conditional_tta_and_requires_reference(self):
        start, result = await self.receive("/轻声讲述 near microphone")
        self.assertEqual(result, [])
        self.assertIn("开始生成音频", start.sent[0][0].text)
        _, missing = await self.receive("/生成")
        self.assertIn("参考音频", missing[0][0].text)
        self.plugin.audio_mgr.generate.assert_not_awaited()
        await self.receive(chain=[behavior.Record("https://media.test/reference.wav")])
        finish, result = await self.receive("/生成")
        self.assertEqual(result, [])
        options, audios = self.plugin.audio_mgr.generate.await_args.args
        _, prepared = self.real.prepare(options, audios)
        self.assertEqual(prepared.config["audio_route"], "reference_to_audio")
        self.assertEqual(
            prepared.config["_audio_prompt"],
            "Use reference voice to describe a quiet evening near microphone",
        )
        self.assertEqual(options.prompt_segments, 1)
        self.assertIsInstance(finish.sent[-1][0], behavior.Record)

    async def test_keyword_inside_collection_keeps_type_question_and_text_count(self):
        await self.receive("/生音频 提示词")
        await self.receive("轻声")
        await self.receive("near microphone")
        await self.receive(chain=[behavior.Record("https://media.test/reference.wav")])
        _, result = await self.receive("/生成")
        self.assertIn("请选择音频类型", result[0][0].text)
        finish, result = await self.receive("参考音频生成")
        self.assertEqual(result, [])
        options, audios = self.plugin.audio_mgr.generate.await_args.args
        _, prepared = self.real.prepare(options, audios)
        self.assertEqual(
            prepared.config["_audio_prompt"], "Speak softly near microphone"
        )
        self.assertIn("2段提示词", finish.sent[0][0].text)

    async def test_audio_preset_prefix_switch_and_group_owner_are_respected(self):
        event, result = await self.receive("轻声")
        self.assertFalse(event.stopped)
        self.assertEqual(result, [])
        self.plugin.audio_conf["preset_need_prefix"] = False
        _, result = await self.receive("轻声")
        self.assertEqual(result, [])
        _, result = await self.receive("/轻声讲述", sender="another-user")
        self.assertIn("本群已有用户正在录入", result[0][0].text)
        self.assertEqual(len(self.plugin._video_collections), 1)

    async def test_character_billing_uses_expanded_prompt(self):
        billing = importlib.import_module(PACKAGE + ".billing")
        platform = patch.object(
            behavior.Event, "get_platform_name", return_value="test", create=True
        )
        platform.start()
        self.addCleanup(platform.stop)
        with tempfile.TemporaryDirectory() as directory:
            model = next(
                route["model"]
                for route in self.plugin.audio_conf["routes"]
                if route["__template_key"] == "reference_to_audio"
            )
            self.plugin.billing = billing.BillingService(
                Path(directory) / "billing.sqlite3",
                {
                    "billing_prices": [
                        {"model": model, "expression": "characters * 0.01"}
                    ]
                },
            )
            identity = behavior.Event([])
            identity._sender_id = "99"
            owner = self.plugin.billing.owner(identity)
            code = self.plugin.billing.store.create_cards("admin", "10", 1)[0]
            self.plugin.billing.store.redeem(owner, code)
            await self.receive("/轻声")
            await self.receive(
                chain=[behavior.Record("https://media.test/reference.wav")]
            )
            _, result = await self.receive("/生成")
            self.assertEqual(result, [])
            balance = self.plugin.billing.store.balance(owner)[0]
            self.assertEqual(
                balance,
                billing.units("10") - len("Speak softly") * billing.units("0.01"),
            )


class VideoRouteHTTPTest(unittest.IsolatedAsyncioTestCase):
    async def test_route_keys_rotate_across_submissions_and_never_reach_public_media(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            requests, main_requests = [], []

            async def main_handler(request):
                main_requests.append(request.path)
                return web.Response(status=500)

            async def route_handler(request):
                requests.append(
                    (request.method, request.path, request.headers.get("Authorization"))
                )
                if request.path == "/result.mp4":
                    return web.Response(body=MP4)
                if request.method == "POST":
                    return web.json_response({"id": "task", "status": "queued"})
                return web.json_response(
                    {
                        "id": "task",
                        "status": "completed",
                        "url": route_base + "/result.mp4",
                    }
                )

            async def start(handler):
                app = web.Application()
                app.router.add_route("*", "/{path:.*}", handler)
                runner = web.AppRunner(app)
                await runner.setup()
                self.addAsyncCleanup(runner.cleanup)
                site = web.TCPSite(runner, "127.0.0.1", 0)
                await site.start()
                return "http://127.0.0.1:" + str(
                    site._server.sockets[0].getsockname()[1]
                )

            main_base, route_base = (
                await start(main_handler),
                await start(route_handler),
            )
            config = audio_fixture.settings()["video_settings"]
            config.update(
                base_url=main_base,
                api_keys="must-not-leak-main-key",
                duration=1,
                poll_interval=0.1,
            )
            config["routes"][0].update(
                model="minimax_h3_lightx2v_no_pic",
                interface_mode="openai_video",
                base_url=route_base,
                api_keys="route-a\nroute-b",
                resolution="480p",
                aspect_ratio="16:9",
            )
            manager = behavior.api.VideoApiManager(config, Path(directory))
            try:
                for _ in range(3):
                    result = await manager.generate("cat in snow", [])
                    self.assertEqual(result.path.read_bytes(), MP4)
            finally:
                if manager._session:
                    await manager._session.close()
            self.assertEqual(main_requests, [])
            self.assertEqual(
                [auth for method, _, auth in requests if method == "POST"],
                ["Bearer route-a", "Bearer route-b", "Bearer route-a"],
            )
            self.assertEqual(
                [
                    auth
                    for method, path, auth in requests
                    if method == "GET" and path != "/result.mp4"
                ],
                ["Bearer route-a", "Bearer route-b", "Bearer route-a"],
            )
            self.assertTrue(
                all(auth is None for _, path, auth in requests if path == "/result.mp4")
            )
