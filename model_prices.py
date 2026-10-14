"""Public price descriptions from the same bounded expressions used to charge."""

import operator
from collections import Counter
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from itertools import product

from .billing import BillingError, Expression, money, units
from .billing_expression import TOKENS

UNKNOWN = object()
LABELS = {
    "p": "输入",
    "c": "输出",
    "len": "完整输入上下文",
    "cr": "缓存读取",
    "cc": "缓存写入",
    "cc1h": "一小时缓存写入",
    "img": "图片输入",
    "img_cr": "图片缓存读取",
    "img_o": "图片输出",
    "ai": "音频输入",
    "ao": "音频输出",
    "input_tokens": "输入",
    "output_tokens": "输出",
    "total_tokens": "总 Token",
    "tokens": "总 Token",
    "seconds": "秒数",
    "actual_seconds": "实际成品秒数",
    "requests": "成功次数",
    "characters": "字符数",
    "image_count": "生成图片数",
    "resolution": "分辨率",
    "quality": "画质",
    "orientation": "方向",
    "size": "尺寸",
    "aspect_ratio": "比例",
    "voice": "音色",
    "media": "媒体类型",
}


def _validate(node, fixed_scale=Decimal(1)):
    """Type-check known values without supplying invented request or usage facts.

    Args:
        node: A private pricing syntax tuple.
        fixed_scale: Raw units used by fixed() in this expression.

    Returns:
        The inferred type and an optional constant value.

    Raises:
        BillingError: If a known type or constant calculation is invalid.
    """
    kind = node[0]
    if kind == "literal":
        value = node[1]
        return (
            "boolean"
            if type(value) is bool
            else "number"
            if isinstance(value, Decimal)
            else "nil"
            if value is None
            else "string",
            value,
        )
    if kind == "variable":
        return (
            "string"
            if node[1]
            in {
                "model",
                "media",
                "resolution",
                "quality",
                "size",
                "aspect_ratio",
                "voice",
            }
            else "number",
            UNKNOWN,
        )

    def require(value, expected):
        if value[0] not in {expected, "any"}:
            raise BillingError("表达式参数类型不合法。")

    if kind == "conditional":
        require(_validate(node[1], fixed_scale), "boolean")
        yes, no = _validate(node[2], fixed_scale), _validate(node[3], fixed_scale)
        if yes[0] != no[0] and "any" not in {yes[0], no[0]}:
            raise BillingError("价格分支的结果类型不一致。")
        return (yes[0] if yes[0] == no[0] else "any", UNKNOWN)
    if kind == "unary":
        value = _validate(node[2], fixed_scale)
        expected = "boolean" if node[1] == "!" else "number"
        require(value, expected)
        constant = value[1]
        if constant is not UNKNOWN:
            constant = (
                not constant
                if node[1] == "!"
                else -constant
                if node[1] == "-"
                else constant
            )
        return expected, constant
    if kind == "binary":
        op = node[1]
        left, right = _validate(node[2], fixed_scale), _validate(node[3], fixed_scale)
        if op in {"&&", "||"}:
            require(left, "boolean")
            require(right, "boolean")
            return "boolean", UNKNOWN
        if op in {"==", "!=", "<", "<=", ">", ">="}:
            if left[0] != right[0] and not {"any", "nil"} & {left[0], right[0]}:
                raise BillingError("比较的类型不一致。")
            return "boolean", UNKNOWN
        require(left, "number")
        require(right, "number")
        if op in {"/", "%"} and right[1] is not UNKNOWN and right[1] == 0:
            raise BillingError("价格表达式不能除以零。")
        if op == "%" and any(
            value[1] is not UNKNOWN and value[1] != value[1].to_integral_value()
            for value in (left, right)
        ):
            raise BillingError("取模仅支持整数。")
        result = UNKNOWN
        if left[1] is not UNKNOWN and right[1] is not UNKNOWN:
            try:
                result = {
                    "+": operator.add,
                    "-": operator.sub,
                    "*": operator.mul,
                    "/": operator.truediv,
                    "%": operator.mod,
                }[op](left[1], right[1])
            except ArithmeticError as exc:
                raise BillingError("价格常量计算失败。") from exc
            if not result.is_finite() or abs(result) > Decimal("1e18"):
                raise BillingError("价格常量计算越界。")
        return "number", result
    function, arguments = node[1], node[2]
    values = [_validate(arg, fixed_scale) for arg in arguments]
    if function in {
        "u",
        "param",
        "header",
        "hour",
        "minute",
        "weekday",
        "month",
        "day",
    }:
        require(values[0], "string")
        if function == "u":
            key = arguments[0][1].strip()
            if key in {"resolution", "quality", "orientation"}:
                return "string", UNKNOWN
            if key in {
                "seconds",
                "actual_seconds",
                "requests",
                "characters",
                "image_count",
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "tokens",
            }:
                return "number", UNKNOWN
        if (
            function in {"hour", "minute", "weekday", "month", "day"}
            and values[0][1] == "Local"
        ):
            raise BillingError("请使用明确的 IANA 时区，不支持依赖服务器环境的 Local。")
        return (
            "any"
            if function in {"u", "param"}
            else "string"
            if function == "header"
            else "number",
            UNKNOWN,
        )
    if function == "has":
        require(values[1], "string")
        return "boolean", UNKNOWN
    if function == "tier":
        require(values[0], "string")
        require(values[1], "number")
        if values[1][1] is not UNKNOWN and values[1][1] < 0:
            raise BillingError("价格不能为负数。")
        return values[1]
    for value in values:
        require(value, "number")
    if function == "fixed" and values[0][1] is not UNKNOWN and values[0][1] < 0:
        raise BillingError("价格不能为负数。")
    constant = UNKNOWN
    if all(value[1] is not UNKNOWN for value in values):
        numbers = [value[1] for value in values]
        constant = (
            min(numbers)
            if function == "min"
            else max(numbers)
            if function == "max"
            else abs(numbers[0])
            if function == "abs"
            else numbers[0].to_integral_value(rounding=ROUND_CEILING)
            if function == "ceil"
            else numbers[0].to_integral_value(rounding=ROUND_FLOOR)
            if function == "floor"
            else numbers[0] * fixed_scale
        )
        if not constant.is_finite() or abs(constant) > Decimal("1e18"):
            raise BillingError("价格常量计算越界。")
    return "number", constant


