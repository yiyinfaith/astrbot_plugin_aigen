"""Configuration consolidation and offline artifact-delivery regressions."""

import copy
import importlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_video_plugin as behavior
from aiohttp import web
from test_api_manager_urls import PACKAGE
from test_audio_generation import wav_bytes
from test_config_migration import loader_class

from config_manager import AUDIO_ADAPTER_DEFAULTS, migrate_config, schema_defaults

ROOT = Path(__file__).resolve().parents[1]
billing = importlib.import_module(PACKAGE + ".billing")


class CompactSettingsTest(unittest.TestCase):
    def setUp(self):
        self.schema = json.loads((ROOT / "_conf_schema.json").read_text("utf-8"))

    def test_host_migration_preserves_custom_adapter_and_is_idempotent(self):
        config = schema_defaults(self.schema)
        config["_config_layout_version"] = 5
        config["image_settings"].update(
            image_delivery_retries=3,
            result_image_download_retries=4,
            download_retries=1,
            prompt_list=["custom:keep exactly"],
        )
        config["audio_settings"].update(
            AUDIO_ADAPTER_DEFAULTS,
            custom_create_path="/v3/create",
            custom_query_path="",
            custom_body_template='{"model":"{model}","input":"{text}","speed":"{speed}"}',
            custom_headers='{"X-Test":"private-value"}',
            custom_url_path="output.url",
            parameter_defaults='{"speed":1.2,"adapter":{"url_path":"result.url"}}',
        )
        config["global_settings"]["billing_timezone"] = "Asia/Shanghai"
        config["video_settings"]["reference_mode"] = "first_frame"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            loaded = loader_class()(str(path), schema=self.schema)
            self.assertTrue(migrate_config(loaded, Path(directory) / "backup"))
            self.assertEqual(loaded["image_settings"]["image_delivery_retries"], 3)
            self.assertEqual(
                loaded["image_settings"]["prompt_list"], ["custom:keep exactly"]
            )
            self.assertNotIn("reference_mode", loaded["video_settings"])
            self.assertNotIn("billing_timezone", loaded["global_settings"])
            defaults = json.loads(loaded["audio_settings"]["parameter_defaults"])
            self.assertEqual(defaults["speed"], 1.2)
            self.assertEqual(defaults["adapter"]["query_path"], "")
            self.assertEqual(defaults["adapter"]["url_path"], "result.url")
            self.assertEqual(
                defaults["adapter"]["headers"], {"X-Test": "private-value"}
            )
            reloaded = loader_class()(str(path), schema=self.schema)
            self.assertFalse(migrate_config(reloaded, Path(directory) / "backup"))
            self.assertEqual(
                reloaded["audio_settings"]["parameter_defaults"],
                loaded["audio_settings"]["parameter_defaults"],
            )
            self.assertEqual(len(list((Path(directory) / "backup").glob("*.json"))), 1)

    def test_default_adapter_migration_keeps_optional_json_empty(self):
        config = schema_defaults(self.schema)
        config["_config_layout_version"] = 5
        config["audio_settings"].update(AUDIO_ADAPTER_DEFAULTS)
        with tempfile.TemporaryDirectory() as directory:
            migrate_config(config, Path(directory))
        self.assertEqual(config["audio_settings"]["parameter_defaults"], "")

    def test_compact_migration_save_failure_preserves_original(self):
        class Broken(dict):
            def save_config(self):
                raise OSError("disk full")

        config = Broken(schema_defaults(self.schema))
        config["_config_layout_version"] = 5
        config["audio_settings"].update(custom_create_path="/custom")
        original = copy.deepcopy(config)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(OSError):
                migrate_config(config, Path(directory))
            self.assertEqual(config, original)
            self.assertEqual(len(list(Path(directory).glob("*.json"))), 1)

    def test_visible_schema_has_one_retry_control_and_no_old_selectors(self):
        self.assertNotIn("billing_timezone", self.schema["global_settings"]["items"])
        self.assertNotIn("reference_mode", self.schema["video_settings"]["items"])
        for kind in ("image", "video", "audio"):
            visible = {
                k
                for k, v in self.schema[kind + "_settings"]["items"].items()
                if not v.get("invisible")
            }
            self.assertEqual(
                {k for k in visible if "retries" in k}, {kind + "_delivery_retries"}
            )
            if kind == "audio":
                self.assertFalse(visible & AUDIO_ADAPTER_DEFAULTS.keys())
                self.assertIn("parameter_defaults", visible)

    def test_audio_json_route_overrides_preserve_adapter_without_upstream_leak(self):
        config = schema_defaults(self.schema)["audio_settings"]
        config.update(base_url="https://example.test", api_keys="key")
        config["parameter_defaults"] = json.dumps(
            {
                "speed": 1.2,
                "adapter": {
                    "create_path": "/create",
                    "query_path": "",
                    "headers": {"X-Private": "secret"},
                    "body_template": {
                        "model": "{model}",
                        "text": "{text}",
                        "speed": "{speed}",
                    },
                },
            }
        )
        config["routes"] = [
            {
                "__template_key": "text_to_speech",
                "model": "custom-tts",
                "interface_mode": "custom_endpoint",
                "parameter_defaults": '{"speed":1.4,"adapter":{"url_path":"result.file"}}',
            }
        ]
        manager = behavior.audio_api.AudioApiManager(config, Path("unused"))
        _, prepared = manager.prepare(
            behavior.audio_router.AudioOptions(
                prompt="read", parameters={"speed": 1.6}
            ),
            [],
        )
        create, query, body, headers = prepared.build_request("read", [])
        self.assertEqual((create, query), ("/create", ""))
        self.assertEqual(body, {"model": "custom-tts", "text": "read", "speed": 1.6})
        self.assertEqual(headers["X-Private"], "secret")
        self.assertEqual(
            prepared.parse_task({"result": {"file": "https://cdn.test/a.wav"}})[2],
            "https://cdn.test/a.wav",
        )
        capabilities = behavior.capabilities.configured_capabilities("audio", config)
        self.assertIn('"speed"', capabilities)
        self.assertNotIn("secret", capabilities)
        self.assertNotIn("X-Private", capabilities)

    def test_invalid_adapter_is_rejected_before_submission(self):
        for adapter in (
            [],
            {"unknown": "x"},
            {"headers": []},
            {"body_template": "{"},
            {"create_path": 3},
        ):
            config = {
                "auto_route": False,
                "model": "custom",
                "interface_mode": "custom_endpoint",
                "base_url": "https://example.test",
                "parameter_defaults": json.dumps({"adapter": adapter}),
            }
            manager = behavior.audio_api.AudioApiManager(config, Path("unused"))
            with self.assertRaises(behavior.router.VideoError):
                manager.prepare(behavior.audio_router.AudioOptions(prompt="read"), [])

    def test_pricing_expression_owns_timezone_and_submission_time_is_frozen(self):
        class Clock(datetime):
            instant = datetime(2026, 10, 15, 15, 59, tzinfo=timezone.utc)

            @classmethod
            def now(cls, tz=None):
                return cls.instant.astimezone(tz)

        with tempfile.TemporaryDirectory() as directory:
            service = billing.BillingService(
                Path(directory) / "ledger.sqlite3",
                {
                    "billing_prices": [
                        {
                            "model": "m",
                            "expression": 'hour("Asia/Shanghai") >= 23 ? 1 : 2',
                        }
                    ],
                },
            )
            with patch.object(billing, "datetime", Clock):
                expression, values, fee, _ = service.quote("video", {"model": "m"}, {})
                self.assertEqual(fee, billing.units("1"))
                Clock.instant = datetime(2026, 10, 15, 16, 1, tzinfo=timezone.utc)
                self.assertEqual(expression.evaluate(values), billing.units("1"))
                self.assertEqual(
                    service.quote("video", {"model": "m"}, {})[2], billing.units("2")
                )
                self.assertEqual(values["_now"], "2026-10-15T15:59:00+00:00")


class ArtifactRetryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.posts = self.gets = 0
        self.failures = 99
        self.invalid = False

        async def handler(request):
            if request.method == "POST":
                self.posts += 1
                return web.json_response(
                    {
                        "id": "task",
                        "status": "succeeded",
                        "url": self.base + "/result.wav",
                    }
                )
            self.gets += 1
            if self.gets <= self.failures:
                raise web.HTTPServiceUnavailable()
            return web.Response(
                body=b"<html>error</html>" if self.invalid else wav_bytes()
            )

        app = web.Application()
        app.router.add_route("*", "/{path:.*}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.base = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
        self.manager = behavior.audio_api.AudioApiManager(
            {
                "base_url": self.base,
                "api_keys": "offline",
                "model": "custom",
                "auto_route": False,
                "interface_mode": "custom_endpoint",
                "audio_delivery_retries": 3,
            },
            Path(self.directory.name),
        )

        async def close_session():
            if self.manager._session:
                await self.manager._session.close()

        self.addAsyncCleanup(close_session)

    async def test_audio_download_retries_original_artifact_without_resubmitting(self):
        self.failures = 1
        with patch.object(behavior.audio_api.asyncio, "sleep", new=AsyncMock()):
            result = await self.manager.generate(
                behavior.audio_router.AudioOptions(prompt="read"), []
            )
        self.assertEqual(result.path.read_bytes(), wav_bytes())
        self.assertEqual((self.posts, self.gets), (1, 2))

    async def test_audio_exhaustion_preserves_original_link_and_cleans_partial_files(
        self,
    ):
        with (
            patch.object(behavior.audio_api.asyncio, "sleep", new=AsyncMock()),
            self.assertRaises(behavior.audio_api.AudioDownloadError) as raised,
        ):
            await self.manager.generate(
                behavior.audio_router.AudioOptions(prompt="read"), []
            )
        self.assertEqual(raised.exception.url, self.base + "/result.wav")
        self.assertEqual((self.posts, self.gets), (1, 3))
        self.assertFalse(list(Path(self.directory.name).rglob("*.part")))

    async def test_invalid_audio_is_never_retried_or_offered_as_completed_link(self):
        self.failures, self.invalid = 0, True
        with self.assertRaises(behavior.audio_api.InvalidMediaError):
            await self.manager.generate(
                behavior.audio_router.AudioOptions(prompt="read"), []
            )
        self.assertEqual((self.posts, self.gets), (1, 1))

    async def test_image_input_and_result_download_use_same_attempt_limit(self):
        input_manager = behavior.ImageManager(
            {"image_delivery_retries": 3, "download_retries": 99}
        )
        result_manager = behavior.image_api.ApiManager(
            {"image_delivery_retries": 3, "result_image_download_retries": 99}
        )
        try:
            with patch.object(behavior.asyncio, "sleep", new=AsyncMock()):
                self.assertIsNone(
                    await input_manager._download_image(self.base + "/input.png")
                )
                self.assertEqual(self.gets, 3)
                with self.assertRaises(behavior.image_api.ImageDownloadError):
                    await result_manager._download_result_image(
                        self.base + "/result.png", base_url=self.base
                    )
                self.assertEqual(self.gets, 6)
        finally:
            if result_manager._session:
                await result_manager._session.close()

    async def test_google_dual_routes_share_total_network_attempt_budget(self):
        for proxy in (None, "http://proxy.test"):
            session = SimpleNamespace(
                get=unittest.mock.Mock(side_effect=OSError("offline"))
            )
            session.close = AsyncMock()
            manager = behavior.image_api.ApiManager({"image_delivery_retries": 3})
            manager._get_session = AsyncMock(return_value=session)
            with (
                patch.object(
                    behavior.image_api.aiohttp, "ClientSession", return_value=session
                ),
                patch.object(behavior.asyncio, "sleep", new=AsyncMock()),
                self.assertRaises(behavior.image_api.ImageDownloadError),
            ):
                await manager._download_result_image(
                    "https://storage.googleapis.com/image.png",
                    base_url="https://api.test",
                    proxy=proxy,
                )
            self.assertEqual(session.get.call_count, 3)

    async def test_audio_delivery_failure_returns_link_after_voice_and_file_attempts(
        self,
    ):
        plugin = behavior.plugin_class().__new__(behavior.plugin_class())
        plugin.audio_conf = {"audio_delivery_retries": 3}
        plugin.audio_mgr = SimpleNamespace(
            prepare=lambda *args: None,
            generate=AsyncMock(
                return_value=behavior.audio_api.AudioResult(
                    Path("generated.wav"), "task", "model", self.base + "/result.wav"
                )
            ),
        )
        plugin.data_mgr = SimpleNamespace(record_usage=AsyncMock())
        event = behavior.Event([])
        event.send = AsyncMock(side_effect=OSError("platform unavailable"))
        result = await plugin._generate_audio(
            event, behavior.audio_router.AudioOptions(prompt="read"), [], tool_call=True
        )
        self.assertEqual(event.send.await_count, 4)
        self.assertIn(self.base + "/result.wav", result[0].text)
        plugin.audio_mgr.generate.assert_awaited_once()
