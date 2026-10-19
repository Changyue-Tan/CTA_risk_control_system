"""持仓账本：按 lot 记录开仓，平仓时先开先平，并按盯市基准计盈亏。

期货持仓不是「一个账户一个净仓」这么简单。本系统允许同一账户、同一合约
同时有多头和空头（双向持仓）。每一笔开仓生成一个 `OpenLot`；平仓时按
(开仓日, lot_id) 排序，先平较早的仓——这就是常见的 FIFO。

盈亏怎么算：平仓价 / 最新价 相对 lot 的 `mtm_basis`（盯市基准）做 `mtm_pnl`。
不要用 `open_price` 去算昨仓的当日盈亏——昨仓的当日盈亏相对昨结算。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from cta_risk.models import (
    InstrumentSpec,
    OpenLot,
    PositionSide,
    PriorPosition,
    Side,
    money,
    mtm_pnl,
    position_side_to_close,
)


class PositionBook:
    """双向持仓账本。"""

    def __init__(self) -> None:
        self.lots: list[OpenLot] = []
        self.trades: list[dict] = []

    def load_prior_positions(self, position: PriorPosition, trading_day: date, lot_id: str) -> None:
        """装入昨仓：开仓日记为上一自然日，盯市基准 = 昨结算。"""
        self.lots.append(
            OpenLot(
                lot_id=lot_id,
                account_id=position.account_id,
                instrument=position.instrument,
                side=position.side,
                qty=position.qty,
                open_price=position.prev_settle,
                open_day=trading_day - timedelta(days=1),
                mtm_basis=position.prev_settle,
            )
        )

    def qty(self, account_id: str, instrument: str, side: PositionSide) -> int:
        """某账户、某合约、某方向的可平手数合计。"""
        return sum(
            lot.qty
            for lot in self.lots
            if lot.account_id == account_id and lot.instrument == instrument and lot.side == side
        )

    def close(
        self,
        account_id: str,
        instrument: str,
        order_side: Side,
        qty: int,
        price: Decimal,
        multiplier: Decimal,
    ) -> Decimal:
        """平仓：卖出平多头，买入平空头。返回这笔平仓的盯市盈亏。

        盈亏相对被平掉那些 lot 的盯市基准计算，所以先平昨仓时用的是昨结算，
        而不是更早的原始开仓价。
        """
        side = position_side_to_close(order_side)
        lots = [
            lot
            for lot in self.lots
            if lot.account_id == account_id and lot.instrument == instrument and lot.side == side
        ]
        lots.sort(key=lambda lot: (lot.open_day, lot.lot_id))
        remain = qty
        pnl = Decimal(0)
        for lot in lots:
            take = min(lot.qty, remain)
            pnl += mtm_pnl(lot.side, price, lot.mtm_basis, take, multiplier)
            lot.qty -= take
            remain -= take
            if remain == 0:
                break
        if remain:
            raise RuntimeError("可平仓位不足")
        self.lots = [lot for lot in self.lots if lot.qty > 0]
        return pnl

    def floating(self, account_id: str, price_of, instruments: dict[str, InstrumentSpec]) -> Decimal:
        """持仓浮盈：所有未平 lot 相对各自盯市基准、按最新价计算的盯市合计。"""
        total = Decimal(0)
        for lot in self.lots_of(account_id):
            spec = instruments[lot.instrument]
            total += mtm_pnl(lot.side, price_of(lot.instrument), lot.mtm_basis, lot.qty, spec.multiplier)
        return total

    def notional(self, account_id: str, instrument: str, price_of, instruments: dict[str, InstrumentSpec]) -> Decimal:
        """单品种名义本金 = Σ 最新价 × 手数 × 乘数（多空都按绝对值计入规模）。"""
        total = Decimal(0)
        for lot in self.lots_of(account_id):
            if lot.instrument != instrument:
                continue
            spec = instruments[instrument]
            total += price_of(instrument) * lot.qty * spec.multiplier
        return total

    def rows(self, account_id: str | None, instrument: str | None, price_of, instruments) -> list[dict]:
        """按账户、合约、方向汇总，直接返回页面和接口用的一行。"""
        grouped: dict[tuple[str, str, PositionSide], list[OpenLot]] = defaultdict(list)
        for lot in self.lots:
            if account_id and lot.account_id != account_id:
                continue
            if instrument and lot.instrument != instrument:
                continue
            grouped[(lot.account_id, lot.instrument, lot.side)].append(lot)

        rows = []
        for (acc, inst, side), lots in sorted(grouped.items()):
            spec = instruments[inst]
            qty = sum(lot.qty for lot in lots)
            last = price_of(inst)
            # 多笔 lot 基准可能不同，这里用手数加权平均，仅供展示。
            mtm_basis = sum((lot.mtm_basis * lot.qty for lot in lots), Decimal(0)) / qty
            floating = sum(mtm_pnl(side, last, lot.mtm_basis, lot.qty, spec.multiplier) for lot in lots)
            gross = last * qty * spec.multiplier
            rows.append(
                {
                    "account_id": acc,
                    "instrument": inst,
                    "exchange": spec.exchange,
                    "side": side.value,
                    "qty": qty,
                    "mtm_basis": format(mtm_basis, "f"),
                    "last": format(last, "f"),
                    "floating_pnl": money(floating),
                    "margin": money(gross * spec.margin_rate),
                    "notional": money(gross),
                }
            )
        return rows

    def lots_of(self, account_id: str) -> list[OpenLot]:
        return [lot for lot in self.lots if lot.account_id == account_id]
