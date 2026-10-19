"""All image artifacts and non-destructive collection error recovery."""

import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from aiohttp import web
from PIL import Image as PILImage

import test_audit_regressions as audit
import test_audio_generation as audio
import test_billing as financial
import test_video_collection as collection
import test_video_plugin as behavior


def pictures():
    result = []
    for color in ("red", "blue"):
        output = io.BytesIO()
        PILImage.new("RGB", (2, 2), color).save(output, format="PNG")
        result.append(output.getvalue())
    return result


class ImageBatchTest(unittest.IsolatedAsyncioTestCase):
    async def test_all_protocols_keep_every_image_in_provider_order(self):
        raw = pictures()
        encoded = [base64.b64encode(item).decode() for item in raw]
        urls = ["data:image/png;base64," + item for item in encoded]
        cases = (
            ("openai_image", {"data": [{"b64_json": item} for item in encoded]}),
            ("openai_response", {"output": [
                {"id": str(i), "type": "image_generation_call", "result": item}
                for i, item in enumerate(encoded)
            ]}),
            ("openai_chat", {"choices": [{"message": {"images": urls}}]}),
            ("openai_chat", {"choices": [{"message": {"content": [
                {"type": "image_url", "image_url": {"url": item}} for item in urls
            ]}}]}),
            ("openai_chat", {"choices": [{"message": {"content":
                "\n".join(f"![image]({item})" for item in urls)
            }}]}),
            ("gemini_official", {"candidates": [{"content": {"parts": [
                {"inlineData": {"mimeType": "image/png", "data": item}}
                for item in encoded
            ]}}]}),
        )
        for mode, response in cases:
            with self.subTest(mode=mode, response=str(response)[:70]):
                calls = []

                async def handler(request):
                    calls.append(request.method)
                    return web.json_response(response)

                base = await audit.ImageHttpAuditTest.server(self, handler)
                manager = audit.ImageHttpAuditTest.manager(self, base, mode)
                result = await manager.call_api([], "cat", "gpt-image-1")
                self.assertIsInstance(result, behavior.image_api.ImageBatch)
                self.assertEqual(list(result.images), raw)
                self.assertEqual(calls, ["POST"])

    async def test_sse_final_outputs_are_complete_without_duplicate_previews(self):
        raw = pictures()
        encoded = [base64.b64encode(item).decode() for item in raw]
        items = [{"id": str(i), "type": "image_generation_call", "result": item}
                 for i, item in enumerate(encoded)]
        cases = (
            ("openai_response", [
                {"type": "response.output_item.done", "item": items[0]},
                {"type": "response.output_item.done", "item": items[0]},
                {"type": "response.output_item.done", "item": items[1]},
                {"type": "response.completed", "response": {"output": items}},
            ]),
            ("openai_image", [
                {"type": "image_generation.partial_image", "b64_json": encoded[1]},
                *[{"type": "image_generation.completed", "b64_json": item}
                  for item in encoded],
            ]),
            ("gemini_official", [
                {"candidates": [{"content": {"parts": [
                    {"inlineData": {"mimeType": "image/png", "data": item}}
                ]}}]} for item in encoded
            ]),
        )
        for mode, events in cases:
            with self.subTest(mode=mode):
                async def handler(request):
                    body = "".join("data: " + json.dumps(item) + "\n\n" for item in events)
                    return web.Response(text=body + "data: [DONE]\n\n",
                                        content_type="text/event-stream")
                manager = audit.ImageHttpAuditTest.manager(
                    self, await audit.ImageHttpAuditTest.server(self, handler), mode
                )
                result = await manager.call_api([], "cat", "gpt-image-1")
                self.assertEqual(list(result.images), raw)

    async def test_batch_is_one_order_and_uses_actual_image_count(self):
        raw = pictures()
        for expression, charge in (
            ('u("image_count") * 0.2', 400000),
            ("fixed(1)", 1000000),
            ("p * 2 + c * 8", 2800),
        ):
            with self.subTest(expression=expression), tempfile.TemporaryDirectory() as directory:
                calls = []
                async def handler(request):
                    calls.append(await request.json())
                    return web.json_response({
                        "data": [{"b64_json": base64.b64encode(item).decode()} for item in raw],
                        "usage": {"input_tokens": 1000, "output_tokens": 100},
                    })
                manager = audit.ImageHttpAuditTest.manager(
                    self, await audit.ImageHttpAuditTest.server(self, handler), "openai_image"
                )
                service = financial.b.BillingService(Path(directory) / "billing.sqlite3", {
                    "billing_prices": [{"model": "m", "expression": expression}]
                })
                event = financial.Identity()
                owner = service.owner(event)
                service.store.redeem(owner, service.store.create_cards("admin", "10", 1)[0])
                result = await financial.b.billed_call(
                    service, event, "image", {"model": "m", "n": 3}, {},
                    lambda: manager.call_api([], "cat", "m", image_options={"n": 3}),
                )
                self.assertEqual(len(result.images), 2)
                self.assertEqual(service.store.balance(owner)[0], 10000000 - charge)
                total, orders = service.store.listing("orders", owner)
                self.assertEqual((len(calls), total, orders[0]["charged"]), (1, 1, charge))

    async def test_download_failure_does_not_discard_surrounding_images(self):
        urls = [f"https://media.test/{i}.png" for i in range(3)]
        raw = pictures()
        async def handler(request):
            return web.json_response({"data": [{"url": url} for url in urls]})
        manager = audit.ImageHttpAuditTest.manager(
            self, await audit.ImageHttpAuditTest.server(self, handler), "openai_image"
        )
        manager._download_result_image = AsyncMock(side_effect=[
            behavior.image_api.ImageResult(raw[0], urls[0]),
            behavior.image_api.ImageDownloadError("offline", urls[1]),
            behavior.image_api.ImageResult(raw[1], urls[2]),
        ])
        result = await manager.call_api([], "cat", "m")
        self.assertEqual(result.images[0], raw[0])
        self.assertEqual(result.images[1].url, urls[1])
        self.assertEqual(result.images[2], raw[1])
        self.assertEqual(manager._download_result_image.await_count, 3)

    async def test_invalid_batch_member_does_not_charge_or_hide_other_members(self):
        raw = pictures()[0]
        async def handler(request):
            return web.json_response({"data": [
                {"b64_json": base64.b64encode(item).decode()}
                for item in (b"<html>error</html>", raw)
            ]})
        manager = audit.ImageHttpAuditTest.manager(
            self, await audit.ImageHttpAuditTest.server(self, handler), "openai_image"
        )
        with tempfile.TemporaryDirectory() as directory:
            service = financial.b.BillingService(Path(directory) / "billing.sqlite3", {
                "billing_prices": [{"model": "m", "expression": 'u("image_count") * 0.2'}]
            })
            event = financial.Identity()
            owner = service.owner(event)
            service.store.redeem(owner, service.store.create_cards("admin", "10", 1)[0])
            result = await financial.b.billed_call(
                service, event, "image", {"model": "m", "n": 2}, {},
                lambda: manager.call_api([], "cat", "m", image_options={"n": 2}),
            )
            self.assertIsInstance(result.images[0], str)
            self.assertEqual(result.images[1], raw)
            self.assertEqual(service.store.balance(owner)[0], 9800000)


