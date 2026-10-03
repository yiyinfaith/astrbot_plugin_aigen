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

    def test_response_alias_resolves_to_response_mode(self):
        manager = self.ApiManager({"interface_mode": "/v1/response"})
        self.assertEqual(manager._get_interface_mode(), "openai_response")


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
                [image_data], "edit this image", "gpt-image-2"
            )
        finally:
            if manager._session and not manager._session.closed:
                await manager._session.close()
            await runner.cleanup()

        self.assertEqual(result, image_data)
        self.assertEqual(captured["path"], "/v1/images/edits")
        self.assertEqual(captured["authorization"], "Bearer test-key")
        self.assertTrue(captured["content_type"].startswith("multipart/form-data;"))
        self.assertEqual(captured["fields"]["model"], "gpt-image-2")
        self.assertEqual(captured["fields"]["prompt"], "edit this image")
        self.assertEqual(captured["fields"]["n"], "1")
        self.assertEqual(captured["fields"]["size"], "1024x1024")
        self.assertEqual(len(captured["files"]), 1)
        self.assertEqual(captured["files"][0]["name"], "image")
        self.assertEqual(captured["files"][0]["content_type"], "image/png")
        self.assertEqual(captured["files"][0]["data"], image_data)


class ApiResponseRequestTest(unittest.IsolatedAsyncioTestCase):
    async def test_response_mode_posts_to_singular_endpoint(self):
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
        application.router.add_post("/v1/response", handle_response)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]

        manager = api_manager(
            {
                "interface_mode": "openai_response",
                "base_url": f"http://127.0.0.1:{port}/v1/response",
                "api_keys": "test-key",
                "image_resolution": "1K",
                "timeout": 5,
            }
        )
        try:
            result = await manager.call_api(
                [image_data], "edit this image", "test-model"
            )
        finally:
            if manager._session and not manager._session.closed:
                await manager._session.close()
            await runner.cleanup()

        self.assertEqual(result, image_data)
        self.assertEqual(captured["path"], "/v1/response")
        self.assertEqual(captured["authorization"], "Bearer test-key")
        self.assertEqual(captured["payload"]["model"], "test-model")
        self.assertEqual(captured["payload"]["tools"], [{"type": "image_generation"}])
        content = captured["payload"]["input"][0]["content"]
        self.assertEqual(content[0]["type"], "input_text")
        self.assertEqual(content[1]["type"], "input_image")


if __name__ == "__main__":
    unittest.main()
