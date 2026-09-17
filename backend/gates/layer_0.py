"""Layer 0 — data warfare protocol. Nothing downstream (state classification, setups)
runs on an observation set that hasn't passed this. Implements the check table from the
implementation spec: freshness, independence, dispersion, reconciliation, coherence,
record integrity, tier sufficiency.

`evaluate()` returns both the GateResult and the *cleaned* observation list — the set
with any doctrine-0.10 "poisoned snapshot" siblings removed — which is what
compute/regime.py, cohort.py and the setups must be built from, never the raw
ds.observations() call directly.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from backend.compute.oi import latest_per_venue
from backend.core.config import Config
from backend.core.observation import Metric, Observation
from backend.core.registry import SourceRegistry
from backend.gates.common import ConditionResult, GateResult
from backend.replay.source import DataSource

# Metrics that must clear independence + tier sufficiency to be usable at all
# (doctrine Layer 1, ground truth set).
DECISION_METRICS = (
    Metric.PRICE,
    Metric.FUNDING_8H,
    Metric.OI_COIN,
    Metric.PERP_VOLUME,
    Metric.SPOT_VOLUME,
)

# How far back to look when hunting for poisoned snapshots / immutable-fact
# reconciliation — generously beyond the longest half-life (supply/float, 1 week).
RECORD_INTEGRITY_LOOKBACK_SECONDS = 8 * 86400


@dataclass(frozen=True)
class Layer0Result:
    gate_result: GateResult
    clean_observations: list[Observation]


def _find_poisoned_snapshots(raw: list[Observation], as_of) -> set[tuple[str, object]]:
    """Doctrine 0.10: if one field from a (source_id, collected_at) snapshot has
    expired while a sibling field from the very same fetch has not, the whole snapshot
    is discarded — no keeping the fields that happen to still look fresh. A snapshot
    that is either wholly expired or wholly fresh is not a cherry-picking risk (the
    ordinary expiry filter in observations() already handles the wholly-expired case),
    so only *mixed* staleness within one snapshot counts as poisoned here."""
    by_snapshot: dict[tuple[str, object], list[Observation]] = defaultdict(list)
    for obs in raw:
        by_snapshot[(obs.source_id, obs.collected_at)].append(obs)
    poisoned = set()
    for key, group in by_snapshot.items():
        if len(group) < 2:
            continue
        expired_flags = {o.is_expired(as_of) for o in group}
        if len(expired_flags) > 1:  # both True and False present -> mixed staleness
            poisoned.add(key)
    return poisoned


def evaluate(instrument: str, ds: DataSource, cfg: Config, registry: SourceRegistry) -> Layer0Result:
    conditions: list[ConditionResult] = []
    as_of = ds.get_as_of()

    # --- 0.10 record integrity: compute the clean set everything else uses --------
    raw = ds.observations_including_expired(instrument, lookback_seconds=RECORD_INTEGRITY_LOOKBACK_SECONDS)
    poisoned = _find_poisoned_snapshots(raw, as_of)
    all_current = ds.observations(instrument)
    clean = [o for o in all_current if (o.source_id, o.collected_at) not in poisoned]
    conditions.append(
        ConditionResult(
            name="record_integrity",
            status="pass",
            computed_value=len(poisoned),
            threshold=0,
            detail=(
                f"{len(poisoned)} snapshot(s) discarded whole because a sibling field had expired"
                if poisoned
                else "no partially-expired snapshots found"
            ),
        )
    )

    # --- 0.2 freshness: defensive re-check on the clean set ------------------------
    expired_in_clean = [o for o in clean if o.is_expired(as_of)]
    conditions.append(
        ConditionResult(
            name="freshness",
            status="pass" if not expired_in_clean else "fail",
            computed_value=len(expired_in_clean),
            threshold=0,
            detail="no observation past its expiry may reach a gate",
        )
    )

    # --- 0.3 independence, per decision metric -------------------------------------
    min_independent = int(cfg.get("min_independent", default=2))
    per_metric_counts: dict[str, int] = {}
    metrics_with_data = [m for m in DECISION_METRICS if any(o.metric == m for o in clean)]
    for metric in metrics_with_data:
        source_ids = {o.source_id for o in clean if o.metric == metric}
        per_metric_counts[metric.value] = registry.independent_upstream_count(source_ids)
    if not metrics_with_data:
        conditions.append(
            ConditionResult(
                name="independence",
                status="unknown",
                computed_value=None,
                threshold=min_independent,
                detail="no decision-path metrics observed at all",
            )
        )
    else:
        worst_metric = min(per_metric_counts, key=per_metric_counts.get)
        worst_count = per_metric_counts[worst_metric]
        conditions.append(
            ConditionResult(
                name="independence",
                status="pass" if worst_count >= min_independent else "fail",
                computed_value=per_metric_counts,
                threshold=min_independent,
                detail=f"weakest metric is {worst_metric!r} with {worst_count} independent upstream(s)",
            )
        )

    # --- 0.4 / 0.8 dispersion, per metric with >=2 venues reporting -----------------
    max_dispersion_cfg = cfg.get("max_dispersion", default={})
    for metric_name, ceiling in max_dispersion_cfg.items():
        try:
            metric = Metric(metric_name)
        except ValueError:
            continue
        per_venue = latest_per_venue([o for o in clean if o.metric == metric])
        if len(per_venue) < 2:
            continue  # dispersion is not computable with a single venue — not a failure
        values = [v.value for v in per_venue.values()]
        lo, hi = min(values), max(values)
        median = sorted(values)[len(values) // 2]
        if median == 0:
            continue
        dispersion = (hi - lo) / abs(median)
        ceiling_dec = Decimal(str(ceiling))
        conditions.append(
            ConditionResult(
                name=f"dispersion_{metric_name}",
                status="pass" if dispersion <= ceiling_dec else "fail",
                computed_value=dispersion,
                threshold=ceiling_dec,
                detail=f"(max-min)/median across {len(per_venue)} venues; logged as a signal, never averaged away",
            )
        )

    # --- 0.5 reconciliation: immutable-ish facts (total supply) across sources -----
    coherence_tol = Decimal(str(cfg.get("coherence_tol", default=0.03)))
    supply_obs = [o for o in clean if o.metric == Metric.SUPPLY_TOTAL]
    per_source_supply = latest_per_venue(supply_obs)  # keyed by venue, good enough proxy for "per source"
    if len(per_source_supply) >= 2:
        values = [v.value for v in per_source_supply.values()]
        lo, hi = min(values), max(values)
        median = sorted(values)[len(values) // 2]
        agree = median != 0 and (hi - lo) / abs(median) <= coherence_tol
        conditions.append(
            ConditionResult(
                name="reconciliation_total_supply",
                status="pass" if agree else "fail",
                computed_value=values,
                threshold=coherence_tol,
                detail="total supply must reconcile across independent sources within tolerance",
            )
        )

    # --- 0.5 coherence: a venue's own futures mark vs its own spot last ------------
    price_by_source = {o.source_id: o.value for o in clean if o.metric == Metric.PRICE}
    venue_pairs = [
        ("binance_futures", "binance_spot"),
        ("bybit_futures", "bybit_spot"),
        ("okx_futures", "okx_spot"),
    ]
    for futures_id, spot_id in venue_pairs:
        if futures_id in price_by_source and spot_id in price_by_source:
            fut, spot = price_by_source[futures_id], price_by_source[spot_id]
            if spot == 0:
                continue
            divergence = abs(fut - spot) / spot
            conditions.append(
                ConditionResult(
                    name=f"coherence_{futures_id}",
                    status="pass" if divergence <= coherence_tol else "fail",
                    computed_value=divergence,
                    threshold=coherence_tol,
                    detail=f"{futures_id} mark price vs {spot_id} last price, same venue",
                )
            )

    # --- 0.1 tier sufficiency: every decision metric is T1, or T2 cross-confirmed --
    for metric in metrics_with_data:
        metric_obs = [o for o in clean if o.metric == metric]
        has_t1 = any(o.tier.value == "T1" for o in metric_obs)
        independent_count = per_metric_counts.get(metric.value, 0)
        sufficient = has_t1 or independent_count >= min_independent
        conditions.append(
            ConditionResult(
                name=f"tier_sufficiency_{metric.value}",
                status="pass" if sufficient else "fail",
                computed_value="T1 present" if has_t1 else f"T2 x{independent_count}",
                threshold="T1, or T2 with independent cross-confirmation",
                detail=f"tier sufficiency for {metric.value}",
            )
        )

    return Layer0Result(gate_result=GateResult(gate="layer_0", conditions=tuple(conditions)), clean_observations=clean)
