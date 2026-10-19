"""风控与清算引擎。

数据流：行情 → `MarketHub` 合成最新价 → 持仓浮盈变化 → `_evaluate_risk`；
委托 → `_cannot_open` / `_cannot_close` → `_fill` → 再评估风险；
收盘 → `settle` 逐日盯市并写出日报。

盘中权益（账户净值）怎么算：

    权益 = 上日权益 + 今日已平仓盯市 + 持仓浮盈 − 手续费
    当日盯市 = 今日已平仓盯市 + 持仓浮盈          # 熔断口径，不含手续费
    可用资金 = 权益 − 保证金
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from itertools import count
from pathlib import Path
from threading import Lock
from typing import Callable

from cta_risk.config import project_root
from cta_risk.market import MarketHub
from cta_risk.quotes import Quote
from cta_risk.models import (
    ActionLevel,
    AccountState,
    Offset,
    OpenLot,
    OrderRequest,
    OrderResult,
    PositionSide,
    RiskEvent,
    RiskLimits,
    Side,
    SystemConfig,
    Tick,
    money,
    mtm_pnl,
    pct,
    position_side_to_close,
)
from cta_risk.positions import PositionBook
from cta_risk.settlement import write_report

logger = logging.getLogger("cta_risk.engine")

HALT_RULE = "daily_loss_circuit_breaker"
ALERT_DAILY = "daily_loss_alert"
ALERT_DRAWDOWN = "max_drawdown_alert"
RESTRICT_DRAWDOWN = "max_drawdown_restrict"
ALERT_CONCENTRATION = "concentration_alert"
RESTRICT_CONCENTRATION = "concentration_restrict"

REJECT_HALT = "熔断：账户单日盯市亏损已达初始资金阈值，禁止开仓"
REJECT_DRAWDOWN = "最大回撤超限，禁止开仓"
REJECT_CONCENTRATION = "单品种集中度超限，禁止继续加仓"
REJECT_EXPOSURE = "单品种名义敞口超限，禁止继续加仓"
REJECT_MARGIN = "可用资金不足，禁止开仓"
REJECT_POSITION = "可平仓位不足"

# set_price 模拟行情时用的数据源名（熔断验收按钮、演示脚本走这条路）。
ACCEPTANCE_SOURCE = "验收"


@dataclass
class Metrics:
    """账户盘中指标快照。"""

    # 账户净值。
    equity: Decimal
    # 当日盯市盈亏（已平仓 + 浮盈，不含手续费）。熔断按它相对初始资金判断。
    daily_mtm: Decimal
    # 持仓占用的保证金。
    margin: Decimal
    # 还能用来开新仓的资金 = equity − margin。
    available: Decimal
    # 相对历史权益高水位的回撤比例，0.10 表示回撤了 10%。
    drawdown: Decimal


@dataclass(frozen=True)
class RiskRule:
    """一条账户级风控规则。`_evaluate_risk` 逐账户 × 逐规则调用 `_gate`。

    - `breached(metrics, limits, account)`：当前是否越过阈值
    - `message(metrics, limits, account)`：越线时记入事件的中文说明
    - `halt=True` 表示熔断：越线时置 `halted`，且不自动恢复（其余规则回落后发恢复事件）
    """

    rule_id: str
    level: ActionLevel
    halt: bool
    breached: Callable[[Metrics, RiskLimits, AccountState], bool]
    message: Callable[[Metrics, RiskLimits, AccountState], str]


# 账户级规则全集。集中度规则涉及“哪个品种”，单独在 _evaluate_risk 里处理。
ACCOUNT_RULES: tuple[RiskRule, ...] = (
    RiskRule(
        ALERT_DAILY,
        ActionLevel.ALERT,
        halt=False,
        breached=lambda m, lim, acc: m.daily_mtm <= -lim.daily_loss_alert_ratio * acc.spec.initial_equity,
        message=lambda m, lim, acc: (
            f"单日盯市盈亏 {money(m.daily_mtm)}，达到初始资金 {pct(lim.daily_loss_alert_ratio)} 预警线"
        ),
    ),
    RiskRule(
        HALT_RULE,
        ActionLevel.HALT,
        halt=True,
        breached=lambda m, lim, acc: m.daily_mtm <= -lim.daily_loss_halt_ratio * acc.spec.initial_equity,
        message=lambda m, lim, acc: (
            f"单日盯市盈亏 {money(m.daily_mtm)}，达到初始资金 {pct(lim.daily_loss_halt_ratio)}，触发熔断"
        ),
    ),
    RiskRule(
        ALERT_DRAWDOWN,
        ActionLevel.ALERT,
        halt=False,
        breached=lambda m, lim, acc: m.drawdown >= lim.max_drawdown_alert_ratio,
        message=lambda m, lim, acc: (
            f"最大回撤 {pct(m.drawdown)}，达到预警线 {pct(lim.max_drawdown_alert_ratio)}"
        ),
    ),
    RiskRule(
        RESTRICT_DRAWDOWN,
        ActionLevel.RESTRICT,
        halt=False,
        breached=lambda m, lim, acc: m.drawdown >= lim.max_drawdown_restrict_ratio,
        message=lambda m, lim, acc: f"最大回撤 {pct(m.drawdown)}，禁止继续开仓",
    ),
)


class RiskSystem:
    """行情接入、持仓成交、三级风控与日终清算。所有状态改动都先拿同一把锁。"""

    def __init__(self, config: SystemConfig, report_dir: Path | None = None) -> None:
        self.config = config
        self.trading_day = config.trading_day
        self.market = MarketHub()
        self.positions = PositionBook()
        self.accounts = {
            spec.account_id: AccountState(spec, spec.initial_equity, peak_equity=spec.initial_equity)
            for spec in config.accounts
        }
        self.events: list[RiskEvent] = []
        self.report_dir = report_dir if report_dir is not None else project_root() / "reports"
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._ids = count(1)
        self._quote_seq: dict[tuple[str, str], int] = {}
        self._active: set[tuple[str, str, str]] = set()

        for instrument, price in config.reference_prices.items():
            self.market.seed_reference_price(instrument, price, self.trading_day)
        for position in config.prior_positions:
            self.positions.load_prior_positions(position, self.trading_day, self._next("L"))
        self._evaluate_risk()

    def on_tick(self, tick: Tick) -> bool:
        with self._lock:
            changed = self.market.ingest(tick)
            if changed:
                self._evaluate_risk()
            return changed

    def publish_quotes(self, quotes: list[Quote]) -> int:
        """把外部行情逐笔送进合并器。每个源按合约单独编号，序号从 1 连续增加。"""
        changed = 0
        for quote in quotes:
            if quote.instrument not in self.config.instruments:
                continue
            key = (quote.source, quote.instrument)
            seq = self._quote_seq.get(key, 0) + 1
            self._quote_seq[key] = seq
            tick = Tick(quote.source, quote.instrument, quote.exchange_ts, seq, quote.last, self.trading_day)
            if self.on_tick(tick):
                changed += 1
        return changed

    def set_price(self, instrument: str, price: Decimal) -> None:
        """把某个合约的最新价直接设成给定价格，并重新检查熔断。"""
        current = self.market.last[instrument]
        seq = self._quote_seq.get((ACCEPTANCE_SOURCE, instrument), 0) + 1
        self._quote_seq[(ACCEPTANCE_SOURCE, instrument)] = seq
        self.on_tick(
            Tick(
                ACCEPTANCE_SOURCE,
                instrument,
                current.exchange_ts + timedelta(seconds=1),
                seq,
                price,
                self.trading_day,
            )
        )

    def submit_order(self, order: OrderRequest) -> OrderResult:
        """能不能下单，能则成交，成交后再看要不要熔断。"""
        with self._lock:
            reason = self._cannot_submit(order)
            if reason:
                logger.info(
                    "拒单 %s %s %s: %s",
                    order.account_id,
                    order.instrument,
                    order.offset.value,
                    reason,
                )
                return OrderResult(False, None, reason, None, 0, Decimal(0))
            result = self._fill(order)
            self._evaluate_risk()
            return result

    def _cannot_submit(self, order: OrderRequest) -> str | None:
        """能下单时返回 None。平仓不看熔断和资金。"""
        if order.account_id not in self.accounts or order.instrument not in self.config.instruments:
            return "账户或合约不存在"
        if order.qty <= 0 or order.price <= 0:
            return "价格和手数必须大于 0"
        if order.offset == Offset.CLOSE:
            return self._cannot_close(order)
        return self._cannot_open(order)

    def _cannot_close(self, order: OrderRequest) -> str | None:
        side = position_side_to_close(order.side)
        held = self.positions.qty(order.account_id, order.instrument, side)
        if held < order.qty:
            return REJECT_POSITION
        return None

    def _cannot_open(self, order: OrderRequest) -> str | None:
        """开仓前置检查。顺序：熔断 → 回撤 → 集中度 → 单品种名义敞口 → 可用资金。"""
        account = self.accounts[order.account_id]
        if account.halted:
            return REJECT_HALT
        spec = self.config.instruments[order.instrument]
        metrics = self.metrics(order.account_id)
        if metrics.equity <= 0:
            return REJECT_MARGIN
        if metrics.drawdown >= self.config.risk.max_drawdown_restrict_ratio:
            return REJECT_DRAWDOWN
        # 集中度：只拦「已经是最大敞口」的那个品种继续加仓；至少持有两个品种才评估。
        notionals = self._notionals(order.account_id)
        if len(notionals) >= 2:
            top, share = self._top_share(notionals)
            if top == order.instrument and share >= self.config.risk.concentration_restrict_ratio:
                return REJECT_CONCENTRATION
        order_notional = order.price * order.qty * spec.multiplier
        held_notional = self.positions.notional(
            order.account_id, order.instrument, self._price_of, self.config.instruments
        )
        if (held_notional + order_notional) / metrics.equity >= self.config.risk.max_instrument_notional_ratio:
            return REJECT_EXPOSURE
        extra_margin = order_notional * spec.margin_rate
        commission = spec.commission_per_lot * order.qty
        if metrics.available - commission - extra_margin < 0:
            return REJECT_MARGIN
        return None

    def _fill(self, order: OrderRequest) -> OrderResult:
        spec = self.config.instruments[order.instrument]
        account = self.accounts[order.account_id]
        account.commission += spec.commission_per_lot * order.qty
        if order.offset == Offset.OPEN:
            realized = Decimal(0)
            self._open_lot(order)
        else:
            realized = self.positions.close(
                order.account_id, order.instrument, order.side, order.qty, order.price, spec.multiplier
            )
            account.realized_pnl += realized
        self.positions.trades.append(
            {
                "account_id": order.account_id,
                "instrument": order.instrument,
                "side": order.side.value,
                "offset": order.offset.value,
                "qty": order.qty,
                "price": money(order.price),
            }
        )
        return OrderResult(True, self._next("O"), None, order.price, order.qty, realized)

    def _open_lot(self, order: OrderRequest) -> None:
        """今仓：盯市基准先等于开仓价；日终清算后会被改写成今日结算价。"""
        side = PositionSide.LONG if order.side == Side.BUY else PositionSide.SHORT
        self.positions.lots.append(
            OpenLot(
                lot_id=self._next("L"),
                account_id=order.account_id,
                instrument=order.instrument,
                side=side,
                qty=order.qty,
                open_price=order.price,
                open_day=self.trading_day,
                mtm_basis=order.price,
            )
        )

    def _evaluate_risk(self) -> None:
        """逐账户评估告警、限制与熔断。熔断不自动恢复，其余规则回落后发恢复事件。"""
        limits = self.config.risk
        for account_id, account in self.accounts.items():
            metrics = self.metrics(account_id)
            # 账户级规则：当日亏损、回撤。数据来自 ACCOUNT_RULES 表。
            for rule in ACCOUNT_RULES:
                self._gate(
                    account_id,
                    rule.rule_id,
                    "*",
                    breached=rule.breached(metrics, limits, account),
                    level=rule.level,
                    auto_clear=not rule.halt,
                    message=rule.message(metrics, limits, account),
                    on_enter=lambda acc=account: setattr(acc, "halted", True) if rule.halt else None,
                )
            # 集中度规则：和「哪个品种」绑定，单独处理。
            self._evaluate_concentration(account_id, metrics, limits)

    def _evaluate_concentration(self, account_id: str, metrics: Metrics, limits: RiskLimits) -> None:
        """单品种名义敞口占比。至少持有两个品种才评估。"""
        notionals = self._notionals(account_id)
        if len(notionals) < 2:
            return
        top, share = self._top_share(notionals)
        if top is None:
            return
        self._gate(
            account_id,
            ALERT_CONCENTRATION,
            top,
            breached=share >= limits.concentration_alert_ratio,
            level=ActionLevel.ALERT,
            auto_clear=True,
            message=f"{top} 名义敞口占比 {pct(share)}，达到集中度预警线",
        )
        self._gate(
            account_id,
            RESTRICT_CONCENTRATION,
            top,
            breached=share >= limits.concentration_restrict_ratio,
            level=ActionLevel.RESTRICT,
            auto_clear=True,
            message=f"{top} 名义敞口占比 {pct(share)}，禁止继续加仓该品种",
        )

    def _gate(
        self,
        account_id: str,
        rule_id: str,
        instrument: str,
        *,
        breached: bool,
        level: ActionLevel,
        auto_clear: bool,
        message: str,
        on_enter: Callable[[], None] | None = None,
    ) -> None:
        """按 (账户, 规则, 合约) 去重。越过阈值记一条事件；`auto_clear` 为真时回落记恢复。"""
        key = (account_id, rule_id, instrument)
        if breached and key not in self._active:
            self._active.add(key)
            if on_enter:
                on_enter()
            event = self._emit(account_id, rule_id, level, instrument, message)
            self._log_event(event)
        elif not breached and key in self._active and auto_clear:
            self._active.remove(key)
            event = self._emit(
                account_id,
                rule_id,
                ActionLevel.RECOVERY,
                instrument,
                f"{rule_id} 恢复",
            )
            self._log_event(event)

    def _emit(
        self,
        account_id: str,
        rule_id: str,
        level: ActionLevel,
        instrument: str,
        message: str,
    ) -> RiskEvent:
        event = RiskEvent(
            self._next("E"),
            account_id,
            rule_id,
            level,
            message,
            None if instrument == "*" else instrument,
        )
        self.events.append(event)
        return event

    def _log_event(self, event: RiskEvent) -> None:
        if event.level == ActionLevel.HALT:
            logger.error("熔断 %s %s", event.account_id, event.message)
        elif event.level == ActionLevel.RESTRICT:
            logger.warning("限制 %s %s", event.account_id, event.message)
        elif event.level == ActionLevel.ALERT:
            logger.warning("告警 %s %s", event.account_id, event.message)
        else:
            logger.info("恢复 %s %s", event.account_id, event.message)

    def resume(self, account_id: str) -> bool:
        """人工解除熔断。亏损仍超过阈值时会立刻再次熔断。"""
        with self._lock:
            account = self.accounts[account_id]
            account.halted = False
            self._active.discard((account_id, HALT_RULE, "*"))
            self._evaluate_risk()
            return account.halted

    def metrics(self, account_id: str) -> Metrics:
        """按最新价重算账户净值、当日盯市、保证金、回撤。"""
        account = self.accounts[account_id]
        floating = self.positions.floating(account_id, self._price_of, self.config.instruments)
        margin = self._margin(account_id)
        equity = account.prev_equity + account.realized_pnl + floating - account.commission
        if equity > account.peak_equity:
            account.peak_equity = equity
        peak = account.peak_equity
        drawdown = (peak - equity) / peak if peak > 0 else Decimal(0)
        daily_mtm = account.realized_pnl + floating
        return Metrics(equity, daily_mtm, margin, equity - margin, drawdown)

    def query_positions(self, account_id: str | None = None, instrument: str | None = None) -> list[dict]:
        with self._lock:
            return self.positions.rows(account_id, instrument, self._price_of, self.config.instruments)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "trading_day": self.trading_day.isoformat(),
                "accounts": [self._account_row(account_id) for account_id in sorted(self.accounts)],
                "quotes": [
                    {"instrument": name, "last": format(tick.last, "f"), "source": tick.source}
                    for name, tick in sorted(self.market.last.items())
                ],
                "positions": self.positions.rows(None, None, self._price_of, self.config.instruments),
                "events": [
                    {
                        "event_id": event.event_id,
                        "account_id": event.account_id,
                        "level": event.level.value,
                        "message": event.message,
                    }
                    for event in self.events
                ],
            }

    def settle(self, prices: dict[str, Decimal]) -> dict:
        """逐日盯市（mark-to-market）清算。

        对每个账户：用今日结算价给未平仓计持仓盯市，加上今日已平仓盯市，
        扣手续费得到结算权益；然后把留仓基准改成今日结算价，交易日 +1，
        当日盈亏计数器清零——下一交易日的浮盈从新的「昨结算」起算。
        """
        with self._lock:
            missing = sorted({lot.instrument for lot in self.positions.lots} - set(prices))
            if missing:
                raise ValueError(f"缺少结算价: {missing}")
            day = self.trading_day
            rows = []
            firm_equity = Decimal(0)
            for account_id in sorted(self.accounts):
                row, equity = self._settle_account(account_id, prices)
                rows.append(row)
                firm_equity += equity
            for instrument, price in prices.items():
                if instrument in self.market.last:
                    self.market.set_settlement_price(instrument, price)
            self.trading_day = day + timedelta(days=1)
            path = self.report_dir / f"risk_report_{day.strftime('%Y%m%d')}.txt"
            write_report(path, day.isoformat(), rows)
            logger.info("完成 %s 清算，公司权益 %s", day.isoformat(), money(firm_equity))
            return {
                "trading_day": day.isoformat(),
                "text_path": str(path),
                "firm_equity": money(firm_equity),
                "accounts": rows,
            }

    def _settle_account(self, account_id: str, prices: dict[str, Decimal]) -> tuple[dict, Decimal]:
        """清算一个账户并滚入下一交易日。

        结算权益 = 上日权益 + 平仓盯市 + 持仓盯市 − 手续费
        """
        account = self.accounts[account_id]
        lines, position_pnl, margin = self._settlement_lines(account_id, prices)
        equity = account.prev_equity + account.realized_pnl + position_pnl - account.commission
        daily_pnl = account.realized_pnl + position_pnl - account.commission
        initial = account.spec.initial_equity
        row = {
            "account_id": account_id,
            "name": account.spec.name,
            "close_pnl": money(account.realized_pnl),
            "position_pnl": money(position_pnl),
            "commission": money(account.commission),
            "equity": money(equity),
            "margin": money(margin),
            "available": money(equity - margin),
            # 风险度 = 保证金 / 权益；当日盈亏比 = 当日净盈亏 / 初始资金。
            "risk_ratio": format(margin / equity, "f") if equity > 0 else "0",
            "daily_pnl_ratio": format(daily_pnl / initial, "f") if initial else "0",
            "halted": account.halted,
            "positions": lines,
        }
        self._roll_to_next_day(account, prices, equity)
        return row, equity

    def last_prices(self) -> dict[str, Decimal]:
        return {name: tick.last for name, tick in self.market.last.items()}

    def _account_row(self, account_id: str) -> dict:
        metrics = self.metrics(account_id)
        account = self.accounts[account_id]
        initial = account.spec.initial_equity
        return {
            "account_id": account_id,
            "name": account.spec.name,
            "equity": money(metrics.equity),
            "daily_mtm": money(metrics.daily_mtm),
            "daily_mtm_ratio": format(metrics.daily_mtm / initial, "f") if initial else "0",
            "drawdown": format(metrics.drawdown, "f"),
            "margin": money(metrics.margin),
            "halted": account.halted,
        }

    def _settlement_lines(self, account_id: str, prices: dict[str, Decimal]) -> tuple[list[dict], Decimal, Decimal]:
        """按结算价给每笔留仓算持仓盯市和保证金，并生成日报持仓明细。"""
        position_pnl = Decimal(0)
        margin = Decimal(0)
        lines = []
        for lot in self.positions.lots_of(account_id):
            spec = self.config.instruments[lot.instrument]
            settle_px = prices[lot.instrument]
            # 相对 lot 当前盯市基准（昨结算或今开仓价）盯到今日结算价。
            pnl = mtm_pnl(lot.side, settle_px, lot.mtm_basis, lot.qty, spec.multiplier)
            gross = settle_px * lot.qty * spec.multiplier
            position_pnl += pnl
            margin += gross * spec.margin_rate
            lines.append(
                {
                    "instrument": lot.instrument,
                    "side": lot.side.value,
                    "qty": lot.qty,
                    "basis": money(lot.mtm_basis),
                    "settle": money(settle_px),
                    "pnl": money(pnl),
                    "notional": money(gross),
                }
            )
        return lines, position_pnl, margin

    def _roll_to_next_day(self, account: AccountState, prices: dict[str, Decimal], equity: Decimal) -> None:
        """日终滚存：留仓基准 ← 今日结算价；当日盈亏/手续费清零；权益成为明日的上日权益。"""
        for lot in self.positions.lots_of(account.spec.account_id):
            lot.mtm_basis = prices[lot.instrument]
        account.prev_equity = equity
        if equity > account.peak_equity:
            account.peak_equity = equity
        account.realized_pnl = Decimal(0)
        account.commission = Decimal(0)

    def _margin(self, account_id: str) -> Decimal:
        """保证金 = Σ 最新价 × 手数 × 乘数 × 保证金率。"""
        total = Decimal(0)
        for lot in self.positions.lots_of(account_id):
            spec = self.config.instruments[lot.instrument]
            total += self._price_of(lot.instrument) * lot.qty * spec.multiplier * spec.margin_rate
        return total

    def _notionals(self, account_id: str) -> dict[str, Decimal]:
        """按合约汇总名义本金，供集中度计算。"""
        result: dict[str, Decimal] = {}
        for lot in self.positions.lots_of(account_id):
            spec = self.config.instruments[lot.instrument]
            gross = self._price_of(lot.instrument) * lot.qty * spec.multiplier
            result[lot.instrument] = result.get(lot.instrument, Decimal(0)) + gross
        return result

    def _top_share(self, notionals: dict[str, Decimal]) -> tuple[str | None, Decimal]:
        """返回名义本金最大的合约及其占总敞口的比例。"""
        total = sum(notionals.values(), Decimal(0))
        if total <= 0 or not notionals:
            return None, Decimal(0)
        top = max(notionals, key=notionals.get)
        return top, notionals[top] / total

    def _price_of(self, instrument: str) -> Decimal:
        return self.market.last[instrument].last

    def _next(self, prefix: str) -> str:
        return f"{prefix}{next(self._ids):06d}"


def halt_price_at_loss_limit(config: SystemConfig, account_id: str = "gamma") -> tuple[str, Decimal]:
    """返回把该账户打到恰好触熔断线的合约和价格。按配置里的昨仓反推。

    默认熔断线是初始资金的 3%：20 手螺纹（乘数 10）要亏 3 万，
    价格需相对昨结算反向移动 150 点。
    """
    position = next(item for item in config.prior_positions if item.account_id == account_id)
    spec = config.instruments[position.instrument]
    account = next(item for item in config.accounts if item.account_id == account_id)
    loss = config.risk.daily_loss_halt_ratio * account.initial_equity
    move = loss / (position.qty * spec.multiplier)
    if position.side == PositionSide.LONG:
        return position.instrument, position.prev_settle - move
    return position.instrument, position.prev_settle + move


def order_result_dict(result: OrderResult) -> dict:
    return {
        "accepted": result.accepted,
        "order_id": result.order_id,
        "reject_reason": result.reject_reason,
        "fill_price": None if result.fill_price is None else format(result.fill_price, "f"),
        "fill_qty": result.fill_qty,
        "realized_pnl": money(result.realized_pnl),
    }
