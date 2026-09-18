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

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta
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


# How long a venue's last OI reading may be carried forward to fill a bucket it has no
# reading in. Matches the normal OI half-life (config expiry.oi_funding_normal_seconds).
MAX_STALENESS_SECONDS = 3600


def aggregate_oi_series(
    observations: list[Observation],
    bucket_seconds: int,
    *,
    start: datetime | None = None,
    max_staleness_seconds: int = MAX_STALENESS_SECONDS,
) -> list[tuple[int, Decimal]]:
    """Aggregate coin OI per bucket from `start` (or the earliest reading) to the latest,
    summed over a FIXED set of venues: those with a reading at both the first and the
    last bucket. Each venue's value in a bucket is its latest reading by the bucket's end,
    carried forward at most max_staleness_seconds; a bucket where any venue in the set has
    no reading that fresh is dropped rather than summed short.

    The fixed set is the point. Venues join and leave the collection — history backfilled
    from one venue, a venue added later, a single failed poll — and summing whichever
    venues happen to report in each bucket reads a venue joining as positions opening and
    a missed poll as positions closing. Holding the set fixed across the span means every
    change in the series is a change in positioning.

    An aggregation of the same metric across venues at each point in time, not a
    cross-venue ratio, so the never-compute-a-ratio-across-observed_at rule (0.10's
    sync_tolerance) does not apply."""
    floor = start - timedelta(seconds=max_staleness_seconds) if start is not None else None
    oi_obs = sorted(
        (o for o in observations if o.metric == Metric.OI_COIN and (floor is None or o.observed_at >= floor)),
        key=lambda o: o.observed_at,
    )
    if not oi_obs:
        return []

    per_venue: dict[str, tuple[list[float], list[Decimal]]] = {}
    for obs in oi_obs:
        times, values = per_venue.setdefault(obs.venue, ([], []))
        times.append(obs.observed_at.timestamp())
        values.append(obs.value)

    span_start = (start or oi_obs[0].observed_at).timestamp()
    buckets = sorted({int(o.observed_at.timestamp() // bucket_seconds) for o in oi_obs if o.observed_at.timestamp() >= span_start})
    if not buckets:
        return []

    def reading(venue: str, bucket: int) -> Decimal | None:
        times, values = per_venue[venue]
        bucket_end = (bucket + 1) * bucket_seconds
        i = bisect_left(times, bucket_end) - 1  # latest reading strictly before the bucket ends
        if i < 0 or bucket_end - times[i] > max_staleness_seconds + bucket_seconds:
            return None
        return values[i]

    venues = [v for v in per_venue if reading(v, buckets[0]) is not None and reading(v, buckets[-1]) is not None]
    if not venues:
        return []

    series: list[tuple[int, Decimal]] = []
    for bucket in buckets:
        values = [reading(v, bucket) for v in venues]
        if any(val is None for val in values):
            continue
        series.append((bucket, sum(values, Decimal(0))))
    return series


def pct_change(old: Decimal, new: Decimal) -> Decimal | None:
    """None (not zero, not an estimate) when the base is zero or missing — a rate of
    change over an undefined base is unknown, not 0%."""
    if old is None or old == 0:
        return None
    return (new - old) / old
