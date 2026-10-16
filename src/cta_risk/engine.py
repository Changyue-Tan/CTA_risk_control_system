from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from itertools import count
from pathlib import Path
from threading import RLock

from cta_risk.config import project_root
from cta_risk.models import (
    AccountMetrics,
    AccountState,
    ActionLevel,
    Offset,
    OpenLot,
    OrderRequest,
    OrderResult,
    PositionSide,
    PositionView,
    RejectRecord,
    Side,
    SystemConfig,
    Tick,
    Trade,
    money,
    mtm_pnl,
    ratio_text,
)
from cta_risk.market import MarketHub
from cta_risk.positions import PositionBook
from cta_risk.risk import (
    ALERT_CONCENTRATION,
    ALERT_DAILY,
    ALERT_DRAWDOWN,
    ALERT_EXPOSURE,
    RESTRICT_CONCENTRATION,
    RESTRICT_DRAWDOWN,
    RESTRICT_EXPOSURE,
    RiskEngine,
    event_dict,
)
from cta_risk.settlement import render_daily_report

logger = logging.getLogger("cta_risk.engine")

_RESTRICT = {RESTRICT_DRAWDOWN, RESTRICT_EXPOSURE, RESTRICT_CONCENTRATION}
_ALERT = {ALERT_DAILY, ALERT_DRAWDOWN, ALERT_EXPOSURE, ALERT_CONCENTRATION}


