"""逐日盯市清算与风控日报。

清算由 `settle_accounts(engine, prices)` 完成；`write_report` 只负责写成可读文件。
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

import logging
from datetime import timedelta
from decimal import Decimal

from cta_risk.models import AccountState, money, mtm_pnl

logger = logging.getLogger("cta_risk.settlement")


def settle_accounts(engine, prices: dict[str, Decimal]) -> dict:
    """逐日盯市（mark-to-market）清算。调用方须已持有引擎锁。

    对每个账户：用今日结算价给未平仓计持仓盯市，加上今日已平仓盯市，
    扣手续费得到结算权益；然后把留仓基准改成今日结算价，交易日 +1，
    当日盈亏计数器清零——下一交易日的浮盈从新的「昨结算」起算。
    """
    missing = sorted({lot.instrument for lot in engine.positions.lots} - set(prices))
    if missing:
        raise ValueError(f"缺少结算价: {missing}")
    day = engine.trading_day
    rows = []
    firm_equity = Decimal(0)
    for account_id in sorted(engine.accounts):
        row, equity = _settle_account(engine, account_id, prices)
        rows.append(row)
        firm_equity += equity
    for instrument, price in prices.items():
        if instrument in engine.market.last:
            engine.market.set_settlement_price(instrument, price)
    engine.trading_day = day + timedelta(days=1)
    path = engine.report_dir / f"risk_report_{day.strftime('%Y%m%d')}.txt"
    write_report(path, day.isoformat(), rows)
    logger.info("完成 %s 清算，公司权益 %s", day.isoformat(), money(firm_equity))
    return {
        "trading_day": day.isoformat(),
        "text_path": str(path),
        "firm_equity": money(firm_equity),
        "accounts": rows,
    }


def _settle_account(engine, account_id: str, prices: dict[str, Decimal]) -> tuple[dict, Decimal]:
    """清算一个账户并滚入下一交易日。

    结算权益 = 上日权益 + 平仓盯市 + 持仓盯市 − 手续费
    """
    account = engine.accounts[account_id]
    lines, position_pnl, margin = _settlement_lines(engine, account_id, prices)
    equity = account.prev_equity + account.realized_pnl + position_pnl - account.commission
    daily_pnl = account.realized_pnl + position_pnl - account.commission
    initial = account.spec.initial_equity
    row = {
        "account_id": account_id,
        "name": account.spec.name,
        "close_pnl": money(account.realized_pnl),
        "position_pnl": money(position_pnl),
        "commission": money(account.commission),
        "equity": money(equity),
        "margin": money(margin),
        "available": money(equity - margin),
        # 风险度 = 保证金 / 权益；当日盈亏比 = 当日净盈亏 / 初始资金。
        "risk_ratio": format(margin / equity, "f") if equity > 0 else "0",
        "daily_pnl_ratio": format(daily_pnl / initial, "f") if initial else "0",
        "halted": account.halted,
        "positions": lines,
    }
    _roll_to_next_day(engine, account, prices, equity)
    return row, equity


def _settlement_lines(engine, account_id: str, prices: dict[str, Decimal]) -> tuple[list[dict], Decimal, Decimal]:
    """按结算价给每笔留仓算持仓盯市和保证金，并生成日报持仓明细。"""
    position_pnl = Decimal(0)
    margin = Decimal(0)
    lines = []
    for lot in engine.positions.lots_of(account_id):
        spec = engine.config.instruments[lot.instrument]
        settle_px = prices[lot.instrument]
        # 相对 lot 当前盯市基准（昨结算或今开仓价）盯到今日结算价。
        pnl = mtm_pnl(lot.side, settle_px, lot.mtm_basis, lot.qty, spec.multiplier)
        gross = settle_px * lot.qty * spec.multiplier
        position_pnl += pnl
        margin += gross * spec.margin_rate
        lines.append(
            {
                "instrument": lot.instrument,
                "side": lot.side.value,
                "qty": lot.qty,
                "basis": money(lot.mtm_basis),
                "settle": money(settle_px),
                "pnl": money(pnl),
                "notional": money(gross),
            }
        )
    return lines, position_pnl, margin


def _roll_to_next_day(engine, account: AccountState, prices: dict[str, Decimal], equity: Decimal) -> None:
    """日终滚存：留仓基准 ← 今日结算价；当日盈亏/手续费清零；权益成为明日的上日权益。"""
    for lot in engine.positions.lots_of(account.spec.account_id):
        lot.mtm_basis = prices[lot.instrument]
    account.prev_equity = equity
    if equity > account.peak_equity:
        account.peak_equity = equity
    account.realized_pnl = Decimal(0)
    account.commission = Decimal(0)


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
