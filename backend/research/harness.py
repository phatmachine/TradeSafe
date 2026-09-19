"""Offline calibration: runs the live regime classifier and setup code over years of free
history (research/store.py, filled by research/backfill.py) and scores what each setup
would have signalled against what price then did.

What it is, and what it deliberately is not:
- It calls the same regime_mod.classify and cascade/continuation/exhaustion.evaluate the
  live report calls, with the same inputs report/contract.py builds (a 30-day single-venue
  price history, coin OI history, funding, current perp and spot volume), in the same
  regime-gated order. Thresholds come from the same Config, and a sweep overrides one at
  a time with the same with_override the live-store sweep uses.
- It does NOT replay Gate U or Layer 0. Those check order-book depth, per-venue
  freshness and cross-venue agreement, none of which exists historically. Every instant
  is evaluated as if the data had been trustworthy — a stated departure from the
  doctrine's "same code path" replay (replay/sweep.py), which remains the tool for
  periods the collector itself recorded.
- Inputs are narrower than live: open interest is Bybit's alone (compared only as
  percentage changes); funding is each venue's last settled rate carried forward, where
  live polls the running rate; perp/spot volume is Binance's alone.
- No liquidation history exists for free, so cascade/squeeze absorption are scored on
  every condition except liquidation_print_settled, and no trapped cohort is named. The
  reports say so wherever those setups appear.
- Event decompression runs over the macro calendar (`python -m backend.research events`).
  The setup doesn't claim a direction, so it is scored as a hypothesis the live path
  never states: price moves against whoever was crowded going into the event, judged
  against a random entry taking the same sides.

Signals are counted as episodes: a run of consecutive grid instants where a setup
qualifies is one signal, entered at its first instant, so a setup that stays true for six
hours isn't scored as six wins.
"""
from __future__ import annotations

import statistics
from bisect import bisect_left, bisect_right
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from backend.compute import regime as regime_mod
from backend.core.config import Config, load_config, with_override
from backend.core.observation import MACRO, Metric
from backend.research import store
from backend.setups import cascade, continuation, event, exhaustion
from backend.sources.calendar import EVENT_KINDS

BAR_SECONDS = 900
# Thresholds the regime classifier reads. Sweeping anything else reuses one regime pass.
REGIME_PARAMS = {
    "swing_timeframe", "swing_lookback_bars", "n_swings_required", "hl_margin_pct",
    "trend_ratio_max", "rv_short_days", "rv_long_days",
}
UNSCORED_CONDITION = "liquidation_print_settled"  # no free liquidation history
SETUP_SIDE = {
    cascade.SETUP_NAME: 1,
    continuation.SETUP_NAME: 1,
    cascade.SHORT_SETUP_NAME: -1,
    continuation.SHORT_SETUP_NAME: -1,
    exhaustion.SETUP_NAME: None,  # doesn't encode a direction: reported as "% price rose"
    event.SETUP_NAME: None,  # side set per signal: against the crowd going into the event
}
CROWD_FADE = {event.SETUP_NAME}
REGIME_FWD_DAYS = 7


class Point:
    """Stand-in for an Observation carrying only what regime and setup code read."""

    __slots__ = ("metric", "venue", "observed_at", "value")

    def __init__(self, metric: Metric, venue: str, ts: int, value: Decimal):
        self.metric = metric
        self.venue = venue
        self.observed_at = datetime.fromtimestamp(ts, tz=timezone.utc)
        self.value = value


@dataclass
class History:
    instrument: str
    price: list[Point]
    price_ts: list[int]
    oi: list[Point]
    oi_ts: list[int]
    funding: dict[str, tuple[list[int], list[Decimal]]]
    volume_ts: list[int]
    perp_cum: list[Decimal]
    spot_cum: list[Decimal]
    events: list[Point] = field(default_factory=list)
    events_ts: list[int] = field(default_factory=list)


def _cumulative(rows: list[tuple[int, Decimal]]) -> tuple[list[int], list[Decimal]]:
    ts, cum, total = [], [], Decimal(0)
    for t, v in rows:
        total += v
        ts.append(t)
        cum.append(total)
    return ts, cum


