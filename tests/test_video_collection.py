"""Regression tests for the user-visible collection and submission contract."""

import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import test_video_plugin as behavior
from test_video_plugin import (
    Event,
    File,
    Image,
    Plain,
    Record,
    Video,
    api,
    router,
)

from config_manager import LEGACY_HELP_TEXT, migrate_config, schema_defaults


class VideoCollectionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        behavior.VideoPluginTest.setUp(self)
        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
                "utf-8"
            )
        )
        self.config = schema_defaults(schema)["video_settings"]
        for rule in self.config["routes"]:
            if not rule["model"]:
                rule["model"] = "provider-" + rule["__template_key"]
        self.config["duration"] = 1
        self.config["extra_prefix"] = ["做视频", "做视频 快速"]
        self.plugin.video_conf = self.config
        self.real = api.VideoApiManager(self.config, Path("unused"))
        self.plugin.video_mgr.prepare = Mock(wraps=self.real.prepare)

    async def receive(
        self, text="", chain=None, *, sender="99", group="group", origin="qq:group"
    ):
        event = Event(chain or [], text)
        event._sender_id = sender
        event.get_group_id = lambda: group
        event.unified_msg_origin = origin
        result = [x async for x in self.plugin.on_preset_request(event)]
        if text.startswith("/生视频") and not event.stopped:
            result = [x async for x in self.plugin.video_command(event)]
        return event, result

    async def test_collects_multiple_text_messages_in_order_and_only_submits_on_finish(
        self,
    ):
        start, result = await self.receive("/生视频")
        self.assertEqual(result, [])
        self.assertEqual(
            start.sent[0][0].text, "请发送材料，最后发送“/生成”开始生成视频"
        )
        for text in ("一只猫在雪地中跑", "镜头平稳跟随", "最后看向镜头"):
            event, result = await self.receive(text)
            self.assertEqual((event.sent, result), ([], []))
            self.assertTrue(event.stopped)
        self.plugin.video_mgr.generate.assert_not_awaited()
        finish, result = await self.receive("/生成")
        self.assertEqual(result, [])
        self.assertEqual(
            self.plugin.video_mgr.generate.await_args.args[0],
            "一只猫在雪地中跑 镜头平稳跟随 最后看向镜头",
        )
        self.assertEqual(finish.sent[0][0].text, "共收到0图片，0音频，0视频，生成中")
        self.assertEqual(len(finish.sent), 2)
        self.assertIsInstance(finish.sent[1][0], Video)
        self.assertFalse(self.plugin._video_collections)

    async def test_all_route_combinations_keep_every_media_source(self):
        cases = [
            ("一只猫", 0, 0, 0, "text_to_video"),
            ("人物跳舞", 1, 0, 0, "image_to_video"),
            ("镜头过渡", 2, 0, 0, "image_to_video"),
            ("首尾帧 镜头过渡", 2, 0, 0, "first_last_frame"),
            ("人物跳舞", 9, 0, 0, "image_to_video"),
            ("首帧 人物跳舞", 1, 0, 0, "first_frame"),
            ("尾帧 人物跳舞", 1, 0, 0, "last_frame"),
            ("人物唱歌", 0, 3, 0, "audio_to_video"),
            ("人物唱歌", 9, 3, 0, "image_audio_to_video"),
            ("动作迁移", 1, 0, 1, "motion_transfer"),
            ("音频同步", 1, 1, 0, "audio_sync"),
            ("视频重绘", 0, 0, 2, "video_to_video"),
            ("视频重绘", 2, 0, 2, "image_video_to_video"),
            ("视频重绘", 0, 4, 2, "audio_video_to_video"),
            ("视频重绘", 10, 4, 2, "image_audio_video_to_video"),
        ]
        for text, ni, na, nv, route in cases:
            with self.subTest(route=route, counts=(ni, na, nv)):
                self.plugin.video_mgr.generate.reset_mock()
                await self.receive("/生视频")
                await self.receive(text)
                for i in range(ni):
                    event, result = await self.receive(
                        chain=[Image(f"https://media.test/{i}.png")]
                    )
                    self.assertEqual((event.sent, result), ([], []))
                for i in range(na):
                    await self.receive(chain=[Record(f"https://media.test/{i}.wav")])
                for i in range(nv):
                    await self.receive(chain=[Video(f"https://media.test/{i}.mp4")])
                finish, result = await self.receive("/生成")
                self.assertEqual(result, [])
                self.assertEqual(
                    finish.sent[0][0].text,
                    f"共收到{ni}图片，{na}音频，{nv}视频，生成中",
                )
                self.plugin.video_mgr.generate.assert_awaited_once()
                args = self.plugin.video_mgr.generate.await_args.args
                self.assertEqual(tuple(map(len, args[1:4])), (ni, na, nv))
                self.assertEqual(
                    self.real.prepare(args[0], *args[1:4], options=args[5])[0], route
                )

    async def test_out_of_range_and_forbidden_prompt_fail_without_submission(self):
        for text, ni, na, nv in [
            ("人物跳舞", 10, 0, 0),
            ("唱歌", 0, 4, 0),
            ("首帧 跳舞", 2, 0, 0),
            ("尾帧 跳舞", 1, 1, 0),
            ("动作迁移", 2, 0, 1),
            ("动作迁移", 1, 1, 1),
            ("动作迁移 不能有文字", 1, 0, 1),
            ("音频同步", 1, 2, 0),
            ("音频同步 不能有文字", 1, 1, 0),
            ("", 1, 0, 0),
        ]:
            with self.subTest(text=text, counts=(ni, na, nv)):
                await self.receive(
                    "/生视频 " + text,
                    [Image(f"https://media.test/{i}.png") for i in range(ni)]
                    + [Record(f"https://media.test/{i}.wav") for i in range(na)]
                    + [Video(f"https://media.test/{i}.mp4") for i in range(nv)],
                )
                finish, result = await self.receive("/生成")
                self.assertEqual(finish.sent, [])
                self.assertTrue(result and isinstance(result[0][0], Plain))
                self.plugin.video_mgr.generate.assert_not_awaited()
                await self.receive("/取消")

    async def test_validation_failure_retains_material_for_missing_input(self):
        await self.receive("/生视频 音频同步", [Image("https://media.test/face.png")])
        _, result = await self.receive("/生成")
        self.assertIn("恰好", result[0][0].text)
        await self.receive(chain=[Record("https://media.test/voice.wav")])
        finish, result = await self.receive("/生成")
        self.assertEqual(result, [])
        self.assertIsInstance(finish.sent[-1][0], Video)

    async def test_legacy_flags_do_not_bypass_explicit_special_route_keywords(self):
        for flag in [
            "--frames",
            "--reference-mode first_last_frame",
            "--reference-mode first_frame",
            "--reference-mode last_frame",
            "--reference-mode motion_transfer",
            "--reference-mode audio_sync",
        ]:
            with self.subTest(flag=flag):
                await self.receive(
                    "/生视频 镜头过渡 " + flag,
                    [
                        Image("https://media.test/a.png"),
                        Image("https://media.test/b.png"),
                    ],
                )
                finish, result = await self.receive("/生成")
                self.assertIn("关键词", result[0][0].text)
                self.assertEqual(finish.sent, [])
                self.plugin.video_mgr.generate.assert_not_awaited()
                await self.receive("/取消")

    async def test_image_audio_and_video_files_are_collected_without_text(self):
        await self.receive("/生视频 视频重绘")
        for name in ("photo.png", "voice.mp3", "clip.mp4"):
            event, result = await self.receive(
                chain=[File(name, url="https://qq.test/" + name)]
            )
            self.assertEqual((event.sent, result), ([], []))
        finish, _ = await self.receive("/生成")
        self.assertEqual(finish.sent[0][0].text, "共收到1图片，1音频，1视频，生成中")

    async def test_identical_sources_are_counted_as_separate_supplied_parameters(self):
        await self.receive(
            "/生视频 镜头过渡",
            [Image("https://media.test/a.png"), Image("https://media.test/a.png")],
        )
        finish, result = await self.receive("/生成")
        self.assertEqual(result, [])
        self.assertEqual(finish.sent[0][0].text, "共收到2图片，0音频，0视频，生成中")

    async def test_sender_group_and_platform_sessions_are_isolated(self):
        scopes = [
            ("99", "group1", "qq:group1"),
            ("100", "group1", "qq:group1"),
            ("99", "group2", "qq:group2"),
            ("99", "group1", "telegram:group1"),
        ]
        for i, (sender, group, origin) in enumerate(scopes):
            await self.receive("/生视频", sender=sender, group=group, origin=origin)
            await self.receive(
                f"独立提示词{i}", sender=sender, group=group, origin=origin
            )
        for i, (sender, group, origin) in enumerate(scopes):
            await self.receive("/生成", sender=sender, group=group, origin=origin)
            self.assertEqual(
                self.plugin.video_mgr.generate.await_args.args[0], f"独立提示词{i}"
            )

    async def test_finish_waits_for_upload_and_repeated_finish_does_not_submit_twice(
        self,
    ):
        await self.receive("/生视频 人物转身")
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.plugin._video_command_inputs

        async def slow_inputs(event, *args):
            if event.message_str == "upload":
                entered.set()
                await release.wait()
            return await original(event, *args)

        self.plugin._video_command_inputs = slow_inputs
        upload = asyncio.create_task(
            self.receive("upload", [Image("https://media.test/a.png")])
        )
        await entered.wait()
        first = asyncio.create_task(self.receive("/生成"))
        second = asyncio.create_task(self.receive("/生成"))
        await asyncio.sleep(0)
        self.plugin.video_mgr.generate.assert_not_awaited()
        release.set()
        await asyncio.gather(upload, first, second)
        self.plugin.video_mgr.generate.assert_awaited_once()
        self.assertEqual(
            self.plugin.video_mgr.generate.await_args.args[1],
            ["https://media.test/a.png"],
        )

    async def test_prefix_lists_longest_match_restart_and_cancel(self):
        start, result = await self.receive("/做视频 快速 猫")
        self.assertEqual(result, [])
        self.assertTrue(start.sent)
        await self.receive("犬")
        await self.receive("/做视频 新的提示词")
        await self.receive("/生成")
        self.assertEqual(
            self.plugin.video_mgr.generate.await_args.args[0], "新的提示词"
        )
        await self.receive("/做视频")
        await self.receive("/取消")
        _, result = await self.receive("/生成")
        self.assertIn("没有正在收集", result[0][0].text)

    async def test_expired_session_and_bad_file_fail_without_progress(self):
        await self.receive("/生视频 猫")
        for collection in self.plugin._video_collections.values():
            collection["updated"] -= 1801
        _, result = await self.receive("/生成")
        self.assertIn("30 分钟", result[0][0].text)
        await self.receive("/生视频 猫")
        event, result = await self.receive(
            chain=[File("unsupported.zip", url="https://qq.test/a.zip")]
        )
        self.assertEqual((event.sent, result), ([], []))
        finish, result = await self.receive("/生成")
        self.assertIn("unsupported.zip", result[0][0].text)
        self.assertEqual(finish.sent, [])
        self.plugin.video_mgr.generate.assert_not_awaited()

    async def test_file_token_and_adapter_omitted_file_are_resolved(self):
        async def action(name, **params):
            if name == "get_msg":
                return {
                    "data": {
                        "message": [
                            {
                                "type": "file",
                                "data": {"file_name": "face.png", "file_id": "face"},
                            }
                        ]
                    }
                }
            return {"data": {"url": "https://qq.test/download?id=" + params["file_id"]}}

        await self.receive("/生视频 人物转身")
        event = Event([])
        event.bot = SimpleNamespace(call_action=AsyncMock(side_effect=action))
        event.unified_msg_origin = "qq:group"
        event.message_obj = SimpleNamespace(
            message_id="123",
            raw_message={
                "message": [
                    {
                        "type": "file",
                        "data": {"file_name": "face.png", "file_id": "face"},
                    }
                ]
            },
        )
        self.assertEqual([x async for x in self.plugin.on_preset_request(event)], [])
        await self.receive("/生成")
        self.assertEqual(
            self.plugin.video_mgr.generate.await_args.args[1],
            ["https://qq.test/download?id=face"],
        )
        self.assertEqual(event.get_messages(), [])

    async def test_qq_voice_uses_astrbot_wav_conversion(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "voice.wav"
            path.write_bytes(b"RIFF" + b"\0" * 8 + b"WAVE" + b"\0" * 32)
            voice = Record("opaque-qq-voice")
            voice.convert_to_file_path = AsyncMock(return_value=str(path))
            await self.receive("/生视频 人物唱歌")
            await self.receive(chain=[voice])
            await self.receive("/生成")
            voice.convert_to_file_path.assert_awaited_once()
            self.assertTrue(
                self.plugin.video_mgr.generate.await_args.args[2][0].startswith(
                    "data:audio/wav;base64,"
                )
            )

    async def test_hot_reload_during_upload_does_not_submit_stale_material(self):
        await self.receive("/生视频 人物转身")
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.plugin._video_command_inputs

        async def slow_inputs(event, *args):
            entered.set()
            await release.wait()
            return await original(event, *args)

        self.plugin._video_command_inputs = slow_inputs
        self.plugin._cleanup_task = None
        self.plugin.api_mgr = SimpleNamespace(_session=None)
        upload = asyncio.create_task(
            self.receive(chain=[Image("https://media.test/a.png")])
        )
        await entered.wait()
        await self.plugin.terminate()
        release.set()
        event, result = await upload
        self.assertEqual((event.sent, result), ([], []))
        self.plugin.video_mgr.generate.assert_not_awaited()

    async def test_failed_usage_storage_still_delivers_the_completed_video(self):
        self.plugin.data_mgr.record_usage.side_effect = OSError("disk full")
        await self.receive("/生视频 猫在雪地上走")
        finish, result = await self.receive("/生成")
        self.assertEqual(result, [])
        self.assertIsInstance(finish.sent[-1][0], Video)


class VideoConfigUpgradeTest(unittest.TestCase):
    def test_only_old_default_help_is_updated_custom_help_is_preserved(self):
        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
                "utf-8"
            )
        )
        for text in (LEGACY_HELP_TEXT, "我的自定义帮助，保留全文"):
            config = schema_defaults(schema)
            config["_config_layout_version"] = 1
            config["global_settings"]["help_text"] = text
            with tempfile.TemporaryDirectory() as tmp:
                migrate_config(config, Path(tmp))
            self.assertEqual(
                config["global_settings"]["help_text"],
                schema_defaults(schema)["global_settings"]["help_text"]
                if text == LEGACY_HELP_TEXT
                else text,
            )

    def test_upgrade_preserves_presets_keys_and_custom_routes(self):
        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
                "utf-8"
            )
        )
        config = schema_defaults(schema)
        config["_config_layout_version"] = 1
        config["image_settings"].update(
            extra_prefix="我的生图",
            prompt_list=[f"预设{i}:完整提示词" for i in range(112)],
            api_keys="private-test-key",
        )
        config["video_settings"]["extra_prefix"] = "我的视频"
        config["video_settings"]["routes"] = [
            {
                "__template_key": "image_video_to_video",
                "model": "wan2.2-animate-move",
                "interface_mode": "dashscope",
                "resolution": "464p",
            },
            {
                "__template_key": "video_to_video",
                "model": "my-provider",
                "interface_mode": "seedance",
            },
        ]
        before = copy.deepcopy(config)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(migrate_config(config, Path(tmp)))
            snapshot = json.loads(next(Path(tmp).glob("*.json")).read_text("utf-8"))
            self.assertEqual(snapshot, before)
            self.assertFalse(migrate_config(config, Path(tmp)))
            self.assertEqual(len(list(Path(tmp).glob("*.json"))), 1)
        self.assertEqual(
            config["image_settings"]["prompt_list"],
            before["image_settings"]["prompt_list"],
        )
        self.assertEqual(config["image_settings"]["api_keys"], "private-test-key")
        self.assertEqual(config["image_settings"]["extra_prefix"], ["我的生图"])
        self.assertEqual(config["video_settings"]["extra_prefix"], ["我的视频"])
        routes = {r["__template_key"]: r for r in config["video_settings"]["routes"]}
        self.assertEqual(routes["image_video_to_video"]["model"], "")
        self.assertEqual(routes["motion_transfer"]["model"], "wan2.2-animate-move")
        self.assertEqual(routes["motion_transfer"]["interface_mode"], "minimax")
        self.assertEqual(routes["video_to_video"]["interface_mode"], "seedance")
        self.assertEqual(
            routes["audio_sync"]["model"], "minimax_h3_image_audio_to_video"
        )

    def test_default_routes_all_use_minimax_and_empty_generic_image_video_model(self):
        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
                "utf-8"
            )
        )
        routes = schema_defaults(schema)["video_settings"]["routes"]
        self.assertTrue(all(r["interface_mode"] == "minimax" for r in routes))
        self.assertEqual(
            next(r for r in routes if r["__template_key"] == "image_video_to_video")[
                "model"
            ],
            "",
        )
        self.assertEqual(set(router.ROUTES), {r["__template_key"] for r in routes})
