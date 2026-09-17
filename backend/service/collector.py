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
import logging
import signal

import httpx

from backend.core.config import Config, load_config
from backend.scripts import backfill_history
from backend.sources import binance, bybit, chain, coinbase, hyperliquid, issuer, kraken, okx
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

POLL_INTERVAL_SECONDS = 60
LIQUIDATION_SUPPORTED_VENUES = {"binance"}  # see sources/liquidations.py


async def collect_once(instrument: str, cfg: Config, *, client: httpx.AsyncClient) -> None:
    for label, fetch_fn in _FETCHERS:
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


async def liquidation_supervisor(stop_event: asyncio.Event) -> None:
    """Restarts the liquidation-listener set whenever the watchlist changes. Only
    Binance is wired up today (see sources/liquidations.py) — other venues' liquidation
    feeds are a documented follow-up, not silently faked."""
    cfg = load_config()
    current_task: asyncio.Task | None = None
    current_set: frozenset[str] = frozenset()
    inner_stop = asyncio.Event()
    while not stop_event.is_set():
        with db.get_connection() as conn:
            instruments = frozenset(db.list_watched_instruments(conn))
        if instruments != current_set:
            if current_task is not None:
                inner_stop.set()
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

    # One-off per instrument, and cheap to repeat on every restart (backfill() checks
    # existing row counts before making a network call) — see backfill_history's
    # docstring for why this can't just be "wait a month" on a fresh deployment.
    cfg = load_config()
    with httpx.Client() as backfill_client:
        for sym in instruments:
            try:
                n = backfill_history.backfill(sym, cfg, client=backfill_client)
            except SourceError as exc:
                logger.info("collector: history backfill failed for %s: %s", sym, exc)
                continue
            if n:
                logger.info("collector: backfilled %d historical daily closes for %s", n, sym)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib_suppress():
            loop.add_signal_handler(sig, stop_event.set)

    await asyncio.gather(poll_loop(stop_event), liquidation_supervisor(stop_event))


class contextlib_suppress:
    """add_signal_handler is unavailable on some platforms (e.g. Windows) — degrade to
    Ctrl-C only rather than crashing the collector on import."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is NotImplementedError


if __name__ == "__main__":
    asyncio.run(main())
