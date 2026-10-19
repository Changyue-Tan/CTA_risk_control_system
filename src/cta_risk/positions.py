from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from cta_risk.models import (
    BootstrapPosition,
    InstrumentSpec,
    OpenLot,
    PositionSide,
    Side,
    money,
    mtm_pnl,
)


class PositionBook:
    """双向持仓。平仓按开仓日、手数编号先开先平。"""

    def __init__(self) -> None:
        self.lots: list[OpenLot] = []
        self.trades: list[dict] = []

    def bootstrap(self, position: BootstrapPosition, trading_day: date, lot_id: str) -> None:
        self.lots.append(
            OpenLot(
                lot_id=lot_id,
                account_id=position.account_id,
                instrument=position.instrument,
                side=position.side,
                qty=position.qty,
                open_price=position.prev_settle,
                open_day=trading_day - timedelta(days=1),
                prev_settle=position.prev_settle,
            )
        )

    def qty(self, account_id: str, instrument: str, side: PositionSide) -> int:
        return sum(
            lot.qty
            for lot in self.lots
            if lot.account_id == account_id and lot.instrument == instrument and lot.side == side
        )

    def close(self, account_id: str, instrument: str, order_side: Side, qty: int, price: Decimal, multiplier: Decimal) -> Decimal:
        """卖出平多头，买入平空头。返回这笔平仓的盯市盈亏。"""
        side = PositionSide.LONG if order_side == Side.SELL else PositionSide.SHORT
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
            pnl += mtm_pnl(lot.side, price, lot.prev_settle, take, multiplier)
            lot.qty -= take
            remain -= take
            if remain == 0:
                break
        if remain:
            raise RuntimeError("可平仓位不足")
        self.lots = [lot for lot in self.lots if lot.qty > 0]
        return pnl

    def floating(self, account_id: str, price_of, instruments: dict[str, InstrumentSpec]) -> Decimal:
        total = Decimal(0)
        for lot in self.lots_of(account_id):
            spec = instruments[lot.instrument]
            total += mtm_pnl(lot.side, price_of(lot.instrument), lot.prev_settle, lot.qty, spec.multiplier)
        return total

    def notional(self, account_id: str, instrument: str, price_of, instruments: dict[str, InstrumentSpec]) -> Decimal:
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
            basis = sum((lot.prev_settle * lot.qty for lot in lots), Decimal(0)) / qty
            floating = sum(mtm_pnl(side, last, lot.prev_settle, lot.qty, spec.multiplier) for lot in lots)
            gross = last * qty * spec.multiplier
            rows.append(
                {
                    "account_id": acc,
                    "instrument": inst,
                    "exchange": spec.exchange,
                    "side": side.value,
                    "qty": qty,
                    "prev_settle": format(basis, "f"),
                    "last": format(last, "f"),
                    "floating_pnl": money(floating),
                    "margin": money(gross * spec.margin_rate),
                    "notional": money(gross),
                }
            )
        return rows

    def lots_of(self, account_id: str) -> list[OpenLot]:
        return [lot for lot in self.lots if lot.account_id == account_id]
