"""新浪财经和东方财富的公开行情。两个网站都不需要柜台账号。

网页行情没有交易所逐笔序号，也不包含断线期间丢掉的中间价。
序号由系统按每个源自己的拉取次数递增。断档回补由 `pixi run demo` 用合成序号打出来。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx

logger = logging.getLogger("cta_risk.quotes")

BEIJING = timezone(timedelta(hours=8))
SOURCE_SINA = "新浪"
SOURCE_EASTMONEY = "东财"

# 配置里的合约 -> (新浪代码, 东财静态行情路径)。这四个是 2026-10-19 仍在交易的合约。
FEEDS = {
    "rb2701": ("nf_RB2701", "113_rb2701"),
    "i2701": ("nf_I2701", "114_i2701"),
    "IF2612": ("nf_IF2612", "220_IF2612"),
    "IC2612": ("nf_IC2612", "220_IC2612"),
}

SINA_URL = "https://hq.sinajs.cn/list="
EASTMONEY_URL = "https://futsseapi.eastmoney.com/static/{path}_qt"


@dataclass(frozen=True)
class Quote:
    source: str
    instrument: str
    last: Decimal
    exchange_ts: datetime


@dataclass
class QuoteBatch:
    quotes: list[Quote]
    errors: list[str]


def fetch_quotes(instruments: list[str]) -> QuoteBatch:
    """两个源都拉。一边失败时，另一边的价格仍然返回。"""
    wanted = [name for name in instruments if name in FEEDS]
    missing = [name for name in instruments if name not in FEEDS]
    errors = [f"没有公开行情代码: {name}" for name in missing]
    quotes: list[Quote] = []
    with httpx.Client(timeout=8.0, follow_redirects=True) as client:
        quotes.extend(_fetch_sina(client, wanted, errors))
        quotes.extend(_fetch_eastmoney(client, wanted, errors))
    return QuoteBatch(quotes, errors)


def parse_sina(payload: str) -> list[Quote]:
    """新浪一行一个合约。商品期货和股指期货的字段位置不同。"""
    by_symbol = {symbol: instrument for instrument, (symbol, _) in FEEDS.items()}
    quotes = []
    for line in payload.splitlines():
        if '="' not in line:
            continue
        symbol = line.split("hq_str_", 1)[1].split("=", 1)[0]
        body = line.split('="', 1)[1].rstrip('";')
        if symbol not in by_symbol or not body:
            continue
        parts = body.split(",")
        last, when = _sina_last_and_time(parts)
        if last is None or when is None:
            continue
        quotes.append(Quote(SOURCE_SINA, by_symbol[symbol], last, when))
    return quotes


def parse_eastmoney(instrument: str, payload: str) -> Quote | None:
    data = json.loads(payload, parse_float=Decimal)
    qt = data.get("qt") or {}
    price = qt.get("p")
    stamp = qt.get("utime")
    if not price or stamp is None:
        return None
    when = datetime.fromtimestamp(int(stamp), BEIJING)
    return Quote(SOURCE_EASTMONEY, instrument, Decimal(price), when)


def _fetch_sina(client: httpx.Client, instruments: list[str], errors: list[str]) -> list[Quote]:
    if not instruments:
        return []
    symbols = ",".join(FEEDS[name][0] for name in instruments)
    try:
        response = client.get(SINA_URL + symbols, headers={"Referer": "https://finance.sina.com.cn"})
        response.raise_for_status()
    except httpx.HTTPError as exc:
        message = f"新浪行情失败: {exc}"
        logger.warning(message)
        errors.append(message)
        return []
    return parse_sina(response.content.decode("gbk", errors="replace"))


def _fetch_eastmoney(client: httpx.Client, instruments: list[str], errors: list[str]) -> list[Quote]:
    quotes = []
    for instrument in instruments:
        url = EASTMONEY_URL.format(path=FEEDS[instrument][1])
        try:
            response = client.get(url, headers={"Referer": "https://quote.eastmoney.com/"})
            response.raise_for_status()
            quote = parse_eastmoney(instrument, response.text)
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
            message = f"东财 {instrument} 失败: {exc}"
            logger.warning(message)
            errors.append(message)
            continue
        if quote is not None:
            quotes.append(quote)
    return quotes


def _sina_last_and_time(parts: list[str]) -> tuple[Decimal | None, datetime | None]:
    if not parts or not parts[0]:
        return None, None
    # 股指以数字开头，最新价在第 4 列，日期后面紧跟 15:00:00 这种时间。
    if _is_number(parts[0]):
        when = _time_after_date(parts)
        if when is None or len(parts) < 4:
            return None, None
        return Decimal(parts[3]), when
    # 商品期货：时间、最新价、日期分别在第 2、9、18 列。
    if len(parts) < 18:
        return None, None
    return Decimal(parts[8]), _clock(parts[17], parts[1])


def _time_after_date(parts: list[str]) -> datetime | None:
    for index, field in enumerate(parts[:-1]):
        if len(field) == 10 and field[4] == "-" and field[7] == "-":
            return _clock(field, parts[index + 1])
    return None


def _clock(day: str, clock: str) -> datetime:
    clock = clock.strip()
    pattern = "%Y-%m-%d %H:%M:%S" if ":" in clock else "%Y-%m-%d %H%M%S"
    return datetime.strptime(f"{day} {clock.zfill(6) if ':' not in clock else clock}", pattern).replace(tzinfo=BEIJING)


def _is_number(value: str) -> bool:
    try:
        Decimal(value)
    except Exception:
        return False
    return True
