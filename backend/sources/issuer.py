"""ETF / fund issuer daily holdings. Optional, supplementary ground-truth (doctrine
Layer 1: "dated, verifiable events from primary sources"). No universal API exists across
issuers, so this reads a per-instrument `holdings_url` from config/instruments.yaml that
is expected to return JSON with a numeric `coins_held` field (adapt per issuer as real
wrappers are added). Absence of an entry is not a Gate U failure — ETF flows are not on
the required Gate U / Layer 0 path, only in the broader ground-truth set.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from backend.core.config import Config
from backend.core.observation import Metric, Observation, Tier, Unit
from backend.sources.base import SourceError, get_json, to_decimal
from backend.sources.chain import _instrument_meta

SOURCE_ID = "issuer_holdings"


async def fetch(instrument: str, cfg: Config, *, client: httpx.AsyncClient) -> list[Observation]:
    meta = _instrument_meta(instrument)
    url = meta.get("holdings_url")
    if not url:
        raise SourceError(f"no issuer holdings_url configured for {instrument!r}")
    data = await get_json(client, url)
    if "coins_held" not in data:
        raise SourceError(f"issuer holdings endpoint missing coins_held for {instrument}: {data}")
    now = datetime.now(timezone.utc)
    return [
        Observation.build(
            metric=Metric.ETF_HOLDINGS,
            instrument=instrument,
            value=to_decimal(data["coins_held"], field="coins_held"),
            unit=Unit.COINS,
            venue=meta.get("issuer", "issuer"),
            source_id=SOURCE_ID,
            tier=Tier.T1,
            observed_at=now,
            cfg=cfg,
            raw=data,
        )
    ]
