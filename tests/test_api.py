from decimal import Decimal

from fastapi.testclient import TestClient

from cta_risk.app import create_app
from cta_risk.config import load_config
from cta_risk.engine import REJECT_HALT, RiskSystem
from tests.conftest import instrument, make_account, make_config


def test_order_position_and_snapshot_round_trip(tmp_path):
    """下单、持仓查询、账户快照和首页可以串起来调用。"""
    engine = RiskSystem(
        make_config([instrument()], [make_account()], prices={"rb2510": Decimal("3000")}),
        report_dir=tmp_path,
    )
    app = create_app(engine)
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        opened = client.post(
            "/api/orders",
            json={
                "account_id": "alpha",
                "instrument": "rb2510",
                "side": "buy",
                "offset": "open",
                "price": "3000",
                "qty": 2,
            },
        )
        assert opened.json()["accepted"] is True
        rows = client.get("/api/positions", params={"account_id": "alpha"}).json()["positions"]
        assert rows[0]["qty"] == 2
        assert client.get("/api/snapshot").json()["accounts"][0]["account_id"] == "alpha"
        assert "拉取真实行情" in client.get("/").text
        assert "熔断验收" in client.get("/").text


def test_circuit_breaker_button_halts_gamma_and_blocks_open(tmp_path):
    """熔断验收把 Gamma 打到 3% 亏损线：开仓拒绝，平仓成交。"""
    engine = RiskSystem(load_config(), report_dir=tmp_path)
    app = create_app(engine)
    with TestClient(app) as client:
        body = client.post("/api/demo/circuit-breaker").json()
        assert body["instrument"] == "rb2701"
        assert body["target_price"] == "2930.00"
        assert body["daily_mtm"] == "-30000.00"
        assert body["halted"] is True
        assert body["open_accepted"] is False
        assert body["open_reject_reason"] == REJECT_HALT
        assert body["close_accepted"] is True
