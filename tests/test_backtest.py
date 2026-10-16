from decimal import Decimal

from cta_risk.backtest import builtin_demo
from cta_risk.config import load_config


def test_builtin_replay_rejects_the_open_after_the_loss_limit():
    result = builtin_demo()
    assert result["open_accepted"] is False
    assert result["halted"] == ["alpha"]
    assert any(event["rule_id"] == "daily_loss_circuit_breaker" for event in result["events"])
    assert "熔断" in result["orders"][0]["reject_reason"]


def test_config_file_has_the_three_exchanges_and_accounts():
    config = load_config()
    exchanges = {item.exchange for item in config.instruments.values()}
    assert exchanges == {"SHFE", "DCE", "CFFEX"}
    assert {item.account_id for item in config.accounts} == {"alpha", "beta", "gamma"}
    assert config.risk.daily_loss_circuit_breaker_ratio == Decimal("0.03")
