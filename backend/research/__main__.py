"""Offline calibration against years of free history — see backend/research/harness.py.

    python -m backend.research backfill BTC ETH SOL ZEC [--since 2023-01-01]
    python -m backend.research events [--since 2023-01-01]    # macro calendar; FRED_API_KEY for CPI/jobs/PCE
    python -m backend.research baseline BTC ETH SOL ZEC
    python -m backend.research sweep BTC ETH SOL ZEC --param trend_ratio_max --values 0.6 0.7 0.8
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m backend.research")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("backfill", help="download free history into the research store")
    p.add_argument("symbols", nargs="+")
    p.add_argument("--since", default="2023-01-01")

    p = sub.add_parser("events", help="download the macro event calendar into the research store")
    p.add_argument("--since", default="2023-01-01")

    for name in ("baseline", "sweep"):
        p = sub.add_parser(name)
        p.add_argument("symbols", nargs="+")
        p.add_argument("--step-hours", type=float, default=1.0, help="evaluation grid spacing")
        if name == "sweep":
            p.add_argument("--param", required=True, help="dotted thresholds.yaml path, e.g. cascade.flush_oi_pct")
            p.add_argument("--values", nargs="+", required=True)

    args = parser.parse_args()
    if args.command == "events":
        from backend.research.backfill import backfill_events

        backfill_events(since=datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc))
        return
    if args.command == "backfill":
        from backend.research.backfill import backfill

        since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
        for symbol in args.symbols:
            backfill(symbol, since=since)
        return

    from backend.research import harness

    if args.command == "baseline":
        print(harness.baseline_report([s.upper() for s in args.symbols], step_hours=args.step_hours))
    else:
        values = [_parse_value(v) for v in args.values]
        print(harness.sweep_report([s.upper() for s in args.symbols], tuple(args.param.split(".")), values,
                                   step_hours=args.step_hours))


def _parse_value(v: str):
    try:
        return int(v)
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return v


if __name__ == "__main__":
    main()
