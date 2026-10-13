"""New API grammar parity, units, task profiles and help-delivery behavior."""

import importlib
import json
import os
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import test_billing as financial
from test_api_manager_urls import PACKAGE, load_api_manager

load_api_manager()
b = importlib.import_module(PACKAGE + ".billing")
expr = importlib.import_module(PACKAGE + ".billing_expression")


class NewAPIGrammarTest(unittest.TestCase):
    def test_has_uses_go_numeric_rendering_without_losing_precision(self):
        for value, rendered in (
            (1.0, "1"),
            (0.0, "0"),
            (1000000.0, "1e+06"),
            (1.2345678901234567, "1.2345678901234567"),
        ):
            with self.subTest(value=value):
                parsed = expr.PricingExpression(
                    'has(param("number"), "' + rendered + '") ? 1 : 0'
                )
                self.assertEqual(
                    parsed.evaluate_raw({"_request": {"number": value}})[0], 1
                )
        self.assertEqual(
            expr.PricingExpression('has(param("number"), ".0") ? 1 : 0').evaluate_raw(
                {"_request": {"number": 1.0}}
            )[0],
            0,
        )

    def test_ternary_tier_and_task_usage(self):
        source = 'v1:u("resolution") == "768p" ? tier("hq", u("seconds") * 0.2) : tier("base", u("seconds") * 0.1)'
        parsed = expr.PricingExpression(source)
        value, tier = parsed.evaluate_raw(
            {"_usage": {"resolution": "768p", "seconds": 2.5}}
        )
        self.assertEqual(value, Decimal("0.5"))
        self.assertEqual(tier, "hq")

    def test_nil_zero_false_and_short_circuit(self):
        parsed = expr.PricingExpression(
            'param("missing") == nil && param("zero") == 0 && param("flag") == false ? 0.5 : u("unavailable")'
        )
        self.assertEqual(
            parsed.evaluate_raw({"_request": {"zero": 0, "flag": False}})[0],
            Decimal("0.5"),
        )
        with self.assertRaises(b.BillingError):
            expr.PricingExpression("1 ? 2 : 3").evaluate_raw({})

    def test_token_coefficients_and_fixed_have_correct_units(self):
        self.assertEqual(
            b.Expression('tier("base", p * 2 + c * 8)', token_mode=True).evaluate(
                {"input_tokens": 1000, "output_tokens": 100}
            ),
            2800,
        )
        self.assertEqual(
            b.Expression('tier("request", fixed(0.01))', token_mode=True).evaluate({}),
            10000,
        )
        self.assertEqual(
            b.Expression('tier("request", fixed(0.01))').evaluate({}), 10000
        )
        self.assertEqual(
            b.Expression('tier("task", u("seconds") * 0.2)').evaluate(
                {"_usage": {"seconds": 2.5}}
            ),
            500000,
        )

    def test_cache_image_and_audio_normalization_matches_newapi(self):
        rule = b.Expression(
            'tier("base", p * 5 + cr * 1.25 + img * 8 + img_cr * 2 + c * 30)',
            token_mode=True,
        )
        self.assertEqual(
            rule.evaluate(
                {
                    "input_tokens": 1000,
                    "output_tokens": 100,
                    "img": 600,
                    "cr": 300,
                    "img_cr": 200,
                }
            ),
            8225,
        )
        self.assertEqual(
            b.Expression(
                'tier("base", p * 2 + c * 6 + ao * 4)', token_mode=True
            ).evaluate({"input_tokens": 100, "output_tokens": 50, "ao": 10}),
            480,
        )

    def test_time_functions_use_sunday_zero_and_one_frozen_instant(self):
        parsed = expr.PricingExpression(
            'weekday("Asia/Shanghai") == 0 && hour("Asia/Shanghai") == 8 ? tier("sunday", 0.5) : 1'
        )
        value, tier = parsed.evaluate_raw(
            {"_now": datetime(2026, 10, 11, 0, 0, tzinfo=timezone.utc)}
        )
        self.assertEqual(value, Decimal("0.5"))
        self.assertEqual(tier, "sunday")

    def test_header_case_paths_and_scalar_math(self):
        parsed = expr.PricingExpression(
            'header(" X-Tier ") == "pro" ? param("items.0.price") + param("items.#") : 0'
        )
        self.assertEqual(
            parsed.evaluate_raw(
                {
                    "_headers": {"x-tier": " pro "},
                    "_request": {"items": [{"price": 3}, {}]},
                }
            )[0],
            5,
        )
        self.assertEqual(
            expr.PricingExpression(
                'has(param("object"), "map[a:1 b:[true <nil>]]") ? 8 : 0'
            ).evaluate_raw({"_request": {"object": {"a": 1, "b": [True, None]}}})[0],
            8,
        )

    def test_unsupported_code_and_missing_usage_are_rejected(self):
        for source in (
            "__import__('os')",
            "param('x').constructor",
            "2 ** 999",
            "[1,2]",
            "v2:1",
            "u(name)",
        ):
            with self.subTest(source=source), self.assertRaises(b.BillingError):
                expr.PricingExpression(source)
        with self.assertRaises(b.BillingError):
            b.Expression('u("seconds") * 0.1').evaluate({"_usage": {}})

    @unittest.skipUnless(
        os.environ.get("AIGEN_NEWAPI_FIXTURES"),
        "Reference fixtures are outside the repository",
    )
    def test_official_newapi_reference_vectors(self):
        cases = json.loads(
            Path(os.environ["AIGEN_NEWAPI_FIXTURES"]).read_text(encoding="utf-8")
        )
        for case in cases:
            with self.subTest(name=case["name"]):
                values = {key: 0 for key in expr.TOKENS}
                values.update(case.get("tokens", {}))
                values["image_count"] = case.get("imageCount", 1)
                values["len"] = case.get("tokens", {}).get(
                    "len", values["p"] + values["cr"] + values["cc"] + values["cc1h"]
                )
                values.update(
                    _request=case.get("body", {}),
                    _headers=case.get("headers", {}),
                    _usage=case.get("usage", {}),
                )
                value, tier = expr.PricingExpression(case["expression"]).evaluate_raw(
                    values
                )
                self.assertAlmostEqual(float(value), case["cost"], places=8)
                self.assertEqual(tier, case.get("tier", ""))


