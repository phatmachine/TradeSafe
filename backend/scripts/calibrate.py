"""Calibration sweep CLI (implementation spec, "Replay harness" / doctrine "Validation
protocol"). Sweeps one threshold across the instrument's full stored history and prints
the scored table — the human reading it picks the value on the stability plateau.

Usage:
    python -m backend.scripts.calibrate ZEC cascade.flush_oi_pct 0.08 0.10 0.12 0.15 0.20
"""
from __future__ import annotations

import sys
from datetime import timedelta

from backend.core.config import load_config
from backend.core.registry import SourceRegistry
from backend.replay.score import score_sweep_point, summarise
from backend.replay.sweep import as_of_grid, sweep_threshold
from backend.store import db


def main() -> None:
    if len(sys.argv) < 4:
        print(
            "usage: python -m backend.scripts.calibrate <SYMBOL> <dotted.threshold.path> <value> [value ...]",
            file=sys.stderr,
        )
        raise SystemExit(1)
    symbol = sys.argv[1].upper()
    param_path = tuple(sys.argv[2].split("."))
    raw_values = sys.argv[3:]
    values: list[float] = []
    for v in raw_values:
        try:
            values.append(float(v))
        except ValueError:
            values.append(v)  # allow non-numeric overrides too

    db.init_db()
    cfg = load_config()
    with db.get_connection() as conn:
        registry = SourceRegistry.from_config(cfg, db.get_all_source_state(conn))
        earliest = conn.execute(
            "SELECT MIN(observed_at) AS m FROM observations WHERE instrument = ?", (symbol,)
        ).fetchone()["m"]
        latest = conn.execute(
            "SELECT MAX(observed_at) AS m FROM observations WHERE instrument = ?", (symbol,)
        ).fetchone()["m"]
        if not earliest or not latest:
            print(f"no stored history for {symbol!r} yet — let the collector run longer first.")
            raise SystemExit(1)
        from datetime import datetime

        start, end = datetime.fromisoformat(earliest), datetime.fromisoformat(latest)
        points = as_of_grid(start=start, end=end, step=timedelta(hours=6))
        print(f"sweeping {len(values)} value(s) across {len(points)} as-of points from {start} to {end}")

        sweep_points = sweep_threshold(
            conn, instrument=symbol, param_path=param_path, values=values, as_of_points=points, base_cfg=cfg, registry=registry
        )
        scores = [score_sweep_point(conn, p, instrument=symbol, cfg=cfg) for p in sweep_points]
    print(summarise(scores))


if __name__ == "__main__":
    main()
