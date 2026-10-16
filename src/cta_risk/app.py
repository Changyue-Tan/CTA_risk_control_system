from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from cta_risk.backtest import builtin_demo
from cta_risk.config import default_config_path, load_config, project_root
from cta_risk.engine import RiskSystem, order_result_dict
from cta_risk.market import LiveFeed, run_gap_demo
from cta_risk.models import Offset, OrderRequest, Side

logger = logging.getLogger("cta_risk.app")
STATIC_DIR = Path(__file__).resolve().parent / "static"


def configure_logging() -> None:
    log_dir = project_root() / "logs"
    log_dir.mkdir(exist_ok=True)
    root = logging.getLogger()
    if root.handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_dir / "risk.log", encoding="utf-8"),
        ],
    )


def start_background_feed(engine: RiskSystem) -> threading.Event:
    prices = {name: engine.market.latest[name].last for name in engine.config.reference_prices}
    feed = LiveFeed(
        engine.config.instruments,
        prices,
        engine.trading_day,
        engine.config.simulator.seed,
        engine.config.simulator.drop_primary_every,
    )
    engine.bind_feed(feed)
    stop = threading.Event()
    interval = engine.config.simulator.interval_ms / 1000

    def loop() -> None:
        while not stop.wait(interval):
            try:
                engine.apply_feed_actions(feed.next_actions())
            except Exception:
                logger.exception("模拟行情步进失败")

    threading.Thread(target=loop, name="market-feed", daemon=True).start()
    return stop


def create_app(engine: RiskSystem | None = None) -> FastAPI:
    configure_logging()
    system = engine or RiskSystem(load_config(default_config_path()))
    system.report_dir.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = None
        if app.state.engine.config.simulator.enabled:
            stop = start_background_feed(app.state.engine)
            logger.info("双源模拟行情已启动")
        yield
        if stop is not None:
            stop.set()

    app = FastAPI(title="CTA 多品种风控与清算", lifespan=lifespan)
    app.state.engine = system
    app.mount("/reports", StaticFiles(directory=system.report_dir), name="reports")

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
        rows = request.app.state.engine.query_positions(account_id, instrument)
        return {"positions": rows}

    @app.post("/api/orders")
    def orders(request: Request, body: dict):
        try:
            order = OrderRequest(
                account_id=str(body["account_id"]),
                instrument=str(body["instrument"]),
                side=Side(str(body["side"])),
                offset=Offset(str(body["offset"])),
                price=Decimal(str(body["price"])),
                qty=int(body["qty"]),
            )
        except (KeyError, ValueError, InvalidOperation) as exc:
            return JSONResponse({"accepted": False, "reject_reason": f"委托字段无效: {exc}"}, status_code=400)
        result = request.app.state.engine.submit_order(order)
        return order_result_dict(result)

    @app.post("/api/settlement")
    def settlement(request: Request, body: dict | None = None):
        engine_ = request.app.state.engine
        raw = (body or {}).get("prices")
        try:
            if raw:
                prices = {name: Decimal(str(price)) for name, price in raw.items()}
            else:
                prices = engine_.last_prices()
            report = engine_.settle(prices)
        except (ValueError, InvalidOperation, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return report

    @app.post("/api/accounts/{account_id}/resume")
    def resume(account_id: str, request: Request):
        try:
            return request.app.state.engine.resume(account_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/demo/circuit-breaker")
    def circuit_breaker(request: Request, account_id: str = "gamma", instrument: str = "rb2510"):
        try:
            return request.app.state.engine.acceptance_shock(account_id, instrument)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/demo/gap")
    def gap_demo():
        return run_gap_demo()

    @app.post("/api/demo/backtest")
    def backtest_demo():
        return builtin_demo()

    return app


app = create_app()
