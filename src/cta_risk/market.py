from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

from cta_risk.models import IngestResult, InstrumentSpec, Tick

logger = logging.getLogger("cta_risk.market")


@dataclass
class _SourceState:
    last_seq: dict[str, int] = field(default_factory=dict)
    seen: dict[str, set[int]] = field(default_factory=dict)
    buffer: dict[str, dict[int, Tick]] = field(default_factory=dict)
    gaps: dict[str, list[tuple[int, int]]] = field(default_factory=dict)


class MarketHub:
    """双源行情合并。

    同一交易所时间戳只采用一次价格，避免两个源把同一笔行情触发两次风控。
    序号出现空洞时先缓存、不前推价格；缺口按序号补齐后再继续。
    旧时间戳只进入流水，不把最新价打回去，因此回补不会制造错误信号。
    同一时间戳两边价格不一致时，主源可以覆盖备份源，且只覆盖一次。
    """

    def __init__(self) -> None:
        self.latest: dict[str, Tick] = {}
        self.tape: list[Tick] = []
        self.canonical_keys: set[tuple[str, datetime]] = set()
        self.applied_count = 0
        self.duplicate_count = 0
        self.stale_count = 0
        self.gaps_detected = 0
        self.gaps_filled = 0
        self.disagreement_count = 0
        self._sources: dict[str, _SourceState] = {}
        self._online: dict[str, bool] = {}

    def set_online(self, source: str, online: bool) -> None:
        self._online[source] = online
        self._state(source)

    def seed(self, instrument: str, price: Decimal, trading_day: date) -> None:
        ts = datetime.combine(trading_day, time(0, 0), tzinfo=timezone.utc)
        tick = Tick("reference", instrument, ts, 0, price, price, price, 0, trading_day)
        self.latest[instrument] = tick
        self.canonical_keys.add((instrument, ts))

    def overwrite_last(self, instrument: str, price: Decimal) -> None:
        current = self.latest[instrument]
        self.latest[instrument] = Tick(
            "settlement",
            instrument,
            current.exchange_ts,
            current.seq,
            price,
            price,
            price,
            current.volume,
            current.trading_day,
        )

    def stats(self) -> dict:
        return {
            "applied": self.applied_count,
            "duplicates": self.duplicate_count,
            "stale": self.stale_count,
            "gaps_detected": self.gaps_detected,
            "gaps_filled": self.gaps_filled,
            "disagreements": self.disagreement_count,
            "sources": [
                {
                    "name": name,
                    "online": self._online.get(name, True),
                    "open_gaps": sum(len(gaps) for gaps in state.gaps.values()),
                    "last_seq": dict(state.last_seq),
                }
                for name, state in self._sources.items()
            ],
        }

    def ingest(self, tick: Tick) -> IngestResult:
        state = self._state(tick.source)
        seen = state.seen.setdefault(tick.instrument, set())
        buffered = state.buffer.setdefault(tick.instrument, {})
        if tick.seq < 1 or tick.seq in seen or tick.seq in buffered:
            self.duplicate_count += 1
            return IngestResult(False, True, False, False, False, False)

        last = state.last_seq.get(tick.instrument, 0)
        if tick.seq > last + 1:
            buffered[tick.seq] = tick
            missing_from = last + 1
            missing_to = tick.seq - 1
            state.gaps.setdefault(tick.instrument, []).append((missing_from, missing_to))
            self.gaps_detected += 1
            logger.info(
                "行情缺口 %s %s 序号 %s-%s",
                tick.source,
                tick.instrument,
                missing_from,
                missing_to,
            )
            return IngestResult(
                False, False, False, True, False, True, missing_from, missing_to
            )
        if tick.seq != last + 1:
            self.duplicate_count += 1
            return IngestResult(False, True, False, False, False, False)

        flags = [self._commit(tick)]
        nxt = state.last_seq.get(tick.instrument, 0) + 1
        while nxt in buffered:
            flags.append(self._commit(buffered.pop(nxt)))
            nxt = state.last_seq.get(tick.instrument, 0) + 1
        self._close_gaps(state, tick.instrument)
        applied = "applied" in flags
        return IngestResult(
            applied=applied,
            duplicate=not applied and "duplicate" in flags,
            stale=not applied and "stale" in flags,
            gap_detected=False,
            disagreement=not applied and "disagreement" in flags,
            buffered=False,
        )

    def _commit(self, tick: Tick) -> str:
        """消费一个连续序号。返回 applied / duplicate / stale / disagreement / stored。"""
        state = self._state(tick.source)
        state.seen.setdefault(tick.instrument, set()).add(tick.seq)
        state.last_seq[tick.instrument] = tick.seq
        self.tape.append(tick)

        current = self.latest.get(tick.instrument)
        key = (tick.instrument, tick.exchange_ts)
        if current is not None and tick.exchange_ts < current.exchange_ts:
            self.stale_count += 1
            return "stale"

        if key in self.canonical_keys:
            if (
                current is not None
                and tick.exchange_ts == current.exchange_ts
                and tick.last != current.last
            ):
                self.disagreement_count += 1
                if tick.source == "primary" and current.source != "primary":
                    self.latest[tick.instrument] = tick
                    self.applied_count += 1
                    return "applied"
                return "disagreement"
            self.duplicate_count += 1
            return "duplicate"

        self.canonical_keys.add(key)
        self.latest[tick.instrument] = tick
        if current is None or tick.last != current.last:
            self.applied_count += 1
            return "applied"
        return "stored"

    def _close_gaps(self, state: _SourceState, instrument: str) -> None:
        last = state.last_seq.get(instrument, 0)
        remaining: list[tuple[int, int]] = []
        for start, end in state.gaps.get(instrument, []):
            if last >= end:
                self.gaps_filled += 1
            else:
                remaining.append((start, end))
        state.gaps[instrument] = remaining

    def _state(self, source: str) -> _SourceState:
        if source not in self._sources:
            self._sources[source] = _SourceState()
            self._online.setdefault(source, True)
        return self._sources[source]