def load_history(instrument: str) -> History:
    with store.connection() as conn:
        price = store.load(conn, instrument, store.PRICE, "binance")
        oi = store.load(conn, instrument, store.OI, "bybit")
        funding = {v: store.load(conn, instrument, store.FUNDING, v) for v in ("binance", "bybit")}
        perp = store.load(conn, instrument, store.PERP_VOLUME, "binance")
        spot = dict(store.load(conn, instrument, store.SPOT_VOLUME, "binance"))
        events = sorted((t, kind) for kind in EVENT_KINDS for t, _ in store.load(conn, MACRO, store.EVENT, kind))
    if not price:
        raise SystemExit(f"no research history for {instrument} — run: python -m backend.research backfill {instrument}")
    # Perp and spot bars are paired by close time so both running sums share one index.
    paired = [(t, pv, spot[t]) for t, pv in perp if t in spot]
    volume_ts, perp_cum = _cumulative([(t, pv) for t, pv, _ in paired])
    _, spot_cum = _cumulative([(t, sv) for t, _, sv in paired])
    return History(
        instrument=instrument,
        price=[Point(Metric.PRICE, "binance", t, v) for t, v in price],
        price_ts=[t for t, _ in price],
        oi=[Point(Metric.OI_COIN, "bybit", t, v) for t, v in oi],
        oi_ts=[t for t, _ in oi],
        funding={v: ([t for t, _ in rows], [x for _, x in rows]) for v, rows in funding.items() if rows},
        volume_ts=volume_ts,
        perp_cum=perp_cum,
        spot_cum=spot_cum,
        events=[Point(Metric.EVENT, kind, t, Decimal(1)) for t, kind in events],
        events_ts=[t for t, _ in events],
    )


def _window(points: list[Point], ts: list[int], start: int, end: int) -> list[Point]:
    return points[bisect_left(ts, start) : bisect_right(ts, end)]


def _latest(ts: list[int], values: list, at: int):
    i = bisect_right(ts, at) - 1
    return values[i] if i >= 0 else None


def _price_at(h: History, at: int) -> Decimal | None:
    p = _latest(h.price_ts, h.price, at)
    return p.value if p is not None else None


def _rolling_sum(ts: list[int], cum: list[Decimal], end: int, seconds: int) -> Decimal | None:
    hi = bisect_right(ts, end) - 1
    lo = bisect_right(ts, end - seconds) - 1
    if hi < 0 or lo < 0:
        return None  # the window reaches back before the history starts
    return cum[hi] - cum[lo]


def _funding_points(h: History, t: int, periods: int = 8) -> list[Point]:
    """Each venue's last settled rate carried forward onto the last few 15-minute
    periods — the shape the live collector's continuous funding polls have."""
    out = []
    for venue, (ts, values) in h.funding.items():
        for k in range(periods - 1, -1, -1):
            at = t - k * BAR_SECONDS
            v = _latest(ts, values, at)
            if v is not None:
                out.append(Point(Metric.FUNDING_8H, venue, at, v))
    return out


def _funding_current(h: History, t: int) -> Decimal | None:
    latest = [v for ts, values in h.funding.values() if (v := _latest(ts, values, t)) is not None]
    return sum(latest, Decimal(0)) / len(latest) if latest else None


@dataclass
class Instant:
    t: int
    regime: str
    setups: dict[str, tuple[bool, dict[str, str]]] = field(default_factory=dict)  # name -> (qualified, statuses)
    sides: dict[str, int] = field(default_factory=dict)  # CROWD_FADE setups: the side each would take


