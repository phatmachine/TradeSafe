"""The orchestrator: Gate U -> Layer 0 -> state classification -> setup evaluation ->
report, exactly as the implementation spec's run() pseudocode lays out, short-circuiting
at the first failure so nothing downstream ever sees unvalidated data.

run_id is derived deterministically from (instrument, as_of, config_hash) rather than a
random UUID — the same instrument at the same as_of under the same config must produce a
byte-identical report on re-run (implementation spec, "Tests that matter more than
coverage").
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from enum import Enum

from backend.compute import cohort as cohort_mod
from backend.compute import regime as regime_mod
from backend.compute.oi import aggregate_oi_coin, latest_per_venue
from backend.compute.ratios import carry_ratio, oi_to_market_cap, perp_to_spot_volume
from backend.compute.volatility import atr as atr_fn
from backend.compute.volatility import daily_closes
from backend.core.config import Config
from backend.core.observation import Metric, Observation
from backend.core.registry import SourceRegistry
from backend.gates import gate_u, layer_0
from backend.gates.common import GateResult
from backend.replay.source import DataSource
from backend.setups import cascade, continuation, event, exhaustion


class Verdict(str, Enum):
    GATE_FAIL = "GATE_FAIL"
    NO_SETUP = "NO_SETUP"
    ELIGIBLE_SETUP = "ELIGIBLE_SETUP"
    CLASSIFIER_CONFLICT = "CLASSIFIER_CONFLICT"


def _run_id(instrument: str, as_of, config_hash: str) -> str:
    return hashlib.sha256(f"{instrument}:{as_of.isoformat()}:{config_hash}".encode()).hexdigest()[:24]


def _single_best_venue_series(observations: list[Observation], metric: Metric) -> list[Observation]:
    by_venue: dict[str, list[Observation]] = {}
    for o in observations:
        if o.metric == metric:
            by_venue.setdefault(o.venue, []).append(o)
    return max(by_venue.values(), key=len, default=[])


def _distance_to_flip(gate: GateResult) -> list[dict]:
    """Report contract section 6: for each failing condition, what would satisfy it and
    where the instrument currently sits — "fails flush completion: coin OI down 9% from
    peak against 12% required" rather than a bare "not eligible"."""
    out = []
    for c in gate.failing_conditions:
        out.append(
            {
                "gate": gate.gate,
                "condition": c.name,
                "status": c.status,
                "current_value": None if c.computed_value is None else str(c.computed_value),
                "required": None if c.threshold is None else str(c.threshold),
                "detail": c.detail,
            }
        )
    return out


@dataclass(frozen=True)
class AnalysisReport:
    run_id: str
    instrument: str
    as_of: str
    config_hash: str
    config_validated: bool
    verdict: str
    gate_status: list[dict]
    data_integrity: dict
    state_classification: dict
    setup_evaluation: list[dict]
    distance_to_flip: list[dict]

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "instrument": self.instrument,
            "as_of": self.as_of,
            "config_hash": self.config_hash,
            "config_validated": self.config_validated,
            "verdict": self.verdict,
            "gate_status": self.gate_status,
            "data_integrity": self.data_integrity,
            "state_classification": self.state_classification,
            "setup_evaluation": self.setup_evaluation,
            "distance_to_flip": self.distance_to_flip,
        }


