"""Transactional balances, recharge cards and bounded pricing expressions."""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import operator
import secrets
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from .billing_expression import TOKENS, PricingExpression
from .billing_expression import BillingExpressionError as BillingError

SCALE = Decimal(1_000_000)
LIMIT = 10**15
REQUEST_NAMES = {
    "duration",
    "resolution",
    "quality",
    "size",
    "aspect_ratio",
    "voice",
    "speed",
    "characters",
    "images",
    "audios",
    "videos",
    "hour",
    "weekday",
    "media",
    "model",
}
USAGE_NAMES = {"seconds", "input_tokens", "output_tokens", "total_tokens"}
CURRENT: ContextVar = ContextVar("aigen_billing", default=None)
LOGGER = logging.getLogger(__name__)


def units(value) -> int:
    """Convert an amount into six-decimal fixed-point ledger units.

    Args:
        value: Nonnegative numeric amount.

    Returns:
        Integer ledger units.
    """
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount > LIMIT / SCALE:
            raise ValueError
        return int((amount * SCALE).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, TypeError, OverflowError) as exc:
        raise BillingError(
            "金额必须是 0 到 1000000000 之间的有限数字，最多保留六位小数。"
        ) from exc


def money(value: int) -> str:
    """Format ledger units without binary floating-point errors.

    Args:
        value: Integer fixed-point units.

    Returns:
        A decimal amount without redundant trailing zeroes.
    """
    return (
        format(Decimal(value) / SCALE, ".6f").rstrip("0").rstrip(".") if value else "0"
    )


class _LegacyExpression:
    """Evaluate a small numeric expression language without Python eval.

    Args:
        source: Arithmetic, comparisons, conditionals and min/max/ceil/floor.
    """

    def __init__(self, source: str):
        """Validate the expression's bounded syntax.

        Args:
            source: Administrator-configured price expression.

        Raises:
            BillingError: If syntax or operations are unsupported.
        """
        if not isinstance(source, str) or len(source) > 2000:
            raise BillingError("价格表达式为空或过长。")
        try:
            self.tree = ast.parse(source, mode="eval").body
        except (SyntaxError, RecursionError) as exc:
            raise BillingError("价格表达式语法错误。") from exc
        nodes = list(ast.walk(self.tree))
        allowed = (
            ast.Constant,
            ast.Name,
            ast.Load,
            ast.BinOp,
            ast.Add,
            ast.Sub,
            ast.Mult,
            ast.Div,
            ast.Mod,
            ast.UnaryOp,
            ast.USub,
            ast.UAdd,
            ast.Not,
            ast.BoolOp,
            ast.And,
            ast.Or,
            ast.Compare,
            ast.Eq,
            ast.NotEq,
            ast.Lt,
            ast.LtE,
            ast.Gt,
            ast.GtE,
            ast.IfExp,
            ast.Call,
        )
        if len(nodes) > 160 or any(not isinstance(n, allowed) for n in nodes):
            raise BillingError("价格表达式含不支持的操作或过于复杂。")
        self.names = set()
        for node in nodes:
            if isinstance(node, ast.Constant) and (
                type(node.value) not in (int, float, str, bool)
                or len(str(node.value)) > 100
            ):
                raise BillingError("价格表达式常量无效。")
            if isinstance(node, ast.Name):
                if node.id not in REQUEST_NAMES | USAGE_NAMES | {
                    "min",
                    "max",
                    "ceil",
                    "floor",
                }:
                    raise BillingError(f"未知计费变量：{node.id}")
                if node.id in REQUEST_NAMES | USAGE_NAMES:
                    self.names.add(node.id)
            if isinstance(node, ast.Call) and (
                not isinstance(node.func, ast.Name)
                or node.func.id not in {"min", "max", "ceil", "floor"}
                or node.keywords
                or not 1 <= len(node.args) <= 8
                or (node.func.id in {"ceil", "floor"} and len(node.args) != 1)
            ):
                raise BillingError("价格表达式函数无效。")

    def evaluate(self, values: dict) -> int:
        """Return a nonnegative amount, evaluating only the selected branches.

        Args:
            values: Validated request metadata and actual upstream usage.

        Returns:
            Rounded ledger units.
        """

        def visit(node):
            if isinstance(node, ast.Constant):
                return (
                    Decimal(str(node.value))
                    if type(node.value) in (int, float)
                    else node.value
                )
            if isinstance(node, ast.Name):
                if node.id not in values or values[node.id] is None:
                    raise BillingError(f"缺少实际计费用量：{node.id}")
                value = values[node.id]
                return (
                    Decimal(str(value))
                    if type(value) in (int, float, Decimal)
                    else value
                )
            if isinstance(node, ast.IfExp):
                return visit(node.body if visit(node.test) else node.orelse)
            if isinstance(node, ast.BoolOp):
                return (all if isinstance(node.op, ast.And) else any)(
                    visit(v) for v in node.values
                )
            if isinstance(node, ast.UnaryOp):
                value = visit(node.operand)
                if isinstance(node.op, ast.Not):
                    return not value
                return -value if isinstance(node.op, ast.USub) else +value
            if isinstance(node, ast.Compare):
                left = visit(node.left)
                for op, right_node in zip(node.ops, node.comparators):
                    right = visit(right_node)
                    compare = {
                        ast.Eq: operator.eq,
                        ast.NotEq: operator.ne,
                        ast.Lt: operator.lt,
                        ast.LtE: operator.le,
                        ast.Gt: operator.gt,
                        ast.GtE: operator.ge,
                    }
                    if not compare[type(op)](left, right):
                        return False
                    left = right
                return True
            if isinstance(node, ast.Call):
                args = [visit(arg) for arg in node.args]
                if node.func.id in {"min", "max"}:
                    return (min if node.func.id == "min" else max)(args)
                return args[0].to_integral_value(
                    rounding="ROUND_CEILING"
                    if node.func.id == "ceil"
                    else "ROUND_FLOOR"
                )
            if isinstance(node, ast.BinOp):
                left, right = visit(node.left), visit(node.right)
                if not isinstance(left, Decimal) or not isinstance(right, Decimal):
                    raise BillingError("价格表达式算术仅支持数字。")
                operations = {
                    ast.Add: lambda: left + right,
                    ast.Sub: lambda: left - right,
                    ast.Mult: lambda: left * right,
                    ast.Div: lambda: left / right,
                    ast.Mod: lambda: left % right,
                }
                result = operations[type(node.op)]()
                if not result.is_finite() or abs(result) > Decimal("1e18"):
                    raise BillingError("价格表达式计算越界。")
                return result
            raise BillingError("价格表达式无效。")

        try:
            return units(visit(self.tree))
        except (ArithmeticError, TypeError, AttributeError, RecursionError) as exc:
            raise BillingError("价格表达式计算失败，请检查类型和除数。") from exc


