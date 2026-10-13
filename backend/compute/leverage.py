"""Leverage risk: pure arithmetic over a single venue's stored closes. How far price must
move against a position before it is liquidated, and how often a move that large
happened within a hold window in this coin's own recent history.

Report context only (report/context.py, section 8). Nothing in gates/, setups/ or the
verdict reads this, and it never produces a size or a leverage to use. It carries
@decision_path like every compute function that takes observations, so belief data can't
reach it either.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from backend.compute.regime import resample_closes
from backend.core.firewall import decision_path
from backend.core.observation import Observation


def liquidation_move(leverage: float, maintenance_margin_rate: float) -> float:
    """Fraction of the entry price an isolated position can move against it before its
    margin falls to maintenance: (1 - leverage * rate) / leverage. 5x at 1% is 19%."""
    if leverage <= 0:
        raise ValueError("leverage must be positive")
    return max(0.0, (1.0 - leverage * maintenance_margin_rate) / leverage)


@dataclass(frozen=True)
class LeverageRow:
    leverage: float
    liquidating_move: float
    long_breach_rate: float | None   # share of hold windows where a long would have been liquidated
    short_breach_rate: float | None
    move_vs_typical: float | None    # liquidating move / typical hold-window move now


@dataclass(frozen=True)
class LeverageRisk:
    rows: tuple[LeverageRow, ...]
    windows: int                      # hold windows counted (one per bar with a full window after it)
    history_days: float               # span of the closes used
    daily_volatility: float | None    # stdev of daily log returns, from the bar closes
    typical_hold_move: float | None   # daily_volatility * sqrt(hold_days)
    status: str                       # "ok" | "insufficient_history"


@decision_path
def leverage_risk(
    price_history: list[Observation],
    *,
    leverages: list[float],
    maintenance_margin_rate: float,
    hold_days: float,
    bar_seconds: int,
    min_history_days: float,
) -> LeverageRisk:
    """`price_history` must be one venue's series (never-mix-venues). Closes stand in for
    the bar's range, so brief wicks are missed and the breach rates are a floor: real
    liquidations would have been at least this frequent."""
    bars = resample_closes(price_history, bar_seconds)
    closes = [float(c) for _, c in bars if c and c > 0]
    history_days = (bars[-1][0] - bars[0][0]) * bar_seconds / 86400 if len(bars) > 1 else 0.0
    hold_bars = max(1, int(round(hold_days * 86400 / bar_seconds)))
    moves = [liquidation_move(lev, maintenance_margin_rate) for lev in leverages]

    if history_days < min_history_days or len(closes) <= hold_bars + 1:
        return LeverageRisk(
            rows=tuple(LeverageRow(lev, m, None, None, None) for lev, m in zip(leverages, moves)),
            windows=0,
            history_days=history_days,
            daily_volatility=None,
            typical_hold_move=None,
            status="insufficient_history",
        )

    # Worst move against a long and against a short inside each hold window.
    down, up = [], []
    for i in range(len(closes) - hold_bars):
        entry = closes[i]
        window = closes[i + 1 : i + hold_bars + 1]
        down.append((entry - min(window)) / entry)
        up.append((max(window) - entry) / entry)
    windows = len(down)

    returns = [math.log(b / a) for a, b in zip(closes, closes[1:])]
    if len(returns) >= 2:
        mean = sum(returns) / len(returns)
        bar_sd = math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1))
        daily_vol = bar_sd * math.sqrt(86400 / bar_seconds)
        typical = daily_vol * math.sqrt(hold_days)
    else:
        daily_vol = typical = None

    rows = tuple(
        LeverageRow(
            leverage=lev,
            liquidating_move=m,
            long_breach_rate=sum(1 for d in down if d >= m) / windows,
            short_breach_rate=sum(1 for u in up if u >= m) / windows,
            move_vs_typical=(m / typical) if typical else None,
        )
        for lev, m in zip(leverages, moves)
    )
    return LeverageRisk(rows, windows, history_days, daily_vol, typical, "ok")

