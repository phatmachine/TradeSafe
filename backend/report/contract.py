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
from dataclasses import dataclass, field
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
from backend.core.observation import MACRO, Metric, Observation
from backend.core.registry import SourceRegistry
from backend.gates import gate_u, layer_0
from backend.gates.common import GateResult
from backend.replay.source import DataSource
from backend.report import factors as factors_mod
from backend.setups import cascade, continuation, event, exhaustion


class Verdict(str, Enum):
    GATE_FAIL = "GATE_FAIL"
    NO_SETUP = "NO_SETUP"
    ELIGIBLE_SETUP = "ELIGIBLE_SETUP"
    CLASSIFIER_CONFLICT = "CLASSIFIER_CONFLICT"


def _run_id(instrument: str, as_of, config_hash: str) -> str:
    return hashlib.sha256(f"{instrument}:{as_of.isoformat()}:{config_hash}".encode()).hexdigest()[:24]


# Which side each setup's conditions argue for, shown on its card. Gate U and Layer 0 are
# "not_directional": they decide whether the report can be trusted at all, not which way
# price goes (see report/factors.py for the evidence that does carry a direction).
SETUP_CASE = {
    cascade.SETUP_NAME: "long",
    continuation.SETUP_NAME: "long",
    cascade.SHORT_SETUP_NAME: "short",
    continuation.SHORT_SETUP_NAME: "short",
    exhaustion.SETUP_NAME: "unclear",
    event.SETUP_NAME: "unclear",
}


def _gate_dict(gate: GateResult) -> dict:
    return {**gate.to_dict(), "case": "not_directional"}


def _setup_dicts(setup_results: dict[str, GateResult]) -> list[dict]:
    return [{**r.to_dict(), "case": SETUP_CASE.get(name, "unclear")} for name, r in setup_results.items()]


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


def _structural_read(setup_name: str, cohort: cohort_mod.Cohort) -> dict:
    """A stated-as-fact, non-imperative read of which side a qualifying setup's own
    evidence points toward — never "buy"/"sell", only what the setup + trapped-cohort
    classification together already say. `direction` is "long" | "short" | "unclear".
    Each directional setup has a mirror: cascade/squeeze absorption (read together with
    the trapped cohort, which must agree) and up/down trend continuation (regime-gated,
    so the regime already fixes the side)."""
    if setup_name in (cascade.SETUP_NAME, cascade.SHORT_SETUP_NAME):
        is_long = setup_name == cascade.SETUP_NAME
        expected = cohort_mod.Cohort.TRAPPED_LONGS if is_long else cohort_mod.Cohort.TRAPPED_SHORTS
        if cohort == expected:
            return {
                "direction": "long" if is_long else "short",
                "read": (
                    "Long — forced-selling cascade absorbed; the trapped_longs cohort finished "
                    "capitulating (doctrine: enter only after a cohort is confirmed destroyed)."
                    if is_long
                    else "Short — forced-buying squeeze absorbed; the trapped_shorts cohort finished "
                    "covering, so the buying that lifted price was forced and is now spent."
                ),
            }
        return {
            "direction": "unclear",
            "read": (
                f"Not determinable — this setup's own conditions describe a "
                f"{'forced-selling washout of longs' if is_long else 'forced-buying squeeze of shorts'}, "
                f"but the trapped cohort came out {cohort.value}; treat this as conflicting "
                "evidence, not a clean read."
            ),
        }
    if setup_name == continuation.SETUP_NAME:
        return {
            "direction": "long",
            "read": (
                "Long — trend_continuation_leverage_reset only ever evaluates in a confirmed "
                "uptrend (regime-gated in report/contract.py): a pullback that flushed leverage "
                "without breaking the prior higher low."
            ),
        }
    if setup_name == continuation.SHORT_SETUP_NAME:
        return {
            "direction": "short",
            "read": (
                "Short — downtrend_continuation_leverage_reset only ever evaluates in a confirmed "
                "downtrend (regime-gated in report/contract.py): a rally that flushed leverage "
                "without breaking the prior lower high."
            ),
        }
    return {
        "direction": "unclear",
        "read": (
            "Not determinable from this setup alone — it identifies exhausted or one-sided "
            f"positioning but doesn't encode which way it resolves; pair trapped_cohort "
            f"({cohort.value}) and regime with your own read of the market."
        ),
    }


