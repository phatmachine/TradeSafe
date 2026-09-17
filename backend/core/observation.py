"""Everything the tool knows is an Observation. Nothing enters the decision path
unwrapped (implementation spec, "Data schema"). This module also owns half-life expiry
(doctrine 0.2): expiry is derived from the metric and, for oi/funding, from the current
volatility regime (the "fast tape" switch), never hardcoded at a call site.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from backend.core.config import Config


class Metric(str, Enum):
    PRICE = "price"
    ORDER_BOOK_DEPTH = "order_book_depth"
    FUNDING_8H = "funding_8h"
    OI_COIN = "oi_coin"
    OI_USD = "oi_usd"
    SPOT_VOLUME = "spot_volume"
    PERP_VOLUME = "perp_volume"
    LIQUIDATION = "liquidation"
    SUPPLY_TOTAL = "supply_total"
    SUPPLY_CIRCULATING = "supply_circulating"
    SUPPLY_LOCKED = "supply_locked"
    SUPPLY_STAKED = "supply_staked"
    ETF_HOLDINGS = "etf_holdings"
    EVENT = "event"
    MARKET_CAP = "market_cap"


class Unit(str, Enum):
    USD = "usd"
    COINS = "coins"
    PCT_8H = "pct_8h"
    PCT = "pct"
    COUNT = "count"
    BOOLEAN = "boolean"
    TIMESTAMP = "timestamp"


class Tier(str, Enum):
    T1 = "T1"
    T2 = "T2"
    T3 = "T3"
    T4 = "T4"
    T0 = "T0"


# Metrics whose expiry can shorten under a "fast tape" (doctrine 0.2 footnote / spec
# fast_tape_rv_threshold).
_FAST_TAPE_METRICS = {Metric.OI_COIN, Metric.OI_USD, Metric.FUNDING_8H}

# Volume and market cap track price/OI-like dynamics closely enough to share their
# expiry bucket rather than needing a distinct doctrine half-life of their own.
_VOLUME_LIKE_METRICS = {Metric.PERP_VOLUME, Metric.SPOT_VOLUME, Metric.MARKET_CAP}


def half_life_seconds(metric: Metric, cfg: Config, *, fast_tape: bool = False) -> int:
    exp = cfg.get("expiry", default={})
    if metric == Metric.PRICE:
        return int(exp.get("price_seconds", 30))
    if metric == Metric.ORDER_BOOK_DEPTH:
        return int(exp.get("order_book_seconds", 15))
    if metric in _FAST_TAPE_METRICS or metric in _VOLUME_LIKE_METRICS:
        if fast_tape:
            return int(exp.get("oi_funding_fast_tape_seconds", 900))
        return int(exp.get("oi_funding_normal_seconds", 3600))
    if metric == Metric.LIQUIDATION:
        return int(exp.get("liquidation_seconds", 14400))
    if metric == Metric.ETF_HOLDINGS:
        return int(exp.get("etf_flow_seconds", 86400))
    if metric in (
        Metric.SUPPLY_TOTAL,
        Metric.SUPPLY_CIRCULATING,
        Metric.SUPPLY_LOCKED,
        Metric.SUPPLY_STAKED,
    ):
        return int(exp.get("supply_float_seconds", 604800))
    if metric == Metric.EVENT:
        return int(exp.get("event_calendar_seconds", 604800))
    # Fail closed on an unmapped metric rather than guessing a TTL.
    raise ValueError(f"no half-life mapping for metric {metric!r}")


@dataclass(frozen=True)
class Observation:
    metric: Metric
    instrument: str
    value: Decimal
    unit: Unit
    venue: str
    source_id: str
    tier: Tier
    collected_at: datetime
    observed_at: datetime
    expires_at: datetime
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("collected_at", "observed_at", "expires_at"):
            dt = getattr(self, name)
            if dt.tzinfo is None:
                raise ValueError(f"Observation.{name} must be timezone-aware")

    def is_expired(self, at: datetime) -> bool:
        return at >= self.expires_at

    def staleness_seconds(self, at: datetime) -> float:
        """collected_at vs observed_at divergence — the gap that caused the logged
        misread (implementation spec, Data schema)."""
        return (self.collected_at - self.observed_at).total_seconds()

    @staticmethod
    def build(
        *,
        metric: Metric,
        instrument: str,
        value: Decimal,
        unit: Unit,
        venue: str,
        source_id: str,
        tier: Tier,
        observed_at: datetime,
        cfg: Config,
        collected_at: datetime | None = None,
        fast_tape: bool = False,
        raw: dict[str, Any] | None = None,
    ) -> "Observation":
        collected = collected_at or datetime.now(timezone.utc)
        ttl = half_life_seconds(metric, cfg, fast_tape=fast_tape)
        return Observation(
            metric=metric,
            instrument=instrument,
            value=value,
            unit=unit,
            venue=venue,
            source_id=source_id,
            tier=tier,
            collected_at=collected,
            observed_at=observed_at,
            expires_at=observed_at + timedelta(seconds=ttl),
            raw=raw or {},
        )
