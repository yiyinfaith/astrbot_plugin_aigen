"""Bounded interpreter for New API's public billing expression grammar."""

from __future__ import annotations

import ast
import operator
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TOKENS = {
    "p",
    "c",
    "len",
    "cr",
    "cc",
    "cc1h",
    "img",
    "img_cr",
    "img_o",
    "ai",
    "ao",
    "image_count",
}
ALIASES = {
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
    "seconds",
    "input_tokens",
    "output_tokens",
    "total_tokens",
}
FUNCTIONS = {
    "tier": 2,
    "fixed": 1,
    "param": 1,
    "header": 1,
    "u": 1,
    "has": 2,
    "hour": 1,
    "minute": 1,
    "weekday": 1,
    "month": 1,
    "day": 1,
    "min": 2,
    "max": 2,
    "abs": 1,
    "ceil": 1,
    "floor": 1,
}
LEXER = re.compile(
    r"""\s+|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[A-Za-z_][A-Za-z_0-9]*|&&|\|\||==|!=|<=|>=|[()+*/%!,?:<>-]"""
)
PRECEDENCE = {
    "||": 1,
    "&&": 2,
    "==": 3,
    "!=": 3,
    "<": 4,
    "<=": 4,
    ">": 4,
    ">=": 4,
    "+": 5,
    "-": 5,
    "*": 6,
    "/": 6,
    "%": 6,
}


class BillingExpressionError(ValueError):
    """An expression cannot be safely compiled or evaluated."""


