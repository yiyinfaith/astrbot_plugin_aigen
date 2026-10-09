from pathlib import Path
import json
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PluginContractTest(unittest.TestCase):
    def test_schema_removes_only_requested_feature_groups(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        removed = {
            "model_list",
            "text_to_image_model",
            "text_to_image_api_url",
            "text_to_image_api_keys",
            "image_aspect_ratio",
            "enable_luxury_mode",
            "luxury_request_count",
            "user_whitelist",
            "user_blacklist",
            "enable_obedient_mode",
            "obedient_whitelist",
            "group_whitelist",
            "group_blacklist",
            "enable_user_limit",
            "enable_group_limit",
            "enable_checkin",
            "checkin_fixed_reward",
            "enable_random_checkin",
            "checkin_random_reward_max",
            "preset_table_quality",
            "preset_table_columns",
            "enable_llm_auto_detect",
            "context_rounds",
            "auto_detect_confidence",
            "context_max_messages",
            "context_max_sessions",
            "enable_preset_ref_images",
            "enable_persona_mode",
            "persona_name",
            "persona_description",
            "persona_trigger_keywords",
            "persona_scene_prompts",
            "persona_default_prompt",
            "persona_photo_style",
            "enable_rebellious_mode",
            "rebellious_probability",
            "batch_max_images",
            "batch_concurrency",
            "batch_retries",
        }
        self.assertFalse(removed.intersection(schema))
        image_schema = schema["image_settings"]["items"]
        self.assertIn("model", image_schema)
        self.assertIn("image_resolution", image_schema)
        self.assertEqual(len(image_schema["prompt_list"]["default"]), 44)
        visible = [name for name, node in schema.items() if not node.get("invisible")]
        self.assertEqual(visible, ["global_settings", "image_settings", "video_settings"])
        self.assertNotIn("仅generic模式", json.dumps(schema, ensure_ascii=False))

    def test_single_tool_and_unified_generation_path_are_present(self):
        main = (ROOT / "main.py").read_text(encoding="utf-8")
        api = (ROOT / "api_manager.py").read_text(encoding="utf-8")
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(main.count("@filter.llm_tool"), 2)
        self.assertIn('@filter.llm_tool(name="generate_image")', main)
        self.assertIn("async def generate_image(", main)
        self.assertNotIn("generate_image_tool", main)
        self.assertTrue(schema["image_settings"]["items"]["enable_llm_tool"]["default"])
        self.assertTrue(schema["video_settings"]["items"]["enable_llm_tool"]["default"])
        tool_start = main.index('@filter.llm_tool(name="generate_image")')
        tool_source = main[tool_start:]
        self.assertIn("show_progress=False", tool_source)
        self.assertIn("include_result_text=False", tool_source)
        self.assertNotIn("use_text_to_image_api", main + api)
        self.assertNotIn("text_to_image_model", main + api)

    def test_new_trigger_switches_replace_legacy_prefix_setting(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        image_schema = schema["image_settings"]["items"]
        main = (ROOT / "main.py").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("prefix", schema)
        self.assertEqual(image_schema["extra_prefix"]["default"], "生图")
        self.assertTrue(image_schema["custom_prompt_need_prefix"]["default"])
        self.assertFalse(image_schema["preset_need_prefix"]["default"])
        self.assertNotIn("memelite", json.dumps(schema, ensure_ascii=False).lower())
        self.assertNotIn("memelite", main.lower())
        self.assertNotIn("memelite", readme.lower())

    def test_personal_limit_is_not_used_for_generation(self):
        main = (ROOT / "main.py").read_text(encoding="utf-8")
        data_manager = (ROOT / "data_manager.py").read_text(encoding="utf-8")
        self.assertNotIn("decrease_user_count", main)
        self.assertNotIn("decrease_group_count", main)
        self.assertNotIn("_quota", main)
        self.assertNotIn("次数不足", main)
        self.assertNotIn("个人剩余：", main)
        self.assertNotIn("get_user_count", data_manager)
        self.assertNotIn("get_group_count", data_manager)
        self.assertIn("match_keyword_in_text", main)

    def test_removed_commands_and_legacy_helpers_are_gone(self):
        main = (ROOT / "main.py").read_text(encoding="utf-8")
        data_manager = (ROOT / "data_manager.py").read_text(encoding="utf-8")
        image_manager = (ROOT / "image_manager.py").read_text(encoding="utf-8")
        api_manager = (ROOT / "api_manager.py").read_text(encoding="utf-8")
        utils = (ROOT / "utils.py").read_text(encoding="utf-8")

        self.assertNotIn("切换API模式", main)
        self.assertNotIn("切换模型", main)
        self.assertNotIn("user_prompts", data_manager)
        self.assertNotIn("preset_images", data_manager)
        self.assertNotIn("extract_pdfs_from_event", image_manager)
        self.assertNotIn("create_preset_table", image_manager)
        self.assertNotIn("_normalize_call_api_args", api_manager)
        self.assertNotIn("generic_prefer_images_api", api_manager)
        self.assertNotIn("normalize_model_list", utils)


if __name__ == "__main__":
    unittest.main()
