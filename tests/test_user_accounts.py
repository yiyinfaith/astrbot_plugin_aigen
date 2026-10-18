"""Command aliases, account discovery and trusted tool billing identities."""

import ast
import asyncio
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import test_billing as billing
import test_finance_webui as finance
import test_video_plugin as behavior


class AccountStoreTest(unittest.TestCase):
    setUp = finance.FinanceStoreTest.setUp

    def test_registration_is_idempotent_and_never_resets_or_charges_balance(self):
        event = billing.Identity(uid="new-user")
        with ThreadPoolExecutor(8) as pool:
            owners = list(
                pool.map(lambda _: self.billing.register_user(event), range(16))
            )
        self.assertEqual(len(set(owners)), 1)
        self.assertEqual(self.finance.listing("users")["total"], 1)
        self.finance.set_balance(
            "admin", self.operation(), "aiocqhttp", "new-user", "0", "-2.5", "test"
        )
        self.billing.register_user(event)
        self.assertEqual(self.billing.store.balance(owners[0]), (-2500000, 0))
        with closing(sqlite3.connect(self.billing.store.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ledger").fetchone()[0], 1)

    operation = finance.FinanceStoreTest.operation

    def test_user_options_paginate_by_exact_platform_and_literal_id(self):
        for platform in ("qq", "QQ", "telegram"):
            for i in range(61):
                self.billing.register_user(
                    billing.Identity(uid=f"{i:03}", platform=platform)
                )
        for uid in ("percent%", "under_score", 'quote"', "slash\\", "中文用户"):
            self.billing.register_user(billing.Identity(uid=uid, platform="qq"))
            found = self.finance.user_options("qq", search=uid)
            self.assertEqual(found["total"], 1)
            self.assertEqual(found["rows"][0]["user_id"], uid)
        first = self.finance.user_options("qq")
        second = self.finance.user_options("qq", page=2)
        self.assertEqual(
            (first["total"], len(first["rows"]), len(second["rows"])), (66, 50, 16)
        )
        self.assertFalse(
            {row["user_id"] for row in first["rows"]}
            & {row["user_id"] for row in second["rows"]}
        )
        self.assertTrue(all(row["platform"] == "qq" for row in first["rows"]))
        self.assertEqual(self.finance.user_options("QQ")["total"], 61)
        self.assertEqual(self.finance.user_options("qq", search="telegram")["total"], 0)
        for args in (
            {"platform": ""},
            {"platform": "qq", "page": True},
            {"platform": "qq", "search": "%" * 201},
        ):
            with self.assertRaises(billing.b.BillingError):
                self.finance.user_options(**args)


class CallerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = billing.SharedGenerationPathsTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.directory.cleanup)
        self.plugin = self.fixture.plugin
        self.service = self.plugin.billing
        self.finance = finance.storage.FinanceStore(self.service.store)

    async def asyncTearDown(self):
        await self.plugin.terminate()

    def event(self, uid, text="", chain=None, platform="aiocqhttp"):
        event = behavior.Event(chain or [], text=text)
        event.get_platform_name = lambda: platform
        event.get_sender_id = lambda: uid
        return event

    async def test_each_finance_alias_registers_the_sender_and_preserves_raw_validation(
        self,
    ):
        for index, name in enumerate(billing.commands.COMMANDS):
            event = self.event("alias-" + str(index), "/" + name)
            result = [part async for part in self.plugin.on_preset_request(event)]
            self.assertTrue(result)
            self.assertTrue(event.stopped)
            owner = self.service.owner(event)
            self.assertEqual(self.service.store.balance(owner), (0, 0))
            self.assertTrue(
                any(
                    row["owner"] == owner
                    for row in self.finance.listing("users")["rows"]
                )
            )
        event = self.event("alias-invalid", "/兑换ai卡密 one two")
        result = [part async for part in self.plugin.redeem_card_command(event, "one")]
        self.assertIn("用法：/兑换AI卡密", result[0][0].text)

    async def test_new_help_aliases_are_not_collected_and_keep_configured_help(self):
        self.plugin.conf["help_text"] = "管理员自定义帮助"
        for name in self.plugin._HELP_COMMANDS:
            event = self.event("helper", "/" + name)
            collection = {"text": []}
            self.plugin._video_collections[self.plugin._video_session_key(event)] = (
                collection
            )
            self.assertEqual(
                [part async for part in self.plugin.on_preset_request(event)], []
            )
            await self.plugin.help_command(event)
            self.assertEqual(collection["text"], [])
            self.assertEqual(event.sent[-1][0].content[0].text, "管理员自定义帮助")
        self.assertEqual(self.finance.listing("users", search="helper")["total"], 1)

    async def test_generation_commands_register_even_before_materials_are_complete(
        self,
    ):
        for uid, command, handler in (
            ("image-user", "/生图", self.plugin.draw_command),
            ("video-user", "/生视频", self.plugin.video_command),
            ("audio-user", "/生音频", self.plugin.audio_command),
        ):
            event = self.event(uid, command)
            [part async for part in handler(event)]
            self.assertEqual(self.finance.listing("users", search=uid)["total"], 1)

    async def test_ordinary_group_chat_never_registers_bystanders(self):
        event = self.event("bystander", "普通群聊文字")
        self.assertEqual(
            [part async for part in self.plugin.on_preset_request(event)], []
        )
        self.assertEqual(self.finance.listing("users", search="bystander")["total"], 0)

    async def test_capability_query_registers_user_and_identifies_its_three_tools(self):
        event = self.event("query-only")
        result = "\n".join(
            [part async for part in self.plugin.get_generation_capabilities(event)]
        )
        self.assertIn("astrbot_plugin_aigen", result)
        for tool in ("generate_image", "generate_video", "generate_audio"):
            self.assertIn("适用工具：" + tool, result)
        self.assertEqual(self.finance.listing("users", search="query-only")["total"], 1)

    async def test_free_generation_registers_sender_without_orders_or_ledger(self):
        event = self.event("free-user")
        result = await billing.b.billed_call(
            self.service,
            event,
            "image",
            {"model": "free"},
            {},
            AsyncMock(return_value=b"image"),
        )
        self.assertEqual(result, b"image")
        self.assertEqual(self.finance.listing("users", search="free-user")["total"], 1)
        self.assertEqual(
            self.service.store.listing("orders", self.service.owner(event))[0], 0
        )

    async def test_all_three_real_tool_entries_charge_their_own_sender_in_one_group(
        self,
    ):
        self.plugin.conf.update(model="image-route", llm_cooldown_seconds=0)
        self.plugin.api_mgr.call_api = AsyncMock(return_value=b"image")
        self.plugin.img_mgr.optimize_output_image = AsyncMock(return_value=b"image")
        self.plugin.video_mgr.prepare = lambda *a, **k: (
            "image_to_video",
            SimpleNamespace(config={"model": "long-route"}),
        )
        self.plugin.audio_mgr.prepare = lambda *a, **k: (
            "text_to_speech",
            SimpleNamespace(config={"model": "audio-route"}),
        )
        self.plugin.audio_mgr.generate = AsyncMock(
            return_value=behavior.audio_api.AudioResult(
                Path("output.wav"), "task", "audio-route"
            )
        )
        quoted = behavior.Reply([behavior.Image("https://example.test/quoted.png")])
        quoted.sender_id = "quoted-author"
        invocations = (
            (
                "image-speaker",
                self.plugin.generate_image,
                {"prompt": "cat", "use_message_media": False},
                1,
            ),
            (
                "video-speaker",
                self.plugin.generate_video,
                {"prompt": "cat", "use_message_media": True},
                2,
            ),
            (
                "audio-speaker",
                self.plugin.generate_audio,
                {"text": "hello", "use_message_media": False},
                3,
            ),
        )

        async def invoke(uid, tool, kwargs, amount):
            event = self.event(uid, "帮我生成", [quoted])
            owner = self.service.owner(event)
            code = self.service.store.create_cards("admin", "10", 1)[0]
            self.service.store.redeem(owner, code)
            replies = [part async for part in tool(event, **kwargs)]
            self.assertEqual(replies, [])
            self.assertEqual(
                self.service.store.balance(owner)[0], (10 - amount) * 1000000
            )
            _, rows = self.service.store.listing("orders", owner)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["owner"], owner)

        await asyncio.gather(*(invoke(*invocation) for invocation in invocations))
        self.assertEqual(
            self.finance.listing("users", search="quoted-author")["total"], 0
        )
        self.assertEqual(self.service.store.balance(self.fixture.owner)[0], 10000000)