def evaluate(h: History, cfg: Config, t: int, regime: regime_mod.Regime | None = None) -> Instant:
    """The post-gate half of report/contract.run_analysis, at instant t."""
    lookback = int(cfg.get("rv_long_days", default=30)) * 86400
    price_w = _window(h.price, h.price_ts, t - lookback, t)
    if regime is None:
        regime = regime_mod.classify(price_w, cfg=cfg).regime
    out = Instant(t=t, regime=regime.value)
    if regime == regime_mod.Regime.UNDETERMINED:
        return out

    as_of = datetime.fromtimestamp(t, tz=timezone.utc)
    oi_w = _window(h.oi, h.oi_ts, t - lookback, t)
    funding_w = _funding_points(h, t)

    def record(name: str, result) -> None:
        statuses = {c.name: c.status for c in result.conditions}
        qualified = all(s == "pass" for n, s in statuses.items() if n != UNSCORED_CONDITION)
        out.setups[name] = (qualified, statuses)

    if regime == regime_mod.Regime.MEAN_REVERTING:
        for side, name in (("long", cascade.SETUP_NAME), ("short", cascade.SHORT_SETUP_NAME)):
            record(name, cascade.evaluate(h.instrument, oi_history=oi_w, price_history=price_w,
                                          funding_history=funding_w, liquidation_history=[], as_of=as_of,
                                          cfg=cfg, side=side))
        record(exhaustion.SETUP_NAME, exhaustion.evaluate(
            h.instrument, oi_history=oi_w, price_history=price_w, funding_current=_funding_current(h, t),
            price_current=_price_at(h, t), cfg=cfg))
    else:
        side, name = (("long", continuation.SETUP_NAME) if regime == regime_mod.Regime.TRENDING_UP
                      else ("short", continuation.SHORT_SETUP_NAME))
        record(name, continuation.evaluate(
            h.instrument, oi_history=oi_w, price_history=price_w, funding_history=funding_w,
            perp_volume_current=_rolling_sum(h.volume_ts, h.perp_cum, t, 86400),
            spot_volume_current=_rolling_sum(h.volume_ts, h.spot_cum, t, 86400),
            cfg=cfg, side=side))

    # Event decompression runs in every determined regime, as it does live. Funding points
    # reach back past the resolved window so the periods going into the event are there.
    resolved_s = float(cfg.get("event", "resolved_within_hours", default=24)) * 3600
    periods = int(resolved_s // BAR_SECONDS) + int(cfg.get("funding_periods", default=3)) + 1
    result = event.evaluate(
        h.instrument, event_history=_window(h.events, h.events_ts, t - lookback, t),
        funding_history=_funding_points(h, t, periods=periods), as_of=as_of, cfg=cfg)
    record(event.SETUP_NAME, result)
    positioning = next(c for c in result.conditions if c.name == "one_sided_positioning_verifiable")
    if positioning.status == "pass":
        out.sides[event.SETUP_NAME] = -1 if positioning.computed_value[0] > 0 else 1
    return out


def grid(h: History, cfg: Config, step_hours: float) -> list[int]:
    lookback = int(cfg.get("rv_long_days", default=30)) * 86400
    first = h.price_ts[0] + lookback
    step = int(step_hours * 3600)
    first = -(-first // step) * step
    return list(range(first, h.price_ts[-1] + 1, step))


def run(h: History, cfg: Config, points: list[int], regimes: list | None = None) -> list[Instant]:
    return [evaluate(h, cfg, t, regimes[i] if regimes else None) for i, t in enumerate(points)]


# --- scoring ---------------------------------------------------------------------------


@dataclass
class SetupScore:
    evaluated: int = 0
    qualified_instants: int = 0
    episodes: int = 0
    scored: list[Decimal] = field(default_factory=list)  # forward return in the setup's direction
    sides: list[int] = field(default_factory=list)  # CROWD_FADE setups: the side of each scored signal
    condition_pass: dict[str, int] = field(default_factory=dict)
    condition_unknown: dict[str, int] = field(default_factory=dict)


@dataclass
class CoinResult:
    instrument: str
    months: float
    regimes: dict[str, int]
    regime_fwd: dict[str, list[Decimal]]
    setups: dict[str, SetupScore]
    # Forward return from EVERY grid instant, per hold length: the base rate a setup's
    # hit rate has to beat. Without it a long setup looks good in any rising market.
    base_fwd: dict[float, list[Decimal]] = field(default_factory=dict)


def score(h: History, cfg: Config, instants: list[Instant]) -> CoinResult:
    hold = cfg.get("expected_hold_window_days", default={})
    last_ts = h.price_ts[-1]

    def fwd(t: int, days: float) -> Decimal | None:
        if t + days * 86400 > last_ts:
            return None
        p0, p1 = _price_at(h, t), _price_at(h, int(t + days * 86400))
        return (p1 - p0) / p0 if p0 and p1 else None

    regimes: dict[str, int] = {}
    regime_fwd: dict[str, list[Decimal]] = {}
    setups: dict[str, SetupScore] = {}
    previously_qualified: set[str] = set()
    for inst in instants:
        regimes[inst.regime] = regimes.get(inst.regime, 0) + 1
        r = fwd(inst.t, REGIME_FWD_DAYS)
        if r is not None:
            regime_fwd.setdefault(inst.regime, []).append(r)
        now_qualified = set()
        for name, (qualified, statuses) in inst.setups.items():
            s = setups.setdefault(name, SetupScore())
            s.evaluated += 1
            for cond, status in statuses.items():
                if status == "pass":
                    s.condition_pass[cond] = s.condition_pass.get(cond, 0) + 1
                elif status == "unknown":
                    s.condition_unknown[cond] = s.condition_unknown.get(cond, 0) + 1
            if not qualified:
                continue
            s.qualified_instants += 1
            now_qualified.add(name)
            if name in previously_qualified:
                continue  # same episode, already entered
            s.episodes += 1
            ret = fwd(inst.t, float(hold.get(name, 10)))
            if ret is not None:
                side = inst.sides.get(name) if name in CROWD_FADE else SETUP_SIDE.get(name)
                s.scored.append(ret * side if side else ret)
                if name in CROWD_FADE:
                    s.sides.append(side)
        previously_qualified = now_qualified
    months = (instants[-1].t - instants[0].t) / (86400 * 30.44) if len(instants) > 1 else 0.0
    base_fwd: dict[float, list[Decimal]] = {}
    for days in {float(hold.get(name, 10)) for name in SETUP_SIDE}:
        base_fwd[days] = [r for inst in instants if (r := fwd(inst.t, days)) is not None]
    return CoinResult(h.instrument, months, regimes, regime_fwd, setups, base_fwd)


# --- runs (one process per coin) -------------------------------------------------------


def _coin_job(instrument: str, param: tuple[str, ...] | None, values: list, step_hours: float) -> list[CoinResult]:
    base = load_config()
    h = load_history(instrument)
    points = grid(h, base, step_hours)
    if param is None:
        return [score(h, base, run(h, base, points))]
    shared_regimes = None
    if param[0] not in REGIME_PARAMS:
        # The swept value can't change the regime, so classify once and reuse it.
        lookback = int(base.get("rv_long_days", default=30)) * 86400
        shared_regimes = [
            regime_mod.classify(_window(h.price, h.price_ts, t - lookback, t), cfg=base).regime for t in points
        ]
    results = []
    for value in values:
        cfg = with_override(base, param, value)
        results.append(score(h, cfg, run(h, cfg, points, shared_regimes)))
    return results


def _run_all(symbols: list[str], param, values, step_hours) -> dict[str, list[CoinResult]]:
    with ProcessPoolExecutor(max_workers=min(len(symbols), 4)) as pool:
        futures = {s: pool.submit(_coin_job, s, param, values, step_hours) for s in symbols}
        return {s: f.result() for s, f in futures.items()}


# --- reports -----------------------------------------------------------------------------


def _pct(n: int, d: int) -> str:
    return f"{100 * n / d:5.1f}%" if d else "    -"


def _base_rate(res: CoinResult, name: str, hold_days: float) -> tuple[float, int]:
    """(wins, total) for a random entry held as long as this setup, on its side. A
    CROWD_FADE setup takes both sides, so its random entry takes them in the same mix."""
    base = res.base_fwd.get(hold_days, [])
    if name in CROWD_FADE:
        s = res.setups.get(name)
        sides = s.sides if s else []
        if not sides or not base:
            return 0, 0
        up = sum(1 for r in base if r > 0) / len(base)
        down = sum(1 for r in base if r < 0) / len(base)
        return sum(up if x > 0 else down for x in sides), len(sides)
    side = SETUP_SIDE.get(name) or 1
    return sum(1 for r in base if r * side > 0), len(base)


def _label(name: str) -> str:
    if name in CROWD_FADE:
        return "went against the pre-event crowd"
    return "price rose" if SETUP_SIDE.get(name) is None else "went its way"


def _setup_line(name: str, scores: list[tuple[str, CoinResult]], hold_days: float) -> list[str]:
    lines = []
    pooled, base_wins, base_total = [], 0, 0
    for coin, res in scores:
        s = res.setups.get(name)
        if s is None:
            lines.append(f"    {coin:<5} never evaluated (its regime never occurred)")
            continue
        pooled.extend(s.scored)
        bw, bt = _base_rate(res, name, hold_days)
        base_wins, base_total = base_wins + bw, base_total + bt
        lines.append("    " + _setup_stats(coin, s, res.months, name, bw, bt))
    if pooled:
        wins = sum(1 for r in pooled if r > 0)
        lines.append(f"    {'all':<5} {len(pooled)} scored signals: {_label(name)} {_pct(wins, len(pooled))} "
                     f"vs {_pct(base_wins, base_total).strip()} for a random entry, "
                     f"median {statistics.median(pooled) * 100:+.1f}%, mean {statistics.mean(pooled) * 100:+.1f}%")
    return lines


def _setup_stats(coin: str, s: SetupScore, months: float, name: str, base_wins: int, base_total: int) -> str:
    per_month = s.episodes / months if months else 0
    if s.scored:
        wins = sum(1 for r in s.scored if r > 0)
        tail = (f"{_label(name)} {_pct(wins, len(s.scored))} vs {_pct(base_wins, base_total).strip()} random, "
                f"median {statistics.median(s.scored) * 100:+.1f}% ({len(s.scored)} scored)")
    else:
        tail = "no scored signals"
    return f"{coin:<5} evaluated {s.evaluated:>6,}h, signals {s.episodes:>3} ({per_month:.2f}/month), {tail}"


def baseline_report(symbols: list[str], *, step_hours: float = 1.0) -> str:
    results = {s: r[0] for s, r in _run_all(symbols, None, [None], step_hours).items()}
    cfg = load_config()
    out = [f"BASELINE — current thresholds (config {cfg.config_hash}), grid every {step_hours:g}h", ""]
    out.append("Regime share, and how price did over the next 7 days in each regime:")
    for coin, res in results.items():
        total = sum(res.regimes.values())
        parts = []
        for reg in ("trending_up", "trending_down", "mean_reverting", "undetermined"):
            f = res.regime_fwd.get(reg, [])
            up = f" (next 7d up {_pct(sum(1 for r in f if r > 0), len(f)).strip()})" if f else ""
            parts.append(f"{reg} {_pct(res.regimes.get(reg, 0), total).strip()}{up}")
        out.append(f"  {coin:<5} {res.months:.0f} months: " + " | ".join(parts))
    out.append("")
    names = [n for n in SETUP_SIDE if any(n in r.setups for r in results.values())]
    for name in names:
        note = "  [scored without the liquidation check — no free history]" if name in (
            cascade.SETUP_NAME, cascade.SHORT_SETUP_NAME) else ""
        if name in CROWD_FADE:
            note = "  [scored as fading the pre-event crowd — a hypothesis the live report doesn't state]"
        hold = cfg.get("expected_hold_window_days", default={}).get(name, 10)
        out.append(f"{name} (held {hold}d){note}")
        out.extend(_setup_line(name, list(results.items()), float(hold)))
        # Which condition blocks it most: pooled pass rate per condition.
        passes, unknowns, evaluated = {}, {}, 0
        for res in results.values():
            s = res.setups.get(name)
            if not s:
                continue
            evaluated += s.evaluated
            for c, n in s.condition_pass.items():
                passes[c] = passes.get(c, 0) + n
            for c, n in s.condition_unknown.items():
                unknowns[c] = unknowns.get(c, 0) + n
        conds = sorted(set(passes) | set(unknowns), key=lambda c: passes.get(c, 0))
        out.append("    condition pass rates (lowest = the bottleneck): " + ", ".join(
            f"{c} {_pct(passes.get(c, 0), evaluated).strip()}" + (" [not scored]" if c == UNSCORED_CONDITION else "")
            for c in conds))
        out.append("")
    return "\n".join(out)


def sweep_report(symbols: list[str], param: tuple[str, ...], values: list, *, step_hours: float = 1.0) -> str:
    all_results = _run_all(symbols, param, values, step_hours)
    out = [f"SWEEP {'.'.join(param)} over {values}, grid every {step_hours:g}h", ""]
    for i, value in enumerate(values):
        per_coin = [(coin, res[i]) for coin, res in all_results.items()]
        out.append(f"== {'.'.join(param)} = {value}")
        out.append("  regimes (share of hours; % of the time price was up 7 days later):")
        for coin, res in per_coin:
            total = sum(res.regimes.values())
            parts = []
            for reg in ("trending_up", "trending_down", "mean_reverting", "undetermined"):
                f = res.regime_fwd.get(reg, [])
                up = f" [7d up {_pct(sum(1 for r in f if r > 0), len(f)).strip()}]" if f else ""
                parts.append(f"{reg} {_pct(res.regimes.get(reg, 0), total).strip()}{up}")
            out.append(f"    {coin:<5} " + " | ".join(parts))
        for name in SETUP_SIDE:
            if any(name in res.setups for _, res in per_coin):
                out.append(f"  {name}")
                hold = float(load_config().get("expected_hold_window_days", default={}).get(name, 10))
                out.extend("  " + line for line in _setup_line(name, per_coin, hold))
        out.append("")
    return "\n".join(out)
