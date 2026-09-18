"""Compaction thins 10-second polls to one row per 15 minutes. It exists purely for speed
and disk, so the guarantees are that it never changes a computed result, never removes a
discrete event, and never changes which snapshots Layer 0 sees as partially expired.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from backend.compute.funding import period_means
from backend.compute.oi import aggregate_oi_series
from backend.compute.regime import resample_closes
from backend.core.config import load_config
from backend.core.observation import Metric, Observation, Tier, Unit
from backend.replay.source import LiveSource
from backend.store import db

NOW = datetime.now(timezone.utc)
START = datetime.fromtimestamp((int((NOW - timedelta(hours=6)).timestamp()) // 900) * 900, tz=timezone.utc)


def obs(cfg, metric, value, at, *, venue="binance", source_id="binance_futures", unit=Unit.USD, collected_at=None):
    return Observation.build(
        metric=metric, instrument="ZEC", value=Decimal(str(value)), unit=unit, venue=venue,
        source_id=source_id, tier=Tier.T1, observed_at=at, cfg=cfg, collected_at=collected_at or at,
    )


def _store(rows):
    with db.get_connection() as conn:
        for o in rows:
            db.insert_observation(conn, o)


def _compact():
    with db.get_connection() as conn:
        return db.compact_observations(conn, instrument="ZEC", since=START, until=NOW - timedelta(hours=2), now=NOW)


def _history(metrics=None):
    with db.get_connection() as conn:
        return LiveSource(conn).observations_including_expired("ZEC", lookback_seconds=86400, metrics=metrics)


def test_compaction_keeps_the_last_reading_per_bucket_and_changes_no_result():
    cfg = load_config()
    polls = [START + timedelta(seconds=10 * i) for i in range(3 * 360)]  # 3 hours of 10s polls
    rows = []
    for i, at in enumerate(polls):
        rows.append(obs(cfg, Metric.PRICE, 100 + i % 37, at))
        rows.append(obs(cfg, Metric.OI_COIN, 1000 + i % 11, at, unit=Unit.COINS))
        rows.append(obs(cfg, Metric.OI_COIN, 500 + i % 7, at, venue="okx", source_id="okx_futures", unit=Unit.COINS))
    _store(rows)

    before = _history()
    closes_before = resample_closes([o for o in before if o.metric == Metric.PRICE], 900)
    oi_before = aggregate_oi_series(before, 900)

    removed = _compact()
    after = _history()
    assert removed > 0 and len(after) == len(before) - removed
    assert resample_closes([o for o in after if o.metric == Metric.PRICE], 900) == closes_before
    assert aggregate_oi_series(after, 900) == oi_before

    # Every row here is older than the 2-hour cutoff, so each hour is left with 4 rows per series.
    first_hour = [o for o in after if o.observed_at < START + timedelta(hours=1) and o.metric == Metric.PRICE]
    assert len(first_hour) == 4
    assert _compact() == 0  # idempotent


def test_funding_period_means_are_unchanged_by_compaction():
    cfg = load_config()
    rows = []
    for i in range(3 * 360):  # binance every 10s, hyperliquid every 60s, different rates
        at = START + timedelta(seconds=10 * i)
        rows.append(obs(cfg, Metric.FUNDING_8H, Decimal("0.01") + Decimal(i % 13) / 1000, at, unit=Unit.PCT_8H))
        if i % 6 == 0:
            rows.append(obs(cfg, Metric.FUNDING_8H, Decimal("-0.02") + Decimal(i % 5) / 1000, at,
                            venue="hyperliquid", source_id="hyperliquid", unit=Unit.PCT_8H))
    _store(rows)
    before = period_means(_history((Metric.FUNDING_8H,)), 900)
    assert _compact() > 0
    assert period_means(_history((Metric.FUNDING_8H,)), 900) == before


def test_liquidation_prints_are_never_compacted():
    cfg = load_config()
    prints = [obs(cfg, Metric.LIQUIDATION, 1000 + i, START + timedelta(seconds=5 * i), venue="okx", source_id="okx_futures")
              for i in range(100)]
    _store(prints)
    assert _compact() == 0
    assert len(_history((Metric.LIQUIDATION,))) == 100


def test_rows_whose_snapshot_is_still_partly_live_are_left_for_layer_0():
    cfg = load_config()
    rows = []
    for i in range(6):  # six CoinGecko-style fetches inside one 15-minute bucket, 5h ago
        at = START + timedelta(minutes=60 + i)
        # market cap expires in 1h, supply in a week: each fetch is a partly-live snapshot
        rows.append(obs(cfg, Metric.MARKET_CAP, 10**9 + i, at, venue="coingecko", source_id="coingecko", collected_at=at))
        rows.append(obs(cfg, Metric.SUPPLY_TOTAL, 21 * 10**6, at, venue="coingecko", source_id="coingecko",
                        unit=Unit.COINS, collected_at=at))
    _store(rows)
    assert _compact() == 0
    assert len(_history((Metric.MARKET_CAP,))) == 6


def test_history_reads_return_only_the_requested_metrics_without_payloads():
    cfg = load_config()
    at = START + timedelta(minutes=5)
    price = obs(cfg, Metric.PRICE, 100, at)
    _store([Observation(**{**price.__dict__, "raw": {"big": "payload"}}), obs(cfg, Metric.FUNDING_8H, 0.01, at, unit=Unit.PCT_8H)])
    history = _history((Metric.PRICE,))
    assert [o.metric for o in history] == [Metric.PRICE]
    assert history[0].raw == {}
