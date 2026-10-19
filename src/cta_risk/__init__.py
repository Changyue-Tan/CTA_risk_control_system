"""多品种期货交易风控与清算系统。

阅读顺序建议（不熟悉期货清算时）：

1. `docs/设计说明.md` 里的术语表
2. `models.py` 模块顶部的用语说明与 `mtm_pnl`
3. `positions.py`（开平仓与 FIFO）
4. `engine.py` 的权益公式、`settle` 与 `_roll_to_next_day`
"""

__version__ = "1.0.0"
