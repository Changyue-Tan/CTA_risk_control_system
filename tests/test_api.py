from decimal import Decimal

from fastapi.testclient import TestClient

from cta_risk.app import create_app
from cta_risk.engine import RiskSystem
from tests.conftest import instrument, make_account, make_config


def test_order_position_and_snapshot_round_trip(tmp_path):
    engine = RiskSystem(
        make_config([instrument()], [make_account()], prices={"rb2510": Decimal("3000")}),
        report_dir=tmp_path,
    )
    app = create_app(engine)
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
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
        assert opened.status_code == 200
        assert opened.json()["accepted"] is True
        rows = client.get("/api/positions", params={"account_id": "alpha"}).json()["positions"]
        assert rows[0]["qty"] == 2
        snapshot = client.get("/api/snapshot")
        assert snapshot.status_code == 200
        assert snapshot.json()["accounts"][0]["account_id"] == "alpha"
        page = client.get("/")
        assert page.status_code == 200
        assert "熔断验收" in page.text
