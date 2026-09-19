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
from typing import Any, Iterable, Iterator

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
-- Snapshot lookups: compaction must know whether any row fetched alongside a given row
-- is still unexpired (Layer 0's record-integrity rule treats one fetch as one unit).
CREATE INDEX IF NOT EXISTS idx_obs_snapshot
    ON observations (source_id, collected_at);

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

-- Scheduled scan (service/scanner.py): one alert each time a setup starts qualifying.
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument TEXT NOT NULL,
    setup TEXT NOT NULL,
    setup_case TEXT,
    verdict TEXT NOT NULL,
    verdict_bias TEXT,
    run_id TEXT,
    as_of TEXT NOT NULL,
    created_at TEXT NOT NULL,
    is_test INTEGER NOT NULL DEFAULT 0
);

-- The setups qualifying at each instrument's last conclusive scan, so a setup alerts once
-- when it starts qualifying rather than on every scan while it keeps qualifying.
CREATE TABLE IF NOT EXISTS scan_state (
    instrument TEXT NOT NULL,
    setup TEXT NOT NULL,
    PRIMARY KEY (instrument, setup)
);

CREATE TABLE IF NOT EXISTS scan_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    finished_at TEXT NOT NULL,
    instruments INTEGER NOT NULL,
    new_alerts INTEGER NOT NULL,
    errors TEXT NOT NULL DEFAULT ''
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
        # WAL lets a report's long history read and the collector's 10-second writes
        # proceed at the same time instead of blocking each other. Persistent: once set,
        # it stays set in the database file.
        conn.execute("PRAGMA journal_mode=WAL")
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
        raw=json.loads(row["raw"]) if "raw" in row.keys() else {},
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


# Every column except `raw`: history reads never look at the source payload, and
# decoding its JSON was ~40% of the cost of loading history.
_HISTORY_COLUMNS = (
    "id, metric, instrument, value, unit, venue, source_id, tier, collected_at, observed_at, expires_at"
)


def query_observations_all(
    conn: sqlite3.Connection,
    *,
    instrument: str,
    as_of: datetime,
    lookback_seconds: float,
    metrics: Iterable[Metric] | None = None,
) -> list[Observation]:
    """Like query_observations, but does NOT filter out expired rows — the historical
    series primitive (see replay/source.py). Still enforces no-lookahead (observed_at <=
    as_of); this is a relaxation of the expiry filter only, never of the lookahead
    guarantee. `metrics` narrows the read to what the caller uses; returned rows carry
    an empty `raw`."""
    from datetime import timedelta

    floor = as_of - timedelta(seconds=lookback_seconds)
    clauses = ["instrument = ?", "observed_at <= ?", "observed_at >= ?"]
    params: list[Any] = [instrument, _iso(as_of), _iso(floor)]
    if metrics is not None:
        wanted = [m.value for m in metrics]
        clauses.append(f"metric IN ({', '.join('?' * len(wanted))})")
        params.extend(wanted)
    rows = conn.execute(
        f"SELECT {_HISTORY_COLUMNS} FROM observations WHERE {' AND '.join(clauses)} ORDER BY observed_at ASC",
        params,
    ).fetchall()
    return [_row_to_observation(r) for r in rows]


COMPACTION_BUCKET_SECONDS = 900
# Discrete events, not samples of a level: every liquidation print and dated event is
# its own fact, so thinning them would delete evidence rather than redundancy.
_NEVER_COMPACTED = (Metric.LIQUIDATION.value, Metric.EVENT.value)


def compact_observations(
    conn: sqlite3.Connection, *, instrument: str, since: datetime, until: datetime, now: datetime
) -> int:
    """Thins rows observed in [since, until) to the latest reading per (metric, venue,
    source_id, 15-minute bucket). The collector polls every 10 seconds, but every history
    consumer buckets at 15 minutes or coarser and takes the last reading per venue per
    bucket, so their results are unchanged — the removed rows only made every report
    slower and the file bigger (30 days of one coin: 5.4M rows / 2.1 GB -> 76k rows).

    What is guaranteed:
    - A live report is unaffected. A row is only removed once it and every row fetched
      alongside it (same source_id and collected_at) have expired, so nothing a current
      read or Layer 0's partially-expired-snapshot rule can see is ever touched.
    - A replay on a 15-minute boundary sees the same latest reading of every series. A
      replay mid-bucket does not (see replay/sweep.as_of_grid).
    - Liquidations and events — discrete facts, not samples of a level — are never touched.

    Callers should pass bucket-aligned bounds and keep each call to a bounded span (a
    day) so no single delete holds the write lock for long."""
    rows = conn.execute(
        f"""DELETE FROM observations WHERE id IN (
              SELECT id FROM (
                SELECT o.id, ROW_NUMBER() OVER (
                         PARTITION BY o.metric, o.venue, o.source_id,
                                      CAST(strftime('%s', o.observed_at) AS INTEGER) / {COMPACTION_BUCKET_SECONDS}
                         ORDER BY o.observed_at DESC, o.id DESC) AS rn
                FROM observations o
                WHERE o.instrument = ? AND o.observed_at >= ? AND o.observed_at < ?
                  AND o.metric NOT IN ({', '.join('?' * len(_NEVER_COMPACTED))})
                  AND NOT EXISTS (
                    SELECT 1 FROM observations s
                    WHERE s.source_id = o.source_id AND s.collected_at = o.collected_at AND s.expires_at > ?)
              ) WHERE rn > 1)""",
        (instrument, _iso(since), _iso(until), *_NEVER_COMPACTED, _iso(now)),
    )
    return rows.rowcount


