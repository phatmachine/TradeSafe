"""Shared result types for Gate U and Layer 0. A condition is never a bare boolean
(implementation spec: "Setup evaluation returns per-condition results, never a boolean" —
the same principle applies to gates) — every condition carries its computed value, its
threshold and an explicit UNKNOWN state distinct from pass/fail (spec, "Unknown is a
distinct state... never null-coalesced to a default").
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Status = Literal["pass", "fail", "unknown"]


@dataclass(frozen=True)
class ConditionResult:
    name: str
    status: Status
    computed_value: Any
    threshold: Any
    detail: str

    @property
    def passed(self) -> bool:
        return self.status == "pass"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "computed_value": None if self.computed_value is None else str(self.computed_value),
            "threshold": None if self.threshold is None else str(self.threshold),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class GateResult:
    gate: str
    conditions: tuple[ConditionResult, ...]

    @property
    def passed(self) -> bool:
        # Fail closed: a gate passes only if every condition explicitly passed. An
        # unknown condition is not a partial pass (doctrine 0.8 — no intermediate
        # confidence state).
        return len(self.conditions) > 0 and all(c.passed for c in self.conditions)

    @property
    def failing_conditions(self) -> tuple[ConditionResult, ...]:
        return tuple(c for c in self.conditions if not c.passed)

    def to_dict(self) -> dict:
        return {
            "gate": self.gate,
            "passed": self.passed,
            "conditions": [c.to_dict() for c in self.conditions],
        }
