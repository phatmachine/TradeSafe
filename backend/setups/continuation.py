"""Layer 3 — trend continuation on leverage reset, in both directions. Added to the
doctrine 2026-09-17 to close a structural gap: the other setups are all mean-reverting,
so applied to a confirmed trend the doctrine could only ever output no-trade.

Long (trend_continuation_leverage_reset) — in a confirmed uptrend, buy the pullback where
all four hold: a 15-25% retrace without breaking the prior structural higher low; coin OI
falling during the retrace (leverage flushed, not added); funding normalising to <=0;
spot volume holding up vs perp. Invalidated by a close below the prior higher low with
coin OI rising — that is distribution, and means the thesis is dead.

Short (downtrend_continuation_leverage_reset, added 2026-09-18) — the exact mirror in a
confirmed downtrend: sell the rally where it retraces 15-25% without breaking the prior
structural lower high; coin OI falls during the rally (shorts covering, not new longs
levering in); funding normalises to >=0 (the short crowd has been flushed); spot volume
holds up vs perp. Invalidated by a close above the prior lower high with OI rising.

"Retrace" is a fraction of the last swing leg, not of price. The leg runs from the most
recent confirmed swing low (the "prior higher low") to the extreme close since it; the
retrace is how much of that leg price has given back. Changed 2026-09-18 from a
drop-from-the-5-hour-high reading, which was structurally unreachable: across 365 days of
4h bars, a confirmed uptrend coincided with that 15-25% drop 0 times on BTC, ETH, SOL
and ZEC (BTC's largest 5-hour drop all year was 8.6%). Read as a fraction of price from
the 30-day high it was still 0-7 times a year. As a fraction of the leg it is the
ordinary meaning of a pullback within a trend.

Structure is read on the regime classifier's own timeframe (swing_timeframe) with its
own swing lookback, so the higher low checked here is the same swing the classifier used
to call the trend in the first place.
"""
from __future__ import annotations

from decimal import Decimal

from backend.compute.funding import period_means
from backend.compute.oi import aggregate_oi_series
from backend.compute.ratios import perp_to_spot_volume
from backend.compute.regime import (
    find_swings,
    resample_closes,
    structural_higher_low,
    structural_lower_high,
    timeframe_seconds,
)
from backend.core.config import Config
from backend.core.observation import Observation
from backend.gates.common import ConditionResult, GateResult

SETUP_NAME = "trend_continuation_leverage_reset"
SHORT_SETUP_NAME = "downtrend_continuation_leverage_reset"