class Expression:
    """Use New API pricing syntax while reading previously saved Python rules.

    Args:
        source: Administrator expression text.
        token_mode: Divide New API token prices by one million at settlement.
    """

    def __init__(self, source: str, *, token_mode=False):
        self.source = source
        self.auto_units = token_mode is None
        self.token_mode = token_mode
        self.legacy = False
        try:
            self.parser = PricingExpression(source)
        except BillingError as original:
            try:
                self.parser = _LegacyExpression(source)
                self.legacy = True
            except BillingError:
                raise original from None
        self.names = self.parser.names
        if self.auto_units:
            self.token_mode = bool(self.names & (TOKENS - {"image_count"}))
        self.usage_keys = getattr(self.parser, "usage_keys", set())
        self.dynamic = bool(
            self.names & USAGE_NAMES
            or self.names & (TOKENS - {"image_count"})
            or self.usage_keys
            - {
                "requests",
                "resolution",
                "orientation",
                "characters",
                "quality",
                "image_count",
            }
        )

    def evaluate(self, values: dict) -> int:
        """Evaluate real currency cost with normalized token accounting.

        Args:
            values: Request and actual usage facts.

        Returns:
            Full integer fee, without a per-request cap.
        """
        if self.legacy:
            return self.parser.evaluate(values)
        context = dict(values)
        optional = {
            key: context.get(key, 0)
            for key in ("cr", "cc", "cc1h", "img", "img_cr", "img_o", "ai", "ao")
        }
        # Missing optional provider dimensions follow New API's zero default;
        # missing aggregate input/output usage remains unknown, never guessed.
        image_cache = optional["img_cr"] if "img_cr" in self.names else 0
        if image_cache and not 0 <= image_cache <= min(optional["cr"], optional["img"]):
            image_cache = 0
        optional["cr"] -= image_cache
        optional["img"] -= image_cache
        optional["img_cr"] = image_cache
        context.update(optional)
        raw_input, raw_output = (
            context.get("input_tokens"),
            context.get("output_tokens"),
        )
        context["len"] = raw_input
        if raw_input is not None:
            context["p"] = max(
                0,
                raw_input
                - sum(
                    optional[key]
                    for key in ("cr", "cc", "cc1h", "img", "img_cr", "ai")
                    if key in self.names
                ),
            )
        if raw_output is not None:
            context["c"] = max(
                0,
                raw_output
                - sum(optional[key] for key in ("img_o", "ao") if key in self.names),
            )
        context["_trace"] = {}
        if self.auto_units:
            context["_fixed_scale"] = 1_000_000 if self.token_mode else 1
        value, _tier = self.parser.evaluate_raw(context)
        return units(
            value / 1_000_000
            if self.token_mode
            or (not self.auto_units and context["_trace"].get("fixed"))
            else value
        )


