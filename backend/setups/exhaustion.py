"""Layer 3 — positioning exhaustion. Requires all three conditions, never funding alone
(doctrine: "This is the rule v1 got wrong"): carry cost material relative to expected
move, open interest rising into a failing price, and a structural break (a lower high or
a higher low).
"""
from __future__ import annotations

from decimal import Decimal

from backend.compute.oi import aggregate_oi_series, pct_change
from backend.compute.ratios import carry_ratio
from backend.compute.regime import find_swings, resample_closes
from backend.compute.volatility import atr as atr_fn
from backend.core.config import Config
from backend.core.observation import Metric, Observation
from backend.gates.common import ConditionResult, GateResult

SETUP_NAME = "positioning_exhaustion"


def evaluate(
    instrument: str,
    *,
    oi_history: list[Observation],
    price_history: list[Observation],
    funding_current: Decimal | None,
    price_current: Decimal | None,
    cfg: Config,
) -> GateResult:
    conditions: list[ConditionResult] = []
    bar_seconds = int(cfg.get("cascade", "bar_seconds", default=900))
    atr_n = int(cfg.get("atr_n", default=14))
    carry_material = Decimal(str(cfg.get("carry_ratio_material", default=0.15)))
    expected_hold_days = Decimal(str(cfg.get("expected_hold_days_default", default=10)))
    break_lookback = int(cfg.get("exhaustion", "structural_break_lookback_bars", default=20))

    bars = resample_closes(price_history, bar_seconds)
    closes = [c for _, c in bars]

    # --- condition 1: carry cost material ----------------------------------------
    ohlc = [(c, c, c) for c in closes]  # true-range proxy from a close-only series
    atr_value = atr_fn(ohlc, atr_n) if len(ohlc) >= atr_n + 1 else None
    ratio = carry_ratio(
        funding_8h_pct=funding_current, expected_hold_days=expected_hold_days, atr_value=atr_value, price=price_current
    )
    if ratio is None:
        conditions.append(
            ConditionResult(
                name="carry_material",
                status="unknown",
                computed_value=None,
                threshold=carry_material,
                detail="insufficient funding, ATR or price data",
            )
        )
    else:
        conditions.append(
            ConditionResult(
                name="carry_material",
                status="pass" if ratio >= carry_material else "fail",
                computed_value=ratio,
                threshold=carry_material,
                detail="carry cost vs expected move (2.3) — never on funding alone",
            )
        )

    # --- condition 2: OI rising into a failing price -----------------------------
    oi_series = aggregate_oi_series(oi_history, bar_seconds)
    if len(oi_series) < 2 or len(closes) < 2:
        conditions.append(
            ConditionResult(
                name="oi_rising_into_failing_price",
                status="unknown",
                computed_value=None,
                threshold="OI up, price down",
                detail="insufficient OI or price history",
            )
        )
    else:
        oi_change = pct_change(oi_series[0][1], oi_series[-1][1])
        price_change = pct_change(closes[0], closes[-1])
        rising_into_failure = (
            oi_change is not None and price_change is not None and oi_change > 0 and price_change < 0
        )
        conditions.append(
            ConditionResult(
                name="oi_rising_into_failing_price",
                status="pass" if rising_into_failure else "fail",
                computed_value={"oi_change": oi_change, "price_change": price_change},
                threshold="OI up, price down",
                detail="quadrant reading (doctrine Layer 2.1 table): leverage building against price",
            )
        )

    # --- condition 3: structural break (lower high or higher low) ---------------
    swings = find_swings(closes, lookback=max(1, break_lookback // 4))
    highs = [s.value for s in swings if s.kind == "high"]
    lows = [s.value for s in swings if s.kind == "low"]
    lower_high = len(highs) >= 2 and highs[-1] < highs[-2]
    higher_low = len(lows) >= 2 and lows[-1] > lows[-2]
    if len(highs) < 2 and len(lows) < 2:
        conditions.append(
            ConditionResult(
                name="structural_break",
                status="unknown",
                computed_value=None,
                threshold="lower high or higher low",
                detail="insufficient confirmed swing points",
            )
        )
    else:
        conditions.append(
            ConditionResult(
                name="structural_break",
                status="pass" if (lower_high or higher_low) else "fail",
                computed_value={"lower_high": lower_high, "higher_low": higher_low},
                threshold="lower high or higher low",
                detail="a break in market structure, not funding, confirms exhaustion",
            )
        )

    return GateResult(gate=SETUP_NAME, conditions=tuple(conditions))
