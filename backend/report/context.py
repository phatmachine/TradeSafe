"""Report context added after the decision, like section 7 (report/belief.py): section 8,
"Leverage risk", and each setup's measured track record. Neither is an input to any gate,
setup or verdict. build_report attaches them to a finished decision, and the scanner reads
a setup's record only to choose whether a qualifying setup raises an alert.

Settings and records live in config/context.yaml, outside config_hash, so changing them
can't change a DecisionRecord or a run_id.
"""
from __future__ import annotations

import dataclasses
import hashlib
from functools import lru_cache
from typing import Any

import yaml

from backend.compute.leverage import leverage_risk as compute_leverage_risk
from backend.core.config import CONFIG_DIR
from backend.core.observation import Metric, Observation
from backend.replay.source import DataSource
from backend.report.contract import AnalysisReport

CONTEXT_FILE = "context.yaml"

LEVERAGE_TITLE = "Leverage risk (context, not evaluated)"
LEVERAGE_CAPTION = (
    "How far price must move against a position before it is liquidated, and how often moves "
    "that large happened in this coin's recent history. Not an input to any check or verdict, "
    "and not a suggested leverage or size."
)
TRACK_TITLE = "Setup track records (context, not evaluated)"
TRACK_CAPTION = (
    "How each setup's past signals turned out in the research replay. A record never changes "
    "whether a setup qualifies; it only decides whether a qualifying setup sends an alert."
)

STATUS_LABEL = {
    "supported": "Record supports it: alerts on",
    "losing": "Record shows it losing: alerts off",
    "unproven": "Too few signals to judge: alerts on, marked unproven",
    "no_direction": "No direction, so no trade result: alerts on",
    "no_record": "No measured record: alerts on, marked unproven",
}


@dataclasses.dataclass(frozen=True)
class ContextConfig:
    raw: dict[str, Any]
    context_hash: str

    def section(self, name: str) -> dict:
        return self.raw.get(name) or {}


@lru_cache(maxsize=1)
def load_context_config() -> ContextConfig:
    path = CONFIG_DIR / CONTEXT_FILE
    data = path.read_bytes()
    return ContextConfig(raw=yaml.safe_load(data) or {}, context_hash=hashlib.sha256(data).hexdigest()[:16])


# --- Track records and the alert rule -------------------------------------------------------


def setup_record(setup: str, ctx: ContextConfig | None = None) -> dict:
    """One setup's measured record plus the alert status the rule in context.yaml gives it."""
    ctx = ctx or load_context_config()
    min_signals = int(ctx.section("alert_policy").get("min_signals", 20))
    rec = (ctx.section("track_records").get("setups") or {}).get(setup)
    if rec is None:
        status = "no_record"
    elif rec.get("side", "none") == "none":
        status = "no_direction"
    elif int(rec["signals"]) < min_signals:
        status = "unproven"
    elif float(rec["mean_return"]) > 0 and float(rec["median_return"]) > 0:
        status = "supported"
    else:
        status = "losing"
    return {
        "setup": setup,
        "status": status,
        "alerts": status != "losing",
        "status_label": STATUS_LABEL[status],
        "record": rec,
        "summary": _summary(rec, status),
    }


def alerts_on(setup: str, ctx: ContextConfig | None = None) -> bool:
    return setup_record(setup, ctx)["alerts"]


def _pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def _summary(rec: dict | None, status: str) -> str:
    if rec is None:
        return "No measured record for this setup yet."
    n, hold = int(rec["signals"]), rec["hold_days"]
    hit, base = float(rec["went_its_way"]) * 100, float(rec["random_entry"]) * 100
    if status == "no_direction":
        measure = rec.get("measure", "price rose")
        head = f"{n} past signals, {hold}-day window: {measure} after {hit:.0f}% of them (random entries: {base:.0f}%)"
    else:
        side = "rise" if rec["side"] == "long" else "fall"
        head = (
            f"{n} past signals held {hold} days: price moved its way ({side}) after {hit:.0f}%, "
            f"against {base:.0f}% for a random entry"
        )
    return (
        f"{head}. Typical result {_pct(float(rec['median_return']))}, average {_pct(float(rec['mean_return']))}, "
        f"worst {_pct(float(rec['worst_return']))}, unleveraged."
    )


