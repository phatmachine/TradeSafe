"""Scores a calibration sweep. Doctrine, Validation protocol: "Score protective and
costly refusals in separate columns. A single blended number hides which direction the
framework is wrong in." — this module never collapses the two into one figure.

Directional bias per setup, used only to grade a REFUSAL's forward return (never to size
or place anything): cascade absorption and uptrend continuation are long by construction
(doctrine: "the condition to buy into" / "buy the pullback"), and their mirrors — squeeze
absorption and downtrend continuation — are short by construction; positioning
exhaustion and event decompression fade whichever cohort the report already named as
trapped for that as_of.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from backend.core.config import Config
from backend.core.observation import Metric
from backend.replay.source import ReplaySource
from backend.replay.sweep import SweepPoint
from backend.report.contract import Verdict

LONG_ONLY_SETUPS = {"cascade_absorption", "trend_continuation_leverage_reset"}
SHORT_ONLY_SETUPS = {"squeeze_absorption", "downtrend_continuation_leverage_reset"}
FADE_COHORT_SETUPS = {"positioning_exhaustion", "event_decompression"}


def _forward_return(conn: sqlite3.Connection, instrument: str, entry_at, hold_days: float) -> Decimal | None:
    exit_at = entry_at + timedelta(days=hold_days)
    entry_prices = ReplaySource(conn, entry_at).observations(instrument, metric=Metric.PRICE)
    exit_prices = ReplaySource(conn, exit_at).observations(instrument, metric=Metric.PRICE)
    if not entry_prices or not exit_prices:
        return None
    entry_price, exit_price = entry_prices[-1].value, exit_prices[-1].value
    if entry_price == 0:
        return None
    return (exit_price - entry_price) / entry_price


def _direction_sign(setup_name: str, trapped_cohort: str | None) -> int | None:
    if setup_name in LONG_ONLY_SETUPS:
        return 1
    if setup_name in SHORT_ONLY_SETUPS:
        return -1
    if setup_name in FADE_COHORT_SETUPS:
        if trapped_cohort == "trapped_shorts":
            return 1  # shorts trapped -> squeeze up -> fade by going long
        if trapped_cohort == "trapped_longs":
            return -1
    return None


@dataclass
class SetupRefusalScore:
    protective: int = 0
    costly: int = 0
    unscored: int = 0


@dataclass
class SweepScore:
    value: object
    config_hash: str
    layer0_pass_rate: float | None
    setups_triggered_per_month: float
    refusals_by_setup: dict[str, SetupRefusalScore] = field(default_factory=dict)


def score_sweep_point(conn: sqlite3.Connection, point: SweepPoint, *, instrument: str, cfg: Config) -> SweepScore:
    reports = point.reports
    gate_u_attempts = [r for r in reports if r.verdict != Verdict.GATE_FAIL.value or len(r.gate_status) >= 1]
    reached_layer0 = [r for r in reports if len(r.gate_status) >= 2]
    layer0_pass_rate = (
        sum(1 for r in reached_layer0 if r.gate_status[1]["passed"]) / len(reached_layer0)
        if reached_layer0
        else None
    )

    eligible = [r for r in reports if r.verdict == Verdict.ELIGIBLE_SETUP.value]
    span_days = max(1.0, (_parse(reports[-1].as_of) - _parse(reports[0].as_of)).total_seconds() / 86400) if len(reports) > 1 else 30.0
    triggered_per_month = len(eligible) / (span_days / 30.0)

    hold_windows = cfg.get("expected_hold_window_days", default={})
    refusals: dict[str, SetupRefusalScore] = {}
    for report in reports:
        trapped_cohort = report.state_classification.get("trapped_cohort") if report.state_classification else None
        for setup in report.setup_evaluation:
            if setup["passed"]:
                continue
            name = setup["gate"]
            hold_days = float(hold_windows.get(name, 10))
            sign = _direction_sign(name, trapped_cohort)
            score = refusals.setdefault(name, SetupRefusalScore())
            if sign is None:
                score.unscored += 1
                continue
            fwd = _forward_return(conn, instrument, _parse(report.as_of), hold_days)
            if fwd is None:
                score.unscored += 1
                continue
            realised = fwd * sign
            if realised <= 0:
                score.protective += 1
            else:
                score.costly += 1

    return SweepScore(
        value=point.value,
        config_hash=point.config_hash,
        layer0_pass_rate=layer0_pass_rate,
        setups_triggered_per_month=triggered_per_month,
        refusals_by_setup=refusals,
    )


def _parse(iso: str):
    from datetime import datetime

    return datetime.fromisoformat(iso)


def summarise(scores: list[SweepScore]) -> str:
    """Doctrine: choose thresholds on a stability plateau, not a peak. This just prints
    the table — the human reading it decides where the plateau is."""
    lines = ["value\tlayer0_pass_rate\tsetups/month\trefusals(setup: protective/costly/unscored)"]
    for s in scores:
        refusal_str = "; ".join(
            f"{name}: {sc.protective}/{sc.costly}/{sc.unscored}" for name, sc in s.refusals_by_setup.items()
        )
        lines.append(f"{s.value}\t{s.layer0_pass_rate}\t{s.setups_triggered_per_month:.2f}\t{refusal_str}")
    return "\n".join(lines)
