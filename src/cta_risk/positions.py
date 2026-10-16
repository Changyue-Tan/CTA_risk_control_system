from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from cta_risk.models import (
    BootstrapPosition,
    InstrumentSpec,
    OpenLot,
    PositionSide,
    PositionView,
    Side,
    Trade,
    mtm_pnl,
)


class PositionBook:
    """双向持仓。平仓按开仓日、手数编号先开先平。"""

    def __init__(self) -> None:
        self.lots: list[OpenLot] = []
        self.trades: list[Trade] = []

    def add_lot(self, lot: OpenLot) -> None:
        self.lots.append(lot)

    def bootstrap(self, position: BootstrapPosition, trading_day: date, lot_id: str) -> None:
        if position.qty <= 0:
            raise ValueError("昨仓手数必须大于 0")
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

    def position_qty(self, account_id: str, instrument: str, side: PositionSide) -> int:
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
        """卖平多、买平空。返回本笔平仓盯市盈亏。"""
        position_side = PositionSide.LONG if order_side == Side.SELL else PositionSide.SHORT
        candidates = [
            lot
            for lot in self.lots
            if lot.account_id == account_id
            and lot.instrument == instrument
            and lot.side == position_side
            and lot.qty > 0
        ]
        candidates.sort(key=lambda lot: (lot.open_day, lot.lot_id))
        remain = qty
        pnl = Decimal(0)
        for lot in candidates:
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
        for lot in self.lots:
            if lot.account_id != account_id:
                continue
            spec = instruments[lot.instrument]
            total += mtm_pnl(lot.side, price_of(lot.instrument), lot.prev_settle, lot.qty, spec.multiplier)
        return total

    def margin_and_notional(
        self,
        account_id: str,
        price_of,
        instruments: dict[str, InstrumentSpec],
    ) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
        margin: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
        notional: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
        for lot in self.lots:
            if lot.account_id != account_id:
                continue
            spec = instruments[lot.instrument]
            price = price_of(lot.instrument)
            value = price * lot.qty * spec.multiplier
            notional[lot.instrument] += value
            margin[lot.instrument] += value * spec.margin_rate
        return dict(margin), dict(notional)

    def views(self, account_id: str | None, instrument: str | None, price_of, instruments) -> list[PositionView]:
        grouped: dict[tuple[str, str, PositionSide], list[OpenLot]] = defaultdict(list)
        for lot in self.lots:
            if account_id and lot.account_id != account_id:
                continue
            if instrument and lot.instrument != instrument:
                continue
            grouped[(lot.account_id, lot.instrument, lot.side)].append(lot)

        rows: list[PositionView] = []
        for (acc, inst, side), lots in sorted(grouped.items()):
            spec = instruments[inst]
            qty = sum(lot.qty for lot in lots)
            last = price_of(inst)
            open_amount = sum(lot.open_price * lot.qty for lot in lots)
            basis_amount = sum(lot.prev_settle * lot.qty for lot in lots)
            floating = sum(mtm_pnl(side, last, lot.prev_settle, lot.qty, spec.multiplier) for lot in lots)
            gross = last * qty * spec.multiplier
            rows.append(
                PositionView(
                    account_id=acc,
                    instrument=inst,
                    exchange=spec.exchange,
                    product=spec.product,
                    side=side,
                    qty=qty,
                    open_price=open_amount / qty,
                    prev_settle=basis_amount / qty,
                    last=last,
                    multiplier=spec.multiplier,
                    floating_pnl=floating,
                    margin=gross * spec.margin_rate,
                    notional=gross,
                )
            )
        return rows
