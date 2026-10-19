from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from cta_risk.config import load_config
from cta_risk.engine import (
    ALERT_DAILY,
    HALT_RULE,
    REJECT_CONCENTRATION,
    REJECT_DRAWDOWN,
    REJECT_EXPOSURE,
    REJECT_HALT,
    REJECT_POSITION,
    RiskSystem,
    three_percent_price,
)
from cta_risk.models import ActionLevel, BootstrapPosition, Offset, OrderRequest, PositionSide, Side, Tick
from tests.conftest import instrument, limits, make_account, make_config


def _engine():
    return RiskSystem(
        make_config(
            [instrument()],
            [make_account("gamma", "1000000")],
            positions=[BootstrapPosition("gamma", "rb2510", PositionSide.LONG, 10, Decimal("3050"))],
            prices={"rb2510": Decimal("3050")},
        )
    )


def _move(engine: RiskSystem, seq: int, price: str, minute: int = 0) -> None:
    ts = datetime(2026, 7, 3, 10, minute, tzinfo=timezone.utc)
    engine.on_tick(Tick("primary", "rb2510", ts, seq, Decimal(price), engine.trading_day))


def test_loss_short_of_three_percent_does_not_halt():
    """单日亏损未到初始资金的 3% 时不熔断。"""
    engine = _engine()
    _move(engine, 1, "2751")
    assert engine.accounts["gamma"].halted is False
    assert engine.metrics("gamma").daily_mtm == Decimal("-29900")


def test_three_percent_loss_halts_open_and_still_allows_close():
    """亏损刚好达到 3% 时拒绝开仓，平仓仍然可以成交。"""
    engine = _engine()
    _move(engine, 1, "2750")
    assert engine.metrics("gamma").daily_mtm == Decimal("-30000")
    opened = engine.submit_order(OrderRequest("gamma", "rb2510", Side.BUY, Offset.OPEN, Decimal("2750"), 1))
    assert opened.accepted is False
    assert opened.reject_reason == REJECT_HALT
    closed = engine.submit_order(OrderRequest("gamma", "rb2510", Side.SELL, Offset.CLOSE, Decimal("2750"), 1))
    assert closed.accepted is True
    assert closed.realized_pnl == Decimal("-3000")


def test_same_tick_from_backup_does_not_emit_a_second_halt():
    """备份源送来同一笔行情时，不重复发出熔断信号。"""
    engine = _engine()
    ts = datetime(2026, 7, 3, 10, 0, tzinfo=timezone.utc)
    engine.on_tick(Tick("primary", "rb2510", ts, 1, Decimal("2750"), engine.trading_day))
    engine.on_tick(Tick("backup", "rb2510", ts, 1, Decimal("2750"), engine.trading_day))
    halts = [event for event in engine.events if event.rule_id == HALT_RULE]
    assert len(halts) == 1
    assert engine.market.applied == 1


def test_halt_stays_until_resume():
    """熔断不会自动解除。亏损还在时，人工恢复会立刻再次熔断。"""
    engine = _engine()
    _move(engine, 1, "2750")
    assert engine.resume("gamma") is True
    _move(engine, 2, "3050", minute=5)
    rejected = engine.submit_order(OrderRequest("gamma", "rb2510", Side.BUY, Offset.OPEN, Decimal("3050"), 1))
    assert rejected.accepted is False
    assert engine.resume("gamma") is False
    opened = engine.submit_order(OrderRequest("gamma", "rb2510", Side.BUY, Offset.OPEN, Decimal("3050"), 1))
    assert opened.accepted is True


def test_accounts_stay_separate_and_gamma_halts_at_three_percent():
    """同一笔螺纹行情只熔断 Gamma。价格比昨结算低 150 点时，亏损正好是初始资金的 3%。"""
    engine = RiskSystem(load_config())
    instrument, price = three_percent_price(engine.config)
    engine.set_price(instrument, price)
    assert instrument == "rb2701"
    assert price == Decimal("2930")
    assert engine.accounts["gamma"].halted is True
    assert engine.accounts["alpha"].halted is False
    assert engine.accounts["beta"].halted is False
    assert engine.metrics("gamma").daily_mtm == Decimal("-30000")
    assert engine.metrics("beta").daily_mtm == Decimal("0")
    opened = engine.submit_order(OrderRequest("gamma", instrument, Side.BUY, Offset.OPEN, price, 1))
    assert opened.reject_reason == REJECT_HALT


def test_notional_cap_blocks_another_open():
    """单品种名义敞口超过权益的上限时，拒绝继续加仓。"""
    engine = RiskSystem(
        make_config(
            [instrument()],
            [make_account()],
            risk=limits(max_instrument_notional_ratio=Decimal("0.2")),
        )
    )
    result = engine.submit_order(OrderRequest("alpha", "rb2510", Side.BUY, Offset.OPEN, Decimal("3000"), 10))
    assert result.accepted is False
    assert result.reject_reason == REJECT_EXPOSURE


