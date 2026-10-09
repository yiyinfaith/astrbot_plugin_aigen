import copy
import json
import unittest
from pathlib import Path

from config_manager import schema_defaults
from video_router import VideoError, VideoOptions, parse_video_command, select_route


class RoutingTest(unittest.TestCase):
    def setUp(self):
        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
                "utf-8"
            )
        )
        self.config = schema_defaults(schema)["video_settings"]
        self.config["routes"] = {r["__template_key"]: r for r in self.config["routes"]}

    def test_media_combinations_route_to_independently_configurable_models(self):
        cases = [
            (0, 0, 0, "text_to_video"),
            (1, 0, 0, "image_to_video"),
            (0, 1, 0, "audio_to_video"),
            (1, 1, 0, "image_audio_to_video"),
            (0, 0, 1, "video_to_video"),
            (1, 0, 1, "image_video_to_video"),
            (0, 1, 1, "audio_video_to_video"),
            (1, 1, 1, "image_audio_video_to_video"),
        ]
        for i, a, v, expected in cases:
            with self.subTest(expected):
                self.config["routes"][expected].update(
                    model="my-" + expected, interface_mode="seedance"
                )
                route, result = select_route(
                    self.config, ["img"] * i, ["audio"] * a, ["video"] * v, 1
                )
                self.assertEqual(route, expected)
                self.assertEqual(result["model"], "my-" + expected)
                self.assertEqual(result["interface_mode"], "seedance")

    def test_missing_video_model_rejects_instead_of_dropping_reference(self):
        with self.assertRaisesRegex(VideoError, "视频生视频.*未提交"):
            select_route(self.config, [], [], ["video"], 1)

    def test_long_duration_selects_configured_long_model_without_changing_duration(
        self,
    ):
        _, result = select_route(
            self.config, ["img"], ["audio"], [], 12, {"duration": 12}
        )
        self.assertEqual(result["model"], "minimax_h3_image_audio_to_video_v2_15s")
        self.assertEqual(result["duration"], 12)

    def test_two_reference_images_are_not_implicitly_first_last_frames(self):
        route, _ = select_route(self.config, ["a", "b"], [], [], 1)
        self.assertEqual(route, "image_to_video")
        route, result = select_route(
            self.config, ["a", "b"], [], [], 1, {"reference_mode": "first_last_frame"}
        )
        self.assertEqual(route, "first_last_frame")
        self.assertEqual(result["model"], "minimax_h3_lightx2v")
        with self.assertRaisesRegex(VideoError, "恰好 2 张"):
            select_route(
                self.config, ["a"], [], [], 1, {"reference_mode": "first_last_frame"}
            )

    def test_per_task_overrides_do_not_mutate_shared_config(self):
        before = copy.deepcopy(self.config)
        _, result = select_route(
            self.config,
            ["image"],
            [],
            ["video"],
            1,
            {"resolution": "832p", "aspect_ratio": "9:16"},
        )
        self.assertEqual(result["resolution"], "832p")
        self.assertEqual(self.config, before)
        _, next_result = select_route(self.config, [], [], [], 1)
        self.assertEqual(next_result["resolution"], "480p")

    def test_fixed_model_can_disable_automatic_routing(self):
        self.config.update(auto_route=False, model="fixed")
        _, result = select_route(self.config, [], [], ["video"], 1)
        self.assertEqual(result["model"], "fixed")

    def test_command_options_preserve_text_quotes_queries_and_media_order(self):
        command = '让猫说 "hello world" --duration 1 --image https://media/a.png?x=1&y=2 --image https://media/b.png --audio "C:/my audio.wav" --ratio 9:16 --frames'
        p = parse_video_command(command)
        self.assertEqual(p.prompt, '让猫说 "hello world"')
        self.assertEqual(
            p.images, ["https://media/a.png?x=1&y=2", "https://media/b.png"]
        )
        self.assertEqual(p.audios, ["C:/my audio.wav"])
        self.assertEqual(p.reference_mode, "first_last_frame")
        self.assertEqual(p.overrides()["aspect_ratio"], "9:16")

    def test_malformed_flags_are_actionable(self):
        for text in (
            "猫 --duration 1.5",
            "猫 --duration -1",
            "--duration",
            "--audio --video url",
            "--unknown x",
            "--reference-mode bad",
            "--seed -3",
        ):
            with self.subTest(text), self.assertRaises(VideoError):
                parse_video_command(text)
        for duration in (True, 1.5, -1):
            with self.assertRaises(VideoError):
                VideoOptions(duration=duration).overrides()
