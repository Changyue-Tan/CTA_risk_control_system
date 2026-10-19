from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from cta_risk.market import MarketHub, demonstrate_gap
from cta_risk.models import Tick


def _tick(source: str, seq: int, price: str, ts: datetime) -> Tick:
    return Tick(source, "rb2510", ts, seq, Decimal(price), date(2026, 7, 3))


def test_gap_replay_does_not_update_the_price_again():
    """主源断档后回补，价格只更新一次。"""
    result = demonstrate_gap()
    assert result["applied"] == 6
    assert result["applied_before_replay"] == 6
    assert result["duplicates"] == 4
    assert result["stale"] == 2
    assert result["gaps"] == 1
    assert result["filled"] == 1
    assert result["last"] == Decimal("3456")
    assert result["primary_seqs"] == [1, 2, 3, 4, 5, 6]


def test_stale_replay_does_not_move_the_market_backward():
    """更早的行情只留档，不把最新价打回去。"""
    hub = MarketHub()
    ts = datetime(2026, 7, 3, 10, 0, tzinfo=timezone.utc)
    assert hub.ingest(_tick("primary", 1, "110", ts + timedelta(seconds=1))) is True
    assert hub.ingest(_tick("primary", 2, "50", ts)) is False
    assert hub.stale == 1
    assert hub.last["rb2510"].last == Decimal("110")
