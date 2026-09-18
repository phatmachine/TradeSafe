"""Funding as a per-period series. The collector writes one FUNDING_8H row per venue per
poll, so "the last N readings" taken raw would be N venues from the same instant, not N
periods — which is not what "funding has held <= 0 for N periods" means. This buckets
into fixed-width periods and takes the cross-venue mean in each (simple mean; the
doctrine's OI-weighting is pending calibration).

Each venue counts once per period, at its latest reading in it. Averaging every raw
reading instead weighted each venue by how often it happens to be polled (the fast loop
polls three venues six times as often as the rest) — and made the result depend on how
many polls were kept, which compaction (store/db.py) changes for older periods.
"""
from __future__ import annotations

from decimal import Decimal

from backend.core.observation import Observation


def period_means(funding_history: list[Observation], bucket_seconds: int) -> list[Decimal]:
    """Oldest first, one value per period that has any reading."""
    by_period: dict[int, dict[str, Decimal]] = {}
    for obs in sorted(funding_history, key=lambda o: o.observed_at):
        bucket = int(obs.observed_at.timestamp() // bucket_seconds)
        by_period.setdefault(bucket, {})[obs.venue] = obs.value  # sorted input: latest per venue wins
    return [sum(vals.values()) / len(vals) for _, vals in sorted(by_period.items())]