class BatchDeliveryTest(unittest.IsolatedAsyncioTestCase):
    setUp = behavior.VideoPluginTest.setUp

    async def asyncTearDown(self):
        await self.plugin.terminate()

    async def test_tool_sends_all_images_without_success_text(self):
        raw = pictures()
        self.plugin.api_mgr.call_api = AsyncMock(return_value=behavior.image_api.ImageBatch(raw))
        self.plugin.img_mgr.optimize_output_image = AsyncMock(side_effect=lambda value: value)
        event = behavior.Event([])
        result = [part async for part in self.plugin.generate_image(
            event, "cat", n=2, use_message_media=False,
        )]
        self.assertEqual(len(result), 1)
        self.assertEqual(json.loads(result[0]), {
            "status": "completed", "media_type": "image", "delivery": "sent",
        })
        self.assertEqual([chain[0].url for chain in event.sent], raw)
        self.assertTrue(all(len(chain) == 1 for chain in event.sent))
        self.plugin.data_mgr.record_usage.assert_awaited_once()

    async def test_command_keeps_all_images_and_one_success_caption(self):
        raw = pictures()
        batch = behavior.image_api.ImageBatch([
            raw[0], behavior.image_api.ImageDownloadError("offline", "https://media.test/2.png"), raw[1]
        ])
        self.plugin.api_mgr.call_api = AsyncMock(return_value=batch)
        self.plugin.img_mgr.optimize_output_image = AsyncMock(side_effect=lambda value: value)
        event = behavior.Event([])
        result = await self.plugin._generate(event, "cat", "自定义", "image", [],
                                             show_progress=False, dispatch_result=True)
        self.assertEqual(result, [])
        images = [item.url for chain in event.sent for item in chain if isinstance(item, behavior.Image)]
        text = [item.text for chain in event.sent for item in chain if isinstance(item, behavior.Plain)]
        self.assertEqual(images, raw)
        self.assertEqual(sum("生成成功" in item for item in text), 1)
        self.assertTrue(any("https://media.test/2.png" in item for item in text))


