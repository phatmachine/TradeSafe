"""Layer 3 — cascade absorption. "Enter only after forced selling has completed:
open-interest collapse confirmed and held, funding reset, liquidation print settled,
price stabilising above the flush wick. Never anticipatory." The highest-conviction
setup because it requires no prediction, only the observation that a specific cohort has
finished being destroyed.

Never fires outside a mean-reverting (or undetermined-but-not-trending — see the
mutual-exclusion note in setups/__init__.py) regime; the caller (report/contract.py) is
responsible for the regime gate, this module only evaluates its own four conditions.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from backend.compute.oi import aggregate_oi_series
from backend.compute.regime import resample_closes
from backend.core.config import Config
from backend.core.observation import Metric, Observation
from backend.gates.common import ConditionResult, GateResult

SETUP_NAME = "cascade_absorption"


def evaluate(
    instrument: str,
    *,
    oi_history: list[Observation],
    price_history: list[Observation],
    funding_history: list[Observation],
    liquidation_history: list[Observation],
    as_of,
    cfg: Config,
) -> GateResult:
    bar_seconds = int(cfg.get("cascade", "bar_seconds", default=900))
    flush_window_hours = float(cfg.get("cascade", "flush_window_hours", default=48))
    flush_oi_pct = Decimal(str(cfg.get("cascade", "flush_oi_pct", default=0.12)))
    flush_hold_periods = int(cfg.get("cascade", "flush_hold_periods", default=3))
    flush_range_pct = Decimal(str(cfg.get("cascade", "flush_range_pct", default=0.03)))
    stab_periods = int(cfg.get("cascade", "stab_periods", default=3))
    funding_periods = int(cfg.get("funding_periods", default=3))
    quiet_minutes = float(cfg.get("cascade", "liquidation_quiet_minutes", default=60))

    conditions: list[ConditionResult] = []

    # --- OI collapse confirmed and held -----------------------------------------
    oi_series = aggregate_oi_series(oi_history, bar_seconds)
    window_bars = max(1, int((flush_window_hours * 3600) // bar_seconds))
    recent = oi_series[-window_bars:] if oi_series else []
    if len(recent) < flush_hold_periods + 1:
        conditions.append(
            ConditionResult(
                name="oi_collapse_confirmed_and_held",
                status="unknown",
                computed_value=None,
                threshold=f">= {flush_oi_pct} drop held {flush_hold_periods} periods",
                detail="insufficient OI history in the flush window",
            )
        )
    else:
        peak = max(v for _, v in recent)
        floor_required = peak * (1 - flush_oi_pct)
        held = all(v <= floor_required for _, v in recent[-flush_hold_periods:])
        current = recent[-1][1]
        drop_pct = (peak - current) / peak if peak else None
        conditions.append(
            ConditionResult(
                name="oi_collapse_confirmed_and_held",
                status="pass" if held and drop_pct is not None and drop_pct >= flush_oi_pct else "fail",
                computed_value=drop_pct,
                threshold=flush_oi_pct,
                detail=f"peak {peak}, current {current}, held for last {flush_hold_periods} periods={held}",
            )
        )

    # --- price stabilising above the flush wick, range contracted ---------------
    bars = resample_closes(price_history, bar_seconds)
    closes = [c for _, c in bars]
    if len(closes) < flush_hold_periods + stab_periods:
        conditions.append(
            ConditionResult(
                name="price_range_contracted",
                status="unknown",
                computed_value=None,
                threshold=flush_range_pct,
                detail="insufficient price history",
            )
        )
        conditions.append(
            ConditionResult(
                name="price_stabilising_above_flush_wick",
                status="unknown",
                computed_value=None,
                threshold=stab_periods,
                detail="insufficient price history",
            )
        )
    else:
        recent_closes = closes[-flush_hold_periods:]
        recent_range = (max(recent_closes) - min(recent_closes)) / recent_closes[-1] if recent_closes[-1] else None
        conditions.append(
            ConditionResult(
                name="price_range_contracted",
                status="pass" if recent_range is not None and recent_range <= flush_range_pct else "fail",
                computed_value=recent_range,
                threshold=flush_range_pct,
                detail="price range over the recent window as a fraction of last close",
            )
        )
        flush_wick_low = min(closes[-(flush_hold_periods + stab_periods) : -stab_periods])
        stab_window = closes[-stab_periods:]
        stabilising = all(c > flush_wick_low for c in stab_window)
        conditions.append(
            ConditionResult(
                name="price_stabilising_above_flush_wick",
                status="pass" if stabilising else "fail",
                computed_value=stab_window,
                threshold=flush_wick_low,
                detail=f"every close for the last {stab_periods} periods must exceed the flush-bar low",
            )
        )

    # --- funding reset -----------------------------------------------------------
    funding_series = sorted(funding_history, key=lambda o: o.observed_at)
    by_period: dict = {}
    for obs in funding_series:
        bucket = int(obs.observed_at.timestamp() // bar_seconds)
        by_period.setdefault(bucket, []).append(obs.value)
    period_means = [sum(vals) / len(vals) for _, vals in sorted(by_period.items())]
    recent_funding = period_means[-funding_periods:]
    if len(recent_funding) < funding_periods:
        conditions.append(
            ConditionResult(
                name="funding_reset",
                status="unknown",
                computed_value=None,
                threshold=f"<= 0 for {funding_periods} periods",
                detail="insufficient funding history across venues",
            )
        )
    else:
        conditions.append(
            ConditionResult(
                name="funding_reset",
                status="pass" if all(f <= 0 for f in recent_funding) else "fail",
                computed_value=recent_funding,
                threshold=f"<= 0 for {funding_periods} periods",
                detail="cross-venue mean funding (see doctrine's OI-weighting; simple mean pending calibration)",
            )
        )

    # --- liquidation print settled ------------------------------------------------
    liq_obs = [o for o in liquidation_history if o.metric == Metric.LIQUIDATION]
    if not liq_obs:
        # UNKNOWN, not pass: an empty liquidation history over the caller's whole
        # lookback cannot distinguish "the tape genuinely went quiet" (which is what this
        # condition wants to confirm) from "the feed has never delivered a row", and
        # treating the second as a pass would let the doctrine's highest-conviction setup
        # fire with no liquidation evidence behind it at all. Unknown is a distinct state
        # and is never null-coalesced to a default (gates/common.py).
        conditions.append(
            ConditionResult(
                name="liquidation_print_settled",
                status="unknown",
                computed_value=None,
                threshold=f"{quiet_minutes} min quiet",
                detail="no liquidation prints in the lookback at all — cannot tell a quiet tape from an absent feed",
            )
        )
    else:
        most_recent = max(o.observed_at for o in liq_obs)
        quiet_for = (as_of - most_recent).total_seconds() / 60
        conditions.append(
            ConditionResult(
                name="liquidation_print_settled",
                status="pass" if quiet_for >= quiet_minutes else "fail",
                computed_value=quiet_for,
                threshold=quiet_minutes,
                detail="minutes since the most recent liquidation print",
            )
        )

    return GateResult(gate=SETUP_NAME, conditions=tuple(conditions))
