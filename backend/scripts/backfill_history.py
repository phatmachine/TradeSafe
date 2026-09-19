"""Backfill of history from Binance's public futures endpoints, so nothing has to wait
weeks after a fresh collector start. This is real historical exchange data, fetched in
bulk after the fact rather than waited for one poll at a time — not a synthetic or
inferred value, and not a threshold change.

- Price (klines): realised_vol_in_band (Gate U condition 6 — needs ~30 days of
  single-venue daily closes) and regime classification (Layer 2.1 — needs enough 4h bars
  to confirm swing structure). Fills the whole window on a first run and, after that,
  any 4h bar the collector wasn't running for: a missing bar shifts which closes count
  as swing points, and six missing bars were enough to flip ZEC's regime.
- Coin open interest (openInterestHist, added 2026-09-18): the OI-vs-price factor, the
  cascade/squeeze flush check, trend continuation's "OI falls during the retrace", and
  the calibration sweep. Binance serves only the latest 30 days of this endpoint, so a
  first run gets 30 days and nothing older is ever available for free.

Written under a distinct source_id (binance_futures_backfill) rather than
binance_futures, so a report's data-integrity section can always tell backfilled history
apart from live-polled observations. Shares the "binance" venue label with the live
Binance futures feed, though, so backend/gates/gate_u.py's single-best-venue selection
naturally stitches backfilled history and live prints into one continuous series.

Only Binance history exists for OI, while live collection sums four venues. Every
consumer reads OI through compute/oi.aggregate_oi_series, which holds the venue set fixed
across the span it compares, so the Binance-only past is never read as positions opening
when the other venues' live readings begin.

Usage: python -m backend.scripts.backfill_history BTC ETH ZEC
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import httpx

from backend.core.config import Config, load_config
from backend.core.observation import Metric, Observation, Tier, Unit, half_life_seconds
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
BAR_SECONDS = 4 * 3600
BARS_PER_DAY = 6
LIMIT = DAYS * BARS_PER_DAY


def _bar(at: datetime) -> int:
    return int(at.timestamp() // BAR_SECONDS)


def _missing_bars(conn, instrument: str, now: datetime) -> set[int]:
    """Closed 4h bars inside the window with no Binance price reading at all: the whole
    window on a first run, afterwards any stretch the collector wasn't running for.
    Compaction keeps a reading per 15 minutes, so a bar the collector covered never reads
    as missing, and repeat calls cost one indexed query with no request."""
    since = now - timedelta(days=DAYS)
    hours = conn.execute(
        """SELECT DISTINCT substr(observed_at, 1, 13) FROM observations
           WHERE instrument = ? AND metric = ? AND venue = ? AND observed_at >= ?""",
        (instrument, Metric.PRICE.value, VENUE, since.isoformat()),
    ).fetchall()
    have = {_bar(datetime.fromisoformat(f"{h[0]}:00:00+00:00")) for h in hours}
    first = _bar(since) + 1  # the first bar wholly inside the window
    earliest = conn.execute(
        "SELECT MIN(observed_at) FROM observations WHERE instrument = ? AND metric = ? AND source_id = ?",
        (instrument, Metric.PRICE.value, SOURCE_ID),
    ).fetchone()[0]
    if earliest:
        # Bars older than Binance's own first one (a recent listing) can never be filled,
        # so they must not count as missing or every call would re-request them.
        first = max(first, _bar(datetime.fromisoformat(earliest)))
    return {b for b in range(first, _bar(now)) if b not in have}


def _drop_unclosed_bars(conn, instrument: str) -> None:
    """Earlier versions also stored the bar still forming at fetch time, stamped with its
    future close time but holding only the price at that moment. Stamped later than any
    live tick in its bar, it permanently stood in for that bar's real close."""
    params = (instrument, Metric.PRICE.value, SOURCE_ID)
    where = "instrument = ? AND metric = ? AND source_id = ? AND observed_at > collected_at"
    if conn.execute(f"SELECT 1 FROM observations WHERE {where} LIMIT 1", params).fetchone():
        conn.execute(f"DELETE FROM observations WHERE {where}", params)


def backfill(instrument: str, cfg: Config, *, client: httpx.Client) -> int:
    instrument = instrument.upper()
    sym = default_usdt_symbols(instrument)
    now = datetime.now(timezone.utc)

    with db.get_connection() as conn:
        _drop_unclosed_bars(conn, instrument)
        missing = _missing_bars(conn, instrument, now)
    if not missing:
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
            closes_at = parse_ms_timestamp(close_time_ms)
            if closes_at >= now or _bar(closes_at) not in missing:
                continue  # still forming, or a bar the collector already covers
            obs = Observation.build(
                metric=Metric.PRICE,
                instrument=instrument,
                value=to_decimal(close_price, field="close"),
                unit=Unit.USD,
                venue=VENUE,
                source_id=SOURCE_ID,
                tier=Tier.T1,
                observed_at=closes_at,
                cfg=cfg,
                raw={"kline": row},
            )
            db.insert_observation(conn, obs)
            inserted += 1
    return inserted


