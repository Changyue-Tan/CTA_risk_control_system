from __future__ import annotations

from datetime import date
from decimal import Decimal

from cta_risk.models import (
    AccountSpec,
    InstrumentSpec,
    RiskLimits,
    SimulatorConfig,
    SystemConfig,
)


def instrument(
    instrument_id: str = "rb2510",
    exchange: str = "SHFE",
    product: str = "rb",
    multiplier: str = "10",
    tick_size: str = "1",
    margin_rate: str = "0.10",
    commission: str = "2",
) -> InstrumentSpec:
    return InstrumentSpec(
        instrument_id,
        exchange,
        product,
        Decimal(multiplier),
        Decimal(tick_size),
        Decimal(margin_rate),
        Decimal(commission),
    )


def make_account(account_id: str = "alpha", equity: str = "1000000", name: str | None = None) -> AccountSpec:
    return AccountSpec(account_id, name or account_id, Decimal(equity))


def limits(**overrides) -> RiskLimits:
    base = dict(
        daily_loss_circuit_breaker_ratio=Decimal("0.03"),
        daily_loss_alert_ratio=Decimal("0.015"),
        max_drawdown_alert_ratio=Decimal("0.05"),
        max_drawdown_restrict_ratio=Decimal("0.08"),
        instrument_margin_alert_ratio=Decimal("0.35"),
        instrument_margin_restrict_ratio=Decimal("0.50"),
        concentration_alert_ratio=Decimal("0.60"),
        concentration_restrict_ratio=Decimal("0.80"),
    )
    base.update(overrides)
    return RiskLimits(**base)


def make_config(
    instruments: list[InstrumentSpec],
    accounts: list[AccountSpec],
    positions=None,
    prices=None,
    risk: RiskLimits | None = None,
) -> SystemConfig:
    book = {item.instrument_id: item for item in instruments}
    if prices is None:
        prices = {name: Decimal("3000") for name in book}
    return SystemConfig(
        trading_day=date(2026, 7, 3),
        instruments=book,
        accounts=accounts,
        risk=risk or limits(),
        bootstrap_positions=positions or [],
        reference_prices=prices,
        simulator=SimulatorConfig(False, 1000, 1, 0),
    )
