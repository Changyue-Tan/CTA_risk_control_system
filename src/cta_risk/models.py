from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class PositionSide(str, Enum):
    LONG = "long"
    SHORT = "short"


class Offset(str, Enum):
    OPEN = "open"
    CLOSE = "close"


def money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def direction(side: PositionSide) -> Decimal:
    return Decimal(1) if side == PositionSide.LONG else Decimal(-1)


def mtm_pnl(side: PositionSide, price: Decimal, basis: Decimal, qty: int, multiplier: Decimal) -> Decimal:
    """盯市盈亏。多头为 (价 - 基准) × 手数 × 乘数，空头相反。

    今仓的基准是开仓价，昨仓的基准是上一交易日结算价。
    """
    return (price - basis) * qty * multiplier * direction(side)


@dataclass(frozen=True)
class InstrumentSpec:
    instrument_id: str
    exchange: str
    multiplier: Decimal
    margin_rate: Decimal
    commission_per_lot: Decimal


@dataclass(frozen=True)
class AccountSpec:
    account_id: str
    name: str
    initial_equity: Decimal


@dataclass(frozen=True)
class RiskLimits:
    # 当日盯市亏损达到初始资金的这个比例时熔断。
    daily_loss_halt_ratio: Decimal
    # 单品种名义本金 / 权益 达到这个比例时，禁止继续加该品种。
    max_instrument_notional_ratio: Decimal


@dataclass(frozen=True)
class BootstrapPosition:
    account_id: str
    instrument: str
    side: PositionSide
    qty: int
    prev_settle: Decimal


@dataclass
class SystemConfig:
    trading_day: date
    instruments: dict[str, InstrumentSpec]
    accounts: list[AccountSpec]
    risk: RiskLimits
    bootstrap_positions: list[BootstrapPosition]
    reference_prices: dict[str, Decimal]


@dataclass(frozen=True)
class Tick:
    source: str
    instrument: str
    exchange_ts: datetime
    seq: int
    last: Decimal
    trading_day: date


@dataclass
class OrderRequest:
    account_id: str
    instrument: str
    side: Side
    offset: Offset
    price: Decimal
    qty: int


@dataclass
class OrderResult:
    accepted: bool
    order_id: str | None
    reject_reason: str | None
    fill_price: Decimal | None
    fill_qty: int
    realized_pnl: Decimal


@dataclass
class OpenLot:
    lot_id: str
    account_id: str
    instrument: str
    side: PositionSide
    qty: int
    open_price: Decimal
    open_day: date
    prev_settle: Decimal


@dataclass
class AccountState:
    spec: AccountSpec
    prev_equity: Decimal
    realized_pnl: Decimal = Decimal(0)
    commission: Decimal = Decimal(0)
    halted: bool = False


@dataclass
class RiskEvent:
    event_id: str
    account_id: str
    rule_id: str
    message: str
