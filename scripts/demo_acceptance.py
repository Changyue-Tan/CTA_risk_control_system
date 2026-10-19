"""打印验收场景：双源补数、3% 熔断、一笔逐日盯市。"""

from __future__ import annotations

from cta_risk.config import load_config
from cta_risk.engine import RiskSystem, three_percent_price
from cta_risk.market import demonstrate_gap
from cta_risk.models import Offset, OrderRequest, Side


def _yes(flag: bool) -> str:
    return "是" if flag else "否"


def main() -> None:
    print("== 行情缺口回补 ==")
    gap = demonstrate_gap()
    print(f"回补前价格更新 {gap['applied_before_replay']} 次，回补后仍是 {gap['applied']} 次")
    print(f"主源序号 {gap['primary_seqs']}，去重 {gap['duplicates']} 笔，过期回补 {gap['stale']} 笔")

    print("\n== 单日亏损 3% 熔断 ==")
    engine = RiskSystem(load_config())
    instrument, price = three_percent_price(engine.config)
    engine.set_price(instrument, price)
    print(f"{instrument} 打到 {price}。Gamma 当日盯市 {engine.metrics('gamma').daily_mtm}，熔断 {_yes(engine.accounts['gamma'].halted)}")
    opened = engine.submit_order(OrderRequest("gamma", instrument, Side.BUY, Offset.OPEN, price, 1))
    closed = engine.submit_order(OrderRequest("gamma", instrument, Side.SELL, Offset.CLOSE, price, 1))
    print(f"开仓受理 {_yes(opened.accepted)}，原因：{opened.reject_reason}")
    print(f"平仓受理 {_yes(closed.accepted)}")

    print("\n== 逐日盯市 ==")
    sample = RiskSystem(load_config())
    report = sample.settle(sample.last_prices())
    print(f"交易日 {report['trading_day']} 公司权益 {report['firm_equity']}")
    print(f"日报 {report['text_path']}")


if __name__ == "__main__":
    main()
