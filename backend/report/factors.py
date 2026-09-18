"""Directional factors — for each piece of directional evidence the report already
computes, whether it currently leans for or against a long, with the reason stated.

Presentation only. Nothing here feeds the verdict, verdict_bias, or any setup: a factor
leaning "supports_long" is context for the person reading the report, never a gate. The
Gate U and Layer 0 checks are deliberately absent — they say whether the data and the
instrument can be trusted at all, not which way price goes, and labelling them
bullish/bearish would be invented rather than derived.

Each factor's lean is one of supports_long | against_long | neutral | unknown. Unknown is
distinct from neutral (never null-coalesced): neutral means the evidence is there and
points nowhere, unknown means there isn't enough history to read it yet.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from statistics import median

from backend.compute.cohort import Cohort
from backend.compute.oi import aggregate_oi_series
from backend.compute.regime import Regime
from backend.core.config import Config
from backend.core.observation import Observation

SUPPORTS = "supports_long"
AGAINST = "against_long"
NEUTRAL = "neutral"
UNKNOWN = "unknown"


def _factor(name: str, value: str | None, lean: str, reason: str) -> dict:
    return {"factor": name, "value": value, "lean": lean, "reason": reason}


def _pct(x: Decimal) -> str:
    return f"{x * 100:+.1f}%"


def _value_at(series: list[Observation], at) -> Observation | None:
    """Latest observation at or before `at` (series sorted ascending)."""
    found = None
    for obs in series:
        if obs.observed_at > at:
            break
        found = obs
    return found


def _price_change(price_history: list[Observation], as_of, window: timedelta) -> Decimal | None:
    series = sorted(price_history, key=lambda o: o.observed_at)
    start, end = _value_at(series, as_of - window), _value_at(series, as_of)
    if start is None or end is None or start.value == 0:
        return None
    return (end.value - start.value) / start.value


def funding_factor(funding_current: Decimal | None, cfg: Config) -> dict:
    neutral_max = Decimal(str(cfg.get("directional_factors", "funding_neutral_max_pct", default=0.01)))
    name = "Funding"
    if funding_current is None:
        return _factor(name, None, UNKNOWN, "no current cross-venue funding reading")
    value = f"{funding_current:+.4f}% per 8h"
    if funding_current > neutral_max:
        return _factor(name, value, AGAINST, "longs are paying shorts to hold — the crowd is long, and a long pays carry")
    if funding_current < 0:
        return _factor(name, value, SUPPORTS, "shorts are paying longs to hold — the crowd is short, and a long is paid carry")
    return _factor(
        name, value, NEUTRAL, f"at or below the venues' default {neutral_max}% baseline — nobody is paying up to be crowded"
    )


def oi_price_factor(oi_history: list[Observation], price_history: list[Observation], as_of, cfg: Config) -> dict:
    """The OI/price quadrant over the window. Coin OI comes from aggregate_oi_series, which
    holds the venue set fixed across the window, so a venue dropping in or out of the
    collection is never mistaken for positions opening or closing."""
    hours = float(cfg.get("directional_factors", "window_hours", default=24))
    min_move = Decimal(str(cfg.get("directional_factors", "min_move_pct", default=0.01)))
    bar_seconds = int(cfg.get("cascade", "bar_seconds", default=900))
    window = timedelta(hours=hours)
    name = f"Open interest vs price ({hours:g}h)"

    start = as_of - window
    series = aggregate_oi_series(oi_history, bar_seconds, start=start)
    # The series must actually begin at the window start, not somewhere inside it.
    reaches_start = bool(series) and series[0][0] * bar_seconds <= start.timestamp() + 3600
    price_change = _price_change(price_history, as_of, window)
    if not reaches_start or series[0][1] == 0 or price_change is None:
        return _factor(name, None, UNKNOWN, f"needs {hours:g}h of collected OI and price history")

    oi_change = (series[-1][1] - series[0][1]) / series[0][1]
    value = f"OI {_pct(oi_change)}, price {_pct(price_change)}"
    oi_up, oi_down = oi_change >= min_move, oi_change <= -min_move
    px_up, px_down = price_change >= min_move, price_change <= -min_move
    if oi_up and px_up:
        return _factor(name, value, SUPPORTS, "new positions opening into a rising price — the move is backed by fresh longs")
    if oi_up and px_down:
        return _factor(name, value, AGAINST, "new positions opening into a falling price — shorts are pressing")
    if oi_down and px_up:
        return _factor(name, value, NEUTRAL, "price rising as positions close — short covering, which tends to fade once it's done")
    if oi_down and px_down:
        return _factor(
            name, value, NEUTRAL,
            "price falling as positions close — longs exiting; if it becomes a flush, that's the cascade setup's job to confirm",
        )
    flat = " and ".join(label for label, up, down in (("OI", oi_up, oi_down), ("price", px_up, px_down)) if not (up or down))
    return _factor(name, value, NEUTRAL, f"{flat} moved less than {min_move:.0%} — no positioning signal")


def cohort_factor(cohort: Cohort) -> dict:
    name = "Trapped cohort"
    if cohort == Cohort.TRAPPED_SHORTS:
        return _factor(name, cohort.value, SUPPORTS, "shorts are being forcibly closed — that is forced buying")
    if cohort == Cohort.TRAPPED_LONGS:
        return _factor(
            name, cohort.value, AGAINST,
            "longs are being forcibly closed — that is forced selling; once it has finished, that's the cascade setup",
        )
    return _factor(name, cohort.value, UNKNOWN, "no consistent liquidation side yet (needs activity in each of three 24h periods)")


def regime_factor(regime: Regime) -> dict:
    name = "Regime"
    if regime == Regime.TRENDING_UP:
        return _factor(name, regime.value, SUPPORTS, "confirmed uptrend — rising swing lows")
    if regime == Regime.TRENDING_DOWN:
        return _factor(name, regime.value, AGAINST, "confirmed downtrend — falling swing highs")
    if regime == Regime.MEAN_REVERTING:
        return _factor(name, regime.value, NEUTRAL, "ranging — direction comes from the range's edges, which the setups test")
    return _factor(name, regime.value, UNKNOWN, "neither a confirmed trend nor a confirmed range")


def _hourly_ratios(
    perp_history: list[Observation], spot_history: list[Observation], perp_venues: set[str], spot_venues: set[str]
) -> list[Decimal]:
    """Perp/spot volume ratio per hour, summed over exactly the venues contributing now —
    an hour missing any of them is skipped, so the baseline compares like with like."""
    def per_hour(history):
        buckets: dict[int, dict[str, Decimal]] = {}
        for obs in sorted(history, key=lambda o: o.observed_at):
            buckets.setdefault(int(obs.observed_at.timestamp() // 3600), {})[obs.venue] = obs.value
        return buckets

    perp, spot = per_hour(perp_history), per_hour(spot_history)
    ratios = []
    for hour in sorted(set(perp) & set(spot)):
        if not (perp_venues <= perp[hour].keys() and spot_venues <= spot[hour].keys()):
            continue
        spot_total = sum(spot[hour][v] for v in spot_venues)
        if spot_total > 0:
            ratios.append(sum(perp[hour][v] for v in perp_venues) / spot_total)
    return ratios


def perp_spot_factor(
    *,
    perp_now: dict[str, Decimal],
    spot_now: dict[str, Decimal],
    perp_history: list[Observation],
    spot_history: list[Observation],
    price_history: list[Observation],
    as_of,
    cfg: Config,
) -> dict:
    """Whether the current volume mix is heavier on perps or on spot than this coin's own
    recent norm, read together with which way price moved: a perp-heavy rally is
    leverage-driven and fragile, a perp-heavy selloff is a leverage flush that tends to
    fuel the rebound, and a spot-heavy move is backed by real buying or selling."""
    hours = float(cfg.get("directional_factors", "window_hours", default=24))
    min_move = Decimal(str(cfg.get("directional_factors", "min_move_pct", default=0.01)))
    baseline_days = float(cfg.get("directional_factors", "perp_spot_baseline_days", default=7))
    min_baseline_hours = int(cfg.get("directional_factors", "perp_spot_min_baseline_hours", default=48))
    band = Decimal(str(cfg.get("directional_factors", "perp_spot_band_pct", default=0.2)))
    name = "Perp/spot volume mix"

    spot_total = sum(spot_now.values(), Decimal(0))
    if not perp_now or spot_total == 0:
        return _factor(name, None, UNKNOWN, "no current perp or spot volume")
    ratio = sum(perp_now.values(), Decimal(0)) / spot_total

    floor = as_of - timedelta(days=baseline_days)
    ratios = _hourly_ratios(
        [o for o in perp_history if o.observed_at >= floor],
        [o for o in spot_history if o.observed_at >= floor],
        set(perp_now),
        set(spot_now),
    )
    price_change = _price_change(price_history, as_of, timedelta(hours=hours))
    if len(ratios) < min_baseline_hours or price_change is None:
        return _factor(
            name, f"{ratio:.1f}x", UNKNOWN,
            f"needs {min_baseline_hours}h of collected volume history to know this coin's usual mix "
            f"(have {len(ratios)}h)",
        )

    usual = median(ratios)
    value = f"{ratio:.1f}x vs usual {usual:.1f}x, price {_pct(price_change)} ({hours:g}h)"
    if abs(price_change) < min_move:
        return _factor(name, value, NEUTRAL, f"price moved less than {min_move:.0%} — no move for the mix to explain")
    rising = price_change > 0
    if ratio > usual * (1 + band):
        if rising:
            return _factor(name, value, AGAINST, "perp-heavy rally — driven by leverage rather than spot buying, so fragile")
        return _factor(name, value, SUPPORTS, "perp-heavy selloff — a leverage flush rather than spot selling, which tends to fuel a rebound")
    if ratio < usual * (1 - band):
        if rising:
            return _factor(name, value, SUPPORTS, "spot-heavy rally — backed by real buying")
        return _factor(name, value, AGAINST, "spot-heavy selloff — real selling, not just leverage unwinding")
    return _factor(name, value, NEUTRAL, f"within {band:.0%} of this coin's usual mix — the move isn't unusually leverage- or spot-driven")


def directional_factors(
    *,
    regime: Regime,
    cohort: Cohort,
    funding_current: Decimal | None,
    oi_history: list[Observation],
    price_history: list[Observation],
    perp_now: dict[str, Decimal],
    spot_now: dict[str, Decimal],
    perp_history: list[Observation],
    spot_history: list[Observation],
    as_of,
    cfg: Config,
) -> list[dict]:
    return [
        regime_factor(regime),
        cohort_factor(cohort),
        funding_factor(funding_current, cfg),
        oi_price_factor(oi_history, price_history, as_of, cfg),
        perp_spot_factor(
            perp_now=perp_now,
            spot_now=spot_now,
            perp_history=perp_history,
            spot_history=spot_history,
            price_history=price_history,
            as_of=as_of,
            cfg=cfg,
        ),
    ]
