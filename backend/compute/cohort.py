"""Layer 2.2 — trapped cohort and direction. "Name who is forced to act, which way, and
by what deadline... If the cohort cannot be named, there is no trade." This is computed
purely from the sign of net liquidation value over a window (doctrine's own computable
definition), requiring the sign to hold consistently across sub-periods, not just in
aggregate — a single lopsided period could be one large forced order, not a cohort.

Sign convention (see sources/liquidations.py): a forced BUY order liquidates a SHORT
position, so positive net liquidation value means shorts are the ones being destroyed —
i.e. a short-dominant tape means TRAPPED SHORTS, matching the doctrine's own worked
example ("persistent positive funding combined with persistent short liquidations means
trapped shorts, not crowded longs").
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from backend.core.config import Config
from backend.core.observation import Metric, Observation


class Cohort(str, Enum):
    TRAPPED_SHORTS = "trapped_shorts"
    TRAPPED_LONGS = "trapped_longs"
    UNNAMED = "unnamed"


@dataclass(frozen=True)
class CohortResult:
    cohort: Cohort
    evidence: dict


def _bucket_liquidations(
    liquidation_obs: list[Observation], *, window_hours: float, n_buckets: int
) -> list[Decimal]:
    if not liquidation_obs:
        return [Decimal(0)] * n_buckets
    latest = max(o.observed_at for o in liquidation_obs)
    bucket_seconds = (window_hours * 3600) / n_buckets
    buckets = [Decimal(0)] * n_buckets
    for obs in liquidation_obs:
        age = (latest - obs.observed_at).total_seconds()
        idx = int(age // bucket_seconds)
        if 0 <= idx < n_buckets:
            # bucket 0 = most recent
            buckets[n_buckets - 1 - idx] += obs.value
    return buckets


def classify(liquidation_history: list[Observation], *, cfg: Config) -> CohortResult:
    """liquidation_history must come from the historical-series primitive
    (observations_including_expired), not observations() — liquidation events are
    discrete past facts, not a "current state" that expires (see replay/source.py)."""
    window_hours = float(cfg.get("liq_window_hours", default=72))
    min_periods = int(cfg.get("liq_min_consistent_periods", default=3))

    liq_obs = [o for o in liquidation_history if o.metric == Metric.LIQUIDATION]
    buckets = _bucket_liquidations(liq_obs, window_hours=window_hours, n_buckets=min_periods)
    evidence = {"buckets_newest_last": [str(b) for b in buckets], "window_hours": window_hours}

    non_zero = [b for b in buckets if b != 0]
    if len(non_zero) < min_periods:
        evidence["reason"] = f"fewer than {min_periods} periods with any liquidation activity"
        return CohortResult(cohort=Cohort.UNNAMED, evidence=evidence)

    signs = {b > 0 for b in non_zero}
    if len(signs) > 1:
        evidence["reason"] = "liquidation sign not consistent across periods"
        return CohortResult(cohort=Cohort.UNNAMED, evidence=evidence)

    dominant_positive = signs.pop()
    cohort = Cohort.TRAPPED_SHORTS if dominant_positive else Cohort.TRAPPED_LONGS
    evidence["net_value"] = str(sum(buckets, Decimal(0)))
    return CohortResult(cohort=cohort, evidence=evidence)
