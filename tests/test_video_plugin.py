"""Behavior checks using the plugin's actual methods and event/media stubs."""

import ast
import asyncio
import base64
import importlib
import io
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image as PILImage
from test_api_manager_urls import PACKAGE, load_api_manager
from test_image_input_order import At, FakeEvent, File, Image, ImageManager, Reply

Image.fromBytes = staticmethod(lambda data: Image(data))

load_api_manager()
image_api = importlib.import_module(PACKAGE + ".api_manager")
router = importlib.import_module(PACKAGE + ".video_router")
api = importlib.import_module(PACKAGE + ".video_api_manager")
inputs = importlib.import_module(PACKAGE + ".video_inputs")
utils = importlib.import_module(PACKAGE + ".utils")
audio_api = importlib.import_module(PACKAGE + ".audio_api_manager")
audio_router = importlib.import_module(PACKAGE + ".audio_router")
image_router = importlib.import_module(PACKAGE + ".image_router")
capabilities = importlib.import_module(PACKAGE + ".generation_capabilities")


class Plain:
    def __init__(self, text):
        self.text = text


class Node:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class MessageChain(list):
    def message(self, text):
        self.append(Plain(text))
        return self


class Video:
    def __init__(self, file):
        self.file = file

    fromFileSystem = staticmethod(lambda path: Video(path))


class Record:
    def __init__(self, file, url=""):
        self.file = file
        self.url = url


