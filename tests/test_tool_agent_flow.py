"""Media tools return facts without forcing the Agent's conversational reply."""

import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import test_video_plugin as behavior


class ToolAgentFlowTest(unittest.IsolatedAsyncioTestCase):
    setUp = behavior.VideoPluginTest.setUp
    asyncTearDown = behavior.VideoPluginTest.asyncTearDown

    def configure_outputs(self):
        self.plugin.api_mgr.call_api = AsyncMock(return_value=b"image")
        self.plugin.img_mgr.optimize_output_image = AsyncMock(side_effect=lambda raw: raw)
        self.plugin.audio_mgr.prepare = lambda *a, **k: None
        self.plugin.audio_mgr.generate = AsyncMock(return_value=behavior.audio_api.AudioResult(
            Path("output.wav"), "audio-task", "audio",
        ))
        self.plugin.context.llm_generate = AsyncMock()
        self.plugin.context.tool_loop_agent = AsyncMock()
        self.plugin.context.get_using_provider_async = AsyncMock()

    def calls(self):
        return (
            ("image", self.plugin.generate_image, {"prompt": "cat"}, behavior.Image),
            ("video", self.plugin.generate_video, {"prompt": "cat"}, behavior.Video),
            ("audio", self.plugin.generate_audio, {"text": "hello"}, behavior.Record),
        )

    def assert_no_extra_llm(self):
        self.plugin.context.llm_generate.assert_not_awaited()
        self.plugin.context.tool_loop_agent.assert_not_awaited()
        self.plugin.context.get_using_provider_async.assert_not_awaited()

    async def test_all_tools_return_one_factual_result_after_media_delivery(self):
        self.configure_outputs()
        for media, tool, arguments, component in self.calls():
            with self.subTest(media=media):
                event = behavior.Event([], "natural language request")
                states = []
                async for result in tool(event, use_message_media=False, **arguments):
                    self.assertEqual(len(event.sent), 1)
                    self.assertEqual(len(event.sent[0]), 1)
                    self.assertIsInstance(event.sent[0][0], component)
                    states.append(json.loads(result))
                self.assertEqual(states, [{
                    "status": "completed", "media_type": media, "delivery": "sent",
                }])
        self.plugin.api_mgr.call_api.assert_awaited_once()
        self.plugin.video_mgr.generate.assert_awaited_once()
        self.plugin.audio_mgr.generate.assert_awaited_once()
        self.assert_no_extra_llm()

    async def test_failed_tools_never_report_completed_or_send_a_completion_caption(self):
        self.configure_outputs()
        self.plugin.api_mgr.call_api.return_value = "upstream failure"
        self.plugin.video_mgr.generate.side_effect = behavior.router.VideoError("video failure")
        self.plugin.audio_mgr.generate.side_effect = behavior.router.VideoError("audio failure")
        for media, tool, arguments, _ in self.calls():
            with self.subTest(media=media):
                event = behavior.Event([])
                results = [part async for part in tool(
                    event, use_message_media=False, **arguments,
                )]
                self.assertEqual(len(results), 1)
                self.assertIsInstance(results[0], str)
                self.assertNotIn('"completed"', results[0])
                self.assertFalse(event.sent)
        self.assert_no_extra_llm()

    async def test_command_delivery_keeps_existing_summary_without_agent_status(self):
        self.configure_outputs()
        invocations = (
            (self.plugin._generate, ("cat", "自定义", "image", []), {"dispatch_result": True}),
            (self.plugin._generate_video, ("cat", "自定义", []), {"dispatch_result": True}),
            (self.plugin._generate_audio, (behavior.audio_router.AudioOptions(prompt="hello"), []), {}),
        )
        for method, arguments, kwargs in invocations:
            with self.subTest(method=method.__name__):
                event = behavior.Event([])
                self.assertEqual(await method(event, *arguments, **kwargs), [])
                self.assertIsInstance(event.sent[0][0], behavior.Plain)
                self.assertTrue(event.sent[0][0].text)
                self.assertNotIn('"delivery"', event.sent[0][0].text)
        self.assert_no_extra_llm()


if __name__ == "__main__":
    unittest.main()
