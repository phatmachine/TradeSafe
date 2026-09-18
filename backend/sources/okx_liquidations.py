"""Completed liquidation events from OKX's public REST endpoint — the liquidation feed
that actually delivers in this deployment. Binance's forceOrder data is websocket-only,
and websocket frames do not flow here (the handshake completes, no frame ever arrives —
see sources/liquidations.py, which is kept running in case that changes). OKX publishes
the same fact over plain REST, which does flow, so this is polled rather than streamed.

The endpoint returns the newest 100 filled liquidation orders per underlying, and pages
backward in time via `after` (records earlier than the given ts). OKX retains roughly the
last 24 hours — measured 2026-09-18: BTC 1,110 prints / 12 pages, ZEC 3,120 / 32 pages —
so a fresh start backfills a day of history rather than waiting for one.

Units: `sz` is a contract count, not coins. A USDT-margined SWAP contract is worth
`ctVal` of the base coin (0.01 BTC for BTC-USDT-SWAP), so the stored value is
sz * ctVal * bkPx in USD. Only `linear` contracts are accepted — an inverse contract's
ctVal is denominated in USD and the same arithmetic would be wrong by a factor of price.

Side convention matches sources/liquidations.py and compute/cohort.py: a forced BUY
(side=buy, closing a short) is stored positive, a forced SELL (closing a long) negative.

Like Binance's stream this is a sample of one venue, not a census of the market, and is
treated as such: it feeds the trapped-cohort classification and the cascade/squeeze
"print settled" condition, never a decision on its own.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal

import httpx

from backend.core.config import Config
from backend.core.observation import Metric, Observation, Tier, Unit
from backend.sources.base import SourceError, get_json, parse_ms_timestamp, to_decimal
from backend.sources.okx import API_BASE

VENUE = "okx"
SOURCE_ID = "okx_futures"  # registered in config/sources.yaml with `liquidations` in its metrics

# Safety cap on backward paging per poll. A full day of ZEC — the busiest instrument
# measured — was 32 pages, so this covers a cold start with headroom while bounding what
# one poll can cost if the venue ever stops honouring `after`.
MAX_PAGES = 50
PAGE_DELAY_SECONDS = 0.1  # OKX public rate limit is per-IP; a cold start pages ~30 times

_contract_values: dict[str, Decimal] = {}


async def contract_value(instrument: str, *, client: httpx.AsyncClient) -> Decimal:
    inst_id = f"{instrument.upper()}-USDT-SWAP"
    if inst_id in _contract_values:
        return _contract_values[inst_id]
    payload = await get_json(
        client, f"{API_BASE}/api/v5/public/instruments", params={"instType": "SWAP", "instId": inst_id}
    )
    data = payload.get("data") or []
    if not data:
        raise SourceError(f"okx instruments: no SWAP listed for {inst_id}: {payload}")
    row = data[0]
    if row.get("ctType") != "linear":
        raise SourceError(f"okx {inst_id} is {row.get('ctType')!r}, not linear — ctVal would not be in coins")
    ct_val = to_decimal(row.get("ctVal"), field="ctVal")
    if ct_val <= 0:
        raise SourceError(f"okx {inst_id} ctVal is not positive: {ct_val}")
    _contract_values[inst_id] = ct_val
    return ct_val


def to_observation(instrument: str, row: dict, ct_val: Decimal, cfg: Config) -> Observation:
    side = row.get("side")
    if side not in ("buy", "sell"):
        raise SourceError(f"okx liquidation row has no usable side: {row}")
    contracts = to_decimal(row.get("sz"), field="sz")
    price = to_decimal(row.get("bkPx"), field="bkPx")
    notional = contracts * ct_val * price
    return Observation.build(
        metric=Metric.LIQUIDATION,
        instrument=instrument,
        value=notional if side == "buy" else -notional,
        unit=Unit.USD,
        venue=VENUE,
        source_id=SOURCE_ID,
        tier=Tier.T1,
        observed_at=parse_ms_timestamp(row.get("ts")),
        cfg=cfg,
        raw={**row, "ctVal": str(ct_val)},
    )


async def fetch_since(
    instrument: str, cfg: Config, *, client: httpx.AsyncClient, since: datetime | None
) -> list[Observation]:
    """Every liquidation print observed at or after `since` (or everything OKX still
    retains when `since` is None), oldest first. Rows at exactly `since` are returned
    too — the caller dedupes them against the store, which is cheaper and safer than
    guessing whether a same-millisecond print was already seen."""
    ct_val = await contract_value(instrument, client=client)
    params = {"instType": "SWAP", "uly": f"{instrument.upper()}-USDT", "state": "filled"}
    out: list[Observation] = []
    after: int | None = None
    for page in range(MAX_PAGES):
        if page:
            await asyncio.sleep(PAGE_DELAY_SECONDS)
        payload = await get_json(
            client,
            f"{API_BASE}/api/v5/public/liquidation-orders",
            params={**params, **({"after": after} if after is not None else {})},
        )
        if payload.get("code") not in ("0", 0):
            raise SourceError(f"okx liquidation-orders error for {instrument}: {payload}")
        data = payload.get("data") or []
        details = data[0].get("details", []) if data else []
        if not details:
            break
        page_obs = [to_observation(instrument, row, ct_val, cfg) for row in details]
        out.extend(o for o in page_obs if since is None or o.observed_at >= since)
        oldest = min(page_obs, key=lambda o: o.observed_at)
        if since is not None and oldest.observed_at <= since:
            break
        after = int(min(to_decimal(row.get("ts"), field="ts") for row in details))
    return sorted(out, key=lambda o: o.observed_at)