def evaluate(
    instrument: str,
    *,
    oi_history: list[Observation],
    price_history: list[Observation],
    funding_history: list[Observation],
    perp_volume_current: Decimal | None,
    spot_volume_current: Decimal | None,
    cfg: Config,
    side: str = "long",
) -> GateResult:
    if side not in ("long", "short"):
        raise ValueError(f"side must be 'long' or 'short', not {side!r}")
    is_long = side == "long"
    structure = "higher_low" if is_long else "lower_high"

    conditions: list[ConditionResult] = []
    tf_seconds = timeframe_seconds(str(cfg.get("swing_timeframe", default="4h")))
    bar_seconds = int(cfg.get("cascade", "bar_seconds", default=900))
    retrace_min = Decimal(str(cfg.get("continuation", "retrace_min_pct", default=0.15)))
    retrace_max = Decimal(str(cfg.get("continuation", "retrace_max_pct", default=0.25)))
    funding_periods = int(cfg.get("funding_periods", default=3))
    hl_margin = Decimal(str(cfg.get("hl_margin_pct", default=0.01)))
    lookback = int(cfg.get("swing_lookback_bars", default=5))

    bars = resample_closes(price_history, tf_seconds)
    closes = [c for _, c in bars]
    swings = find_swings(closes, lookback)
    anchors = [s for s in swings if s.kind == ("low" if is_long else "high")]

    # --- condition 1: retrace of the last leg within band, structure intact -------
    anchor = extreme_index = None
    if len(closes) < 2 or len(anchors) < 2:
        conditions.append(
            ConditionResult(
                name=f"retrace_within_band_{structure}_intact",
                status="unknown",
                computed_value=None,
                threshold=f"[{retrace_min}, {retrace_max}] of the last leg",
                detail=f"insufficient price/swing history ({len(anchors)} confirmed swing {'lows' if is_long else 'highs'}, 2 needed)",
            )
        )
    else:
        anchor = anchors[-1]
        tail = closes[anchor.index :]
        extreme = max(tail) if is_long else min(tail)
        extreme_index = anchor.index + tail.index(extreme)
        current = closes[-1]
        leg = (extreme - anchor.value) if is_long else (anchor.value - extreme)
        retrace = ((extreme - current) if is_long else (current - extreme)) / leg if leg > 0 else None
        if is_long:
            intact = structural_higher_low([a.value for a in anchors], hl_margin) and current > anchor.value
        else:
            intact = structural_lower_high([a.value for a in anchors], hl_margin) and current < anchor.value
        in_band = retrace is not None and retrace_min <= retrace <= retrace_max
        conditions.append(
            ConditionResult(
                name=f"retrace_within_band_{structure}_intact",
                status="pass" if in_band and intact else "fail",
                computed_value={
                    "retrace_of_leg": retrace,
                    "leg_from": anchor.value,
                    "leg_to": extreme,
                    "current": current,
                    f"{structure}_intact": intact,
                },
                threshold=f"[{retrace_min}, {retrace_max}] of the last leg, {structure.replace('_', ' ')} intact",
                detail=(
                    f"share of the move from the last swing {'low up to its high' if is_long else 'high down to its low'} "
                    "that price has given back, without breaking prior structure"
                ),
            )
        )

    # --- condition 2: coin OI falls during the retrace ----------------------------
    oi_series = aggregate_oi_series(oi_history, bar_seconds)
    oi_at_extreme = oi_now = None
    if extreme_index is not None and oi_series:
        start = bars[extreme_index][0] * tf_seconds
        in_extreme_bar = [v for b, v in oi_series if start <= b * bar_seconds < start + tf_seconds]
        if in_extreme_bar:
            oi_at_extreme, oi_now = in_extreme_bar[-1], oi_series[-1][1]
    if oi_at_extreme is None or oi_at_extreme == 0:
        conditions.append(
            ConditionResult(
                name="oi_falls_during_retrace",
                status="unknown",
                computed_value=None,
                threshold="< 0",
                detail=(
                    "no swing leg to measure from"
                    if extreme_index is None
                    else "OI history does not reach back to the leg's extreme bar"
                ),
            )
        )
    else:
        oi_change = (oi_now - oi_at_extreme) / oi_at_extreme
        conditions.append(
            ConditionResult(
                name="oi_falls_during_retrace",
                status="pass" if oi_change < 0 else "fail",
                computed_value=oi_change,
                threshold="< 0",
                detail=(
                    "coin OI change since the leg's extreme — leverage flushed, not added, during the "
                    + ("pullback" if is_long else "rally")
                ),
            )
        )

    # --- condition 3: funding normalises (<= 0 long, >= 0 short) ------------------
    recent_funding = period_means(funding_history, bar_seconds)[-funding_periods:]
    funding_threshold = "<= 0" if is_long else ">= 0"
    if len(recent_funding) < funding_periods:
        conditions.append(
            ConditionResult(
                name="funding_normalised",
                status="unknown",
                computed_value=None,
                threshold=f"{funding_threshold} for {funding_periods} periods",
                detail="insufficient funding history",
            )
        )
    else:
        normalised = all(f <= 0 for f in recent_funding) if is_long else all(f >= 0 for f in recent_funding)
        conditions.append(
            ConditionResult(
                name="funding_normalised",
                status="pass" if normalised else "fail",
                computed_value=recent_funding,
                threshold=f"{funding_threshold} for {funding_periods} periods",
                detail=(
                    f"cross-venue mean funding, last {funding_periods} periods — the "
                    + ("long" if is_long else "short")
                    + " crowd has been flushed"
                ),
            )
        )

    # --- condition 4: spot volume holds up vs perp --------------------------------
    ratio = perp_to_spot_volume(perp_volume_current, spot_volume_current)
    if ratio is None:
        conditions.append(
            ConditionResult(
                name="spot_volume_holds_up",
                status="unknown",
                computed_value=None,
                threshold="perp/spot not spiking",
                detail="insufficient volume data",
            )
        )
    else:
        ceiling = Decimal(str(cfg.get("gate_u", "perp_spot_ratio_ceiling", default=3.0)))
        conditions.append(
            ConditionResult(
                name="spot_volume_holds_up",
                status="pass" if ratio <= ceiling else "fail",
                computed_value=ratio,
                threshold=ceiling,
                detail="price discovery must not shift entirely into derivatives during the retrace",
            )
        )

    # --- invalidation (informational, not a pass/fail entry condition) -----------
    if anchor is not None and oi_at_extreme is not None:
        broke = closes[-1] < anchor.value if is_long else closes[-1] > anchor.value
        oi_rising = oi_now > oi_at_extreme
        conditions.append(
            ConditionResult(
                name="invalidation_not_triggered",
                status="fail" if broke and oi_rising else "pass",
                computed_value={f"closed_beyond_prior_{structure}": broke, "oi_rising": oi_rising},
                threshold=f"not (close beyond prior {structure.replace('_', ' ')} AND OI rising)",
                detail=(
                    "a close through prior structure with OI rising means "
                    + ("distribution" if is_long else "accumulation")
                    + ", not a cheap entry"
                ),
            )
        )

    return GateResult(gate=SETUP_NAME if is_long else SHORT_SETUP_NAME, conditions=tuple(conditions))
