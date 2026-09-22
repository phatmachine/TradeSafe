"""The belief-data firewall. T4 observations (doctrine 0.1: evidence of what participants
believe, never of what is true) must be structurally incapable of reaching a gate, a
compute, a setup or a classifier, not merely unused by convention. Layer 1 excludes
"sentiment indices used as timing tools" outright.

Three walls, each enough alone:
- Storage: T4 rows live in their own table (store table belief_observations);
  db.insert_observation refuses them, so no decision-path query can return one.
- This module: every decision-path entry point is wrapped in @decision_path, which puts
  each observation argument through decision_inputs() and hands a DataSource argument
  over as a guarded view whose reads go through it too. Any T4 observation raises.
- The view exposes only the decision reads (get_as_of, observations,
  observations_including_expired). Belief reads exist on the DataSource, but a function
  holding the guarded view has no way to reach them.

Belief data reaches the report only through the report builder (report/belief.py), after
the decision is made. The guard can only see what carries a tier: a bare Decimal copied
out of a T4 observation carries none, which is why the walls above sit where observations
enter, not where their values are used.
"""
from __future__ import annotations

import contextvars
import functools
import operator
from typing import Any, Callable, Iterable, TypeVar

from backend.core.observation import Tier

F = TypeVar("F", bound=Callable[..., Any])


class BeliefDataInDecisionPath(Exception):
    pass


def _is_belief(o: Any) -> bool:
    return getattr(o, "tier", None) == Tier.T4


_tier = operator.attrgetter("tier")


def _has_belief(obs: list | tuple) -> bool:
    try:
        return Tier.T4 in map(_tier, obs)  # C speed: ~3 ms per 100k observations
    except AttributeError:  # something without a tier is in the list
        return any(_is_belief(o) for o in obs)


def decision_inputs(observations: Iterable[Any]) -> list:
    """Every gate/compute/setup/classifier gets its inputs through here. No exceptions."""
    obs = list(observations)
    if _has_belief(obs):
        bad = [o for o in obs if _is_belief(o)]
        raise BeliefDataInDecisionPath(
            f"{len(bad)} T4 observation(s) reached the decision path: "
            + ", ".join(sorted({getattr(o.metric, "value", str(o.metric)) for o in bad}))
        )
    return obs


class GuardedSource:
    """A DataSource as the decision path sees it: the decision reads only, each checked."""

    __slots__ = ("_ds",)

    def __init__(self, ds: Any):
        self._ds = ds

    def get_as_of(self):
        return self._ds.get_as_of()

    def observations(self, *args: Any, **kwargs: Any) -> list:
        return decision_inputs(self._ds.observations(*args, **kwargs))

    def observations_including_expired(self, *args: Any, **kwargs: Any) -> list:
        return decision_inputs(self._ds.observations_including_expired(*args, **kwargs))


def _is_data_source(v: Any) -> bool:
    # Duck-typed: core/ can't import replay/ (which imports core/).
    return hasattr(v, "observations_including_expired") and hasattr(v, "get_as_of")


def _guard(v: Any) -> Any:
    if isinstance(v, GuardedSource):
        return v
    if _is_data_source(v):
        return GuardedSource(v)
    if isinstance(v, (list, tuple)):
        if _has_belief(v):
            decision_inputs(v)
    elif _is_belief(v):
        decision_inputs([v])
    return v


def _guard_source(v: Any) -> Any:
    return GuardedSource(v) if _is_data_source(v) and not isinstance(v, GuardedSource) else v


# Set while a guarded call is running. Observations are checked where they enter the
# decision path: the outermost guarded call's arguments, and every read through a
# GuardedSource. A nested call's lists are built from those, so re-scanning them at every
# level only repeats work (it made the research harness 60% slower); a DataSource is
# still wrapped at every level, which costs nothing.
_inside = contextvars.ContextVar("decision_path_inside", default=False)


def decision_path(fn: F) -> F:
    """Marks a decision-path entry point and guards its arguments. tests/test_belief_firewall.py
    checks that every public gate/compute/setup function taking observations or a
    DataSource carries it."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if _inside.get():
            return fn(*(_guard_source(a) for a in args), **{k: _guard_source(v) for k, v in kwargs.items()})
        token = _inside.set(True)
        try:
            return fn(*(_guard(a) for a in args), **{k: _guard(v) for k, v in kwargs.items()})
        finally:
            _inside.reset(token)

    wrapper.__decision_path__ = True  # type: ignore[attr-defined]
    return wrapper  # type: ignore[return-value]
