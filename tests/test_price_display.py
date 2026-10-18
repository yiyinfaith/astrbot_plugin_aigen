"""Price presentation must preserve coefficients, conditions and saved rules."""

import copy
import unittest

from test_finance_webui import web


class PriceDisplayTest(unittest.TestCase):
    def table(self, expression, model="m", **extra):
        preview = web.FinanceWebUI.preview(
            {"model": model, "expression": expression, **extra}
        )
        self.assertTrue(preview["valid"], preview)
        return preview["table"]

    def test_workflow_categories_resolve_else_without_guessing_tier_names(self):
        expression = (
            'hour("Asia/Shanghai") < 8 ? '
            '(u("resolution") == "480p" ? tier("x", u("seconds") * 0.02) : tier("y", u("seconds") * 0.03)) : '
            '(u("resolution") == "480p" ? tier("z", u("seconds") * 0.03) : tier("w", u("seconds") * 0.04))'
        )
        table = self.table(expression, "minimax_h3_lightx2v_v5_15s")
        self.assertEqual(
            table["columns"], [{"key": "seconds", "label": "视频生成单价"}]
        )
        self.assertEqual(
            [row["condition"] for row in table["rows"]],
            [
                "输出分辨率：480p · 00:00至08:00（Asia/Shanghai）",
                "输出分辨率：480p · 00:00至08:00以外的时段（Asia/Shanghai）",
                "输出分辨率：768p · 00:00至08:00（Asia/Shanghai）",
                "输出分辨率：768p · 00:00至08:00以外的时段（Asia/Shanghai）",
            ],
        )
        self.assertEqual(
            [r["fees"]["seconds"] for r in table["rows"]],
            ["0.02/秒", "0.03/秒", "0.03/秒", "0.04/秒"],
        )

    def test_unknown_model_else_keeps_its_exact_exclusion(self):
        table = self.table(
            'u("resolution") == "480p" ? u("seconds") * 0.02 : u("seconds") * 0.03'
        )
        self.assertEqual(table["rows"][1]["condition"], "输出分辨率不为480p")
        self.assertNotIn("768p", str(table))

    def test_three_declared_resolutions_use_the_correct_fallback(self):
        table = self.table(
            'u("resolution") == "480p" ? u("seconds") * 0.02 : u("resolution") == "768p" ? u("seconds") * 0.03 : u("seconds") * 0.1',
            "minimax_h3_image_audio_to_video_v2",
        )
        self.assertEqual(
            [r["fees"]["seconds"] for r in table["rows"]],
            ["0.02/秒", "0.03/秒", "0.1/秒"],
        )
        self.assertEqual(table["rows"][-1]["condition"], "输出分辨率：1080p")

    def test_combined_hours_and_reversed_comparisons_are_exact(self):
        table = self.table(
            '8 <= hour("Asia/Shanghai") && hour("Asia/Shanghai") < 20 ? fixed(1) : fixed(2)'
        )
        self.assertEqual(table["rows"][0]["condition"], "08:00至20:00（Asia/Shanghai）")
        self.assertEqual(
            table["rows"][1]["condition"], "08:00至20:00以外的时段（Asia/Shanghai）"
        )
        table = self.table('hour("UTC") > 22 || hour("UTC") < 2 ? 1 : 2')
        self.assertEqual(
            table["rows"][0]["condition"], "00:00至02:00、23:00至24:00（UTC）"
        )

    def test_not_equal_hours_and_distinct_timezones_do_not_lose_conditions(self):
        table = self.table('hour("UTC") != 8 ? 1 : 2')
        self.assertIn("09:00至24:00", table["rows"][0]["condition"])
        table = self.table('hour("UTC") < 8 && hour("Asia/Shanghai") < 20 ? 1 : 2')
        self.assertIn("UTC", table["rows"][0]["condition"])
        self.assertIn("Asia/Shanghai", table["rows"][0]["condition"])

    def test_token_columns_and_fixed_component_preserve_units(self):
        table = self.table('tier("base", p * 2 + c * 8 + fixed(0.2))')
        self.assertEqual(
            table["rows"][0]["fees"],
            {"p": "2/百万 Token", "c": "8/百万 Token", "fixed": "0.2/次"},
        )
        old = self.table("p * 2 + fixed(0.2)", mode="expression")
        self.assertEqual(
            old["rows"][0]["fees"], {"p": "2/百万 Token", "fixed": "0.2/次"}
        )

    def test_all_supported_units_and_zero(self):
        for expression, expected in [
            ('u("actual_seconds") * 0.1', {"actual_seconds": "0.1/秒"}),
            ('u("characters") * 0.0001', {"characters": "0.1/千字符"}),
            ('u("image_count") * 0.2', {"image_count": "0.2/张"}),
            (
                'u("requests") * 0.3 + fixed(0.1)',
                {"requests": "0.3/次", "fixed": "0.1/次"},
            ),
            ("0", {"free": "免费"}),
        ]:
            with self.subTest(expression=expression):
                self.assertEqual(self.table(expression)["rows"][0]["fees"], expected)

    def test_nonlinear_fee_shows_the_complete_formula(self):
        table = self.table('max(u("seconds"), 10) * 0.1')
        self.assertIn("max", table["rows"][0]["fees"]["formula"])
        self.assertNotIn("seconds", {col["key"] for col in table["columns"]})

    def test_autodl_minimum_is_a_single_footnote_and_rule_is_unchanged(self):
        rule = {"model": "IndexTTS-2", "expression": 'u("seconds") * 0.001'}
        before = copy.deepcopy(rule)
        preview = web.FinanceWebUI.preview(rule)
        self.assertEqual(
            preview["table"]["notes"], ["AutoDL 规则：不足 10 秒按 10 秒计费。"]
        )
        self.assertEqual(len(preview["table"]["rows"]), 1)
        self.assertEqual(rule, before)

    def test_invalid_expression_is_not_replaced_with_a_plausible_price(self):
        preview = web.FinanceWebUI.preview(
            {"model": "m", "expression": 'u("seconds") / 0'}
        )
        self.assertFalse(preview["valid"])
        self.assertNotIn("table", preview)

    def test_alias_media_labels_and_dynamic_timezone_are_not_guessed(self):
        for model, label in [
            ("wan2.2-animate-move", "视频生成单价"),
            ("IndexTTS-2", "音频生成单价"),
        ]:
            self.assertEqual(
                self.table('u("seconds") * 0.1', model)["columns"][0]["label"], label
            )
        table = self.table('hour(header("X-Timezone")) < 8 ? 1 : 2')
        self.assertNotIn("（header）", str(table))
        self.assertNotIn("00:00至08:00", str(table))


if __name__ == "__main__":
    unittest.main()
