"""The always-on collector (implementation spec, "Two services, two lifecycles"). Polls
venues for every watched instrument and only ever writes Observation rows — it holds no
decision logic at all, never calls a gate, and never fetches on behalf of the analysis
API (which reads only from the store). Ships before any gate is written and runs
continuously from then on, because venue history is short-lived (spec, "Collector-first
sequencing") — a week the collector isn't running is calibration data that never comes
back.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import signal
from datetime import datetime, timedelta, timezone

import httpx

from backend.core.config import Config, load_config
from backend.core.observation import Metric
from backend.scripts import backfill_history
from backend.sources import binance, bybit, chain, coinalyze, coinbase, hyperliquid, issuer, kraken, okx, okx_liquidations
from backend.sources.base import SourceError
from backend.sources.liquidations import run_liquidation_listeners
from backend.store import db

logger = logging.getLogger("tradesafe.collector")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# venue label used for collector_events / source-registry bookkeeping when a fetch call
# fails before we know which specific source_id inside it was responsible.
_FETCHERS = [
    ("binance", binance.fetch),
    ("bybit", bybit.fetch),
    ("okx", okx.fetch),
    ("hyperliquid", hyperliquid.fetch),
    ("coinbase", coinbase.fetch),
    ("kraken", kraken.fetch),
    ("chain", chain.fetch),
    ("issuer", issuer.fetch),
]

# The subset polled on the fast loop below, to keep PRICE (30s half-life) and
# ORDER_BOOK_DEPTH (15s) inside their own expiry windows — the full loop's 60s cadence
# is slower than those metrics decay, which left spot_depth_sufficient reading `unknown`
# on most on-demand reports (doctrine 0.2 expiry is a property of the market, so the
# cadence is what has to move, not the half-life). These three venues carry both metrics
# and have rate limits generous enough for it; CoinGecko/Kraken deliberately stay on the
# slow loop, where their tighter limits are not a problem.
_FAST_FETCHERS = [
    ("binance", binance.fetch),
    ("bybit", bybit.fetch),
    ("okx", okx.fetch),
]

POLL_INTERVAL_SECONDS = 60
FAST_POLL_INTERVAL_SECONDS = 10
# 15s keeps a busy tape inside one or two pages per poll (ZEC, the busiest measured,
# averages ~2 prints/min) while fetch_since pages further back if a cascade outruns it.
LIQUIDATION_POLL_INTERVAL_SECONDS = 15
# binance: websocket (sources/liquidations.py) — frames don't flow in this environment
# okx: REST poll (sources/okx_liquidations.py) — the one that actually delivers here
LIQUIDATION_SUPPORTED_VENUES = {"binance", "okx"}
# binance + bybit, per minute, via Coinalyze (sources/coinalyze.py). Each symbol costs one
# call of the free tier's 40 a minute; the poll stretches to stay under this many.
COINALYZE_CALLS_PER_MINUTE = 30
# About what Coinalyze keeps at 1-minute granularity: a cold start fills this much.
COINALYZE_LOOKBACK = timedelta(hours=24)
COINALYZE_OVERLAP = timedelta(minutes=10)

# Compaction (store.db.compact_observations): rows older than COMPACT_AFTER are thinned
# to one per 15 minutes per series, hourly. Without it every 10-second poll is kept for
# good, and since each report reads up to 30 days of history, report time and file size
# grow without bound — 5-7s per report after just 8 hours of the fast loop. Two hours
# comfortably outlasts every short half-life (the longest, OI/funding/volume, is 1h), and
# nothing reads history at finer than 15 minutes.
COMPACT_AFTER = timedelta(hours=2)
COMPACTION_INTERVAL_SECONDS = 3600
COMPACTION_RECENT_SPAN = timedelta(days=2)  # after the first pass, only recent rows need it
# VACUUM after the first pass (reclaims everything that pass freed) and then daily. Freed
# pages are reused by new rows in between, so the file stays bounded either way; the
# daily rewrite just keeps surviving rows contiguous so history reads stay fast.
VACUUM_INTERVAL = timedelta(days=1)


async def collect_once(
    instrument: str,
    cfg: Config,
    *,
    client: httpx.AsyncClient,
    fetchers: list | None = None,
) -> None:
    for label, fetch_fn in fetchers if fetchers is not None else _FETCHERS:
        try:
            observations = await fetch_fn(instrument, cfg, client=client)
        except SourceError as exc:
            logger.info("collector: %s/%s failed: %s", label, instrument, exc)
            with db.get_connection() as conn:
                db.record_collector_event(conn, source_id=label, venue=label, event_type="fetch_failed", detail=str(exc))
                db.record_source_failure(conn, label)
            continue
        except Exception as exc:  # noqa: BLE001 - never let one venue's bug stop the loop
            logger.exception("collector: unexpected error in %s/%s", label, instrument)
            with db.get_connection() as conn:
                db.record_collector_event(conn, source_id=label, venue=label, event_type="unexpected_error", detail=str(exc))
            continue

        if not observations:
            continue
        with db.get_connection() as conn:
            for obs in observations:
                db.insert_observation(conn, obs)
            for source_id in {o.source_id for o in observations}:
                db.record_source_success(conn, source_id)


async def poll_loop(stop_event: asyncio.Event) -> None:
    cfg = load_config()
    async with httpx.AsyncClient() as client:
        while not stop_event.is_set():
            with db.get_connection() as conn:
                instruments = db.list_watched_instruments(conn)
            for instrument in instruments:
                await collect_once(instrument, cfg, client=client)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass


async def fast_poll_loop(stop_event: asyncio.Event) -> None:
    """Keeps the short-half-life metrics fresh. Instruments are fetched concurrently
    rather than in sequence so one cycle finishes well inside ORDER_BOOK_DEPTH's 15s
    expiry even with a full watchlist — done sequentially the cycle alone would outlast
    the window it exists to stay inside."""
    cfg = load_config()
    async with httpx.AsyncClient() as client:
        while not stop_event.is_set():
            with db.get_connection() as conn:
                instruments = db.list_watched_instruments(conn)
            await asyncio.gather(
                *(
                    collect_once(instrument, cfg, client=client, fetchers=_FAST_FETCHERS)
                    for instrument in instruments
                )
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=FAST_POLL_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass


async def collect_okx_liquidations(instrument: str, cfg: Config, *, client: httpx.AsyncClient) -> int:
    """One poll of OKX's liquidation history for one instrument. Pages overlap from one
    poll to the next, so every row is checked against what is already stored before it
    is inserted. Returns the number of new prints written."""
    source_id = okx_liquidations.SOURCE_ID
    with db.get_connection() as conn:
        since = db.latest_observed_at(conn, instrument=instrument, metric=Metric.LIQUIDATION, source_id=source_id)
    observations = await okx_liquidations.fetch_since(instrument, cfg, client=client, since=since)
    if not observations:
        return 0
    with db.get_connection() as conn:
        seen = (
            db.observation_keys_since(
                conn, instrument=instrument, metric=Metric.LIQUIDATION, source_id=source_id, since=since
            )
            if since is not None
            else set()
        )
        new = [o for o in observations if (o.observed_at.isoformat(), str(o.value)) not in seen]
        for obs in new:
            db.insert_observation(conn, obs)
        db.record_source_success(conn, source_id)
    return len(new)


async def okx_liquidation_poll_loop(stop_event: asyncio.Event) -> None:
    """Failures are logged as collector_events but deliberately NOT counted against the
    okx_futures reliability register: that source_id also carries OKX's price, funding
    and OI, and a flaky liquidation endpoint must not get those demoted."""
    cfg = load_config()
    async with httpx.AsyncClient() as client:
        while not stop_event.is_set():
            with db.get_connection() as conn:
                instruments = db.list_watched_instruments(conn)
            for instrument in instruments:
                try:
                    n = await collect_okx_liquidations(instrument, cfg, client=client)
                    if n:
                        logger.debug("collector: %d new okx liquidation prints for %s", n, instrument)
                except SourceError as exc:
                    logger.info("collector: okx liquidations/%s failed: %s", instrument, exc)
                    with db.get_connection() as conn:
                        db.record_collector_event(
                            conn, source_id=okx_liquidations.SOURCE_ID, venue="okx", event_type="fetch_failed", detail=str(exc)
                        )
                except Exception as exc:  # noqa: BLE001 - never let one instrument stop the loop
                    logger.exception("collector: unexpected error in okx liquidations/%s", instrument)
                    with db.get_connection() as conn:
                        db.record_collector_event(
                            conn, source_id=okx_liquidations.SOURCE_ID, venue="okx", event_type="unexpected_error", detail=str(exc)
                        )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=LIQUIDATION_POLL_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass


async def collect_coinalyze_liquidations(
    instruments: list[str], cfg: Config, *, client: httpx.AsyncClient, now: datetime | None = None
) -> int:
    """One poll of Binance and Bybit per-minute liquidations for the whole watchlist.
    Starts a little before the oldest instrument's newest stored minute (a day back for
    one never collected), and stores each minute and side once: a later revision of a
    minute already stored is not added on top of it. Returns the rows written."""
    source_id = coinalyze.SOURCE_ID
    now = now or datetime.now(timezone.utc)
    floor = now - COINALYZE_LOOKBACK
    with db.get_connection() as conn:
        latest = [
            db.latest_observed_at(conn, instrument=i, metric=Metric.LIQUIDATION, source_id=source_id)
            for i in instruments
        ]
    since = max(floor, min((t or floor) for t in latest) - COINALYZE_OVERLAP) if latest else floor
    observations = await coinalyze.fetch_since(instruments, cfg, client=client, since=since, now=now)
    if not observations:
        return 0
    written = 0
    with db.get_connection() as conn:
        for (instrument, venue) in {(o.instrument, o.venue) for o in observations}:
            seen = {
                (at, value.startswith("-"))
                for at, value in db.observation_keys_since(
                    conn, instrument=instrument, metric=Metric.LIQUIDATION, source_id=source_id, since=since, venue=venue
                )
            }
            for obs in observations:
                if obs.instrument != instrument or obs.venue != venue:
                    continue
                key = (obs.observed_at.isoformat(), obs.value < 0)
                if key not in seen:
                    db.insert_observation(conn, obs)
                    seen.add(key)
                    written += 1
        db.record_source_success(conn, source_id)
    return written


async def coinalyze_liquidation_poll_loop(stop_event: asyncio.Event) -> None:
    """Off entirely without an API key. Failures are logged as collector_events, not
    counted against the source's reliability register (same reasoning as OKX's loop)."""
    if not coinalyze.api_key():
        logger.info("collector: %s not set, so Binance/Bybit liquidations are not collected", coinalyze.API_KEY_ENV)
        return
    cfg = load_config()
    async with httpx.AsyncClient() as client:
        while not stop_event.is_set():
            with db.get_connection() as conn:
                instruments = db.list_watched_instruments(conn)
            try:
                n = await collect_coinalyze_liquidations(instruments, cfg, client=client)
                if n:
                    logger.debug("collector: %d new coinalyze liquidation minutes", n)
            except SourceError as exc:
                logger.info("collector: coinalyze liquidations failed: %s", exc)
                with db.get_connection() as conn:
                    db.record_collector_event(
                        conn, source_id=coinalyze.SOURCE_ID, venue="coinalyze", event_type="fetch_failed", detail=str(exc)
                    )
            except Exception as exc:  # noqa: BLE001 - never let a bad payload stop the loop
                logger.exception("collector: unexpected error in coinalyze liquidations")
                with db.get_connection() as conn:
                    db.record_collector_event(
                        conn, source_id=coinalyze.SOURCE_ID, venue="coinalyze", event_type="unexpected_error", detail=str(exc)
                    )
            calls = len(coinalyze.symbols_for(instruments))
            interval = 60 * max(1, math.ceil(calls / COINALYZE_CALLS_PER_MINUTE))
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass


def _floor_to_bucket(at: datetime) -> datetime:
    bucket = db.COMPACTION_BUCKET_SECONDS
    return datetime.fromtimestamp(int(at.timestamp()) // bucket * bucket, tz=timezone.utc)


def compact_once(*, full: bool) -> int:
    """One compaction pass over every watched instrument, one day per transaction so the
    write lock is never held long enough to stall the 10-second poll. `full` covers
    everything stored (the first pass after startup); otherwise only the last two days,
    which is all an hourly pass can have left uncompacted."""
    now = datetime.now(timezone.utc)
    until = _floor_to_bucket(now - COMPACT_AFTER)
    removed = 0
    with db.get_connection() as conn:
        instruments = db.list_watched_instruments(conn)
    for instrument in instruments:
        if full:
            with db.get_connection() as conn:
                earliest = db.earliest_observed_at(conn, instrument=instrument)
            if earliest is None:
                continue
            start = _floor_to_bucket(earliest)
        else:
            start = _floor_to_bucket(until - COMPACTION_RECENT_SPAN)
        while start < until:
            end = min(start + timedelta(days=1), until)
            with db.get_connection() as conn:
                removed += db.compact_observations(conn, instrument=instrument, since=start, until=end, now=now)
            start = end
    return removed


async def compaction_loop(stop_event: asyncio.Event) -> None:
    full = True
    last_vacuum: datetime | None = None
    while not stop_event.is_set():
        try:
            removed = await asyncio.to_thread(compact_once, full=full)
            full = False
            if removed:
                logger.info("collector: compacted %d superseded observations", removed)
            now = datetime.now(timezone.utc)
            if last_vacuum is None or now - last_vacuum >= VACUUM_INTERVAL:
                await asyncio.to_thread(db.vacuum)
                last_vacuum = now
                logger.info("collector: vacuumed the store")
        except Exception:  # noqa: BLE001 - a failed pass is retried next hour; never stop collection
            logger.exception("collector: compaction pass failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=COMPACTION_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def liquidation_supervisor(stop_event: asyncio.Event) -> None:
    """Restarts the Binance liquidation websocket whenever the watchlist changes (see
    sources/liquidations.py). OKX's REST feed runs separately in
    okx_liquidation_poll_loop, since polling needs no supervisor to follow the list."""
    cfg = load_config()
    current_task: asyncio.Task | None = None
    current_set: frozenset[str] = frozenset()
    inner_stop = asyncio.Event()
    while not stop_event.is_set():
        with db.get_connection() as conn:
            instruments = frozenset(db.list_watched_instruments(conn))
        if instruments != current_set:
            if current_task is not None:
                # Cancel rather than only signalling: the listener spends almost all its
                # time awaiting the next frame, and an Event it can only check between
                # frames would leave this await hanging until the venue happened to send
                # one — which on a quiet tape is indefinitely, so the watchlist change
                # would never reach the stream.
                inner_stop.set()
                current_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await current_task
            inner_stop = asyncio.Event()
            current_set = instruments
            if instruments:
                current_task = asyncio.create_task(run_liquidation_listeners(list(instruments), cfg, stop_event=inner_stop))
            else:
                current_task = None
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass
    if current_task is not None:
        inner_stop.set()
        await current_task


async def main() -> None:
    db.init_db()
    with db.get_connection() as conn:
        if not db.list_watched_instruments(conn):
            # Seed a couple of majors so the store has something the moment it boots;
            # the API adds any instrument a user actually looks up to this list too.
            for sym in ("BTC", "ETH"):
                db.add_watched_instrument(conn, sym)
        instruments = db.list_watched_instruments(conn)

    # Fills a fresh instrument's history and any 4h bars missed while the collector was
    # down, and is cheap to repeat on every restart (each backfill checks what's stored
    # before making a network call) — see backfill_history's docstring for why this can't
    # just be "wait a month" on a fresh deployment.
    cfg = load_config()
    with httpx.Client() as backfill_client:
        for sym in instruments:
            for label, fn in (
                ("closes", backfill_history.backfill),
                ("open interest readings", backfill_history.backfill_open_interest),
            ):
                try:
                    n = fn(sym, cfg, client=backfill_client)
                except SourceError as exc:
                    logger.info("collector: %s backfill failed for %s: %s", label, sym, exc)
                    continue
                if n:
                    logger.info("collector: backfilled %d historical %s for %s", n, label, sym)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib_suppress():
            loop.add_signal_handler(sig, stop_event.set)

    await asyncio.gather(
        poll_loop(stop_event),
        fast_poll_loop(stop_event),
        liquidation_supervisor(stop_event),
        okx_liquidation_poll_loop(stop_event),
        coinalyze_liquidation_poll_loop(stop_event),
        compaction_loop(stop_event),
    )


class contextlib_suppress:
    """add_signal_handler is unavailable on some platforms (e.g. Windows) — degrade to
    Ctrl-C only rather than crashing the collector on import."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is NotImplementedError


if __name__ == "__main__":
    asyncio.run(main())
