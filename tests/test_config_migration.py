"""Tests for the current grouped configuration contract."""

import copy
import json
import unittest
from pathlib import Path

from config_manager import media_attempts, media_config, schema_defaults

ROOT = Path(__file__).resolve().parents[1]


class ConfigSchemaTest(unittest.TestCase):
    def setUp(self):
        self.schema = json.loads((ROOT / "_conf_schema.json").read_text("utf-8"))
        self.defaults = schema_defaults(self.schema)

    def test_schema_exposes_only_current_grouped_sections(self):
        self.assertEqual(
            set(self.schema),
            {"global_settings", "image_settings", "video_settings", "audio_settings"},
        )
        for group in self.defaults.values():
            self.assertIsInstance(group, dict)

    def test_new_install_has_no_builtin_presets_or_legacy_fields(self):
        for kind in ("image", "video", "audio"):
            settings = self.defaults[f"{kind}_settings"]
            self.assertEqual(settings["prompt_list"], [])
            self.assertNotIn("result_image_download_retries", settings)
            self.assertNotIn("result_video_download_retries", settings)
            self.assertNotIn("download_retries", settings)
        self.assertNotIn("billing_prices", self.defaults["global_settings"])
        self.assertEqual(self.defaults["global_settings"]["help_text"], "")
        self.assertNotIn("_config_layout_version", self.defaults)
        self.assertNotIn("custom_create_path", self.defaults["audio_settings"])

    def test_schema_defaults_are_independent(self):
        first = schema_defaults(self.schema)
        second = schema_defaults(self.schema)
        first["image_settings"]["prompt_list"].append("local-only")
        first["video_settings"]["routes"][0]["model"] = "local-only"
        self.assertEqual(second["image_settings"]["prompt_list"], [])
        self.assertNotEqual(
            second["video_settings"]["routes"][0]["model"], "local-only"
        )

    def test_media_config_merges_global_and_media_settings(self):
        config = copy.deepcopy(self.defaults)
        config["global_settings"]["timeout"] = 42
        config["video_settings"]["model"] = "video-model"
        self.assertEqual(
            media_config(config, "video")["timeout"],
            42,
        )
        self.assertEqual(media_config(config, "video")["model"], "video-model")

    def test_media_attempts_are_clamped_to_supported_range(self):
        self.assertEqual(media_attempts({"image_delivery_retries": 0}, "image"), 2)
        self.assertEqual(media_attempts({"image_delivery_retries": 99}, "image"), 5)
        self.assertEqual(media_attempts({"image_delivery_retries": -2}, "image"), 1)


if __name__ == "__main__":
    unittest.main()
