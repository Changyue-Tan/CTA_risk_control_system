"""打印验收场景：双源补数、3% 熔断、回测拒单、一笔逐日盯市。"""

from __future__ import annotations

from decimal import Decimal

from cta_risk.backtest import builtin_demo
from cta_risk.config import load_config
from cta_risk.engine import RiskSystem
from cta_risk.market import run_gap_demo
from cta_risk.models import Offset, OrderRequest, Side


def main() -> None:
    print("== 行情缺口回补 ==")
    gap = run_gap_demo()
    print(f"检测到缺口序号 {gap['missing'][0]}-{gap['missing'][1]}")
    print(f"回补前价格更新 {gap['applied_before_replay']} 次，回补后仍是 {gap['applied']} 次")
    print(f"主源序号已补齐 {gap['primary_seqs']}，去重 {gap['duplicates']} 笔，未闭合缺口 {gap['open_gaps']}")

    print("\n== 单日亏损 3% 熔断 ==")
    engine = RiskSystem(load_config())
    trace = engine.acceptance_shock("gamma")
    print(f"Gamma 螺纹结算基准 3450，验收价 {trace['target_price']}")
    print(f"当日盯市 {trace['daily_mtm']}，阈值 {trace['threshold']}，熔断 {trace['halted']}")
    print(f"开仓受理 {trace['open_accepted']}，原因：{trace['open_reject_reason']}")
    print(f"平仓受理 {trace['close_accepted']}")

    print("\n== 历史回放 ==")
    replay = builtin_demo()
    print(f"回放后熔断账户 {replay['halted']}，样例开仓受理 {replay['open_accepted']}")

    print("\n== 逐日盯市样例 ==")
    sample = RiskSystem(load_config())
    report = sample.settle(sample.last_prices())
    print("按昨结算价清算，三账户盈亏为零，权益等于初始资金合计")
    print(f"交易日 {report['trading_day']} 公司权益 {report['firm_equity']}")
    print(f"文本日报 {report['text_path']}")
    print(f"HTML 日报 {report['html_path']}")

    print("\n== 样例委托：Gamma 熔断后不能再开仓 ==")
    blocked = engine.submit_order(
        OrderRequest("gamma", "rb2510", Side.BUY, Offset.OPEN, Decimal(trace["target_price"]), 1)
    )
    print(f"再次开仓受理 {blocked.accepted}，原因：{blocked.reject_reason}")


if __name__ == "__main__":
    main()