def test_positions_can_be_queried_by_account_and_instrument():
    """持仓可以按账户查询，也可以按合约查询。"""
    engine = RiskSystem(load_config())
    by_name = engine.query_positions(instrument="rb2701")
    assert {row["account_id"] for row in by_name} == {"alpha", "gamma"}
    beta = engine.query_positions(account_id="beta")
    assert {row["instrument"] for row in beta} == {"IF2612", "IC2612"}


def test_close_rejects_more_than_the_open_position():
    """平仓手数超过持仓时整笔拒绝。"""
    engine = _engine()
    result = engine.submit_order(OrderRequest("gamma", "rb2510", Side.SELL, Offset.CLOSE, Decimal("3050"), 11))
    assert result.reject_reason == REJECT_POSITION


def test_config_covers_three_exchanges():
    """样例配置里有上期所、大商所、中金所，熔断线是初始资金的 3%。"""
    config = load_config()
    assert {item.exchange for item in config.instruments.values()} == {"SHFE", "DCE", "CFFEX"}
    assert config.risk.daily_loss_halt_ratio == Decimal("0.03")
    assert config.risk.daily_loss_alert_ratio == Decimal("0.02")


def test_daily_loss_alert_fires_once_and_recovers():
    """当日亏损越过 2% 告警线只发一次，价格回到成本后发恢复事件。"""
    engine = _engine()
    _move(engine, 1, "2850")
    assert engine.metrics("gamma").daily_mtm == Decimal("-20000")
    alerts = [event for event in engine.events if event.rule_id == ALERT_DAILY]
    assert len(alerts) == 1
    assert alerts[0].level == ActionLevel.ALERT
    # 仍在告警区内再跌一点，不应重复告警。
    _move(engine, 2, "2849", minute=1)
    assert len([event for event in engine.events if event.rule_id == ALERT_DAILY]) == 1
    _move(engine, 3, "3050", minute=2)
    recoveries = [
        event
        for event in engine.events
        if event.rule_id == ALERT_DAILY and event.level == ActionLevel.RECOVERY
    ]
    assert len(recoveries) == 1
    assert engine.accounts["gamma"].halted is False


def test_drawdown_restrict_blocks_open():
    """最大回撤达到限制线时拒绝开仓；当日亏损线故意抬高，避免先触发熔断。"""
    engine = RiskSystem(
        make_config(
            [instrument()],
            [make_account("alpha", "1000000")],
            positions=[BootstrapPosition("alpha", "rb2510", PositionSide.LONG, 10, Decimal("3000"))],
            prices={"rb2510": Decimal("3000")},
            risk=limits(
                daily_loss_alert_ratio=Decimal("0.5"),
                daily_loss_halt_ratio=Decimal("0.5"),
                max_drawdown_alert_ratio=Decimal("0.02"),
                max_drawdown_restrict_ratio=Decimal("0.05"),
            ),
        )
    )
    _move(engine, 1, "2500")
    assert engine.metrics("alpha").drawdown == Decimal("0.05")
    opened = engine.submit_order(OrderRequest("alpha", "rb2510", Side.BUY, Offset.OPEN, Decimal("2500"), 1))
    assert opened.accepted is False
    assert opened.reject_reason == REJECT_DRAWDOWN


def test_concentration_restrict_blocks_add_on_top_instrument():
    """两品种时，最大敞口占比超过限制线后禁止继续加该品种，允许加小品种。"""
    engine = RiskSystem(
        make_config(
            [
                instrument(),
                instrument("i2601", exchange="DCE", multiplier="100", margin_rate="0.12", commission="3"),
            ],
            [make_account("alpha", "1000000")],
            positions=[
                BootstrapPosition("alpha", "rb2510", PositionSide.LONG, 20, Decimal("3000")),
                BootstrapPosition("alpha", "i2601", PositionSide.LONG, 1, Decimal("700")),
            ],
            prices={"rb2510": Decimal("3000"), "i2601": Decimal("700")},
            risk=limits(max_instrument_notional_ratio=Decimal("10")),
        )
    )
    blocked = engine.submit_order(OrderRequest("alpha", "rb2510", Side.BUY, Offset.OPEN, Decimal("3000"), 1))
    assert blocked.accepted is False
    assert blocked.reject_reason == REJECT_CONCENTRATION
    allowed = engine.submit_order(OrderRequest("alpha", "i2601", Side.BUY, Offset.OPEN, Decimal("700"), 1))
    assert allowed.accepted is True