def _verdict_bias(structural_reads: list[dict]) -> str | None:
    """Rolls per-setup directions up to one banner-level bias. None means the question
    doesn't apply (no eligible setup at all); "unclear" means a setup qualified but
    couldn't be read directionally, or different setups disagreed."""
    if not structural_reads:
        return None
    directions = {r["direction"] for r in structural_reads}
    if directions == {"long"}:
        return "long"
    if directions == {"short"}:
        return "short"
    return "unclear"


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
    structural_reads: list[dict]
    verdict_bias: str | None
    directional_factors: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "instrument": self.instrument,
            "as_of": self.as_of,
            "config_hash": self.config_hash,
            "config_validated": self.config_validated,
            "verdict": self.verdict,
            "verdict_bias": self.verdict_bias,
            "gate_status": self.gate_status,
            "data_integrity": self.data_integrity,
            "state_classification": self.state_classification,
            "setup_evaluation": self.setup_evaluation,
            "distance_to_flip": self.distance_to_flip,
            "structural_reads": self.structural_reads,
            "directional_factors": self.directional_factors,
        }


def _data_integrity_summary(instrument: str, ds: DataSource, registry: SourceRegistry, clean: list[Observation]) -> dict:
    used_sources = sorted({o.source_id for o in clean})
    all_sources = sorted(r.source_id for r in registry.enabled_sources())
    rejected = []
    for sid in all_sources:
        if sid in used_sources:
            continue
        rec = registry.get(sid)
        if rec and not rec.is_rejected and set(rec.metrics) <= {Metric.EVENT.value}:
            # Calendar sources give dated, market-wide events that the event setup reads
            # as history — never a current reading of this coin — so their absence from
            # `clean` is not a rejection.
            continue
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
            gate_status=[_gate_dict(gu)],
            data_integrity={},
            state_classification={},
            setup_evaluation=[],
            distance_to_flip=_distance_to_flip(gu),
            structural_reads=[],
            verdict_bias=None,
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
            gate_status=[_gate_dict(gu), _gate_dict(l0.gate_result)],
            data_integrity=_data_integrity_summary(instrument, ds, registry, l0.clean_observations),
            state_classification={},
            setup_evaluation=[],
            distance_to_flip=_distance_to_flip(l0.gate_result),
            structural_reads=[],
            verdict_bias=None,
        )

    clean = l0.clean_observations
    gate_status = [_gate_dict(gu), _gate_dict(l0.gate_result)]
    data_integrity = _data_integrity_summary(instrument, ds, registry, clean)

    rv_long_days = int(cfg.get("rv_long_days", default=30))
    history_lookback = rv_long_days * 86400
    all_history = ds.observations_including_expired(
        instrument,
        lookback_seconds=history_lookback,
        metrics=(Metric.PRICE, Metric.OI_COIN, Metric.FUNDING_8H, Metric.PERP_VOLUME, Metric.SPOT_VOLUME, Metric.EVENT),
    )
    price_hist_all = [o for o in all_history if o.metric == Metric.PRICE]
    price_hist_single_venue = _single_best_venue_series(price_hist_all, Metric.PRICE)
    oi_hist = [o for o in all_history if o.metric == Metric.OI_COIN]
    funding_hist = [o for o in all_history if o.metric == Metric.FUNDING_8H]
    # The instrument's own events plus the market-wide macro calendar (sources/calendar.py),
    # which applies to every coin and is stored once under MACRO.
    event_hist = [o for o in all_history if o.metric == Metric.EVENT] + ds.observations_including_expired(
        MACRO, lookback_seconds=history_lookback, metrics=(Metric.EVENT,)
    )
    # Liquidations are discrete prints (never compacted) and the busiest coins log
    # thousands a day, so they're read only as far back as anything here looks at them —
    # the cohort window or the print-settled baseline, plus a day so that baseline can
    # tell an exchange reporting from before it began — not the 30 days the level series
    # need.
    liq_lookback_hours = max(
        float(cfg.get("liq_window_hours", default=72)),
        (float(cfg.get("cascade", "liquidation_baseline_days", default=7)) + 1) * 24,
    )
    liq_hist = ds.observations_including_expired(
        instrument,
        lookback_seconds=liq_lookback_hours * 3600,
        metrics=(Metric.LIQUIDATION,),
    )

    regime_result = regime_mod.classify(price_hist_single_venue, cfg=cfg)
    cohort_result = cohort_mod.classify(liq_hist, cfg=cfg)

    perp_now = {v: o.value for v, o in latest_per_venue([o for o in clean if o.metric == Metric.PERP_VOLUME]).items()}
    spot_now = {v: o.value for v, o in latest_per_venue([o for o in clean if o.metric == Metric.SPOT_VOLUME]).items()}
    perp_vol = sum(perp_now.values(), Decimal(0)) or None
    spot_vol = sum(spot_now.values(), Decimal(0)) or None
    # Cross-venue mean of each venue's latest reading — not of every reading still inside
    # funding's 1h half-life, which weighted venues by poll frequency (see compute/funding.py).
    funding_now = [o.value for o in latest_per_venue([o for o in clean if o.metric == Metric.FUNDING_8H]).values()]
    funding_current = sum(funding_now, Decimal(0)) / len(funding_now) if funding_now else None

    # Computed whenever the gates pass — including an undetermined regime, which blocks
    # every setup but still leaves the directional evidence worth reading.
    directional_factors = factors_mod.directional_factors(
        regime=regime_result.regime,
        cohort=cohort_result.cohort,
        funding_current=funding_current,
        oi_history=oi_hist,
        price_history=price_hist_single_venue,
        perp_now=perp_now,
        spot_now=spot_now,
        perp_history=[o for o in all_history if o.metric == Metric.PERP_VOLUME],
        spot_history=[o for o in all_history if o.metric == Metric.SPOT_VOLUME],
        as_of=as_of,
        cfg=cfg,
    )

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
            structural_reads=[],
            verdict_bias=None,
            directional_factors=directional_factors,
        )

    oi_agg = aggregate_oi_coin([o for o in clean if o.metric == Metric.OI_COIN])
    price_obs = [o for o in clean if o.metric == Metric.PRICE]
    price_current = price_obs[-1].value if price_obs else None
    mcap_obs = [o for o in clean if o.metric == Metric.MARKET_CAP]
    market_cap = mcap_obs[-1].value if mcap_obs else None

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
        for side, name in (("long", cascade.SETUP_NAME), ("short", cascade.SHORT_SETUP_NAME)):
            setup_results[name] = cascade.evaluate(
                instrument,
                oi_history=oi_hist,
                price_history=price_hist_single_venue,
                funding_history=funding_hist,
                liquidation_history=liq_hist,
                as_of=as_of,
                cfg=cfg,
                side=side,
            )
        setup_results[exhaustion.SETUP_NAME] = exhaustion.evaluate(
            instrument,
            oi_history=oi_hist,
            price_history=price_hist_single_venue,
            funding_current=funding_current,
            price_current=price_current,
            cfg=cfg,
        )
    trend_side = {
        regime_mod.Regime.TRENDING_UP: ("long", continuation.SETUP_NAME),
        regime_mod.Regime.TRENDING_DOWN: ("short", continuation.SHORT_SETUP_NAME),
    }.get(regime_result.regime)
    if trend_side is not None:
        side, name = trend_side
        setup_results[name] = continuation.evaluate(
            instrument,
            oi_history=oi_hist,
            price_history=price_hist_single_venue,
            funding_history=funding_hist,
            perp_volume_current=perp_vol,
            spot_volume_current=spot_vol,
            cfg=cfg,
            side=side,
        )
    setup_results[event.SETUP_NAME] = event.evaluate(
        instrument, event_history=event_hist, funding_history=funding_hist, as_of=as_of, cfg=cfg
    )

    # Doctrine mutual exclusion: trend continuation (either direction) and positioning
    # exhaustion can never both qualify. The regime gate above already keeps them from
    # being evaluated together in normal operation; this is the defensive check the
    # doctrine asks for in case that gate is ever loosened.
    exhaustion_passed = exhaustion.SETUP_NAME in setup_results and setup_results[exhaustion.SETUP_NAME].passed
    continuation_passed = any(
        name in setup_results and setup_results[name].passed
        for name in (continuation.SETUP_NAME, continuation.SHORT_SETUP_NAME)
    )
    if exhaustion_passed and continuation_passed:
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
            setup_evaluation=_setup_dicts(setup_results),
            distance_to_flip=[],
            structural_reads=[],
            verdict_bias=None,
            directional_factors=directional_factors,
        )

    any_eligible = any(r.passed for r in setup_results.values())
    distance_to_flip = []
    for r in setup_results.values():
        distance_to_flip.extend(_distance_to_flip(r))
    structural_reads = [
        {"setup": name, **_structural_read(name, cohort_result.cohort)}
        for name, r in setup_results.items()
        if r.passed
    ]
    verdict_bias = _verdict_bias(structural_reads)

    return AnalysisReport(
        run_id=run_id,
        instrument=instrument,
        as_of=as_of.isoformat(),
        config_hash=cfg.config_hash,
        config_validated=cfg.validated,
        verdict=Verdict.ELIGIBLE_SETUP.value if any_eligible else Verdict.NO_SETUP.value,
        verdict_bias=verdict_bias,
        gate_status=gate_status,
        data_integrity=data_integrity,
        state_classification=state_classification,
        structural_reads=structural_reads,
        setup_evaluation=_setup_dicts(setup_results),
        distance_to_flip=distance_to_flip,
        directional_factors=directional_factors,
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
