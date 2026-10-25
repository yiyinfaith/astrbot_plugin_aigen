import asyncio
import base64
import importlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
from aiohttp import web
from test_api_manager_urls import PACKAGE, load_api_manager

ApiManager = load_api_manager()
utils = importlib.import_module(PACKAGE + ".utils")
IMAGE = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


class KeepaliveTest(unittest.IsolatedAsyncioTestCase):
    async def test_gemini_gateway_compat_preserves_image_parameters_and_reference(self):
        calls = []
        encoded = base64.b64encode(IMAGE).decode()

        async def handle(request):
            calls.append((request.path, await request.json()))
            self.assertEqual(request.headers.get("Authorization"), "Bearer secret")
            self.assertNotIn("x-goog-api-key", request.headers)
            delta = {
                "choices": [
                    {
                        "delta": {
                            "content": "![image](data:image/png;base64," + encoded + ")"
                        },
                        "finish_reason": "stop",
                    }
                ]
            }
            return web.Response(
                text=": ping\n\ndata: " + json.dumps(delta) + "\n\ndata: [DONE]\n\n",
                content_type="text/event-stream",
            )

        runner, base = await self.start(handle)
        manager = ApiManager(
            {
                "interface_mode": "gemini_official",
                "base_url": base,
                "api_keys": "secret",
                "use_stream": True,
                "gemini_stream_openai_compat": True,
                "timeout": 2,
            }
        )
        try:
            result = await manager.call_api(
                [IMAGE],
                "draw",
                "gemini-3.1-flash-image",
                image_options={
                    "aspect_ratio": "16:9",
                    "resolution": "2K",
                    "temperature": 0.4,
                    "seed": 7429,
                },
            )
            self.assertEqual(result, IMAGE)
            self.assertEqual(len(calls), 1)
            path, body = calls[0]
            self.assertEqual(path, "/v1/chat/completions")
            self.assertTrue(body["stream"])
            self.assertEqual(body["model"], "gemini-3.1-flash-image")
            self.assertEqual(
                body["extra_body"]["google"]["image_config"],
                {"aspect_ratio": "16:9", "image_size": "2K"},
            )
            self.assertEqual(body["temperature"], 0.4)
            self.assertEqual(body["seed"], 7429)
            self.assertEqual(
                body["messages"][0]["content"][1]["image_url"]["url"],
                "data:image/png;base64," + encoded,
            )
        finally:
            if manager._session:
                await manager._session.close()
            await runner.cleanup()

    async def test_fragmented_heartbeat_and_large_data_line(self):
        async def chunks():
            for part in (
                b": hea",
                b"rtbeat\r",
                b"\n\r\n",
                b"data: ",
                b"a" * 100000,
                b"b" * 100000,
                b"\n\n",
            ):
                yield part

        response = SimpleNamespace(
            headers={"Content-Type": "text/event-stream"},
            charset="utf-8",
            content=SimpleNamespace(iter_any=chunks),
        )
        self.assertEqual(
            await utils.read_api_response(response),
            "data: " + "a" * 100000 + "b" * 100000 + "\n",
        )

    async def start(self, handler):
        app = web.Application()
        app.router.add_post("/{tail:.*}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    async def test_heartbeats_keep_socket_read_alive_and_never_become_output(self):
        async def handle(request):
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            for _ in range(8):
                await response.write(b": keep-alive\r\n\r\n")
                await asyncio.sleep(0.02)
            await response.write(b'data: {"answer":"7429"}\n\n')
            await response.write_eof()
            return response

        runner, base = await self.start(handle)
        try:
            # Without real response traffic, this read deadline expires well
            # before the answer. The hard total deadline remains in effect.
            async with (
                aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=2, sock_read=0.09)
                ) as session,
                session.post(base) as response,
            ):
                with patch.object(utils, "MAX_API_RESPONSE_BYTES", 40):
                    text = await utils.read_api_response(response)
            self.assertEqual(text, 'data: {"answer":"7429"}\n')
        finally:
            await runner.cleanup()

    async def test_all_image_stream_routes_wait_through_heartbeats_with_one_post(self):
        encoded = base64.b64encode(IMAGE).decode()
        for mode in [
            "openai_chat",
            "gemini_official",
            "openai_response",
            "openai_image",
        ]:
            with self.subTest(mode=mode):
                calls = []

                async def handle(request, calls=calls, mode=mode):
                    calls.append((request.path, await request.json()))
                    response = web.StreamResponse(
                        headers={"Content-Type": "text/event-stream"}
                    )
                    await response.prepare(request)
                    for _ in range(4):
                        await response.write(b": keep-alive\n\n")
                        await asyncio.sleep(0.01)
                    if mode == "gemini_official":
                        data = {
                            "candidates": [
                                {
                                    "content": {
                                        "parts": [
                                            {
                                                "inlineData": {
                                                    "mimeType": "image/png",
                                                    "data": encoded,
                                                }
                                            }
                                        ]
                                    },
                                    "finishReason": "STOP",
                                }
                            ]
                        }
                    elif mode == "openai_response":
                        data = {
                            "response": {
                                "status": "completed",
                                "output": [
                                    {"type": "image_generation_call", "result": encoded}
                                ],
                            }
                        }
                    elif mode == "openai_image":
                        data = {"data": [{"b64_json": encoded}]}
                    else:
                        data = {
                            "choices": [
                                {
                                    "delta": {
                                        "images": [
                                            {"url": "data:image/png;base64," + encoded}
                                        ]
                                    },
                                    "finish_reason": "stop",
                                }
                            ]
                        }
                    await response.write(
                        ("data: " + json.dumps(data) + "\n\n").encode()
                    )
                    await response.write(b": final-heartbeat\n\ndata: [DONE]\n\n")
                    await response.write_eof()
                    return response

                runner, base = await self.start(handle)
                manager = ApiManager(
                    {
                        "interface_mode": mode,
                        "base_url": base,
                        "api_keys": "secret",
                        "use_stream": True,
                        "timeout": 2,
                    }
                )
                try:
                    result = await manager.call_api(
                        [], "draw", "gemini-3.1-flash-image"
                    )
                    self.assertEqual(result, IMAGE)
                    self.assertEqual(len(calls), 1)
                finally:
                    if manager._session:
                        await manager._session.close()
                    await runner.cleanup()

    async def test_heartbeats_do_not_disable_total_timeout_or_resubmit(self):
        calls = []

        async def handle(request):
            calls.append(await request.json())
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            try:
                for _ in range(30):
                    await response.write(b": keep-alive\n\n")
                    await asyncio.sleep(0.01)
            except (ConnectionResetError, aiohttp.ClientConnectionError):
                pass
            return response

        runner, base = await self.start(handle)
        manager = ApiManager(
            {
                "interface_mode": "openai_chat",
                "base_url": base,
                "api_keys": "secret",
                "use_stream": True,
                "timeout": 0.08,
            }
        )
        try:
            result = await manager.call_api([], "draw", "model")
            self.assertIsInstance(result, str)
            self.assertIn("超时", result)
            self.assertEqual(len(calls), 1)
        finally:
            if manager._session:
                await manager._session.close()
            await runner.cleanup()

    async def test_cancel_closes_stream_without_another_generation(self):
        started = asyncio.Event()
        disconnected = asyncio.Event()
        calls = []

        async def handle(request):
            calls.append(await request.json())
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            started.set()
            try:
                for _ in range(100):
                    await response.write(b": keep-alive\n\n")
                    await asyncio.sleep(0.01)
            except (ConnectionResetError, aiohttp.ClientConnectionError):
                disconnected.set()
            return response

        runner, base = await self.start(handle)
        manager = ApiManager(
            {
                "interface_mode": "openai_chat",
                "base_url": base,
                "api_keys": "secret",
                "use_stream": True,
                "timeout": 3,
            }
        )
        try:
            task = asyncio.create_task(manager.call_api([], "draw", "model"))
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(disconnected.wait(), 2)
            self.assertEqual(len(calls), 1)
        finally:
            if manager._session:
                await manager._session.close()
            await runner.cleanup()
