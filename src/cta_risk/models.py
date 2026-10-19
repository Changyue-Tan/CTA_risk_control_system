"""领域模型与盯市公式。

读代码前先记住几组期货用语（本项目的简化口径）：

- **合约 / 品种** (`instrument`)：如 rb2701。一手合约对应的人民币金额 = 价格 × **乘数**。
- **开仓 / 平仓** (`Offset`)：开仓新建仓位；平仓减少仓位。卖出平多、买入平空。
- **多头 / 空头** (`PositionSide`)：做多盼涨，做空盼跌。
- **昨结算 / 结算价**：交易所每个交易日结束后公布的官方价格。本系统用它做逐日盯市。
- **盯市盈亏 (mark-to-market)**：相对某个「基准价」算出的浮动或已实现盈亏，见 `mtm_pnl`。
- **盯市基准** (`OpenLot.mtm_basis`)：算当日盈亏时减掉的价。今仓 = 开仓价，昨仓 = 昨结算；日终清算后统一改成今日结算价。
- **权益**：账户净值。盘中 ≈ 上日权益 + 已平仓盯市 + 持仓浮盈 − 手续费。
- **保证金**：按名义本金 × 保证金率占用的资金；可用资金 = 权益 − 保证金。
- **名义敞口 / 名义本金**：价格 × 手数 × 乘数，表示仓位规模（不是盈亏）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum


class Side(str, Enum):
    """委托买卖方向。与持仓方向不同：平多是卖、平空是买。"""

    BUY = "buy"
    SELL = "sell"


class PositionSide(str, Enum):
    """持仓方向。"""

    LONG = "long"
    SHORT = "short"


class Offset(str, Enum):
    """开平标志。期货必须显式声明是开仓还是平仓。"""

    OPEN = "open"
    CLOSE = "close"


class ActionLevel(str, Enum):
    """风控动作级别：告警只记事件；限制拒开仓；熔断拒开仓且需人工恢复。"""

    ALERT = "alert"
    RESTRICT = "restrict"
    HALT = "halt"
    RECOVERY = "recovery"


def money(value: Decimal) -> str:
    """金额统一保留两位小数（四舍五入），供报表和接口输出。"""
    return format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def pct(value: Decimal) -> str:
    """把 0.03 格式化成 '3.00%'。"""
    points = (value * Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(points, "f") + "%"


def direction(side: PositionSide) -> Decimal:
    """多头盈亏符号为 +1，空头为 -1（空头价格下跌时盈利）。"""
    return Decimal(1) if side == PositionSide.LONG else Decimal(-1)


def position_side_to_close(order_side: Side) -> PositionSide:
    """这笔平仓对应的是哪边持仓：卖出平多头，买入平空头（方向相反）。"""
    return PositionSide.LONG if order_side == Side.SELL else PositionSide.SHORT


def mtm_pnl(side: PositionSide, price: Decimal, basis: Decimal, qty: int, multiplier: Decimal) -> Decimal:
    """盯市盈亏 = (现价 − 盯市基准) × 手数 × 乘数 × 方向。

    例：多头 10 手螺纹，乘数 10，基准 3000，现价 3050
    → (3050 − 3000) × 10 × 10 × 1 = 5000 元盈利。

    空头同样价格变动则符号相反：基准 3000、现价 2900、2 手
    → (2900 − 3000) × 2 × 10 × (−1) = 2000 元盈利。

    基准（`basis`）约定：今仓用开仓价，昨仓用上一交易日结算价；
    日终清算后，留仓的基准会被改写成今日结算价（见 `RiskSystem._roll_to_next_day`）。
    """
    return (price - basis) * qty * multiplier * direction(side)


@dataclass(frozen=True)
class InstrumentSpec:
    """合约规格。乘数、保证金率、手续费来自配置，不随行情变。"""

    instrument_id: str
    exchange: str
    # 一手对应的价格单位倍数。螺纹常见为 10（元/吨 × 10 吨）。
    multiplier: Decimal
    # 保证金 = 名义本金 × 保证金率。
    margin_rate: Decimal
    # 每手单向手续费（开仓、平仓各收一次）。
    commission_per_lot: Decimal


@dataclass(frozen=True)
class AccountSpec:
    account_id: str
    name: str
    # 账户初始资金；当日亏损熔断线按它的比例计算。
    initial_equity: Decimal


@dataclass(frozen=True)
class RiskLimits:
    # 当日盯市亏损达到初始资金的这个比例时告警。
    daily_loss_alert_ratio: Decimal
    # 当日盯市亏损达到初始资金的这个比例时熔断。
    daily_loss_halt_ratio: Decimal
    # 最大回撤达到这个比例时告警。
    max_drawdown_alert_ratio: Decimal
    # 最大回撤达到这个比例时限制开仓。
    max_drawdown_restrict_ratio: Decimal
    # 单品种名义敞口占总敞口达到这个比例时告警。
    concentration_alert_ratio: Decimal
    # 单品种名义敞口占总敞口达到这个比例时限制加仓。
    concentration_restrict_ratio: Decimal
    # 单品种名义本金 / 权益 达到这个比例时，禁止继续加该品种。
    max_instrument_notional_ratio: Decimal


@dataclass(frozen=True)
class PriorPosition:
    """开盘前的昨仓。盯市基准就是昨结算价 `prev_settle`。"""

    account_id: str
    instrument: str
    side: PositionSide
    qty: int
    prev_settle: Decimal


@dataclass
class SystemConfig:
    trading_day: date
    instruments: dict[str, InstrumentSpec]
    accounts: list[AccountSpec]
    risk: RiskLimits
    prior_positions: list[PriorPosition]
    # 开盘前最新价占位，通常等于昨结算。
    reference_prices: dict[str, Decimal]


@dataclass(frozen=True)
class Tick:
    """一笔行情。`seq` 是数据源侧序号；`exchange_ts` 用于双源去重。"""

    source: str
    instrument: str
    exchange_ts: datetime
    seq: int
    last: Decimal
    trading_day: date


@dataclass
class OrderRequest:
    account_id: str
    instrument: str
    side: Side
    offset: Offset
    price: Decimal
    qty: int


@dataclass
class OrderResult:
    accepted: bool
    order_id: str | None
    reject_reason: str | None
    fill_price: Decimal | None
    fill_qty: int
    # 仅平仓时有值：这笔平仓相对基准价实现的盯市盈亏。
    realized_pnl: Decimal


@dataclass
class OpenLot:
    """一笔未平仓手数（lot）。平仓按开仓日、lot_id 先开先平（FIFO）。

    `mtm_basis` 是当前的盯市基准价：
    - 昨仓：昨结算价
    - 今仓开出来时：等于开仓价
    - 日终清算后：改成今日结算价（进入下一交易日时又成为「昨结算」）
    """

    lot_id: str
    account_id: str
    instrument: str
    side: PositionSide
    qty: int
    open_price: Decimal
    open_day: date
    mtm_basis: Decimal


@dataclass
class AccountState:
    """账户盘中状态。日终清算后会把当日盈亏归零，权益滚入 `prev_equity`。"""

    spec: AccountSpec
    # 上一交易日结算后的权益（今日盈亏的起点）。
    prev_equity: Decimal
    # 历史权益高水位，用来算最大回撤。
    peak_equity: Decimal
    # 今日已平仓盯市盈亏累计（未扣手续费）。
    realized_pnl: Decimal = Decimal(0)
    # 今日手续费累计。
    commission: Decimal = Decimal(0)
    # 是否处于熔断：为 True 时禁止开仓，平仓仍允许。
    halted: bool = False


@dataclass
class RiskEvent:
    event_id: str
    account_id: str
    rule_id: str
    level: ActionLevel
    message: str
    instrument: str | None = None
