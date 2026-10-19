from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from cta_risk.models import Tick

# 交易日从北京时间 0 点算起。占位参考价必须早于当天的日盘收盘，否则 15:00 的行情会被当成过期数据。
BEIJING = timezone(timedelta(hours=8))


class MarketHub:
    """把主源和备份源收成每个合约一个最新价。

    同一个交易所时间戳只采用一次。序号跳号时先缓存，等缺的序号到了再继续。
    时间早于当前最新价的回补只留档，不把价格打回去。
    """

    def __init__(self) -> None:
        self.last: dict[str, Tick] = {}
        self.tape: list[Tick] = []
        self.applied = 0
        self.duplicates = 0
        self.stale = 0
        self.gaps = 0
        self.filled = 0
        self._seq: dict[tuple[str, str], int] = {}
        self._buffer: dict[tuple[str, str], dict[int, Tick]] = {}
        self._seen_ts: set[tuple[str, datetime]] = set()

    def seed(self, instrument: str, price: Decimal, trading_day: date) -> None:
        ts = datetime.combine(trading_day, time(0, 0), tzinfo=BEIJING)
        self.last[instrument] = Tick("参考价", instrument, ts, 0, price, trading_day)
        self._seen_ts.add((instrument, ts))

    def mark(self, instrument: str, price: Decimal) -> None:
        current = self.last[instrument]
        self.last[instrument] = Tick(
            current.source,
            instrument,
            current.exchange_ts,
            current.seq,
            price,
            current.trading_day,
        )

    def ingest(self, tick: Tick) -> bool:
        """价格相对最新价发生变化时返回 True。"""
        if not self._seq_is_next(tick):
            return False
        changed = self._move_last_price(tick)
        return self._release_buffered(tick) or changed

    def _seq_is_next(self, tick: Tick) -> bool:
        """序号必须正好是下一笔。跳号先缓存，等缺的序号到了再放行。"""
        key = (tick.source, tick.instrument)
        expected = self._seq.get(key, 0) + 1
        if tick.seq < expected or tick.seq < 1:
            self.duplicates += 1
            return False
        if tick.seq > expected:
            self._buffer.setdefault(key, {})[tick.seq] = tick
            self.gaps += 1
            return False
        return True

    def _release_buffered(self, tick: Tick) -> bool:
        """当前序号落地后，把紧跟着、已经到了的序号按顺序放出来。"""
        pending = self._buffer.get((tick.source, tick.instrument), {})
        changed = False
        nxt = tick.seq + 1
        while nxt in pending:
            changed = self._move_last_price(pending.pop(nxt)) or changed
            self.filled += 1
            nxt += 1
        return changed

    def _move_last_price(self, tick: Tick) -> bool:
        """同一时间戳只用一次。时间早于当前最新价的只留档，不把价格打回去。"""
        self._seq[(tick.source, tick.instrument)] = tick.seq
        self.tape.append(tick)
        current = self.last.get(tick.instrument)
        if current is not None and tick.exchange_ts < current.exchange_ts:
            self.stale += 1
            return False
        stamp = (tick.instrument, tick.exchange_ts)
        if stamp in self._seen_ts:
            self.duplicates += 1
            return False
        self._seen_ts.add(stamp)
        self.last[tick.instrument] = tick
        if current is None or tick.last != current.last:
            self.applied += 1
            return True
        return False


def demonstrate_gap() -> dict:
    """主源缺了 4、5 两笔。备份源先顶上，主源再按序号补回，价格不重复更新。"""
    hub = MarketHub()
    instrument = "rb2510"
    day = date(2026, 7, 3)
    base = datetime(2026, 7, 3, 9, 0, tzinfo=timezone.utc)

    def tick(source: str, seq: int) -> Tick:
        return Tick(source, instrument, base + timedelta(seconds=seq), seq, Decimal(3450 + seq), day)

    # 1–3：备份先到并改价，主源是同一时间戳，不再改价。
    for seq in (1, 2, 3):
        hub.ingest(tick("backup", seq))
        hub.ingest(tick("primary", seq))
    # 4–6：只有备份。主源直接跳到 6，先缓存，价格停在备份的 6。
    for seq in (4, 5, 6):
        hub.ingest(tick("backup", seq))
    hub.ingest(tick("primary", 6))
    before = hub.applied
    # 主源补上 4、5 后放行缓存的 6。这三笔时间都更早，价格保持 3456。
    hub.ingest(tick("primary", 4))
    hub.ingest(tick("primary", 5))
    return {
        "applied": hub.applied,
        "applied_before_replay": before,
        "duplicates": hub.duplicates,
        "stale": hub.stale,
        "gaps": hub.gaps,
        "filled": hub.filled,
        "last": hub.last[instrument].last,
        "primary_seqs": [item.seq for item in hub.tape if item.source == "primary"],
    }
