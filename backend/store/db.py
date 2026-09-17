"""SQLite persistence. This is the only place SQL lives. Storage is trivial at this
cadence (implementation spec, "Host requirements") so a single file is enough — no
credentials, no exchange-writable state, nothing worth stealing on the box.

The analysis path (gates/compute/setups) never talks to this module directly; it goes
through backend/replay/source.py's DataSource interface, so live and replay share code
(implementation spec, "Same code path").
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from backend.core.observation import Metric, Observation, Tier, Unit

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "records.db"


def db_path() -> Path:
    return Path(os.environ.get("TRADESAFE_DB_PATH", str(DEFAULT_DB_PATH)))


SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric TEXT NOT NULL,
    instrument TEXT NOT NULL,
    value TEXT NOT NULL,
    unit TEXT NOT NULL,
    venue TEXT NOT NULL,
    source_id TEXT NOT NULL,
    tier TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    raw TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_obs_lookup
    ON observations (instrument, metric, observed_at);

CREATE TABLE IF NOT EXISTS source_state (
    source_id TEXT PRIMARY KEY,
    reliability_prior REAL NOT NULL DEFAULT 1.0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    demoted_at TEXT
);

CREATE TABLE IF NOT EXISTS collector_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    venue TEXT NOT NULL,
    event_type TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decision_records (
    run_id TEXT PRIMARY KEY,
    instrument TEXT NOT NULL,
    run_at TEXT NOT NULL,
    observation_ids TEXT NOT NULL,
    gate_results TEXT NOT NULL,
    classification TEXT NOT NULL,
    setups_evaluated TEXT NOT NULL,
    verdict TEXT NOT NULL,
    config_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decision_instrument
    ON decision_records (instrument, run_at);

CREATE TABLE IF NOT EXISTS watched_instruments (
    instrument TEXT PRIMARY KEY,
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    position_id TEXT PRIMARY KEY,
    instrument TEXT NOT NULL,
    setup TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    entry_evidence TEXT NOT NULL,
    trapped_cohort TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    closed_at TEXT
);
"""


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA)


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def insert_observation(conn: sqlite3.Connection, obs: Observation) -> int:
    cur = conn.execute(
        """INSERT INTO observations
           (metric, instrument, value, unit, venue, source_id, tier,
            collected_at, observed_at, expires_at, raw)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            obs.metric.value,
            obs.instrument,
            str(obs.value),
            obs.unit.value,
            obs.venue,
            obs.source_id,
            obs.tier.value,
            _iso(obs.collected_at),
            _iso(obs.observed_at),
            _iso(obs.expires_at),
            json.dumps(obs.raw, default=str),
        ),
    )
    return int(cur.lastrowid)


def _row_to_observation(row: sqlite3.Row) -> Observation:
    return Observation(
        metric=Metric(row["metric"]),
        instrument=row["instrument"],
        value=Decimal(row["value"]),
        unit=Unit(row["unit"]),
        venue=row["venue"],
        source_id=row["source_id"],
        tier=Tier(row["tier"]),
        collected_at=_dt(row["collected_at"]),
        observed_at=_dt(row["observed_at"]),
        expires_at=_dt(row["expires_at"]),
        raw=json.loads(row["raw"]),
    )


def query_observations(
    conn: sqlite3.Connection,
    *,
    instrument: str,
    metric: Metric | None = None,
    as_of: datetime | None = None,
    lookback_seconds: float | None = None,
) -> list[Observation]:
    """The single read path for observations. as_of enforces the no-lookahead rule
    (observed_at <= as_of) structurally — both LiveSource and ReplaySource route through
    this with as_of set to "now" or a frozen instant respectively, and no caller can ask
    for anything past it. Expired-as-of-as_of rows are never returned (doctrine 0.2)."""
    clauses = ["instrument = ?"]
    params: list[Any] = [instrument]
    if metric is not None:
        clauses.append("metric = ?")
        params.append(metric.value)
    if as_of is not None:
        clauses.append("observed_at <= ?")
        params.append(_iso(as_of))
        clauses.append("expires_at > ?")
        params.append(_iso(as_of))
    if lookback_seconds is not None and as_of is not None:
        from datetime import timedelta

        floor = as_of - timedelta(seconds=lookback_seconds)
        clauses.append("observed_at >= ?")
        params.append(_iso(floor))
    sql = f"SELECT * FROM observations WHERE {' AND '.join(clauses)} ORDER BY observed_at ASC"
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_observation(r) for r in rows]


def query_observations_all(
    conn: sqlite3.Connection,
    *,
    instrument: str,
    as_of: datetime,
    lookback_seconds: float,
) -> list[Observation]:
    """Like query_observations, but does NOT filter out expired rows — used only by
    Layer 0's record-integrity check (doctrine 0.10) to find snapshots where a sibling
    field has already expired, so the whole snapshot can be discarded rather than
    cherry-picked. Still enforces no-lookahead (observed_at <= as_of); this is a
    relaxation of the expiry filter only, never of the lookahead guarantee."""
    from datetime import timedelta

    floor = as_of - timedelta(seconds=lookback_seconds)
    rows = conn.execute(
        """SELECT * FROM observations
           WHERE instrument = ? AND observed_at <= ? AND observed_at >= ?
           ORDER BY observed_at ASC""",
        (instrument, _iso(as_of), _iso(floor)),
    ).fetchall()
    return [_row_to_observation(r) for r in rows]


def get_source_state(conn: sqlite3.Connection, source_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM source_state WHERE source_id = ?", (source_id,)
    ).fetchone()
    if row is None:
        return {"reliability_prior": 1.0, "failure_count": 0, "demoted_at": None}
    return {
        "reliability_prior": row["reliability_prior"],
        "failure_count": row["failure_count"],
        "demoted_at": _dt(row["demoted_at"]) if row["demoted_at"] else None,
    }


def get_all_source_state(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute("SELECT * FROM source_state").fetchall()
    return {
        r["source_id"]: {
            "reliability_prior": r["reliability_prior"],
            "failure_count": r["failure_count"],
            "demoted_at": _dt(r["demoted_at"]) if r["demoted_at"] else None,
        }
        for r in rows
    }


def record_source_failure(conn: sqlite3.Connection, source_id: str, *, demote_after: int = 2) -> None:
    """Layer 6 audit loop primitive: a source that fails twice moves to T0 permanently
    (doctrine, Layer 6 "Source scoring"). Demotion is not reversible by a later good
    reading."""
    state = get_source_state(conn, source_id)
    if state["demoted_at"] is not None:
        return
    new_count = state["failure_count"] + 1
    demoted_at = _iso(datetime.now(timezone.utc)) if new_count >= demote_after else None
    conn.execute(
        """INSERT INTO source_state (source_id, reliability_prior, failure_count, demoted_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(source_id) DO UPDATE SET
             failure_count = excluded.failure_count,
             reliability_prior = MAX(0.0, reliability_prior - 0.25),
             demoted_at = COALESCE(source_state.demoted_at, excluded.demoted_at)""",
        (source_id, max(0.0, 1.0 - 0.25 * new_count), new_count, demoted_at),
    )


def record_source_success(conn: sqlite3.Connection, source_id: str) -> None:
    conn.execute(
        """INSERT INTO source_state (source_id, reliability_prior, failure_count, demoted_at)
           VALUES (?, 1.0, 0, NULL)
           ON CONFLICT(source_id) DO UPDATE SET
             reliability_prior = MIN(1.0, reliability_prior + 0.05)""",
        (source_id,),
    )


def record_collector_event(
    conn: sqlite3.Connection, *, source_id: str, venue: str, event_type: str, detail: str = ""
) -> None:
    conn.execute(
        "INSERT INTO collector_events (source_id, venue, event_type, detail, at) VALUES (?, ?, ?, ?, ?)",
        (source_id, venue, event_type, detail, _iso(datetime.now(timezone.utc))),
    )


def save_decision_record(conn: sqlite3.Connection, record: dict) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO decision_records
           (run_id, instrument, run_at, observation_ids, gate_results, classification,
            setups_evaluated, verdict, config_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            record["run_id"],
            record["instrument"],
            record["run_at"],
            json.dumps(record["observation_ids"]),
            json.dumps(record["gate_results"], default=str),
            json.dumps(record["classification"], default=str),
            json.dumps(record["setups_evaluated"], default=str),
            record["verdict"],
            record["config_hash"],
        ),
    )


def list_decision_records(conn: sqlite3.Connection, instrument: str, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM decision_records WHERE instrument = ? ORDER BY run_at DESC LIMIT ?",
        (instrument, limit),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for key in ("observation_ids", "gate_results", "classification", "setups_evaluated"):
            d[key] = json.loads(d[key])
        out.append(d)
    return out


def add_watched_instrument(conn: sqlite3.Connection, instrument: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO watched_instruments (instrument, added_at) VALUES (?, ?)",
        (instrument.upper(), _iso(datetime.now(timezone.utc))),
    )


def remove_watched_instrument(conn: sqlite3.Connection, instrument: str) -> None:
    conn.execute("DELETE FROM watched_instruments WHERE instrument = ?", (instrument.upper(),))


def list_watched_instruments(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT instrument FROM watched_instruments ORDER BY added_at ASC").fetchall()
    return [r["instrument"] for r in rows]


def save_position(conn: sqlite3.Connection, position: dict) -> None:
    conn.execute(
        """INSERT INTO positions
           (position_id, instrument, setup, opened_at, entry_evidence, trapped_cohort, status, closed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            position["position_id"],
            position["instrument"],
            position["setup"],
            position["opened_at"],
            json.dumps(position["entry_evidence"], default=str),
            json.dumps(position["trapped_cohort"], default=str),
            position.get("status", "open"),
            position.get("closed_at"),
        ),
    )


def get_position(conn: sqlite3.Connection, position_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM positions WHERE position_id = ?", (position_id,)
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["entry_evidence"] = json.loads(d["entry_evidence"])
    d["trapped_cohort"] = json.loads(d["trapped_cohort"])
    return d


def list_open_positions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM positions WHERE status = 'open'").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["entry_evidence"] = json.loads(d["entry_evidence"])
        d["trapped_cohort"] = json.loads(d["trapped_cohort"])
        out.append(d)
    return out


def close_position(conn: sqlite3.Connection, position_id: str, *, at: datetime) -> None:
    conn.execute(
        "UPDATE positions SET status = 'closed', closed_at = ? WHERE position_id = ?",
        (_iso(at), position_id),
    )
