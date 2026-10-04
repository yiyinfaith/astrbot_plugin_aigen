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
        }
        self.assertFalse(removed.intersection(schema))
        self.assertIn("model", schema)
        self.assertIn("image_resolution", schema)
        self.assertEqual(len(schema["prompt_list"]["default"]), 44)
        self.assertNotIn("仅generic模式", json.dumps(schema, ensure_ascii=False))

    def test_single_tool_and_unified_generation_path_are_present(self):
        main = (ROOT / "main.py").read_text(encoding="utf-8")
        api = (ROOT / "api_manager.py").read_text(encoding="utf-8")
        self.assertEqual(main.count("@filter.llm_tool"), 1)
        self.assertIn('@filter.llm_tool(name="generate_image")', main)
        self.assertNotIn("use_text_to_image_api", main + api)
        self.assertNotIn("text_to_image_model", main + api)

    def test_new_trigger_switches_replace_legacy_prefix_setting(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        main = (ROOT / "main.py").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("prefix", schema)
        self.assertEqual(schema["extra_prefix"]["default"], "生图")
        self.assertTrue(schema["custom_prompt_need_prefix"]["default"])
        self.assertFalse(schema["preset_need_prefix"]["default"])
        self.assertNotIn("memelite", json.dumps(schema, ensure_ascii=False).lower())
        self.assertNotIn("memelite", main.lower())
        self.assertNotIn("memelite", readme.lower())

    def test_personal_limit_is_not_used_for_generation(self):
        main = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("decrease_user_count", main)
        self.assertNotIn("个人剩余：", main)
        self.assertIn("match_keyword_in_text", main)


if __name__ == "__main__":
    unittest.main()
