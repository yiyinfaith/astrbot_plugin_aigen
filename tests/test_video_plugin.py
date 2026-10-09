"""Behavior checks using the plugin's actual methods and event/media stubs."""

import ast
import asyncio
import importlib
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from unittest.mock import AsyncMock

from test_api_manager_urls import PACKAGE, load_api_manager
from test_image_input_order import At, FakeEvent, File, Image, ImageManager, Reply

load_api_manager()
router = importlib.import_module(PACKAGE + ".video_router")
api = importlib.import_module(PACKAGE + ".video_api_manager")
inputs = importlib.import_module(PACKAGE + ".video_inputs")
utils = importlib.import_module(PACKAGE + ".utils")


class Plain:
    def __init__(self, text):
        self.text = text


class Video:
    def __init__(self, file):
        self.file = file

    fromFileSystem = staticmethod(lambda path: Video(path))


class Record:
    def __init__(self, file):
        self.file = file


class Event(FakeEvent):
    def __init__(self, chain, text=""):
        super().__init__(chain, text=text)
        self.sent = []
        self.stopped = False

    def chain_result(self, chain):
        return chain

    async def send(self, result):
        self.sent.append(result)

    def stop_event(self):
        self.stopped = True

    def get_group_id(self):
        return "group"


def plugin_class():
    path = Path(__file__).resolve().parents[1] / "main.py"
    tree = ast.parse(path.read_text("utf-8"))
    tree.body = [
        ast.ImportFrom(
            module="__future__", names=[ast.alias(name="annotations")], level=0
        ),
        *[node for node in tree.body if isinstance(node, ast.ClassDef)],
    ]
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            node.decorator_list = [
                d
                for d in node.decorator_list
                if isinstance(d, ast.Name) and d.id in {"staticmethod", "classmethod"}
            ]
    tree.body[1].bases = []
    ast.fix_missing_locations(tree)
    namespace = {
        "__name__": "plugin_behavior",
        "asyncio": asyncio,
        "re": re,
        "Path": Path,
        "datetime": datetime,
        "timezone": timezone,
        "monotonic": monotonic,
        "Plain": Plain,
        "Video": Video,
        "Image": Image,
        "VideoError": router.VideoError,
        "VideoOptions": router.VideoOptions,
        "parse_video_command": router.parse_video_command,
        "collect_media": inputs.collect_media,
        "resolve_media": inputs.resolve_media,
        "strip_media_text": inputs.strip_media_text,
        "text_media": inputs.text_media,
        "norm_id": utils.norm_id,
        "match_keyword_in_text": utils.match_keyword_in_text,
        "extract_image_urls_from_text": utils.extract_image_urls_from_text,
        "logger": __import__("logging").getLogger("behavior"),
        "aiohttp": __import__("aiohttp"),
    }
    exec(compile(tree, str(path), "exec"), namespace)  # noqa: S102
    return namespace["ImageGeneratorPlugin"]


class VideoPluginTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        cls = plugin_class()
        self.plugin = cls.__new__(cls)
        self.plugin.context = object()
        self.plugin.conf = {"model": "image", "llm_cooldown_seconds": 0}
        self.plugin.video_conf = {"model": "video", "llm_cooldown_seconds": 0}
        self.plugin._llm_last_call = {}
        self.plugin.data_mgr = type(
            "Data",
            (),
            {
                "record_usage": AsyncMock(),
                "prompt_map": {},
                "video_prompt_map": {"舞动": "舞蹈"},
            },
        )()
        self.plugin.img_mgr = ImageManager({})
        self.plugin.img_mgr.load_bytes = AsyncMock(
            side_effect=lambda src: str(src).encode()
        )
        self.plugin.img_mgr.get_avatar = AsyncMock(
            side_effect=lambda uid: str(uid).encode()
        )
        self.plugin.video_mgr = type(
            "Manager",
            (),
            {
                "generate": AsyncMock(
                    return_value=api.VideoResult(
                        Path("generated.mp4"), "task", "routed-model"
                    )
                ),
                "prepare": lambda *args: None,
            },
        )()

    async def test_llm_tool_success_sends_video_only_and_reads_quoted_media(self):
        event = Event(
            [Reply([Image("https://i.test/ref.png"), Record("https://m.test/ref.wav")])]
        )
        result = [
            reply
            async for reply in self.plugin.generate_video(event, "说话", duration=1)
        ]
        self.assertEqual(event.sent, [])
        self.assertEqual(len(result[0]), 1)
        self.assertIsInstance(result[0][0], Video)
        args = self.plugin.video_mgr.generate.await_args.args
        self.assertEqual(
            args[1:4], (["https://i.test/ref.png"], ["https://m.test/ref.wav"], [])
        )

    async def test_tool_uses_message_media_public_urls(self):
        event = Event(
            [
                Image("https://i.test/message.png"),
                Record("https://m.test/message.wav"),
                Video("https://m.test/ref.mp4"),
            ]
        )
        [
            reply
            async for reply in self.plugin.generate_video(
                event,
                "run",
            )
        ]
        args = self.plugin.video_mgr.generate.await_args.args
        self.assertEqual(
            args[1:4],
            (
                ["https://i.test/message.png"],
                ["https://m.test/message.wav"],
                ["https://m.test/ref.mp4"],
            ),
        )

    async def test_tool_reads_qq_file_components_without_touching_file_property(self):
        event = Event(
            [
                File("photo.png", url="https://qq.test/photo.png"),
                File("voice.mp3", url="https://qq.test/voice.mp3"),
                File("clip.mp4", url="https://qq.test/clip.mp4"),
            ]
        )
        [reply async for reply in self.plugin.generate_video(event, "animate")]
        args = self.plugin.video_mgr.generate.await_args.args
        self.assertEqual(
            args[1:4],
            (
                ["https://qq.test/photo.png"],
                ["https://qq.test/voice.mp3"],
                ["https://qq.test/clip.mp4"],
            ),
        )

    async def test_llm_can_request_text_only_even_with_message_attachments(self):
        event = Event([Image("ignored"), Video("https://m.test/ref.mp4")])
        [
            reply
            async for reply in self.plugin.generate_video(
                event, "cloud", use_message_media=False
            )
        ]
        self.assertEqual(
            self.plugin.video_mgr.generate.await_args.args[1:4], ([], [], [])
        )

    async def test_command_reads_quoted_audio_and_ignores_unquoted_audio(self):
        event = Event(
            [
                Record("https://m.test/unquoted.wav"),
                Reply([Record("https://m.test/quoted.wav")]),
            ]
        )
        _, _, audios, videos = await self.plugin._video_command_inputs(
            event, "dance", "自定义"
        )
        self.assertEqual(
            (audios, videos),
            (["https://m.test/quoted.wav"], []),
        )

    async def test_command_accepts_image_file_and_quoted_audio_video_files(self):
        event = Event(
            [
                File("photo.png", url="https://qq.test/photo.png"),
                File("voice.mp3", url="https://qq.test/unquoted.mp3"),
                Reply(
                    [
                        File("voice.mp3", url="https://qq.test/quoted.mp3"),
                        File("clip.mp4", url="https://qq.test/quoted.mp4"),
                    ]
                ),
            ]
        )
        _, images, audios, videos = await self.plugin._video_command_inputs(
            event, "animate", "自定义"
        )
        self.assertEqual(images, ["https://qq.test/photo.png"])
        self.assertEqual(audios, ["https://qq.test/quoted.mp3"])
        self.assertEqual(videos, ["https://qq.test/quoted.mp4"])

    async def test_command_rejects_media_urls_in_text(self):
        url = "https://m.test/ref.wav?signature=abc&expires=123"
        event = Event([Plain(f'--audio "{url}"')])
        with self.assertRaisesRegex(router.VideoError, "不接受媒体 URL"):
            await self.plugin._video_command_inputs(
                event, f'dance --audio "{url}"', "自定义"
            )
        self.plugin.video_mgr.generate.assert_not_awaited()

    async def test_command_can_have_no_prompt_for_motion_transfer(self):
        event = Event(
            [
                Image("https://i.test/person.png"),
                Reply([Video("https://m.test/motion.mp4")]),
            ],
            "/生视频",
        )
        result = [reply async for reply in self.plugin.video_command(event)]
        self.assertTrue(event.stopped)
        self.assertIsInstance(result[0][0], Video)
        self.assertEqual(
            self.plugin.video_mgr.generate.await_args.args[3],
            ["https://m.test/motion.mp4"],
        )

    async def test_command_option_error_does_not_submit_or_show_progress(self):
        event = Event([], "/生视频 猫 --duration 1.2")
        result = [reply async for reply in self.plugin.video_command(event)]
        self.assertIn("整数", result[0][0].text)
        self.plugin.video_mgr.generate.assert_not_awaited()
        self.assertEqual(event.sent, [])

    async def test_multimedia_order_and_mention_avatar_are_preserved(self):
        event = Event(
            [
                Image("https://i.test/sent.png"),
                At("2"),
                Reply(
                    [
                        Image("https://i.test/quoted.png"),
                        Video("https://m.test/one.mp4"),
                        Record("https://m.test/audio.wav"),
                    ]
                ),
                Video("https://m.test/two.mp4"),
            ]
        )
        options, images, audios, videos = await self.plugin._video_command_inputs(
            event, "跳舞 --duration 1", "自定义"
        )
        self.assertEqual(
            images,
            [
                "https://i.test/sent.png",
                "https://q1.qlogo.cn/g?b=qq&nk=2&s=640",
                "https://i.test/quoted.png",
            ],
        )
        self.assertEqual(videos, ["https://m.test/one.mp4"])
        self.assertEqual(audios, ["https://m.test/audio.wav"])
        self.assertEqual(options.duration, 1)

    async def test_video_preset_keeps_single_image_priority_and_prefix_switch(self):
        event = Event(
            [
                Image("https://i.test/sent.png"),
                At("2"),
                Reply([Image("https://i.test/quoted.png")]),
            ],
            "@2舞动",
        )
        result = [reply async for reply in self.plugin.on_preset_request(event)]
        self.assertEqual(
            self.plugin.video_mgr.generate.await_args.args[1],
            ["https://i.test/quoted.png"],
        )
        self.assertTrue(result)
        self.plugin.video_conf["preset_need_prefix"] = True
        self.plugin.video_mgr.generate.reset_mock()
        result = [
            reply async for reply in self.plugin.on_preset_request(Event([], "舞动"))
        ]
        self.assertEqual(result, [])
        self.plugin.video_mgr.generate.assert_not_awaited()

    async def test_media_data_url_type_and_local_magic_validation(self):
        with self.assertRaises(router.VideoError):
            await inputs.resolve_media("data:video/mp4;base64,YQ==", "audio")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "voice.silk"
            path.write_bytes(b"#!SILK_V3")
            with self.assertRaisesRegex(router.VideoError, "SILK"):
                await inputs.resolve_media(str(path), "audio")
