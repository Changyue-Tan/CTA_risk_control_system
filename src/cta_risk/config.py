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
    SimulatorConfig,
    SystemConfig,
)


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_config_path() -> Path:
    return project_root() / "config" / "system.yaml"


def _dec(value: object) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise TypeError(f"无法将 {value!r} 转为 Decimal")
    return Decimal(str(value))


def load_config(path: Path | None = None) -> SystemConfig:
    cfg_path = path or default_config_path()
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    instruments = {
        name: InstrumentSpec(
            instrument_id=name,
            exchange=str(item["exchange"]),
            product=str(item["product"]),
            multiplier=_dec(item["multiplier"]),
            tick_size=_dec(item["tick_size"]),
            margin_rate=_dec(item["margin_rate"]),
            commission_per_lot=_dec(item["commission_per_lot"]),
        )
        for name, item in raw["instruments"].items()
    }
    accounts = [
        AccountSpec(
            account_id=str(item["id"]),
            name=str(item["name"]),
            initial_equity=_dec(item["initial_equity"]),
        )
        for item in raw["accounts"]
    ]
    risk_raw = raw["risk"]
    risk = RiskLimits(
        daily_loss_circuit_breaker_ratio=_dec(risk_raw["daily_loss_circuit_breaker_ratio"]),
        daily_loss_alert_ratio=_dec(risk_raw["daily_loss_alert_ratio"]),
        max_drawdown_alert_ratio=_dec(risk_raw["max_drawdown_alert_ratio"]),
        max_drawdown_restrict_ratio=_dec(risk_raw["max_drawdown_restrict_ratio"]),
        instrument_margin_alert_ratio=_dec(risk_raw["instrument_margin_alert_ratio"]),
        instrument_margin_restrict_ratio=_dec(risk_raw["instrument_margin_restrict_ratio"]),
        concentration_alert_ratio=_dec(risk_raw["concentration_alert_ratio"]),
        concentration_restrict_ratio=_dec(risk_raw["concentration_restrict_ratio"]),
    )
    positions = [
        BootstrapPosition(
            account_id=str(item["account"]),
            instrument=str(item["instrument"]),
            side=PositionSide(str(item["side"])),
            qty=int(item["qty"]),
            prev_settle=_dec(item["prev_settle"]),
        )
        for item in raw.get("bootstrap_positions", [])
    ]
    prices = {name: _dec(px) for name, px in raw["reference_prices"].items()}
    sim_raw = raw.get("simulator", {})
    simulator = SimulatorConfig(
        enabled=bool(sim_raw.get("enabled", False)),
        interval_ms=int(sim_raw.get("interval_ms", 1000)),
        seed=int(sim_raw.get("seed", 7)),
        drop_primary_every=int(sim_raw.get("drop_primary_every", 8)),
    )
    missing_prices = set(instruments) - set(prices)
    if missing_prices:
        raise ValueError(f"缺少参考价: {sorted(missing_prices)}")
    return SystemConfig(
        trading_day=date.fromisoformat(str(raw["trading_day"])),
        instruments=instruments,
        accounts=accounts,
        risk=risk,
        bootstrap_positions=positions,
        reference_prices=prices,
        simulator=simulator,
    )
