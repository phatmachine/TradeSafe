"""Hyperliquid info API. No key required. Positions and liquidations here are publicly
auditable on-chain, not merely reported by an intermediary (doctrine, Layer 1 source
stack table) — this is the one venue where the order book itself is T1 by construction.

Hyperliquid pays funding hourly; its `funding` field is a per-hour rate. We multiply by
8 for a linear approximation comparable to the other venues' 8h rate. This is an
approximation, not compounding, and is noted as such — doctrine 0.4 says log dispersion
rather than paper over it, so if this approximation matters it will show up as venue
dispersion in Layer 0 rather than being silently smoothed.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from backend.core.config import Config
from backend.core.observation import Metric, Observation, Tier, Unit
from backend.sources.base import SourceError, post_json, to_decimal

API_URL = "https://api.hyperliquid.xyz/info"
VENUE = "hyperliquid"
SOURCE_ID = "hyperliquid"


async def fetch(instrument: str, cfg: Config, *, client: httpx.AsyncClient) -> list[Observation]:
    payload = await post_json(client, API_URL, json_body={"type": "metaAndAssetCtxs"})
    if not isinstance(payload, list) or len(payload) != 2:
        raise SourceError(f"hyperliquid metaAndAssetCtxs: unexpected shape: {type(payload)}")
    meta, asset_ctxs = payload
    universe = meta.get("universe", [])
    symbol = instrument.upper()
    idx = next((i for i, a in enumerate(universe) if a.get("name") == symbol), None)
    if idx is None:
        raise SourceError(f"hyperliquid: {symbol!r} not listed")
    ctx = asset_ctxs[idx]
    for field in ("funding", "openInterest", "markPx", "dayNtlVlm"):
        if field not in ctx:
            raise SourceError(f"hyperliquid asset ctx missing {field!r} for {symbol}: {ctx}")

    now = datetime.now(timezone.utc)
    mark_px = to_decimal(ctx["markPx"], field="markPx")
    out = [
        Observation.build(
            metric=Metric.FUNDING_8H,
            instrument=instrument,
            value=to_decimal(ctx["funding"], field="funding") * 8 * 100,
            unit=Unit.PCT_8H,
            venue=VENUE,
            source_id=SOURCE_ID,
            tier=Tier.T1,
            observed_at=now,
            cfg=cfg,
            raw=ctx,
        ),
        Observation.build(
            metric=Metric.PRICE,
            instrument=instrument,
            value=mark_px,
            unit=Unit.USD,
            venue=VENUE,
            source_id=SOURCE_ID,
            tier=Tier.T1,
            observed_at=now,
            cfg=cfg,
            raw={"markPx": ctx["markPx"]},
        ),
        Observation.build(
            metric=Metric.OI_COIN,
            instrument=instrument,
            value=to_decimal(ctx["openInterest"], field="openInterest"),
            unit=Unit.COINS,
            venue=VENUE,
            source_id=SOURCE_ID,
            tier=Tier.T1,
            observed_at=now,
            cfg=cfg,
            raw=ctx,
        ),
    ]
    if mark_px > 0:
        # Same-venue division only: USD notional volume from this venue's own mark
        # price, never another venue's — permitted under the never-mix-venues rule.
        coin_volume = to_decimal(ctx["dayNtlVlm"], field="dayNtlVlm") / mark_px
        out.append(
            Observation.build(
                metric=Metric.PERP_VOLUME,
                instrument=instrument,
                value=coin_volume,
                unit=Unit.COINS,
                venue=VENUE,
                source_id=SOURCE_ID,
                tier=Tier.T1,
                observed_at=now,
                cfg=cfg,
                raw=ctx,
            )
        )
    return out
