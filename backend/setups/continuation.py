"""Layer 3 — trend continuation on leverage reset. Added to the doctrine 2026-09-17 to
close a structural gap: the other three setups are all mean-reverting, so applied to a
confirmed uptrend the doctrine could only ever output no-trade. In a confirmed trending
regime, buy the pullback where all four hold: a 15-25% retrace without breaking the
prior structural higher low; coin OI falling during the retrace (leverage flushed, not
added); funding normalising to <=0; spot volume holding up vs perp.

Invalidation (reported, not a condition): a close below the prior higher low with
coin-denominated OI rising — that is distribution, and means the thesis is dead.
"""
from __future__ import annotations

from decimal import Decimal

from backend.compute.oi import aggregate_oi_series, pct_change
from backend.compute.ratios import perp_to_spot_volume
from backend.compute.regime import find_swings, resample_closes, structural_higher_low
from backend.core.config import Config
from backend.core.observation import Metric, Observation
from backend.gates.common import ConditionResult, GateResult

SETUP_NAME = "trend_continuation_leverage_reset"


def evaluate(
    instrument: str,
    *,
    oi_history: list[Observation],
    price_history: list[Observation],
    funding_history: list[Observation],
    perp_volume_current: Decimal | None,
    spot_volume_current: Decimal | None,
    cfg: Config,
) -> GateResult:
    conditions: list[ConditionResult] = []
    bar_seconds = int(cfg.get("cascade", "bar_seconds", default=900))
    retrace_min = Decimal(str(cfg.get("continuation", "retrace_min_pct", default=0.15)))
    retrace_max = Decimal(str(cfg.get("continuation", "retrace_max_pct", default=0.25)))
    funding_periods = int(cfg.get("funding_periods", default=3))
    hl_margin = Decimal(str(cfg.get("hl_margin_pct", default=0.01)))
    lookback = int(cfg.get("swing_lookback_bars", default=5))

    bars = resample_closes(price_history, bar_seconds)
    closes = [c for _, c in bars]
    swings = find_swings(closes, lookback)
    swing_lows = [s.value for s in swings if s.kind == "low"]

    # --- condition 1: retrace within band, prior higher low intact ---------------
    if len(closes) < 2 or len(swing_lows) < 2:
        conditions.append(
            ConditionResult(
                name="retrace_within_band_higher_low_intact",
                status="unknown",
                computed_value=None,
                threshold=f"[{retrace_min}, {retrace_max}]",
                detail="insufficient price/swing history",
            )
        )
    else:
        recent_high = max(closes[-max(lookback * 4, 20) :])
        current = closes[-1]
        retrace = (recent_high - current) / recent_high if recent_high else None
        hl_intact = structural_higher_low(swing_lows, hl_margin)
        in_band = retrace is not None and retrace_min <= retrace <= retrace_max
        conditions.append(
            ConditionResult(
                name="retrace_within_band_higher_low_intact",
                status="pass" if in_band and hl_intact else "fail",
                computed_value={"retrace": retrace, "higher_low_intact": hl_intact},
                threshold=f"[{retrace_min}, {retrace_max}], higher low intact",
                detail="pullback depth vs the recent high, without breaking prior structure",
            )
        )

    # --- condition 2: coin OI falls during the retrace ----------------------------
    oi_series = aggregate_oi_series(oi_history, bar_seconds)
    if len(oi_series) < 2:
        conditions.append(
            ConditionResult(
                name="oi_falls_during_retrace",
                status="unknown",
                computed_value=None,
                threshold="< 0",
                detail="insufficient OI history",
            )
        )
    else:
        oi_change = pct_change(oi_series[0][1], oi_series[-1][1])
        conditions.append(
            ConditionResult(
                name="oi_falls_during_retrace",
                status="pass" if oi_change is not None and oi_change < 0 else "fail",
                computed_value=oi_change,
                threshold="< 0",
                detail="leverage flushed, not added, during the pullback",
            )
        )

    # --- condition 3: funding normalises to <= 0 ----------------------------------
    recent_funding = [o.value for o in sorted(funding_history, key=lambda o: o.observed_at)][-funding_periods:]
    if len(recent_funding) < funding_periods:
        conditions.append(
            ConditionResult(
                name="funding_normalised",
                status="unknown",
                computed_value=None,
                threshold="<= 0",
                detail="insufficient funding history",
            )
        )
    else:
        conditions.append(
            ConditionResult(
                name="funding_normalised",
                status="pass" if all(f <= 0 for f in recent_funding) else "fail",
                computed_value=recent_funding,
                threshold="<= 0",
                detail=f"last {funding_periods} periods",
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
                detail="price discovery must not shift entirely into derivatives during the pullback",
            )
        )

    # --- invalidation (informational, not a pass/fail entry condition) -----------
    if len(closes) >= 1 and len(swing_lows) >= 1 and len(oi_series) >= 2:
        prior_higher_low = swing_lows[-1]
        closed_below = closes[-1] < prior_higher_low
        oi_rising = oi_series[-1][1] > oi_series[0][1]
        invalidated = closed_below and oi_rising
        conditions.append(
            ConditionResult(
                name="invalidation_not_triggered",
                status="pass" if not invalidated else "fail",
                computed_value={"closed_below_prior_higher_low": closed_below, "oi_rising": oi_rising},
                threshold="not (close below prior higher low AND OI rising)",
                detail="a close below the prior higher low with OI rising means distribution, not a cheap entry",
            )
        )

    return GateResult(gate=SETUP_NAME, conditions=tuple(conditions))
