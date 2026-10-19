"""Provider result, parameter validation and caller identity regressions."""

import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from aiohttp import web

import test_audit_regressions as audit
import test_audio_generation as audio
import test_billing as financial
import test_video_plugin as behavior


class AsyncAudioResultTest(unittest.IsolatedAsyncioTestCase):
    async def generate(self, directory, result):
        calls = []

        async def handler(request):
            calls.append(request.method)
            if request.method == "POST":
                return web.json_response({"id": "original-task", "status": "queued"})
            return web.json_response({"id": "original-task", **result})

        base = await audit.ImageHttpAuditTest.server(self, handler)
        config = {
            "model": "custom-tts",
            "interface_mode": "custom_endpoint",
            "auto_route": False,
            "base_url": base,
            "api_keys": "offline-key",
            "poll_interval": 0.1,
            "custom_create_path": "/audio/create",
            "custom_query_path": "/audio/tasks/{task_id}",
            "custom_body_template": '{"model":"{model}","text":"{text}"}',
            "custom_base64_path": "result.audio_base64",
        }
        manager = behavior.audio_api.AudioApiManager(config, Path(directory))

        async def close():
            if manager._session:
                await manager._session.close()

        self.addAsyncCleanup(close)
        service = financial.b.BillingService(
            Path(directory) / "billing.sqlite3",
            {"billing_prices": [{"model": config["model"], "expression": "fixed(0.1)"}]},
        )
        event = financial.Identity()
        owner = service.owner(event)
        service.store.redeem(owner, service.store.create_cards("admin", "10", 1)[0])

        async def invoke():
            return await financial.b.billed_call(
                service, event, "audio", config, {},
                lambda: manager._generate_audio("hello", []),
            )

        return manager, service, owner, calls, invoke

    async def test_custom_poll_base64_is_delivered_once_and_settled(self):
        content = audio.wav_bytes()
        with tempfile.TemporaryDirectory() as directory:
            manager, service, owner, calls, invoke = await self.generate(directory, {
                "status": "completed",
                "result": {"audio_base64": base64.b64encode(content).decode()},
            })
            result = await invoke()
            self.assertEqual(result.path.read_bytes(), content)
            self.assertEqual(result.task_id, "original-task")
            self.assertEqual(calls, ["POST", "GET"])
            self.assertEqual(service.store.balance(owner)[0], 9900000)

    async def test_failed_or_invalid_inline_poll_never_charges(self):
        for status, encoded in (
            ("failed", base64.b64encode(audio.wav_bytes()).decode()),
            ("completed", "not valid base64!"),
            ("completed", base64.b64encode(b"<html>error</html>").decode()),
        ):
            with self.subTest(status=status, encoded=encoded[:20]), tempfile.TemporaryDirectory() as directory:
                manager, service, owner, calls, invoke = await self.generate(directory, {
                    "status": status, "result": {"audio_base64": encoded},
                })
                with self.assertRaises(behavior.router.VideoError):
                    await invoke()
                self.assertEqual(calls, ["POST", "GET"])
                self.assertEqual(service.store.balance(owner)[0], 10000000)
                self.assertFalse(list(manager.data_dir.glob("*.wav")))


class VideoDefaultsValidationTest(unittest.TestCase):
    def test_unrouted_defaults_still_reject_wrong_types_and_unsupported_fields(self):
        for mode, defaults, message in (
            ("seedance", '{"draft":"yes"}', "true 或 false"),
            ("minimax", '{"watermark":true}', "不支持参数 watermark"),
        ):
            manager = behavior.api.VideoApiManager({
                "model": "provider-model", "interface_mode": mode,
                "auto_route": False, "parameter_defaults": defaults,
            }, Path("unused"))
            with self.subTest(mode=mode), self.assertRaisesRegex(behavior.router.VideoError, message):
                manager.prepare("cat", [], [], [])

    def test_explicit_values_override_unrouted_defaults_and_keep_false_zero(self):
        manager = behavior.api.VideoApiManager({
            "model": "provider-model", "interface_mode": "seedance",
            "auto_route": False,
            "parameter_defaults": '{"draft":"invalid default", "watermark":true}',
        }, Path("unused"))
        _, request = manager.prepare("cat", [], [], [], options=behavior.router.VideoOptions(
            seed=0, parameters={"draft": False, "watermark": False},
        ))
        body = request.build_request("cat", [], [], [])[2]
        self.assertEqual((body["draft"], body["watermark"], body["seed"]), (False, False, 0))


class ToolIdentityIsolationTest(unittest.IsolatedAsyncioTestCase):
    setUp = behavior.VideoPluginTest.setUp

    async def asyncTearDown(self):
        await self.plugin.terminate()

    async def test_equal_ids_on_different_platforms_have_independent_cooldowns(self):
        for kind, kwargs in (
            ("image", {"prompt": "cat", "use_message_media": False}),
            ("video", {"prompt": "cat", "use_message_media": False}),
            ("audio", {"text": "hello", "use_message_media": False}),
        ):
            config = self.plugin.conf if kind == "image" else getattr(self.plugin, kind + "_conf")
            config["llm_cooldown_seconds"] = 60
            generator = AsyncMock(return_value=[])
            setattr(self.plugin, "_generate" if kind == "image" else "_generate_" + kind, generator)
            tool = getattr(self.plugin, "generate_" + kind)
            for platform in ("aiocqhttp", "telegram"):
                event = behavior.Event([])
                event.get_platform_name = lambda platform=platform: platform
                result = [part async for part in tool(event, **kwargs)]
                self.assertEqual(result, [], (kind, platform))
            repeated = behavior.Event([])
            repeated.get_platform_name = lambda: "aiocqhttp"
            result = [part async for part in tool(repeated, **kwargs)]
            self.assertIn("冷却", result[0])
            self.assertEqual(generator.await_count, 2)