class ActualFeeAndProfileTest(unittest.IsolatedAsyncioTestCase):
    setUp = financial.GenerationBillingTest.setUp
    call = financial.GenerationBillingTest.call
    balance = financial.GenerationBillingTest.balance
    rule = financial.GenerationBillingTest.rule

    async def test_autodl_profile_and_general_seconds_are_distinct(self):
        self.rule(mode="autodl_seconds", price="0.2")
        self.config["billing_prices"][0]["model"] = "IndexTTS-2"

        async def generated():
            b.track(payload={"extra_info": {"audio_length": 2500}})
            return b"audio"

        await self.call(generated, model="IndexTTS-2")
        self.assertEqual(self.balance(), (8000000, 0))
        self.config["billing_prices"][0]["mode"] = "seconds"
        await self.call(generated, model="IndexTTS-2")
        self.assertEqual(self.balance(), (7500000, 0))

    async def test_motion_profile_uses_output_measurement_without_capping(self):
        self.rule(mode="expression", expression='tier("base", u("seconds") * 0.2)')

        async def generated():
            b.track(payload={"usage": {"duration_seconds": 12}})
            return b"video"

        await self.call(generated, duration=1)
        self.assertEqual(self.balance(), (7600000, 0))

    async def test_tokens_can_charge_beyond_balance_and_topup_restores_access(self):
        self.rule(mode="token_expression", expression='tier("base", p * 2 + c * 8)')

        async def generated():
            b.track(
                payload={"usage": {"input_tokens": 1000000, "output_tokens": 2000000}}
            )
            return b"media"

        await self.call(generated)
        self.assertEqual(self.balance(), (-8000000, 0))
        card = self.service.store.create_cards("admin", "10", 1)[0]
        self.service.store.redeem(self.owner, card)
        self.assertEqual(self.balance(), (2000000, 0))

        async def small():
            b.track(payload={"usage": {"input_tokens": 1000, "output_tokens": 2000}})
            return b"media"

        await self.call(small)
        self.assertEqual(self.balance(), (1982000, 0))

    async def test_removed_help_and_forward_configured_text(self):
        from test_video_plugin import Event, Node, plugin_class

        commands = importlib.import_module(PACKAGE + ".billing_commands")
        self.assertNotIn("计费帮助", commands.COMMANDS)
        plugin = plugin_class().__new__(plugin_class())
        plugin.conf = {"help_text": "管理员的完整帮助\n余额说明"}
        plugin.context = SimpleNamespace()
        event = Event([])
        event.get_platform_name = lambda: "aiocqhttp"
        await plugin.help_command(event)
        self.assertTrue(event.stopped)
        self.assertEqual(len(event.sent), 1)
        self.assertIsInstance(event.sent[0][0], Node)
        self.assertEqual(event.sent[0][0].content[0].text, plugin.conf["help_text"])
