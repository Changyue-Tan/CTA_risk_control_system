from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from cta_risk.backtest import run_backtest
from cta_risk.config import load_config
from cta_risk.engine import ALERT_DAILY, HALT_RULE, REJECT_HALT
from cta_risk.models import Offset, OrderRequest, Side, Tick


def test_backtest_replays_gamma_alert_then_halt():
    """回放螺纹阴跌：先发 2% 告警，再发 3% 熔断，开仓被拒。"""
    config = load_config()
    day = config.trading_day
    base = datetime(day.year, day.month, day.day, 10, 0, tzinfo=timezone.utc)
    ticks = [
        Tick("primary", "rb2701", base + timedelta(minutes=1), 1, Decimal("2980"), day),
        Tick("primary", "rb2701", base + timedelta(minutes=2), 2, Decimal("2930"), day),
    ]
    order_ts = base + timedelta(minutes=3)
    result = run_backtest(
        config,
        ticks,
        [(order_ts, OrderRequest("gamma", "rb2701", Side.BUY, Offset.OPEN, Decimal("2930"), 1))],
    )
    gamma_events = [event for event in result["events"] if event["account_id"] == "gamma"]
    rules = [event["rule_id"] for event in gamma_events]
    assert ALERT_DAILY in rules
    assert HALT_RULE in rules
    assert result["orders"][0]["accepted"] is False
    assert result["orders"][0]["reject_reason"] == REJECT_HALT
    assert result["final"]["accounts"]["gamma"]["halted"] is True
    assert result["final"]["accounts"]["gamma"]["daily_mtm"] == "-30000.00"
    assert result["points"] == 3