def _data_integrity_summary(instrument: str, ds: DataSource, registry: SourceRegistry, clean: list[Observation]) -> dict:
    used_sources = sorted({o.source_id for o in clean})
    all_sources = sorted(r.source_id for r in registry.enabled_sources())
    rejected = []
    for sid in all_sources:
        if sid in used_sources:
            continue
        rec = registry.get(sid)
        reason = "demoted (repeat failure)" if rec and rec.is_rejected else "no current observation"
        rejected.append({"source_id": sid, "reason": reason})

    independent_by_metric = {}
    for metric in (Metric.PRICE, Metric.FUNDING_8H, Metric.OI_COIN, Metric.PERP_VOLUME, Metric.SPOT_VOLUME):
        source_ids = {o.source_id for o in clean if o.metric == metric}
        if source_ids:
            independent_by_metric[metric.value] = registry.independent_upstream_count(source_ids)

    dispersion = {}
    for metric in (Metric.PRICE, Metric.FUNDING_8H, Metric.OI_COIN):
        per_venue = latest_per_venue([o for o in clean if o.metric == metric])
        if len(per_venue) >= 2:
            values = [v.value for v in per_venue.values()]
            lo, hi, med = min(values), max(values), sorted(values)[len(values) // 2]
            dispersion[metric.value] = str((hi - lo) / abs(med)) if med else None

    return {
        "sources_queried": all_sources,
        "sources_used": used_sources,
        "sources_rejected": rejected,
        "independent_upstream_count_by_metric": independent_by_metric,
        "venue_dispersion_observed": dispersion,
    }


def run_analysis(instrument: str, ds: DataSource, cfg: Config, registry: SourceRegistry) -> AnalysisReport:
    as_of = ds.get_as_of()
    run_id = _run_id(instrument, as_of, cfg.config_hash)

    gu = gate_u.evaluate(instrument, ds, cfg)
    if not gu.passed:
        return AnalysisReport(
            run_id=run_id,
            instrument=instrument,
            as_of=as_of.isoformat(),
            config_hash=cfg.config_hash,
            config_validated=cfg.validated,
            verdict=Verdict.GATE_FAIL.value,
            gate_status=[gu.to_dict()],
            data_integrity={},
            state_classification={},
            setup_evaluation=[],
            distance_to_flip=_distance_to_flip(gu),
        )

    l0 = layer_0.evaluate(instrument, ds, cfg, registry)
    if not l0.gate_result.passed:
        return AnalysisReport(
            run_id=run_id,
            instrument=instrument,
            as_of=as_of.isoformat(),
            config_hash=cfg.config_hash,
            config_validated=cfg.validated,
            verdict=Verdict.GATE_FAIL.value,
            gate_status=[gu.to_dict(), l0.gate_result.to_dict()],
            data_integrity=_data_integrity_summary(instrument, ds, registry, l0.clean_observations),
            state_classification={},
            setup_evaluation=[],
            distance_to_flip=_distance_to_flip(l0.gate_result),
        )

    clean = l0.clean_observations
    gate_status = [gu.to_dict(), l0.gate_result.to_dict()]
    data_integrity = _data_integrity_summary(instrument, ds, registry, clean)

    rv_long_days = int(cfg.get("rv_long_days", default=30))
    history_lookback = rv_long_days * 86400
    all_history = ds.observations_including_expired(instrument, lookback_seconds=history_lookback)
    price_hist_all = [o for o in all_history if o.metric == Metric.PRICE]
    price_hist_single_venue = _single_best_venue_series(price_hist_all, Metric.PRICE)
    oi_hist = [o for o in all_history if o.metric == Metric.OI_COIN]
    funding_hist = [o for o in all_history if o.metric == Metric.FUNDING_8H]
    liq_hist = [o for o in all_history if o.metric == Metric.LIQUIDATION]
    event_hist = [o for o in all_history if o.metric == Metric.EVENT]

    regime_result = regime_mod.classify(price_hist_single_venue, cfg=cfg)
    if regime_result.regime == regime_mod.Regime.UNDETERMINED:
        return AnalysisReport(
            run_id=run_id,
            instrument=instrument,
            as_of=as_of.isoformat(),
            config_hash=cfg.config_hash,
            config_validated=cfg.validated,
            verdict=Verdict.NO_SETUP.value,
            gate_status=gate_status,
            data_integrity=data_integrity,
            state_classification={"regime": regime_result.regime.value, "evidence": _jsonable(regime_result.evidence)},
            setup_evaluation=[],
            distance_to_flip=[],
        )

    cohort_result = cohort_mod.classify(liq_hist, cfg=cfg)

    oi_agg = aggregate_oi_coin([o for o in clean if o.metric == Metric.OI_COIN])
    price_obs = [o for o in clean if o.metric == Metric.PRICE]
    price_current = price_obs[-1].value if price_obs else None
    mcap_obs = [o for o in clean if o.metric == Metric.MARKET_CAP]
    market_cap = mcap_obs[-1].value if mcap_obs else None
    perp_vol = sum((o.value for o in latest_per_venue([o for o in clean if o.metric == Metric.PERP_VOLUME]).values()), Decimal(0)) or None
    spot_vol = sum((o.value for o in latest_per_venue([o for o in clean if o.metric == Metric.SPOT_VOLUME]).values()), Decimal(0)) or None
    funding_obs = [o for o in clean if o.metric == Metric.FUNDING_8H]
    funding_current = sum((o.value for o in funding_obs), Decimal(0)) / len(funding_obs) if funding_obs else None

    daily = daily_closes(price_hist_single_venue)
    bars = [(c, c, c) for c in daily]
    atr_n = int(cfg.get("atr_n", default=14))
    atr_value = atr_fn(bars, atr_n) if len(bars) >= atr_n + 1 else None
    expected_hold_days = Decimal(str(cfg.get("expected_hold_days_default", default=10)))

    constraint_ratios = {
        "oi_coin_total": str(oi_agg.total_coins) if oi_agg.venue_count else None,
        "oi_to_market_cap": _str_or_none(oi_to_market_cap(oi_agg.total_coins if oi_agg.venue_count else None, price_current, market_cap)),
        "perp_to_spot_volume": _str_or_none(perp_to_spot_volume(perp_vol, spot_vol)),
        "carry_to_expected_move": _str_or_none(
            carry_ratio(funding_8h_pct=funding_current, expected_hold_days=expected_hold_days, atr_value=atr_value, price=price_current)
        ),
    }

    state_classification = {
        "regime": regime_result.regime.value,
        "regime_evidence": _jsonable(regime_result.evidence),
        "trapped_cohort": cohort_result.cohort.value,
        "trapped_cohort_evidence": _jsonable(cohort_result.evidence),
        "constraint_ratios": constraint_ratios,
    }

    setup_results: dict[str, GateResult] = {}
    if regime_result.regime == regime_mod.Regime.MEAN_REVERTING:
        setup_results[cascade.SETUP_NAME] = cascade.evaluate(
            instrument,
            oi_history=oi_hist,
            price_history=price_hist_single_venue,
            funding_history=funding_hist,
            liquidation_history=liq_hist,
            as_of=as_of,
            cfg=cfg,
        )
        setup_results[exhaustion.SETUP_NAME] = exhaustion.evaluate(
            instrument,
            oi_history=oi_hist,
            price_history=price_hist_single_venue,
            funding_current=funding_current,
            price_current=price_current,
            cfg=cfg,
        )
    if regime_result.regime == regime_mod.Regime.TRENDING_UP:
        setup_results[continuation.SETUP_NAME] = continuation.evaluate(
            instrument,
            oi_history=oi_hist,
            price_history=price_hist_single_venue,
            funding_history=funding_hist,
            perp_volume_current=perp_vol,
            spot_volume_current=spot_vol,
            cfg=cfg,
        )
    setup_results[event.SETUP_NAME] = event.evaluate(
        instrument, event_history=event_hist, funding_history=funding_hist, as_of=as_of, cfg=cfg
    )

    # Doctrine mutual exclusion: trend continuation and positioning exhaustion can never
    # both qualify. The regime gate above already keeps them from being evaluated
    # together in normal operation; this is the defensive check the doctrine asks for
    # in case that gate is ever loosened.
    if (
        exhaustion.SETUP_NAME in setup_results
        and continuation.SETUP_NAME in setup_results
        and setup_results[exhaustion.SETUP_NAME].passed
        and setup_results[continuation.SETUP_NAME].passed
    ):
        return AnalysisReport(
            run_id=run_id,
            instrument=instrument,
            as_of=as_of.isoformat(),
            config_hash=cfg.config_hash,
            config_validated=cfg.validated,
            verdict=Verdict.CLASSIFIER_CONFLICT.value,
            gate_status=gate_status,
            data_integrity=data_integrity,
            state_classification=state_classification,
            setup_evaluation=[r.to_dict() for r in setup_results.values()],
            distance_to_flip=[],
        )

    any_eligible = any(r.passed for r in setup_results.values())
    distance_to_flip = []
    for r in setup_results.values():
        distance_to_flip.extend(_distance_to_flip(r))

    return AnalysisReport(
        run_id=run_id,
        instrument=instrument,
        as_of=as_of.isoformat(),
        config_hash=cfg.config_hash,
        config_validated=cfg.validated,
        verdict=Verdict.ELIGIBLE_SETUP.value if any_eligible else Verdict.NO_SETUP.value,
        gate_status=gate_status,
        data_integrity=data_integrity,
        state_classification=state_classification,
        setup_evaluation=[r.to_dict() for r in setup_results.values()],
        distance_to_flip=distance_to_flip,
    )


def _str_or_none(v: Decimal | None) -> str | None:
    return None if v is None else str(v)


def persist_report(conn, report: AnalysisReport) -> None:
    from backend.store import db

    db.save_decision_record(
        conn,
        {
            "run_id": report.run_id,
            "instrument": report.instrument,
            "run_at": report.as_of,
            "observation_ids": [],
            "gate_results": report.gate_status,
            "classification": report.state_classification,
            "setups_evaluated": report.setup_evaluation,
            "verdict": report.verdict,
            "config_hash": report.config_hash,
        },
    )


def _jsonable(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, list):
            out[k] = [str(x) for x in v]
        elif isinstance(v, Decimal):
            out[k] = str(v)
        else:
            out[k] = v if isinstance(v, (str, int, float, bool)) or v is None else str(v)
    return out