def vacuum() -> None:
    """Rewrites the file compactly. Compaction removes ~99% of rows, but SQLite keeps the
    freed pages in the file and leaves the survivors scattered across it. Measured on 30
    days of one coin's polling: 2.1 GB -> 29 MB in 0.4s, and a report's price-history
    read went from 1.13s to 0.16s. Holds the write lock while it runs, so it's done
    rarely (see service/collector.py)."""
    with get_connection() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")


def earliest_observed_at(conn: sqlite3.Connection, *, instrument: str) -> datetime | None:
    row = conn.execute("SELECT MIN(observed_at) FROM observations WHERE instrument = ?", (instrument,)).fetchone()
    return _dt(row[0]) if row and row[0] else None


def earliest_liquidation(conn: sqlite3.Connection, *, instrument: str, venue: str) -> datetime | None:
    """When this venue's stored liquidations for the instrument begin, from any source."""
    row = conn.execute(
        "SELECT MIN(observed_at) FROM observations WHERE instrument = ? AND metric = ? AND venue = ?",
        (instrument, Metric.LIQUIDATION.value, venue),
    ).fetchone()
    return _dt(row[0]) if row and row[0] else None


def latest_observed_at(
    conn: sqlite3.Connection, *, instrument: str, metric: Metric, source_id: str
) -> datetime | None:
    row = conn.execute(
        "SELECT MAX(observed_at) FROM observations WHERE instrument = ? AND metric = ? AND source_id = ?",
        (instrument, metric.value, source_id),
    ).fetchone()
    return _dt(row[0]) if row and row[0] else None


def observation_keys_since(
    conn: sqlite3.Connection,
    *,
    instrument: str,
    metric: Metric,
    source_id: str,
    since: datetime,
    venue: str | None = None,
) -> set[tuple[str, str]]:
    """(observed_at, value) of every stored row at or after `since` — what a polled
    event feed (whose pages overlap from one poll to the next) dedupes against before
    inserting, so the same discrete event is never counted twice. `venue` narrows it for
    a source that carries several venues."""
    sql = """SELECT observed_at, value FROM observations
             WHERE instrument = ? AND metric = ? AND source_id = ? AND observed_at >= ?"""
    params: list = [instrument, metric.value, source_id, _iso(since)]
    if venue is not None:
        sql += " AND venue = ?"
        params.append(venue)
    return {(r[0], r[1]) for r in conn.execute(sql, params).fetchall()}


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


def get_qualifying_setups(conn: sqlite3.Connection, instrument: str) -> set[str]:
    rows = conn.execute("SELECT setup FROM scan_state WHERE instrument = ?", (instrument,)).fetchall()
    return {r["setup"] for r in rows}


def set_qualifying_setups(conn: sqlite3.Connection, instrument: str, setups: set[str]) -> None:
    conn.execute("DELETE FROM scan_state WHERE instrument = ?", (instrument,))
    conn.executemany(
        "INSERT INTO scan_state (instrument, setup) VALUES (?, ?)", [(instrument, s) for s in sorted(setups)]
    )


def _alert_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["is_test"] = bool(d["is_test"])
    return d


def insert_alert(conn: sqlite3.Connection, alert: dict) -> dict:
    cur = conn.execute(
        """INSERT INTO alerts
           (instrument, setup, setup_case, verdict, verdict_bias, run_id, as_of, created_at, is_test)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            alert["instrument"],
            alert["setup"],
            alert.get("setup_case"),
            alert["verdict"],
            alert.get("verdict_bias"),
            alert.get("run_id"),
            alert["as_of"],
            _iso(datetime.now(timezone.utc)),
            1 if alert.get("is_test") else 0,
        ),
    )
    return _alert_dict(conn.execute("SELECT * FROM alerts WHERE id = ?", (cur.lastrowid,)).fetchone())


def list_alerts(conn: sqlite3.Connection, *, after_id: int = 0, limit: int = 20) -> list[dict]:
    """Newest first. `after_id` returns only alerts newer than one the caller has seen."""
    rows = conn.execute(
        "SELECT * FROM alerts WHERE id > ? ORDER BY id DESC LIMIT ?", (after_id, limit)
    ).fetchall()
    return [_alert_dict(r) for r in rows]


def latest_alert_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(id) FROM alerts").fetchone()
    return row[0] or 0


def record_scan_run(conn: sqlite3.Connection, *, instruments: int, new_alerts: int, errors: str = "") -> None:
    conn.execute(
        "INSERT INTO scan_runs (finished_at, instruments, new_alerts, errors) VALUES (?, ?, ?, ?)",
        (_iso(datetime.now(timezone.utc)), instruments, new_alerts, errors),
    )


def last_scan_run(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None
