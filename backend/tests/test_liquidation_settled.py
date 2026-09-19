""""Liquidation print settled" = the last hour of liquidations is back to the coin's usual
level, measured on the same exchanges on both sides of the comparison — not silence, which
some exchange almost never offers and which got rarer with every exchange added.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from backend.core.config import load_config
from backend.core.observation import Metric, Observation, Tier, Unit
from backend.service import collector
from backend.setups.cascade import _liquidation_settled
from backend.sources import coinalyze
from backend.store import db

AS_OF = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


def liq(value, at, venue="binance", source_id="coinalyze"):
    return Observation.build(
        metric=Metric.LIQUIDATION, instrument="ZEC", value=Decimal(value), unit=Unit.USD, venue=venue,
        source_id=source_id, tier=Tier.T2, observed_at=at, cfg=load_config(),
    )


def hourly(value, days, venue="binance", skip_last_hours=1):
    """One print per hour for `days`, leaving the most recent hours empty."""
    return [liq(value, AS_OF - timedelta(hours=h, minutes=30), venue) for h in range(skip_last_hours, days * 24)]


def settled(rows):
    return _liquidation_settled(rows, as_of=AS_OF, quiet_minutes=60, baseline_days=7, multiple=Decimal(1))


def test_back_to_the_usual_hour_passes_and_a_running_burst_fails():
    usual = hourly(5000, 8)
    ok = settled(usual + [liq(-4000, AS_OF - timedelta(minutes=20))])  # long liquidations count too
    assert (ok.status, ok.computed_value, ok.threshold) == ("pass", Decimal(4000), Decimal(5000))
    burst = settled(usual + [liq(-20000, AS_OF - timedelta(minutes=20))])
    assert burst.status == "fail"


def test_an_exchange_without_the_whole_baseline_is_left_out_of_both_sides():
    rows = hourly(5000, 8) + hourly(5000, 2, venue="bybit") + [liq(90000, AS_OF - timedelta(minutes=5), venue="bybit")]
    result = settled(rows)
    assert result.status == "pass"
    assert "binance" in result.detail and "bybit" not in result.detail


def test_no_exchange_with_enough_history_or_still_reporting_is_unknown():
    assert settled(hourly(5000, 3)).status == "unknown"  # nothing reaches back 7 days
    stale = [liq(5000, AS_OF - timedelta(days=d)) for d in range(2, 9)]  # last print 2 days ago
    assert settled(stale).status == "unknown"  # a dead feed can't make the tape look calm
    assert settled([]).status == "unknown"


class _Fake15m:
    """Coinalyze 15-minute history: one short-liquidation total per bar in [from, to]."""

    def __init__(self):
        self.calls = []

    async def get(self, url, headers, params, timeout):
        self.calls.append(params)
        rows = [{"t": t, "l": 0, "s": 100} for t in range(params["from"] // 900 * 900, params["to"], 900)]

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return [{"symbol": s, "history": rows} for s in params["symbols"].split(",")]

        return R()


def test_backfill_fills_only_before_each_exchanges_own_history_and_runs_once(monkeypatch):
    monkeypatch.setenv(coinalyze.API_KEY_ENV, "test-key")
    cfg = load_config()
    okx_direct_from = AS_OF - timedelta(days=2)
    with db.get_connection() as conn:
        db.insert_observation(conn, liq(100, okx_direct_from, venue="okx", source_id="okx_futures"))
    client = _Fake15m()
    assert asyncio.run(collector.backfill_coinalyze_liquidations(["ZEC"], cfg, client=client, now=AS_OF)) > 0

    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT venue, MIN(observed_at), MAX(observed_at) FROM observations WHERE source_id = 'coinalyze' GROUP BY venue"
        ).fetchall()
    ends = {v: datetime.fromisoformat(hi) for v, lo, hi in rows}
    assert set(ends) == {"binance", "bybit", "okx"}
    assert ends["okx"] < okx_direct_from  # stops where OKX's direct feed begins
    assert ends["binance"] < AS_OF - collector.COINALYZE_LOOKBACK  # leaves the last day to the minute feed

    calls = len(client.calls)
    assert asyncio.run(collector.backfill_coinalyze_liquidations(["ZEC"], cfg, client=client, now=AS_OF)) == 0
    assert len(client.calls) == calls
