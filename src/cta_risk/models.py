from __future__ import annotations

from dataclasses import dataclass, field
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


class ActionLevel(str, Enum):
    ALERT = "alert"
    RESTRICT = "restrict"
    HALT = "halt"
    RECOVERY = "recovery"


def money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def ratio_text(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP), "f")


def pct(value: Decimal) -> str:
    points = (value * Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(points, "f") + "%"


def position_direction(side: PositionSide) -> Decimal:
    return Decimal(1) if side == PositionSide.LONG else Decimal(-1)


def mtm_pnl(side: PositionSide, price: Decimal, basis: Decimal, qty: int, multiplier: Decimal) -> Decimal:
    """盯市盈亏。多头 (价-基准)*手数*乘数，空头相反。

    基准价：今仓用开仓价，昨仓用上一交易日结算价。调用方把基准放在 basis 里。
    """
    return (price - basis) * qty * multiplier * position_direction(side)


@dataclass(frozen=True)
class InstrumentSpec:
    instrument_id: str
    exchange: str
    product: str
    multiplier: Decimal
    tick_size: Decimal
    margin_rate: Decimal
    commission_per_lot: Decimal


@dataclass(frozen=True)
class AccountSpec:
    account_id: str
    name: str
    initial_equity: Decimal


@dataclass(frozen=True)
class RiskLimits:
    daily_loss_circuit_breaker_ratio: Decimal
    daily_loss_alert_ratio: Decimal
    max_drawdown_alert_ratio: Decimal
    max_drawdown_restrict_ratio: Decimal
    instrument_margin_alert_ratio: Decimal
    instrument_margin_restrict_ratio: Decimal
    concentration_alert_ratio: Decimal
    concentration_restrict_ratio: Decimal


@dataclass(frozen=True)
class BootstrapPosition:
    account_id: str
    instrument: str
    side: PositionSide
    qty: int
    prev_settle: Decimal


@dataclass(frozen=True)
class SimulatorConfig:
    enabled: bool
    interval_ms: int
    seed: int
    drop_primary_every: int


@dataclass
class SystemConfig:
    trading_day: date
    instruments: dict[str, InstrumentSpec]
    accounts: list[AccountSpec]
    risk: RiskLimits
    bootstrap_positions: list[BootstrapPosition]
    reference_prices: dict[str, Decimal]
    simulator: SimulatorConfig


@dataclass(frozen=True)
class Tick:
    source: str
    instrument: str
    exchange_ts: datetime
    seq: int
    last: Decimal
    bid: Decimal
    ask: Decimal
    volume: int
    trading_day: date


@dataclass
class IngestResult:
    applied: bool
    duplicate: bool
    stale: bool
    gap_detected: bool
    disagreement: bool
    buffered: bool
    missing_from: int | None = None
    missing_to: int | None = None


@dataclass
class OrderRequest:
    account_id: str
    instrument: str
    side: Side
    offset: Offset
    price: Decimal
    qty: int
    trading_day: date | None = None
    ts: datetime | None = None


@dataclass
class OrderResult:
    accepted: bool
    order_id: str | None
    reject_reason: str | None
    fill_price: Decimal | None
    fill_qty: int
    realized_pnl: Decimal


@dataclass
class RejectRecord:
    ts: datetime
    trading_day: date
    account_id: str
    instrument: str
    side: str
    offset: str
    qty: int
    reason: str


@dataclass
class Trade:
    trade_id: str
    order_id: str
    account_id: str
    instrument: str
    side: Side
    offset: Offset
    price: Decimal
    qty: int
    realized_pnl: Decimal
    commission: Decimal
    trading_day: date
    ts: datetime


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
    peak_equity: Decimal = Decimal(0)
    halted: bool = False
    halt_reason: str = ""


@dataclass
class RiskEvent:
    event_id: str
    ts: datetime
    trading_day: date
    account_id: str
    rule_id: str
    level: ActionLevel
    instrument: str | None
    message: str
    value: str


@dataclass
class AccountMetrics:
    equity: Decimal
    available: Decimal
    daily_mtm: Decimal
    daily_mtm_ratio: Decimal
    drawdown: Decimal
    margin: Decimal
    margin_ratio: Decimal
    floating: Decimal
    realized: Decimal
    commission: Decimal
    concentration: Decimal
    instrument_count: int
    margin_by_instrument: dict[str, Decimal] = field(default_factory=dict)
    notional_by_instrument: dict[str, Decimal] = field(default_factory=dict)


@dataclass
class PositionView:
    account_id: str
    instrument: str
    exchange: str
    product: str
    side: PositionSide
    qty: int
    open_price: Decimal
    prev_settle: Decimal
    last: Decimal
    multiplier: Decimal
    floating_pnl: Decimal
    margin: Decimal
    notional: Decimal