class PricingExpression:
    """Compile safe pricing syntax shared with New API.

    Args:
        source: An unversioned or v1 New API billing expression.
    """

    def __init__(self, source: str):
        source = source.strip()
        if source.startswith("v1:"):
            source = source[3:].strip()
        if not source or len(source) > 6000:
            raise BillingExpressionError("价格表达式为空或过长。")
        self.tokens, position = [], 0
        for match in LEXER.finditer(source):
            if match.start() != position:
                raise BillingExpressionError(
                    "不支持的表达式语法，仅支持 NewAPI 常用计费语法。"
                )
            position = match.end()
            token = match.group()
            if not token.isspace():
                self.tokens.append(token)
        if position != len(source) or len(self.tokens) > 512:
            raise BillingExpressionError("价格表达式含非法语法或过于复杂。")
        self.index, self.names, self.usage_keys, self.parameter_paths = (
            0,
            set(),
            set(),
            set(),
        )
        self.functions = set()
        self.tree = self.parse(0, 0)
        if self.index != len(self.tokens):
            raise BillingExpressionError("价格表达式语法错误。")

    def take(self, expected=None):
        """Consume a token and optionally require its exact spelling.

        Args:
            expected: Optional punctuation required at this position.

        Returns:
            The consumed token.
        """
        if self.index >= len(self.tokens):
            raise BillingExpressionError("价格表达式意外结束。")
        value = self.tokens[self.index]
        self.index += 1
        if expected is not None and value != expected:
            raise BillingExpressionError(f"价格表达式需要 {expected}。")
        return value

    def parse(self, minimum: int, depth: int):
        """Parse arithmetic and right-associative conditional expressions.

        Args:
            minimum: Lowest accepted binary precedence.
            depth: Bounded recursive syntax depth.

        Returns:
            A private immutable syntax tuple.
        """
        if depth > 60:
            raise BillingExpressionError("价格表达式嵌套过深。")
        token = self.take()
        if token in {"+", "-", "!"}:
            left = ("unary", token, self.parse(7, depth + 1))
        elif token == "(":
            left = self.parse(0, depth + 1)
            self.take(")")
        elif token.startswith(('"', "'")):
            try:
                left = ("literal", ast.literal_eval(token))
            except (ValueError, SyntaxError) as exc:
                raise BillingExpressionError("字符串转义无效。") from exc
        elif token in {"true", "false", "nil"}:
            left = ("literal", {"true": True, "false": False, "nil": None}[token])
        elif token[0].isdigit() or token.startswith("."):
            value = Decimal(token)
            if not value.is_finite() or abs(value) > Decimal("1e18"):
                raise BillingExpressionError("价格表达式数值越界。")
            left = ("literal", value)
        elif re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", token):
            if self.index < len(self.tokens) and self.tokens[self.index] == "(":
                if token not in FUNCTIONS:
                    raise BillingExpressionError(f"不支持的计费函数：{token}")
                self.take("(")
                args = []
                if self.index < len(self.tokens) and self.tokens[self.index] != ")":
                    while True:
                        args.append(self.parse(0, depth + 1))
                        if (
                            self.index >= len(self.tokens)
                            or self.tokens[self.index] != ","
                        ):
                            break
                        self.take(",")
                self.take(")")
                if len(args) != FUNCTIONS[token]:
                    raise BillingExpressionError(f"函数 {token} 参数数量不正确。")
                if token in {"u", "param", "header"}:
                    if args[0][0] != "literal" or not isinstance(args[0][1], str):
                        raise BillingExpressionError(
                            f"函数 {token} 的路径必须是字符串常量。"
                        )
                    if token == "u":
                        self.usage_keys.add(args[0][1].strip())
                    if token == "param":
                        path = args[0][1].strip()
                        if any(
                            mark in path
                            for mark in ("|", "@", "*", "?", "[", "]", "#(")
                        ):
                            raise BillingExpressionError(
                                "不支持高级 GJSON 查询；请使用明确字段路径，不会以缺失字段替代不支持的语法。"
                            )
                        self.parameter_paths.add(args[0][1].strip())
                self.functions.add(token)
                left = ("call", token, tuple(args))
            else:
                if token not in TOKENS | ALIASES:
                    raise BillingExpressionError(f"未知计费变量：{token}")
                self.names.add(token)
                left = ("variable", token)
        else:
            raise BillingExpressionError("价格表达式语法错误。")
        while self.index < len(self.tokens):
            op = self.tokens[self.index]
            precedence = PRECEDENCE.get(op, -1)
            if precedence < minimum:
                break
            self.take()
            left = ("binary", op, left, self.parse(precedence + 1, depth + 1))
        if (
            minimum == 0
            and self.index < len(self.tokens)
            and self.tokens[self.index] == "?"
        ):
            self.take("?")
            yes = self.parse(0, depth + 1)
            self.take(":")
            left = ("conditional", left, yes, self.parse(0, depth + 1))
        return left

    def evaluate_raw(self, values: dict):
        """Evaluate without currency conversion or guessed context.

        Args:
            values: Variables, _request, _usage, _headers and a frozen _now.

        Returns:
            Raw numeric cost and matched tier.
        """
        tier = ""

        def number(value):
            if type(value) not in (int, float, Decimal):
                raise BillingExpressionError(
                    "计费算术需要数字；缺少实际用量或参数类型不正确。"
                )
            result = Decimal(str(value))
            if not result.is_finite() or abs(result) > Decimal("1e18"):
                raise BillingExpressionError("价格表达式计算越界。")
            return result

        def boolean(value):
            if type(value) is not bool:
                raise BillingExpressionError("计费条件必须为布尔值。")
            return value

        def visit(node):
            nonlocal tier
            kind = node[0]
            if kind == "literal":
                return node[1]
            if kind == "variable":
                if node[1] not in values or values[node[1]] is None:
                    raise BillingExpressionError(f"缺少实际计费用量：{node[1]}")
                return values[node[1]]
            if kind == "conditional":
                return visit(node[2] if boolean(visit(node[1])) else node[3])
            if kind == "unary":
                value = visit(node[2])
                if node[1] == "!":
                    return not boolean(value)
                return -number(value) if node[1] == "-" else number(value)
            if kind == "binary":
                op, left_node, right_node = node[1:]
                left = visit(left_node)
                if op == "&&":
                    return boolean(left) and boolean(visit(right_node))
                if op == "||":
                    return boolean(left) or boolean(visit(right_node))
                right = visit(right_node)
                numeric = type(left) in (int, float, Decimal) and type(right) in (
                    int,
                    float,
                    Decimal,
                )
                if op in {"==", "!="}:
                    equal = (
                        number(left) == number(right)
                        if numeric
                        else type(left) is type(right) and left == right
                    )
                    return equal if op == "==" else not equal
                if op in {"<", "<=", ">", ">="}:
                    if numeric:
                        left, right = number(left), number(right)
                    elif not isinstance(left, str) or not isinstance(right, str):
                        raise BillingExpressionError("计费比较的类型不匹配。")
                    return {
                        "<": operator.lt,
                        "<=": operator.le,
                        ">": operator.gt,
                        ">=": operator.ge,
                    }[op](left, right)
                left, right = number(left), number(right)
                if op == "%" and (
                    left != left.to_integral_value()
                    or right != right.to_integral_value()
                ):
                    raise BillingExpressionError("取模仅支持整数。")
                result = {
                    "+": operator.add,
                    "-": operator.sub,
                    "*": operator.mul,
                    "/": operator.truediv,
                    "%": operator.mod,
                }[op](left, right)
                return number(result)
            function, args = node[1], [visit(arg) for arg in node[2]]
            if function == "tier":
                if not isinstance(args[0], str):
                    raise BillingExpressionError("tier 档位名称必须为字符串。")
                tier = args[0][:128]
                return number(args[1])
            if function == "fixed":
                if isinstance(values.get("_trace"), dict):
                    values["_trace"]["fixed"] = True
                return number(args[0]) * 1_000_000
            if function == "u":
                if "_usage" not in values:
                    raise BillingExpressionError("缺少实际任务用量。")
                return values["_usage"].get(args[0].strip())
            if function == "header":
                if "_headers" not in values:
                    raise BillingExpressionError(
                        "此接口没有提供可核验的请求头计费上下文。"
                    )
                headers = {
                    str(key).strip().lower(): str(value).strip()
                    for key, value in values["_headers"].items()
                    if str(key).strip() and str(value).strip()
                }
                return headers.get(args[0].strip().lower(), "")
            if function == "param":
                if "_request" not in values:
                    raise BillingExpressionError("缺少请求参数计费上下文。")
                value = values["_request"]
                # Literal paths support escaped dots, array indices and lengths.
                parts = re.findall(r"(?:\\.|[^.])+", args[0].strip())
                for part in parts:
                    part = re.sub(r"\\(.)", r"\1", part)
                    if isinstance(value, dict):
                        value = value.get(part)
                    elif isinstance(value, list) and part == "#":
                        value = len(value)
                    elif isinstance(value, list) and part.isdigit():
                        value = value[int(part)] if int(part) < len(value) else None
                    else:
                        return None
                return value if parts else None
            if function == "has":
                if args[0] is None or not args[1]:
                    return False

                def go_string(value):
                    if value is None:
                        return "<nil>"
                    if isinstance(value, bool):
                        return str(value).lower()
                    if isinstance(value, list):
                        return "[" + " ".join(go_string(item) for item in value) + "]"
                    if isinstance(value, dict):
                        return (
                            "map["
                            + " ".join(
                                str(key) + ":" + go_string(value[key])
                                for key in sorted(value)
                            )
                            + "]"
                        )
                    return str(value)

                return args[1] in go_string(args[0])

            if function in {"hour", "minute", "weekday", "month", "day"}:
                if args[0] == "Local":
                    raise BillingExpressionError(
                        "请使用明确的 IANA 时区，不支持依赖服务器环境的 Local。"
                    )
                try:
                    zone = ZoneInfo(args[0]) if args[0] else timezone.utc
                except (ZoneInfoNotFoundError, ValueError):
                    zone = timezone.utc
                now = values.get("_now") or datetime.now(timezone.utc)
                if isinstance(now, str):
                    now = datetime.fromisoformat(now)
                now = now.astimezone(zone)
                return {
                    "hour": now.hour,
                    "minute": now.minute,
                    "weekday": (now.weekday() + 1) % 7,
                    "month": now.month,
                    "day": now.day,
                }[function]
            numbers = [number(arg) for arg in args]
            if function in {"min", "max"}:
                return (min if function == "min" else max)(numbers)
            if function == "abs":
                return abs(numbers[0])
            return numbers[0].to_integral_value(
                rounding="ROUND_CEILING" if function == "ceil" else "ROUND_FLOOR"
            )

        try:
            return number(visit(self.tree)), tier
        except (
            ArithmeticError,
            InvalidOperation,
            TypeError,
            ValueError,
            RecursionError,
        ) as exc:
            raise BillingExpressionError("价格表达式计算失败，未扣费。") from exc
