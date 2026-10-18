"""Readable price tables derived from syntax, without changing settlement rules."""

import operator
from decimal import Decimal

from .billing_expression import TOKENS
from .model_prices import LABELS, _branches, _linear, _number, _scales, _text

COMPARE = {
    "==": operator.eq,
    "!=": operator.ne,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
}


def _specialize(node, facts):
    """Substitute declared model categories only; never infer them from tier IDs."""
    kind = node[0]
    if kind in {"literal", "variable"}:
        return node
    if kind == "call":
        if node[1] == "u" and node[2][0][1].strip() in facts:
            return ("literal", facts[node[2][0][1].strip()])
        return (kind, node[1], tuple(_specialize(arg, facts) for arg in node[2]))
    if kind == "conditional":
        condition = _specialize(node[1], facts)
        if condition[0] == "literal" and type(condition[1]) is bool:
            return _specialize(node[2] if condition[1] else node[3], facts)
        return (
            kind,
            condition,
            _specialize(node[2], facts),
            _specialize(node[3], facts),
        )
    if kind == "unary":
        child = _specialize(node[2], facts)
        if node[1] == "!" and child[0] == "literal" and type(child[1]) is bool:
            return ("literal", not child[1])
        return (kind, node[1], child)
    left, right = (_specialize(part, facts) for part in node[2:])
    op = node[1]
    if (
        op in COMPARE
        and left[0] == right[0] == "literal"
        and type(left[1]) is type(right[1])
    ):
        return ("literal", COMPARE[op](left[1], right[1]))
    if op in {"&&", "||"}:
        for fixed, other in [(left, right), (right, left)]:
            if fixed[0] == "literal" and type(fixed[1]) is bool:
                return fixed if fixed[1] == (op == "||") else other
    return (kind, op, left, right)


def _hours(node):
    """Return exact matching integer hours for one timezone, when representable."""
    if node[0] == "unary" and node[1] == "!":
        child = _hours(node[2])
        return (child[0], set(range(24)) - child[1]) if child else None
    if node[0] != "binary":
        return None
    op, left, right = node[1:]
    if op in {"&&", "||"}:
        a, b = _hours(left), _hours(right)
        if a and b and a[0] == b[0]:
            return (a[0], a[1] & b[1] if op == "&&" else a[1] | b[1])
        return None
    if op not in COMPARE:
        return None
    for probe, literal, reverse in [(left, right, False), (right, left, True)]:
        if (
            probe[0] == "call"
            and probe[1] == "hour"
            and probe[2][0][0] == "literal"
            and isinstance(probe[2][0][1], str)
            and literal[0] == "literal"
            and isinstance(literal[1], Decimal)
        ):
            values = {
                h
                for h in range(24)
                if (
                    COMPARE[op](literal[1], h)
                    if reverse
                    else COMPARE[op](h, literal[1])
                )
            }
            return (probe[2][0][1], values)
    return None


def _condition(node, matches=True):
    hours = _hours(node)
    if hours and hours[1]:
        zone, values = hours
        spans = []
        for h in sorted(values):
            if spans and spans[-1][1] == h:
                spans[-1][1] = h + 1
            else:
                spans.append([h, h + 1])
        text = "、".join(f"{a:02d}:00至{b:02d}:00" for a, b in spans)
        return text + ("" if matches else "以外的时段") + f"（{zone}）"
    if node[0] == "unary" and node[1] == "!":
        return _condition(node[2], not matches)
    if node[0] == "binary":
        op, left, right = node[1:]
        if op in {"&&", "||"}:
            join = " 且 " if op == "&&" else " 或 "
            text = join.join(_condition(part) for part in [left, right])
            return text if matches else f"不满足（{text}）"
        if op in COMPARE:
            if not matches:
                op = {
                    "==": "!=",
                    "!=": "==",
                    "<": ">=",
                    "<=": ">",
                    ">": "<=",
                    ">=": "<",
                }[op]
            label = _text(left)
            if label == "分辨率":
                label = "输出分辨率"
            relation = {
                "==": "：",
                "!=": "不为",
                "<": "小于",
                "<=": "不超过",
                ">": "大于",
                ">=": "至少",
            }[op]
            return label + relation + _text(right)
    text = _text(node)
    return text if matches else f"不满足（{text}）"


