from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import yaml

from cta_risk.models import (
    AccountSpec,
    BootstrapPosition,
    InstrumentSpec,
    PositionSide,
    RiskLimits,
    SystemConfig,
)


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_config_path() -> Path:
    return project_root() / "config" / "system.yaml"


def _dec(value: object) -> Decimal:
    return Decimal(str(value))


def load_config(path: Path | None = None) -> SystemConfig:
    raw = yaml.safe_load((path or default_config_path()).read_text(encoding="utf-8"))
    instruments = {
        name: InstrumentSpec(
            instrument_id=name,
            exchange=str(item["exchange"]),
            multiplier=_dec(item["multiplier"]),
            margin_rate=_dec(item["margin_rate"]),
            commission_per_lot=_dec(item["commission_per_lot"]),
        )
        for name, item in raw["instruments"].items()
    }
    accounts = [
        AccountSpec(str(item["id"]), str(item["name"]), _dec(item["initial_equity"]))
        for item in raw["accounts"]
    ]
    risk = raw["risk"]
    positions = [
        BootstrapPosition(
            str(item["account"]),
            str(item["instrument"]),
            PositionSide(str(item["side"])),
            int(item["qty"]),
            _dec(item["prev_settle"]),
        )
        for item in raw.get("bootstrap_positions", [])
    ]
    prices = {name: _dec(px) for name, px in raw["reference_prices"].items()}
    missing = set(instruments) - set(prices)
    if missing:
        raise ValueError(f"缺少参考价: {sorted(missing)}")
    return SystemConfig(
        trading_day=date.fromisoformat(str(raw["trading_day"])),
        instruments=instruments,
        accounts=accounts,
        risk=RiskLimits(
            daily_loss_alert_ratio=_dec(risk["daily_loss_alert_ratio"]),
            daily_loss_halt_ratio=_dec(risk["daily_loss_halt_ratio"]),
            max_drawdown_alert_ratio=_dec(risk["max_drawdown_alert_ratio"]),
            max_drawdown_restrict_ratio=_dec(risk["max_drawdown_restrict_ratio"]),
            concentration_alert_ratio=_dec(risk["concentration_alert_ratio"]),
            concentration_restrict_ratio=_dec(risk["concentration_restrict_ratio"]),
            max_instrument_notional_ratio=_dec(risk["max_instrument_notional_ratio"]),
        ),
        bootstrap_positions=positions,
        reference_prices=prices,
    )
