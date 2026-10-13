"""AstrBot-event billing commands; card secrets stay in administrator DMs."""

import json
import sqlite3

from .billing import BillingError, money, units

COMMANDS = {
    "余额",
    "兑换卡密",
    "生成卡密",
    "剩余卡密",
    "作废卡密",
    "我的账单",
    "计费订单",
    "结算订单",
}
ADMIN_COMMANDS = {"生成卡密", "剩余卡密", "作废卡密", "计费订单", "结算订单"}


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
    command, args = parts[0], parts[1:]
    if command in ADMIN_COMMANDS:
        if not event.is_admin():
            return "仅 AstrBot 管理员可使用此命令。"
        if event.get_group_id():
            return "请私聊机器人使用此管理员命令。"
    try:
        owner = service.owner(event)
        store = service.store
        if command == "余额":
            available, _held = store.balance(owner)
            return f"用户 ID：{event.get_sender_id()}\n余额：{money(available)}\n/我的账单 查询订单；/兑换卡密 <卡密> 充值。"
        if command == "兑换卡密":
            if len(args) != 1:
                return "用法：/兑换卡密 <卡密>，建议私聊机器人兑换。"
            amount = store.redeem(owner, args[0])
            available, _held = store.balance(owner)
            return f"兑换成功，充值 {money(amount)}。余额 {money(available)}。"
        if command == "生成卡密":
            if not 1 <= len(args) <= 2:
                return "用法：/生成卡密 <面额> [数量]"
            codes = store.create_cards(
                owner, args[0], int(args[1]) if len(args) == 2 else 1
            )
            return (
                f"已生成 {len(codes)} 张卡密，每张 {money(units(args[0]))}：\n"
                + "\n".join(codes)
            )
        if command == "作废卡密":
            if len(args) != 1:
                return "用法：/作废卡密 <卡密>"
            with store.transaction() as db:
                count = db.execute(
                    "UPDATE cards SET redeemed_by='[revoked]',redeemed_at=CURRENT_TIMESTAMP WHERE code=? AND redeemed_by IS NULL",
                    (args[0].upper(),),
                ).rowcount
            return "卡密已作废。" if count else "卡密不存在或已兑换/作废。"
        if command == "结算订单":
            if len(args) != 2:
                return "用法：/结算订单 <订单ID> <金额>；旧待核对订单仅供核对，不会自动扣费。"
            changed = store.finish(
                args[0], units(args[1]), f"管理员 {owner} 人工核对结算", admin=True
            )
            return "订单已结算。" if changed else "该订单已经结算，没有重复扣费。"
        if len(args) > 1:
            return "查询命令最多填写一个页码。"
        page = int(args[0]) if args else 1
        kind = {"剩余卡密": "cards", "计费订单": "review", "我的账单": "orders"}[
            command
        ]
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
            if kind == "cards":
                lines.append(f"{row['code']}  面额 {money(row['amount'])}")
            else:
                evidence = json.loads(row["details"]).get("result", {})
                lines.append(
                    f"{row['id']}｜{row['model']}｜{states[row['status']]}｜已扣 {money(row['charged']) if row['charged'] is not None else '待定'}\n{row['note']}"
                    + (f"\n用户 {row['owner']}" if kind == "review" else "")
                    + (
                        f"\n上游任务：{evidence['task_id']}"
                        if evidence.get("task_id")
                        else ""
                    )
                    + (
                        f"\n实际用量：{json.dumps(evidence['usage'], ensure_ascii=False)}"
                        if kind == "review" and evidence.get("usage")
                        else ""
                    )
                )
        return "\n".join(lines)
    except BillingError as exc:
        return str(exc)
    except (ValueError, TypeError):
        return "参数格式错误，请发送 /ai生成帮助 查看用法。"
    except sqlite3.Error:
        return "账本暂时不可用，请联系管理员；不要重复提交生成。"