OI_PERIOD = "15m"
OI_PERIOD_SECONDS = 900
OI_PAGE = 499  # periods per request; the endpoint caps limit at 500
# Binance rejects startTime older than 30 days; stay a little inside it.
OI_HISTORY = timedelta(days=29, hours=23)


def _existing_oi_backfill(conn, instrument: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM observations WHERE instrument = ? AND metric = ? AND source_id = ? LIMIT 1",
        (instrument, Metric.OI_COIN.value, SOURCE_ID),
    ).fetchone()
    return row is not None


def backfill_open_interest(instrument: str, cfg: Config, *, client: httpx.Client) -> int:
    """One-off per instrument, like the price backfill: skipped entirely once any
    backfilled OI exists, so repeat calls cost one indexed lookup. Stops short of now by
    OI's half-life plus a margin, so every row is already expired when written — backfill
    can only ever feed history-based reads (observations_including_expired) and never
    reaches a gate as a current reading. The live collector owns "now"."""
    instrument = instrument.upper()
    with db.get_connection() as conn:
        if _existing_oi_backfill(conn, instrument):
            return 0

    sym = default_usdt_symbols(instrument)
    now = datetime.now(timezone.utc)
    end = now - timedelta(seconds=half_life_seconds(Metric.OI_COIN, cfg) + 300)
    cursor = now - OI_HISTORY
    rows: dict[int, dict] = {}
    while cursor < end:
        window_end = min(cursor + timedelta(seconds=OI_PERIOD_SECONDS * OI_PAGE), end)
        try:
            resp = client.get(
                f"{FUTURES_BASE}/futures/data/openInterestHist",
                params={
                    "symbol": sym.futures,
                    "period": OI_PERIOD,
                    "limit": OI_PAGE + 1,
                    "startTime": int(cursor.timestamp() * 1000),
                    "endTime": int(window_end.timestamp() * 1000),
                },
                timeout=10.0,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise SourceError(f"GET openInterestHist failed for {sym.futures}: {exc}") from exc
        page = resp.json()
        if not isinstance(page, list):
            raise SourceError(f"binance openInterestHist returned no list for {sym.futures}: {page}")
        for row in page:
            rows[int(row["timestamp"])] = row  # keyed by time: page edges can overlap
        cursor = window_end

    inserted = 0
    with db.get_connection() as conn:
        for ts, row in sorted(rows.items()):
            observed_at = parse_ms_timestamp(ts)
            if observed_at > end:
                continue
            db.insert_observation(
                conn,
                Observation.build(
                    metric=Metric.OI_COIN,
                    instrument=instrument,
                    value=to_decimal(row.get("sumOpenInterest"), field="sumOpenInterest"),
                    unit=Unit.COINS,
                    venue=VENUE,
                    source_id=SOURCE_ID,
                    tier=Tier.T1,
                    observed_at=observed_at,
                    cfg=cfg,
                    raw={"openInterestHist": row},
                ),
            )
            inserted += 1
    return inserted


def bootstrap(instrument: str, cfg: Config) -> None:
    """Best-effort: both backfills, with a venue failure left for the next call to retry.
    Called before every analysis (on-demand report and scheduled scan) — each backfill
    checks what's stored before making any request, so this is cheap when nothing is
    missing."""
    with httpx.Client() as client:
        for fn in (backfill, backfill_open_interest):
            try:
                fn(instrument, cfg, client=client)
            except SourceError:
                pass


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python -m backend.scripts.backfill_history <SYMBOL> [SYMBOL ...]", file=sys.stderr)
        raise SystemExit(1)
    db.init_db()
    cfg = load_config()
    with httpx.Client() as client:
        for symbol in sys.argv[1:]:
            for label, fn in ((f"{INTERVAL} closes", backfill), (f"{OI_PERIOD} open interest readings", backfill_open_interest)):
                try:
                    n = fn(symbol, cfg, client=client)
                except SourceError as exc:
                    print(f"{symbol.upper()}: {label} backfill failed: {exc}", file=sys.stderr)
                    continue
                if n == 0:
                    print(f"{symbol.upper()}: {label} already complete, skipped")
                else:
                    print(f"{symbol.upper()}: inserted {n} historical {label}")


if __name__ == "__main__":
    main()
