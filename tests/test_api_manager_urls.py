import base64
import importlib
import pathlib
import sys
import types
import unittest

from aiohttp import web

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = "shoubanhua_test_package"


def load_api_manager():
    if PACKAGE not in sys.modules:
        package = types.ModuleType(PACKAGE)
        package.__path__ = [str(ROOT)]
        sys.modules[PACKAGE] = package

    if "astrbot" not in sys.modules:
        astrbot = types.ModuleType("astrbot")
        astrbot.logger = types.SimpleNamespace(
            info=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: None,
        )
        astrbot.__path__ = []
        sys.modules["astrbot"] = astrbot

    if "astrbot.api" not in sys.modules:
        api = types.ModuleType("astrbot.api")
        api.logger = sys.modules["astrbot"].logger
        sys.modules["astrbot.api"] = api

    return importlib.import_module(f"{PACKAGE}.api_manager").ApiManager


class ApiEndpointBuildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ApiManager = load_api_manager()

    def setUp(self):
        self.manager = self.ApiManager({})

    def test_chat_mode_replaces_version_and_endpoint(self):
        self.assertEqual(
            self.manager._normalize_generic_chat_url(
                "https://api.example.com/openai/v1beta/chat/completions"
            ),
            "https://api.example.com/openai/v1/chat/completions",
        )

    def test_image_mode_replaces_version_and_endpoint(self):
        self.assertEqual(
            self.manager._convert_to_images_api_url(
                "https://api.example.com/v1/images/generations"
            ),
            "https://api.example.com/v1/images/generations",
        )

    def test_gemini_mode_always_uses_v1beta(self):
        self.assertEqual(
            self.manager._build_gemini_api_url(
                "https://generativelanguage.googleapis.com/v1/models/old:generateContent",
                "models/gemini-2.5-flash-image",
            ),
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-image:generateContent",
        )

class ApiImagesEditRequestTest(unittest.IsolatedAsyncioTestCase):
    async def test_image_mode_posts_edits_as_multipart(self):
        api_manager = load_api_manager()
        image_data = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
        )
        captured = {"fields": {}, "files": []}

        async def handle_edit(request):
            captured["path"] = request.path
            captured["authorization"] = request.headers.get("Authorization")
            captured["content_type"] = request.headers.get("Content-Type", "")

            reader = await request.multipart()
            async for part in reader:
                if part.filename:
                    captured["files"].append(
                        {
                            "name": part.name,
                            "filename": part.filename,
                            "content_type": part.headers.get("Content-Type"),
                            "data": await part.read(),
                        }
                    )
                else:
                    captured["fields"][part.name] = await part.text()

            return web.json_response(
                {"data": [{"b64_json": base64.b64encode(image_data).decode()}]}
            )

        application = web.Application()
        application.router.add_post("/v1/images/edits", handle_edit)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]

        manager = api_manager(
            {
                "interface_mode": "openai_image",
                "base_url": f"http://127.0.0.1:{port}/v1/images/generations",
                "api_keys": "test-key",
                "image_resolution": "1K",
                "image_aspect_ratio": "4:3",
                "timeout": 5,
            }
        )
        try:
            result = await manager.call_api(
                [image_data],
                "edit this image",
                "gpt-image-1",
                image_options={
                    "input_fidelity": "high",
                    "partial_images": 2,
                },
            )
        finally:
            if manager._session and not manager._session.closed:
                await manager._session.close()
            await runner.cleanup()

        self.assertEqual(result, image_data)
        self.assertEqual(captured["path"], "/v1/images/edits")
        self.assertEqual(captured["authorization"], "Bearer test-key")
        self.assertTrue(captured["content_type"].startswith("multipart/form-data;"))
        self.assertEqual(captured["fields"]["model"], "gpt-image-1")
        self.assertEqual(captured["fields"]["prompt"], "edit this image")
        self.assertNotIn("n", captured["fields"])
        self.assertEqual(captured["fields"]["size"], "1024x1024")
        self.assertEqual(captured["fields"]["input_fidelity"], "high")
        self.assertNotIn("partial_images", captured["fields"])
        self.assertEqual(len(captured["files"]), 1)
        self.assertEqual(captured["files"][0]["name"], "image")
        self.assertEqual(captured["files"][0]["content_type"], "image/png")
        self.assertEqual(captured["files"][0]["data"], image_data)


class ApiImagesCapabilityFilteringTest(unittest.TestCase):
    def test_unknown_model_does_not_receive_model_specific_options(self):
        api_manager = load_api_manager()
        manager = api_manager({})
        payload = {}
        manager._add_openai_image_options(
            payload,
            {
                "quality": "high",
                "input_fidelity": "high",
            },
            include_gpt_image_fields=True,
            include_input_fidelity=True,
            allowed_quality=set(),
            allowed_input_fidelity=set(),
        )
        self.assertNotIn("quality", payload)
        self.assertNotIn("input_fidelity", payload)


