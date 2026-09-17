"""Kraken public spot endpoints. No key required. Added as an independent spot venue for
the same reason as sources/coinbase.py — see that module's docstring.

Kraken's REST API is queried by pair (e.g. "XBTUSD") but its JSON response keys results
by Kraken's own internal pair name (e.g. "XXBTZUSD"), which doesn't always match what was
requested. Rather than replicate Kraken's asset-naming quirks (X/Z prefixes, BTC -> XBT),
every query here asks for exactly one pair, so the single entry in the response's
`result` dict is unambiguous regardless of its key.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from backend.core.config import Config
from backend.core.observation import Metric, Observation, Tier, Unit
from backend.sources.base import SourceError, get_json, sum_depth_within_band, to_decimal

BASE = "https://api.kraken.com"
VENUE = "kraken"
SOURCE_ID = "kraken_spot"


def _pair(instrument: str) -> str:
    base = "XBT" if instrument.upper() == "BTC" else instrument.upper()
    return f"{base}USD"


async def _public(client: httpx.AsyncClient, endpoint: str, pair: str) -> dict:
    data = await get_json(client, f"{BASE}/0/public/{endpoint}", params={"pair": pair})
    errors = data.get("error") or []
    if errors:
        raise SourceError(f"kraken {endpoint} error for {pair}: {errors}")
    result = data.get("result") or {}
    if not result:
        raise SourceError(f"kraken {endpoint} returned no result for {pair}: {data}")
    return next(iter(result.values()))


async def fetch(instrument: str, cfg: Config, *, client: httpx.AsyncClient) -> list[Observation]:
    pair = _pair(instrument)
    now = datetime.now(timezone.utc)
    out: list[Observation] = []

    ticker = await _public(client, "Ticker", pair)
    if "c" not in ticker or "v" not in ticker:
        raise SourceError(f"kraken ticker missing fields for {pair}: {ticker}")
    mid = to_decimal(ticker["c"][0], field="c[0]")
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
            raw={"c": ticker["c"]},
        )
    )
    out.append(
        Observation.build(
            metric=Metric.SPOT_VOLUME,
            instrument=instrument,
            value=to_decimal(ticker["v"][1], field="v[1]"),
            unit=Unit.COINS,
            venue=VENUE,
            source_id=SOURCE_ID,
            tier=Tier.T1,
            observed_at=now,
            cfg=cfg,
            raw={"v": ticker["v"]},
        )
    )

    band_pct = to_decimal(cfg.get("gate_u", "order_book_band_pct", default=0.01), field="order_book_band_pct")
    book = await _public(client, "Depth", pair)
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
