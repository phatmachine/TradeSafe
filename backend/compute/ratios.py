"""Constraint ratios (doctrine 2.3) and the cost model (funding vs expected move). Every
ratio here is computed from observations already validated by Layer 0 (independence,
freshness, sync tolerance) — this module does no validation itself, only arithmetic, and
returns None (never a default) when an input is missing.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ConstraintRatios:
    oi_coin_total: Decimal | None
    oi_rate_of_change: Decimal | None       # fraction, e.g. 0.17 == +17%
    oi_to_market_cap: Decimal | None        # fraction
    perp_to_spot_volume: Decimal | None     # ratio, e.g. 3.5x
    carry_to_expected_move: Decimal | None  # fraction; see carry_ratio()


def oi_to_market_cap(oi_coin_total: Decimal | None, price: Decimal | None, market_cap: Decimal | None) -> Decimal | None:
    if market_cap is None or market_cap == 0:
        return None
    if oi_coin_total is None or price is None:
        return None
    return (oi_coin_total * price) / market_cap


def perp_to_spot_volume(perp_volume_coins: Decimal | None, spot_volume_coins: Decimal | None) -> Decimal | None:
    if spot_volume_coins is None or spot_volume_coins == 0:
        return None
    if perp_volume_coins is None:
        return None
    return perp_volume_coins / spot_volume_coins


def carry_ratio(
    *,
    funding_8h_pct: Decimal | None,
    expected_hold_days: Decimal,
    atr_value: Decimal | None,
    price: Decimal | None,
) -> Decimal | None:
    """(funding_8h * 3 periods/day * hold_days) / (ATR / price) — implementation spec's
    "Carry cost vs expected move" formula, doctrine's repaired funding rule (2.3): 10%
    annualised carry against 130% realised vol is not a constraint on anyone, and this
    ratio is what makes that comparison explicit and reproducible."""
    if funding_8h_pct is None or atr_value is None or price is None or price == 0:
        return None
    total_carry_pct = (funding_8h_pct / Decimal(100)) * 3 * expected_hold_days
    expected_move_pct = atr_value / price
    if expected_move_pct == 0:
        return None
    return total_carry_pct / expected_move_pct
