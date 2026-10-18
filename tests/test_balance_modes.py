"""Atomic relative adjustments, exact accounting and the revised collection UI."""

import re
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import test_finance_webui as fixture
import test_video_collection as collection
import test_video_plugin as behavior


class AdjustmentTest(unittest.TestCase):
    setUp = fixture.FinanceStoreTest.setUp
    operation = fixture.FinanceStoreTest.operation

    def adjust(self, amount, mode="set", expected="0", operation=None):
        return self.finance.set_balance(
            "admin",
            operation or self.operation(),
            "aiocqhttp",
            "123",
            expected,
            amount,
            "test",
            mode=mode,
        )

    def test_relative_adjustments_use_current_balance_and_keep_audit_and_ledger_exact(
        self,
    ):
        self.adjust("10")
        # Displayed balance is deliberately stale after another admin's recharge.
        result = self.adjust("0.1", mode="add")
        self.assertEqual(
            (result["before"], result["after"], result["delta"]), ("10", "10.1", "0.1")
        )
        result = self.adjust("0.2", mode="subtract")
        self.assertEqual(
            (result["before"], result["after"], result["delta"]),
            ("10.1", "9.9", "-0.2"),
        )
        result = self.adjust("10", mode="subtract")
        self.assertEqual(result["after"], "-0.1")
        with closing(sqlite3.connect(self.billing.store.path)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT SUM(delta) FROM ledger WHERE owner=?", (self.owner,)
                ).fetchone()[0],
                -100000,
            )
        self.assertEqual(
            self.finance.listing("audit")["rows"][0]["result"]["mode"], "subtract"
        )
        with self.assertRaises(fixture.storage.FinanceConflict):
            self.adjust("15", expected="0")

    def test_concurrent_relative_changes_apply_once_each_and_retries_never_double_credit(
        self,
    ):
        self.adjust("10")
        with ThreadPoolExecutor(8) as pool:
            results = list(
                pool.map(lambda _: self.adjust("0.000001", mode="add"), range(16))
            )
        self.assertEqual(len(results), 16)
        self.assertEqual(self.billing.store.balance(self.owner)[0], 10000016)
        operation = self.operation()
        with ThreadPoolExecutor(8) as pool:
            results = list(
                pool.map(
                    lambda _: self.adjust("2", mode="add", operation=operation),
                    range(8),
                )
            )
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(self.billing.store.balance(self.owner)[0], 12000016)
        with self.assertRaises(fixture.storage.FinanceConflict):
            self.adjust("2", mode="subtract", operation=operation)
        with closing(sqlite3.connect(self.billing.store.path)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT SUM(delta) FROM ledger WHERE owner=?", (self.owner,)
                ).fetchone()[0],
                12000016,
            )

    def test_invalid_modes_amounts_and_result_overflow_do_not_change_funds(self):
        self.adjust("1000000000")
        for mode in ("unknown", None, [], True):
            with self.assertRaises(fixture.b.BillingError):
                self.adjust("1", mode=mode)
        for mode in ("add", "subtract"):
            for amount in ("0", "-1", "NaN", "Infinity", "0.0000001"):
                with self.assertRaises(fixture.b.BillingError):
                    self.adjust(amount, mode=mode)
        with self.assertRaises(fixture.b.BillingError):
            self.adjust("0.000001", mode="add")
        self.assertEqual(self.billing.store.balance(self.owner)[0], 1000000000000000)

    def test_insufficient_balance_uses_requested_recharge_message(self):
        with self.assertRaises(fixture.b.BillingError) as caught:
            self.billing.store.begin(self.owner, "video", "model", 1000000, {})
        self.assertEqual(
            str(caught.exception),
            "用户余额不足；请发送“/AI余额”查询，或发送“/兑换AI卡密 xxxxxx”进行充值（中间记得加空格，否则充值失败，需迅速撤回重试）。",
        )


class AdjustmentApiTest(unittest.IsolatedAsyncioTestCase):
    setUp = fixture.FinanceApiTest.setUp
    asyncTearDown = fixture.FinanceApiTest.asyncTearDown
    request = fixture.FinanceApiTest.request

    async def test_api_applies_selected_mode_and_rejects_unknown_values(self):
        payload = {
            "platform": "aiocqhttp",
            "user_id": "123",
            "expected": "0",
            "amount": "3",
            "reason": "test",
            "mode": "add",
            "operation_id": self.fixture.operation(),
        }
        result, status = await self.request("set-balance", payload)
        self.assertEqual((status, result["after"], result["mode"]), (200, "3", "add"))
        result, status = await self.request(
            "set-balance",
            {
                **payload,
                "mode": "subtract",
                "amount": "1",
                "operation_id": self.fixture.operation(),
            },
        )
        self.assertEqual((status, result["after"]), (200, "2"))
        _, status = await self.request(
            "set-balance",
            {**payload, "mode": "unknown", "operation_id": self.fixture.operation()},
        )
        self.assertEqual(status, 400)


class AliasCollectionTest(unittest.IsolatedAsyncioTestCase):
    setUp = collection.VideoCollectionTest.setUp
    asyncTearDown = collection.VideoCollectionTest.asyncTearDown

    async def receive(self, text):
        event = behavior.Event([], text=text)
        event.get_platform_name = lambda: "aiocqhttp"
        event.unified_msg_origin = "qq:group"
        result = [part async for part in self.plugin.on_preset_request(event)]
        if (
            text.split(maxsplit=1)[0] in {"/生视频", "/视频", "/生成视频"}
            and not event.stopped
        ):
            result = [part async for part in self.plugin.video_command(event)]
        return event, result

    async def test_video_alias_starts_and_restarts_without_becoming_prompt(self):
        await self.receive("/视频 首帧")
        record = next(iter(self.plugin._video_collections.values()))
        self.assertEqual(record["text"], ["首帧"])
        await self.receive("/视频 尾帧")
        record = next(iter(self.plugin._video_collections.values()))
        self.assertEqual(record["text"], ["尾帧"])
        self.assertEqual(self.plugin._resolve_audio_command("/音频 朗读"), "朗读")
        self.assertIsNone(self.plugin._resolve_audio_command("音频很好听"))

    async def test_audio_alias_enters_the_audio_collection(self):
        await self.receive("/音频 朗读")
        record = next(iter(self.plugin._video_collections.values()))
        self.assertEqual(record["kind"], "audio")
        self.assertEqual(record["audio_mode"], "朗读")

    async def test_settings_send_one_container_with_all_options_and_usage(self):
        await self.receive("/视频")
        event, _ = await self.receive("/设置")
        self.assertEqual(len(event.sent), 1)
        self.assertEqual(len(event.sent[0]), 1)
        self.assertIsInstance(event.sent[0][0], behavior.Nodes)
        text = "\n\n".join(node.content[0].text for node in event.sent[0][0].nodes)
        self.assertIn("一次性发送", text)
        self.assertIn("未选参数保持原值", text)
        self.assertIn("不加 /", text)
        choices = re.search("例如：([0-9 ]+)。", text).group(1)
        record = next(iter(self.plugin._video_collections.values()))
        behavior.capabilities.apply_menu_selection(choices, record["settings_menu"], {})
