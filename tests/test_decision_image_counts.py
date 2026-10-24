"""Decision's logical image count must work on Gemini and native batch APIs."""
import json
import unittest
from unittest.mock import AsyncMock

import test_video_plugin as behavior


class DecisionImageCountTest(unittest.IsolatedAsyncioTestCase):
    setUp = behavior.VideoPluginTest.setUp

    async def asyncTearDown(self):
        await self.plugin.terminate()

    async def test_gemini_one_image_does_not_forward_unsupported_n(self):
        self.plugin.conf.update(interface_mode="gemini_official", model="gemini-3.1-flash-image", llm_cooldown_seconds=60)
        self.plugin.api_mgr.call_api = AsyncMock(return_value=b"image-bytes")
        self.plugin.img_mgr.optimize_output_image = AsyncMock(side_effect=lambda raw: raw)
        event = behavior.Event([])
        results = [item async for item in self.plugin.generate_image(event, "red triangle", n=1, use_message_media=False)]
        self.assertEqual(json.loads(results[0])["status"], "completed")
        self.assertEqual(len(event.sent), 1)
        self.assertEqual(self.plugin.api_mgr.call_api.await_count, 1)
        self.assertNotIn("n", self.plugin.api_mgr.call_api.call_args.kwargs.get("image_options", {}))

    async def test_gemini_multiple_images_are_individually_generated_with_one_cooldown(self):
        self.plugin.conf.update(interface_mode="gemini_official", model="gemini-3.1-flash-image", llm_cooldown_seconds=60)
        self.plugin.api_mgr.call_api = AsyncMock(return_value=b"image-bytes")
        self.plugin.img_mgr.optimize_output_image = AsyncMock(side_effect=lambda raw: raw)
        event = behavior.Event([])
        results = [item async for item in self.plugin.generate_image(event, "red triangle", n=3, use_message_media=False)]
        self.assertEqual(json.loads(results[0])["status"], "completed")
        self.assertEqual(len(event.sent), 3)
        self.assertEqual(self.plugin.api_mgr.call_api.await_count, 3)
        self.assertEqual(event.get_extra("aigen_completed_media_units"), 3)
        denied = [item async for item in self.plugin.generate_image(event, "again", n=1, use_message_media=False)]
        self.assertIn("冷却", denied[0])
        self.assertEqual(self.plugin.api_mgr.call_api.await_count, 3)
        self.assertEqual(event.get_extra("aigen_completed_media_units"), 0)

    async def test_partial_failure_keeps_completed_units_and_stops_remaining_submissions(self):
        self.plugin.conf.update(interface_mode="gemini_official", model="gemini-3.1-flash-image")
        self.plugin.api_mgr.call_api = AsyncMock(side_effect=[b"image-bytes", ValueError("failed"), b"must-not-submit"])
        self.plugin.img_mgr.optimize_output_image = AsyncMock(side_effect=lambda raw: raw)
        event = behavior.Event([])
        results = [item async for item in self.plugin.generate_image(event, "red triangle", n=3, use_message_media=False)]
        self.assertTrue(results)
        self.assertEqual(len(event.sent), 1)
        self.assertEqual(self.plugin.api_mgr.call_api.await_count, 2)
        self.assertEqual(event.get_extra("aigen_completed_media_units"), 1)
