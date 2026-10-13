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
    "计费帮助",
}
ADMIN_COMMANDS = {"生成卡密", "剩余卡密", "作废卡密", "计费订单", "结算订单"}
HELP = """计费始终启用，未配置价格的模型免费。余额单位由管理员定价决定。
/余额：查询可用和冻结余额
/兑换卡密 <卡密>：一次性充值，建议私聊
/我的账单 [页码]：查询自己的生成订单
管理员命令（请私聊机器人）：
/生成卡密 <面额> [数量]：数量默认1，最多100
/剩余卡密 [页码]：列出剩余有效卡密，每页20条
/作废卡密 <卡密>：使尚未兑换的卡密失效
/计费订单 [页码]：查询待核对订单
/结算订单 <订单ID> <金额>：核对上游后结算，0表示退款；不能超过冻结额度
生成前预留余额，成功后结算差额；明确失败退款。状态不明或缺少实际用量时保留冻结，等待管理员核对。发送失败不重复扣费。"""


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
        if command == "计费帮助":
            return HELP
        if command == "余额":
            available, held = store.balance(owner)
            return f"用户 ID：{event.get_sender_id()}\n可用余额：{money(available)}\n冻结余额：{money(held)}\n/我的账单 查询订单；/兑换卡密 <卡密> 充值。"
        if command == "兑换卡密":
            if len(args) != 1:
                return "用法：/兑换卡密 <卡密>，建议私聊机器人兑换。"
            amount = store.redeem(owner, args[0])
            available, held = store.balance(owner)
            return f"兑换成功，充值 {money(amount)}。可用余额 {money(available)}，冻结 {money(held)}。"
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
                return (
                    "用法：/结算订单 <订单ID> <金额>；0表示退款。请先核对上游实际结果。"
                )
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
            "reserved": "生成中",
            "review": "待核对",
            "settled": "已结算",
            "refunded": "已退款",
        }
        for row in rows:
            if kind == "cards":
                lines.append(f"{row['code']}  面额 {money(row['amount'])}")
            else:
                evidence = json.loads(row["details"]).get("result", {})
                lines.append(
                    f"{row['id']}｜{row['model']}｜{states[row['status']]}｜预留 {money(row['reserved'])}｜已扣 {money(row['charged']) if row['charged'] is not None else '待定'}\n{row['note']}"
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
        return "参数格式错误，请发送 /计费帮助 查看用法。"
    except sqlite3.Error:
        return "账本暂时不可用，请联系管理员；不要重复提交生成。"
