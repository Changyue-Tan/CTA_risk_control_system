from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from cta_risk.config import default_config_path, load_config
from cta_risk.engine import RiskSystem, halt_price_at_loss_limit, order_result_dict
from cta_risk.models import Offset, OrderRequest, Side, money
from cta_risk.quotes import fetch_quotes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(engine: RiskSystem | None = None) -> FastAPI:
    app = FastAPI(title="CTA 多品种风控与清算")
    app.state.engine = engine or RiskSystem(load_config(default_config_path()))

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/snapshot")
    def snapshot(request: Request):
        return request.app.state.engine.snapshot()

    @app.get("/api/positions")
    def positions(request: Request, account_id: str | None = None, instrument: str | None = None):
        return {"positions": request.app.state.engine.query_positions(account_id, instrument)}

    @app.post("/api/orders")
    def orders(request: Request, body: dict):
        try:
            order = OrderRequest(
                str(body["account_id"]),
                str(body["instrument"]),
                Side(str(body["side"])),
                Offset(str(body["offset"])),
                Decimal(str(body["price"])),
                int(body["qty"]),
            )
        except (KeyError, ValueError, InvalidOperation) as exc:
            return JSONResponse({"accepted": False, "reject_reason": f"委托字段无效: {exc}"}, status_code=400)
        return order_result_dict(request.app.state.engine.submit_order(order))

    @app.post("/api/settlement")
    def settlement(request: Request):
        engine = request.app.state.engine
        try:
            return engine.settle(engine.last_prices())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/accounts/{account_id}/resume")
    def resume(account_id: str, request: Request):
        if account_id not in request.app.state.engine.accounts:
            raise HTTPException(status_code=404, detail="账户不存在")
        halted = request.app.state.engine.resume(account_id)
        return {"account_id": account_id, "halted": halted}

    @app.post("/api/quotes")
    def quotes(request: Request):
        """从新浪和东财各拉一次最新价。同一时间戳不会触发第二次熔断。"""
        engine = request.app.state.engine
        batch = fetch_quotes(list(engine.config.instruments))
        changed = engine.publish_quotes(batch.quotes)
        return {
            "changed": changed,
            "quotes": [
                {
                    "source": quote.source,
                    "instrument": quote.instrument,
                    "last": format(quote.last, "f"),
                    "exchange_ts": quote.exchange_ts.isoformat(),
                }
                for quote in batch.quotes
            ],
            "errors": batch.errors,
        }

    @app.post("/api/demo/circuit-breaker")
    def circuit_breaker(request: Request):
        """把 Gamma 的螺纹打到恰好亏掉初始资金 3% 的价格。20 手、乘数 10 时，价格比昨结算低 150 点。"""
        engine = request.app.state.engine
        instrument, price = halt_price_at_loss_limit(engine.config)
        engine.set_price(instrument, price)
        metrics = engine.metrics("gamma")
        opened = engine.submit_order(OrderRequest("gamma", instrument, Side.BUY, Offset.OPEN, price, 1))
        closed = engine.submit_order(OrderRequest("gamma", instrument, Side.SELL, Offset.CLOSE, price, 1))
        return {
            "instrument": instrument,
            "target_price": format(price, "f"),
            "daily_mtm": money(metrics.daily_mtm),
            "halted": engine.accounts["gamma"].halted,
            "open_accepted": opened.accepted,
            "open_reject_reason": opened.reject_reason,
            "close_accepted": closed.accepted,
        }

    return app


app = create_app()
