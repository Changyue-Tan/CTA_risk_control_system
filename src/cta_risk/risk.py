from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from itertools import count

from cta_risk.models import (
    ActionLevel,
    AccountMetrics,
    Offset,
    OrderRequest,
    PositionSide,
    RiskEvent,
    Side,
    money,
    pct,
)

HALT_DAILY = "daily_loss_circuit_breaker"
ALERT_DAILY = "daily_loss_alert"
ALERT_DRAWDOWN = "max_drawdown_alert"
RESTRICT_DRAWDOWN = "max_drawdown_restrict"
ALERT_EXPOSURE = "instrument_exposure_alert"
RESTRICT_EXPOSURE = "instrument_exposure_restrict"
ALERT_CONCENTRATION = "concentration_alert"
RESTRICT_CONCENTRATION = "concentration_restrict"

REJECT_HALT = "熔断：账户单日盯市亏损已达初始资金阈值，禁止开仓"
REJECT_DRAWDOWN = "最大回撤超限，禁止开仓"
REJECT_EXPOSURE = "单品种保证金占比超限，禁止继续加仓该品种"
REJECT_CONCENTRATION = "品种集中度超限，禁止继续加仓该品种"
REJECT_MARGIN = "可用资金不足，禁止开仓"
REJECT_POSITION = "可平仓位不足"
REJECT_EQUITY = "权益不大于零，禁止开仓"


def event_dict(event: RiskEvent) -> dict:
    return {
        "event_id": event.event_id,
        "ts": event.ts.isoformat(),
        "trading_day": event.trading_day.isoformat(),
        "account_id": event.account_id,
        "rule_id": event.rule_id,
        "level": event.level.value,
        "instrument": event.instrument,
        "message": event.message,
        "value": event.value,
    }


