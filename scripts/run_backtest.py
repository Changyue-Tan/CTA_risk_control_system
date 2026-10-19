"""合成回放：Gamma 螺纹阴跌，依次触发 2% 告警、3% 熔断，开仓被拒。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from cta_risk.backtest import run_backtest
from cta_risk.config import load_config
from cta_risk.models import Offset, OrderRequest, Side, Tick

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def main() -> None:
    config = load_config()
    day = config.trading_day
    base = datetime(day.year, day.month, day.day, 10, 0, tzinfo=timezone.utc)
    # 3080 -> 2980 亏 2 万（2%）-> 2930 亏 3 万（3%）
    ticks = [
        Tick("primary", "rb2701", base + timedelta(minutes=1), 1, Decimal("2980"), day),
        Tick("primary", "rb2701", base + timedelta(minutes=2), 2, Decimal("2930"), day),
    ]
    order_ts = base + timedelta(minutes=3)
    orders = [
        (
            order_ts,
            OrderRequest("gamma", "rb2701", Side.BUY, Offset.OPEN, Decimal("2930"), 1),
        )
    ]
    result = run_backtest(config, ticks, orders)
    print("== 回测：Gamma 螺纹阴跌 ==")
    for event in result["events"]:
        if event["account_id"] != "gamma":
            continue
        print(f"[{event['level']}] {event['rule_id']}: {event['message']}")
    order = result["orders"][0]
    print(f"开仓受理: {'是' if order['accepted'] else '否'}，原因: {order['reject_reason']}")
    gamma = result["final"]["accounts"]["gamma"]
    print(f"最终盯市 {gamma['daily_mtm']}，熔断 {'是' if gamma['halted'] else '否'}")


if __name__ == "__main__":
    main()
