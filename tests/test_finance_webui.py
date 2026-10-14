"""Financial WebUI transactions, expression previews and request boundaries."""

import asyncio
import importlib
import json
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, patch

import test_billing as financial
from test_api_manager_urls import PACKAGE, load_api_manager

load_api_manager()
b = importlib.import_module(PACKAGE + ".billing")
storage = importlib.import_module(PACKAGE + ".finance_store")
pricing = importlib.import_module(PACKAGE + ".model_prices")
web_stub = types.ModuleType("astrbot.api.web")
web_stub.request = types.SimpleNamespace()
web_stub.json_response = lambda data: (data, 200)
web_stub.error_response = lambda message, status_code=400: (
    {"error": message},
    status_code,
)
sys.modules.setdefault("astrbot.api.web", web_stub)
web = importlib.import_module(PACKAGE + ".finance_web")


class FinanceStoreTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.billing = b.BillingService(
            Path(self.directory.name) / "billing.sqlite3", {}
        )
        self.finance = storage.FinanceStore(
            self.billing.store, [{"model": "m", "expression": "fixed(1)"}]
        )
        self.billing.finance = self.finance
        self.owner = self.billing.owner(financial.Identity())

    def operation(self):
        return uuid.uuid4().hex

    def test_import_is_once_and_does_not_compile_prices_at_startup(self):
        with patch.object(
            pricing,
            "compile_price",
            side_effect=AssertionError("must not compile on reload"),
        ):
            storage.FinanceStore(
                self.billing.store, [{"model": "bad", "expression": "invalid()"}]
            )
        self.assertEqual(
            self.finance.rules(), [{"model": "m", "expression": "fixed(1)"}]
        )
        with closing(sqlite3.connect(self.billing.store.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)

    def test_cards_are_grouped_and_retry_does_not_create_another_batch(self):
        operation = self.operation()
        first = self.finance.create_cards("admin", operation, "10", 3)
        self.assertEqual(self.finance.create_cards("admin", operation, "10", 3), first)
        self.finance.create_cards("admin", self.operation(), "20", 2)
        groups = self.finance.overview()["categories"]
        self.assertEqual(
            [(g["amount"], g["active"]) for g in groups], [("10", 3), ("20", 2)]
        )
        with self.assertRaises(storage.FinanceConflict):
            self.finance.create_cards("admin", operation, "10", 4)
        with self.assertRaises(storage.FinanceConflict):
            self.finance.create_cards("someone-else", operation, "10", 3)

    def test_delete_is_atomic_retains_redeemed_records_and_cannot_be_redeemed(self):
        codes = self.finance.create_cards("admin", self.operation(), "10", 3)["codes"]
        self.billing.store.redeem(self.owner, codes[0])
        with self.assertRaises(storage.FinanceConflict):
            self.finance.delete_cards("admin", self.operation(), codes[:2])
        self.assertEqual(self.finance.overview()["categories"][0]["active"], 2)
        operation = self.operation()
        self.assertEqual(
            self.finance.delete_cards("admin", operation, codes[1:]), {"deleted": 2}
        )
        self.assertEqual(
            self.finance.delete_cards("admin", operation, codes[1:]), {"deleted": 2}
        )
        with self.assertRaises(b.BillingError):
            self.billing.store.redeem(self.owner, codes[1])
        history = self.finance.listing("redemptions")["rows"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["user_id"], "123")
        self.assertEqual(history[0]["amount"], "10")

    def test_balance_edit_is_idempotent_and_detects_intervening_recharge(self):
        operation = self.operation()
        result = self.finance.set_balance(
            "admin", operation, "aiocqhttp", "123", "0", "10", "人工充值"
        )
        self.assertEqual(
            self.finance.set_balance(
                "admin", operation, "aiocqhttp", "123", "0", "10", "人工充值"
            ),
            result,
        )
        self.assertEqual(self.billing.store.balance(self.owner)[0], 10000000)
        self.billing.store.redeem(
            self.owner,
            self.finance.create_cards("admin", self.operation(), "2", 1)["codes"][0],
        )
        with self.assertRaises(storage.FinanceConflict):
            self.finance.set_balance(
                "admin", self.operation(), "aiocqhttp", "123", "10", "20", "过期编辑"
            )
        self.assertEqual(self.billing.store.balance(self.owner)[0], 12000000)
        self.finance.set_balance(
            "admin", self.operation(), "aiocqhttp", "123", "12", "-1.25", "更正债务"
        )
        self.assertEqual(self.billing.store.balance(self.owner)[0], -1250000)
        rows = self.finance.listing("audit")["rows"]
        changes = [r["result"] for r in rows if r["kind"] == "balance"]
        self.assertEqual(len(changes), 2)
        self.assertEqual(changes[0]["delta"], "-13.25")
        self.assertEqual(self.finance.listing("users", status="negative")["total"], 1)

    def test_concurrent_card_delete_and_redeem_have_one_consistent_outcome(self):
        code = self.finance.create_cards("admin", self.operation(), "10", 1)["codes"][0]

        def delete():
            try:
                self.finance.delete_cards("admin", self.operation(), [code])
                return "deleted"
            except b.BillingError:
                return "already redeemed"

        def redeem():
            try:
                self.billing.store.redeem(self.owner, code)
                return "redeemed"
            except b.BillingError:
                return "already deleted"

        with ThreadPoolExecutor(max_workers=2) as pool:
            a, c = pool.submit(delete), pool.submit(redeem)
            outcomes = {a.result(), c.result()}
        self.assertIn(
            outcomes, ({"deleted", "already deleted"}, {"redeemed", "already redeemed"})
        )
        self.assertEqual(self.finance.overview()["categories"][0]["active"], 0)
        balance = self.billing.store.balance(self.owner)[0]
        self.assertEqual(balance, 10000000 if "redeemed" in outcomes else 0)

    def test_search_is_literal_and_audit_does_not_return_card_secrets(self):
        code = self.finance.create_cards("admin", self.operation(), "10", 1)["codes"][0]
        self.assertEqual(self.finance.listing("cards", search="%")["total"], 0)
        self.assertEqual(self.finance.listing("cards", search=code[-6:])["total"], 1)
        self.assertNotIn(code, json.dumps(self.finance.listing("audit")))

    def test_price_save_uses_revision_and_allows_an_invalid_expression(self):
        operation = self.operation()
        rows = [{"model": "bad", "expression": "not_a_function(1)"}]
        result = self.finance.save_prices("admin", operation, 0, rows)
        self.assertEqual(self.finance.save_prices("admin", operation, 0, rows), result)
        self.assertEqual(self.finance.rules(), rows)
        with self.assertRaises(storage.FinanceConflict):
            self.finance.save_prices("admin", self.operation(), 0, [])


class FinancePriceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = FinanceStoreTest()
        self.fixture.setUp()
        self.service, self.finance = self.fixture.billing, self.fixture.finance
        self.event = financial.Identity()
        code = self.finance.create_cards("admin", uuid.uuid4().hex, "10", 1)["codes"][0]
        self.service.store.redeem(self.fixture.owner, code)

    def tearDown(self):
        self.fixture.directory.cleanup()

    async def test_invalid_selected_rule_blocks_before_progress_or_submission(self):
        self.finance.save_prices(
            "admin",
            uuid.uuid4().hex,
            0,
            [
                {"model": "bad", "expression": 'u("seconds") + "oops"'},
                {"model": "good", "expression": "fixed(1)"},
            ],
        )
        submit, progress = AsyncMock(return_value=b"image"), AsyncMock()
        with self.assertRaisesRegex(
            b.BillingError, "价格设置不合法，请联系管理员配置模型价格"
        ):
            await b.billed_call(
                self.service,
                self.event,
                "image",
                {"model": "bad"},
                {},
                submit,
                before_submit=progress,
            )
        submit.assert_not_awaited()
        progress.assert_not_awaited()
        self.assertEqual(self.service.store.balance(self.fixture.owner)[0], 10000000)
        await b.billed_call(
            self.service,
            self.event,
            "image",
            {"model": "good"},
            {},
            submit,
            before_submit=progress,
        )
        self.assertEqual(self.service.store.balance(self.fixture.owner)[0], 9000000)

    async def test_price_edit_does_not_change_an_already_started_order(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def generate():
            entered.set()
            await release.wait()
            return b"image"

        task = asyncio.create_task(
            b.billed_call(
                self.service, self.event, "image", {"model": "m"}, {}, generate
            )
        )
        await entered.wait()
        self.finance.save_prices(
            "admin", uuid.uuid4().hex, 0, [{"model": "m", "expression": "fixed(3)"}]
        )
        release.set()
        await task
        self.assertEqual(self.service.store.balance(self.fixture.owner)[0], 9000000)
        await b.billed_call(
            self.service,
            self.event,
            "image",
            {"model": "m"},
            {},
            AsyncMock(return_value=b"image"),
        )
        self.assertEqual(self.service.store.balance(self.fixture.owner)[0], 6000000)

    def test_auto_units_and_mixed_fixed_fee_are_consistent(self):
        values = {"input_tokens": 1000, "output_tokens": 200}
        self.assertEqual(
            pricing.compile_price({"expression": "p * 2 + c * 8"}).evaluate(values),
            3600,
        )
        self.assertEqual(
            pricing.compile_price(
                {"expression": "p * 2 + c * 8 + fixed(0.2)"}
            ).evaluate(values),
            203600,
        )
        self.assertEqual(
            pricing.compile_price(
                {"expression": 'fixed(0.2) + u("seconds") * 0.1'}
            ).evaluate({"_usage": {"seconds": 5}}),
            700000,
        )
        self.assertTrue(pricing.compile_price({"expression": "p * 0 + c * 0"}).free)

    def test_preview_formats_token_seconds_tiers_and_reports_invalid_rules(self):
        preview = web.FinanceWebUI.preview(
            {"model": "m", "expression": 'tier("base", p * 2 + c * 8)'}
        )
        self.assertTrue(preview["valid"])
        self.assertEqual(
            preview["tiers"][0]["fees"],
            ["输入：2 / 百万 Token", "输出：8 / 百万 Token"],
        )
        preview = web.FinanceWebUI.preview(
            {
                "model": "m",
                "expression": 'u("resolution") == "768p" ? tier("high", u("seconds") * 0.2) : tier("base", u("seconds") * 0.1)',
            }
        )
        self.assertEqual(len(preview["tiers"]), 2)
        self.assertIn("每秒：0.2", preview["tiers"][0]["fees"])
        for source in (
            "1 / 0",
            'true ? 1 : "x"',
            "-1",
            'u("seconds") + "oops"',
            "unknown(1)",
        ):
            with self.subTest(source=source):
                self.assertFalse(
                    web.FinanceWebUI.preview({"model": "m", "expression": source})[
                        "valid"
                    ]
                )
        self.assertTrue(
            web.FinanceWebUI.preview(
                {"model": "m", "expression": 'ceil(u("seconds") / 5) * 0.2'}
            )["valid"]
        )

    def test_nested_tiers_expand_and_large_constants_are_rejected(self):
        result = web.FinanceWebUI.preview(
            {
                "model": "m",
                "expression": 'tier("base", (u("resolution") == "768p" ? u("seconds") * 0.2 : u("seconds") * 0.1))',
            }
        )
        self.assertEqual(len(result["tiers"]), 2)
        self.assertIn("每秒：0.2", result["tiers"][0]["fees"])
        for source in ("1e18 * 1e18", "min(-1, -2)", "true ? -1 : 1"):
            self.assertFalse(
                web.FinanceWebUI.preview({"model": "m", "expression": source})["valid"]
            )


class FinanceApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = FinanceStoreTest()
        self.fixture.setUp()
        self.api = web.FinanceWebUI(
            types.SimpleNamespace(register_web_api=lambda *args: None),
            self.fixture.finance,
        )

    async def asyncTearDown(self):
        await self.api.close()
        self.fixture.directory.cleanup()

    async def request(self, action, body=None, *, user="admin", method="POST"):
        query = types.SimpleNamespace(get=lambda name, default=None, **kwargs: default)
        incoming = types.SimpleNamespace(
            username=user,
            method=method,
            query=query,
            body=AsyncMock(return_value=json.dumps(body or {}).encode()),
        )
        with patch.object(web, "request", incoming):
            return await self.api.handle(action)

    async def test_missing_dashboard_identity_cannot_read_or_modify_finances(self):
        self.assertEqual((await self.request("cards", user=None, method="GET"))[1], 401)
        self.assertEqual(
            (
                await self.request(
                    "create-cards",
                    {"amount": "10", "count": 1, "operation_id": uuid.uuid4().hex},
                    user=None,
                )
            )[1],
            401,
        )
        self.assertEqual(self.fixture.finance.overview()["categories"], [])

    async def test_browser_actor_is_not_trusted_and_methods_are_checked(self):
        body = {
            "amount": "10",
            "count": 1,
            "operation_id": uuid.uuid4().hex,
            "actor": "forged",
        }
        result, status = await self.request(
            "create-cards", body, user="dashboard-admin"
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["count"], 1)
        self.assertEqual(
            self.fixture.finance.listing("audit")["rows"][0]["actor"], "dashboard-admin"
        )
        self.assertEqual((await self.request("create-cards", method="GET"))[1], 405)

    async def test_no_price_check_runs_until_a_page_or_generation_requests_it(self):
        self.fixture.finance.save_prices(
            "admin", uuid.uuid4().hex, 0, [{"model": "bad", "expression": "invalid()"}]
        )
        result, status = await self.request("prices", method="GET")
        self.assertEqual(status, 200)
        self.assertFalse(result["rows"][0]["valid"])
        self.assertEqual(result["rows"][0]["error"], "表达式不合法")

    async def test_page_preview_and_closed_reload_state(self):
        result, status = await self.request(
            "preview", {"model": "m", "expression": 'u("seconds") * 0.1'}
        )
        self.assertEqual(status, 200)
        self.assertIn("每秒：0.1", result["tiers"][0]["fees"])
        await self.api.close()
        self.assertEqual((await self.request("overview", method="GET"))[1], 503)

    async def test_cancelled_client_write_still_drains_before_reload(self):
        entered, release = threading.Event(), threading.Event()
        original = self.fixture.finance.create_cards

        def slow_create(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test worker timed out")
            return original(*args)

        self.fixture.finance.create_cards = slow_create
        body = {"amount": "2", "count": 1, "operation_id": uuid.uuid4().hex}
        task = asyncio.create_task(self.request("create-cards", body))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            closing_task = asyncio.create_task(self.api.close())
            await asyncio.sleep(0.02)
            self.assertFalse(closing_task.done())
            release.set()
            await closing_task
            self.assertEqual(
                self.fixture.finance.overview()["categories"][0]["active"], 1
            )
            self.assertEqual(len(self.api.workers), 0)
        finally:
            release.set()
