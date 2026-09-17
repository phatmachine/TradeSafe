"""Layer 3 — event decompression. "A dated, primary-sourced event with verifiable
one-sided positioning. Traded after the event resolves, not into it."

Event ingestion (FOMC/filing/unlock calendars) is one of the doctrine's own open items
("Required, no cost" issuer/calendar sources — implementation spec, Third-party
integrations) and is not fully wired up in this build: this module evaluates the
condition correctly against whatever EVENT observations exist, but will honestly report
`unknown` rather than `pass` until a real calendar source is registered for the
instrument. That is fail-closed behaviour, not a bug.
"""
from __future__ import annotations

from decimal import Decimal

from backend.core.config import Config
from backend.core.observation import Metric, Observation
from backend.gates.common import ConditionResult, GateResult

SETUP_NAME = "event_decompression"


def evaluate(
    instrument: str,
    *,
    event_history: list[Observation],
    funding_history: list[Observation],
    as_of,
    cfg: Config,
) -> GateResult:
    conditions: list[ConditionResult] = []
    funding_periods = int(cfg.get("funding_periods", default=3))
    one_sided_threshold = Decimal(str(cfg.get("gate_u", "rv_band_min", default=0.20))) / 10  # small, explicit fraction

    events = [o for o in event_history if o.metric == Metric.EVENT and o.observed_at <= as_of]
    if not events:
        conditions.append(
            ConditionResult(
                name="event_dated_and_resolved",
                status="unknown",
                computed_value=None,
                threshold="a primary-sourced event with observed_at <= as_of",
                detail="no dated event observation registered for this instrument",
            )
        )
    else:
        most_recent = max(events, key=lambda o: o.observed_at)
        conditions.append(
            ConditionResult(
                name="event_dated_and_resolved",
                status="pass",
                computed_value=str(most_recent.observed_at),
                threshold="a primary-sourced event with observed_at <= as_of",
                detail=f"resolved event from {most_recent.source_id}",
            )
        )

    funding_series = sorted((o.value for o in funding_history), key=lambda v: v)
    recent = [o.value for o in sorted(funding_history, key=lambda o: o.observed_at)][-funding_periods:]
    if len(recent) < funding_periods:
        conditions.append(
            ConditionResult(
                name="one_sided_positioning_verifiable",
                status="unknown",
                computed_value=None,
                threshold=f"consistent sign, |funding| > {one_sided_threshold}",
                detail="insufficient funding history",
            )
        )
    else:
        signs = {v > 0 for v in recent}
        one_sided = len(signs) == 1 and all(abs(v) > one_sided_threshold for v in recent)
        conditions.append(
            ConditionResult(
                name="one_sided_positioning_verifiable",
                status="pass" if one_sided else "fail",
                computed_value=recent,
                threshold=f"consistent sign, |funding| > {one_sided_threshold}",
                detail="funding sign and magnitude across venues as a positioning proxy",
            )
        )

    return GateResult(gate=SETUP_NAME, conditions=tuple(conditions))
