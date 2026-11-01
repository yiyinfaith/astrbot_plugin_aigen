"""Offline artifact delivery regressions."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_video_plugin as behavior
from aiohttp import web
from test_audio_generation import wav_bytes


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
