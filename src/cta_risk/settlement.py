from __future__ import annotations

def write_report(path, day: str, rows: list[dict]) -> None:
    """把一日清算写成纯文本。"""
    lines = [f"风控日报  {day}", ""]
    for row in rows:
        status = "熔断" if row["halted"] else "正常"
        lines.append(f"[{row['account_id']}] {row['name']}  {status}")
        lines.append(
            f"  平仓盯市 {row['close_pnl']}  持仓盯市 {row['position_pnl']}  "
            f"手续费 {row['commission']}  权益 {row['equity']}"
        )
        lines.append(f"  保证金 {row['margin']}  可用 {row['available']}")
        if not row["positions"]:
            lines.append("  持仓  （空仓）")
        for pos in row["positions"]:
            side = "多" if pos["side"] == "long" else "空"
            lines.append(
                f"  {pos['instrument']} {side} {pos['qty']}手  "
                f"基准 {pos['basis']}  结算 {pos['settle']}  盯市 {pos['pnl']}  "
                f"名义敞口 {pos['notional']}"
            )
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
