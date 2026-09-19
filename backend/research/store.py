"""The research store: years of free exchange history for offline calibration.

Kept in its own file, apart from the live store, on purpose. The live store's contract is
that every row is an Observation the collector really made; this is bulk history fetched
after the fact, from fewer venues, at 15-minute resolution. Nothing in the live path
(gates, setups, reports) ever opens this file, so none of it can reach a live report.

One table of (instrument, metric, venue, ts) -> value, where ts is epoch seconds. Values
stay as the venue's own decimal strings.
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "store" / "research.db"

# Metric names used in this store (not the live Metric enum: these are raw venue series).
PRICE = "price"              # Binance USDT-M perp 15m close
PERP_VOLUME = "perp_volume"  # Binance perp base-asset volume per 15m bar
SPOT_VOLUME = "spot_volume"  # Binance spot base-asset volume per 15m bar
FUNDING = "funding"          # settled funding rate, percent per settlement
OI = "oi"                    # Bybit linear open interest, coins, 15m snapshots
EVENT = "event"              # scheduled macro events under instrument MACRO, venue = kind, value 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
    instrument TEXT NOT NULL,
    metric TEXT NOT NULL,
    venue TEXT NOT NULL,
    ts INTEGER NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (instrument, metric, venue, ts)
) WITHOUT ROWID;
"""


def path() -> Path:
    return Path(os.environ.get("TRADESAFE_RESEARCH_DB_PATH", str(DEFAULT_PATH)))


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30)
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def write(conn: sqlite3.Connection, instrument: str, metric: str, venue: str, rows: Iterable[tuple[int, str]]) -> int:
    data = [(instrument, metric, venue, int(ts), str(value)) for ts, value in rows]
    conn.executemany("INSERT OR REPLACE INTO series VALUES (?, ?, ?, ?, ?)", data)
    return len(data)


def latest_ts(conn: sqlite3.Connection, instrument: str, metric: str, venue: str) -> int | None:
    row = conn.execute(
        "SELECT MAX(ts) FROM series WHERE instrument = ? AND metric = ? AND venue = ?", (instrument, metric, venue)
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def load(conn: sqlite3.Connection, instrument: str, metric: str, venue: str) -> list[tuple[int, Decimal]]:
    rows = conn.execute(
        "SELECT ts, value FROM series WHERE instrument = ? AND metric = ? AND venue = ? ORDER BY ts",
        (instrument, metric, venue),
    ).fetchall()
    return [(ts, Decimal(v)) for ts, v in rows]
