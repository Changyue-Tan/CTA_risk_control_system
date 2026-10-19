"""用同一套风控引擎回放历史行情和委托，验证规则是否按预期触发。"""

from __future__ import annotations

from datetime import datetime

from cta_risk.engine import RiskSystem
from cta_risk.models import OrderRequest, SystemConfig, Tick, money


def run_backtest(
    config: SystemConfig,
    ticks: list[Tick],
    orders: list[tuple[datetime, OrderRequest]],
) -> dict:
    """按时间排序回放。同一时刻先吃行情，再处理委托。"""
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
                    "instrument": payload.instrument,
                    "offset": payload.offset.value,
                }
            )
        point = {"ts": ts.isoformat(), "accounts": {}}
        for account_id in engine.accounts:
            metrics = engine.metrics(account_id)
            point["accounts"][account_id] = {
                "equity": money(metrics.equity),
                "daily_mtm": money(metrics.daily_mtm),
                "drawdown": format(metrics.drawdown, "f"),
                "halted": engine.accounts[account_id].halted,
            }
        curve.append(point)

    return {
        "points": len(curve),
        "events": [
            {
                "event_id": event.event_id,
                "account_id": event.account_id,
                "rule_id": event.rule_id,
                "level": event.level.value,
                "message": event.message,
            }
            for event in engine.events
        ],
        "halted": [account_id for account_id, account in engine.accounts.items() if account.halted],
        "orders": order_results,
        "curve": curve,
        "final": curve[-1] if curve else None,
    }