class AliasContractTest(unittest.TestCase):
    def test_formal_command_names_and_exact_aliases(self):
        tree = ast.parse(
            (Path(__file__).resolve().parents[1] / "main.py").read_text("utf-8")
        )
        found = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                for decorator in node.decorator_list:
                    if (
                        isinstance(decorator, ast.Call)
                        and isinstance(decorator.func, ast.Attribute)
                        and decorator.func.attr == "command"
                    ):
                        self.assertNotIn("aliases", {k.arg for k in decorator.keywords})
                        aliases = next(
                            (
                                ast.literal_eval(k.value)
                                for k in decorator.keywords
                                if k.arg == "alias"
                            ),
                            set(),
                        )
                        found[ast.literal_eval(decorator.args[0])] = aliases
        for name, aliases in {
            "AI帮助": {"ai帮助", "AI菜单", "ai菜单"},
            "AI余额": {"ai余额"},
            "AI账单": {"ai账单"},
            "兑换AI卡密": {"兑换ai卡密"},
        }.items():
            self.assertEqual(found[name], aliases)
        self.assertTrue(
            {"ai生成帮助", "余额", "我的账单", "兑换卡密"}.isdisjoint(found)
        )


class DiscoveryApiTest(unittest.IsolatedAsyncioTestCase):
    setUp = finance.FinanceApiTest.setUp
    asyncTearDown = finance.FinanceApiTest.asyncTearDown
    request = finance.FinanceApiTest.request

    async def test_platforms_come_from_astrbot_and_keep_historical_account_types(self):
        self.api.context.platform_manager = SimpleNamespace(
            get_insts=lambda: [
                SimpleNamespace(meta=lambda: SimpleNamespace(name="telegram")),
                SimpleNamespace(meta=lambda: SimpleNamespace(name="telegram")),
                SimpleNamespace(meta=lambda: SimpleNamespace(name="discord")),
            ]
        )
        self.fixture.billing.register_user(billing.Identity(platform="aiocqhttp"))
        result, status = await self.request("platforms", method="GET")
        self.assertEqual(status, 200)
        self.assertEqual(
            [row["value"] for row in result["rows"]],
            ["aiocqhttp", "discord", "telegram"],
        )
        self.assertFalse(result["rows"][0]["configured"])
        self.assertTrue(result["rows"][1]["configured"])
        self.assertEqual(
            (await self.request("platforms", user=None, method="GET"))[1], 401
        )
        self.assertEqual(
            (await self.request("user-options", user=None, method="GET"))[1], 401
        )
        self.assertEqual((await self.request("user-options", method="GET"))[1], 400)
