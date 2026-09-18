"""Backfills years of free public history into the research store (research/store.py).

What exists for free, measured 2026-09-18, all back to at least 2023-01-01:
- Binance USDT-M perp klines, 15m: price (close) and perp volume
- Binance spot klines, 15m: spot volume
- Binance and Bybit settled funding rates
- Bybit linear open interest, 15m snapshots

What does not, so it can't be calibrated from here: liquidation history (OKX keeps 24h,
everything longer is paid) and order-book depth (no venue publishes it historically).

Every series resumes from its last stored timestamp, so re-running only fetches what's
new. Open interest comes from Bybit alone — Binance serves only 30 days — so anything
calibrated on it uses percentage changes, never levels, which are venue-specific.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import Decimal

import httpx

from backend.research import store

BINANCE_FUTURES = "https://fapi.binance.com"
BINANCE_SPOT = "https://api.binance.com"
BYBIT = "https://api.bybit.com"
BAR_MS = 15 * 60 * 1000
DEFAULT_SINCE = datetime(2023, 1, 1, tzinfo=timezone.utc)


def _get(client: httpx.Client, url: str, params: dict, *, attempts: int = 5):
    """GET with backoff on rate limiting or a transient failure; raises after `attempts`."""
    for attempt in range(attempts):
        try:
            resp = client.get(url, params=params, timeout=20.0)
            if resp.status_code in (418, 429) or resp.status_code >= 500:
                raise httpx.HTTPStatusError("rate limited or server error", request=resp.request, response=resp)
            resp.raise_for_status()
            payload = resp.json()
            if isinstance(payload, dict) and payload.get("retCode") not in (None, 0):
                raise RuntimeError(f"bybit error {payload.get('retCode')}: {payload.get('retMsg')}")
            return payload
        except (httpx.HTTPError, RuntimeError):
            if attempt == attempts - 1:
                raise
            time.sleep(2 ** attempt)


def _start_ms(conn, instrument: str, metric: str, venue: str, since: datetime) -> int:
    last = store.latest_ts(conn, instrument, metric, venue)
    return (last + 1) * 1000 if last is not None else int(since.timestamp() * 1000)


def _klines(client, conn, instrument: str, *, base: str, path: str, limit: int, since: datetime, metrics: dict) -> int:
    """metrics maps a store metric to the kline column it takes ({"price": 4, ...}).
    Stored at the bar's close time so a bar lands in the bucket it closed in, as a live
    poll just before the bucket's end would."""
    symbol = f"{instrument}USDT"
    first_metric = next(iter(metrics))
    start = _start_ms(conn, instrument, first_metric, "binance", since)
    now_ms = int(time.time() * 1000)
    written = 0
    while start < now_ms:
        rows = _get(client, f"{base}{path}", {"symbol": symbol, "interval": "15m", "startTime": start, "limit": limit})
        closed = [r for r in rows if int(r[6]) < now_ms]  # never store the bar still forming
        if not closed:
            break
        for metric, column in metrics.items():
            written += store.write(conn, instrument, metric, "binance", ((int(r[6]) // 1000, r[column]) for r in closed))
        conn.commit()
        start = int(closed[-1][0]) + BAR_MS
        if len(rows) < limit:
            break
    return written


def _binance_funding(client, conn, instrument: str, since: datetime) -> int:
    start = _start_ms(conn, instrument, store.FUNDING, "binance", since)
    written = 0
    while True:
        rows = _get(client, f"{BINANCE_FUTURES}/fapi/v1/fundingRate",
                    {"symbol": f"{instrument}USDT", "startTime": start, "limit": 1000})
        if not rows:
            break
        written += store.write(conn, instrument, store.FUNDING, "binance",
                               ((int(r["fundingTime"]) // 1000, Decimal(r["fundingRate"]) * 100) for r in rows))
        conn.commit()
        start = int(rows[-1]["fundingTime"]) + 1
        if len(rows) < 1000:
            break
    return written


def _bybit_windowed(client, conn, instrument: str, *, metric: str, path: str, params: dict, window_ms: int,
                    since: datetime, parse) -> int:
    """Bybit pages newest-first within [startTime, endTime]; walking fixed windows forward
    (each small enough to fit one page) is simpler and complete."""
    start = _start_ms(conn, instrument, metric, "bybit", since)
    now_ms = int(time.time() * 1000)
    written = 0
    while start < now_ms:
        end = min(start + window_ms - 1, now_ms)
        payload = _get(client, f"{BYBIT}{path}",
                       {"category": "linear", "symbol": f"{instrument}USDT", "startTime": start, "endTime": end,
                        "limit": 200, **params})
        rows = payload["result"]["list"]
        if rows:
            written += store.write(conn, instrument, metric, "bybit", (parse(r) for r in rows))
            conn.commit()
        start = end + 1
        time.sleep(0.05)
    return written


def backfill(instrument: str, *, since: datetime = DEFAULT_SINCE, client: httpx.Client | None = None, log=print) -> dict[str, int]:
    instrument = instrument.upper()
    own_client = client is None
    client = client or httpx.Client()
    counts: dict[str, int] = {}
    try:
        with store.connection() as conn:
            steps = [
                ("binance perp price+volume", lambda: _klines(
                    client, conn, instrument, base=BINANCE_FUTURES, path="/fapi/v1/klines", limit=1500, since=since,
                    metrics={store.PRICE: 4, store.PERP_VOLUME: 5})),
                ("binance spot volume", lambda: _klines(
                    client, conn, instrument, base=BINANCE_SPOT, path="/api/v3/klines", limit=1000, since=since,
                    metrics={store.SPOT_VOLUME: 5})),
                ("binance funding", lambda: _binance_funding(client, conn, instrument, since)),
                ("bybit funding", lambda: _bybit_windowed(
                    client, conn, instrument, metric=store.FUNDING, path="/v5/market/funding/history", params={},
                    window_ms=200 * 3600 * 1000, since=since,  # 200 rows even at hourly funding
                    parse=lambda r: (int(r["fundingRateTimestamp"]) // 1000, Decimal(r["fundingRate"]) * 100))),
                ("bybit open interest", lambda: _bybit_windowed(
                    client, conn, instrument, metric=store.OI, path="/v5/market/open-interest",
                    params={"intervalTime": "15min"}, window_ms=200 * BAR_MS, since=since,
                    parse=lambda r: (int(r["timestamp"]) // 1000, r["openInterest"]))),
            ]
            for label, step in steps:
                t = time.perf_counter()
                counts[label] = step()
                log(f"{instrument}: {label}: +{counts[label]:,} rows ({time.perf_counter() - t:.0f}s)")
    finally:
        if own_client:
            client.close()
    return counts