def _scales(expression, node):
    """Share fixed and settlement units across validation and fee descriptions.

    Args:
        expression: Compiled rule with its preserved currency convention.
        node: Current price branch, including only its own fixed() calls.

    Returns:
        Raw fixed multiplier and final currency divisor.
    """
    fixed = False
    pending = [node]
    while pending:
        current = pending.pop()
        if current[0] == "call":
            if current[1] == "fixed":
                fixed = True
                break
            pending.extend(current[2])
        elif current[0] == "conditional":
            pending.extend(current[1:])
        elif current[0] in {"unary", "binary"}:
            pending.extend(current[2:])
    return (
        Decimal(1_000_000 if expression.token_mode or not expression.auto_units else 1),
        Decimal(
            1_000_000
            if expression.token_mode or (not expression.auto_units and fixed)
            else 1
        ),
    )


def compile_price(rule: dict) -> Expression:
    """Compile a selected rule on demand, preserving old saved price units.

    Args:
        rule: One model price entry.

    Returns:
        A validated expression with its currency convention.

    Raises:
        BillingError: If the rule is not a valid numeric price expression.
    """
    mode = rule.get("mode")
    if mode is not None and not isinstance(mode, str):
        raise BillingError("不支持的价格格式。")
    if mode is None and "expression" not in rule:
        if "input_price" in rule or "output_price" in rule:
            mode = "tokens"
        elif "price" in rule:
            mode = "request"
    if mode in {"request", "seconds", "characters", "autodl_seconds"}:
        price = money(units(rule.get("price", "0")))
        source = (
            "0"
            if price == "0"
            else {
                "request": price,
                "seconds": f"seconds * {price}",
                "characters": f"characters * {price} / 1000",
                "autodl_seconds": f'u("seconds") * {price}',
            }[mode]
        )
        expression = Expression(source)
    elif mode == "tokens":
        parts = [
            f"{name} * {money(units(rule.get(key, '0')))} / 1000000"
            for name, key in (
                ("input_tokens", "input_price"),
                ("output_tokens", "output_price"),
            )
            if units(rule.get(key, "0"))
        ]
        expression = Expression(" + ".join(parts) or "0")
    else:
        if mode not in {None, "", "expression", "token_expression"}:
            raise BillingError("不支持的价格格式。")
        source = rule.get("expression", "0")
        if not isinstance(source, str):
            raise BillingError("价格表达式必须是文字。")
        expression = Expression(
            source.strip() or "0",
            token_mode=(mode == "token_expression") if mode else None,
        )
        if not mode and expression.legacy:
            raise BillingError("请使用 NewAPI 价格表达式语法。")
    if not expression.legacy:
        fixed_scale, divisor = _scales(expression, expression.parser.tree)
        inferred, value = _validate(expression.parser.tree, fixed_scale)
        if inferred not in {"number", "any"} or (value is not UNKNOWN and value < 0):
            raise BillingError("价格表达式必须返回非负数字。")
        if value is not UNKNOWN:
            units(value / divisor)
        try:
            branches = _branches(expression.parser.tree)
            for _, node in branches:
                _, constant = _validate(node, fixed_scale)
                if constant is not UNKNOWN and constant < 0:
                    raise BillingError("价格分支不能返回负数。")
                if constant is not UNKNOWN:
                    units(constant / _scales(expression, node)[1])
            expression.free = all(
                not any(_linear(node, _scales(expression, node)[0]).values())
                for _, node in branches
            )
        except BillingError:
            raise
        except ValueError:
            expression.free = False
    return expression


