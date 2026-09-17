"""Binance USD-M futures + spot public endpoints. No key required (implementation spec,
"T1 is free"). Binance's own /fapi/v1/openInterest endpoint returns open interest already
denominated in the base coin, so no price division — and therefore no venue-mixing risk
— is needed to satisfy the doctrine's "open interest in coins, never dollars" rule.
"""
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

FUTURES_BASE = "https://fapi.binance.com"
SPOT_BASE = "https://api.binance.com"

VENUE = "binance"
FUTURES_SOURCE_ID = "binance_futures"
SPOT_SOURCE_ID = "binance_spot"


async def fetch(instrument: str, cfg: Config, *, client: httpx.AsyncClient) -> list[Observation]:
    sym = default_usdt_symbols(instrument)
    now = datetime.now(timezone.utc)
    out: list[Observation] = []

    premium = await get_json(client, f"{FUTURES_BASE}/fapi/v1/premiumIndex", params={"symbol": sym.futures})
    if "lastFundingRate" not in premium or "markPrice" not in premium:
        raise SourceError(f"binance premiumIndex missing fields for {sym.futures}: {premium}")
    observed_at = now
    out.append(
        Observation.build(
            metric=Metric.FUNDING_8H,
            instrument=instrument,
            value=to_decimal(premium["lastFundingRate"], field="lastFundingRate") * 100,
            unit=Unit.PCT_8H,
            venue=VENUE,
            source_id=FUTURES_SOURCE_ID,
            tier=Tier.T1,
            observed_at=observed_at,
            cfg=cfg,
            raw=premium,
        )
    )
    out.append(
        Observation.build(
            metric=Metric.PRICE,
            instrument=instrument,
            value=to_decimal(premium["markPrice"], field="markPrice"),
            unit=Unit.USD,
            venue=VENUE,
            source_id=FUTURES_SOURCE_ID,
            tier=Tier.T1,
            observed_at=observed_at,
            cfg=cfg,
            raw={"markPrice": premium["markPrice"]},
        )
    )

    oi = await get_json(client, f"{FUTURES_BASE}/fapi/v1/openInterest", params={"symbol": sym.futures})
    if "openInterest" not in oi:
        raise SourceError(f"binance openInterest missing fields for {sym.futures}: {oi}")
    out.append(
        Observation.build(
            metric=Metric.OI_COIN,
            instrument=instrument,
            value=to_decimal(oi["openInterest"], field="openInterest"),
            unit=Unit.COINS,
            venue=VENUE,
            source_id=FUTURES_SOURCE_ID,
            tier=Tier.T1,
            observed_at=observed_at,
            cfg=cfg,
            raw=oi,
        )
    )

    futures_ticker = await get_json(client, f"{FUTURES_BASE}/fapi/v1/ticker/24hr", params={"symbol": sym.futures})
    if "volume" not in futures_ticker:
        raise SourceError(f"binance futures 24hr ticker missing volume for {sym.futures}: {futures_ticker}")
    out.append(
        Observation.build(
            metric=Metric.PERP_VOLUME,
            instrument=instrument,
            value=to_decimal(futures_ticker["volume"], field="volume"),
            unit=Unit.COINS,
            venue=VENUE,
            source_id=FUTURES_SOURCE_ID,
            tier=Tier.T1,
            observed_at=observed_at,
            cfg=cfg,
            raw=futures_ticker,
        )
    )

    try:
        spot_ticker = await get_json(client, f"{SPOT_BASE}/api/v3/ticker/24hr", params={"symbol": sym.spot})
        out.append(
            Observation.build(
                metric=Metric.SPOT_VOLUME,
                instrument=instrument,
                value=to_decimal(spot_ticker["volume"], field="volume"),
                unit=Unit.COINS,
                venue=VENUE,
                source_id=SPOT_SOURCE_ID,
                tier=Tier.T1,
                observed_at=observed_at,
                cfg=cfg,
                raw=spot_ticker,
            )
        )
        out.append(
            Observation.build(
                metric=Metric.PRICE,
                instrument=instrument,
                value=to_decimal(spot_ticker["lastPrice"], field="lastPrice"),
                unit=Unit.USD,
                venue=VENUE,
                source_id=SPOT_SOURCE_ID,
                tier=Tier.T1,
                observed_at=observed_at,
                cfg=cfg,
                raw={"lastPrice": spot_ticker["lastPrice"]},
            )
        )

        band_pct = to_decimal(cfg.get("gate_u", "order_book_band_pct", default=0.01), field="order_book_band_pct")
        depth = await get_json(client, f"{SPOT_BASE}/api/v3/depth", params={"symbol": sym.spot, "limit": 100})
        mid = to_decimal(spot_ticker["lastPrice"], field="lastPrice")
        within_band = sum_depth_within_band(depth.get("bids", []), mid, band_pct) + sum_depth_within_band(
            depth.get("asks", []), mid, band_pct
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
                observed_at=observed_at,
                cfg=cfg,
                raw={"band_pct": str(band_pct), "mid": str(mid)},
            )
        )
    except SourceError:
        # Spot listing absent for this symbol on Binance is common and is not fatal to
        # the futures observations already collected; Gate U's perp/spot ratio simply
        # can't be computed from Binance alone in that case (other venues may cover it).
        pass

    return out
