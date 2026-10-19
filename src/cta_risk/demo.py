"""现场演示里页面演不了的两段：断线补数，以及阴跌回放。

3% 熔断由监控台上的「熔断验收」按钮演示，这里不再打一遍。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from cta_risk.backtest import run_backtest
from cta_risk.config import load_config
from cta_risk.market import MarketHub
from cta_risk.models import Offset, OrderRequest, Side, SystemConfig, Tick


def replay_gap() -> dict:
    """主源缺 4、5 两笔。备份先顶上，主源再按序号补回，价格不重复更新。"""
    hub = MarketHub()
    instrument = "rb2510"
    day_base = datetime(2026, 7, 3, 9, 0, tzinfo=timezone.utc)
    day = day_base.date()

    def tick(source: str, seq: int) -> Tick:
        return Tick(source, instrument, day_base + timedelta(seconds=seq), seq, Decimal(3450 + seq), day)

    for seq in (1, 2, 3):
        hub.ingest(tick("backup", seq))
        hub.ingest(tick("primary", seq))
    for seq in (4, 5, 6):
        hub.ingest(tick("backup", seq))
    hub.ingest(tick("primary", 6))
    applied_before_replay = hub.applied_count
    hub.ingest(tick("primary", 4))
    hub.ingest(tick("primary", 5))
    return {
        "applied": hub.applied_count,
        "applied_before_replay": applied_before_replay,
        "duplicates": hub.duplicate_count,
        "stale": hub.stale_count,
        "last": hub.last[instrument].last,
        "primary_seqs": [item.seq for item in hub.tape if item.source == "primary"],
    }


def replay_decline(config: SystemConfig | None = None) -> dict:
    """Gamma 螺纹从昨结算跌到 2% 告警，再跌到 3% 熔断，随后开仓被拒。"""
    config = load_config() if config is None else config
    day = config.trading_day
    base = datetime(day.year, day.month, day.day, 10, 0, tzinfo=timezone.utc)
    ticks = [
        Tick("primary", "rb2701", base + timedelta(minutes=1), 1, Decimal("2980"), day),
        Tick("primary", "rb2701", base + timedelta(minutes=2), 2, Decimal("2930"), day),
    ]
    order_ts = base + timedelta(minutes=3)
    return run_backtest(
        config,
        ticks,
        [(order_ts, OrderRequest("gamma", "rb2701", Side.BUY, Offset.OPEN, Decimal("2930"), 1))],
    )


def main() -> None:
    logging.getLogger("cta_risk").setLevel(logging.CRITICAL)
    gap = replay_gap()
    print("== 行情缺口回补 ==")
    print(f"回补前价格更新 {gap['applied_before_replay']} 次，回补后仍是 {gap['applied']} 次")
    print(f"主源序号 {gap['primary_seqs']}，去重 {gap['duplicates']} 笔，过期回补 {gap['stale']} 笔")
    print(f"最新价停在 {gap['last']}")

    result = replay_decline()
    print("\n== 回测：Gamma 螺纹阴跌 ==")
    for event in result["events"]:
        if event["account_id"] != "gamma":
            continue
        print(f"[{event['level']}] {event['rule_id']}: {event['message']}")
    order = result["orders"][0]
    accepted = "是" if order["accepted"] else "否"
    print(f"开仓受理: {accepted}，原因: {order['reject_reason']}")
    gamma = result["final"]["accounts"]["gamma"]
    halted = "是" if gamma["halted"] else "否"
    print(f"最终盯市 {gamma['daily_mtm']}，熔断 {halted}")


if __name__ == "__main__":
    main()
