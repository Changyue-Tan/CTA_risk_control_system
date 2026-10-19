"""日终风控日报的文本输出。

清算数字本身在 `RiskSystem.settle` 里算完；这里只负责写成可读文件。
日报里每一行账户字段含义：

- 平仓盯市：今日已平仓相对基准价实现的盈亏
- 持仓盯市：收盘时仍持有的仓位，按今日结算价相对基准价的浮盈
- 手续费：今日开平仓手续费合计
- 权益：上日权益 + 平仓盯市 + 持仓盯市 − 手续费
- 保证金：按结算价占用的保证金；可用 = 权益 − 保证金
- 风险度：保证金 / 权益
- 当日盈亏比：当日净盈亏 / 初始资金
- 基准 / 结算：该笔持仓的盯市起点价，以及今日用于盯市的结算价
"""

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
        lines.append(
            f"  风险度 {row.get('risk_ratio', '0')}  "
            f"当日盈亏比 {row.get('daily_pnl_ratio', '0')}"
        )
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