class ApiImagesGenerationOptionsTest(unittest.IsolatedAsyncioTestCase):
    async def test_openai_image_options_use_official_json_fields(self):
        api_manager = load_api_manager()
        captured = {}

        async def handle_generation(request):
            captured["path"] = request.path
            captured["payload"] = await request.json()
            return web.json_response(
                {"data": [{"b64_json": base64.b64encode(b"image").decode()}]}
            )

        application = web.Application()
        application.router.add_post("/v1/images/generations", handle_generation)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]

        manager = api_manager(
            {
                "interface_mode": "openai_image",
                "base_url": f"http://127.0.0.1:{port}",
                "api_keys": "test-key",
                "use_stream": True,
                "timeout": 5,
            }
        )
        try:
            result = await manager.call_api(
                [],
                "draw",
                "gpt-image-1",
                image_options={
                    "aspect_ratio": "16:9",
                    "size": "1536x1024",
                    "quality": "high",
                    "background": "transparent",
                    "output_format": "webp",
                    "output_compression": 80,
                    "moderation": "low",
                    "partial_images": 3,
                    "input_fidelity": "high",
                    "action": "edit",
                    "n": 2,
                },
            )
        finally:
            if manager._session and not manager._session.closed:
                await manager._session.close()
            await runner.cleanup()

        self.assertEqual(result, b"image")
        self.assertEqual(captured["path"], "/v1/images/generations")
        payload = captured["payload"]
        self.assertEqual(payload["size"], "1536x1024")
        self.assertEqual(payload["n"], 2)
        self.assertEqual(payload["quality"], "high")
        self.assertEqual(payload["background"], "transparent")
        self.assertEqual(payload["output_format"], "webp")
        self.assertEqual(payload["output_compression"], 80)
        self.assertEqual(payload["moderation"], "low")
        self.assertEqual(payload["partial_images"], 3)
        self.assertNotIn("input_fidelity", payload)
        self.assertNotIn("action", payload)


class ApiImagesMultipartFallbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_json_to_multipart_fallback_keeps_optional_fields(self):
        api_manager = load_api_manager()
        image_data = b"input-image"
        output_data = b"output-image"
        captured = {"requests": 0, "fields": {}}

        async def handle_generation(request):
            captured["requests"] += 1
            if request.content_type == "application/json":
                return web.json_response(
                    {"error": {"message": "use multipart/form-data"}},
                    status=415,
                )

            captured["content_type"] = request.headers.get("Content-Type", "")
            reader = await request.multipart()
            async for part in reader:
                if not part.filename:
                    captured["fields"][part.name] = await part.text()

            return web.json_response(
                {"data": [{"b64_json": base64.b64encode(output_data).decode()}]}
            )

        application = web.Application()
        application.router.add_post("/custom-image", handle_generation)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]

        manager = api_manager(
            {
                "interface_mode": "openai_image",
                "base_url": f"http://127.0.0.1:{port}/custom-image",
                "api_keys": "test-key",
                "timeout": 5,
            }
        )
        try:
            result = await manager.call_images_api(
                [image_data],
                "edit this image",
                "gpt-image-1",
                "test-key",
                f"http://127.0.0.1:{port}/custom-image",
                exact_endpoint=True,
                image_options={
                    "quality": "high",
                    "background": "transparent",
                    "output_format": "webp",
                    "output_compression": 80,
                    "input_fidelity": "high",
                    "partial_images": 2,
                },
            )
        finally:
            if manager._session and not manager._session.closed:
                await manager._session.close()
            await runner.cleanup()

        self.assertEqual(result, output_data)
        self.assertEqual(captured["requests"], 2)
        self.assertTrue(captured["content_type"].startswith("multipart/form-data;"))
        self.assertEqual(captured["fields"]["quality"], "high")
        self.assertEqual(captured["fields"]["background"], "transparent")
        self.assertEqual(captured["fields"]["output_format"], "webp")
        self.assertEqual(captured["fields"]["output_compression"], "80")
        self.assertEqual(captured["fields"]["input_fidelity"], "high")
        self.assertNotIn("partial_images", captured["fields"])


class ApiResponseRequestTest(unittest.IsolatedAsyncioTestCase):
    async def test_response_mode_posts_to_plural_endpoint(self):
        api_manager = load_api_manager()
        image_data = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
        )
        captured = {}

        async def handle_response(request):
            captured["path"] = request.path
            captured["authorization"] = request.headers.get("Authorization")
            captured["payload"] = await request.json()
            return web.json_response(
                {
                    "output": [
                        {
                            "type": "image_generation_call",
                            "result": base64.b64encode(image_data).decode(),
                        }
                    ]
                }
            )

        application = web.Application()
        application.router.add_post("/v1/responses", handle_response)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]

        manager = api_manager(
            {
                "interface_mode": "openai_response",
                "base_url": f"http://127.0.0.1:{port}/v1/responses",
                "api_keys": "test-key",
                "image_resolution": "1K",
                "timeout": 5,
            }
        )
        try:
            result = await manager.call_api(
                [image_data],
                "edit this image",
                "gpt-image-1",
                image_options={
                    "size": "1536x1024",
                    "quality": "high",
                    "background": "transparent",
                    "input_fidelity": "high",
                    "partial_images": 2,
                    "action": "edit",
                },
            )
        finally:
            if manager._session and not manager._session.closed:
                await manager._session.close()
            await runner.cleanup()

        self.assertEqual(result, image_data)
        self.assertEqual(captured["path"], "/v1/responses")
        self.assertEqual(captured["authorization"], "Bearer test-key")
        self.assertEqual(captured["payload"]["model"], "gpt-image-1")
        self.assertEqual(captured["payload"]["tools"][0]["type"], "image_generation")
        self.assertEqual(captured["payload"]["tools"][0]["size"], "1536x1024")
        self.assertEqual(captured["payload"]["tools"][0]["quality"], "high")
        self.assertEqual(
            captured["payload"]["tools"][0]["background"], "transparent"
        )
        self.assertEqual(
            captured["payload"]["tools"][0]["input_fidelity"], "high"
        )
        self.assertNotIn("partial_images", captured["payload"]["tools"][0])
        self.assertEqual(captured["payload"]["tools"][0]["action"], "edit")
        content = captured["payload"]["input"][0]["content"]
        self.assertEqual(content[0]["type"], "input_text")
        self.assertEqual(content[1]["type"], "input_image")


if __name__ == "__main__":
    unittest.main()
