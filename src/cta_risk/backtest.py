from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from cta_risk.engine import RiskSystem
from cta_risk.models import (
    AccountSpec,
    BootstrapPosition,
    InstrumentSpec,
    Offset,
    OrderRequest,
    PositionSide,
    RiskLimits,
    Side,
    SimulatorConfig,
    SystemConfig,
    Tick,
)
from cta_risk.risk import event_dict


def run_backtest(config: SystemConfig, ticks: list[Tick], orders: list[tuple[datetime, OrderRequest]]) -> dict:
    """用同一套风控引擎回放历史行情和委托。"""
    engine = RiskSystem(config)
    stream: list[tuple] = [("tick", tick.exchange_ts, index, tick) for index, tick in enumerate(ticks)]
    stream.extend(("order", ts, index, order) for index, (ts, order) in enumerate(orders))
    stream.sort(key=lambda item: (item[1], 0 if item[0] == "tick" else 1, item[2]))
    curve = []
    order_results = []
    for kind, ts, _index, payload in stream:
        if kind == "tick":
            engine.on_tick(payload)
        else:
            result = engine.submit_order(payload)
            order_results.append(
                {
                    "ts": ts.isoformat(),
                    "accepted": result.accepted,
                    "reject_reason": result.reject_reason,
                    "account_id": payload.account_id,
                }
            )
        point = {"ts": ts.isoformat(), "accounts": {}}
        for account_id in engine.accounts:
            metrics = engine.metrics(account_id)
            point["accounts"][account_id] = {
                "equity": format(metrics.equity, "f"),
                "daily_mtm": format(metrics.daily_mtm, "f"),
                "halted": engine.accounts[account_id].halted,
            }
        curve.append(point)
    return {
        "points": len(curve),
        "events": [event_dict(event) for event in engine.risk.events],
        "halted": [account_id for account_id, account in engine.accounts.items() if account.halted],
        "orders": order_results,
        "final": curve[-1] if curve else None,
    }


def builtin_demo() -> dict:
    """固定样本：10 手螺纹昨仓，价格打到初始资金 3% 的亏损后再开仓，应当被拒绝。"""
    instrument = InstrumentSpec(
        "rb2510",
        "SHFE",
        "rb",
        Decimal(10),
        Decimal(1),
        Decimal("0.10"),
        Decimal(2),
    )
    day = date(2026, 7, 3)
    config = SystemConfig(
        trading_day=day,
        instruments={"rb2510": instrument},
        accounts=[AccountSpec("alpha", "回测账户", Decimal(1_000_000))],
        risk=RiskLimits(
            daily_loss_circuit_breaker_ratio=Decimal("0.03"),
            daily_loss_alert_ratio=Decimal("0.015"),
            max_drawdown_alert_ratio=Decimal("0.05"),
            max_drawdown_restrict_ratio=Decimal("0.08"),
            instrument_margin_alert_ratio=Decimal("0.35"),
            instrument_margin_restrict_ratio=Decimal("0.50"),
            concentration_alert_ratio=Decimal("0.60"),
            concentration_restrict_ratio=Decimal("0.80"),
        ),
        bootstrap_positions=[
            BootstrapPosition("alpha", "rb2510", PositionSide.LONG, 10, Decimal(3050))
        ],
        reference_prices={"rb2510": Decimal(3050)},
        simulator=SimulatorConfig(False, 1000, 1, 0),
    )
    shock_ts = datetime(2026, 7, 3, 10, 15, tzinfo=timezone.utc)
    order_ts = shock_ts + timedelta(seconds=1)
    tick = Tick(
        "primary",
        "rb2510",
        shock_ts,
        1,
        Decimal(2750),
        Decimal(2749),
        Decimal(2751),
        10,
        day,
    )
    order = OrderRequest("alpha", "rb2510", Side.BUY, Offset.OPEN, Decimal(2750), 1, day, order_ts)
    result = run_backtest(config, [tick], [(order_ts, order)])
    result["open_accepted"] = result["orders"][0]["accepted"] if result["orders"] else None
    return result