class BillingStore:
    """Persist atomic balance, card and order changes in a private SQLite file."""

    def __init__(self, path: Path):
        """Open the ledger and retain interrupted orders for review.

        Args:
            path: Private database file inside the plugin data directory.
        """
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            statements = (
                "CREATE TABLE IF NOT EXISTS balances (owner TEXT PRIMARY KEY, amount INTEGER NOT NULL)",
                "CREATE TABLE IF NOT EXISTS cards (code TEXT PRIMARY KEY, amount INTEGER NOT NULL CHECK(amount > 0), creator TEXT NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, redeemed_by TEXT, redeemed_at TEXT)",
                "CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, owner TEXT NOT NULL, model TEXT NOT NULL, media TEXT NOT NULL, reserved INTEGER NOT NULL, charged INTEGER, status TEXT NOT NULL, details TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)",
                "CREATE TABLE IF NOT EXISTS ledger (id INTEGER PRIMARY KEY, owner TEXT NOT NULL, delta INTEGER NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)",
                "CREATE INDEX IF NOT EXISTS orders_owner ON orders(owner, created)",
            )
            for statement in statements:
                db.execute(statement)
            schema = db.execute(
                "SELECT sql FROM sqlite_master WHERE name='balances'"
            ).fetchone()[0]
            if "CHECK" in schema.upper():
                db.execute(
                    "ALTER TABLE balances RENAME TO balances_before_actual_billing"
                )
                db.execute(
                    "CREATE TABLE balances (owner TEXT PRIMARY KEY, amount INTEGER NOT NULL)"
                )
                db.execute(
                    "INSERT INTO balances SELECT owner,amount FROM balances_before_actual_billing"
                )
                db.execute("DROP TABLE balances_before_actual_billing")
            # Old holds and interrupted requests have no usable completed result.
            # Release them exactly once; never charge a late/recovered failure.
            for row in db.execute(
                "SELECT * FROM orders WHERE status IN ('reserved','running','review')"
            ).fetchall():
                db.execute(
                    "UPDATE balances SET amount=amount+? WHERE owner=?",
                    (row["reserved"], row["owner"]),
                )
                db.execute(
                    "UPDATE orders SET reserved=0,charged=0,status='refunded',note='升级/中断时未取得完整成功结果，本次不扣费' WHERE id=?",
                    (row["id"],),
                )
                db.execute(
                    "INSERT INTO ledger(owner,delta,kind,ref) VALUES(?,?,'refund',?)",
                    (row["owner"], row["reserved"], row["id"]),
                )
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS orders_one_running_owner ON orders(owner) WHERE status='running'"
            )
            if db.execute("PRAGMA user_version").fetchone()[0] < 2:
                db.execute("PRAGMA user_version=2")
        self.path.chmod(0o600)

    @contextmanager
    def transaction(self):
        """Serialize mutations across tasks, threads and database connections.

        Yields:
            A connection owning an immediate transaction.
        """
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def create_cards(self, creator: str, amount, count: int) -> list[str]:
        """Create a bounded batch of distinct, one-use cryptographic cards.

        Args:
            creator: Administrator balance identity.
            amount: Positive decimal denomination.
            count: Number of cards, from 1 to 100.

        Returns:
            The complete batch of redeemable codes.
        """
        value = units(amount)
        if value <= 0 or not 1 <= count <= 100:
            raise BillingError("面额必须大于 0，单次生成数量须为 1–100。")
        codes = []
        with self.transaction() as db:
            for _ in range(count):
                for _attempt in range(5):
                    code = "AG-" + secrets.token_hex(16).upper()
                    if db.execute(
                        "INSERT OR IGNORE INTO cards(code,amount,creator) VALUES(?,?,?)",
                        (code, value, creator),
                    ).rowcount:
                        codes.append(code)
                        break
                else:
                    raise BillingError("卡密随机数碰撞，请重试。")
        return codes

    def redeem(self, owner: str, code: str) -> int:
        """Redeem a card and credit its balance in one transaction.

        Args:
            owner: Credited balance identity.
            code: User-supplied recharge code.

        Returns:
            Credited integer units.

        Raises:
            BillingError: If the card is invalid, used or exceeds the balance cap.
        """
        with self.transaction() as db:
            row = db.execute(
                "SELECT amount FROM cards WHERE code=? AND redeemed_by IS NULL",
                (code.strip().upper(),),
            ).fetchone()
            if not row:
                raise BillingError("卡密无效或已兑换。")
            value = row[0]
            db.execute("INSERT OR IGNORE INTO balances VALUES(?,0)", (owner,))
            balance = db.execute(
                "SELECT amount FROM balances WHERE owner=?", (owner,)
            ).fetchone()[0]
            held = db.execute(
                "SELECT COALESCE(SUM(reserved),0) FROM orders WHERE owner=? AND status IN ('reserved','review')",
                (owner,),
            ).fetchone()[0]
            if balance + held + value > LIMIT:
                raise BillingError("余额超出上限，请联系管理员。")
            db.execute(
                "UPDATE cards SET redeemed_by=?,redeemed_at=CURRENT_TIMESTAMP WHERE code=?",
                (owner, code.strip().upper()),
            )
            db.execute(
                "UPDATE balances SET amount=amount+? WHERE owner=?", (value, owner)
            )
            # Do not duplicate redeemable secrets into the ledger.
            db.execute(
                "INSERT INTO ledger(owner,delta,kind,ref) VALUES(?,?,'recharge','card')",
                (owner, value),
            )
        return value

    def begin(
        self, owner: str, media: str, model: str, expected: int | None, details: dict
    ) -> str:
        """Register one charged task per user without freezing or capping funds.

        Args:
            owner: Charged balance identity.
            media: Requested media family.
            model: Final selected model.
            expected: Known fixed fee, or None for usage determined on completion.
            details: Price and request metadata without credentials.

        Returns:
            New accounting order ID.
        """
        order = secrets.token_hex(12)
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO balances VALUES(?,0)", (owner,))
            if db.execute(
                "SELECT 1 FROM orders WHERE owner=? AND status='running'", (owner,)
            ).fetchone():
                raise BillingError(
                    "该用户已有收费任务正在生成，请等待结束；免费模型不受影响。"
                )
            balance = db.execute(
                "SELECT amount FROM balances WHERE owner=?", (owner,)
            ).fetchone()[0]
            if balance <= 0 or (expected is not None and balance < expected):
                raise BillingError(
                    "用户余额不足；请发送“/AI余额”查询，或发送“/兑换AI卡密 xxxxxx”进行充值（中间记得加空格，否则充值失败，需迅速撤回重试）。"
                )
            db.execute(
                "INSERT INTO orders(id,owner,model,media,reserved,status,details) VALUES(?,?,?,?,0,'running',?)",
                (
                    order,
                    owner,
                    model,
                    media,
                    json.dumps(details, ensure_ascii=False, default=str),
                ),
            )
        return order

    def finish(
        self, order: str, charge: int | None, note: str, *, admin=False, evidence=None
    ):
        """Idempotently settle/refund or retain a hold for manual review.

        Args:
            order: Accounting order ID.
            charge: Final units, or None to retain funds for review.
            note: Human-readable accounting reason.
            admin: Require that the order is already awaiting review.
            evidence: Optional safe usage and task metadata.

        Returns:
            Whether a previously open order was changed.
        """
        with self.transaction() as db:
            row = db.execute("SELECT * FROM orders WHERE id=?", (order,)).fetchone()
            if not row:
                raise BillingError("订单不存在。")
            if row["status"] in {"settled", "refunded"}:
                return False
            if admin and row["status"] != "review":
                raise BillingError("仅能处理待核对订单，不能更改正在生成的订单。")
            if evidence is not None:
                details = json.loads(row["details"])
                details["result"] = evidence
                db.execute(
                    "UPDATE orders SET details=? WHERE id=?",
                    (json.dumps(details, ensure_ascii=False, default=str), order),
                )
            if charge is None:
                db.execute(
                    "UPDATE orders SET status='review',note=? WHERE id=?",
                    (note[:500], order),
                )
                return True
            if type(charge) is not int or charge < 0:
                raise BillingError("结算金额必须是非负整数单位。")
            refund = row["reserved"] - charge
            db.execute(
                "UPDATE balances SET amount=amount+? WHERE owner=?",
                (refund, row["owner"]),
            )
            db.execute(
                "UPDATE orders SET charged=?,status=?,note=? WHERE id=?",
                (charge, "settled" if charge else "refunded", note[:500], order),
            )
            db.execute(
                "INSERT INTO ledger(owner,delta,kind,ref) VALUES(?,?,'settle',?)",
                (row["owner"], refund, order),
            )
        return True

    def checkpoint(self, order: str, evidence: dict):
        """Persist submission/task evidence while keeping an active order locked.

        Args:
            order: Reserved order ID.
            evidence: Safe state, task ID and numeric usage; no request secrets.
        """
        with self.transaction() as db:
            row = db.execute(
                "SELECT details FROM orders WHERE id=? AND status='running'", (order,)
            ).fetchone()
            if row:
                details = json.loads(row[0])
                details["result"] = evidence
                db.execute(
                    "UPDATE orders SET details=? WHERE id=?",
                    (json.dumps(details, ensure_ascii=False, default=str), order),
                )

    def ensure_user(self, owner: str):
        """Create a zero-balance user without changing existing funds or history."""
        with self.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO balances(owner,amount) VALUES(?,0)", (owner,)
            )

    def balance(self, owner: str) -> tuple[int, int]:
        """Return available and frozen funds from a consistent snapshot.

        Args:
            owner: Balance identity.

        Returns:
            Available and frozen integer units.
        """
        with self.transaction() as db:
            row = db.execute(
                "SELECT amount FROM balances WHERE owner=?", (owner,)
            ).fetchone()
            held = db.execute(
                "SELECT COALESCE(SUM(reserved),0) FROM orders WHERE owner=? AND status IN ('reserved','review')",
                (owner,),
            ).fetchone()[0]
            return (row[0] if row else 0), held

    def listing(
        self, kind: str, owner: str = "", page: int = 1
    ) -> tuple[int, list[dict]]:
        """List paginated active cards or orders without cross-user leaks.

        Args:
            kind: Active cards, review orders or one user's orders.
            owner: Required identity for a user's order history.
            page: One-based page number.

        Returns:
            Total count and at most twenty rows.
        """
        if not 1 <= page <= 100000:
            raise BillingError("页码必须为 1–100000。")
        if kind == "cards":
            table, clause, args = "cards", "redeemed_by IS NULL", ()
        elif kind == "review":
            table, clause, args = "orders", "1=1", ()
        elif kind == "orders":
            table, clause, args = "orders", "owner=?", (owner,)
        else:
            raise BillingError("无效查询类型。")
        with self.transaction() as db:
            total = db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {clause}", args
            ).fetchone()[0]
            rows = db.execute(
                f"SELECT * FROM {table} WHERE {clause} ORDER BY rowid DESC LIMIT 20 OFFSET ?",
                (*args, (page - 1) * 20),
            ).fetchall()
        return total, [dict(row) for row in rows]


