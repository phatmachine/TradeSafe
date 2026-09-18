"""Open-interest history: a change in the aggregate series must only ever mean a change in
positioning, never a change in which venues happen to be reporting — which matters most
once 30 days of Binance-only history sit behind four venues of live data.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from backend.compute.oi import aggregate_oi_series
from backend.core.config import load_config
from backend.core.observation import Metric, Observation, Tier, Unit
from backend.replay.source import LiveSource
from backend.scripts import backfill_history
from backend.store import db

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
BAR = 900


def oi(cfg, venue, value, minutes):
    return Observation.build(
        metric=Metric.OI_COIN, instrument="ZEC", value=Decimal(value), unit=Unit.COINS, venue=venue,
        source_id=f"{venue}_futures", tier=Tier.T1, observed_at=T0 + timedelta(minutes=minutes), cfg=cfg,
    )


def test_a_venue_joining_is_not_read_as_positions_opening():
    cfg = load_config()
    history = [oi(cfg, "binance", 100, m) for m in range(0, 120, 15)]
    history += [oi(cfg, "okx", 400, m) for m in range(60, 120, 15)]  # okx arrives an hour in
    series = aggregate_oi_series(history, BAR)
    assert [v for _, v in series] == [Decimal(100)] * 8  # binance only, the whole span

    # Over a span where both venues report at both ends, both are summed.
    later = aggregate_oi_series(history, BAR, start=T0 + timedelta(minutes=60))
    assert [v for _, v in later] == [Decimal(500)] * 4


def test_a_missed_poll_is_not_read_as_positions_closing():
    cfg = load_config()
    history = [oi(cfg, "binance", 100, m) for m in range(0, 90, 15)]
    history += [oi(cfg, "okx", 400, m) for m in (0, 15, 45, 60, 75)]  # okx misses the 30-min poll
    series = aggregate_oi_series(history, BAR)
    assert [v for _, v in series] == [Decimal(500)] * 6  # carried forward, no dip


def test_a_reading_older_than_the_staleness_limit_is_not_carried_forward():
    cfg = load_config()
    history = [oi(cfg, "binance", 100, m) for m in range(0, 240, 15)]
    history += [oi(cfg, "okx", 400, m) for m in (0, 225)]  # okx silent for over 3 hours
    series = aggregate_oi_series(history, BAR)
    # Buckets where okx's last reading is over an hour stale are dropped, not summed short.
    assert all(v == Decimal(500) for _, v in series)
    assert len(series) < 16


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeBinance:
    """openInterestHist: one row per 15 minutes in [startTime, endTime]."""

    def __init__(self):
        self.calls = 0

    def get(self, url, params, timeout):
        self.calls += 1
        step = BAR * 1000
        first = -(-params["startTime"] // step) * step
        rows = [
            {"timestamp": t, "sumOpenInterest": "1000.5", "sumOpenInterestValue": "0"}
            for t in range(first, params["endTime"] + 1, step)
        ]
        return _FakeResponse(rows)


def test_oi_backfill_covers_a_month_but_never_reaches_a_gate():
    cfg = load_config()
    client = _FakeBinance()
    n = backfill_history.backfill_open_interest("ZEC", cfg, client=client)
    assert n > 29 * 96  # ~30 days of 15-minute readings

    with db.get_connection() as conn:
        stored = conn.execute(
            "SELECT MIN(observed_at), MAX(observed_at), COUNT(*), COUNT(DISTINCT observed_at) FROM observations "
            "WHERE metric = 'oi_coin' AND source_id = 'binance_futures_backfill'"
        ).fetchone()
        assert stored[2] == stored[3] == n  # page edges overlapped, but no timestamp stored twice
        newest = datetime.fromisoformat(stored[1])
        assert datetime.now(timezone.utc) - newest > timedelta(hours=1)
        # Every backfilled row is already past its half-life: none is a current reading.
        current = LiveSource(conn).observations("ZEC", metric=Metric.OI_COIN)
        assert current == []
        history = LiveSource(conn).observations_including_expired("ZEC", lookback_seconds=31 * 86400)
        assert sum(1 for o in history if o.metric == Metric.OI_COIN) == n

    # One-off: a second run makes no request at all.
    calls = client.calls
    assert backfill_history.backfill_open_interest("ZEC", cfg, client=client) == 0
    assert client.calls == calls
