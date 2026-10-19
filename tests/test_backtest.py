from __future__ import annotations

from cta_risk.demo import replay_decline
from cta_risk.engine import ALERT_DAILY, HALT_RULE, REJECT_HALT


def test_backtest_replays_gamma_alert_then_halt():
    """回放螺纹阴跌：先发 2% 告警，再发 3% 熔断，开仓被拒。"""
    result = replay_decline()
    gamma_events = [event for event in result["events"] if event["account_id"] == "gamma"]
    rules = [event["rule_id"] for event in gamma_events]
    assert ALERT_DAILY in rules
    assert HALT_RULE in rules
    assert result["orders"][0]["accepted"] is False
    assert result["orders"][0]["reject_reason"] == REJECT_HALT
    assert result["final"]["accounts"]["gamma"]["halted"] is True
    assert result["final"]["accounts"]["gamma"]["daily_mtm"] == "-30000.00"
    assert result["points"] == 3
