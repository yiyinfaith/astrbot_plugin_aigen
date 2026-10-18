"""User balance, card redemption and personal order commands."""

import json
import sqlite3

from .billing import BillingError, money

COMMANDS = {
    "AI余额": "AI余额",
    "ai余额": "AI余额",
    "兑换AI卡密": "兑换AI卡密",
    "兑换ai卡密": "兑换AI卡密",
    "AI账单": "AI账单",
    "ai账单": "AI账单",
}


def handle_command(service, event, text: str) -> str:
    """Authorize and execute one command without passing secrets to the LLM.

    Args:
        service: Initialized billing service.
        event: Incoming AstrBot event with trusted role and sender ID.
        text: Recognized command without its slash prefix.

    Returns:
        A single reply, suitable for one message or a merged forward.
    """
    parts = text.split()
    command, args = (parts[0] if parts else ""), parts[1:]
    if command not in COMMANDS:
        return "不支持的命令。"
    command = COMMANDS[command]
    try:
        owner = service.register_user(event)
        store = service.store
        if command == "AI余额":
            available, _held = store.balance(owner)
            return f"用户 ID：{event.get_sender_id()}\n余额：{money(available)}\n/AI账单 查询订单；/兑换AI卡密 <卡密> 充值。"
        if command == "兑换AI卡密":
            if len(args) != 1:
                return "用法：/兑换AI卡密 <卡密>，建议私聊机器人兑换。"
            amount = store.redeem(owner, args[0])
            available, _held = store.balance(owner)
            return f"兑换成功，充值 {money(amount)}。余额 {money(available)}。"
        if len(args) > 1:
            return "查询命令最多填写一个页码。"
        page = int(args[0]) if args else 1
        kind = "orders"
        total, rows = store.listing(kind, owner, page)
        lines = [f"{command}：共 {total} 条，第 {page}/{max(1, (total + 19) // 20)} 页"]
        states = {
            "running": "生成中",
            "reserved": "旧订单",
            "review": "待核对",
            "settled": "已结算",
            "refunded": "未扣费/已退款",
        }
        for row in rows:
            evidence = json.loads(row["details"]).get("result", {})
            lines.append(
                f"{row['id']}｜{row['model']}｜{states[row['status']]}｜已扣 {money(row['charged']) if row['charged'] is not None else '待定'}\n{row['note']}"
                + (
                    f"\n上游任务：{evidence['task_id']}"
                    if evidence.get("task_id")
                    else ""
                )
            )
        return "\n".join(lines)
    except BillingError as exc:
        return str(exc)
    except (ValueError, TypeError):
        return "参数格式错误，请发送 /AI帮助 查看用法。"
    except sqlite3.Error:
        return "账本暂时不可用，请联系管理员；不要重复提交生成。"
