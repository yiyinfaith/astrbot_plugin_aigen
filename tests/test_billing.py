"""Financial invariants, pricing safety and shared generation integration."""

import asyncio
import importlib
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_api_manager_urls import PACKAGE, load_api_manager

load_api_manager()
b = importlib.import_module(PACKAGE + ".billing")
commands = importlib.import_module(PACKAGE + ".billing_commands")


class Identity:
    def __init__(self, uid="123", platform="aiocqhttp", admin=False, group=""):
        self.uid, self.platform, self.admin, self.group = uid, platform, admin, group

    def get_sender_id(self):
        return self.uid

    def get_platform_name(self):
        return self.platform

    def get_group_id(self):
        return self.group

    def is_admin(self):
        return self.admin


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = b.BillingStore(Path(self.directory.name) / "billing.sqlite3")

    def credit(self, amount="10", owner="u"):
        code = self.store.create_cards("admin", amount, 1)[0]
        self.store.redeem(owner, code)

    def test_decimal_amounts_and_display(self):
        for value in ("0", "1", "10", "100", "0.01", "0.000001", "123.456789"):
            self.assertEqual(b.money(b.units(value)), value)
        self.assertEqual(b.units("0.0000005"), 1)
        for value in ("NaN", "Infinity", "-1", "1e99", True):
            with self.assertRaises(b.BillingError):
                b.units(value)

    def test_concurrent_card_redemption_exactly_once(self):
        code = self.store.create_cards("admin", "1.234567", 1)[0]

        def redeem(i):
            try:
                return self.store.redeem(str(i), code)
            except b.BillingError:
                return 0

        with ThreadPoolExecutor(max_workers=8) as pool:
            values = list(pool.map(redeem, range(16)))
        self.assertEqual(sum(values), 1234567)
        self.assertEqual(sum(bool(value) for value in values), 1)
        self.assertEqual(self.store.listing("cards")[0], 0)

    def test_simultaneous_requests_only_start_one_paid_task_per_user(self):
        self.credit("1")

        def begin(_):
            try:
                return self.store.begin("u", "video", "m", None, {})
            except b.BillingError:
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            orders = list(pool.map(begin, range(12)))
        self.assertEqual(sum(bool(order) for order in orders), 1)
        self.assertEqual(self.store.balance("u"), (1000000, 0))
        self.store.finish(
            next(order for order in orders if order), 1500000, "actual fee"
        )
        self.assertEqual(self.store.balance("u"), (-500000, 0))

    def test_settlement_refund_and_repeated_delivery_are_idempotent(self):
        self.credit()
        order = self.store.begin("u", "audio", "m", 4000000, {})
        self.store.finish(order, 1250000, "success")
        self.assertFalse(self.store.finish(order, 0, "retry"))
        self.assertEqual(self.store.balance("u"), (8750000, 0))
        with self.store.transaction() as db:
            total = db.execute(
                "SELECT SUM(delta) FROM ledger WHERE owner='u'"
            ).fetchone()[0]
        self.assertEqual(total, 8750000)

    def test_crash_recovery_does_not_charge_and_releases_old_holds_once(self):
        self.credit()
        order = self.store.begin("u", "audio", "m", None, {})
        with self.store.transaction() as db:
            db.execute("UPDATE balances SET amount=amount-4000000 WHERE owner='u'")
            db.execute(
                "UPDATE orders SET status='reserved',reserved=4000000 WHERE id=?",
                (order,),
            )
            db.execute(
                "INSERT INTO ledger(owner,delta,kind,ref) VALUES('u',-4000000,'reserve',?)",
                (order,),
            )
            db.execute("ALTER TABLE balances RENAME TO original_balances")
            db.execute(
                "CREATE TABLE balances(owner TEXT PRIMARY KEY, amount INTEGER NOT NULL CHECK(amount >= 0))"
            )
            db.execute("INSERT INTO balances SELECT * FROM original_balances")
            db.execute("DROP TABLE original_balances")
        reopened = b.BillingStore(self.store.path)
        self.assertEqual(reopened.balance("u"), (10000000, 0))
        self.assertEqual(reopened.listing("orders", "u")[1][0]["charged"], 0)
        self.assertFalse(reopened.finish(order, 1000000, "late success"))
        reopened = b.BillingStore(self.store.path)
        self.assertEqual(reopened.balance("u"), (10000000, 0))
        with reopened.transaction() as db:
            self.assertEqual(
                db.execute("SELECT SUM(delta) FROM ledger WHERE owner='u'").fetchone()[
                    0
                ],
                10000000,
            )

    def test_card_random_collision_rolls_back_entire_batch(self):
        with (
            patch.object(b.secrets, "token_hex", return_value="same"),
            self.assertRaises(b.BillingError),
        ):
            self.store.create_cards("admin", "1", 2)
        self.assertEqual(self.store.listing("cards")[0], 0)

    def test_card_listing_only_returns_valid_cards_and_paginates(self):
        cards = self.store.create_cards("admin", "1", 25)
        self.store.redeem("u", cards[0])
        total, page = self.store.listing("cards", page=2)
        self.assertEqual(total, 24)
        self.assertEqual(len(page), 4)
        self.assertNotIn(cards[0], [row["code"] for row in page])

    def test_identity_is_shared_across_chats_but_separated_across_platforms(self):
        self.assertEqual(
            b.BillingService.owner(Identity(group="a")),
            b.BillingService.owner(Identity(group="b")),
        )
        self.assertNotEqual(
            b.BillingService.owner(Identity()),
            b.BillingService.owner(Identity(platform="telegram")),
        )
        with self.assertRaises(b.BillingError):
            b.BillingService.owner(Identity(uid=""))