def track_records(setups: list[str] | None = None, ctx: ContextConfig | None = None) -> dict:
    ctx = ctx or load_context_config()
    tr = ctx.section("track_records")
    names = setups if setups is not None else list((tr.get("setups") or {}).keys())
    return {
        "title": TRACK_TITLE,
        "caption": TRACK_CAPTION,
        "context_hash": ctx.context_hash,
        "measured_at": tr.get("measured_at"),
        "method": tr.get("method"),
        "caveats": list(tr.get("caveats") or []),
        "setups": [setup_record(s, ctx) for s in names],
        "error": None,
    }


# --- Leverage risk ----------------------------------------------------------------------------


def _single_best_venue_series(observations: list[Observation]) -> list[Observation]:
    by_venue: dict[str, list[Observation]] = {}
    for o in observations:
        by_venue.setdefault(o.venue, []).append(o)
    return max(by_venue.values(), key=len, default=[])


def _r(x: float | None, places: int = 4) -> float | None:
    return None if x is None else round(x, places)


def leverage_section(instrument: str, ds: DataSource, ctx: ContextConfig | None = None) -> dict:
    ctx = ctx or load_context_config()
    lr = ctx.section("leverage_risk")
    lookback_days = float(lr.get("lookback_days", 60))
    prices = ds.observations_including_expired(
        instrument, lookback_seconds=lookback_days * 86400, metrics=(Metric.PRICE,)
    )
    series = _single_best_venue_series(prices)
    hold_days = float(lr.get("hold_days", 10))
    result = compute_leverage_risk(
        series,
        leverages=[float(x) for x in lr.get("leverages", [2, 3, 5, 10])],
        maintenance_margin_rate=float(lr.get("maintenance_margin_rate", 0.01)),
        hold_days=hold_days,
        bar_seconds=int(lr.get("bar_seconds", 14400)),
        min_history_days=float(lr.get("min_history_days", 17)),
    )
    return {
        "title": LEVERAGE_TITLE,
        "caption": LEVERAGE_CAPTION,
        "context_hash": ctx.context_hash,
        "status": result.status,
        "venue": series[0].venue if series else None,
        "hold_days": hold_days,
        "maintenance_margin_rate": float(lr.get("maintenance_margin_rate", 0.01)),
        "history_days": _r(result.history_days, 1),
        "windows": result.windows,
        "daily_volatility": _r(result.daily_volatility),
        "typical_hold_move": _r(result.typical_hold_move),
        "rows": [
            {
                "leverage": row.leverage,
                "liquidating_move": _r(row.liquidating_move),
                "long_breach_rate": _r(row.long_breach_rate),
                "short_breach_rate": _r(row.short_breach_rate),
                "move_vs_typical": _r(row.move_vs_typical, 2),
            }
            for row in result.rows
        ],
        "note": (
            "Uses 4-hour closes, which miss brief wicks, so real liquidations would have been at "
            "least this frequent. Isolated margin; fees and funding not included."
        ),
        "error": None,
    }


# --- Attaching both to a finished decision --------------------------------------------------


def attach_context(report: AnalysisReport, ds: DataSource, ctx: ContextConfig | None = None) -> AnalysisReport:
    """Adds section 8 and the track records to a finished report. A failure in either leaves
    the decision standing and says so in that section, as section 7 does."""
    try:
        ctx = ctx or load_context_config()
    except Exception as exc:  # noqa: BLE001
        err = f"Context unavailable: {type(exc).__name__}: {exc}"
        return dataclasses.replace(
            report,
            leverage_risk={"title": LEVERAGE_TITLE, "caption": LEVERAGE_CAPTION, "error": err},
            track_records={"title": TRACK_TITLE, "caption": TRACK_CAPTION, "setups": [], "error": err},
        )
    try:
        leverage = leverage_section(report.instrument, ds, ctx)
    except Exception as exc:  # noqa: BLE001
        leverage = {"title": LEVERAGE_TITLE, "caption": LEVERAGE_CAPTION,
                    "error": f"Leverage risk unavailable: {type(exc).__name__}: {exc}"}
    try:
        records = track_records([s["gate"] for s in report.setup_evaluation], ctx)
    except Exception as exc:  # noqa: BLE001
        records = {"title": TRACK_TITLE, "caption": TRACK_CAPTION, "setups": [],
                   "error": f"Track records unavailable: {type(exc).__name__}: {exc}"}
    return dataclasses.replace(report, leverage_risk=leverage, track_records=records)
