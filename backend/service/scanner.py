"""Scheduled setup scan. Every TRADESAFE_SCAN_INTERVAL_MINUTES (default 15) it runs the
same run_analysis() an on-demand report uses over every watched instrument, persists the
report like any other, and records an alert the first time a setup qualifies: once per
episode, not on every scan while the setup keeps qualifying.

Runs inside the API process, the analysis service, rather than the collector, which
holds no decision logic. An alert carries only what the report itself says (the setup,
its long/short type, the verdict and its direction badge), never an instruction, size or
price.
"""
from __future__ import annotations

import asyncio
import logging
import os

from backend.core.config import Config, load_config
from backend.core.registry import SourceRegistry
from backend.replay.source import LiveSource
from backend.report.contract import AnalysisReport, Verdict, persist_report, run_analysis
from backend.scripts import backfill_history
from backend.store import db

logger = logging.getLogger("tradesafe.scanner")

# Long enough for a freshly started collector's first polls to land, so the first scan
# after a deploy reads current data rather than failing the freshness gate.
FIRST_SCAN_DELAY_SECONDS = 60


def interval_seconds() -> int:
    """0 or less turns the scanner off."""
    return int(float(os.environ.get("TRADESAFE_SCAN_INTERVAL_MINUTES", "15")) * 60)


def qualifying_setups(report: AnalysisReport) -> set[str] | None:
    """The setups this report says qualify, or None when it can't say. A failed trust gate
    means the setups weren't checked, which is not the same as them having stopped
    qualifying: treating it as "none" would re-alert the same episode once data returns."""
    if report.verdict == Verdict.GATE_FAIL.value:
        return None
    return {s["gate"] for s in report.setup_evaluation if s["passed"]}


def record_scan(conn, report: AnalysisReport) -> list[dict]:
    """Compares this report with the instrument's last conclusive scan and stores an alert
    for each setup that has started qualifying since. Returns the new alerts."""
    now = qualifying_setups(report)
    if now is None:
        return []
    before = db.get_qualifying_setups(conn, report.instrument)
    cases = {s["gate"]: s.get("case") for s in report.setup_evaluation}
    alerts = [
        db.insert_alert(
            conn,
            {
                "instrument": report.instrument,
                "setup": setup,
                "setup_case": cases.get(setup),
                "verdict": report.verdict,
                "verdict_bias": report.verdict_bias,
                "run_id": report.run_id,
                "as_of": report.as_of,
            },
        )
        for setup in sorted(now - before)
    ]
    db.set_qualifying_setups(conn, report.instrument, now)
    return alerts


def scan_instrument(symbol: str, cfg: Config) -> list[dict]:
    backfill_history.bootstrap(symbol, cfg)
    with db.get_connection() as conn:
        registry = SourceRegistry.from_config(cfg, db.get_all_source_state(conn))
        report = run_analysis(symbol, LiveSource(conn), cfg, registry)
        persist_report(conn, report)
        return record_scan(conn, report)


def scan_once() -> int:
    """One pass over the watchlist. One instrument failing is logged against the run and
    doesn't stop the rest. Returns the number of new alerts."""
    cfg = load_config()
    with db.get_connection() as conn:
        instruments = db.list_watched_instruments(conn)
    new_alerts, errors = 0, []
    for symbol in instruments:
        try:
            new_alerts += len(scan_instrument(symbol, cfg))
        except Exception as exc:
            logger.exception("scanner: %s failed", symbol)
            errors.append(f"{symbol}: {exc}")
    with db.get_connection() as conn:
        db.record_scan_run(conn, instruments=len(instruments), new_alerts=new_alerts, errors="; ".join(errors))
    return new_alerts


async def scan_loop(stop_event: asyncio.Event) -> None:
    interval = interval_seconds()
    if interval <= 0:
        return
    delay = FIRST_SCAN_DELAY_SECONDS
    while True:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
            return
        except asyncio.TimeoutError:
            pass
        try:
            # A pass takes a second or so per instrument; off the event loop so the API
            # keeps answering requests meanwhile.
            await asyncio.to_thread(scan_once)
        except Exception:
            logger.exception("scanner: scan failed")
        delay = interval