class RiskEngine:
    """阈值只在状态切换时发一次信号。熔断不随盈亏回升自动解除。"""

    def __init__(self) -> None:
        self.active: set[tuple[str, str, str]] = set()
        self.events: list[RiskEvent] = []
        self._ids = count(1)

    def evaluate(self, system) -> list[RiskEvent]:
        fresh: list[RiskEvent] = []
        now = system.now()
        for account_id in system.accounts:
            fresh.extend(self._eval_account(system, account_id, now))
        if len(self.events) > 1000:
            self.events = self.events[-1000:]
        return fresh

    def clear_halt(self, account_id: str) -> None:
        self.active = {key for key in self.active if not (key[0] == account_id and key[1] == HALT_DAILY)}

    def pre_trade(self, system, order: OrderRequest) -> str | None:
        if order.account_id not in system.accounts:
            return "账户不存在"
        spec = system.config.instruments.get(order.instrument)
        if spec is None:
            return "合约不存在"
        if order.qty <= 0:
            return "手数必须大于 0"
        if order.price <= 0:
            return "价格必须大于 0"
        if order.price % spec.tick_size != 0:
            return "价格不符合最小变动价位"
        account = system.accounts[order.account_id]
        if order.offset == Offset.CLOSE:
            side = PositionSide.LONG if order.side == Side.SELL else PositionSide.SHORT
            held = system.positions.position_qty(order.account_id, order.instrument, side)
            if held < order.qty:
                return REJECT_POSITION
            return None

        if account.halted:
            return REJECT_HALT
        metrics = system.metrics(order.account_id)
        limits = system.config.risk
        if metrics.drawdown >= limits.max_drawdown_restrict_ratio:
            return REJECT_DRAWDOWN
        if metrics.equity <= 0:
            return REJECT_EQUITY

        commission = spec.commission_per_lot * order.qty
        equity_after = metrics.equity - commission
        if equity_after <= 0:
            return REJECT_MARGIN
        add_notional = order.price * order.qty * spec.multiplier
        add_margin = add_notional * spec.margin_rate
        inst_margin = metrics.margin_by_instrument.get(order.instrument, Decimal(0)) + add_margin
        if inst_margin / equity_after >= limits.instrument_margin_restrict_ratio:
            return REJECT_EXPOSURE

        notionals = dict(metrics.notional_by_instrument)
        notionals[order.instrument] = notionals.get(order.instrument, Decimal(0)) + add_notional
        total_notional = sum(notionals.values(), Decimal(0))
        names = sum(1 for value in notionals.values() if value > 0)
        share = notionals[order.instrument] / total_notional if total_notional else Decimal(0)
        if names >= 2 and share >= limits.concentration_restrict_ratio:
            return REJECT_CONCENTRATION

        margin_after = metrics.margin + add_margin
        if equity_after - margin_after < 0:
            return REJECT_MARGIN
        return None

    def _eval_account(self, system, account_id: str, now: datetime) -> list[RiskEvent]:
        metrics = system.metrics(account_id)
        account = system.accounts[account_id]
        limits = system.config.risk
        fresh: list[RiskEvent] = []
        threshold = -limits.daily_loss_circuit_breaker_ratio * account.spec.initial_equity
        self._gate(
            system,
            fresh,
            now,
            account_id,
            HALT_DAILY,
            "*",
            metrics.daily_mtm <= threshold,
            ActionLevel.HALT,
            False,
            (
                f"单日盯市盈亏 {money(metrics.daily_mtm)}（{pct(metrics.daily_mtm_ratio)}），"
                f"达到初始资金 {pct(limits.daily_loss_circuit_breaker_ratio)}，触发熔断"
            ),
            money(metrics.daily_mtm),
            on_enter=lambda: self._halt(account, fresh),
        )
        alert_line = -limits.daily_loss_alert_ratio * account.spec.initial_equity
        self._gate(
            system,
            fresh,
            now,
            account_id,
            ALERT_DAILY,
            "*",
            metrics.daily_mtm <= alert_line,
            ActionLevel.ALERT,
            True,
            f"单日盯市盈亏 {money(metrics.daily_mtm)}，达到初始资金 {pct(limits.daily_loss_alert_ratio)} 预警线",
            money(metrics.daily_mtm),
        )
        self._gate(
            system,
            fresh,
            now,
            account_id,
            ALERT_DRAWDOWN,
            "*",
            metrics.drawdown >= limits.max_drawdown_alert_ratio,
            ActionLevel.ALERT,
            True,
            f"最大回撤 {pct(metrics.drawdown)}，达到预警线 {pct(limits.max_drawdown_alert_ratio)}",
            pct(metrics.drawdown),
        )
        self._gate(
            system,
            fresh,
            now,
            account_id,
            RESTRICT_DRAWDOWN,
            "*",
            metrics.drawdown >= limits.max_drawdown_restrict_ratio,
            ActionLevel.RESTRICT,
            True,
            f"最大回撤 {pct(metrics.drawdown)}，禁止继续开仓",
            pct(metrics.drawdown),
        )
        for instrument, inst_margin in metrics.margin_by_instrument.items():
            ratio = inst_margin / metrics.equity if metrics.equity > 0 else Decimal(1)
            self._gate(
                system,
                fresh,
                now,
                account_id,
                ALERT_EXPOSURE,
                instrument,
                ratio >= limits.instrument_margin_alert_ratio,
                ActionLevel.ALERT,
                True,
                f"{instrument} 保证金占权益 {pct(ratio)}，达到单品种预警线",
                pct(ratio),
            )
            self._gate(
                system,
                fresh,
                now,
                account_id,
                RESTRICT_EXPOSURE,
                instrument,
                ratio >= limits.instrument_margin_restrict_ratio,
                ActionLevel.RESTRICT,
                True,
                f"{instrument} 保证金占权益 {pct(ratio)}，禁止继续加仓",
                pct(ratio),
            )
        if metrics.instrument_count >= 2:
            top_name, top_share = self._top_share(metrics)
            self._gate(
                system,
                fresh,
                now,
                account_id,
                ALERT_CONCENTRATION,
                top_name,
                top_share >= limits.concentration_alert_ratio,
                ActionLevel.ALERT,
                True,
                f"{top_name} 名义敞口占比 {pct(top_share)}，达到集中度预警线",
                pct(top_share),
            )
            self._gate(
                system,
                fresh,
                now,
                account_id,
                RESTRICT_CONCENTRATION,
                top_name,
                top_share >= limits.concentration_restrict_ratio,
                ActionLevel.RESTRICT,
                True,
                f"{top_name} 名义敞口占比 {pct(top_share)}，禁止继续加仓该品种",
                pct(top_share),
            )
        return fresh

    def _halt(self, account, _fresh: list[RiskEvent]) -> None:
        account.halted = True
        account.halt_reason = REJECT_HALT

    def _top_share(self, metrics: AccountMetrics) -> tuple[str, Decimal]:
        total = sum(metrics.notional_by_instrument.values(), Decimal(0))
        if total == 0:
            return "*", Decimal(0)
        name = max(metrics.notional_by_instrument, key=metrics.notional_by_instrument.get)
        return name, metrics.notional_by_instrument[name] / total

    def _gate(
        self,
        system,
        fresh: list[RiskEvent],
        now: datetime,
        account_id: str,
        rule_id: str,
        instrument: str,
        breached: bool,
        level: ActionLevel,
        auto_clear: bool,
        message: str,
        value: str,
        on_enter=None,
    ) -> None:
        key = (account_id, rule_id, instrument)
        if breached and key not in self.active:
            self.active.add(key)
            if on_enter:
                on_enter()
            fresh.append(self._emit(system, now, account_id, rule_id, level, instrument, message, value))
        elif not breached and key in self.active and auto_clear:
            self.active.remove(key)
            fresh.append(
                self._emit(
                    system,
                    now,
                    account_id,
                    rule_id,
                    ActionLevel.RECOVERY,
                    None if instrument == "*" else instrument,
                    f"{rule_id} 恢复",
                    value,
                )
            )

    def _emit(self, system, now, account_id, rule_id, level, instrument, message, value) -> RiskEvent:
        event = RiskEvent(
            event_id=f"E{next(self._ids):06d}",
            ts=now,
            trading_day=system.trading_day,
            account_id=account_id,
            rule_id=rule_id,
            level=level,
            instrument=None if instrument in (None, "*") else instrument,
            message=message,
            value=value,
        )
        self.events.append(event)
        return event