class ClearFailedVideoTest(unittest.IsolatedAsyncioTestCase):
    setUp = collection.VideoCollectionTest.setUp
    receive = collection.VideoCollectionTest.receive

    async def asyncTearDown(self):
        await self.plugin.terminate()

    async def test_clear_failure_preserves_materials_parameters_owner_and_deadline(self):
        await self.receive("/生视频")
        await self.receive("cat", chain=[behavior.Image("https://media.test/ref.png")])
        await self.receive(chain=[behavior.File("bad.zip", url="https://media.test/bad.zip")])
        active = next(iter(self.plugin._video_collections.values()))
        active["parameters"] = {"duration": 5}
        deadline = active["deadline"]
        _, result = await self.receive("/生成")
        self.assertIn("/清除失败项", result[0][0].text)
        await self.receive("/清除失败项", sender="someone-else")
        self.assertTrue(active["errors"])
        _, result = await self.receive("/清除失败项")
        self.assertIn("已清除 1 项", result[0][0].text)
        self.assertEqual(active["errors"], [])
        self.assertEqual(active["deadline"], deadline)
        self.assertEqual(active["parameters"], {"duration": 5})
        self.assertEqual(active["text"], ["cat"])
        self.assertEqual(active["images"], ["https://media.test/ref.png"])
        await self.receive("/生成")
        self.plugin.video_mgr.generate.assert_awaited_once()


class ClearFailedAudioTest(unittest.IsolatedAsyncioTestCase):
    setUp = audio.AudioCollectionTest.setUp
    receive = audio.AudioCollectionTest.receive

    async def asyncTearDown(self):
        await self.plugin.terminate()

    async def test_audio_can_clear_failed_file_then_continue_type_selection(self):
        await self.receive("/生音频 朗读")
        await self.receive("hello")
        await self.receive(chain=[behavior.Record("https://media.test/ref.wav")])
        await self.receive(chain=[behavior.File("bad.zip", url="https://media.test/bad.zip")])
        active = next(iter(self.plugin._video_collections.values()))
        deadline = active["deadline"]
        _, result = await self.receive("/生成")
        self.assertIn("/清除失败项", result[0][0].text)
        await self.receive("/清除失败项")
        self.assertEqual((active["text"], active["audios"], active["deadline"]),
                         (["hello"], ["https://media.test/ref.wav"], deadline))
        _, result = await self.receive("/生成")
        self.assertIn("音色克隆", result[0][0].text)
        await self.receive("音色克隆")
        self.plugin.audio_mgr.generate.assert_awaited_once()
