"""The five invariants the implementation spec calls out as mattering more than
coverage: an expired observation must never reach a gate; a source sharing an
upstream_id must not satisfy independence; replay must be provably incapable of
returning a future observation; a partially-rejected source must contribute nothing; and
the same instrument and as_of must produce a byte-identical report on re-run.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from backend.core.config import load_config
from backend.core.observation import Metric, Observation, Tier, Unit
from backend.core.registry import SourceRegistry
from backend.gates import layer_0
from backend.replay.source import LiveSource, ReplaySource
from backend.report.contract import run_analysis
from backend.store import db

NOW = datetime.now(timezone.utc)


def mk(cfg, metric, value, unit, venue, source_id, *, tier=Tier.T1, observed_at=None, collected_at=None):
    return Observation.build(
        metric=metric,
        instrument="ZEC",
        value=Decimal(str(value)),
        unit=unit,
        venue=venue,
        source_id=source_id,
        tier=tier,
        observed_at=observed_at or NOW,
        collected_at=collected_at,
        cfg=cfg,
    )


def test_expired_observation_never_reaches_a_gate():
    cfg = load_config()
    stale = mk(cfg, Metric.PRICE, 100, Unit.USD, "binance", "binance_futures", observed_at=NOW - timedelta(hours=2))
    with db.get_connection() as conn:
        db.insert_observation(conn, stale)
        ds = LiveSource(conn)
        result = ds.observations("ZEC", metric=Metric.PRICE)
    assert result == [], "an expired PRICE observation (30s half-life) must not be returned by the live query path"


def test_shared_upstream_id_does_not_satisfy_independence():
    cfg = load_config()
    registry = SourceRegistry.from_config(cfg)
    # binance_futures and binance_spot share upstream_id "binance" in sources.yaml.
    count = registry.independent_upstream_count(["binance_futures", "binance_spot"])
    assert count == 1
    assert registry.independence_satisfied(["binance_futures", "binance_spot"], cfg) is False
    # A genuinely different upstream brings it to 2.
    count2 = registry.independent_upstream_count(["binance_futures", "bybit_futures"])
    assert count2 == 2


def test_replay_cannot_see_the_future():
    cfg = load_config()
    as_of = NOW
    future = mk(cfg, Metric.PRICE, 999, Unit.USD, "binance", "binance_futures", observed_at=NOW + timedelta(days=1))
    present = mk(cfg, Metric.PRICE, 100, Unit.USD, "binance", "binance_futures", observed_at=NOW)
    with db.get_connection() as conn:
        db.insert_observation(conn, future)
        db.insert_observation(conn, present)
        replay = ReplaySource(conn, as_of=as_of)
        current_view = replay.observations("ZEC", metric=Metric.PRICE)
        historical_view = replay.observations_including_expired("ZEC", lookback_seconds=3600)
    assert all(o.value != Decimal("999") for o in current_view)
    assert all(o.value != Decimal("999") for o in historical_view), (
        "observations_including_expired relaxes the expiry filter, never the observed_at <= as_of filter"
    )


def test_partially_rejected_snapshot_contributes_nothing():
    """Two fields from the SAME fetch (same source_id + collected_at): price already
    expired (30s half-life), funding still within its 1h half-life. Doctrine 0.10 says
    the whole snapshot is discarded, not just the expired field."""
    cfg = load_config()
    registry = SourceRegistry.from_config(cfg)
    shared_collected_at = NOW - timedelta(minutes=50)
    stale_sibling_observed_at = NOW - timedelta(minutes=50)  # price: 30s TTL -> long expired
    fresh_sibling_observed_at = NOW - timedelta(minutes=50)  # funding: 1h TTL -> still valid alone

    price_obs = mk(
        cfg, Metric.PRICE, 100, Unit.USD, "binance", "binance_futures",
        observed_at=stale_sibling_observed_at, collected_at=shared_collected_at,
    )
    funding_obs = mk(
        cfg, Metric.FUNDING_8H, 0.01, Unit.PCT_8H, "binance", "binance_futures",
        observed_at=fresh_sibling_observed_at, collected_at=shared_collected_at,
    )
    # A second venue so independence/tier checks don't block the test on an unrelated axis.
    funding_obs_2 = mk(cfg, Metric.FUNDING_8H, 0.011, Unit.PCT_8H, "bybit", "bybit_futures", observed_at=NOW)

    with db.get_connection() as conn:
        db.insert_observation(conn, price_obs)
        db.insert_observation(conn, funding_obs)
        db.insert_observation(conn, funding_obs_2)
        ds = LiveSource(conn)
        result = layer_0.evaluate("ZEC", ds, cfg, registry)

    funding_values_used = [o.value for o in result.clean_observations if o.metric == Metric.FUNDING_8H and o.venue == "binance"]
    assert funding_values_used == [], (
        "funding from the same poisoned (source_id, collected_at) snapshot as the expired "
        "price must be discarded too, even though funding's own half-life hadn't elapsed"
    )
    record_integrity = next(c for c in result.gate_result.conditions if c.name == "record_integrity")
    assert record_integrity.computed_value == 1


def test_report_is_byte_identical_on_repeat_replay_run():
    cfg = load_config()
    registry = SourceRegistry.from_config(cfg)
    as_of = NOW
    obs = [
        mk(cfg, Metric.PRICE, 100, Unit.USD, "binance", "binance_futures"),
        mk(cfg, Metric.FUNDING_8H, 0.01, Unit.PCT_8H, "binance", "binance_futures"),
    ]
    with db.get_connection() as conn:
        for o in obs:
            db.insert_observation(conn, o)
        report_a = run_analysis("ZEC", ReplaySource(conn, as_of=as_of), cfg, registry)
        report_b = run_analysis("ZEC", ReplaySource(conn, as_of=as_of), cfg, registry)

    assert report_a.to_dict() == report_b.to_dict()
    assert report_a.run_id == report_b.run_id, "run_id must be derived, not random, for reproducibility"
