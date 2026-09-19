"""Layer 3 — event decompression. "A dated, primary-sourced event with verifiable
one-sided positioning. Traded after the event resolves, not into it."

Events are scheduled, market-wide US macro releases (sources/calendar.py): CPI, the jobs
report and PCE from FRED's release calendar, and FOMC decisions from the Fed's own
calendar, each at its fixed release time. Two readings make the doctrine line checkable:

- "After the event resolves" = the most recent event happened within the last
  `event.resolved_within_hours`. Any past event would not do: with monthly releases one
  has always happened in the last month, and the setup would fire on funding alone.
- "One-sided positioning" is read going INTO the event: the cross-venue funding means of
  the `funding_periods` periods just before it all share a sign and exceed
  `event.one_sided_funding_min_pct` — who was crowded when the news landed, not who is
  crowded now.

It doesn't say which way that resolves: report/contract.py reads it "unclear". The
research harness scores the fade-the-crowd reading without the live path claiming it.
"""
from __future__ import annotations

from decimal import Decimal

from backend.compute.funding import period_means
from backend.core.config import Config
from backend.core.observation import Metric, Observation
from backend.gates.common import ConditionResult, GateResult
from backend.sources.calendar import EVENT_LABELS

SETUP_NAME = "event_decompression"


def evaluate(
    instrument: str,
    *,
    event_history: list[Observation],
    funding_history: list[Observation],
    as_of,
    cfg: Config,
) -> GateResult:
    resolved_hours = float(cfg.get("event", "resolved_within_hours", default=24))
    one_sided_min = Decimal(str(cfg.get("event", "one_sided_funding_min_pct", default=0.02)))
    funding_periods = int(cfg.get("funding_periods", default=3))
    period_seconds = int(cfg.get("cascade", "bar_seconds", default=900))
    event_threshold = f"a scheduled event within the last {resolved_hours:g}h"
    funding_threshold = f"same sign, |funding| > {one_sided_min} for the {funding_periods} periods before the event"

    events = sorted(
        (o for o in event_history if o.metric == Metric.EVENT and o.observed_at <= as_of), key=lambda o: o.observed_at
    )
    if not events:
        missing = "no dated event registered"
        return GateResult(gate=SETUP_NAME, conditions=(
            ConditionResult("event_dated_and_resolved", "unknown", None, event_threshold, missing),
            ConditionResult("one_sided_positioning_verifiable", "unknown", None, funding_threshold,
                            "no event to read positioning going into"),
        ))

    last = events[-1]
    label = EVENT_LABELS.get(last.venue, last.venue)
    hours_since = (as_of - last.observed_at).total_seconds() / 3600
    conditions = [
        ConditionResult(
            name="event_dated_and_resolved",
            status="pass" if hours_since <= resolved_hours else "fail",
            computed_value=f"{label}, {hours_since:.1f}h ago",
            threshold=event_threshold,
            detail=f"most recent: {label} at {last.observed_at:%Y-%m-%d %H:%M} UTC",
        )
    ]

    before = [o for o in funding_history if o.observed_at < last.observed_at]
    means = period_means(before, period_seconds)[-funding_periods:]
    if len(means) < funding_periods:
        conditions.append(ConditionResult(
            "one_sided_positioning_verifiable", "unknown", None, funding_threshold,
            f"fewer than {funding_periods} funding periods recorded before the {label}",
        ))
    else:
        one_sided = len({v > 0 for v in means}) == 1 and all(abs(v) > one_sided_min for v in means)
        conditions.append(ConditionResult(
            name="one_sided_positioning_verifiable",
            status="pass" if one_sided else "fail",
            computed_value=means,
            threshold=funding_threshold,
            detail=f"cross-venue mean funding going into the {label}, as a positioning proxy",
        ))
    return GateResult(gate=SETUP_NAME, conditions=tuple(conditions))
