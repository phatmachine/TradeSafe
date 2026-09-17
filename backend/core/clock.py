"""A Clock is the only source of "now" anywhere in this codebase. Compute, gates and
setups must never call datetime.utcnow() directly — they take a Clock, so the exact same
code path works for a live run (RealClock) and a replay run (FrozenClock pinned to
as_of). This is what backend/replay/source.py's no-lookahead guarantee is built on.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone


class Clock(ABC):
    @abstractmethod
    def now(self) -> datetime:
        ...


class RealClock(Clock):
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FrozenClock(Clock):
    """Pinned to a fixed as_of instant. Used by ReplaySource so a replay run can never
    observe anything with observed_at > as_of, structurally rather than by discipline."""

    def __init__(self, as_of: datetime):
        if as_of.tzinfo is None:
            raise ValueError("FrozenClock requires a timezone-aware datetime")
        self._as_of = as_of

    def now(self) -> datetime:
        return self._as_of


def utcnow() -> datetime:
    """Convenience for call sites that are not clock-injected (logging, HTTP timestamps
    outside the decision path). Never call this from compute/gates/setups."""
    return datetime.now(timezone.utc)
