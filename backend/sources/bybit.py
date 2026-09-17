"""Bybit v5 public market endpoints. No key required."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from backend.core.config import Config
from backend.core.observation import Metric, Observation, Tier, Unit
from backend.sources.base import (
    SourceError,
    default_usdt_symbols,
    get_json,
    sum_depth_within_band,
    to_decimal,
)

API_BASE = "https://api.bybit.com"
VENUE = "bybit"
FUTURES_SOURCE_ID = "bybit_futures"
SPOT_SOURCE_ID = "bybit_spot"


def _first_result(payload: dict, *, context: str) -> dict:
    result = payload.get("result", {})
    items = result.get("list") or []
    if not items:
        raise SourceError(f"bybit {context}: empty result list: {payload}")
    return items[0]


async def fetch(instrument: str, cfg: Config, *, client: httpx.AsyncClient) -> list[Observation]:
    sym = default_usdt_symbols(instrument)
    now = datetime.now(timezone.utc)
    out: list[Observation] = []

    linear = await get_json(
        client, f"{API_BASE}/v5/market/tickers", params={"category": "linear", "symbol": sym.futures}
    )
    row = _first_result(linear, context="linear tickers")
    for field in ("fundingRate", "openInterest", "volume24h", "lastPrice"):
        if field not in row:
            raise SourceError(f"bybit linear ticker missing {field!r} for {sym.futures}: {row}")

    out.append(
        Observation.build(
            metric=Metric.FUNDING_8H,
            instrument=instrument,
            value=to_decimal(row["fundingRate"], field="fundingRate") * 100,
            unit=Unit.PCT_8H,
            venue=VENUE,
            source_id=FUTURES_SOURCE_ID,
            tier=Tier.T1,
            observed_at=now,
            cfg=cfg,
            raw=row,
        )
    )
    out.append(
        Observation.build(
            metric=Metric.PRICE,
            instrument=instrument,
            value=to_decimal(row["lastPrice"], field="lastPrice"),
            unit=Unit.USD,
            venue=VENUE,
            source_id=FUTURES_SOURCE_ID,
            tier=Tier.T1,
            observed_at=now,
            cfg=cfg,
            raw={"lastPrice": row["lastPrice"]},
        )
    )
    # Bybit's linear (USDT-margined) openInterest is denominated in the base coin
    # directly — no division by price, no venue mixing.
    out.append(
        Observation.build(
            metric=Metric.OI_COIN,
            instrument=instrument,
            value=to_decimal(row["openInterest"], field="openInterest"),
            unit=Unit.COINS,
            venue=VENUE,
            source_id=FUTURES_SOURCE_ID,
            tier=Tier.T1,
            observed_at=now,
            cfg=cfg,
            raw=row,
        )
    )
    out.append(
        Observation.build(
            metric=Metric.PERP_VOLUME,
            instrument=instrument,
            value=to_decimal(row["volume24h"], field="volume24h"),
            unit=Unit.COINS,
            venue=VENUE,
            source_id=FUTURES_SOURCE_ID,
            tier=Tier.T1,
            observed_at=now,
            cfg=cfg,
            raw=row,
        )
    )

    try:
        spot = await get_json(
            client, f"{API_BASE}/v5/market/tickers", params={"category": "spot", "symbol": sym.spot}
        )
        srow = _first_result(spot, context="spot tickers")
        out.append(
            Observation.build(
                metric=Metric.SPOT_VOLUME,
                instrument=instrument,
                value=to_decimal(srow["volume24h"], field="volume24h"),
                unit=Unit.COINS,
                venue=VENUE,
                source_id=SPOT_SOURCE_ID,
                tier=Tier.T1,
                observed_at=now,
                cfg=cfg,
                raw=srow,
            )
        )
        out.append(
            Observation.build(
                metric=Metric.PRICE,
                instrument=instrument,
                value=to_decimal(srow["lastPrice"], field="lastPrice"),
                unit=Unit.USD,
                venue=VENUE,
                source_id=SPOT_SOURCE_ID,
                tier=Tier.T1,
                observed_at=now,
                cfg=cfg,
                raw={"lastPrice": srow["lastPrice"]},
            )
        )

        band_pct = to_decimal(cfg.get("gate_u", "order_book_band_pct", default=0.01), field="order_book_band_pct")
        book = await get_json(client, f"{API_BASE}/v5/market/orderbook", params={"category": "spot", "symbol": sym.spot, "limit": 50})
        result = book.get("result", {})
        mid = to_decimal(srow["lastPrice"], field="lastPrice")
        within_band = sum_depth_within_band(result.get("b", []), mid, band_pct) + sum_depth_within_band(
            result.get("a", []), mid, band_pct
        )
        out.append(
            Observation.build(
                metric=Metric.ORDER_BOOK_DEPTH,
                instrument=instrument,
                value=within_band,
                unit=Unit.COINS,
                venue=VENUE,
                source_id=SPOT_SOURCE_ID,
                tier=Tier.T1,
                observed_at=now,
                cfg=cfg,
                raw={"band_pct": str(band_pct), "mid": str(mid)},
            )
        )
    except SourceError:
        pass

    return out
