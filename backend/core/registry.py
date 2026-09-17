"""SourceRegistry: what a source is (static, from config/sources.yaml) plus what it has
proven to be (dynamic reliability_prior / demoted_at, persisted in the store and updated
by the Layer 6 audit loop). upstream_id is what makes doctrine 0.3 real — two source_ids
sharing an upstream_id are one source for independence-counting purposes, and that
grouping is asserted here explicitly, never inferred from a domain name.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from backend.core.config import Config
from backend.core.observation import Tier


@dataclass(frozen=True)
class SourceRecord:
    source_id: str
    name: str
    upstream_id: str
    collection_method: str
    tier: Tier
    metrics: tuple[str, ...]
    enabled: bool = True
    reliability_prior: float = 1.0
    demoted_at: datetime | None = None

    @property
    def is_rejected(self) -> bool:
        """Permanent T0 demotion (audit loop, two failures) or static T0 registration."""
        return self.demoted_at is not None or self.tier == Tier.T0


class SourceRegistry:
    def __init__(self, records: dict[str, SourceRecord]):
        self._records = records

    @classmethod
    def from_config(
        cls,
        cfg: Config,
        dynamic_state: dict[str, dict] | None = None,
    ) -> "SourceRegistry":
        dynamic_state = dynamic_state or {}
        records: dict[str, SourceRecord] = {}
        for entry in cfg.sources.get("sources", []):
            source_id = entry["source_id"]
            dyn = dynamic_state.get(source_id, {})
            records[source_id] = SourceRecord(
                source_id=source_id,
                name=entry["name"],
                upstream_id=entry["upstream_id"],
                collection_method=entry["collection_method"],
                tier=Tier(entry["tier"]),
                metrics=tuple(entry.get("metrics", [])),
                enabled=bool(entry.get("enabled", True)),
                reliability_prior=float(dyn.get("reliability_prior", 1.0)),
                demoted_at=dyn.get("demoted_at"),
            )
        return cls(records)

    def get(self, source_id: str) -> SourceRecord | None:
        return self._records.get(source_id)

    def enabled_sources(self) -> Iterable[SourceRecord]:
        return (r for r in self._records.values() if r.enabled and not r.is_rejected)

    def independent_upstream_count(self, source_ids: Iterable[str]) -> int:
        """The core of 0.3: count distinct upstream_id values among the given
        source_ids, excluding any source that has been permanently rejected."""
        upstreams: set[str] = set()
        for sid in source_ids:
            rec = self._records.get(sid)
            if rec is None or rec.is_rejected:
                continue
            upstreams.add(rec.upstream_id)
        return len(upstreams)

    def independence_satisfied(self, source_ids: Iterable[str], cfg: Config) -> bool:
        required = int(cfg.get("min_independent", default=2))
        return self.independent_upstream_count(source_ids) >= required