def _number(value):
    value = Decimal(value)
    if not value:
        return "0"
    # A short exponent must not expand into megabytes of zeros in a preview.
    if value and value.adjusted() < -18:
        return str(value)
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _text(node):
    kind = node[0]
    if kind == "literal":
        return _number(node[1]) if isinstance(node[1], Decimal) else str(node[1])
    if kind == "variable":
        return LABELS.get(node[1], node[1])
    if kind == "binary":
        return f"({_text(node[2])} {node[1]} {_text(node[3])})"
    if kind == "unary":
        return ("不满足 " if node[1] == "!" else node[1]) + _text(node[2])
    if kind == "conditional":
        return f"{_text(node[1])} 时 {_text(node[2])}，否则 {_text(node[3])}"
    function, arguments = node[1:]
    if function in {"u", "param"}:
        return LABELS.get(arguments[0][1], arguments[0][1])
    if function == "header":
        return "请求头条件"
    if function == "tier":
        return _text(arguments[1])
    label = {
        "fixed": "按次",
        "ceil": "向上取整",
        "floor": "向下取整",
        "hour": "小时",
        "minute": "分钟",
        "weekday": "星期（日=0）",
        "month": "月份",
        "day": "日期",
    }.get(function, function)
    return f"{label}({', '.join(_text(arg) for arg in arguments)})"


def _branches(node, conditions=()):
    """Expand nested price tiers, bounded before any Cartesian allocation."""
    if node[0] == "conditional":
        rows = _branches(node[2], conditions + (_text(node[1]),)) + _branches(
            node[3], conditions + ("不满足 " + _text(node[1]),)
        )
        if len(rows) > 64:
            raise ValueError("Too many display branches")
        return rows
    if node[0] in {"literal", "variable"}:
        return [(conditions, node)]
    children = node[2] if node[0] == "call" else node[2:]
    expanded = [_branches(child) for child in children]
    count = 1
    for rows in expanded:
        count *= len(rows)
        if count > 64:
            raise ValueError("Too many display branches")
    return [
        (
            conditions + tuple(label for labels, _ in parts for label in labels),
            (node[0], node[1], tuple(part[1] for part in parts))
            if node[0] == "call"
            else (node[0], node[1], *(part[1] for part in parts)),
        )
        for parts in product(*expanded)
    ]