class PricingTest(unittest.TestCase):
    def test_resolution_duration_and_time_tiers(self):
        expr = b.Expression(
            '(0.2 if resolution == "1080p" else 0.1) * duration * (0.5 if 0 <= hour < 8 else 1)'
        )
        self.assertEqual(
            expr.evaluate({"resolution": "1080p", "duration": 10, "hour": 7}), 1000000
        )
        self.assertEqual(
            expr.evaluate({"resolution": "1080p", "duration": 10, "hour": 8}), 2000000
        )
        self.assertEqual(
            expr.evaluate({"resolution": "720p", "duration": 5, "hour": 8}), 500000
        )

    def test_decimal_arithmetic_and_rounding(self):
        self.assertEqual(b.Expression("0.1 + 0.2").evaluate({}), 300000)
        self.assertEqual(
            b.Expression("ceil(seconds / 5) * 0.1").evaluate({"seconds": 5.01}), 200000
        )
        self.assertEqual(
            b.Expression("max(0.2, min(3, seconds))").evaluate({"seconds": 7}), 3000000
        )

    def test_no_eval_attributes_comprehensions_or_expensive_powers(self):
        for expr in (
            "__import__('os')",
            "(1).__class__",
            "2 ** 999999",
            "[x for x in x]",
            "'x' * 999999999",
            "open('secret')",
            "min(x=1)",
            "(lambda: 1)()",
            "1 / 0",
            "-1",
        ):
            with self.subTest(expr=expr), self.assertRaises(b.BillingError):
                b.Expression(expr).evaluate({})

    def test_missing_usage_is_not_zero(self):
        with self.assertRaises(b.BillingError):
            b.Expression("input_tokens / 1000000").evaluate({})
        self.assertEqual(
            b.Expression("1 if media == 'image' else seconds").evaluate(
                {"media": "image"}
            ),
            1000000,
        )


class GenerationBillingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = {
            "billing_prices": [{"model": "m", "mode": "request", "price": "1"}]
        }
        self.service = b.BillingService(
            Path(self.directory.name) / "billing.sqlite3", self.config
        )
        self.event = Identity()
        self.owner = self.service.owner(self.event)
        code = self.service.store.create_cards("admin", "10", 1)[0]
        self.service.store.redeem(self.owner, code)

    async def call(self, callback, model="m", **kwargs):
        return await b.billed_call(
            self.service,
            self.event,
            "video",
            {"model": model, **kwargs},
            {"characters": 5, "images": 0, "audios": 0, "videos": 0},
            callback,
        )

    def balance(self):
        return self.service.store.balance(self.owner)

    def rule(self, **kwargs):
        self.config["billing_prices"] = [{"model": "m", **kwargs}]

    async def test_unconfigured_and_zero_prices_are_free(self):
        for model in ("unknown", "m"):
            self.rule(mode="request", price="0")
            self.assertEqual(
                await self.call(AsyncMock(return_value=b"image"), model=model), b"image"
            )
        self.assertEqual(self.balance(), (10000000, 0))
        self.assertEqual(self.service.store.listing("orders", self.owner)[0], 0)

    async def test_insufficient_funds_block_before_upstream(self):
        self.rule(mode="request", price="11")
        callback = AsyncMock(return_value=b"image")
        with self.assertRaises(b.BillingError):
            await self.call(callback)
        callback.assert_not_called()

    async def test_success_once_and_local_failure_refunded(self):
        await self.call(AsyncMock(return_value=b"image"))
        self.assertEqual(self.balance(), (9000000, 0))
        with self.assertRaises(ValueError):
            await self.call(AsyncMock(side_effect=ValueError("bad local input")))
        self.assertEqual(self.balance(), (9000000, 0))

    async def test_uncertain_submit_and_cancel_do_not_charge(self):
        async def unknown():
            b.track(state="submitted")
            raise TimeoutError

        with self.assertRaises(TimeoutError):
            await self.call(unknown)

        async def canceled():
            b.track(state="submitted")
            raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await self.call(canceled)
        self.assertEqual(self.balance(), (10000000, 0))
        self.assertEqual(self.service.store.listing("orders", self.owner)[0], 2)

    async def test_definite_upstream_failure_refunds(self):
        async def failed():
            b.track(state="submitted")
            b.track(state="failed")
            raise ValueError("failed task")

        with self.assertRaises(ValueError):
            await self.call(failed)
        self.assertEqual(self.balance(), (10000000, 0))

    async def test_generated_artifact_download_failure_is_still_charged(self):
        async def failed_download():
            b.track(state="completed")
            raise OSError("download")

        with self.assertRaises(OSError):
            await self.call(failed_download)
        self.assertEqual(self.balance(), (9000000, 0))

    async def test_tokens_use_full_actual_fee_and_missing_usage_is_free(self):
        self.rule(mode="tokens", input_price="1", output_price="2", reserve="3")

        async def generated():
            b.track(
                payload={
                    "usage": {"prompt_tokens": 1000000, "completion_tokens": 500000}
                }
            )
            return b"image"

        await self.call(generated)
        self.assertEqual(self.balance(), (8000000, 0))
        await self.call(AsyncMock(return_value=b"missing usage"))
        self.assertEqual(self.balance(), (8000000, 0))

        async def above_cap():
            b.track(
                payload={"usage": {"input_tokens": 4000000, "output_tokens": 5000000}}
            )
            return b"image"

        await self.call(above_cap)
        self.assertEqual(self.balance(), (-6000000, 0))
        callback = AsyncMock(return_value=b"next")
        with self.assertRaises(b.BillingError):
            await self.call(callback)
        callback.assert_not_called()

    async def test_concurrent_users_keep_actual_usage_isolated(self):
        self.rule(mode="tokens", input_price="1")
        other = Identity(uid="other")
        other_owner = self.service.owner(other)
        card = self.service.store.create_cards("admin", "10", 1)[0]
        self.service.store.redeem(other_owner, card)

        async def generate(tokens):
            b.track(payload={"usage": {"input_tokens": tokens}})
            await asyncio.sleep(0.01)
            return b"image"

        await asyncio.gather(
            self.call(lambda: generate(1000000)),
            b.billed_call(
                self.service,
                other,
                "video",
                {"model": "m"},
                {},
                lambda: generate(2000000),
            ),
        )
        self.assertEqual(self.balance(), (9000000, 0))
        self.assertEqual(self.service.store.balance(other_owner), (8000000, 0))

    async def test_dynamic_pricing_has_no_reservation_setting(self):
        self.rule(mode="seconds", price="0.1")

        async def generated():
            b.track(payload={"usage": {"duration_seconds": 1.5}})
            return b"media"

        await self.call(generated)
        self.assertEqual(self.balance(), (9850000, 0))

    async def test_old_instance_cannot_submit_after_hot_reload_begins(self):
        self.service.closed = True
        callback = AsyncMock(return_value=b"image")
        with self.assertRaises(b.BillingError):
            await self.call(callback)
        callback.assert_not_called()
        self.assertEqual(self.balance(), (10000000, 0))

    async def test_duplicates_rejected_and_different_models_are_free(self):
        self.config["billing_prices"] *= 2
        with self.assertRaises(b.BillingError):
            await self.call(AsyncMock())
        await self.call(AsyncMock(return_value=b"image"), model="other")
        self.assertEqual(self.balance(), (10000000, 0))

    async def test_characters_and_request_duration_are_known_before_submit(self):
        self.rule(mode="characters", price="100")
        await self.call(AsyncMock(return_value=b"audio"))
        self.assertEqual(self.balance(), (9500000, 0))
        self.rule(
            mode="expression",
            expression="duration * (0.2 if resolution == '1080p' else 0.1)",
        )
        await self.call(
            AsyncMock(return_value=b"video"), duration=5, resolution="1080p"
        )
        self.assertEqual(self.balance(), (8500000, 0))

    async def test_actual_seconds_use_local_completed_artifact(self):
        self.rule(mode="seconds", price="0.2", reserve="3")
        result = SimpleNamespace(path=Path(self.directory.name) / "result.mp4")
        process = SimpleNamespace(
            returncode=0, communicate=AsyncMock(return_value=(b"2.5\n", b""))
        )
        with patch.object(
            b.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
        ):
            await self.call(AsyncMock(return_value=result))
        self.assertEqual(self.balance(), (9500000, 0))

    async def test_missing_probe_and_usage_do_not_charge(self):
        self.rule(mode="seconds", price="0.2", reserve="3")
        result = SimpleNamespace(path=Path(self.directory.name) / "result.mp4")
        with patch.object(
            b.asyncio,
            "create_subprocess_exec",
            AsyncMock(side_effect=FileNotFoundError),
        ):
            self.assertIs(await self.call(AsyncMock(return_value=result)), result)
        self.assertEqual(self.balance(), (10000000, 0))

    async def test_submission_evidence_survives_process_interruption(self):
        order = self.service.store.begin(self.owner, "video", "m", 1000000, {})
        usage = b.Usage(self.service.store, order)
        token = b.CURRENT.set(usage)
        try:
            b.track(
                state="submitted",
                payload={
                    "task": {"task_id": "upstream-123"},
                    "usage": {"input_tokens": 12},
                },
            )
        finally:
            b.CURRENT.reset(token)
        self.service.store = b.BillingStore(self.service.store.path)
        _, rows = self.service.store.listing("orders", self.owner)
        self.assertIn("upstream-123", rows[0]["details"])
        self.assertIn("input_tokens", rows[0]["details"])
        self.assertEqual(self.balance(), (10000000, 0))

    async def test_database_settlement_failure_does_not_discard_paid_media(self):
        import sqlite3

        with (
            patch.object(
                self.service.store,
                "finish",
                side_effect=sqlite3.OperationalError("disk full"),
            ),
            self.assertLogs(b.LOGGER, level="ERROR"),
        ):
            self.assertEqual(
                await self.call(AsyncMock(return_value=b"completed")), b"completed"
            )
        self.assertEqual(self.balance(), (10000000, 0))

    async def test_failed_settlement_recovers_without_unlocking_an_active_owner(self):
        import sqlite3

        other = Identity(uid="other")
        other_owner = self.service.owner(other)
        code = self.service.store.create_cards("admin", "10", 1)[0]
        self.service.store.redeem(other_owner, code)
        active = self.service.store.begin(other_owner, "video", "m", 1000000, {})
        with (
            patch.object(
                self.service.store,
                "finish",
                side_effect=sqlite3.OperationalError("disk full"),
            ),
            self.assertLogs(b.LOGGER, level="ERROR"),
        ):
            await self.call(AsyncMock(return_value=b"completed"))
        self.assertEqual(len(self.service.unsettled), 1)
        self.assertEqual(await self.call(AsyncMock(return_value=b"second")), b"second")
        self.assertEqual(self.balance(), (9000000, 0))
        self.assertFalse(self.service.unsettled)
        with self.assertRaisesRegex(b.BillingError, "已有收费任务"):
            self.service.store.begin(other_owner, "video", "m", 1000000, {})
        self.service.store.finish(active, 0, "test cleanup")
        with self.service.store.transaction() as database:
            self.assertEqual(
                database.execute(
                    "SELECT COUNT(*) FROM orders WHERE status='running'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                database.execute(
                    "SELECT SUM(charged) FROM orders WHERE owner=?", (self.owner,)
                ).fetchone()[0],
                1000000,
            )

    async def test_checkpoint_failure_does_not_abandon_a_submitted_job(self):
        import sqlite3

        async def generate():
            b.track(state="submitted", payload={"task_id": "task-123"})
            b.track(state="completed")
            return b"completed"

        with (
            patch.object(
                self.service.store,
                "checkpoint",
                side_effect=sqlite3.OperationalError("disk full"),
            ),
            self.assertLogs(b.LOGGER, level="ERROR"),
        ):
            self.assertEqual(await self.call(generate), b"completed")
        self.assertEqual(self.balance(), (9000000, 0))
        _, orders = self.service.store.listing("orders", self.owner)
        self.assertEqual(orders[0]["status"], "settled")
        self.assertIn("task-123", orders[0]["details"])

    async def test_removed_admin_commands_cannot_change_or_expose_finances(self):
        for command in (
            "生成卡密 3 1",
            "剩余卡密",
            "作废卡密 x",
            "计费订单",
            "结算订单 x 0",
            "模型价格",
        ):
            self.assertIn(
                "不支持",
                commands.handle_command(self.service, Identity(admin=True), command),
            )
        self.assertEqual(self.balance(), (10000000, 0))
        card = self.service.store.create_cards(self.owner, "2", 1)[0]
        self.assertIn(
            "成功",
            commands.handle_command(self.service, self.event, "兑换卡密 " + card),
        )
        self.assertIn(
            "已兑换",
            commands.handle_command(self.service, self.event, "兑换卡密 " + card),
        )
        await self.call(AsyncMock(return_value=b"image"))
        self.assertIn(
            "共 0 条",
            commands.handle_command(self.service, Identity(uid="other"), "我的账单"),
        )


