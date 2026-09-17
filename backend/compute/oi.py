"""Coin-denominated open interest. Doctrine, Measurement rules: "Open interest is
counted in coins, never in dollars. Dollar OI conflates position change with price
change." Every function here operates purely on already-fetched Observation lists so the
exact same code runs against live or replayed data (implementation spec, "Same code
path").

Never-mix-venues rule: aggregation only ever sums OI_COIN observations, each already
denominated in its own venue's coin count. Nothing here divides a USD figure by a price
from a different venue.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from backend.core.observation import Metric, Observation


@dataclass(frozen=True)
class VenueReading:
    venue: str
    source_id: str
    value: Decimal


@dataclass(frozen=True)
class AggregateOI:
    total_coins: Decimal
    per_venue: tuple[VenueReading, ...]

    @property
    def venue_count(self) -> int:
        return len(self.per_venue)


def latest_per_venue(observations: list[Observation]) -> dict[str, Observation]:
    """Most recent observation per venue. Callers pass observations already filtered to
    a single metric and already excluded of expired rows (the store's as_of query does
    this) — this function only breaks ties on observed_at."""
    latest: dict[str, Observation] = {}
    for obs in observations:
        cur = latest.get(obs.venue)
        if cur is None or obs.observed_at > cur.observed_at:
            latest[obs.venue] = obs
    return latest


def aggregate_oi_coin(observations: list[Observation]) -> AggregateOI:
    oi_obs = [o for o in observations if o.metric == Metric.OI_COIN]
    per_venue = latest_per_venue(oi_obs)
    readings = tuple(
        VenueReading(venue=v, source_id=o.source_id, value=o.value) for v, o in sorted(per_venue.items())
    )
    total = sum((r.value for r in readings), Decimal(0))
    return AggregateOI(total_coins=total, per_venue=readings)


def aggregate_oi_series(observations: list[Observation], bucket_seconds: int) -> list[tuple[int, Decimal]]:
    """A time series of aggregate coin OI, bucketed. Each venue's own series is resampled
    to the same buckets (last reading per venue per bucket) and then summed across
    whichever venues have a reading in that bucket — an aggregation across venues at
    each metric's own equivalent point in time, not a cross-venue ratio, so this does not
    fall under the never-compute-a-ratio-across-observed_at rule (0.10's sync_tolerance
    applies to ratios, not sums of the same metric)."""
    oi_obs = [o for o in observations if o.metric == Metric.OI_COIN]
    per_venue: dict[str, dict[int, Observation]] = {}
    for obs in sorted(oi_obs, key=lambda o: o.observed_at):
        bucket = int(obs.observed_at.timestamp() // bucket_seconds)
        per_venue.setdefault(obs.venue, {})[bucket] = obs

    all_buckets = sorted({b for venue_buckets in per_venue.values() for b in venue_buckets})
    series: list[tuple[int, Decimal]] = []
    for b in all_buckets:
        total = sum((venue_buckets[b].value for venue_buckets in per_venue.values() if b in venue_buckets), Decimal(0))
        series.append((b, total))
    return series


def pct_change(old: Decimal, new: Decimal) -> Decimal | None:
    """None (not zero, not an estimate) when the base is zero or missing — a rate of
    change over an undefined base is unknown, not 0%."""
    if old is None or old == 0:
        return None
    return (new - old) / old
