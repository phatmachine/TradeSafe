"""Layer 2.1 — state classification: trending / mean-reverting / undetermined. This is
the single gate the doctrine says would have prevented the v1 failure ("Mean-reversion
setups are locked out in a trending regime"). `undetermined` is a real third value, not
an error state — it blocks every setup exactly like a failed gate (implementation spec,
run() pseudocode: `if state.regime == UNDETERMINED: return report(NO_SETUP, state)`).

The exact numeric shape of "trending" is one of the doctrine's own open items (appendix,
"Design questions for the skill build") — this module makes the interpretation explicit
and auditable so it can be calibrated, rather than burying it as a magic threshold.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from backend.compute.volatility import realised_volatility_annualised
from backend.core.config import Config
from backend.core.observation import Observation


class Regime(str, Enum):
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    MEAN_REVERTING = "mean_reverting"
    UNDETERMINED = "undetermined"


@dataclass(frozen=True)
class SwingPoint:
    index: int
    kind: str  # "high" | "low"
    value: Decimal


@dataclass(frozen=True)
class RegimeResult:
    regime: Regime
    evidence: dict


def resample_closes(observations: list[Observation], bucket_seconds: int) -> list[tuple[int, Decimal]]:
    """Buckets a single venue's price series into fixed-width bars, close = last tick in
    the bucket. Callers must pre-filter to one venue (never-mix-venues)."""
    buckets: dict[int, Observation] = {}
    for obs in sorted(observations, key=lambda o: o.observed_at):
        bucket = int(obs.observed_at.timestamp() // bucket_seconds)
        buckets[bucket] = obs  # last write per bucket wins because input is sorted ascending
    return [(b, buckets[b].value) for b in sorted(buckets)]


def _timeframe_seconds(tf: str) -> int:
    unit = tf[-1]
    n = int(tf[:-1])
    return {"m": 60, "h": 3600, "d": 86400}[unit] * n


def find_swings(closes: list[Decimal], lookback: int) -> list[SwingPoint]:
    """A confirmed local extreme with `lookback` bars on both sides. Only returns swings
    with enough trailing bars to be confirmed — no lookahead into unconfirmed structure."""
    swings: list[SwingPoint] = []
    for i in range(lookback, len(closes) - lookback):
        window = closes[i - lookback : i + lookback + 1]
        if closes[i] == max(window):
            swings.append(SwingPoint(index=i, kind="high", value=closes[i]))
        elif closes[i] == min(window):
            swings.append(SwingPoint(index=i, kind="low", value=closes[i]))
    return swings


def _monotonic_higher_lows(lows: list[Decimal], margin_pct: Decimal) -> bool:
    if len(lows) < 2:
        return False
    return all(lows[i] >= lows[i - 1] * (1 + margin_pct) for i in range(1, len(lows)))


def _monotonic_lower_highs(highs: list[Decimal], margin_pct: Decimal) -> bool:
    if len(highs) < 2:
        return False
    return all(highs[i] <= highs[i - 1] * (1 - margin_pct) for i in range(1, len(highs)))


def classify(
    price_history: list[Observation],
    *,
    cfg: Config,
) -> RegimeResult:
    """price_history must already be a single venue's series (caller's responsibility —
    never-mix-venues), pulled via the historical-series primitive so it is not filtered
    by current-state expiry (see replay/source.py)."""
    tf_seconds = _timeframe_seconds(str(cfg.get("swing_timeframe", default="4h")))
    lookback = int(cfg.get("swing_lookback_bars", default=5))
    n_swings_required = int(cfg.get("n_swings_required", default=3))
    hl_margin = Decimal(str(cfg.get("hl_margin_pct", default=0.01)))
    trend_ratio_max = Decimal(str(cfg.get("trend_ratio_max", default=0.7)))
    rv_short_days = int(cfg.get("rv_short_days", default=7))
    rv_long_days = int(cfg.get("rv_long_days", default=30))

    bars = resample_closes(price_history, tf_seconds)
    closes = [c for _, c in bars]

    from backend.compute.volatility import daily_closes

    daily = daily_closes(price_history)
    rv_short = realised_volatility_annualised(daily[-(rv_short_days + 1) :]) if daily else None
    rv_long = realised_volatility_annualised(daily) if daily else None

    evidence = {
        "bars_used": len(closes),
        "rv_short": rv_short,
        "rv_long": rv_long,
    }

    if len(closes) < 2 * lookback + 2 or rv_short is None or rv_long is None or rv_long == 0:
        evidence["reason"] = "insufficient bar or volatility history"
        return RegimeResult(regime=Regime.UNDETERMINED, evidence=evidence)

    rv_ratio = Decimal(str(rv_short)) / Decimal(str(rv_long))
    evidence["rv_ratio"] = rv_ratio
    net_direction = closes[-1] - closes[0]
    evidence["net_direction"] = net_direction

    swings = find_swings(closes, lookback)
    swing_lows = [s.value for s in swings if s.kind == "low"][-n_swings_required:]
    swing_highs = [s.value for s in swings if s.kind == "high"][-n_swings_required:]
    evidence["swing_lows"] = swing_lows
    evidence["swing_highs"] = swing_highs

    low_tight_enough = rv_ratio <= trend_ratio_max

    if (
        low_tight_enough
        and net_direction > 0
        and len(swing_lows) >= n_swings_required
        and _monotonic_higher_lows(swing_lows, hl_margin)
    ):
        return RegimeResult(regime=Regime.TRENDING_UP, evidence=evidence)

    if (
        low_tight_enough
        and net_direction < 0
        and len(swing_highs) >= n_swings_required
        and _monotonic_lower_highs(swing_highs, hl_margin)
    ):
        return RegimeResult(regime=Regime.TRENDING_DOWN, evidence=evidence)

    # Mean-reverting is an affirmative finding, not "trending failed": short-term vol
    # dominating long-term vol, with swing structure that is NOT monotonic in either
    # direction, is direct evidence of chop rather than absence of evidence of a trend.
    non_monotonic = not _monotonic_higher_lows(swing_lows, Decimal(0)) and not _monotonic_lower_highs(
        swing_highs, Decimal(0)
    )
    if rv_ratio > trend_ratio_max and non_monotonic:
        return RegimeResult(regime=Regime.MEAN_REVERTING, evidence=evidence)

    evidence["reason"] = "neither a confirmed trend nor a confirmed range"
    return RegimeResult(regime=Regime.UNDETERMINED, evidence=evidence)


def structural_higher_low(swing_lows: list[Decimal], margin_pct: Decimal) -> bool:
    """Doctrine 'structural higher low': the most recent swing low exceeds the prior
    swing low by at least margin_pct. Used directly by the trend-continuation setup."""
    if len(swing_lows) < 2:
        return False
    return swing_lows[-1] >= swing_lows[-2] * (1 + margin_pct)