class LiveFeed:
    """主源 + 备份源。备份源始终连续；主源定期丢序号，下一拍再回补。"""

    def __init__(
        self,
        instruments: dict[str, InstrumentSpec],
        prices: dict[str, Decimal],
        trading_day: date,
        seed: int,
        drop_primary_every: int,
    ) -> None:
        self.instruments = instruments
        self.prices = dict(prices)
        self.trading_day = trading_day
        self.rng = random.Random(seed)
        self.drop_every = drop_primary_every
        self.step = 0
        self.clock = datetime.combine(trading_day, time(9, 0), tzinfo=timezone.utc)
        names = sorted(prices)
        self.names = names
        self.seqs = {
            "primary": {name: 0 for name in names},
            "backup": {name: 0 for name in names},
        }
        self.pending: list[Tick] = []

    def set_prices(self, prices: dict[str, Decimal]) -> None:
        for name, price in prices.items():
            if name in self.prices:
                self.prices[name] = price

    def next_actions(self) -> list[tuple[str, Tick | str]]:
        actions: list[tuple[str, Tick | str]] = []
        if self.pending:
            actions.append(("online", "primary"))
            actions.extend(("tick", tick) for tick in self.pending)
            self.pending.clear()

        self.step += 1
        backup_ticks: list[Tick] = []
        primary_ticks: list[Tick] = []
        for name in self.names:
            backup, primary = self._advance(name)
            backup_ticks.append(backup)
            primary_ticks.append(primary)
        actions.extend(("tick", tick) for tick in backup_ticks)

        if self.drop_every and self.step % self.drop_every == 0:
            actions.append(("offline", "primary"))
            self.pending.extend(primary_ticks)
            for name in self.names:
                backup, primary = self._advance(name)
                actions.append(("tick", backup))
                actions.append(("tick", primary))
        else:
            actions.extend(("tick", tick) for tick in primary_ticks)
        return actions

    def _advance(self, instrument: str) -> tuple[Tick, Tick]:
        spec = self.instruments[instrument]
        shock = Decimal(str(round(self.rng.uniform(-0.0015, 0.0015), 6)))
        raw = self.prices[instrument] * (Decimal(1) + shock)
        steps = (raw / spec.tick_size).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        price = max(spec.tick_size, steps * spec.tick_size)
        self.prices[instrument] = price
        self.clock += timedelta(milliseconds=200)
        ts = self.clock

        def make(source: str) -> Tick:
            self.seqs[source][instrument] += 1
            return Tick(
                source,
                instrument,
                ts,
                self.seqs[source][instrument],
                price,
                price - spec.tick_size,
                price + spec.tick_size,
                0,
                self.trading_day,
            )

        return make("backup"), make("primary")


def run_gap_demo() -> dict:
    """主源断档、备份源顶上、主源回补。同一时间戳只触发一次价格更新。"""
    hub = MarketHub()
    instrument = "rb2510"
    trading_day = date(2026, 7, 3)
    base = datetime(2026, 7, 3, 1, 30, tzinfo=timezone.utc)

    def tick(source: str, seq: int) -> Tick:
        price = Decimal(3450 + seq)
        return Tick(
            source,
            instrument,
            base + timedelta(seconds=seq),
            seq,
            price,
            price - 1,
            price + 1,
            100 + seq,
            trading_day,
        )

    for seq in (1, 2, 3):
        hub.ingest(tick("backup", seq))
        hub.ingest(tick("primary", seq))
    for seq in (4, 5, 6):
        hub.ingest(tick("backup", seq))
    gap = hub.ingest(tick("primary", 6))
    applied_before_replay = hub.applied_count
    hub.ingest(tick("primary", 4))
    hub.ingest(tick("primary", 5))
    primary_seqs = [item.seq for item in hub.tape if item.source == "primary"]
    return {
        "gap_detected": gap.gap_detected,
        "missing": [gap.missing_from, gap.missing_to],
        "applied": hub.applied_count,
        "applied_before_replay": applied_before_replay,
        "replay_did_not_add_prices": hub.applied_count == applied_before_replay,
        "duplicates": hub.duplicate_count,
        "stale": hub.stale_count,
        "gaps_detected": hub.gaps_detected,
        "gaps_filled": hub.gaps_filled,
        "open_gaps": sum(len(gaps) for state in hub._sources.values() for gaps in state.gaps.values()),
        "final_last": format(hub.latest[instrument].last, "f"),
        "primary_seqs": primary_seqs,
        "risk_updates": hub.applied_count,
    }
