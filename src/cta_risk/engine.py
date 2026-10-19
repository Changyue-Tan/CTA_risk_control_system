from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from itertools import count
from pathlib import Path
from threading import Lock

from cta_risk.config import project_root
from cta_risk.market import MarketHub
from cta_risk.quotes import Quote
from cta_risk.models import (
    AccountState,
    Offset,
    OpenLot,
    OrderRequest,
    OrderResult,
    PositionSide,
    RiskEvent,
    Side,
    SystemConfig,
    Tick,
    money,
    mtm_pnl,
)
from cta_risk.positions import PositionBook
from cta_risk.settlement import write_report

HALT_RULE = "daily_loss_circuit_breaker"
REJECT_HALT = "熔断：账户单日盯市亏损已达初始资金阈值，禁止开仓"
REJECT_EXPOSURE = "单品种名义敞口超限，禁止继续加仓"
REJECT_MARGIN = "可用资金不足，禁止开仓"
REJECT_POSITION = "可平仓位不足"


@dataclass
class Metrics:
    equity: Decimal
    daily_mtm: Decimal
    margin: Decimal
    available: Decimal


class RiskSystem:
    """行情、持仓、熔断和日终清算。所有改动都先拿到同一把锁。"""

    def __init__(self, config: SystemConfig, report_dir: Path | None = None) -> None:
        self.config = config
        self.trading_day = config.trading_day
        self.market = MarketHub()
        self.positions = PositionBook()
        self.accounts = {
            spec.account_id: AccountState(spec, spec.initial_equity) for spec in config.accounts
        }
        self.events: list[RiskEvent] = []
        self.report_dir = report_dir if report_dir is not None else project_root() / "reports"
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._ids = count(1)
        self._scenario_seq: dict[str, int] = {}

        for instrument, price in config.reference_prices.items():
            self.market.seed(instrument, price, self.trading_day)
        for position in config.bootstrap_positions:
            self.positions.bootstrap(position, self.trading_day, self._next("L"))
        self._halt_on_daily_loss()

    def on_tick(self, tick: Tick) -> bool:
        with self._lock:
            changed = self.market.ingest(tick)
            if changed:
                self._halt_on_daily_loss()
            return changed

    def publish_quotes(self, quotes: list[Quote]) -> int:
        """把外部行情逐笔送进合并器。每个源按合约单独编号，序号从 1 连续增加。"""
        changed = 0
        for quote in quotes:
            if quote.instrument not in self.config.instruments:
                continue
            key = f"{quote.source}:{quote.instrument}"
            seq = self._scenario_seq.get(key, 0) + 1
            self._scenario_seq[key] = seq
            tick = Tick(quote.source, quote.instrument, quote.exchange_ts, seq, quote.last, self.trading_day)
            if self.on_tick(tick):
                changed += 1
        return changed

    def set_price(self, instrument: str, price: Decimal) -> None:
        """把某个合约的最新价直接设成给定价格，并重新检查熔断。"""
        current = self.market.last[instrument]
        seq = self._scenario_seq.get(instrument, 0) + 1
        self._scenario_seq[instrument] = seq
        self.on_tick(
            Tick(
                "验收",
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
                return OrderResult(False, None, reason, None, 0, Decimal(0))
            result = self._fill(order)
            self._halt_on_daily_loss()
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
        side = PositionSide.LONG if order.side == Side.SELL else PositionSide.SHORT
        held = self.positions.qty(order.account_id, order.instrument, side)
        if held < order.qty:
            return REJECT_POSITION
        return None

    def _cannot_open(self, order: OrderRequest) -> str | None:
        account = self.accounts[order.account_id]
        if account.halted:
            return REJECT_HALT
        spec = self.config.instruments[order.instrument]
        metrics = self.metrics(order.account_id)
        if metrics.equity <= 0:
            return REJECT_MARGIN
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
                prev_settle=order.price,
            )
        )

    def _halt_on_daily_loss(self) -> None:
        """当日盯市亏损达到初始资金的阈值时熔断。只在第一次越过阈值时记一条事件。"""
        limit = self.config.risk.daily_loss_halt_ratio
        for account_id, account in self.accounts.items():
            metrics = self.metrics(account_id)
            breached = metrics.daily_mtm <= -limit * account.spec.initial_equity
            if breached and not account.halted:
                account.halted = True
                self.events.append(
                    RiskEvent(
                        self._next("E"),
                        account_id,
                        HALT_RULE,
                        f"单日盯市盈亏 {money(metrics.daily_mtm)}，达到初始资金 {limit * 100}% ，触发熔断",
                    )
                )

    def resume(self, account_id: str) -> bool:
        """人工解除熔断。亏损仍超过阈值时会立刻再次熔断。"""
        with self._lock:
            account = self.accounts[account_id]
            account.halted = False
            self._halt_on_daily_loss()
            return account.halted

    def metrics(self, account_id: str) -> Metrics:
        account = self.accounts[account_id]
        floating = self.positions.floating(account_id, self._price_of, self.config.instruments)
        margin = self._margin(account_id)
        equity = account.prev_equity + account.realized_pnl + floating - account.commission
        return Metrics(equity, account.realized_pnl + floating, margin, equity - margin)

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
                    {"event_id": event.event_id, "account_id": event.account_id, "message": event.message}
                    for event in self.events
                ],
            }

    def settle(self, prices: dict[str, Decimal]) -> dict:
        """逐日盯市。结算后，留仓的基准价改为今日结算价。"""
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
                    self.market.mark(instrument, price)
            self.trading_day = day + timedelta(days=1)
            path = self.report_dir / f"risk_report_{day.strftime('%Y%m%d')}.txt"
            write_report(path, day.isoformat(), rows)
            return {
                "trading_day": day.isoformat(),
                "text_path": str(path),
                "firm_equity": money(firm_equity),
                "accounts": rows,
            }

    def _settle_account(self, account_id: str, prices: dict[str, Decimal]) -> tuple[dict, Decimal]:
        """清算一个账户：按结算价盯市，再把留仓基准改成今日结算价。"""
        account = self.accounts[account_id]
        lines, position_pnl, margin = self._settlement_lines(account_id, prices)
        equity = account.prev_equity + account.realized_pnl + position_pnl - account.commission
        row = {
            "account_id": account_id,
            "name": account.spec.name,
            "close_pnl": money(account.realized_pnl),
            "position_pnl": money(position_pnl),
            "commission": money(account.commission),
            "equity": money(equity),
            "margin": money(margin),
            "available": money(equity - margin),
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
            "margin": money(metrics.margin),
            "halted": account.halted,
        }

    def _settlement_lines(self, account_id: str, prices: dict[str, Decimal]) -> tuple[list[dict], Decimal, Decimal]:
        position_pnl = Decimal(0)
        margin = Decimal(0)
        lines = []
        for lot in self.positions.lots_of(account_id):
            spec = self.config.instruments[lot.instrument]
            settle_px = prices[lot.instrument]
            pnl = mtm_pnl(lot.side, settle_px, lot.prev_settle, lot.qty, spec.multiplier)
            gross = settle_px * lot.qty * spec.multiplier
            position_pnl += pnl
            margin += gross * spec.margin_rate
            lines.append(
                {
                    "instrument": lot.instrument,
                    "side": lot.side.value,
                    "qty": lot.qty,
                    "basis": money(lot.prev_settle),
                    "settle": money(settle_px),
                    "pnl": money(pnl),
                    "notional": money(gross),
                }
            )
        return lines, position_pnl, margin

    def _roll_to_next_day(self, account: AccountState, prices: dict[str, Decimal], equity: Decimal) -> None:
        for lot in self.positions.lots_of(account.spec.account_id):
            lot.prev_settle = prices[lot.instrument]
        account.prev_equity = equity
        account.realized_pnl = Decimal(0)
        account.commission = Decimal(0)

    def _margin(self, account_id: str) -> Decimal:
        total = Decimal(0)
        for lot in self.positions.lots_of(account_id):
            spec = self.config.instruments[lot.instrument]
            total += self._price_of(lot.instrument) * lot.qty * spec.multiplier * spec.margin_rate
        return total

    def _price_of(self, instrument: str) -> Decimal:
        return self.market.last[instrument].last

    def _next(self, prefix: str) -> str:
        return f"{prefix}{next(self._ids):06d}"


def three_percent_price(config: SystemConfig, account_id: str = "gamma") -> tuple[str, Decimal]:
    """返回把该账户打到恰好亏掉初始资金 3% 的合约和价格。按配置里的昨仓计算。"""
    position = next(item for item in config.bootstrap_positions if item.account_id == account_id)
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
