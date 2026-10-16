from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from cta_risk.config import load_config
from cta_risk.engine import RiskSystem
from cta_risk.models import (
    BootstrapPosition,
    Offset,
    OrderRequest,
    PositionSide,
    Side,
    Tick,
)
from cta_risk.risk import HALT_DAILY, REJECT_CONCENTRATION, REJECT_DRAWDOWN, REJECT_EXPOSURE, REJECT_HALT
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
    engine.on_tick(
        Tick(
            "primary",
            "rb2510",
            ts,
            seq,
            Decimal(price),
            Decimal(price) - 1,
            Decimal(price) + 1,
            seq,
            engine.trading_day,
        )
    )


def test_loss_short_of_three_percent_does_not_halt():
    engine = _engine()
    _move(engine, 1, "2751")
    assert engine.accounts["gamma"].halted is False
    assert engine.metrics("gamma").daily_mtm == Decimal("-29900")


def test_three_percent_loss_halts_open_and_still_allows_close():
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
    engine = _engine()
    ts = datetime(2026, 7, 3, 10, 0, tzinfo=timezone.utc)
    primary = Tick("primary", "rb2510", ts, 1, Decimal("2750"), Decimal("2749"), Decimal("2751"), 1, engine.trading_day)
    backup = Tick("backup", "rb2510", ts, 1, Decimal("2750"), Decimal("2749"), Decimal("2751"), 1, engine.trading_day)
    engine.on_tick(primary)
    engine.on_tick(backup)
    halts = [event for event in engine.risk.events if event.rule_id == HALT_DAILY]
    assert len(halts) == 1
    assert engine.market.applied_count == 1


def test_halt_stays_until_resume_and_resume_rechecks_the_loss():
    engine = _engine()
    _move(engine, 1, "2750")
    resumed = engine.resume("gamma")
    assert resumed["halted"] is True
    _move(engine, 2, "3050", minute=5)
    assert engine.accounts["gamma"].halted is True
    rejected = engine.submit_order(OrderRequest("gamma", "rb2510", Side.BUY, Offset.OPEN, Decimal("3050"), 1))
    assert rejected.accepted is False
    resumed = engine.resume("gamma")
    assert resumed["halted"] is False
    opened = engine.submit_order(OrderRequest("gamma", "rb2510", Side.BUY, Offset.OPEN, Decimal("3050"), 1))
    assert opened.accepted is True


def test_accounts_are_isolated_when_the_same_price_moves():
    engine = RiskSystem(load_config())
    ts = datetime(2026, 7, 3, 10, 0, tzinfo=timezone.utc)
    engine.on_tick(Tick("primary", "rb2510", ts, 1, Decimal("3300"), Decimal("3299"), Decimal("3301"), 1, engine.trading_day))
    assert engine.accounts["gamma"].halted is True
    assert engine.accounts["alpha"].halted is False
    assert engine.accounts["beta"].halted is False
    assert engine.metrics("gamma").daily_mtm == Decimal("-30000")
    assert engine.metrics("beta").daily_mtm == Decimal("0")


def test_yaml_acceptance_shock_matches_the_three_percent_case():
    engine = RiskSystem(load_config())
    trace = engine.acceptance_shock("gamma")
    assert trace["target_price"] == "3300"
    assert trace["daily_mtm"] == "-30000.00"
    assert trace["threshold"] == "-30000.00"
    assert trace["halted"] is True
    assert trace["open_accepted"] is False
    assert "熔断" in trace["open_reject_reason"]
    assert trace["close_accepted"] is True


def test_drawdown_restricts_new_opens():
    engine = _engine()
    engine.accounts["gamma"].peak_equity = Decimal("1000000")
    engine.accounts["gamma"].prev_equity = Decimal("900000")
    result = engine.submit_order(OrderRequest("gamma", "rb2510", Side.BUY, Offset.OPEN, Decimal("3050"), 1))
    assert result.accepted is False
    assert result.reject_reason == REJECT_DRAWDOWN


def test_single_instrument_margin_cap_blocks_the_add():
    engine = RiskSystem(
        make_config(
            [instrument()],
            [make_account()],
            risk=limits(instrument_margin_restrict_ratio=Decimal("0.01")),
        )
    )
    result = engine.submit_order(OrderRequest("alpha", "rb2510", Side.BUY, Offset.OPEN, Decimal("3000"), 10))
    assert result.accepted is False
    assert result.reject_reason == REJECT_EXPOSURE


def test_concentration_blocks_only_when_two_products_are_open():
    copper = instrument("cu2510", product="cu", multiplier="5", tick_size="10", margin_rate="0.01")
    engine = RiskSystem(
        make_config(
            [instrument(margin_rate="0.01"), copper],
            [make_account(equity="10000000")],
            prices={"rb2510": Decimal("3000"), "cu2510": Decimal("70000")},
            risk=limits(concentration_restrict_ratio=Decimal("0.80"), instrument_margin_restrict_ratio=Decimal("0.90")),
        )
    )
    assert engine.submit_order(OrderRequest("alpha", "rb2510", Side.BUY, Offset.OPEN, Decimal("3000"), 10)).accepted
    assert engine.submit_order(
        OrderRequest("alpha", "cu2510", Side.BUY, Offset.OPEN, Decimal("70000"), 1)
    ).accepted
    # 螺纹 10 手名义 30 万，铜 1 手名义 35 万。再开 37 手螺纹后，螺纹占比超过 80%。
    blocked = engine.submit_order(OrderRequest("alpha", "rb2510", Side.BUY, Offset.OPEN, Decimal("3000"), 37))
    assert blocked.accepted is False
    assert blocked.reject_reason == REJECT_CONCENTRATION


def test_positions_can_be_queried_by_account_and_instrument():
    engine = RiskSystem(load_config())
    by_name = engine.query_positions(instrument="rb2510")
    assert {row["account_id"] for row in by_name} == {"alpha", "gamma"}
    beta = engine.query_positions(account_id="beta")
    assert {row["instrument"] for row in beta} == {"IF2510", "IC2510"}
    assert engine.query_positions(account_id="beta", instrument="rb2510") == []


def test_close_rejects_more_than_the_open_position():
    engine = _engine()
    result = engine.submit_order(OrderRequest("gamma", "rb2510", Side.SELL, Offset.CLOSE, Decimal("3050"), 11))
    assert result.accepted is False
    assert "可平" in result.reject_reason
