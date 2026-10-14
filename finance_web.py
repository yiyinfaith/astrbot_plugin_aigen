"""AstrBot Pages adapter for the administrator's financial console."""

from __future__ import annotations

import asyncio
import json
import sqlite3

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

from .billing import BillingError
from .finance_store import FinanceConflict
from .model_prices import compile_price, price_sections


class FinanceWebUI:
    def __init__(self, context, finance):
        """Register the official authenticated plugin-extension route.

        Args:
            context: AstrBot plugin context.
            finance: Management store sharing the live ledger.
        """
        self.finance = finance
        self.closed = False
        self.tasks = set()
        self.workers = set()
        context.register_web_api(
            "/astrbot_plugin_aigen/finance/<action>",
            self.handle,
            ["GET", "POST"],
            "Manage AIGen model prices, recharge cards and user balances",
        )

    async def close(self):
        """Drain in-flight database work before the plugin is unloaded."""
        self.closed = True
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        if self.workers:
            await asyncio.gather(*tuple(self.workers), return_exceptions=True)

    async def _work(self, function, *args, **kwargs):
        """Keep database workers drainable even if their HTTP request is cancelled.

        Args:
            function: Blocking storage or preview operation.
            *args: Operation positional arguments.
            **kwargs: Operation keyword arguments.

        Returns:
            The blocking operation's result.
        """
        worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        self.workers.add(worker)

        def complete(done):
            self.workers.discard(done)
            if not done.cancelled():
                done.exception()  # Consume errors after a disconnected HTTP client.

        worker.add_done_callback(complete)
        # shield prevents cancelled HTTP clients from dropping running writes.
        return await asyncio.shield(worker)

    @staticmethod
    def preview(rule):
        """Describe one rule without a paid request or invented usage facts.

        Args:
            rule: Raw editor rule or preserved legacy rule.

        Returns:
            Structured conditions and fee lines, or a validation diagnostic.
        """
        try:
            expression = compile_price(rule)
            section = price_sections([{**rule, "model": rule.get("model") or "预览"}])[
                -1
            ]
            body = section.split("具体费用：", 1)[-1].strip()
            return {
                "valid": True,
                "expression": expression.source,
                "unit": "每百万 Token" if expression.token_mode else "余额单位",
                "tiers": [
                    {"condition": lines[0], "fees": lines[1:] or [lines[0]]}
                    for part in body.split("\n\n")
                    if (lines := part.splitlines())
                ],
            }
        except BillingError as exc:
            return {
                "valid": False,
                "expression": str(rule.get("expression", "")),
                "error": "表达式不合法",
                "detail": str(exc),
                "tiers": [],
            }

    async def handle(self, action):
        """Validate the trusted Dashboard identity and dispatch a bounded request.

        Args:
            action: Plugin-relative operation selected by AstrBot's route matcher.

        Returns:
            A native AstrBot JSON response, including clear conflict statuses.
        """
        actor = request.username
        if not isinstance(actor, str) or not actor.strip():
            return error_response("请先登录 AstrBot 管理面板。", status_code=401)
        if self.closed:
            return error_response("插件正在重载，请稍后刷新。", status_code=503)
        reads = {
            "overview",
            "prices",
            "cards",
            "users",
            "redemptions",
            "orders",
            "audit",
        }
        writes = {
            "preview",
            "save-prices",
            "create-cards",
            "delete-cards",
            "set-balance",
        }
        if (
            request.method not in {"GET", "POST"}
            or (request.method == "GET" and action not in reads)
            or (request.method == "POST" and action not in writes)
        ):
            return error_response("不支持的操作。", status_code=405)
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            if request.method == "GET":
                if action == "overview":
                    result = await self._work(self.finance.overview)
                elif action == "prices":
                    result = await self._work(self.finance.prices)
                    result["rows"] = await self._work(
                        lambda: [
                            {
                                "id": row["id"],
                                "model": row["rule"].get("model", "")
                                if isinstance(row["rule"], dict)
                                else "",
                                **self.preview(
                                    row["rule"]
                                    if isinstance(row["rule"], dict)
                                    else {"expression": "无效配置"}
                                ),
                            }
                            for row in result["rows"]
                        ]
                    )
                else:
                    result = await self._work(
                        self.finance.listing,
                        action,
                        page=request.query.get("page", 1, type=int),
                        search=request.query.get("search", ""),
                        status=request.query.get("status", "all"),
                        denomination=request.query.get("denomination", ""),
                    )
                return json_response(result)
            raw = await request.body()
            if len(raw) > 256 * 1024:
                return error_response("提交内容过大，请分批保存。", status_code=413)
            try:
                payload = json.loads(raw)
            except (ValueError, UnicodeDecodeError) as exc:
                raise BillingError("请提交合法 JSON。") from exc
            if not isinstance(payload, dict):
                raise BillingError("提交内容必须是 JSON 对象。")
            operation = payload.get("operation_id")
            if action == "preview":
                model, source = payload.get("model", ""), payload.get("expression", "")
                if (
                    not isinstance(model, str)
                    or not isinstance(source, str)
                    or len(model) > 200
                    or len(source) > 6000
                ):
                    raise BillingError("模型 ID 或表达式长度不合法。")
                rule = {"model": model, "expression": source}
                if type(payload.get("id")) is int:
                    snapshot = await self._work(self.finance.prices)
                    for row in snapshot["rows"]:
                        original = row["rule"]
                        if (
                            row["id"] == payload["id"]
                            and isinstance(original, dict)
                            and original.get("model") == model
                        ):
                            existing = self.preview(original)
                            if existing.get("expression") == source:
                                rule = original
                            break
                result = await self._work(self.preview, rule)
            elif action == "save-prices":
                result = await self._work(
                    self.finance.save_prices,
                    actor,
                    operation,
                    payload.get("revision"),
                    payload.get("rows"),
                )
            elif action == "create-cards":
                result = await self._work(
                    self.finance.create_cards,
                    actor,
                    operation,
                    payload.get("amount"),
                    payload.get("count"),
                )
            elif action == "delete-cards":
                result = await self._work(
                    self.finance.delete_cards, actor, operation, payload.get("codes")
                )
            else:
                result = await self._work(
                    self.finance.set_balance,
                    actor,
                    operation,
                    payload.get("platform"),
                    payload.get("user_id"),
                    payload.get("expected"),
                    payload.get("amount"),
                    payload.get("reason"),
                )
            return json_response(result)
        except FinanceConflict as exc:
            return error_response(str(exc), status_code=409)
        except BillingError as exc:
            return error_response(str(exc), status_code=400)
        except sqlite3.Error:
            logger.exception("AIGen financial database operation failed")
            return error_response(
                "账本暂时不可用，请稍后刷新；重复提交会按请求编号去重。",
                status_code=503,
            )
        except Exception:  # noqa: BLE001
            logger.exception("AIGen financial WebUI request failed")
            return error_response("操作失败，请联系管理员检查日志。", status_code=500)
        finally:
            self.tasks.discard(task)
