"""Run a report from the terminal against the live store, no API/auth required. Useful
on the VPS itself, or locally against a store the collector has been filling.

Usage: python -m backend.scripts.report_cli ZEC
"""
from __future__ import annotations

import sys

from backend.core.config import load_config
from backend.core.registry import SourceRegistry
from backend.replay.source import LiveSource
from backend.report.contract import persist_report, run_analysis
from backend.report.render import render_text
from backend.store import db


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python -m backend.scripts.report_cli <SYMBOL>", file=sys.stderr)
        raise SystemExit(1)
    symbol = sys.argv[1].upper()
    db.init_db()
    cfg = load_config()
    with db.get_connection() as conn:
        registry = SourceRegistry.from_config(cfg, db.get_all_source_state(conn))
        ds = LiveSource(conn)
        report = run_analysis(symbol, ds, cfg, registry)
        persist_report(conn, report)
        db.add_watched_instrument(conn, symbol)
    print(render_text(report))


if __name__ == "__main__":
    main()
