"""Failure and concurrency regressions found in the v1.4.2 source audit."""

import asyncio
import base64
import importlib
import json
import tempfile
import unittest
import wave
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import test_audio_generation as audio_tests
import test_video_collection as collection_tests
import test_video_plugin as behavior
from aiohttp import web
from test_api_manager_urls import PACKAGE, load_api_manager

load_api_manager()
image_api = importlib.import_module(PACKAGE + ".api_manager")
data_module = importlib.import_module(PACKAGE + ".data_manager")
utils = importlib.import_module(PACKAGE + ".utils")
IMAGE = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


class ImageHttpAuditTest(unittest.IsolatedAsyncioTestCase):
    async def server(self, handler):
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        return "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])

    def manager(self, base, mode="openai_image"):
        manager = image_api.ApiManager(
            {
                "base_url": base,
                "interface_mode": mode,
                "api_keys": "offline-key",
                "use_stream": True,
                "timeout": 5,
            }
        )

        async def close():
            if manager._session:
                await manager._session.close()

        self.addAsyncCleanup(close)
        return manager

    async def test_compatibility_fallback_preserves_cancellation_and_download_errors(
        self,
    ):
        async def handler(request):
            return web.json_response(
                {"error": {"message": "use images api"}}, status=400
            )

        base = await self.server(handler)
        manager = self.manager(base, "openai_chat")
        manager._should_fallback_to_images_api = lambda *args: True
        for error in (
            asyncio.CancelledError(),
            image_api.ImageDownloadError(
                "download unavailable", "https://media.test/result.png"
            ),
        ):
            manager.call_images_api = AsyncMock(side_effect=error)
            with self.assertRaises(type(error)):
                await manager.call_api([], "cat", "gpt-image-1")
            self.assertEqual(manager.call_images_api.await_count, 1)

    async def test_success_json_preserves_identifiers_and_error_messages_redact_keys(
        self,
    ):
        status = 200

        async def handler(request):
            if status == 200:
                return web.json_response(
                    {"id": "secret-task", "url": "https://media.test/secret-result.mp4"}
                )
            return web.json_response(
                {"error": {"message": "invalid credential secret"}}, status=status
            )

        base = await self.server(handler)
        manager = behavior.api.VideoApiManager(
            {"interface_mode": "minimax", "timeout": 5}, Path("unused")
        )
        manager._get_session = self.manager(base)._get_session
        result = await manager._json_request("GET", base, "secret", {})
        self.assertEqual(result["id"], "secret-task")
        self.assertEqual(result["url"], "https://media.test/secret-result.mp4")
        status = 401
        with self.assertRaises(behavior.api.VideoHttpError) as error:
            await manager._json_request("GET", base, "secret", {})
        self.assertNotIn("secret", str(error.exception))

    async def test_ambiguous_rejections_never_submit_another_image_job(self):
        calls = []
        status = 500

        async def handler(request):
            calls.append(request.path)
            return web.json_response(
                {"error": {"message": "stream unsupported; use images api; multipart"}},
                status=status,
            )

        base = await self.server(handler)
        for mode in ("openai_image", "openai_response", "openai_chat"):
            for references in ([], [IMAGE]):
                for status in (401, 403, 429, 500, 502, 503):
                    with self.subTest(
                        mode=mode, status=status, references=len(references)
                    ):
                        calls.clear()
                        result = await self.manager(base, mode).call_api(
                            references, "cat", "gpt-image-1"
                        )
                        self.assertIsInstance(result, str)
                        self.assertEqual(len(calls), 1)

    async def test_explicit_stream_format_rejection_preserves_safe_fallback(self):
        calls = []

        async def handler(request):
            fields = (
                await request.post()
                if request.content_type.startswith("multipart")
                else await request.json()
            )
            calls.append(fields.get("stream", False))
            if len(calls) == 1:
                return web.json_response(
                    {"error": {"message": "stream is not supported"}}, status=400
                )
            encoded = base64.b64encode(IMAGE).decode()
            return web.json_response(
                {"output": [{"type": "image_generation_call", "result": encoded}]}
                if request.path.endswith("responses")
                else {"data": [{"b64_json": encoded}]}
            )

        base = await self.server(handler)
        for mode, references in (
            ("openai_image", []),
            ("openai_image", [IMAGE]),
            ("openai_response", []),
        ):
            with self.subTest(mode=mode, references=len(references)):
                calls.clear()
                result = await self.manager(base, mode).call_api(
                    references, "cat", "gpt-image-1"
                )
                self.assertEqual(result, IMAGE)
                self.assertEqual(len(calls), 2)
                self.assertTrue(calls[0])
                self.assertFalse(calls[1])

    async def test_unrelated_bad_request_does_not_trigger_stream_fallback(self):
        calls = []

        async def handler(request):
            calls.append(request.path)
            return web.json_response(
                {"error": "stream request: unsupported image size"}, status=400
            )

        base = await self.server(handler)
        result = await self.manager(base).call_api([], "cat", "gpt-image-1")
        self.assertIsInstance(result, str)
        self.assertEqual(len(calls), 1)

    async def test_multi_reference_edits_keep_order_and_all_files(self):
        files = []

        async def handler(request):
            multipart = await request.multipart()
            while (part := await multipart.next()) is not None:
                value = await part.read()
                if part.filename:
                    files.append((part.name, bytes(value)))
            return web.json_response(
                {"data": [{"b64_json": base64.b64encode(IMAGE).decode()}]}
            )

        manager = self.manager(await self.server(handler))
        inputs = [IMAGE, IMAGE + b"second", IMAGE + b"third"]
        self.assertEqual(await manager.call_api(inputs, "cat", "gpt-image-1"), IMAGE)
        self.assertEqual(files, [("image[]", item) for item in inputs])

    async def test_custom_json_keeps_all_references(self):
        captured = {}

        async def handler(request):
            captured.update(await request.json())
            return web.json_response(
                {"data": [{"b64_json": base64.b64encode(IMAGE).decode()}]}
            )

        manager = self.manager(await self.server(handler))
        await manager.call_images_api(
            [IMAGE, IMAGE + b"second"],
            "cat",
            "custom",
            "key",
            manager.config["base_url"],
            exact_endpoint=True,
        )
        self.assertEqual(len(captured["images"]), 2)
        self.assertNotEqual(captured["images"][0], captured["images"][1])

    async def test_chat_artifact_download_error_retains_public_fallback_url(self):
        manager = self.manager("https://api.example.test", "openai_chat")

        async def handler(request):
            return web.json_response(
                {
                    "choices": [
                        {
                            "message": {
                                "images": [{"url": "https://media.test/image.png"}]
                            }
                        }
                    ]
                }
            )

        manager.config["base_url"] = await self.server(handler)
        manager._download_result_image = AsyncMock(
            side_effect=image_api.ImageDownloadError(
                "failed", "https://media.test/image.png"
            )
        )
        with self.assertRaises(image_api.ImageDownloadError) as error:
            await manager.call_api([], "cat", "model")
        self.assertEqual(error.exception.url, "https://media.test/image.png")

    async def test_chunked_image_download_has_a_size_bound(self):
        async def handler(request):
            response = web.StreamResponse()
            await response.prepare(request)
            await response.write(b"1234")
            await response.write(b"5678")
            await response.write_eof()
            return response

        base = await self.server(handler)
        manager = behavior.ImageManager({"download_retries": 0})
        with patch.object(utils, "MAX_IMAGE_BYTES", 6):
            self.assertIsNone(await manager._download_image(base))

    async def test_invalid_inline_output_is_not_a_successful_paid_image(self):
        billing = importlib.import_module(PACKAGE + ".billing")
        import test_billing as financial

        async def handler(request):
            return web.json_response(
                {
                    "data": [
                        {"b64_json": base64.b64encode(b"<html>error</html>").decode()}
                    ]
                }
            )

        manager = self.manager(await self.server(handler))
        with tempfile.TemporaryDirectory() as directory:
            service = billing.BillingService(
                Path(directory) / "billing.sqlite3",
                {"billing_prices": [{"model": "m", "price": "1"}]},
            )
            event = financial.Identity()
            owner = service.owner(event)
            service.store.redeem(owner, service.store.create_cards("admin", "10", 1)[0])
            result = await billing.billed_call(
                service,
                event,
                "image",
                {"model": "m"},
                {},
                lambda: manager.call_api([], "cat", "m"),
            )
            self.assertIn("完整图片", result)
            self.assertEqual(service.store.balance(owner), (10000000, 0))

    async def test_html_reference_is_rejected_before_generation(self):
        manager = behavior.ImageManager({})
        manager.load_raw_bytes = AsyncMock(return_value=b"<html>error</html>")
        self.assertIsNone(await manager.load_bytes("https://media.test/not-image.png"))


class PluginAuditTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = collection_tests.VideoCollectionTest()
        self.fixture.setUp()
        self.plugin = self.fixture.plugin

    async def asyncTearDown(self):
        await self.plugin.terminate()

    async def test_help_is_not_a_prompt_and_preserves_collection(self):
        await self.fixture.receive("/生视频")
        await self.fixture.receive("a cat")
        before = next(iter(self.plugin._video_collections.values()))
        help_event, result = await self.fixture.receive("/ai生成帮助")
        self.assertFalse(help_event.stopped)
        self.assertEqual(result, [])
        self.assertIs(next(iter(self.plugin._video_collections.values())), before)
        await self.fixture.receive("/生成")
        self.assertEqual(self.plugin.video_mgr.generate.await_args.args[0], "a cat")

    async def test_group_collection_lock_survives_per_member_conversation_ids(self):
        await self.fixture.receive("/生视频 cat", origin="qqbot:GroupMessage:group_99")
        outsider, result = await self.fixture.receive(
            "/生音频 朗读", sender="100", origin="qqbot:GroupMessage:group_100"
        )
        self.assertIn("已有用户正在录入", result[0][0].text)
        self.assertTrue(outsider.stopped)
        self.assertEqual(len(self.plugin._video_collections), 1)
        await self.fixture.receive(
            "/生视频 dog", sender="100", origin="anotherbot:GroupMessage:group_100"
        )
        self.assertEqual(len(self.plugin._video_collections), 2)

    async def test_statistics_failure_still_delivers_completed_image(self):
        self.plugin.api_mgr.call_api = AsyncMock(return_value=IMAGE)
        self.plugin.img_mgr.optimize_output_image = AsyncMock(return_value=IMAGE)
        self.plugin.data_mgr.record_usage = AsyncMock(side_effect=OSError("disk full"))
        event = behavior.Event([])
        result = await self.plugin._generate(
            event,
            "cat",
            "自定义",
            "model",
            [],
            show_progress=False,
            include_result_text=False,
            dispatch_result=True,
        )
        self.assertEqual(result, [])
        self.assertIsInstance(event.sent[-1][0], behavior.Image)

    async def test_unreadable_explicit_reference_does_not_route_to_text_generation(
        self,
    ):
        self.plugin.api_mgr.call_api = AsyncMock()
        self.plugin.img_mgr.load_bytes = AsyncMock(return_value=None)
        event = behavior.Event(
            [behavior.Image("https://media.test/missing.png")], "/生图 cat"
        )
        result = [item async for item in self.plugin.draw_command(event)]
        self.assertIn("未提交", result[0][0].text)
        self.plugin.api_mgr.call_api.assert_not_awaited()

    async def test_failed_tool_reference_releases_cooldown_and_returns_an_error(self):
        self.plugin.conf["llm_cooldown_seconds"] = 60
        self.plugin.img_mgr.load_bytes = AsyncMock(return_value=None)
        event = behavior.Event([behavior.Image("https://media.test/missing.png")])
        result = [
            item async for item in self.plugin.generate_image(event, prompt="cat")
        ]
        self.assertIn("未提交", result[0])
        self.assertFalse(self.plugin._llm_last_call)

    def test_audio_settings_apply_current_and_proposed_parameters(self):
        self.plugin.audio_conf = dict(
            audio_tests.settings()["audio_settings"],
            auto_route=False,
            model="IndexTTS-2",
            interface_mode="minimax",
        )
        collection = {
            "kind": "audio",
            "text": "原文",
            "audio_mode": "朗读",
            "audio_type": "reference_to_speech",
            "audios": ["https://media.test/a.wav"],
            "parameters": {"emo_control_method": "使用情感向量控制", "emo_happy": 0.8},
        }
        config = self.plugin._collection_settings_config(collection)
        self.assertEqual(config["emo_happy"], 0.8)
        config = self.plugin._collection_settings_config(
            collection, {**collection["parameters"], "emo_happy": 1}
        )
        self.assertEqual(config["emo_happy"], 1)
        self.assertEqual(collection["parameters"]["emo_happy"], 0.8)

    def test_custom_video_template_cannot_drop_text_or_references(self):
        for template, images, audios, videos in (
            ('{"model":"{model}"}', [], [], []),
            ('{"prompt":"{prompt}"}', ["a", "b"], [], []),
            ('{"prompt":"{prompt}"}', [], ["a"], []),
            ('{"prompt":"{prompt}"}', [], [], ["v"]),
        ):
            with self.subTest(
                template=template, images=images, audios=audios, videos=videos
            ):
                manager = behavior.api.VideoApiManager(
                    {
                        "model": "custom",
                        "interface_mode": "custom_endpoint",
                        "custom_body_template": template,
                    },
                    Path("unused"),
                )
                with self.assertRaises(behavior.router.VideoError):
                    manager.build_request("cat", images, audios, videos)


