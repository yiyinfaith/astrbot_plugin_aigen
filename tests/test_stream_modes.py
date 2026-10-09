import base64
import unittest

from aiohttp import web

from test_api_manager_urls import load_api_manager


IMAGE_DATA = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


class StreamModeRequestTest(unittest.IsolatedAsyncioTestCase):
    async def _server(self, handler, route):
        application = web.Application()
        application.router.add_post(route, handler)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        return runner, site._server.sockets[0].getsockname()[1]

    async def test_openai_chat_uses_stream_flag(self):
        captured = {}

        async def handle(request):
            captured["payload"] = await request.json()
            return web.json_response(
                {
                    "choices": [
                        {
                            "message": {
                                "images": [
                                    {
                                        "url": "data:image/png;base64,"
                                        + base64.b64encode(IMAGE_DATA).decode()
                                    }
                                ]
                            }
                        }
                    ]
                }
            )

        runner, port = await self._server(handle, "/v1/chat/completions")
        manager_cls = load_api_manager()
        manager = manager_cls(
            {
                "interface_mode": "openai_chat",
                "base_url": f"http://127.0.0.1:{port}",
                "api_keys": "test-key",
                "use_stream": True,
                "timeout": 5,
            }
        )
        try:
            result = await manager.call_api([], "draw", "test-model")
        finally:
            if manager._session and not manager._session.closed:
                await manager._session.close()
            await runner.cleanup()

        self.assertEqual(result, IMAGE_DATA)
        self.assertTrue(captured["payload"]["stream"])

    async def test_openai_chat_does_not_send_gemini_extension_fields(self):
        captured = {}

        async def handle(request):
            captured["payload"] = await request.json()
            return web.json_response(
                {
                    "choices": [
                        {
                            "message": {
                                "images": [
                                    {
                                        "url": "data:image/png;base64,"
                                        + base64.b64encode(IMAGE_DATA).decode()
                                    }
                                ]
                            }
                        }
                    ]
                }
            )

        runner, port = await self._server(handle, "/v1/chat/completions")
        manager_cls = load_api_manager()
        manager = manager_cls(
            {
                "interface_mode": "openai_chat",
                "base_url": f"http://127.0.0.1:{port}",
                "api_keys": "test-key",
                "timeout": 5,
            }
        )
        try:
            result = await manager.call_api([], "draw", "gemini-3-pro-image-preview")
        finally:
            if manager._session and not manager._session.closed:
                await manager._session.close()
            await runner.cleanup()

        self.assertEqual(result, IMAGE_DATA)
        self.assertEqual(set(captured["payload"]), {"model", "messages"})

    async def test_gemini_official_requests_image_only_with_native_fields(self):
        captured = {}

        async def handle(request):
            captured["path"] = request.path
            captured["api_key"] = request.headers.get("x-goog-api-key")
            captured["payload"] = await request.json()
            return web.json_response(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "inlineData": {
                                            "mimeType": "image/png",
                                            "data": base64.b64encode(IMAGE_DATA).decode(),
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                }
            )

        runner, port = await self._server(
            handle, "/v1beta/models/gemini-2.5-flash-image:generateContent"
        )
        manager_cls = load_api_manager()
        manager = manager_cls(
            {
                "interface_mode": "gemini_official",
                "base_url": f"http://127.0.0.1:{port}",
                "api_keys": "test-key",
                "timeout": 5,
            }
        )
        try:
            result = await manager.call_api(
                [], "draw", "gemini-2.5-flash-image", image_options={"aspect_ratio": "16:9"}
            )
        finally:
            if manager._session and not manager._session.closed:
                await manager._session.close()
            await runner.cleanup()

        self.assertEqual(result, IMAGE_DATA)
        self.assertEqual(captured["path"], "/v1beta/models/gemini-2.5-flash-image:generateContent")
        self.assertEqual(captured["api_key"], "test-key")
        config = captured["payload"]["generationConfig"]
        self.assertEqual(config["responseModalities"], ["IMAGE"])
        self.assertEqual(config["imageConfig"]["aspectRatio"], "16:9")
        self.assertNotIn("image_config", captured["payload"])

    async def test_openai_response_stream_is_parsed(self):
        captured = {}

        async def handle(request):
            captured["payload"] = await request.json()
            body = (
                'data: {"item":{"type":"image_generation_call","result":"'
                + base64.b64encode(IMAGE_DATA).decode()
                + '"}}\n\n'
                "data: [DONE]\n\n"
            )
            return web.Response(text=body, content_type="text/event-stream")

        runner, port = await self._server(handle, "/v1/responses")
        manager_cls = load_api_manager()
        manager = manager_cls(
            {
                "interface_mode": "openai_response",
                "base_url": f"http://127.0.0.1:{port}",
                "api_keys": "test-key",
                "use_stream": True,
                "timeout": 5,
            }
        )
        try:
            result = await manager.call_api([], "draw", "test-model")
        finally:
            if manager._session and not manager._session.closed:
                await manager._session.close()
            await runner.cleanup()

        self.assertEqual(result, IMAGE_DATA)
        self.assertTrue(captured["payload"]["stream"])


if __name__ == "__main__":
    unittest.main()
