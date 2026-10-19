from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi.testclient import TestClient

from cta_risk.app import create_app
from cta_risk.engine import RiskSystem
from cta_risk.market import MarketHub
from cta_risk.models import Tick
from cta_risk.quotes import Quote, QuoteBatch, parse_eastmoney, parse_sina
from tests.conftest import instrument, make_account, make_config

BEIJING = timezone(timedelta(hours=8))

SINA = """
var hq_str_nf_RB2701="螺纹钢2701,214538,3071.000,3077.000,3066.000,0.000,3071.000,3072.000,3071.000,0.000,3080.000,1,1,1,1,沪,螺纹钢,2026-10-19,1";
var hq_str_nf_IF2612="4086.400,4140.200,4082.000,4110.400,1,2026-10-19,15:00:00,沪深300";
var hq_str_nf_RB2510="";
"""


def test_sina_parser_reads_commodity_and_index_quotes():
    """商品期货取第 9 列，股指期货取第 4 列。空行跳过。"""
    quotes = {item.instrument: item for item in parse_sina(SINA)}
    assert quotes["rb2701"].last == Decimal("3071.000")
    assert quotes["rb2701"].exchange_ts == datetime(2026, 10, 19, 21, 45, 38, tzinfo=BEIJING)
    assert quotes["IF2612"].last == Decimal("4110.400")
    assert quotes["IF2612"].exchange_ts == datetime(2026, 10, 19, 15, 0, tzinfo=BEIJING)
    assert "rb2510" not in quotes


def test_eastmoney_parser_keeps_decimal_prices():
    quote = parse_eastmoney("rb2701", '{"qt":{"p":3071.0,"utime":1792417554}}')
    assert quote is not None
    assert quote.last == Decimal("3071.0")
    assert quote.source == "东财"
    assert quote.exchange_ts.astimezone(BEIJING).date().isoformat() == "2026-10-19"


def test_day_session_close_is_newer_than_the_reference_price():
    """参考价放在北京时间 0 点，所以当天 15:00 的股指收盘可以覆盖它。"""
    hub = MarketHub()
    day = datetime(2026, 10, 19, tzinfo=BEIJING).date()
    hub.seed_reference_price("IF2612", Decimal("4076.4"), day)
    close = datetime(2026, 10, 19, 15, 0, tzinfo=BEIJING)
    assert hub.ingest(Tick("新浪", "IF2612", close, 1, Decimal("4110.4"), day)) is True
    assert hub.last["IF2612"].last == Decimal("4110.4")


def test_same_exchange_timestamp_from_the_second_source_does_not_move_price_again():
    engine = RiskSystem(make_config([instrument()], [make_account()], prices={"rb2510": Decimal("3000")}))
    when = datetime(2026, 10, 19, 21, 45, tzinfo=BEIJING)
    first = Quote("新浪", "rb2510", Decimal("3071"), when)
    second = Quote("东财", "rb2510", Decimal("3072"), when)
    assert engine.publish_quotes([first, second]) == 1
    assert engine.market.last["rb2510"].last == Decimal("3071")
    assert engine.market.last["rb2510"].source == "新浪"


def test_quote_endpoint_updates_the_snapshot(monkeypatch):
    engine = RiskSystem(make_config([instrument()], [make_account()], prices={"rb2510": Decimal("3000")}))
    when = datetime(2026, 10, 19, 21, 45, tzinfo=BEIJING)
    batch = QuoteBatch([Quote("新浪", "rb2510", Decimal("3071"), when)], [])
    monkeypatch.setattr("cta_risk.app.fetch_quotes", lambda instruments: batch)
    with TestClient(create_app(engine)) as client:
        body = client.post("/api/quotes").json()
        assert body["changed"] == 1
        assert body["quotes"][0]["last"] == "3071"
        assert client.get("/api/snapshot").json()["quotes"][0]["last"] == "3071"
        assert "拉取真实行情" in client.get("/").text