class AudioAuditTest(unittest.IsolatedAsyncioTestCase):
    async def test_minimax_pcm_preserves_configured_rate_channels_and_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {
                "model": "speech-02-hd",
                "interface_mode": "minimax_speech",
                "base_url": "https://api.example.test",
                "api_keys": "offline-key",
                "auto_route": False,
                "response_format": "pcm",
                "sample_rate": 16000,
                "channels": 2,
            }
            manager = behavior.audio_api.AudioApiManager(config, Path(directory))
            raw = b"\0\0" * 2 * 16000
            manager._json_request = AsyncMock(
                return_value={"data": {"audio": raw.hex()}}
            )
            result = await manager._generate_audio("hello", [])
            with wave.open(str(result.path), "rb") as audio:
                self.assertEqual(
                    (audio.getframerate(), audio.getnchannels(), audio.getnframes()),
                    (16000, 2, 16000),
                )
                self.assertEqual(audio.readframes(16000), raw)

    async def test_gemini_preserves_reference_audio_mime(self):
        for suffix, mime, raw in (
            (".flac", "audio/flac", b"fLaCexample"),
            (".ogg", "audio/ogg", b"OggSexample"),
            (".m4a", "audio/mp4", b"\0\0\0\0ftypaudio"),
        ):
            with (
                self.subTest(suffix=suffix),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory) / ("reference" + suffix)
                path.write_bytes(raw)
                manager = behavior.audio_api.AudioApiManager(
                    {
                        "model": "gemini-tts",
                        "interface_mode": "gemini_official",
                        "base_url": "https://api.example.test",
                        "api_keys": "key",
                        "response_format": "wav",
                        "auto_route": False,
                    },
                    Path(directory),
                )
                manager.download_audio = AsyncMock(return_value=path)
                manager._json_request = AsyncMock(
                    return_value={
                        "candidates": [
                            {
                                "content": {
                                    "parts": [
                                        {
                                            "inlineData": {
                                                "mimeType": "audio/wav",
                                                "data": base64.b64encode(
                                                    audio_tests.wav_bytes()
                                                ).decode(),
                                            }
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                )
                await manager._generate_audio(
                    "read", ["https://media.test/reference" + suffix]
                )
                inline = manager._json_request.await_args.args[4]["contents"][0][
                    "parts"
                ][-1]["inlineData"]
                self.assertEqual(inline["mimeType"], mime)
                self.assertEqual(base64.b64decode(inline["data"]), raw)


class StatisticsAuditTest(unittest.IsolatedAsyncioTestCase):
    async def test_partial_statistics_preserve_valid_counts_under_concurrent_writes(
        self,
    ):
        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "daily_stats.json"
            path.write_text(
                json.dumps(
                    {
                        "date": today,
                        "users": {"99": 2, "invalid": "bad", "other": 5},
                        "groups": [],
                        "by_media": {"image": {"users": None}},
                    }
                )
            )
            manager = data_module.DataManager(Path(directory), {})
            await manager.initialize()
            await asyncio.gather(
                *(manager.record_usage("99", "group") for _ in range(3))
            )
            saved = json.loads(path.read_text())
            self.assertEqual(saved["users"], {"99": 5, "other": 5})
            self.assertEqual(saved["groups"], {"group": 3})
            self.assertEqual(saved["by_media"]["image"]["users"], {"99": 3})

    async def test_invalid_statistics_root_recovers_on_record(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "daily_stats.json"
            path.write_text("[]")
            manager = data_module.DataManager(Path(directory), {})
            await manager.initialize()
            await manager.record_usage("99", None, "audio")
            self.assertEqual(json.loads(path.read_text())["users"], {"99": 1})


class ArtifactBillingAuditTest(unittest.IsolatedAsyncioTestCase):
    async def test_wrong_video_or_audio_artifact_is_not_charged_or_retried(self):
        import test_billing as financial

        billing = financial.b
        cases = (
            ("video", b"<html>error ftyp not a video</html>"),
            ("audio", b"<html>no audio</html>"),
            ("inline", b"RIFFxxxxWEBP" + b"x" * 40),
        )
        for kind, body in cases:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                calls = []

                async def handler(request):
                    calls.append(request.method)
                    if request.method == "POST" and kind != "inline":
                        return web.json_response(
                            {
                                "id": "task",
                                "status": "completed",
                                "url": "http://" + request.host + "/artifact",
                            }
                        )
                    return web.Response(
                        body=body, content_type="application/octet-stream"
                    )

                base = await ImageHttpAuditTest.server(self, handler)
                config = {
                    "base_url": base,
                    "api_keys": "offline-key",
                    "auto_route": False,
                    "timeout": 5,
                }
                if kind == "video":
                    config.update(
                        model="minimax_h3_lightx2v_no_pic", interface_mode="minimax"
                    )
                    manager = behavior.api.VideoApiManager(config, Path(directory))
                    submit = lambda: manager.generate("cat", [])
                    media = "video"
                else:
                    config.update(
                        model="test-speech",
                        interface_mode="openai_speech"
                        if kind == "inline"
                        else "custom_endpoint",
                        response_format="wav",
                        custom_create_path="/audio/create",
                        custom_query_path="/audio/tasks/{task_id}",
                        custom_body_template='{"model":"{model}","prompt":"{prompt}"}',
                    )
                    manager = behavior.audio_api.AudioApiManager(
                        config, Path(directory)
                    )
                    submit = lambda: manager.generate(
                        behavior.audio_router.AudioOptions(mode="朗读", prompt="hello"),
                        [],
                    )
                    media = "audio"
                service = billing.BillingService(
                    Path(directory) / "billing.sqlite3",
                    {
                        "billing_prices": [
                            {"model": config["model"], "expression": "fixed(1)"}
                        ]
                    },
                )
                event = financial.Identity()
                owner = service.owner(event)
                service.store.redeem(
                    owner, service.store.create_cards("admin", "10", 1)[0]
                )
                try:
                    with self.assertRaises(behavior.router.InvalidMediaError):
                        await billing.billed_call(
                            service, event, media, config, {}, submit
                        )
                    self.assertEqual(service.store.balance(owner)[0], 10000000)
                    self.assertEqual(calls.count("POST"), 1)
                    self.assertEqual(calls.count("GET"), 0 if kind == "inline" else 1)
                    _, rows = service.store.listing("orders", owner)
                    self.assertEqual(rows[0]["status"], "refunded")
                    self.assertEqual(rows[0]["charged"], 0)
                finally:
                    if manager._session:
                        await manager._session.close()