class Event(FakeEvent):
    def __init__(self, chain, text=""):
        super().__init__(chain, text=text)
        self.sent = []
        self.stopped = False
        self.fail_video_send = False
        self.fail_video_send_always = False
        self.fail_image_send_always = False
        self.image_send_attempts = 0
        self.video_send_attempts = 0

    def chain_result(self, chain):
        return chain

    async def send(self, result):
        if (
            (self.fail_video_send or self.fail_video_send_always)
            and result
            and isinstance(result[0], Video)
        ):
            self.video_send_attempts += 1
            if not self.fail_video_send_always:
                self.fail_video_send = False
            raise RuntimeError("video adapter rejected local file")
        if self.fail_image_send_always and result and isinstance(result[0], Image):
            self.image_send_attempts += 1
            raise RuntimeError("image adapter rejected local file")
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
        "Node": Node,
        "BILLING_COMMANDS": importlib.import_module(
            PACKAGE + ".billing_commands"
        ).COMMANDS,
        "handle_command": importlib.import_module(
            PACKAGE + ".billing_commands"
        ).handle_command,
        "BillingError": importlib.import_module(PACKAGE + ".billing").BillingError,
        "billed_call": importlib.import_module(PACKAGE + ".billing").billed_call,
        "AUDIO": capabilities.AUDIO,
        "IMAGE": capabilities.IMAGE,
        "supplied": capabilities.supplied,
        "validate_options": capabilities.validate_options,
        "build_menu": capabilities.build_menu,
        "apply_menu_selection": capabilities.apply_menu_selection,
        "configured_capabilities": capabilities.configured_capabilities,
        "select_route": router.select_route,
        "select_audio_route": audio_router.select_audio_route,
        "MessageChain": MessageChain,
        "Video": Video,
        "Image": Image,
        "Record": Record,
        "File": File,
        "AudioOptions": audio_router.AudioOptions,
        "AudioDownloadError": audio_api.AudioDownloadError,
        "AUDIO_TYPES": audio_router.AUDIO_TYPES,
        "parse_audio_materials": audio_router.parse_audio_materials,
        "select_image_route": image_router.select_image_route,
        "VideoError": router.VideoError,
        "VideoDownloadError": api.VideoDownloadError,
        "ImageDownloadError": image_api.ImageDownloadError,
        "ApiManager": image_api.ApiManager,
        "video_params": api.video_params,
        "detect_aspect_ratio_from_image": importlib.import_module(
            PACKAGE + ".generation_params"
        ).detect_aspect_ratio_from_image,
        "resolve_image_generation_params": importlib.import_module(
            PACKAGE + ".generation_params"
        ).resolve_image_generation_params,
        "openai_size_for_generation": importlib.import_module(
            PACKAGE + ".generation_params"
        ).openai_size_for_generation,
        "VideoOptions": router.VideoOptions,
        "parse_video_command": router.parse_video_command,
        "collect_media": inputs.collect_media,
        "normalize_media_event": inputs.normalize_media_event,
        "resolve_media": inputs.resolve_media,
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
        self.plugin.context = SimpleNamespace(send_message=AsyncMock(return_value=True))
        self.plugin.conf = {"model": "image", "llm_cooldown_seconds": 0}
        self.plugin.video_conf = {"model": "video", "llm_cooldown_seconds": 0}
        self.plugin.audio_conf = {"model": "audio", "llm_cooldown_seconds": 0}
        self.plugin.audio_mgr = SimpleNamespace(_session=None)
        self.plugin._llm_last_call = {}
        self.plugin._video_collections = {}
        self.plugin._video_busy = set()
        self.plugin._video_generation_tasks = set()
        self.plugin._video_collection_tasks = set()
        self.plugin._cleanup_task = None
        self.plugin.api_mgr = SimpleNamespace(_session=None)
        self.plugin._terminating = False
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
                "prepare": lambda *args, **kwargs: None,
            },
        )()

    async def asyncTearDown(self):
        await self.plugin.terminate()

    async def test_concurrent_llm_calls_reserve_cooldown_before_media_reads(self):
        self.plugin.video_conf["llm_cooldown_seconds"] = 60
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_images(*args, **kwargs):
            entered.set()
            await release.wait()
            return []

        self.plugin.img_mgr.extract_image_sources_from_event = slow_images

        async def call():
            return [x async for x in self.plugin.generate_video(Event([]), prompt="猫")]

        first = asyncio.create_task(call())
        await entered.wait()
        second = await asyncio.wait_for(call(), 1)
        self.assertIn("冷却中", second[0])
        self.plugin.video_mgr.generate.assert_not_awaited()
        release.set()
        await first
        self.plugin.video_mgr.generate.assert_awaited_once()

    async def test_hot_reload_cancels_llm_call_while_it_is_reading_media(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_images(*args, **kwargs):
            entered.set()
            await release.wait()
            return []

        self.plugin.img_mgr.extract_image_sources_from_event = slow_images

        async def call():
            return [x async for x in self.plugin.generate_video(Event([]), prompt="猫")]

        task = asyncio.create_task(call())
        await entered.wait()
        self.assertIn(task, self.plugin._video_generation_tasks)
        await self.plugin.terminate()
        self.assertTrue(task.cancelled())
        self.assertFalse(self.plugin._video_generation_tasks)
        self.plugin.video_mgr.generate.assert_not_awaited()
        result = await call()
        self.assertIn("正在重载", result[0])
        self.plugin.video_mgr.generate.assert_not_awaited()

    async def test_invalid_llm_options_release_cooldown_reservation(self):
        self.plugin.video_conf["llm_cooldown_seconds"] = 60
        result = [
            x
            async for x in self.plugin.generate_video(
                Event([]), prompt="猫", duration=1.2
            )
        ]
        self.assertIn("整数", result[0])
        self.assertFalse(self.plugin._llm_last_call)
        self.assertFalse(self.plugin._video_generation_tasks)
        result = [
            x
            async for x in self.plugin.generate_video(
                Event([]), prompt="猫", duration=1
            )
        ]
        self.assertEqual(result, [])
        self.plugin.video_mgr.generate.assert_awaited_once()

    async def test_llm_tool_success_sends_video_only_and_reads_quoted_media(self):
        event = Event(
            [Reply([Image("https://i.test/ref.png"), Record("https://m.test/ref.wav")])]
        )
        result = [
            reply
            async for reply in self.plugin.generate_video(event, "说话", duration=1)
        ]
        self.assertEqual(result, [])
        self.assertEqual(len(event.sent), 1)
        self.assertTrue(any(isinstance(chain[0], Video) for chain in event.sent))
        self.assertTrue(all(isinstance(chain[0], Video) for chain in event.sent))
        args = self.plugin.video_mgr.generate.await_args.args
        self.assertEqual(
            args[1:4], (["https://i.test/ref.png"], ["https://m.test/ref.wav"], [])
        )

    async def test_qq_cached_images_reach_real_video_request_builder(self):
        self.plugin.video_conf.update(
            interface_mode="openai_video",
            reference_mode="first_frame",
            routes={
                "image_to_video": {
                    "model": "minimax_h3_lightx2v_v5",
                    "interface_mode": "openai_video",
                },
                "first_last_frame": {
                    "model": "minimax_h3_lightx2v",
                    "interface_mode": "openai_video",
                },
            },
            duration=1,
            resolution="480p",
            aspect_ratio="16:9",
        )
        out = io.BytesIO()
        PILImage.new("RGB", (2, 2), "red").save(out, format="PNG")
        expected = "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "QQ cached.png"
            path.write_bytes(out.getvalue())
            current = Image("")
            current.file = str(path)
            quoted = Image("")
            quoted.file = path.as_uri()
            manager = api.VideoApiManager(self.plugin.video_conf, Path(tmp))
            cases = (
                [current],
                [Reply([quoted])],
                [current, Reply([quoted])],
                [File("photo.png", file_=str(path))],
            )
            for chain in cases:
                with self.subTest(components=[type(s).__name__ for s in chain]):
                    (
                        options,
                        images,
                        audios,
                        videos,
                    ) = await self.plugin._video_command_inputs(
                        Event(chain), "让人物跳舞 1秒", "自定义"
                    )
                    route, prepared = manager.prepare(
                        options.prompt, images, audios, videos, options=options
                    )
                    self.assertEqual(route, "image_to_video")
                    _, _, body, _ = prepared.build_request(
                        options.prompt,
                        [api.image_source(i) for i in images],
                        audios,
                        videos,
                    )
                    refs = body["input_reference"]
                    if isinstance(refs, dict):
                        refs = [refs]
                    self.assertEqual(refs, [{"image_url": expected}] * len(chain))

    async def test_tools_return_failures_as_llm_text_not_direct_chat_results(self):
        event = Event([Image("https://qq.test/image.png")])
        self.plugin.video_mgr.generate.side_effect = router.VideoError(
            "upstream failed"
        )
        result = [x async for x in self.plugin.generate_video(event, "人物跳舞")]
        self.assertEqual(result, ["upstream failed"])
        self.assertEqual(event.sent, [])

    async def test_command_default_and_explicit_tool_frame_choice(self):
        event = Event([Image("https://qq.test/image.png")])
        self.plugin.video_conf["reference_mode"] = "first_frame"
        for prompt in ("", "人物跳舞", "图生视频"):
            options, *_ = await self.plugin._video_command_inputs(
                event, prompt, "自定义"
            )
            self.assertEqual(options.reference_mode, "auto")
        options, *_ = await self.plugin._video_command_inputs(
            event, "人物跳舞 --frames", "自定义"
        )
        self.assertEqual(options.reference_mode, "first_last_frame")
        [
            x
            async for x in self.plugin.generate_video(
                event, "固定起始画面", reference_mode="first_frame"
            )
        ]
        self.assertEqual(
            self.plugin.video_mgr.generate.await_args.args[5].reference_mode,
            "first_frame",
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

    async def test_qq_record_and_video_tokens_resolve_to_adapter_urls(self):
        async def call_action(name, **kwargs):
            return {"data": {"url": f"https://qq.test/{name}.media"}}

        event = Event([Record("record-token"), Video("video-token")])
        event.bot = SimpleNamespace(call_action=AsyncMock(side_effect=call_action))
        audios, videos = await inputs.collect_media(event, self.plugin.img_mgr)
        self.assertEqual(audios, ["https://qq.test/get_record.media"])
        self.assertEqual(videos, ["https://qq.test/get_video.media"])
        self.assertEqual(
            [call.args[0] for call in event.bot.call_action.await_args_list],
            ["get_record", "get_video"],
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

    async def test_command_reads_both_current_and_quoted_audio(self):
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
            (["https://m.test/unquoted.wav", "https://m.test/quoted.wav"], []),
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
        self.assertEqual(
            audios, ["https://qq.test/unquoted.mp3", "https://qq.test/quoted.mp3"]
        )
        self.assertEqual(videos, ["https://qq.test/quoted.mp4"])

    async def test_empty_reply_fetches_onebot_files_with_file_ids(self):
        async def action(name, **params):
            if name == "get_msg":
                return {
                    "data": {
                        "message": [
                            {
                                "type": "file",
                                "data": {
                                    "file_name": "photo.png",
                                    "file_id": "photo",
                                    "file_size": 100,
                                },
                            },
                            {
                                "type": "file",
                                "data": {"file_name": "voice.mp3", "file_id": "voice"},
                            },
                            {
                                "type": "file",
                                "data": {"file_name": "clip.mp4", "file_id": "clip"},
                            },
                        ]
                    }
                }
            if name == "get_file":
                raise RuntimeError("unsupported API")
            return {"data": {"url": "https://qq.test/download?id=" + params["file_id"]}}

        event = Event([Reply(reply_id="123")])
        event.bot = SimpleNamespace(call_action=AsyncMock(side_effect=action))
        _, images, audios, videos = await self.plugin._video_command_inputs(
            event, "animate", "自定义"
        )
        self.assertEqual(images, ["https://qq.test/download?id=photo"])
        self.assertEqual(audios, ["https://qq.test/download?id=voice"])
        self.assertEqual(videos, ["https://qq.test/download?id=clip"])

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
            "/生视频 动作迁移",
        )
        result = [reply async for reply in self.plugin.video_command(event)]
        self.assertTrue(event.stopped)
        self.assertEqual(result, [])
        self.plugin.video_mgr.generate.assert_not_awaited()
        finish = Event([], "/生成")
        self.assertEqual([x async for x in self.plugin.on_preset_request(finish)], [])
        self.assertTrue(any(isinstance(chain[0], Video) for chain in finish.sent))
        self.assertEqual(
            self.plugin.video_mgr.generate.await_args.args[3],
            ["https://m.test/motion.mp4"],
        )

    async def test_command_option_error_does_not_submit_or_show_progress(self):
        event = Event([], "/生视频 猫 --duration 1.2")
        result = [reply async for reply in self.plugin.video_command(event)]
        self.assertEqual(result, [])
        finish = Event([], "/生成")
        result = [reply async for reply in self.plugin.on_preset_request(finish)]
        self.assertIn("整数", result[0][0].text)
        self.plugin.video_mgr.generate.assert_not_awaited()
        self.assertEqual(
            event.sent[0][0].text,
            "请发送材料，录入阶段发送“/设置”可设置详细参数，最后发送“/生成”开始生成视频，120秒超时（整个过程共用）",
        )

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
                "https://i.test/quoted.png",
            ],
        )
        self.assertEqual(videos, ["https://m.test/one.mp4", "https://m.test/two.mp4"])
        self.assertEqual(audios, ["https://m.test/audio.wav"])
        self.assertEqual(options.duration, 1)

    async def test_video_preset_collects_all_images_and_respects_prefix_switch(self):
        event = Event(
            [
                Image("https://i.test/sent.png"),
                At("2"),
                Reply([Image("https://i.test/quoted.png")]),
            ],
            "@2舞动",
        )
        result = [reply async for reply in self.plugin.on_preset_request(event)]
        self.plugin.video_mgr.generate.assert_not_awaited()
        finish = Event([], "/生成")
        self.assertEqual([x async for x in self.plugin.on_preset_request(finish)], [])
        self.assertEqual(
            self.plugin.video_mgr.generate.await_args.args[1],
            ["https://i.test/sent.png", "https://i.test/quoted.png"],
        )
        self.assertEqual(result, [])
        self.assertTrue(any(isinstance(chain[0], Video) for chain in finish.sent))
        self.plugin.video_conf["preset_need_prefix"] = True
        self.plugin.video_mgr.generate.reset_mock()
        result = [
            reply async for reply in self.plugin.on_preset_request(Event([], "舞动"))
        ]
        self.assertEqual(result, [])
        self.plugin.video_mgr.generate.assert_not_awaited()

    async def test_local_video_send_falls_back_to_upstream_url_with_expiry(self):
        event = Event([])
        event.fail_video_send_always = True
        self.plugin.video_conf["video_delivery_retries"] = 2
        self.plugin.video_mgr.generate.return_value = api.VideoResult(
            Path("generated.mp4"),
            "task",
            "routed-model",
            "https://media.test/video.mp4",
            "2026-10-10 12:00:00 UTC",
        )
        result = await self.plugin._generate_video(
            event,
            "cloud",
            "自定义",
            [],
            [],
            [],
            dispatch_result=True,
        )
        self.assertEqual(result, [])
        self.assertEqual(len(event.sent), 2)  # progress notice + URL fallback
        self.assertIn("https://media.test/video.mp4", event.sent[-1][0].text)
        self.assertIn("2026-10-10 12:00:00 UTC", event.sent[-1][0].text)
        self.assertEqual(event.video_send_attempts, 2)

    async def test_local_image_send_retries_before_url_fallback(self):
        event = Event([])
        event.fail_image_send_always = True
        self.plugin.conf["image_delivery_retries"] = 2
        result = await self.plugin._deliver_image_result(
            event,
            b"image-bytes",
            "https://media.test/image.png",
            "2026-10-10 12:00:00 UTC",
            include_result_text=False,
        )
        self.assertEqual(result, [])
        self.assertEqual(event.image_send_attempts, 2)
        self.assertEqual(len(event.sent), 1)
        self.assertIn("https://media.test/image.png", event.sent[0][0].text)
        self.assertIn("2026-10-10 12:00:00 UTC", event.sent[0][0].text)

    async def test_llm_video_fallback_is_returned_to_bot_without_plugin_text(self):
        event = Event([])
        event.fail_video_send_always = True
        self.plugin.video_conf["video_delivery_retries"] = 2
        self.plugin.video_mgr.generate.return_value = api.VideoResult(
            Path("generated.mp4"),
            "task",
            "routed-model",
            "https://media.test/tool-fallback.mp4",
            "2026-10-10 12:00:00 UTC",
        )
        result = [
            reply
            async for reply in self.plugin.generate_video(event, "cloud", duration=1)
        ]
        self.assertEqual(len(event.sent), 0)
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], str)
        self.assertIn("https://media.test/tool-fallback.mp4", result[0])

    async def test_llm_image_fallback_is_returned_to_bot_without_plugin_text(self):
        event = Event([])
        event.fail_image_send_always = True
        self.plugin.conf["image_delivery_retries"] = 2
        result = await self.plugin._deliver_image_result(
            event,
            b"image-bytes",
            "https://media.test/tool-fallback.png",
            "2026-10-10 12:00:00 UTC",
            include_result_text=False,
            send_fallback_message=False,
        )
        self.assertEqual(len(event.sent), 0)
        self.assertEqual(len(result), 1)
        self.assertIn("https://media.test/tool-fallback.png", result[0].text)

    async def test_llm_image_success_sends_media_without_plugin_text(self):
        event = Event([])
        result = await self.plugin._deliver_image_result(
            event,
            b"image-bytes",
            include_result_text=False,
        )
        self.assertEqual(result, [])
        self.assertEqual(len(event.sent), 1)
        self.assertEqual(len(event.sent[0]), 1)
        self.assertIsInstance(event.sent[0][0], Image)

    async def test_generate_image_tool_sends_only_media(self):
        self.plugin.conf.update({"enable_llm_tool": True, "model": "image"})
        self.plugin.api_mgr = SimpleNamespace(
            call_api=AsyncMock(return_value=b"image-bytes")
        )
        self.plugin.img_mgr.optimize_output_image = AsyncMock(
            side_effect=lambda raw: raw
        )
        event = Event([Image("https://media.test/input.png")])
        result = [
            reply
            async for reply in self.plugin.generate_image(event, "make it cinematic")
        ]
        self.assertEqual(result, [])
        self.assertEqual(len(event.sent), 1)
        self.assertEqual(len(event.sent[0]), 1)
        self.assertIsInstance(event.sent[0][0], Image)

    async def test_generate_image_tool_returns_fallback_to_bot_without_sending_text(
        self,
    ):
        self.plugin.conf.update({"enable_llm_tool": True, "model": "image"})
        self.plugin.api_mgr = SimpleNamespace(
            call_api=AsyncMock(
                side_effect=image_api.ImageDownloadError(
                    "download failed",
                    "https://media.test/tool-image-fallback.png",
                    "2026-10-10 12:00:00 UTC",
                )
            )
        )
        self.plugin.img_mgr.optimize_output_image = AsyncMock(
            side_effect=lambda raw: raw
        )
        event = Event([Image("https://media.test/input.png")])
        result = [
            reply async for reply in self.plugin.generate_image(event, "edit this")
        ]
        self.assertEqual(len(event.sent), 0)
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], str)
        self.assertIn("https://media.test/tool-image-fallback.png", result[0])

    async def test_local_download_failure_falls_back_to_upstream_url(self):
        event = Event([])
        self.plugin.video_mgr.generate.side_effect = api.VideoDownloadError(
            "视频已完成但下载失败", "https://media.test/download-fallback.mp4", ""
        )
        result = await self.plugin._generate_video(
            event,
            "cloud",
            "自定义",
            [],
            [],
            [],
            dispatch_result=True,
        )
        self.assertEqual(result, [])
        self.assertEqual(len(event.sent), 2)  # progress notice + one fallback message
        self.assertIn(
            "https://media.test/download-fallback.mp4", event.sent[-1][0].text
        )
        self.assertIn("AutoDL 未返回明确时间", event.sent[-1][0].text)

    async def test_media_data_url_type_and_local_magic_validation(self):
        with self.assertRaises(router.VideoError):
            await inputs.resolve_media("data:video/mp4;base64,YQ==", "audio")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "voice.silk"
            path.write_bytes(b"#!SILK_V3")
            with self.assertRaisesRegex(router.VideoError, "SILK"):
                await inputs.resolve_media(str(path), "audio")

    async def test_quoted_audio_video_file_uris_keep_absolute_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "QQ voice.wav"
            audio.write_bytes(b"RIFF" + b"\x00" * 4 + b"WAVEfmt " + b"\x00" * 24)
            video = Path(tmp) / "QQ video.mp4"
            video.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 24)
            for segments in (
                [Record(audio.as_uri()), Video(video.as_uri())],
                [
                    File(audio.name, file_=audio.as_uri()),
                    File(video.name, file_=video.as_uri()),
                ],
            ):
                audios, videos = await inputs.collect_media(
                    Event([Reply(segments)]), self.plugin.img_mgr, quoted_only=True
                )
                self.assertTrue(audios[0].startswith("data:audio/wav;base64,"))
                self.assertTrue(videos[0].startswith("data:video/mp4;base64,"))
