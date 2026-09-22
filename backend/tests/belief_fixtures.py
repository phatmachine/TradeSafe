"""Shared fixtures for the belief-data tests: a store that clears Gate U and Layer 0 and
lands in a mean-reverting regime, so a report runs every setup it can (cascade, squeeze,
exhaustion, event) and the firewall tests exercise the whole decision path rather than
stopping at GATE_FAIL; and Stocktwits stream pages in the documented v2 shape.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from backend.core.config import Config
from backend.core.observation import MACRO, Metric, Observation, Tier, Unit
from backend.store import db

INSTRUMENT = "SOL"
BAR_4H = 4 * 3600
# 4h-aligned, so every synthetic close lands in its own bar.
AS_OF = datetime.fromtimestamp((1_789_000_000 // BAR_4H) * BAR_4H + 600, tz=timezone.utc)


def _obs(cfg: Config, metric, value, unit, venue, source_id, at, *, tier=Tier.T1, instrument=INSTRUMENT) -> Observation:
    return Observation.build(
        metric=metric, instrument=instrument, value=Decimal(str(value)), unit=unit, venue=venue,
        source_id=source_id, tier=tier, observed_at=at, collected_at=at, cfg=cfg,
    )


def choppy_closes(n: int = 35 * 6) -> list[Decimal]:
    """A 2-day cycle whose amplitude keeps changing, so swing lows and highs are neither
    rising nor falling in sequence: chop, not trend."""
    amplitudes = [5, 11, 3, 8, 6, 12, 4, 9]
    return [
        Decimal(str(round(100 + amplitudes[(i // 12) % len(amplitudes)] * math.sin(2 * math.pi * i / 12), 4)))
        for i in range(n)
    ]


def fill_decision_store(cfg: Config) -> None:
    """Everything a report reads for INSTRUMENT, as of AS_OF, from two independent venues."""
    rows: list[Observation] = []
    closes = choppy_closes()
    start = AS_OF - timedelta(seconds=BAR_4H * (len(closes) - 1))
    for i, c in enumerate(closes[:-1]):
        rows.append(_obs(cfg, Metric.PRICE, c, Unit.USD, "binance", "binance_futures", start + timedelta(seconds=i * BAR_4H)))
    price = closes[-1]
    for venue in ("binance", "bybit"):
        fut, spot = f"{venue}_futures", f"{venue}_spot"
        rows += [
            _obs(cfg, Metric.PRICE, price, Unit.USD, venue, fut, AS_OF),
            _obs(cfg, Metric.FUNDING_8H, "0.01", Unit.PCT_8H, venue, fut, AS_OF),
            _obs(cfg, Metric.OI_COIN, 1000, Unit.COINS, venue, fut, AS_OF),
            _obs(cfg, Metric.PERP_VOLUME, 1000, Unit.COINS, venue, fut, AS_OF),
            _obs(cfg, Metric.ORDER_BOOK_DEPTH, 1000, Unit.COINS, venue, fut, AS_OF),
            _obs(cfg, Metric.PRICE, price, Unit.USD, venue, spot, AS_OF),
            _obs(cfg, Metric.SPOT_VOLUME, 500, Unit.COINS, venue, spot, AS_OF),
        ]
    rows += [
        _obs(cfg, Metric.SUPPLY_TOTAL, 500_000_000, Unit.COINS, "chain", "chain_evm_generic", AS_OF),
        _obs(cfg, Metric.MARKET_CAP, 50_000_000_000, Unit.USD, "coingecko", "coingecko", AS_OF, tier=Tier.T2),
        _obs(cfg, Metric.LIQUIDATION, "-25000", Unit.USD, "okx", "okx_futures", AS_OF - timedelta(hours=5)),
    ]
    with db.get_connection() as conn:
        for o in rows:
            db.insert_observation(conn, o)
        db.insert_observation(conn, Observation.build(
            metric=Metric.EVENT, instrument=MACRO, value=Decimal(1), unit=Unit.COUNT, venue="us_cpi",
            source_id="fred", tier=Tier.T1, observed_at=AS_OF - timedelta(hours=6),
            collected_at=AS_OF - timedelta(hours=6), cfg=cfg, raw={"kind": "us_cpi"},
        ))


def _ts(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def stream_page(
    *,
    newest: datetime,
    tags: list[str | None],
    bodies: list[str] | None = None,
    spacing_minutes: int = 3,
    symbol: str = "SOL.X",
    watchlist_count: int | None = 12345,
) -> bytes:
    """A symbol-stream page in the documented v2 shape, newest message first. Built from
    documentation, not captured: no live response could be read from this deployment."""
    bodies = bodies or [f"${symbol} message {i}" for i in range(len(tags))]
    messages = []
    for i, (tag, text) in enumerate(zip(tags, bodies)):
        m = {
            "id": 664776885 - i,
            "body": text,
            "created_at": _ts(newest - timedelta(minutes=spacing_minutes * i)),
            "user": {"id": 1000 + i, "username": f"user{i}", "name": f"User {i}"},
            "source": {"id": 1, "title": "StockTwits Web"},
            "symbols": [{"id": 13676, "symbol": symbol, "title": "Solana"}],
            "entities": {"sentiment": {"basic": tag} if tag else None},
        }
        messages.append(m)
    payload: dict = {
        "response": {"status": 200},
        "cursor": {"more": True, "since": messages[0]["id"] if messages else None, "max": messages[-1]["id"] if messages else None},
        "messages": messages,
    }
    symbol_obj = {"id": 13676, "symbol": symbol, "title": "Solana"}
    if watchlist_count is not None:
        symbol_obj["watchlist_count"] = watchlist_count
    payload["symbol"] = symbol_obj
    return json.dumps(payload).encode()
