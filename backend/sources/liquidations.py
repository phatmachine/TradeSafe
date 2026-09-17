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

STREAM_URL_TMPL = "wss://fstream.binance.com/ws/{symbol_lower}@forceOrder"
VENUE = "binance"
SOURCE_ID = "binance_futures"


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


async def listen_one(instrument: str, cfg: Config, *, stop_event: asyncio.Event) -> None:
    """Runs until stop_event is set, reconnecting on any failure. Every failure is
    recorded as a collector_event (feeds the Layer 6 source-reliability register)."""
    symbol_lower = f"{instrument.upper()}USDT".lower()
    url = STREAM_URL_TMPL.format(symbol_lower=symbol_lower)
    backoff = 1.0
    while not stop_event.is_set():
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                backoff = 1.0
                while not stop_event.is_set():
                    raw = await asyncio.wait_for(ws.recv(), timeout=60)
                    msg = json.loads(raw)
                    obs = _to_observation(instrument, msg, cfg)
                    if obs is None:
                        continue
                    with db.get_connection() as conn:
                        db.insert_observation(conn, obs)
        except asyncio.TimeoutError:
            continue
        except Exception as exc:  # noqa: BLE001 - reconnect on anything, this is a long-lived loop
            logger.warning("liquidation stream for %s dropped: %s", instrument, exc)
            with db.get_connection() as conn:
                db.record_collector_event(
                    conn, source_id=SOURCE_ID, venue=VENUE, event_type="ws_disconnect", detail=str(exc)
                )
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            backoff = min(backoff * 2, 60.0)


async def run_liquidation_listeners(instruments: list[str], cfg: Config, *, stop_event: asyncio.Event) -> None:
    tasks = [asyncio.create_task(listen_one(i, cfg, stop_event=stop_event)) for i in instruments]
    try:
        await stop_event.wait()
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
