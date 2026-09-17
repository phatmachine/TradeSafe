"""Completed liquidation events (doctrine Layer 1: "completed liquidation events, with
dominant side"). The doctrine itself warns that reported liquidation totals are samples,
not censuses — Binance's own public forceOrder stream throttles to roughly one event per
second per symbol under load (implementation spec, "Measurement rules"). This listener
records exactly what the stream sends, unmodified, and tags every row with the venue and
source so Layer 0 can weigh it as a sample rather than a census. It is a supplementary
signal; cascade-absorption completion is decided from coin-OI delta (not throttled), per
the doctrine's own substitution rule — never from this feed alone.

Side convention: a forced SELL order liquidates a long position; a forced BUY order
liquidates a short position. Net liquidation "value" stored here is positive for a short
liquidated (BUY) and negative for a long liquidated (SELL) — compute/cohort.py documents
and owns the sign convention consumed downstream.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal

import websockets

from backend.core.config import Config
from backend.core.observation import Metric, Observation, Tier, Unit
from backend.store import db

logger = logging.getLogger("tradesafe.liquidations")

# The all-market stream rather than one {symbol}@forceOrder connection per instrument.
# Per-symbol forceOrder is sparse — a quiet symbol can go many minutes between prints —
# which combined with the old 60s recv timeout meant the listener spent its life
# reconnecting and missed anything that landed mid-reconnect. One all-market connection
# receives every liquidation on the venue, so the stream is continuously live and
# liveness is provable; the payload's own symbol field is matched against the watchlist.
STREAM_URL = "wss://fstream.binance.com/ws/!forceOrder@arr"
VENUE = "binance"
SOURCE_ID = "binance_futures"
QUOTE_SUFFIX = "USDT"  # matches sources/base.default_usdt_symbols


def _to_observation(instrument: str, msg: dict, cfg: Config) -> Observation | None:
    order = msg.get("o")
    if not order:
        return None
    side = order.get("S")
    qty = order.get("q")
    price = order.get("ap") or order.get("p")
    ts = order.get("T")
    if side not in ("BUY", "SELL") or qty is None or price is None or ts is None:
        return None
    notional = Decimal(str(qty)) * Decimal(str(price))
    signed = notional if side == "BUY" else -notional
    observed_at = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)
    return Observation.build(
        metric=Metric.LIQUIDATION,
        instrument=instrument,
        value=signed,
        unit=Unit.USD,
        venue=VENUE,
        source_id=SOURCE_ID,
        tier=Tier.T1,
        observed_at=observed_at,
        cfg=cfg,
        raw=order,
    )


def instrument_for_symbol(symbol: str | None, watched: set[str]) -> str | None:
    """Map a venue symbol from the all-market stream back to a watched instrument.
    Only USDT-quoted symbols are accepted, so a coin-margined or alt-quoted contract on
    the same stream is never silently attributed to the USDT instrument we track."""
    if not symbol:
        return None
    symbol = symbol.upper()
    if not symbol.endswith(QUOTE_SUFFIX):
        return None
    base = symbol[: -len(QUOTE_SUFFIX)]
    return base if base in watched else None


async def run_liquidation_listeners(instruments: list[str], cfg: Config, *, stop_event: asyncio.Event) -> None:
    """Runs until stop_event is set, reconnecting with backoff on any failure. Every
    failure is recorded as a collector_event (feeds the Layer 6 source-reliability
    register). Silence is deliberately NOT treated as failure — connection liveness is
    websockets' ping/pong job (ping_interval/ping_timeout below), whereas a gap in
    liquidations is real information about the tape.
    """
    watched = {i.upper() for i in instruments}
    backoff = 1.0
    while not stop_event.is_set():
        try:
            async with websockets.connect(STREAM_URL, ping_interval=20, ping_timeout=20) as ws:
                backoff = 1.0
                logger.info("liquidation stream connected, watching %s", sorted(watched))
                while not stop_event.is_set():
                    raw = await ws.recv()
                    msg = json.loads(raw)
                    instrument = instrument_for_symbol((msg.get("o") or {}).get("s"), watched)
                    if instrument is None:
                        continue
                    obs = _to_observation(instrument, msg, cfg)
                    if obs is None:
                        continue
                    with db.get_connection() as conn:
                        db.insert_observation(conn, obs)
                        db.record_source_success(conn, SOURCE_ID)
        except Exception as exc:  # noqa: BLE001 - reconnect on anything, this is a long-lived loop
            logger.warning("liquidation stream dropped: %s", exc)
            with db.get_connection() as conn:
                db.record_collector_event(
                    conn, source_id=SOURCE_ID, venue=VENUE, event_type="ws_disconnect", detail=str(exc)
                )
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            backoff = min(backoff * 2, 60.0)
