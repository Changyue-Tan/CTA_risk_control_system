from __future__ import annotations

from decimal import Decimal

from cta_risk.engine import RiskSystem
from cta_risk.models import Offset, OrderRequest, Side
from tests.conftest import instrument, make_account, make_config


def _book(tmp_path):
    return RiskSystem(
        make_config([instrument()], [make_account()], prices={"rb2510": Decimal("3000")}),
        report_dir=tmp_path,
    )


def test_mark_to_market_settles_yesterday_and_today_lots(tmp_path):
    """今仓和昨仓按各自基准做逐日盯市，并写出日报。"""
    engine = _book(tmp_path)
    assert engine.submit_order(OrderRequest("alpha", "rb2510", Side.BUY, Offset.OPEN, Decimal("3000"), 10)).accepted
    first = engine.settle({"rb2510": Decimal("3050")})
    assert first["accounts"][0]["equity"] == "1004980.00"
    assert first["accounts"][0]["position_pnl"] == "5000.00"
    assert first["accounts"][0]["commission"] == "20.00"
    assert engine.trading_day.isoformat() == "2026-07-04"

    closed = engine.submit_order(OrderRequest("alpha", "rb2510", Side.SELL, Offset.CLOSE, Decimal("3100"), 4))
    assert closed.realized_pnl == Decimal("2000")
    second = engine.settle({"rb2510": Decimal("3080")})
    account = second["accounts"][0]
    assert account["close_pnl"] == "2000.00"
    assert account["position_pnl"] == "1800.00"
    assert account["commission"] == "8.00"
    assert account["equity"] == "1008772.00"
    assert account["margin"] == "18480.00"
    # 风险度 = 18480 / 1008772；当日净盈亏 = 2000 + 1800 − 8 = 3792，相对初始资金 100 万。
    assert account["risk_ratio"] == format(Decimal("18480") / Decimal("1008772"), "f")
    assert account["daily_pnl_ratio"] == format(Decimal("3792") / Decimal("1000000"), "f")
    text = (tmp_path / "risk_report_20260704.txt").read_text(encoding="utf-8")
    assert "1008772.00" in text
    assert "名义敞口" in text
    assert "风险度" in text
    assert "当日盈亏比" in text


def test_short_position_profits_when_settlement_falls(tmp_path):
    """结算价下跌时，空头产生盈利。"""
    engine = _book(tmp_path)
    engine.submit_order(OrderRequest("alpha", "rb2510", Side.SELL, Offset.OPEN, Decimal("3000"), 2))
    report = engine.settle({"rb2510": Decimal("2900")})
    assert report["accounts"][0]["position_pnl"] == "2000.00"
    assert report["accounts"][0]["equity"] == "1001996.00"


def test_close_uses_fifo_and_the_older_basis(tmp_path):
    """平仓先平昨仓，盈亏按昨结算价计算。"""
    engine = _book(tmp_path)
    engine.submit_order(OrderRequest("alpha", "rb2510", Side.BUY, Offset.OPEN, Decimal("3000"), 5))
    engine.settle({"rb2510": Decimal("3000")})
    engine.submit_order(OrderRequest("alpha", "rb2510", Side.BUY, Offset.OPEN, Decimal("3100"), 5))
    closed = engine.submit_order(OrderRequest("alpha", "rb2510", Side.SELL, Offset.CLOSE, Decimal("3200"), 5))
    assert closed.realized_pnl == Decimal("10000")
    rows = engine.query_positions(account_id="alpha", instrument="rb2510")
    assert rows[0]["qty"] == 5
    assert Decimal(rows[0]["mtm_basis"]) == Decimal("3100")
