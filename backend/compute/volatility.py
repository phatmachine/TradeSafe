"""Realised volatility and ATR. Doctrine Layer 1 requires "realised volatility across
multiple windows" as ground truth; Layer 4/2.3 sizing and carry-materiality math is
expressed in ATR terms. All functions are pure over a price series already restricted to
a single venue's own prints (never-mix-venues) and a fixed as_of horizon.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from backend.core.observation import Observation

ANNUALISATION_FACTOR_DAILY = math.sqrt(365)


def daily_closes(observations: list[Observation]) -> list[Decimal]:
    """Last observation per UTC calendar day, from a single venue's series, sorted
    oldest-first. Callers must pre-filter to one venue — mixing venues into one price
    series is exactly the corruption the never-mix-venues rule exists to prevent."""
    by_day: dict[object, Observation] = {}
    for obs in observations:
        day = obs.observed_at.date()
        cur = by_day.get(day)
        if cur is None or obs.observed_at > cur.observed_at:
            by_day[day] = obs
    return [by_day[day].value for day in sorted(by_day.keys())]


@dataclass(frozen=True)
class PricePoint:
    observed_at: "object"  # datetime, kept generic to avoid a hard import cycle
    price: Decimal


def log_returns(prices: list[Decimal]) -> list[float]:
    out: list[float] = []
    for prev, cur in zip(prices, prices[1:]):
        if prev <= 0 or cur <= 0:
            continue
        out.append(math.log(float(cur) / float(prev)))
    return out


def realised_volatility_annualised(prices: list[Decimal]) -> float | None:
    """Annualised stdev of log returns. None (unknown) with fewer than 2 usable returns
    — never a guessed volatility from insufficient history."""
    returns = log_returns(prices)
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    stdev = math.sqrt(variance)
    return stdev * ANNUALISATION_FACTOR_DAILY


def true_range(high: Decimal, low: Decimal, prev_close: Decimal) -> Decimal:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr(bars: list[tuple[Decimal, Decimal, Decimal]], n: int) -> Decimal | None:
    """bars: list of (high, low, close), oldest first, all from one venue. Simple moving
    average of true range over the last n bars. None if fewer than n+1 bars available."""
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        high, low, _ = bars[i]
        _, _, prev_close = bars[i - 1]
        trs.append(true_range(high, low, prev_close))
    last_n = trs[-n:]
    return sum(last_n, Decimal(0)) / n
