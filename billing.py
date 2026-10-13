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
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


class BillingError(ValueError):
    """A pricing or accounting operation cannot proceed safely."""


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


class Expression:
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
            db.executescript("""
                CREATE TABLE IF NOT EXISTS balances (owner TEXT PRIMARY KEY, amount INTEGER NOT NULL CHECK(amount >= 0));
                CREATE TABLE IF NOT EXISTS cards (code TEXT PRIMARY KEY, amount INTEGER NOT NULL CHECK(amount > 0), creator TEXT NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, redeemed_by TEXT, redeemed_at TEXT);
                CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, owner TEXT NOT NULL, model TEXT NOT NULL, media TEXT NOT NULL, reserved INTEGER NOT NULL, charged INTEGER, status TEXT NOT NULL, details TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS ledger (id INTEGER PRIMARY KEY, owner TEXT NOT NULL, delta INTEGER NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE INDEX IF NOT EXISTS orders_owner ON orders(owner, created);
            """)
            # Hot reload waits for running tasks before constructing this store.
            db.execute(
                "UPDATE orders SET status='review', note='进程中断或热重载，需核对上游任务' WHERE status='reserved'"
            )
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

    def reserve(
        self, owner: str, media: str, model: str, amount: int, details: dict
    ) -> str:
        """Freeze funds before generation, preventing concurrent overspending.

        Args:
            owner: Charged balance identity.
            media: Image, video or audio family.
            model: Final selected model ID.
            amount: Positive fixed-point reservation.
            details: Immutable price and request metadata without secrets.

        Returns:
            New accounting order ID.

        Raises:
            BillingError: If funds are insufficient or the amount is invalid.
        """
        if type(amount) is not int or not 0 < amount <= LIMIT:
            raise BillingError("预留金额无效。")
        order = secrets.token_hex(12)
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO balances VALUES(?,0)", (owner,))
            if not db.execute(
                "UPDATE balances SET amount=amount-? WHERE owner=? AND amount>=?",
                (amount, owner, amount),
            ).rowcount:
                raise BillingError(
                    f"余额不足，本次需预留 {money(amount)}；请 /余额 查询，或 /兑换卡密 <卡密> 充值。"
                )
            db.execute(
                "INSERT INTO orders(id,owner,model,media,reserved,status,details) VALUES(?,?,?,?,?,'reserved',?)",
                (
                    order,
                    owner,
                    model,
                    media,
                    amount,
                    json.dumps(details, ensure_ascii=False),
                ),
            )
            db.execute(
                "INSERT INTO ledger(owner,delta,kind,ref) VALUES(?,?,'reserve',?)",
                (owner, -amount, order),
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
            if charge < 0 or charge > row["reserved"]:
                raise BillingError("结算金额不能超过已冻结额度。")
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
                "SELECT details FROM orders WHERE id=? AND status='reserved'", (order,)
            ).fetchone()
            if row:
                details = json.loads(row[0])
                details["result"] = evidence
                db.execute(
                    "UPDATE orders SET details=? WHERE id=?",
                    (json.dumps(details, ensure_ascii=False, default=str), order),
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
            table, clause, args = "orders", "status='review'", ()
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
            for rule in self.config.get("billing_prices", [])
            if isinstance(rule, dict)
            and str(rule.get("model", "")).strip() == model
            and rule.get("media", "all") in {"all", media}
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise BillingError(f"模型 {model} 有重复计费规则，请管理员修正。")
        rule = matches[0]
        try:
            now = datetime.now(
                ZoneInfo(str(self.config.get("billing_timezone", "Asia/Shanghai")))
            )
        except ZoneInfoNotFoundError as exc:
            raise BillingError("计费时区无效，请管理员修正。") from exc
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
        mode = rule.get("mode", "request")
        if mode == "expression":
            expression = Expression(str(rule.get("expression", "0")))
        elif mode == "tokens":
            input_price, output_price = (
                units(rule.get("input_price", "0")),
                units(rule.get("output_price", "0")),
            )
            pieces = []
            if input_price:
                pieces.append(f"input_tokens * {money(input_price)} / 1000000")
            if output_price:
                pieces.append(f"output_tokens * {money(output_price)} / 1000000")
            expression = Expression(" + ".join(pieces) or "0")
        elif mode in {"request", "seconds", "characters"}:
            price = units(rule.get("price", "0"))
            expression = Expression(
                "0"
                if not price
                else f"{money(price)}"
                + {
                    "request": "",
                    "seconds": " * seconds",
                    "characters": " * characters / 1000",
                }[mode]
            )
        else:
            raise BillingError("不支持的计费方式。")
        dynamic = bool(expression.names & USAGE_NAMES)
        reserved = (
            units(rule.get("reserve", "0")) if dynamic else expression.evaluate(values)
        )
        if dynamic and reserved <= 0:
            raise BillingError(
                f"模型 {model} 按实际用量计费，需配置大于 0 的单次预留上限。"
            )
        if reserved == 0:
            return None
        return expression, values, reserved, dict(rule)


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
                }
                for target, names in mapping.items():
                    for name in names:
                        value = usage.get(name)
                        if type(value) in (int, float) and 0 <= value <= 1e12:
                            self.values[target] = value
                            break
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
            self.values["total_tokens"] = max(
                self.values.get("total_tokens", 0),
                self.values["input_tokens"] + self.values["output_tokens"],
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
            usage.store.checkpoint(usage.order, evidence)
            usage.saved = snapshot


async def billed_call(service, event, media: str, config: dict, counts: dict, callback):
    """Reserve, invoke once and settle independently of artifact delivery.

    Args:
        service: Billing service, or None in isolated adapter tests.
        event: AstrBot user identity.
        media: Selected media family.
        config: Effective route/model and parameters.
        counts: Input character and material counts.
        callback: Coroutine factory issuing the single generation operation.

    Returns:
        The original adapter result.
    """
    if service and service.closed:
        raise BillingError("插件正在重载，请稍后重试；未提交生成任务。")
    quote = service.quote(media, config, counts) if service else None
    if quote is None:
        return await callback()
    expression, values, reserved, rule = quote
    order = service.store.reserve(
        service.owner(event),
        media,
        str(config["model"]),
        reserved,
        {"rule": rule, "request": values},
    )
    usage = Usage(service.store, order)
    task = asyncio.current_task()
    service.tasks.add(task)
    token = CURRENT.set(usage)
    result, error = None, None
    try:
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
        if usage.state == "completed":
            # Probe only local completed artifacts; never download again for billing.
            path = getattr(result, "path", None)
            if path and "seconds" in expression.names:
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
                actual = expression.evaluate({**values, **usage.values})
                charge = min(reserved, actual)
                note = "成功，实际费用 " + money(actual)
                if actual > reserved:
                    note += "；已按预留上限封顶"
            except BillingError as exc:
                note = str(exc)
        elif usage.state in {"new", "failed"}:
            charge, note = 0, "生成前失败或上游明确拒绝/失败，退回预留"
        else:
            note = "上游状态不明，保留冻结额度，请核对原任务"
        if usage.task_id:
            note += f"；上游任务 {usage.task_id}"
        try:
            service.store.finish(
                order,
                charge,
                note,
                evidence={
                    "usage": usage.values,
                    "state": usage.state,
                    "task_id": usage.task_id,
                    "path": str(getattr(result, "path", "")),
                },
            )
        except sqlite3.Error:
            # A completed artifact must still be delivered. Its durable hold is
            # recoverable after the database/storage problem has been resolved.
            LOGGER.exception("Could not settle billing order %s; retaining hold", order)
            charge = None
        if charge is None:
            LOGGER.warning("Billing order %s needs review: %s", order, note)
            suffix = f"\n订单 {order} 待核对，已冻结 {money(reserved)}；/我的账单 可查询，请勿重复提交。"
            if isinstance(result, str):
                result += suffix
            if isinstance(error, Exception):
                error.args = (str(error) + suffix,)
    finally:
        service.tasks.discard(task)
    if error is not None:
        raise error
    return result
