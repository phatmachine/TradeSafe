"""Exit monitoring — a separate, explicitly manual command against a position the user
recorded themselves (doctrine: "it reports; it does not close"). It re-evaluates, from
fresh data, exactly three things: whether the named trapped cohort (2.2) still holds,
whether the setup's expected-hold window has elapsed (the time stop), and — for cascade
absorption and its squeeze mirror, where the doctrine gives an explicit formula — whether
the premise is spent (coin OI rebuilt to within a band of its pre-flush level). Nothing here sizes, closes, or recommends anything.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from backend.compute import cohort as cohort_mod
from backend.compute.oi import aggregate_oi_series
from backend.core.config import Config
from backend.core.observation import Metric
from backend.replay.source import DataSource


def evaluate_exit(position: dict, ds: DataSource, cfg: Config) -> dict:
    instrument = position["instrument"]
    setup = position["setup"]
    opened_at = datetime.fromisoformat(position["opened_at"])
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=timezone.utc)
    entry_cohort = (position.get("trapped_cohort") or {}).get("cohort")
    as_of = ds.get_as_of()

    liq_window_hours = float(cfg.get("liq_window_hours", default=72))
    liq_hist = ds.observations_including_expired(instrument, lookback_seconds=liq_window_hours * 3600)
    current_cohort = cohort_mod.classify(liq_hist, cfg=cfg)
    thesis_invalidated = (
        current_cohort.cohort.value != "unnamed"
        and entry_cohort is not None
        and current_cohort.cohort.value != entry_cohort
    )

    hold_days_elapsed = (as_of - opened_at).total_seconds() / 86400
    expected_window = float(cfg.get("expected_hold_window_days", default={}).get(setup, 10))
    time_stop_elapsed = hold_days_elapsed >= expected_window

    premise_spent = None
    if setup in ("cascade_absorption", "squeeze_absorption"):
        bar_seconds = int(cfg.get("cascade", "bar_seconds", default=900))
        flush_window_hours = float(cfg.get("cascade", "flush_window_hours", default=48))
        spent_band = Decimal(str(cfg.get("cascade", "spent_band_pct", default=0.05)))
        oi_hist = [
            o
            for o in ds.observations_including_expired(instrument, lookback_seconds=flush_window_hours * 3600 * 2)
            if o.metric == Metric.OI_COIN
        ]
        series = aggregate_oi_series(oi_hist, bar_seconds)
        window_bars = max(1, int((flush_window_hours * 3600) // bar_seconds))
        recent = series[-window_bars:] if series else []
        if len(recent) >= 2:
            pre_flush_level = max(v for _, v in recent)
            current_level = recent[-1][1]
            within_band = pre_flush_level != 0 and abs(current_level - pre_flush_level) / pre_flush_level <= spent_band
            premise_spent = bool(within_band or thesis_invalidated)

    return {
        "position_id": position["position_id"],
        "instrument": instrument,
        "setup": setup,
        "as_of": as_of.isoformat(),
        "entry_cohort": entry_cohort,
        "current_cohort": current_cohort.cohort.value,
        "thesis_invalidated": thesis_invalidated,
        "hold_days_elapsed": hold_days_elapsed,
        "expected_hold_window_days": expected_window,
        "time_stop_elapsed": time_stop_elapsed,
        "premise_spent": premise_spent,
        "note": "This reports status only. It never closes or resizes a position.",
    }
