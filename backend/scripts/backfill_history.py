"""One-time backfill of price history from Binance's public futures klines endpoint, so
realised_vol_in_band (Gate U condition 6 — needs ~30 days of single-venue daily closes)
and regime classification (Layer 2.1 — needs enough 4h bars to confirm swing structure)
don't have to wait a month after a fresh collector start. This is real historical
exchange data, fetched in bulk after the fact rather than waited for one poll at a time —
not a synthetic or inferred value, and not a threshold change.

Written under a distinct source_id (binance_futures_backfill) rather than
binance_futures, so a report's data-integrity section can always tell backfilled history
apart from live-polled observations. Shares the "binance" venue label with the live
Binance futures feed, though, so backend/gates/gate_u.py's single-best-venue selection
naturally stitches backfilled history and live prints into one continuous series.

Usage: python -m backend.scripts.backfill_history BTC ETH ZEC
"""
from __future__ import annotations

import sys

import httpx

from backend.core.config import Config, load_config
from backend.core.observation import Metric, Observation, Tier, Unit
from backend.sources.base import SourceError, default_usdt_symbols, parse_ms_timestamp, to_decimal
from backend.store import db

FUTURES_BASE = "https://fapi.binance.com"
VENUE = "binance"
SOURCE_ID = "binance_futures_backfill"
DAYS = 35

# 4h rather than 1d, because two different consumers read this series and only one of
# them is satisfied by daily closes. compute/volatility.daily_closes() collapses whatever
# it is given to one value per UTC day, so realised vol works either way — but
# compute/regime.classify() resamples to swing_timeframe (4h) and needs 2*swing_lookback
# +2 bars with enough local extrema to find n_swings_required swings. Backfilled daily
# closes resampled to 4h produced 32 sparse bars yielding a single confirmed swing low
# against the 3 required, so BTC and ETH classified UNDETERMINED — which blocks every
# setup — despite both satisfying the trend's volatility and direction conditions.
INTERVAL = "4h"
BARS_PER_DAY = 6
LIMIT = DAYS * BARS_PER_DAY


def _existing_backfill_count(conn, instrument: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM observations WHERE instrument = ? AND source_id = ?",
        (instrument, SOURCE_ID),
    ).fetchone()
    return row[0] if row else 0


def backfill(instrument: str, cfg: Config, *, client: httpx.Client) -> int:
    instrument = instrument.upper()
    sym = default_usdt_symbols(instrument)

    with db.get_connection() as conn:
        if _existing_backfill_count(conn, instrument) >= LIMIT:
            return 0

    try:
        resp = client.get(
            f"{FUTURES_BASE}/fapi/v1/klines",
            params={"symbol": sym.futures, "interval": INTERVAL, "limit": LIMIT},
            timeout=10.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise SourceError(f"GET klines failed for {sym.futures}: {exc}") from exc

    klines = resp.json()
    if not isinstance(klines, list) or not klines:
        raise SourceError(f"binance klines returned no data for {sym.futures}: {klines}")

    inserted = 0
    with db.get_connection() as conn:
        for row in klines:
            close_time_ms, close_price = row[6], row[4]
            obs = Observation.build(
                metric=Metric.PRICE,
                instrument=instrument,
                value=to_decimal(close_price, field="close"),
                unit=Unit.USD,
                venue=VENUE,
                source_id=SOURCE_ID,
                tier=Tier.T1,
                observed_at=parse_ms_timestamp(close_time_ms),
                cfg=cfg,
                raw={"kline": row},
            )
            db.insert_observation(conn, obs)
            inserted += 1
    return inserted


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python -m backend.scripts.backfill_history <SYMBOL> [SYMBOL ...]", file=sys.stderr)
        raise SystemExit(1)
    db.init_db()
    cfg = load_config()
    with httpx.Client() as client:
        for symbol in sys.argv[1:]:
            try:
                n = backfill(symbol, cfg, client=client)
            except SourceError as exc:
                print(f"{symbol.upper()}: backfill failed: {exc}", file=sys.stderr)
                continue
            if n == 0:
                print(f"{symbol.upper()}: already backfilled, skipped")
            else:
                print(f"{symbol.upper()}: inserted {n} historical {INTERVAL} closes")


if __name__ == "__main__":
    main()
