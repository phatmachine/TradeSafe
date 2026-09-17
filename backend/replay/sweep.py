"""Calibration sweep (implementation spec, "Replay harness"). Build this before trusting
any threshold: sweeps a value across the instrument's full stored history and hands every
run's report to score.py, which is what turns an open threshold from a guess into a
measurement. Every run goes through the exact same run_analysis() the live path uses —
only the DataSource (ReplaySource, clock pinned to each as_of) and the swept Config
differ (implementation spec, "Same code path").
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from backend.core.config import Config, with_override
from backend.core.registry import SourceRegistry
from backend.replay.source import ReplaySource
from backend.report.contract import AnalysisReport, run_analysis


@dataclass(frozen=True)
class SweepPoint:
    value: Any
    config_hash: str
    reports: tuple[AnalysisReport, ...]


def as_of_grid(*, start: datetime, end: datetime, step: timedelta) -> list[datetime]:
    points = []
    t = start
    while t <= end:
        points.append(t)
        t += step
    return points


def sweep_threshold(
    conn: sqlite3.Connection,
    *,
    instrument: str,
    param_path: tuple[str, ...],
    values: list[Any],
    as_of_points: list[datetime],
    base_cfg: Config,
    registry: SourceRegistry,
) -> list[SweepPoint]:
    """Every other threshold is held at base_cfg's value while param_path is swept — the
    doctrine's own instruction to vary one thing at a time and read the plateau, not
    chase a joint optimum across several knobs at once."""
    points: list[SweepPoint] = []
    for value in values:
        cfg = with_override(base_cfg, param_path, value)
        reports = []
        for as_of in as_of_points:
            ds = ReplaySource(conn, as_of)
            reports.append(run_analysis(instrument, ds, cfg, registry))
        points.append(SweepPoint(value=value, config_hash=cfg.config_hash, reports=tuple(reports)))
    return points
