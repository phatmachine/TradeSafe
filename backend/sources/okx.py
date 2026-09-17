"""OKX v5 public endpoints. No key required.

Field units per OKX docs: for a SWAP instrument, vol24h is a contract count and
volCcy24h is the base-currency (coin) volume; for SPOT it is the other way around
(vol24h is base-currency, volCcy24h is quote-currency). Each fetch below picks the
coin-denominated field explicitly rather than assuming the same field name means the
same unit across instrument types.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from backend.core.config import Config
from backend.core.observation import Metric, Observation, Tier, Unit
from backend.sources.base import SourceError, get_json, sum_depth_within_band, to_decimal

API_BASE = "https://www.okx.com"
VENUE = "okx"
FUTURES_SOURCE_ID = "okx_futures"
SPOT_SOURCE_ID = "okx_spot"


def _symbols(instrument: str) -> tuple[str, str]:
    base = instrument.upper()
    return f"{base}-USDT-SWAP", f"{base}-USDT"


def _first(payload: dict, *, context: str) -> dict:
    data = payload.get("data") or []
    if not data:
        raise SourceError(f"okx {context}: empty data: {payload}")
    return data[0]


async def fetch(instrument: str, cfg: Config, *, client: httpx.AsyncClient) -> list[Observation]:
    swap_id, spot_id = _symbols(instrument)
    now = datetime.now(timezone.utc)
    out: list[Observation] = []

    funding = await get_json(client, f"{API_BASE}/api/v5/public/funding-rate", params={"instId": swap_id})
    frow = _first(funding, context="funding-rate")
    out.append(
        Observation.build(
            metric=Metric.FUNDING_8H,
            instrument=instrument,
            value=to_decimal(frow["fundingRate"], field="fundingRate") * 100,
            unit=Unit.PCT_8H,
            venue=VENUE,
            source_id=FUTURES_SOURCE_ID,
            tier=Tier.T1,
            observed_at=now,
            cfg=cfg,
            raw=frow,
        )
    )

    oi = await get_json(client, f"{API_BASE}/api/v5/public/open-interest", params={"instId": swap_id})
    oirow = _first(oi, context="open-interest")
    out.append(
        Observation.build(
            metric=Metric.OI_COIN,
            instrument=instrument,
            value=to_decimal(oirow["oiCcy"], field="oiCcy"),
            unit=Unit.COINS,
            venue=VENUE,
            source_id=FUTURES_SOURCE_ID,
            tier=Tier.T1,
            observed_at=now,
            cfg=cfg,
            raw=oirow,
        )
    )

    ticker = await get_json(client, f"{API_BASE}/api/v5/market/ticker", params={"instId": swap_id})
    trow = _first(ticker, context="swap ticker")
    out.append(
        Observation.build(
            metric=Metric.PRICE,
            instrument=instrument,
            value=to_decimal(trow["last"], field="last"),
            unit=Unit.USD,
            venue=VENUE,
            source_id=FUTURES_SOURCE_ID,
            tier=Tier.T1,
            observed_at=now,
            cfg=cfg,
            raw={"last": trow["last"]},
        )
    )
    out.append(
        Observation.build(
            metric=Metric.PERP_VOLUME,
            instrument=instrument,
            value=to_decimal(trow["volCcy24h"], field="volCcy24h"),
            unit=Unit.COINS,
            venue=VENUE,
            source_id=FUTURES_SOURCE_ID,
            tier=Tier.T1,
            observed_at=now,
            cfg=cfg,
            raw=trow,
        )
    )

    try:
        sticker = await get_json(client, f"{API_BASE}/api/v5/market/ticker", params={"instId": spot_id})
        srow = _first(sticker, context="spot ticker")
        out.append(
            Observation.build(
                metric=Metric.SPOT_VOLUME,
                instrument=instrument,
                value=to_decimal(srow["vol24h"], field="vol24h"),
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
                value=to_decimal(srow["last"], field="last"),
                unit=Unit.USD,
                venue=VENUE,
                source_id=SPOT_SOURCE_ID,
                tier=Tier.T1,
                observed_at=now,
                cfg=cfg,
                raw={"last": srow["last"]},
            )
        )

        band_pct = to_decimal(cfg.get("gate_u", "order_book_band_pct", default=0.01), field="order_book_band_pct")
        book = await get_json(client, f"{API_BASE}/api/v5/market/books", params={"instId": spot_id, "sz": 100})
        brow = _first(book, context="spot order book")
        mid = to_decimal(srow["last"], field="last")
        within_band = sum_depth_within_band(brow.get("bids", []), mid, band_pct) + sum_depth_within_band(
            brow.get("asks", []), mid, band_pct
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
