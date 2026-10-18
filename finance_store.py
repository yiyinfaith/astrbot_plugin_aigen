"""Transactional WebUI management for prices, cards and the shared ledger."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from contextlib import closing
from decimal import Decimal, InvalidOperation

from .billing import LIMIT, SCALE, BillingError, money, units


class FinanceConflict(BillingError):
    """The displayed financial state changed before the requested edit."""


class FinanceStore:
    def __init__(self, store, legacy_prices=None):
        """Extend the existing ledger without validating price expressions.

        Args:
            store: The already initialized BillingStore, never a second instance.
            legacy_prices: Existing config rules to import exactly once.
        """
        self.store = store
        with store.transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS finance_settings (key TEXT PRIMARY KEY,value TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS model_prices (id INTEGER PRIMARY KEY,rule TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS admin_operations (id TEXT PRIMARY KEY,actor TEXT NOT NULL,kind TEXT NOT NULL,request_hash TEXT NOT NULL,result TEXT NOT NULL,created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            if not db.execute(
                "SELECT 1 FROM finance_settings WHERE key='prices_imported'"
            ).fetchone():
                for rule in legacy_prices if isinstance(legacy_prices, list) else []:
                    db.execute(
                        "INSERT INTO model_prices(rule) VALUES(?)",
                        (json.dumps(rule, ensure_ascii=False),),
                    )
                db.execute("INSERT INTO finance_settings VALUES('prices_imported','1')")
                db.execute(
                    "INSERT OR IGNORE INTO finance_settings VALUES('price_revision','0')"
                )
            db.execute("PRAGMA user_version=3")

    def rules(self):
        """Read the effective rules for one new generation.

        Returns:
            An independent list preserving imported price units.
        """
        with closing(sqlite3.connect(self.store.path, timeout=10)) as db:
            return [
                json.loads(row[0])
                for row in db.execute("SELECT rule FROM model_prices ORDER BY id")
            ]

    def prices(self):
        """Read a consistent editor snapshot.

        Returns:
            The current revision and complete rule list.
        """
        with closing(sqlite3.connect(self.store.path, timeout=10)) as db:
            db.execute("BEGIN")
            revision = int(
                db.execute(
                    "SELECT value FROM finance_settings WHERE key='price_revision'"
                ).fetchone()[0]
            )
            rows = [
                {"id": row[0], "rule": json.loads(row[1])}
                for row in db.execute("SELECT id,rule FROM model_prices ORDER BY id")
            ]
            return {"revision": revision, "rows": rows}

    def _mutate(self, actor, operation, kind, payload, change):
        """Deduplicate all financial writes in the same database transaction.

        Args:
            actor: Trusted Dashboard identity.
            operation: Client retry identifier.
            kind: Operation family.
            payload: Validated parameters without the retry identifier.
            change: Transaction callback returning the confirmed result.

        Returns:
            The original result for this exact operation.

        Raises:
            FinanceConflict: If an identifier is reused for a different operation.
        """
        if not isinstance(operation, str) or not 16 <= len(operation) <= 128:
            raise BillingError("请求编号不合法，请刷新页面后重试。")
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        with self.store.transaction() as db:
            existing = db.execute(
                "SELECT * FROM admin_operations WHERE id=?", (operation,)
            ).fetchone()
            if existing:
                if (
                    existing["actor"] != actor
                    or existing["kind"] != kind
                    or existing["request_hash"] != digest
                ):
                    raise FinanceConflict("请求编号已用于其他操作，请刷新页面。")
                return json.loads(existing["result"])
            result = change(db)
            db.execute(
                "INSERT INTO admin_operations(id,actor,kind,request_hash,result) VALUES(?,?,?,?,?)",
                (
                    operation,
                    actor,
                    kind,
                    digest,
                    json.dumps(result, ensure_ascii=False),
                ),
            )
            return result

    def save_prices(self, actor, operation, revision, rows):
        """Save model IDs and expressions without requiring valid expressions.

        Args:
            actor: Trusted administrator.
            operation: Retry identifier.
            revision: Expected editor revision.
            rows: Complete editor rows, each containing model and expression.

        Returns:
            The confirmed new revision.

        Raises:
            FinanceConflict: If another editor changed the prices first.
        """
        if type(revision) is not int or not isinstance(rows, list) or len(rows) > 500:
            raise BillingError("模型价格参数不合法。")
        validated, models = [], set()
        for row in rows:
            if not isinstance(row, dict):
                raise BillingError("模型价格必须包含模型 ID 和表达式。")
            model, expression = row.get("model"), row.get("expression")
            if (
                not isinstance(model, str)
                or not model.strip()
                or len(model) > 200
                or not isinstance(expression, str)
                or len(expression) > 6000
                or (row.get("id") is not None and type(row.get("id")) is not int)
            ):
                raise BillingError("模型 ID 须为 1–200 字符，表达式最多 6000 字符。")
            model = model.strip()
            if model in models:
                raise BillingError("模型 ID 重复，请每个模型只保留一条规则。")
            models.add(model)
            validated.append(
                {"id": row.get("id"), "model": model, "expression": expression.strip()}
            )

        def change(db):
            current = int(
                db.execute(
                    "SELECT value FROM finance_settings WHERE key='price_revision'"
                ).fetchone()[0]
            )
            if current != revision:
                raise FinanceConflict("价格已被其他页面修改，请刷新后再保存。")
            originals = {
                row[0]: json.loads(row[1])
                for row in db.execute("SELECT id,rule FROM model_prices")
            }
            from .model_prices import compile_price

            saved = []
            for row in validated:
                original = originals.get(row["id"])
                if (
                    isinstance(original, dict)
                    and str(original.get("model", "")).strip() == row["model"]
                ):
                    try:
                        source = compile_price(original).source
                    except BillingError:
                        source = original.get("expression", "")
                    if source == row["expression"]:
                        saved.append({**original, "model": row["model"]})
                        continue
                saved.append({"model": row["model"], "expression": row["expression"]})
            db.execute("DELETE FROM model_prices")
            for rule in saved:
                db.execute(
                    "INSERT INTO model_prices(rule) VALUES(?)",
                    (json.dumps(rule, ensure_ascii=False),),
                )
            db.execute(
                "UPDATE finance_settings SET value=? WHERE key='price_revision'",
                (str(current + 1),),
            )
            return {"saved": True, "revision": current + 1, "models": len(saved)}

        return self._mutate(
            actor,
            operation,
            "prices",
            {"revision": revision, "rows": validated},
            change,
        )

    def create_cards(self, actor, operation, amount, count):
        """Generate a batch once, even if a browser retries after a lost reply.

        Args:
            actor: Trusted administrator.
            operation: Retry identifier.
            amount: Positive denomination in balance units.
            count: Batch size, from one to one hundred.

        Returns:
            Generated codes and their denomination.
        """
        value = units(amount)
        if not value or type(count) is not int or not 1 <= count <= 100:
            raise BillingError("面额须大于 0，每批生成 1–100 张卡密。")

        def change(db):
            codes = []
            for _ in range(count):
                while True:
                    code = "AIGEN-" + secrets.token_hex(12).upper()
                    if not db.execute(
                        "SELECT 1 FROM cards WHERE code=?", (code,)
                    ).fetchone():
                        break
                db.execute(
                    "INSERT INTO cards(code,amount,creator) VALUES(?,?,?)",
                    (code, value, actor),
                )
                codes.append(code)
            return {"codes": codes, "amount": money(value), "count": count}

        return self._mutate(
            actor, operation, "create_cards", {"amount": value, "count": count}, change
        )

    def delete_cards(self, actor, operation, codes):
        """Remove redeemability while retaining recharge history.

        Args:
            actor: Trusted administrator.
            operation: Retry identifier.
            codes: Selected active codes, at most one hundred.

        Returns:
            The confirmed deleted count.

        Raises:
            FinanceConflict: If a selected card was redeemed or deleted meanwhile.
        """
        if (
            not isinstance(codes, list)
            or not 1 <= len(codes) <= 100
            or any(not isinstance(code, str) or len(code) > 128 for code in codes)
        ):
            raise BillingError("请选择 1–100 张有效卡密。")
        codes = sorted({code.strip().upper() for code in codes})

        def change(db):
            for code in codes:
                row = db.execute(
                    "SELECT redeemed_by FROM cards WHERE code=?", (code,)
                ).fetchone()
                if not row or row[0] is not None:
                    raise FinanceConflict("选中的卡密已兑换或删除，请刷新列表。")
            for code in codes:
                db.execute(
                    "UPDATE cards SET redeemed_by='[deleted]',redeemed_at=CURRENT_TIMESTAMP WHERE code=? AND redeemed_by IS NULL",
                    (code,),
                )
            return {"deleted": len(codes)}

        return self._mutate(actor, operation, "delete_cards", {"codes": codes}, change)

    def set_balance(
        self, actor, operation, platform, user_id, expected, amount, reason
    ):
        """Set a balance atomically and reject edits based on stale amounts.

        Args:
            actor: Trusted administrator.
            operation: Retry identifier.
            platform: AstrBot platform type.
            user_id: AstrBot sender ID.
            expected: The balance displayed when editing began.
            amount: Requested new balance, including an allowed debt.
            reason: Administrator's explanation.

        Returns:
            Before/after amounts and the balance owner.

        Raises:
            FinanceConflict: If charging, redemption or another edit changed it.
        """
        if (
            not isinstance(platform, str)
            or not isinstance(user_id, str)
            or not platform.strip()
            or not user_id.strip()
            or len(platform) > 100
            or len(user_id) > 200
            or not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > 500
        ):
            raise BillingError("请填写平台、用户 ID 和修改原因。")
        try:
            numbers = [Decimal(str(value)) for value in (expected, amount)]
            if any(
                not n.is_finite()
                or abs(n * SCALE) > LIMIT
                or n * SCALE != (n * SCALE).to_integral_value()
                for n in numbers
            ):
                raise ValueError
            old, new = [int(n * SCALE) for n in numbers]
        except (ValueError, InvalidOperation, TypeError) as exc:
            raise BillingError("余额须为有限数字，最多六位小数。") from exc
        owner = json.dumps(
            [platform.strip(), user_id.strip()],
            ensure_ascii=False,
            separators=(",", ":"),
        )

        def change(db):
            row = db.execute(
                "SELECT amount FROM balances WHERE owner=?", (owner,)
            ).fetchone()
            current = row[0] if row else 0
            if current != old:
                raise FinanceConflict(
                    "用户余额已变动，请刷新后再修改，避免覆盖充值或扣费。"
                )
            db.execute(
                "INSERT INTO balances(owner,amount) VALUES(?,?) ON CONFLICT(owner) DO UPDATE SET amount=excluded.amount",
                (owner, new),
            )
            db.execute(
                "INSERT INTO ledger(owner,delta,kind,ref) VALUES(?,?,'admin_adjust',?)",
                (owner, new - old, operation),
            )
            return {
                "owner": owner,
                "before": money(old),
                "after": money(new),
                "delta": money(new - old),
                "reason": reason.strip(),
            }

        return self._mutate(
            actor,
            operation,
            "balance",
            {"owner": owner, "expected": old, "amount": new, "reason": reason.strip()},
            change,
        )

    def listing(
        self, kind, *, page=1, page_size=30, search="", status="all", denomination=""
    ):
        """Read paginated operational records with parameterized filters.

        Args:
            kind: Cards, users, redemptions, orders or administrator audit.
            page: One-based page number.
            page_size: Ten, thirty, fifty, one hundred, two hundred, or all rows.
            search: User/model/code search text, or an exact card denomination.
            status: Card state or an order state.
            denomination: Optional card denomination.

        Returns:
            Total count and the requested page, or every matching row for all.
        """
        if (
            type(page) is not int
            or not 1 <= page <= 100000
            or not isinstance(search, str)
            or len(search) > 200
        ):
            raise BillingError("查询参数不合法。")
        if isinstance(page_size, str) and page_size.isascii() and page_size.isdecimal():
            page_size = int(page_size) if len(page_size) <= 3 else None
        if page_size != "all" and (
            type(page_size) is not int or page_size not in {10, 30, 50, 100, 200}
        ):
            raise BillingError("每页数量须为 10、30、50、100、200 或全部。")
        if page_size == "all":
            page = 1
        args, filters = [], []
        search_amount = None
        if kind in {"cards", "redemptions"}:
            table = "cards"
            if kind == "redemptions":
                filters.append(
                    "redeemed_by IS NOT NULL AND redeemed_by NOT IN ('[revoked]','[deleted]')"
                )
            elif status == "active":
                filters.append("redeemed_by IS NULL")
            elif status == "redeemed":
                filters.append(
                    "redeemed_by IS NOT NULL AND redeemed_by NOT IN ('[revoked]','[deleted]')"
                )
            elif status == "deleted":
                filters.append("redeemed_by IN ('[revoked]','[deleted]')")
            elif status != "all":
                raise BillingError("卡密状态不合法。")
            if denomination:
                filters.append("amount=?")
                args.append(units(denomination))
            if search:
                try:
                    search_amount = units(search.strip())
                except BillingError:
                    pass
                filters.append(
                    "(code LIKE ? ESCAPE '\\' OR redeemed_by LIKE ? ESCAPE '\\'"
                    + (" OR amount=?" if search_amount is not None else "")
                    + ")"
                )
        elif kind == "users":
            table = "balances"
            if status == "negative":
                filters.append("amount < 0")
            elif status != "all":
                raise BillingError("余额筛选不合法。")
            if search:
                filters.append("owner LIKE ? ESCAPE '\\'")
        elif kind == "orders":
            table = "orders"
            if status != "all":
                if status not in {
                    "running",
                    "settled",
                    "refunded",
                    "review",
                    "reserved",
                }:
                    raise BillingError("订单状态不合法。")
                filters.append("status=?")
                args.append(status)
            if search:
                filters.append(
                    "(owner LIKE ? ESCAPE '\\' OR model LIKE ? ESCAPE '\\' OR id LIKE ? ESCAPE '\\')"
                )
        elif kind == "audit":
            table = "admin_operations"
            if search:
                filters.append(
                    "(actor LIKE ? ESCAPE '\\' OR result LIKE ? ESCAPE '\\')"
                )
        else:
            raise BillingError("记录类型不合法。")
        if search:
            escaped = (
                "%"
                + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                + "%"
            )
            args.extend([escaped] * filters[-1].count("LIKE"))
            if search_amount is not None:
                args.append(search_amount)
        clause = " AND ".join(filters) or "1=1"
        with closing(sqlite3.connect(self.store.path, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            total = db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {clause}", args
            ).fetchone()[0]
            query = f"SELECT * FROM {table} WHERE {clause} ORDER BY rowid DESC"
            row_args = tuple(args)
            if page_size != "all":
                query += " LIMIT ? OFFSET ?"
                row_args += (page_size, (page - 1) * page_size)
            rows = [dict(row) for row in db.execute(query, row_args)]
        for row in rows:
            for key in ("amount", "charged", "reserved", "delta"):
                if row.get(key) is not None:
                    row[key] = money(row[key])
            owner = row.get("owner") or row.get("redeemed_by") or ""
            try:
                platform, user_id = json.loads(owner)
                if not isinstance(platform, str) or not isinstance(user_id, str):
                    raise TypeError
            except (ValueError, TypeError):
                platform, user_id = "", owner
            row.update(platform=platform, user_id=user_id)
            if kind == "audit":
                result = json.loads(row["result"])
                row["result"] = {
                    key: result[key]
                    for key in (
                        "owner",
                        "before",
                        "after",
                        "delta",
                        "reason",
                        "amount",
                        "count",
                        "deleted",
                        "revision",
                        "models",
                    )
                    if key in result
                }
                row.pop("request_hash", None)
        return {"total": total, "page": page, "page_size": page_size, "rows": rows}

    def overview(self):
        """Read denominations and operational totals from one snapshot.

        Returns:
            Totals, card categories and active task count.
        """
        with closing(sqlite3.connect(self.store.path, timeout=10)) as db:
            db.execute("BEGIN")
            groups = [
                {
                    "amount": money(row[0]),
                    "active": row[1],
                    "redeemed": row[2],
                    "deleted": row[3],
                }
                for row in db.execute(
                    "SELECT amount,SUM(redeemed_by IS NULL),SUM(redeemed_by IS NOT NULL AND redeemed_by NOT IN ('[revoked]','[deleted]')),SUM(redeemed_by IN ('[revoked]','[deleted]')) FROM cards GROUP BY amount ORDER BY amount"
                )
            ]
            return {
                "categories": groups,
                "users": db.execute("SELECT COUNT(*) FROM balances").fetchone()[0],
                "models": db.execute("SELECT COUNT(*) FROM model_prices").fetchone()[0],
                "charged": money(
                    db.execute(
                        "SELECT COALESCE(SUM(charged),0) FROM orders WHERE status='settled'"
                    ).fetchone()[0]
                ),
                "running": db.execute(
                    "SELECT COUNT(*) FROM orders WHERE status='running'"
                ).fetchone()[0],
            }
