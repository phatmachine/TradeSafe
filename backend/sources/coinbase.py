"""Coinbase Exchange public spot endpoints. No key required (implementation spec, "T1 is
free"). Added as an independent spot venue for Gate U condition 2 (perp/spot volume
ratio) and Layer 0 independence — Binance/Bybit/OKX spot alone understate real
market-wide spot liquidity for majors (see deploy/README.md, "Known limitations").
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from backend.core.config import Config
from backend.core.observation import Metric, Observation, Tier, Unit
from backend.sources.base import SourceError, get_json, sum_depth_within_band, to_decimal

BASE = "https://api.exchange.coinbase.com"
VENUE = "coinbase"
SOURCE_ID = "coinbase_spot"


def _product_id(instrument: str) -> str:
    return f"{instrument.upper()}-USD"


async def fetch(instrument: str, cfg: Config, *, client: httpx.AsyncClient) -> list[Observation]:
    product = _product_id(instrument)
    now = datetime.now(timezone.utc)
    out: list[Observation] = []

    ticker = await get_json(client, f"{BASE}/products/{product}/ticker")
    if "price" not in ticker or "volume" not in ticker:
        raise SourceError(f"coinbase ticker missing fields for {product}: {ticker}")
    mid = to_decimal(ticker["price"], field="price")
    out.append(
        Observation.build(
            metric=Metric.PRICE,
            instrument=instrument,
            value=mid,
            unit=Unit.USD,
            venue=VENUE,
            source_id=SOURCE_ID,
            tier=Tier.T1,
            observed_at=now,
            cfg=cfg,
            raw={"price": ticker["price"]},
        )
    )
    out.append(
        Observation.build(
            metric=Metric.SPOT_VOLUME,
            instrument=instrument,
            value=to_decimal(ticker["volume"], field="volume"),
            unit=Unit.COINS,
            venue=VENUE,
            source_id=SOURCE_ID,
            tier=Tier.T1,
            observed_at=now,
            cfg=cfg,
            raw={"volume": ticker["volume"]},
        )
    )

    band_pct = to_decimal(cfg.get("gate_u", "order_book_band_pct", default=0.01), field="order_book_band_pct")
    book = await get_json(client, f"{BASE}/products/{product}/book", params={"level": "2"})
    within_band = sum_depth_within_band(book.get("bids", []), mid, band_pct) + sum_depth_within_band(
        book.get("asks", []), mid, band_pct
    )
    out.append(
        Observation.build(
            metric=Metric.ORDER_BOOK_DEPTH,
            instrument=instrument,
            value=within_band,
            unit=Unit.COINS,
            venue=VENUE,
            source_id=SOURCE_ID,
            tier=Tier.T1,
            observed_at=now,
            cfg=cfg,
            raw={"band_pct": str(band_pct), "mid": str(mid)},
        )
    )
    return out