class RiskSystem:
    """行情、持仓、盘中风控、盘后清算的同一把锁串起来。"""

    def __init__(self, config: SystemConfig, report_dir: Path | None = None) -> None:
        self.config = config
        self.trading_day = config.trading_day
        self.market = MarketHub()
        self.positions = PositionBook()
        self.accounts: dict[str, AccountState] = {}
        self.risk = RiskEngine()
        self.rejects: list[RejectRecord] = []
        self.feed = None
        self.report_dir = report_dir if report_dir is not None else project_root() / "reports"
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.last_report: dict | None = None
        self._lock = RLock()
        self._order_ids = count(1)
        self._lot_ids = count(1)
        self._trade_ids = count(1)
        self._scenario_seq: dict[str, int] = {}

        for spec in config.accounts:
            self.accounts[spec.account_id] = AccountState(
                spec=spec,
                prev_equity=spec.initial_equity,
                peak_equity=spec.initial_equity,
            )
        for instrument, price in config.reference_prices.items():
            if instrument not in config.instruments:
                raise ValueError(f"参考价对应的合约不存在: {instrument}")
            self.market.seed(instrument, price, self.trading_day)
        for position in config.bootstrap_positions:
            if position.account_id not in self.accounts:
                raise ValueError(f"昨仓账户不存在: {position.account_id}")
            if position.instrument not in config.instruments:
                raise ValueError(f"昨仓合约不存在: {position.instrument}")
            self.positions.bootstrap(
                position,
                self.trading_day,
                f"L{next(self._lot_ids):06d}",
            )
        self.risk.evaluate(self)

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def bind_feed(self, feed) -> None:
        self.feed = feed

    def on_tick(self, tick: Tick):
        with self._lock:
            result = self.market.ingest(tick)
            events = self._emit_risk() if result.applied else []
            return result, events

    def apply_feed_actions(self, actions) -> None:
        with self._lock:
            for kind, payload in actions:
                if kind == "offline":
                    self.market.set_online(payload, False)
                elif kind == "online":
                    self.market.set_online(payload, True)
                elif kind == "tick":
                    result = self.market.ingest(payload)
                    if result.applied:
                        self._emit_risk()

    def submit_order(self, order: OrderRequest) -> OrderResult:
        with self._lock:
            return self._submit(order)

    def resume(self, account_id: str) -> dict:
        with self._lock:
            account = self.accounts.get(account_id)
            if account is None:
                raise ValueError(f"账户不存在: {account_id}")
            account.halted = False
            account.halt_reason = ""
            self.risk.clear_halt(account_id)
            events = self._emit_risk()
            return {
                "account_id": account_id,
                "halted": account.halted,
                "events": [event_dict(event) for event in events],
            }

    def query_positions(self, account_id: str | None = None, instrument: str | None = None) -> list[dict]:
        with self._lock:
            return [
                _position_dict(row)
                for row in self.positions.views(
                    account_id,
                    instrument,
                    self._price_of,
                    self.config.instruments,
                )
            ]

    def snapshot(self) -> dict:
        with self._lock:
            views = self.positions.views(None, None, self._price_of, self.config.instruments)
            accounts = []
            firm_equity = Decimal(0)
            firm_daily = Decimal(0)
            firm_margin = Decimal(0)
            for account_id in sorted(self.accounts):
                metrics = self.metrics(account_id)
                firm_equity += metrics.equity
                firm_daily += metrics.daily_mtm
                firm_margin += metrics.margin
                account = self.accounts[account_id]
                accounts.append(
                    {
                        "account_id": account_id,
                        "name": account.spec.name,
                        "initial_equity": money(account.spec.initial_equity),
                        "equity": money(metrics.equity),
                        "available": money(metrics.available),
                        "daily_mtm": money(metrics.daily_mtm),
                        "daily_mtm_ratio": ratio_text(metrics.daily_mtm_ratio),
                        "drawdown": ratio_text(metrics.drawdown),
                        "margin": money(metrics.margin),
                        "margin_ratio": ratio_text(metrics.margin_ratio),
                        "concentration": ratio_text(metrics.concentration),
                        "status": self.status_of(account_id),
                        "halted": account.halted,
                        "halt_reason": account.halt_reason,
                    }
                )
            board: dict[str, dict] = {}
            for view in views:
                row = board.setdefault(
                    view.instrument,
                    {
                        "instrument": view.instrument,
                        "exchange": view.exchange,
                        "long_qty": 0,
                        "short_qty": 0,
                        "floating_pnl": Decimal(0),
                        "gross_notional": Decimal(0),
                        "net_notional": Decimal(0),
                    },
                )
                signed = view.notional if view.side == PositionSide.LONG else -view.notional
                if view.side == PositionSide.LONG:
                    row["long_qty"] += view.qty
                else:
                    row["short_qty"] += view.qty
                row["floating_pnl"] += view.floating_pnl
                row["gross_notional"] += view.notional
                row["net_notional"] += signed
            quotes = []
            for instrument, tick in sorted(self.market.latest.items()):
                quotes.append(
                    {
                        "instrument": instrument,
                        "exchange": self.config.instruments[instrument].exchange,
                        "last": format(tick.last, "f"),
                        "bid": format(tick.bid, "f"),
                        "ask": format(tick.ask, "f"),
                        "source": tick.source,
                        "exchange_ts": tick.exchange_ts.isoformat(),
                    }
                )
            return {
                "trading_day": self.trading_day.isoformat(),
                "quotes": quotes,
                "market": self.market.stats(),
                "accounts": accounts,
                "positions": [_position_dict(view) for view in views],
                "by_instrument": [
                    {
                        **row,
                        "floating_pnl": money(row["floating_pnl"]),
                        "gross_notional": money(row["gross_notional"]),
                        "net_notional": money(row["net_notional"]),
                    }
                    for row in board.values()
                ],
                "firm": {
                    "equity": money(firm_equity),
                    "daily_mtm": money(firm_daily),
                    "margin": money(firm_margin),
                    "halted_accounts": sum(1 for item in accounts if item["halted"]),
                },
                "events": [event_dict(event) for event in self.risk.events[-100:]],
            }

    def metrics(self, account_id: str) -> AccountMetrics:
        account = self.accounts[account_id]
        floating = self.positions.floating(account_id, self._price_of, self.config.instruments)
        margin_map, notional_map = self.positions.margin_and_notional(
            account_id,
            self._price_of,
            self.config.instruments,
        )
        margin = sum(margin_map.values(), Decimal(0))
        equity = account.prev_equity + account.realized_pnl + floating - account.commission
        if equity > account.peak_equity:
            account.peak_equity = equity
        drawdown = (
            (account.peak_equity - equity) / account.peak_equity if account.peak_equity else Decimal(0)
        )
        daily = account.realized_pnl + floating
        initial = account.spec.initial_equity
        return AccountMetrics(
            equity=equity,
            available=equity - margin,
            daily_mtm=daily,
            daily_mtm_ratio=(daily / initial) if initial else Decimal(0),
            drawdown=drawdown,
            margin=margin,
            margin_ratio=(margin / equity) if equity else Decimal(0),
            floating=floating,
            realized=account.realized_pnl,
            commission=account.commission,
            concentration=_concentration(notional_map),
            instrument_count=sum(1 for value in notional_map.values() if value > 0),
            margin_by_instrument=margin_map,
            notional_by_instrument=notional_map,
        )

    def status_of(self, account_id: str) -> str:
        if self.accounts[account_id].halted:
            return "HALTED"
        rules = {key[1] for key in self.risk.active if key[0] == account_id}
        if rules & _RESTRICT:
            return "RESTRICTED"
        if rules & _ALERT:
            return "ALERT"
        return "NORMAL"

    def last_prices(self) -> dict[str, Decimal]:
        return {instrument: tick.last for instrument, tick in self.market.latest.items()}

    def settle(self, prices: dict[str, Decimal]) -> dict:
        """逐日盯市。今仓基准是开仓价，昨仓基准是上日结算价；结算后基准改为今日结算价。"""
        with self._lock:
            needed = {lot.instrument for lot in self.positions.lots}
            missing = sorted(needed - set(prices))
            if missing:
                raise ValueError(f"缺少结算价: {missing}")
            unknown = sorted(set(prices) - set(self.config.instruments))
            if unknown:
                raise ValueError(f"未知合约: {unknown}")

            day = self.trading_day
            account_rows = []
            firm_equity = Decimal(0)
            firm_daily = Decimal(0)
            firm_margin = Decimal(0)
            firm_available = Decimal(0)
            for account_id in sorted(self.accounts):
                account = self.accounts[account_id]
                lots = [lot for lot in self.positions.lots if lot.account_id == account_id]
                position_lines = []
                position_pnl = Decimal(0)
                margin = Decimal(0)
                for lot in lots:
                    spec = self.config.instruments[lot.instrument]
                    settle_px = prices[lot.instrument]
                    pnl = mtm_pnl(lot.side, settle_px, lot.prev_settle, lot.qty, spec.multiplier)
                    lot_margin = settle_px * lot.qty * spec.multiplier * spec.margin_rate
                    position_pnl += pnl
                    margin += lot_margin
                    position_lines.append(
                        {
                            "instrument": lot.instrument,
                            "side": lot.side.value,
                            "qty": lot.qty,
                            "basis": money(lot.prev_settle),
                            "settle": money(settle_px),
                            "pnl": money(pnl),
                            "margin": money(lot_margin),
                        }
                    )
                equity = account.prev_equity + account.realized_pnl + position_pnl - account.commission
                mtm_total = account.realized_pnl + position_pnl
                daily_pnl = mtm_total - account.commission
                available = equity - margin
                account_rows.append(
                    {
                        "account_id": account_id,
                        "name": account.spec.name,
                        "prev_equity": money(account.prev_equity),
                        "close_pnl": money(account.realized_pnl),
                        "position_pnl": money(position_pnl),
                        "commission": money(account.commission),
                        "mtm_pnl": money(mtm_total),
                        "daily_pnl": money(daily_pnl),
                        "equity": money(equity),
                        "margin": money(margin),
                        "available": money(available),
                        "margin_ratio": ratio_text((margin / equity) if equity else Decimal(0)),
                        "halted": account.halted,
                        "positions": position_lines,
                        "_equity": equity,
                    }
                )
                firm_equity += equity
                firm_daily += daily_pnl
                firm_margin += margin
                firm_available += available
                for lot in lots:
                    lot.prev_settle = prices[lot.instrument]
                account.prev_equity = equity
                account.realized_pnl = Decimal(0)
                account.commission = Decimal(0)
                if equity > account.peak_equity:
                    account.peak_equity = equity

            for instrument, price in prices.items():
                if instrument in self.market.latest:
                    self.market.overwrite_last(instrument, price)
            self.trading_day = day + timedelta(days=1)
            if self.feed is not None:
                self.feed.set_prices(prices)
                self.feed.trading_day = self.trading_day

            payload = {
                "trading_day": day.isoformat(),
                "generated_at": self.now().isoformat(timespec="seconds"),
                "accounts": [{key: value for key, value in row.items() if key != "_equity"} for row in account_rows],
                "firm": {
                    "equity": money(firm_equity),
                    "daily_pnl": money(firm_daily),
                    "margin": money(firm_margin),
                    "available": money(firm_available),
                },
                "events": [
                    event_dict(event) for event in self.risk.events if event.trading_day == day
                ],
                "rejects": [
                    {
                        "account_id": item.account_id,
                        "instrument": item.instrument,
                        "side": item.side,
                        "offset": item.offset,
                        "qty": item.qty,
                        "reason": item.reason,
                    }
                    for item in self.rejects
                    if item.trading_day == day
                ],
            }
            text, html = render_daily_report(payload)
            stem = f"risk_report_{day.strftime('%Y%m%d')}"
            text_path = self.report_dir / f"{stem}.txt"
            html_path = self.report_dir / f"{stem}.html"
            text_path.write_text(text, encoding="utf-8")
            html_path.write_text(html, encoding="utf-8")
            self.last_report = {
                "trading_day": day.isoformat(),
                "text_path": str(text_path),
                "html_path": str(html_path),
                "html_url": f"/reports/{html_path.name}",
                "firm_equity": money(firm_equity),
                "firm_daily_pnl": money(firm_daily),
                "accounts": payload["accounts"],
            }
            self._emit_risk()
            logger.info("完成 %s 清算，公司权益 %s", day.isoformat(), money(firm_equity))
            return self.last_report

    def acceptance_shock(self, account_id: str, instrument: str = "rb2510") -> dict:
        """把指定账户该合约的价格打到单日亏损阈值，并试一笔开仓、一笔平仓。"""
        with self._lock:
            if account_id not in self.accounts:
                raise ValueError(f"账户不存在: {account_id}")
            if instrument not in self.config.instruments:
                raise ValueError(f"合约不存在: {instrument}")
            target = self._threshold_price(account_id, instrument)
            current = self.market.latest[instrument]
            ts = current.exchange_ts + timedelta(seconds=1)
            seq = self._scenario_seq.get(instrument, 0) + 1
            self._scenario_seq[instrument] = seq
            tick = Tick(
                "scenario",
                instrument,
                ts,
                seq,
                target,
                target,
                target,
                0,
                self.trading_day,
            )
            self.market.ingest(tick)
            if self.feed is not None:
                self.feed.prices[instrument] = target
                if ts > self.feed.clock:
                    self.feed.clock = ts
            events = self._emit_risk()
            metrics = self.metrics(account_id)
            opened = self._submit(
                OrderRequest(account_id, instrument, Side.BUY, Offset.OPEN, target, 1)
            )
            long_qty = self.positions.position_qty(account_id, instrument, PositionSide.LONG)
            short_qty = self.positions.position_qty(account_id, instrument, PositionSide.SHORT)
            if long_qty > 0:
                closed = self._submit(
                    OrderRequest(account_id, instrument, Side.SELL, Offset.CLOSE, target, 1)
                )
            elif short_qty > 0:
                closed = self._submit(
                    OrderRequest(account_id, instrument, Side.BUY, Offset.CLOSE, target, 1)
                )
            else:
                closed = OrderResult(False, None, "没有可平仓位", None, 0, Decimal(0))
            return {
                "account_id": account_id,
                "instrument": instrument,
                "target_price": format(target, "f"),
                "daily_mtm": money(metrics.daily_mtm),
                "threshold": money(
                    -self.config.risk.daily_loss_circuit_breaker_ratio
                    * self.accounts[account_id].spec.initial_equity
                ),
                "halted": self.accounts[account_id].halted,
                "open_accepted": opened.accepted,
                "open_reject_reason": opened.reject_reason,
                "close_accepted": closed.accepted,
                "close_reject_reason": closed.reject_reason,
                "events": [event_dict(event) for event in events],
            }

    def _threshold_price(self, account_id: str, instrument: str) -> Decimal:
        account = self.accounts[account_id]
        target = -self.config.risk.daily_loss_circuit_breaker_ratio * account.spec.initial_equity
        coeff = Decimal(0)
        base = Decimal(0)
        other = account.realized_pnl
        for lot in self.positions.lots:
            if lot.account_id != account_id:
                continue
            spec = self.config.instruments[lot.instrument]
            direction = Decimal(1) if lot.side == PositionSide.LONG else Decimal(-1)
            unit = lot.qty * spec.multiplier * direction
            if lot.instrument == instrument:
                coeff += unit
                base += -lot.prev_settle * unit
            else:
                price = self.market.latest[lot.instrument].last
                other += (price - lot.prev_settle) * unit
        if coeff == 0:
            raise ValueError(f"{account_id} 在 {instrument} 上没有净持仓，无法做熔断验收")
        raw = (target - other - base) / coeff
        tick = self.config.instruments[instrument].tick_size
        steps = (raw / tick).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        price = steps * tick
        if price <= 0:
            price = tick
        if self._daily_with(account_id, {instrument: price}) > target:
            price += -tick if coeff > 0 else tick
        if price <= 0:
            raise ValueError("无法得到有效的熔断价格")
        return price

    def _daily_with(self, account_id: str, overrides: dict[str, Decimal]) -> Decimal:
        account = self.accounts[account_id]

        def price_of(inst: str) -> Decimal:
            if inst in overrides:
                return overrides[inst]
            return self._price_of(inst)

        floating = self.positions.floating(account_id, price_of, self.config.instruments)
        return account.realized_pnl + floating

    def _submit(self, order: OrderRequest) -> OrderResult:
        reason = self.risk.pre_trade(self, order)
        if reason:
            self.rejects.append(
                RejectRecord(
                    ts=self.now(),
                    trading_day=self.trading_day,
                    account_id=order.account_id,
                    instrument=order.instrument,
                    side=order.side.value,
                    offset=order.offset.value,
                    qty=order.qty,
                    reason=reason,
                )
            )
            logger.info("拒单 %s %s %s: %s", order.account_id, order.instrument, order.offset.value, reason)
            return OrderResult(False, None, reason, None, 0, Decimal(0))
        result = self._fill(order)
        self._emit_risk()
        return result

    def _fill(self, order: OrderRequest) -> OrderResult:
        spec = self.config.instruments[order.instrument]
        commission = spec.commission_per_lot * order.qty
        order_id = f"O{next(self._order_ids):06d}"
        ts = order.ts or self.now()
        day = order.trading_day or self.trading_day
        realized = Decimal(0)
        if order.offset == Offset.OPEN:
            side = PositionSide.LONG if order.side == Side.BUY else PositionSide.SHORT
            self.positions.add_lot(
                OpenLot(
                    lot_id=f"L{next(self._lot_ids):06d}",
                    account_id=order.account_id,
                    instrument=order.instrument,
                    side=side,
                    qty=order.qty,
                    open_price=order.price,
                    open_day=day,
                    prev_settle=order.price,
                )
            )
        else:
            realized = self.positions.close(
                order.account_id,
                order.instrument,
                order.side,
                order.qty,
                order.price,
                spec.multiplier,
            )
            self.accounts[order.account_id].realized_pnl += realized
        self.accounts[order.account_id].commission += commission
        self.positions.trades.append(
            Trade(
                trade_id=f"T{next(self._trade_ids):06d}",
                order_id=order_id,
                account_id=order.account_id,
                instrument=order.instrument,
                side=order.side,
                offset=order.offset,
                price=order.price,
                qty=order.qty,
                realized_pnl=realized,
                commission=commission,
                trading_day=day,
                ts=ts,
            )
        )
        return OrderResult(True, order_id, None, order.price, order.qty, realized)

    def _emit_risk(self):
        events = self.risk.evaluate(self)
        for event in events:
            if event.level == ActionLevel.HALT:
                logger.warning("熔断 %s %s", event.account_id, event.message)
        return events

    def _price_of(self, instrument: str) -> Decimal:
        tick = self.market.latest.get(instrument)
        if tick is None:
            raise KeyError(f"没有行情: {instrument}")
        return tick.last


def _concentration(notionals: dict[str, Decimal]) -> Decimal:
    total = sum(notionals.values(), Decimal(0))
    if total == 0:
        return Decimal(0)
    return max(notionals.values()) / total


def _position_dict(view: PositionView) -> dict:
    return {
        "account_id": view.account_id,
        "instrument": view.instrument,
        "exchange": view.exchange,
        "product": view.product,
        "side": view.side.value,
        "qty": view.qty,
        "open_price": format(view.open_price, "f"),
        "prev_settle": format(view.prev_settle, "f"),
        "last": format(view.last, "f"),
        "multiplier": format(view.multiplier, "f"),
        "floating_pnl": money(view.floating_pnl),
        "margin": money(view.margin),
        "notional": money(view.notional),
    }


def order_result_dict(result: OrderResult) -> dict:
    return {
        "accepted": result.accepted,
        "order_id": result.order_id,
        "reject_reason": result.reject_reason,
        "fill_price": None if result.fill_price is None else format(result.fill_price, "f"),
        "fill_qty": result.fill_qty,
        "realized_pnl": money(result.realized_pnl),
    }