class BillingService:
    """Select exact model prices and produce immutable per-order quotes."""

    def __init__(self, path: Path, config: dict):
        """Construct the shared service.

        Args:
            path: Durable plugin database path.
            config: Global price configuration.
        """
        self.store = BillingStore(path)
        self.config = config
        self.tasks = set()
        self.closed = False
        self.unsettled = set()

    def recover_unsettled(self):
        """Release this instance's finished tasks after storage recovers.

        Raises:
            sqlite3.Error: If storage remains unavailable.
        """
        for order in tuple(self.unsettled):
            self.store.finish(order, 0, "结算数据库异常，本次不扣费")
            self.unsettled.discard(order)

    @staticmethod
    def owner(event) -> str:
        """Use platform type and AstrBot sender ID; share funds across groups.

        Args:
            event: AstrBot event exposing platform and sender identity.

        Returns:
            Collision-safe serialized balance identity.
        """
        platform = str(event.get_platform_name() or "").strip()
        uid = str(event.get_sender_id() or "").strip()
        if not uid or not platform:
            raise BillingError("无法识别 AstrBot 用户 ID，未提交生成。")
        return json.dumps([platform, uid], ensure_ascii=False, separators=(",", ":"))

    def register_user(self, event) -> str:
        """Register the trusted message sender, including free or query-only use.

        Args:
            event: Original AstrBot event injected by the command/tool runner.

        Returns:
            The account shared across this sender's groups and private chats.
        """
        owner = self.owner(event)
        try:
            self.store.ensure_user(owner)
        except sqlite3.Error as exc:
            raise BillingError("用户账本暂时不可用，请稍后重试或联系管理员。") from exc
        return owner

    def quote(self, media: str, config: dict, counts: dict):
        """Find an exact model rule; an unpriced model is always free.

        Args:
            media: Requested media family.
            config: Effective route and normalized request parameters.
            counts: Text and material counts.

        Returns:
            Expression, metadata, reservation and rule; None for free requests.
        """
        model = str(config.get("model", ""))
        matches = [
            rule
            for rule in (
                self.finance.rules()
                if hasattr(self, "finance")
                else self.config.get("billing_prices", [])
            )
            if isinstance(rule, dict)
            and str(rule.get("model", "")).strip() == model
            and rule.get("media", "all") in {"all", media}
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise BillingError(f"模型 {model} 有重复计费规则，请管理员修正。")
        rule = matches[0]
        now = datetime.now(timezone.utc)
        values = {
            name: config.get(name, "")
            for name in ("resolution", "quality", "size", "aspect_ratio", "voice")
        }
        if media == "audio":
            voice_field = {
                "minimax_speech": "minimax_voice",
                "gemini_official": "gemini_voice",
                "dashscope": "dashscope_voice",
            }.get(config.get("interface_mode"), "voice")
            values["voice"] = config.get(
                voice_field,
                {"openai_speech": "alloy", "minimax_speech": "male-qn-qingse"}.get(
                    config.get("interface_mode"), ""
                ),
            )
        values.update(
            duration=config.get("duration", 0) or 0,
            speed=config.get("speed", 1),
            model=model,
            media=media,
            hour=now.hour,
            weekday=now.weekday(),
            **counts,
        )
        from .model_prices import compile_price

        try:
            expression = compile_price(rule)
        except BillingError as exc:
            raise BillingError("价格设置不合法，请联系管理员配置模型价格。") from exc
        if getattr(expression, "free", False):
            return None
        request = {
            key: value
            for key, value in config.items()
            if key in REQUEST_NAMES
            or key
            in {
                "generate_audio",
                "seed",
                "emotion",
                "service_tier",
                "draft",
                "watermark",
                "n",
                "response_format",
                "negative_prompt",
            }
        }
        request.update(
            {key: value for key, value in values.items() if key in REQUEST_NAMES}
        )
        request.update(seconds=values["duration"], ratio=values["aspect_ratio"])
        request["parameters"] = dict(request)
        values["_request"] = request
        values["_now"] = now.astimezone(timezone.utc).isoformat()
        values["image_count"] = config.get("n", 1) or 1
        # These are the AutoDL usage-profile facts validated by the selected route.
        ratio = str(values["aspect_ratio"])
        orientation = (
            "square"
            if ratio == "1:1"
            else "portrait"
            if ratio in {"9:16", "3:4", "4:5"}
            else "landscape"
            if ratio and ratio != "adaptive"
            else ""
        )
        facts = {"requests": 1, "characters": values.get("characters", 0)}
        facts["image_count"] = values["image_count"]
        if values["quality"]:
            facts["quality"] = values["quality"]
        if values["resolution"]:
            facts["resolution"] = values["resolution"]
        if orientation:
            facts["orientation"] = orientation
        values["_usage"] = facts
        if (
            not expression.legacy
            and "header" in expression.parser.functions
            and "_headers" not in values
        ):
            raise BillingError("价格设置不合法，请联系管理员配置模型价格。")
        try:
            expected = None if expression.dynamic else expression.evaluate(values)
        except BillingError as exc:
            raise BillingError("价格设置不合法，请联系管理员配置模型价格。") from exc
        if expected == 0:
            return None
        return expression, values, expected, dict(rule)


class Usage:
    """Track one task's usage without mutable metrics shared across requests."""

    def __init__(self, store=None, order=""):
        """Create request-local state with optional durable checkpoints.

        Args:
            store: Owning ledger, or None for isolated parser checks.
            order: Accounting order ID.
        """
        self.state = "new"
        self.values = {}
        self.task_id = ""
        self.store, self.order = store, order
        self.saved = ""

    def response(self, data):
        """Read documented token/duration fields without estimating missing usage.

        Args:
            data: Parsed provider response or cumulative SSE event.
        """
        if not isinstance(data, dict):
            return
        for nested in (
            data,
            data.get("response"),
            data.get("data"),
            data.get("output"),
            data.get("task"),
        ):
            if not isinstance(nested, dict):
                continue
            self.task_id = str(
                nested.get("task_id") or nested.get("id") or self.task_id
            )[:200]
            usage = nested.get("usage") or nested.get("usageMetadata") or {}
            if isinstance(usage, dict):
                mapping = {
                    "input_tokens": (
                        "input_tokens",
                        "prompt_tokens",
                        "promptTokenCount",
                    ),
                    "output_tokens": (
                        "output_tokens",
                        "completion_tokens",
                        "candidatesTokenCount",
                    ),
                    "total_tokens": ("total_tokens", "totalTokenCount"),
                    "seconds": ("video_duration", "audio_duration", "duration_seconds"),
                    "image_count": ("image_count",),
                }
                for target, names in mapping.items():
                    for name in names:
                        value = usage.get(name)
                        if type(value) in (int, float) and 0 <= value <= 1e12:
                            self.values[target] = value
                            break
                if "candidatesTokenCount" in usage:
                    thoughts = usage.get("thoughtsTokenCount", 0)
                    candidates = usage["candidatesTokenCount"]
                    if (
                        type(candidates) in (int, float)
                        and type(thoughts) in (int, float)
                        and 0 <= candidates <= 1e12
                        and 0 <= thoughts <= 1e12
                    ):
                        self.values["output_tokens"] = candidates + thoughts
                input_details = (
                    usage.get("input_tokens_details")
                    or usage.get("prompt_tokens_details")
                    or {}
                )
                output_details = (
                    usage.get("output_tokens_details")
                    or usage.get("completion_tokens_details")
                    or {}
                )
                input_details = input_details if isinstance(input_details, dict) else {}
                output_details = (
                    output_details if isinstance(output_details, dict) else {}
                )
                optional = {
                    "cr": input_details.get(
                        "cached_tokens", usage.get("cachedContentTokenCount")
                    ),
                    "cc": usage.get("cache_creation_input_tokens"),
                    "img": input_details.get("image_tokens"),
                    "ai": input_details.get("audio_tokens"),
                    "img_o": output_details.get("image_tokens"),
                    "ao": output_details.get("audio_tokens"),
                }
                cached = input_details.get("cached_tokens_details") or {}
                if isinstance(cached, dict):
                    optional["img_cr"] = cached.get("image_tokens")
                for name, value in optional.items():
                    if type(value) in (int, float) and 0 <= value <= 1e12:
                        self.values[name] = value
            # MiniMax speech audio_length is documented in milliseconds.
            extra = nested.get("extra_info")
            if (
                isinstance(extra, dict)
                and type(extra.get("audio_length")) in (int, float)
                and 0 <= extra["audio_length"] <= 1e12
            ):
                self.values["seconds"] = extra["audio_length"] / 1000
        if "input_tokens" in self.values and "output_tokens" in self.values:
            # A later cumulative SSE event replaces the previous counts.
            self.values["total_tokens"] = (
                self.values["input_tokens"] + self.values["output_tokens"]
            )


def track(*, state: str = "", payload=None, raw: str = "", status: int = 0):
    """Update only the current billed task from an adapter's request boundary.

    Args:
        state: Explicit submission, completion or failure state.
        payload: Optional parsed response.
        raw: Optional JSON/SSE response text.
        status: Submission HTTP status; polling errors leave the hold open.
    """
    usage = CURRENT.get()
    if usage is None:
        return
    if state:
        usage.state = state
    if status and 400 <= status < 500 and status != 408:
        usage.state = "failed"
    if payload is not None:
        usage.response(payload)
    if raw:
        try:
            usage.response(json.loads(raw))
        except (ValueError, TypeError):
            for line in raw.splitlines():
                if line.startswith("data:"):
                    try:
                        usage.response(json.loads(line[5:].strip()))
                    except (ValueError, TypeError):
                        pass
    if usage.store:
        evidence = {
            "state": usage.state,
            "task_id": usage.task_id,
            "usage": usage.values,
        }
        snapshot = json.dumps(evidence, sort_keys=True)
        if snapshot != usage.saved:
            try:
                usage.store.checkpoint(usage.order, evidence)
            except sqlite3.Error:
                # An evidence write must not abandon an already submitted job.
                # Final settlement still validates and persists the full result.
                LOGGER.exception("Could not save billing checkpoint %s", usage.order)
            else:
                usage.saved = snapshot


async def billed_call(
    service,
    event,
    media: str,
    config: dict,
    counts: dict,
    callback,
    *,
    before_submit=None,
):
    """Reserve, invoke once and settle independently of artifact delivery.

    Args:
        service: Billing service, or None in isolated adapter tests.
        event: AstrBot user identity.
        media: Selected media family.
        config: Effective route/model and parameters.
        counts: Input character and material counts.
        callback: Coroutine factory issuing the single generation operation.
        before_submit: Optional progress sender, invoked only after price checks.

    Returns:
        The original adapter result.
    """
    if service and service.closed:
        raise BillingError("插件正在重载，请稍后重试；未提交生成任务。")
    # The LLM cannot select an owner. Always use the invoking message event,
    # never the chat/group ID, quoted sender, material owner or model arguments.
    owner = service.register_user(event) if service else ""
    quote = service.quote(media, config, counts) if service else None
    if quote is None:
        task = asyncio.current_task()
        if service:
            service.tasks.add(task)
        try:
            if before_submit:
                await before_submit()
            return await callback()
        finally:
            if service:
                service.tasks.discard(task)
    service.recover_unsettled()
    expression, values, expected, rule = quote
    order = service.store.begin(
        owner,
        media,
        str(config["model"]),
        expected,
        {"rule": rule, "request": values},
    )
    usage = Usage(service.store, order)
    task = asyncio.current_task()
    service.tasks.add(task)
    token = CURRENT.set(usage)
    result, error = None, None
    try:
        if before_submit:
            await before_submit()
        result = await callback()
        if isinstance(result, bytes) or getattr(result, "path", None):
            usage.state = "completed"
    except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001
        error = exc
    finally:
        CURRENT.reset(token)
    try:
        note = ""
        charge = None
        facts = {}
        if usage.state == "completed" and not isinstance(error, asyncio.CancelledError):
            # Probe only local completed artifacts; never download again for billing.
            path = getattr(result, "path", None)
            if path and (
                "seconds" in expression.names
                or expression.usage_keys & {"seconds", "actual_seconds"}
            ):
                try:
                    process = await asyncio.create_subprocess_exec(
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "default=noprint_wrappers=1:nokey=1",
                        str(path),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    try:
                        output, _ = await asyncio.wait_for(process.communicate(), 10)
                        seconds = Decimal(output.decode().strip())
                        if (
                            process.returncode == 0
                            and seconds.is_finite()
                            and seconds > 0
                        ):
                            usage.values["seconds"] = seconds
                    finally:
                        if process.returncode is None:
                            process.kill()
                            await process.wait()
                except (OSError, ValueError, InvalidOperation, asyncio.TimeoutError):
                    pass
                except asyncio.CancelledError as exc:
                    error = exc
            try:
                if isinstance(error, asyncio.CancelledError):
                    raise BillingError("生成在结算前取消，本次不扣费")
                facts = {**values.get("_usage", {}), **usage.values}
                if "total_tokens" in usage.values:
                    facts["tokens"] = usage.values["total_tokens"]
                if "seconds" in usage.values:
                    facts["actual_seconds"] = usage.values["seconds"]
                    if str(config["model"]) in {"IndexTTS-2", "indextts2-v1"}:
                        facts["seconds"] = max(10, usage.values["seconds"])
                charge = expression.evaluate(
                    {**values, **usage.values, "_usage": facts}
                )
                note = "成功，按实际用量扣费 " + money(charge)
                if (
                    facts.get("seconds") != usage.values.get("seconds")
                    and "seconds" in facts
                ):
                    note += f"；AutoDL 最低计费规则：实际 {usage.values['seconds']} 秒，计费 {facts['seconds']} 秒"
            except BillingError as exc:
                charge, note = 0, str(exc) + "；无可核验实际费用，本次不扣费"
        else:
            charge, note = 0, "生成失败、取消或未返回完整成功结果，本次不扣费"
        if usage.task_id:
            note += f"；上游任务 {usage.task_id}"
        try:
            service.store.finish(
                order,
                charge,
                note,
                evidence={
                    "usage": usage.values,
                    "billing_usage": facts,
                    "state": usage.state,
                    "task_id": usage.task_id,
                    "path": str(getattr(result, "path", "")),
                },
            )
        except sqlite3.Error:
            # Deliver the artifact and recover this exact finished order when
            # storage becomes available. Never release another active task.
            service.unsettled.add(order)
            LOGGER.exception(
                "Could not settle billing order %s; no charge recorded", order
            )
            charge = None
        if charge is None:
            LOGGER.warning(
                "Billing order %s could not be settled; no charge recorded", order
            )

    finally:
        service.tasks.discard(task)
    if error is not None:
        raise error
    return result