def _linear(node, fixed_scale):
    """Extract a complete linear fee; never guess coefficients of other shapes."""
    kind = node[0]
    if kind == "literal" and isinstance(node[1], Decimal):
        return {"": node[1]}
    if kind == "variable":
        return {node[1]: Decimal(1)}
    if kind == "call":
        name, args = node[1:]
        if name == "tier":
            return _linear(args[1], fixed_scale)
        if name in {"u", "param"}:
            return {args[0][1]: Decimal(1)}
        if name == "fixed":
            return {
                key: value * fixed_scale
                for key, value in _linear(args[0], fixed_scale).items()
            }
    if kind == "unary" and node[1] in {"+", "-"}:
        return {
            key: value * (-1 if node[1] == "-" else 1)
            for key, value in _linear(node[2], fixed_scale).items()
        }
    if kind == "binary":
        left, right = _linear(node[2], fixed_scale), _linear(node[3], fixed_scale)
        if node[1] in {"+", "-"}:
            result = dict(left)
            for key, value in right.items():
                result[key] = result.get(key, Decimal(0)) + value * (
                    -1 if node[1] == "-" else 1
                )
            return result
        if node[1] == "*":
            if set(left) <= {""}:
                return {key: value * left.get("", 0) for key, value in right.items()}
            if set(right) <= {""}:
                return {key: value * right.get("", 0) for key, value in left.items()}
        if node[1] == "/" and set(right) == {""} and right[""]:
            return {key: value / right[""] for key, value in left.items()}
    raise ValueError("Price is not linear")


def price_sections(rules) -> list[str]:
    """Render public fees and tiers without executing a generation request.

    Args:
        rules: Configured model price entries.

    Returns:
        Complete merged-forward sections, including invalid entries.
    """
    rules = rules if isinstance(rules, list) else []
    counts = Counter(
        str(rule.get("model", "")).strip() for rule in rules if isinstance(rule, dict)
    )
    sections = [
        "模型价格\n价格使用余额单位。未设置价格或表达式为空的模型免费；实际费用按成功产物用量结算。Token 单价按百万 Token 显示。"
    ]
    for rule in rules:
        model = str(rule.get("model", "")).strip() if isinstance(rule, dict) else ""
        title = f"模型 ID：{model or '（未填写）'}\n具体费用："
        try:
            if not model or counts[model] != 1:
                raise BillingError("模型 ID 为空或重复。")
            expression = compile_price(rule)
            if expression.legacy:
                sections.append(
                    title + "按已保存的条件公式计费：\n" + expression.source
                )
                continue
            branches = _branches(expression.parser.tree)
            if len(branches) > 64:
                raise ValueError("Too many display branches")
            rows = []
            for conditions, node in branches:
                tier = (
                    str(node[2][0][1])
                    if node[0] == "call" and node[1] == "tier"
                    else ""
                )
                heading = "；".join(conditions) or "默认"
                if tier:
                    heading += f" · {tier}"
                fixed_scale, divisor = _scales(expression, node)
                try:
                    if (
                        not expression.auto_units
                        and not expression.token_mode
                        and any("按次(" in condition for condition in conditions)
                    ):
                        raise ValueError(
                            "Legacy fixed() in a condition changes settlement units"
                        )
                    rates = _linear(node, fixed_scale)
                    details = []
                    for key, value in rates.items():
                        value /= divisor
                        if not value:
                            continue
                        if key in TOKENS - {"image_count"} or key in {
                            "input_tokens",
                            "output_tokens",
                            "total_tokens",
                            "tokens",
                        }:
                            details.append(
                                f"{LABELS.get(key, key)}：{_number(value * 1_000_000)} / 百万 Token"
                            )
                        elif key in {"seconds", "actual_seconds"}:
                            details.append(f"每秒：{_number(value)}")
                        elif key == "characters":
                            details.append(f"每千字符：{_number(value * 1000)}")
                        elif key in {"", "requests"}:
                            details.append(f"每次：{_number(value)}")
                        elif key == "image_count":
                            details.append(f"每张：{_number(value)}")
                        else:
                            raise ValueError(
                                "Cannot display this usage as a known unit"
                            )
                    rows.append(heading + "\n" + ("\n".join(details) or "免费（0）"))
                except ValueError:
                    suffix = (
                        "（计算结果按百万单位结算）" if expression.token_mode else ""
                    )
                    rows.append(heading + "\n按公式计算：" + _text(node) + suffix)
            if (
                model in {"IndexTTS-2", "indextts2-v1"}
                and "seconds" in expression.usage_keys
            ):
                rows.append("AutoDL 规则：不足 10 秒按 10 秒计费。")
            sections.append(title + "\n" + "\n\n".join(rows))
        except BillingError:
            sections.append(title + "表达式不合法")
        except ValueError:
            sections.append(title + "按条件公式计算；分支较多，无法展开为单价列表。")
    if not rules:
        sections.append("尚未设置模型价格，所有模型免费。")
    return sections
