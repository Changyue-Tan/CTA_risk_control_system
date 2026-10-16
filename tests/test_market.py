from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from cta_risk.market import LiveFeed, MarketHub, run_gap_demo
from cta_risk.models import InstrumentSpec, Tick


def _tick(source: str, seq: int, price: str, ts: datetime, instrument: str = "rb2510") -> Tick:
    px = Decimal(price)
    return Tick(source, instrument, ts, seq, px, px - 1, px + 1, seq, date(2026, 7, 3))


def test_gap_replay_fills_tape_without_extra_price_updates():
    result = run_gap_demo()
    assert result["gap_detected"] is True
    assert result["missing"] == [4, 5]
    assert result["applied"] == 6
    assert result["applied_before_replay"] == 6
    assert result["replay_did_not_add_prices"] is True
    # 主源 1–3 与备份重复；4 和 5 早于备份已经写入的第 6 笔，只进流水；第 6 笔回补时与备份重复。
    assert result["duplicates"] == 4
    assert result["stale"] == 2
    assert result["gaps_detected"] == 1
    assert result["gaps_filled"] == 1
    assert result["open_gaps"] == 0
    assert result["final_last"] == "3456"
    assert result["primary_seqs"] == [1, 2, 3, 4, 5, 6]
    assert result["risk_updates"] == 6


def test_primary_overrides_backup_disagreement_once():
    hub = MarketHub()
    ts = datetime(2026, 7, 3, 10, 0, tzinfo=timezone.utc)
    assert hub.ingest(_tick("backup", 1, "100", ts)).applied is True
    corrected = hub.ingest(_tick("primary", 1, "101", ts))
    assert corrected.applied is True
    assert hub.latest["rb2510"].last == Decimal("101")
    ignored = hub.ingest(_tick("backup", 2, "99", ts))
    assert ignored.applied is False
    assert hub.latest["rb2510"].last == Decimal("101")
    assert hub.disagreement_count == 2


def test_stale_replay_does_not_move_the_market_backward():
    hub = MarketHub()
    ts = datetime(2026, 7, 3, 10, 0, tzinfo=timezone.utc)
    later = ts + timedelta(seconds=1)
    assert hub.ingest(_tick("primary", 1, "110", later)).applied is True
    stale = hub.ingest(_tick("primary", 2, "50", ts))
    assert stale.applied is False
    assert stale.stale is True
    assert hub.stale_count == 1
    assert hub.latest["rb2510"].last == Decimal("110")
    assert [item.seq for item in hub.tape] == [1, 2]


def test_live_feed_outage_replays_a_contiguous_tape():
    instruments = {
        "rb2510": InstrumentSpec("rb2510", "SHFE", "rb", Decimal(10), Decimal(1), Decimal("0.1"), Decimal(1)),
        "cu2510": InstrumentSpec("cu2510", "SHFE", "cu", Decimal(5), Decimal(10), Decimal("0.1"), Decimal(1)),
    }
    prices = {"rb2510": Decimal("3450"), "cu2510": Decimal("74500")}
    feed = LiveFeed(instruments, prices, date(2026, 7, 3), seed=7, drop_primary_every=5)
    hub = MarketHub()
    for _ in range(11):
        for kind, payload in feed.next_actions():
            if kind == "tick":
                hub.ingest(payload)
            else:
                hub.set_online(payload, kind == "online")

    assert hub.gaps_detected == 4
    assert hub.gaps_filled == 4
    open_gaps = sum(row["open_gaps"] for row in hub.stats()["sources"])
    assert open_gaps == 0
    assert hub.duplicate_count > 0
    assert hub.stale_count > 0

    seqs: dict[str, list[int]] = defaultdict(list)
    for tick in hub.tape:
        if tick.source == "primary":
            seqs[tick.instrument].append(tick.seq)
    assert set(seqs) == {"rb2510", "cu2510"}
    for values in seqs.values():
        assert values == list(range(1, values[-1] + 1))
