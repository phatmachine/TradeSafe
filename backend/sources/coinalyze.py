"""Completed liquidations on Binance and Bybit, per exchange and per minute, from
Coinalyze's free API. Both exchanges publish liquidations only over websockets, and
websocket frames don't flow in this deployment (see sources/liquidations.py), so without
this the largest venue's liquidations never reach the store at all. OKX keeps its own
direct feed (sources/okx_liquidations.py) and is not requested here, so no venue is
counted twice. Hyperliquid is listed by Coinalyze but returned no liquidations when
measured, so it isn't requested either.

What arrives is a per-minute total per side, not individual orders: one Observation per
minute and side, in USD, with the sign convention of sources/liquidations.py (positive =
shorts liquidated by forced buys, negative = longs liquidated by forced sells). A minute
is stored only once it has closed plus SETTLE_SECONDS, stamped at its last millisecond,
so "minutes since the last print" can never read a minute as quieter than it was.

Coinalyze is a second-hand (T2) aggregator of the exchanges' own feeds, which are
themselves throttled samples (doctrine, Measurement rules): like OKX's feed this informs
the trapped-cohort classification and the print-settled condition, never a decision on
its own. It keeps only ~1,500-2,000 intraday points per series (about a day of minutes),
so history finer than daily exists only if collected as it happens.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import httpx

from backend.core.config import Config
from backend.core.observation import Metric, Observation, Tier, Unit
from backend.sources.base import SourceError, to_decimal

API_BASE = "https://api.coinalyze.net/v1"
API_KEY_ENV = "COINALYZE_API_KEY"
SOURCE_ID = "coinalyze"  # registered in config/sources.yaml
MARKETS = {"binance": "{base}USDT_PERP.A", "bybit": "{base}USDT.6"}
MAX_SYMBOLS_PER_REQUEST = 20  # the API's own cap; each symbol costs one call of 40/min
# How long after a minute closes before it's read as final, so a total still being
# filled in on Coinalyze's side is never stored as that minute's whole.
SETTLE_SECONDS = 120


def api_key() -> str | None:
    return os.environ.get(API_KEY_ENV) or None


def symbols_for(instruments: list[str]) -> dict[str, tuple[str, str]]:
    """Coinalyze symbol -> (instrument, venue). A coin an exchange doesn't list is simply
    absent from the response, so no symbol needs checking in advance."""
    return {
        fmt.format(base=inst.upper()): (inst.upper(), venue)
        for inst in instruments
        for venue, fmt in MARKETS.items()
    }


def to_observations(
    series: list[dict], symbols: dict[str, tuple[str, str]], cfg: Config, *, now: datetime
) -> list[Observation]:
    out: list[Observation] = []
    for s in series:
        if s.get("symbol") not in symbols:
            continue
        instrument, venue = symbols[s["symbol"]]
        for row in s.get("history") or []:
            start = datetime.fromtimestamp(int(row["t"]), tz=timezone.utc)
            end = start + timedelta(minutes=1) - timedelta(milliseconds=1)
            if end > now - timedelta(seconds=SETTLE_SECONDS):
                continue  # still open, or too recent to be final
            for field, sign in (("s", 1), ("l", -1)):
                usd = to_decimal(row.get(field, 0), field=field)
                if usd > 0:
                    out.append(
                        Observation.build(
                            metric=Metric.LIQUIDATION,
                            instrument=instrument,
                            value=usd * sign,
                            unit=Unit.USD,
                            venue=venue,
                            source_id=SOURCE_ID,
                            tier=Tier.T2,
                            observed_at=end,
                            cfg=cfg,
                            raw={"symbol": s["symbol"], "t": row["t"], field: row.get(field)},
                        )
                    )
    return sorted(out, key=lambda o: o.observed_at)


async def fetch_since(
    instruments: list[str], cfg: Config, *, client: httpx.AsyncClient, since: datetime, now: datetime | None = None
) -> list[Observation]:
    """Per-minute liquidations for every instrument's Binance and Bybit perpetual from
    `since` on, oldest first. Overlaps with what's stored are the caller's to dedupe."""
    key = api_key()
    if not key:
        raise SourceError(f"{API_KEY_ENV} is not set")
    now = now or datetime.now(timezone.utc)
    symbols = symbols_for(instruments)
    names = list(symbols)
    out: list[Observation] = []
    for i in range(0, len(names), MAX_SYMBOLS_PER_REQUEST):
        try:
            resp = await client.get(
                f"{API_BASE}/liquidation-history",
                headers={"api_key": key},
                params={
                    "symbols": ",".join(names[i : i + MAX_SYMBOLS_PER_REQUEST]),
                    "interval": "1min",
                    "from": int(since.timestamp()),
                    "to": int(now.timestamp()),
                    "convert_to_usd": "true",
                },
                timeout=20.0,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise SourceError(f"GET coinalyze liquidation-history failed: {exc}") from exc
        payload = resp.json()
        if not isinstance(payload, list):
            raise SourceError(f"coinalyze liquidation-history returned no list: {payload}")
        out.extend(to_observations(payload, symbols, cfg, now=now))
    return sorted(out, key=lambda o: o.observed_at)