def _cells(expression, node, media):
    scale, divisor = _scales(expression, node)
    rates = _linear(node, scale)
    cells, labels = {}, {}
    for key, value in rates.items():
        value /= divisor
        if not value:
            continue
        label, suffix = "", ""
        if key in TOKENS - {"image_count"} or key in {
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "tokens",
        }:
            label, suffix = LABELS.get(key, key) + "单价", "/百万 Token"
            value *= 1_000_000
        elif key in {"seconds", "actual_seconds"}:
            label = (
                "成品单价"
                if key == "actual_seconds"
                else media + "生成单价"
                if media
                else "生成单价"
            )
            suffix = "/秒"
        elif key == "characters":
            label, suffix, value = "文字单价", "/千字符", value * 1000
        elif key in {"", "requests"}:
            label, suffix = "按次单价", "/次"
        elif key == "image_count":
            label, suffix = "图片生成单价", "/张"
        else:
            raise ValueError("Cannot display an unknown billing unit")
        # A fixed base fee and u(requests) are distinct additive components.
        column = key or "fixed"
        labels[column] = label
        cells[column] = _number(value) + suffix
    if not cells:
        cells, labels = {"free": "免费"}, {"free": "费用"}
    return cells, labels


def price_table(rule, expression):
    """Build a bounded condition/rate table using the compiled rule's exact units."""
    model = str(rule.get("model", "")).strip()
    from .video_api_manager import RESOLUTIONS, WORKFLOWS

    workflow = WORKFLOWS.get(
        "wan2.2animate-v4-motion_retargeting"
        if model == "wan2.2-animate-move"
        else model
    )
    media = (
        "视频"
        if workflow
        else "音频"
        if model in {"IndexTTS-2", "indextts2-v1"}
        else ""
    )
    choices = (
        RESOLUTIONS[workflow[6]]
        if workflow and "resolution" in expression.usage_keys
        else (None,)
    )
    result = {"columns": [], "rows": [], "notes": []}
    labels = {}
    if model in {"IndexTTS-2", "indextts2-v1"} and "seconds" in expression.usage_keys:
        result["notes"].append("AutoDL 规则：不足 10 秒按 10 秒计费。")
    try:
        if expression.legacy:
            raise ValueError("Legacy formula")
        if (
            not expression.auto_units
            and not expression.token_mode
            and any(
                "按次(" in _text(condition)
                for conditions, _ in _branches(expression.parser.tree, structured=True)
                for condition, _ in conditions
            )
        ):
            raise ValueError("Conditional legacy units")
        for resolution in choices:
            tree = (
                _specialize(expression.parser.tree, {"resolution": resolution})
                if resolution
                else expression.parser.tree
            )
            for conditions, node in _branches(tree, structured=True):
                parts = [f"输出分辨率：{resolution}"] if resolution else []
                parts += [
                    _condition(condition, matches) for condition, matches in conditions
                ]
                # Preserve legacy fixed() scaling when its condition can alter units.
                if (
                    not expression.auto_units
                    and not expression.token_mode
                    and any("按次(" in _text(c) for c, _ in conditions)
                ):
                    raise ValueError("Conditional legacy units")
                try:
                    fees, columns = _cells(expression, node, media)
                except ValueError:
                    fees = {
                        "formula": "按公式计算："
                        + _text(node)
                        + (
                            "（计算结果按百万单位结算）"
                            if expression.token_mode
                            else ""
                        )
                    }
                    columns = {"formula": "计费方式"}
                labels.update(columns)
                result["rows"].append(
                    {"condition": " · ".join(parts) or "默认", "fees": fees}
                )
                if len(result["rows"]) > 128:
                    raise ValueError("Too many display rows")
        result["columns"] = [
            {"key": key, "label": label} for key, label in labels.items()
        ]
    except ValueError:
        result.update(
            columns=[{"key": "formula", "label": "计费方式"}],
            rows=[
                {
                    "condition": "按条件公式计费",
                    "fees": {"formula": expression.source},
                }
            ],
        )
    return result