class UsageTest(unittest.TestCase):
    def test_gemini_thinking_is_billed_once_as_output(self):
        usage = b.Usage()
        usage.response(
            {
                "usageMetadata": {
                    "promptTokenCount": 10,
                    "candidatesTokenCount": 20,
                    "thoughtsTokenCount": 30,
                    "totalTokenCount": 60,
                }
            }
        )
        self.assertEqual(usage.values["output_tokens"], 50)
        self.assertEqual(usage.values["total_tokens"], 60)
        usage.response(
            {
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 50,
                    "completion_tokens_details": {"reasoning_tokens": 30},
                }
            }
        )
        self.assertEqual(usage.values["output_tokens"], 50)

    def test_sse_usage_and_minimax_milliseconds(self):
        usage = b.Usage()
        token = b.CURRENT.set(usage)
        try:
            b.track(
                raw="data: "
                + json.dumps({"usage": {"input_tokens": 12, "output_tokens": 34}})
                + "\ndata: [DONE]\n"
            )
            b.track(payload={"extra_info": {"audio_length": 1234}})
        finally:
            b.CURRENT.reset(token)
        self.assertEqual(
            usage.values,
            {
                "input_tokens": 12,
                "output_tokens": 34,
                "total_tokens": 46,
                "seconds": 1.234,
            },
        )

    def test_runtime_duration_is_never_output_seconds(self):
        usage = b.Usage()
        usage.response({"data": {"duration": 200}, "duration": 5, "elapsed": 100})
        self.assertNotIn("seconds", usage.values)

    def test_cumulative_stream_counts_replace_previous_counts(self):
        usage = b.Usage()
        usage.response({"usage": {"input_tokens": 5, "output_tokens": 10}})
        usage.response({"usage": {"input_tokens": 5, "output_tokens": 20}})
        self.assertEqual(usage.values["total_tokens"], 25)
        usage.response({"usage": {"input_tokens": 5, "output_tokens": 15}})
        self.assertEqual(usage.values["total_tokens"], 20)


class SharedGenerationPathsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_video_plugin import Event, VideoPluginTest

        self.fixture = VideoPluginTest()
        self.fixture.setUp()
        self.plugin = self.fixture.plugin
        self.event = Event([])
        self.event.get_platform_name = lambda: "aiocqhttp"
        self.event.is_admin = lambda: False
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.plugin.billing = b.BillingService(
            Path(self.directory.name) / "billing.sqlite3",
            {
                "billing_prices": [
                    {"model": "image-route", "price": "1"},
                    {"model": "long-route", "price": "2"},
                    {"model": "audio-route", "price": "3"},
                ]
            },
        )
        self.owner = self.plugin.billing.owner(self.event)
        code = self.plugin.billing.store.create_cards("admin", "10", 1)[0]
        self.plugin.billing.store.redeem(self.owner, code)

    async def asyncTearDown(self):
        await self.plugin.terminate()

    async def test_image_command_and_tool_share_routed_model_billing(self):
        self.plugin.conf = {
            "model": "base",
            "interface_mode": "openai_image",
            "auto_route": True,
            "routes": [{"__template_key": "text_to_image", "model": "image-route"}],
        }
        self.plugin.api_mgr.call_api = AsyncMock(return_value=b"image")
        self.plugin.img_mgr.optimize_output_image = AsyncMock(return_value=b"image")
        await self.plugin._generate(
            self.event,
            "prompt",
            "自定义",
            "base",
            [],
            show_progress=False,
            include_result_text=False,
        )
        self.assertEqual(self.plugin.billing.store.balance(self.owner), (9000000, 0))
        self.assertEqual(self.plugin.api_mgr.call_api.call_args.args[2], "image-route")

    async def test_video_prices_effective_long_model_and_not_delivery_retries(self):
        from test_video_plugin import api

        effective = {
            "model": "long-route",
            "duration": 12,
            "resolution": "480p",
            "aspect_ratio": "16:9",
        }
        self.plugin.video_mgr.prepare = lambda *args, **kwargs: (
            "text_to_video",
            SimpleNamespace(config=effective),
        )
        self.plugin.video_mgr.generate = AsyncMock(
            return_value=api.VideoResult(
                Path("generated.mp4"),
                "task",
                "long-route",
                "https://example.test/result.mp4",
            )
        )
        self.event.fail_video_send = True
        result = await self.plugin._generate_video(
            self.event,
            "prompt",
            "自定义",
            [],
            duration=12,
            tool_call=True,
            dispatch_result=True,
        )
        self.assertEqual(result, [])
        self.assertEqual(self.plugin.billing.store.balance(self.owner), (8000000, 0))
        self.plugin.video_mgr.generate.assert_awaited_once()

    async def test_audio_tool_success_has_one_charge_and_only_media(self):
        from test_video_plugin import Record, audio_api, audio_router

        options = audio_router.AudioOptions(
            mode="朗读", prompt="exact reading", prompt_segments=1
        )
        self.plugin.audio_mgr.prepare = lambda *args: (
            "text_to_speech",
            SimpleNamespace(config={"model": "audio-route"}),
        )
        self.plugin.audio_mgr.generate = AsyncMock(
            return_value=audio_api.AudioResult(
                Path("completed.wav"), "task", "audio-route"
            )
        )
        self.assertEqual(
            await self.plugin._generate_audio(self.event, options, [], tool_call=True),
            [],
        )
        self.assertEqual(self.plugin.billing.store.balance(self.owner), (7000000, 0))
        self.assertIsInstance(self.event.sent[-1][0], Record)

    async def test_recharge_command_is_not_collected_as_prompt(self):
        key = self.plugin._video_session_key(self.event)
        collection = {"text": []}
        self.plugin._video_collections[key] = collection
        self.event.message_str = "/余额"
        result = [part async for part in self.plugin.on_preset_request(self.event)]
        self.assertTrue(result)
        self.assertTrue(self.event.stopped)
        self.assertIs(self.plugin._video_collections[key], collection)
        self.assertEqual(collection["text"], [])

    async def test_hot_reload_cancels_active_image_without_charging(self):
        entered = asyncio.Event()

        async def pending():
            b.track(state="submitted")
            entered.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(
            b.billed_call(
                self.plugin.billing,
                self.event,
                "image",
                {"model": "image-route"},
                {},
                pending,
            )
        )
        await entered.wait()
        await self.plugin.terminate()
        self.assertTrue(task.cancelled())
        self.assertFalse(self.plugin.billing.tasks)
        self.assertEqual(self.plugin.billing.store.balance(self.owner), (10000000, 0))


if __name__ == "__main__":
    unittest.main()
