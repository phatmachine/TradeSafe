"""DataSource: the one seam between "live" and "replay". Everything above this layer
(gates, compute, setups, report) calls a DataSource and never touches backend/store or a
Clock directly, so the exact same analysis code runs whether the instant in question is
right now or three months ago (implementation spec, "Same code path").

No lookahead is enforced structurally, not by caller discipline: both LiveSource and
ReplaySource go through store.query_observations(as_of=...), whose SQL filters
observed_at <= as_of. A ReplaySource pinned to an as_of in the past is *incapable* of
returning a row with a later observed_at — there is no code path that could accidentally
pass a different, more permissive as_of, because get_as_of() is the only source of that
value and it comes from the Clock the DataSource was built with.
"""
from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from datetime import datetime

from backend.core.clock import Clock, FrozenClock, RealClock
from backend.core.observation import Metric, Observation
from backend.store import db


class DataSource(ABC):
    @abstractmethod
    def get_as_of(self) -> datetime:
        ...

    @abstractmethod
    def observations(
        self,
        instrument: str,
        *,
        metric: Metric | None = None,
        lookback_seconds: float | None = None,
    ) -> list[Observation]:
        ...

    @abstractmethod
    def observations_including_expired(
        self, instrument: str, *, lookback_seconds: float
    ) -> list[Observation]:
        """The historical-series primitive. Half-life expiry (doctrine 0.2) answers "is
        this still a valid reading of the CURRENT state" — it does not mean a past print
        was never real. Anything that looks backward over a window (realised
        volatility, regime/swing classification, OI rate-of-change, liquidation
        aggregation, and Layer 0's own 0.10 poisoned-snapshot detection) must use this,
        never observations(), which only ever answers "what is true right now" and would
        silently return an empty series for any window wider than a metric's half-life.
        Still enforces no-lookahead (observed_at <= as_of) — only the expiry filter is
        relaxed."""
        ...


class LiveSource(DataSource):
    """Reads the store as of "now". Never fetches from a venue directly (implementation
    spec, "Two services, two lifecycles") — that is the collector's job, running as a
    separate always-on process that only ever writes."""

    def __init__(self, conn: sqlite3.Connection, clock: Clock | None = None):
        self._conn = conn
        self._clock = clock or RealClock()

    def get_as_of(self) -> datetime:
        return self._clock.now()

    def observations(
        self,
        instrument: str,
        *,
        metric: Metric | None = None,
        lookback_seconds: float | None = None,
    ) -> list[Observation]:
        return db.query_observations(
            self._conn,
            instrument=instrument,
            metric=metric,
            as_of=self.get_as_of(),
            lookback_seconds=lookback_seconds,
        )

    def observations_including_expired(
        self, instrument: str, *, lookback_seconds: float
    ) -> list[Observation]:
        return db.query_observations_all(
            self._conn, instrument=instrument, as_of=self.get_as_of(), lookback_seconds=lookback_seconds
        )


class ReplaySource(DataSource):
    """Reads the same store, clock pinned to a fixed as_of. Historical gaps are honest:
    if the collector wasn't running far enough back to have a metric, this returns
    nothing for it and the gate that needs it reports UNKNOWN, exactly as a live run
    would if a venue were down (implementation spec, "Historical gaps are honest")."""

    def __init__(self, conn: sqlite3.Connection, as_of: datetime):
        self._conn = conn
        self._clock = FrozenClock(as_of)

    def get_as_of(self) -> datetime:
        return self._clock.now()

    def observations(
        self,
        instrument: str,
        *,
        metric: Metric | None = None,
        lookback_seconds: float | None = None,
    ) -> list[Observation]:
        return db.query_observations(
            self._conn,
            instrument=instrument,
            metric=metric,
            as_of=self.get_as_of(),
            lookback_seconds=lookback_seconds,
        )

    def observations_including_expired(
        self, instrument: str, *, lookback_seconds: float
    ) -> list[Observation]:
        return db.query_observations_all(
            self._conn, instrument=instrument, as_of=self.get_as_of(), lookback_seconds=lookback_seconds
        )
