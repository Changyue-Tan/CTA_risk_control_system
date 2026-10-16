from __future__ import annotations

from decimal import Decimal

from cta_risk.engine import RiskSystem
from cta_risk.models import Offset, OrderRequest, Side
from tests.conftest import instrument, make_account, make_config


def _book(tmp_path):
    engine = RiskSystem(
        make_config([instrument()], [make_account()], prices={"rb2510": Decimal("3000")}),
        report_dir=tmp_path,
    )
    return engine


def test_mark_to_market_settles_yesterday_and_today_lots(tmp_path):
    engine = _book(tmp_path)
    opened = engine.submit_order(OrderRequest("alpha", "rb2510", Side.BUY, Offset.OPEN, Decimal("3000"), 10))
    assert opened.accepted is True
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
    text = (tmp_path / "risk_report_20260704.txt").read_text(encoding="utf-8")
    assert "1008772.00" in text
    assert (tmp_path / "risk_report_20260704.html").exists()


def test_short_position_profits_when_settlement_falls(tmp_path):
    engine = _book(tmp_path)
    engine.submit_order(OrderRequest("alpha", "rb2510", Side.SELL, Offset.OPEN, Decimal("3000"), 2))
    report = engine.settle({"rb2510": Decimal("2900")})
    assert report["accounts"][0]["position_pnl"] == "2000.00"
    assert report["accounts"][0]["commission"] == "4.00"
    assert report["accounts"][0]["equity"] == "1001996.00"


def test_close_uses_fifo_and_the_older_basis(tmp_path):
    engine = _book(tmp_path)
    engine.submit_order(OrderRequest("alpha", "rb2510", Side.BUY, Offset.OPEN, Decimal("3000"), 5))
    engine.settle({"rb2510": Decimal("3000")})
    engine.submit_order(OrderRequest("alpha", "rb2510", Side.BUY, Offset.OPEN, Decimal("3100"), 5))
    closed = engine.submit_order(OrderRequest("alpha", "rb2510", Side.SELL, Offset.CLOSE, Decimal("3200"), 5))
    assert closed.realized_pnl == Decimal("10000")
    rows = engine.query_positions(account_id="alpha", instrument="rb2510")
    assert len(rows) == 1
    assert rows[0]["qty"] == 5
    assert Decimal(rows[0]["prev_settle"]) == Decimal("3100")
