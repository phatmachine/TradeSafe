"""The directional paths: every setup that can put Long or Short on the verdict banner
must be reachable from a market state that plainly satisfies it, and its mirror must
read the other way. Before 2026-09-18 no code path could produce either label in
practice, which is what these guard against regressing to.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from backend.compute import cohort as cohort_mod
from backend.core.config import load_config
from backend.core.observation import Metric, Observation, Tier, Unit
from backend.report.contract import _structural_read, _verdict_bias
from backend.service import collector
from backend.setups import cascade, continuation
from backend.sources import okx_liquidations
from backend.store import db

BAR_4H = 4 * 3600
# Aligned to a 4h boundary so every synthetic close lands in its own bar.
T0 = datetime.fromtimestamp((1_789_000_000 // BAR_4H) * BAR_4H, tz=timezone.utc)
AS_OF = T0 + timedelta(seconds=38 * BAR_4H + 600)


def obs(cfg, metric, value, unit, at, *, venue="binance", source_id="binance_futures"):
    return Observation.build(
        metric=metric,
        instrument="ZEC",
        value=Decimal(str(value)),
        unit=unit,
        venue=venue,
        source_id=source_id,
        tier=Tier.T1,
        observed_at=at,
        cfg=cfg,
    )


def _interpolate(knots):
    closes = []
    for (i0, v0), (i1, v1) in zip(knots, knots[1:]):
        for i in range(i0, i1):
            closes.append(Decimal(v0) + (Decimal(v1) - Decimal(v0)) * (i - i0) / (i1 - i0))
    closes.append(Decimal(knots[-1][1]))
    return closes


# An uptrend of confirmed higher lows 100 -> 110 -> 120, a leg from 120 up to 140, then a
# pullback to 136: 20% of the leg given back, the 120 higher low intact.
UPTREND_KNOTS = [(0, 130), (6, 100), (12, 115), (18, 110), (24, 125), (30, 120), (36, 140), (38, 136)]


def _continuation_inputs(cfg, closes, funding_value):
    at = lambda i: T0 + timedelta(seconds=i * BAR_4H + 600)  # noqa: E731
    price = [obs(cfg, Metric.PRICE, c, Unit.USD, at(i)) for i, c in enumerate(closes)]
    # OI read inside the leg's extreme bar (36), lower by the latest bar: leverage flushed.
    oi = [obs(cfg, Metric.OI_COIN, 1000, Unit.COINS, at(36)), obs(cfg, Metric.OI_COIN, 900, Unit.COINS, at(38))]
    funding = [
        obs(cfg, Metric.FUNDING_8H, funding_value, Unit.PCT_8H, AS_OF - timedelta(minutes=m)) for m in (30, 15, 0)
    ]
    return dict(oi_history=oi, price_history=price, funding_history=funding,
                perp_volume_current=Decimal(500), spot_volume_current=Decimal(100), cfg=cfg)


def test_uptrend_pullback_of_the_leg_qualifies_long():
    cfg = load_config()
    result = continuation.evaluate("ZEC", **_continuation_inputs(cfg, _interpolate(UPTREND_KNOTS), -0.001), side="long")
    assert result.gate == continuation.SETUP_NAME
    assert result.passed, [c for c in result.conditions if not c.passed]
    retrace = result.conditions[0].computed_value["retrace_of_leg"]
    assert retrace == Decimal("0.2")


def test_shallow_pullback_does_not_qualify():
    cfg = load_config()
    knots = UPTREND_KNOTS[:-1] + [(38, 139)]  # 5% of the leg — not yet a pullback
    result = continuation.evaluate("ZEC", **_continuation_inputs(cfg, _interpolate(knots), -0.001), side="long")
    assert not result.passed
    assert result.conditions[0].status == "fail"


def test_downtrend_rally_of_the_leg_qualifies_short():
    cfg = load_config()
    mirrored = [Decimal(300) - c for c in _interpolate(UPTREND_KNOTS)]
    result = continuation.evaluate("ZEC", **_continuation_inputs(cfg, mirrored, 0.001), side="short")
    assert result.gate == continuation.SHORT_SETUP_NAME
    assert result.passed, [c for c in result.conditions if not c.passed]
    # The long-side reading of the same mirrored tape must not qualify.
    assert not continuation.evaluate("ZEC", **_continuation_inputs(cfg, mirrored, 0.001), side="long").passed


def _squeeze_inputs(cfg, funding_value):
    at = lambda n: AS_OF - timedelta(minutes=15 * n)  # noqa: E731
    # Coin OI collapses 15% from its peak and holds there for the last three periods.
    oi = [obs(cfg, Metric.OI_COIN, v, Unit.COINS, at(n)) for n, v in ((5, 1000), (4, 990), (3, 850), (2, 850), (1, 850), (0, 850))]
    # A squeeze spike to 112, then three tight closes that all stay under it.
    closes = [100, 110, 112, 108, 107, "107.5"]
    price = [obs(cfg, Metric.PRICE, c, Unit.USD, at(len(closes) - 1 - i)) for i, c in enumerate(closes)]
    funding = [obs(cfg, Metric.FUNDING_8H, funding_value, Unit.PCT_8H, at(n)) for n in (2, 1, 0)]
    # Short-liquidation prints (positive) every hour for 8 days, none in the last 3 hours:
    # back under the usual hour, so settled, and a short-dominant tape in every 24h bucket.
    liqs = [obs(cfg, Metric.LIQUIDATION, 5000, Unit.USD, AS_OF - timedelta(hours=h)) for h in range(3, 8 * 24)]
    return dict(oi_history=oi, price_history=price, funding_history=funding, liquidation_history=liqs, as_of=AS_OF, cfg=cfg)


def test_squeeze_absorption_qualifies_and_reads_short():
    cfg = load_config()
    inputs = _squeeze_inputs(cfg, 0.001)
    squeeze = cascade.evaluate("ZEC", **inputs, side="short")
    assert squeeze.gate == cascade.SHORT_SETUP_NAME
    assert squeeze.passed, [c for c in squeeze.conditions if not c.passed]
    # Positive funding means the long-side cascade's "funding reset" cannot also pass.
    assert not cascade.evaluate("ZEC", **inputs, side="long").passed

    cohort = cohort_mod.classify(inputs["liquidation_history"], cfg=cfg).cohort
    assert cohort == cohort_mod.Cohort.TRAPPED_SHORTS
    read = _structural_read(cascade.SHORT_SETUP_NAME, cohort)
    assert read["direction"] == "short"
    assert _verdict_bias([read]) == "short"


def test_absorption_reads_unclear_when_cohort_disagrees():
    for name, cohort in (
        (cascade.SETUP_NAME, cohort_mod.Cohort.TRAPPED_SHORTS),
        (cascade.SHORT_SETUP_NAME, cohort_mod.Cohort.TRAPPED_LONGS),
        (cascade.SHORT_SETUP_NAME, cohort_mod.Cohort.UNNAMED),
    ):
        assert _structural_read(name, cohort)["direction"] == "unclear"
    assert _structural_read(cascade.SETUP_NAME, cohort_mod.Cohort.TRAPPED_LONGS)["direction"] == "long"
    assert _structural_read(continuation.SHORT_SETUP_NAME, cohort_mod.Cohort.UNNAMED)["direction"] == "short"


def test_okx_liquidation_row_units_and_sign():
    cfg = load_config()
    row = {"side": "buy", "posSide": "short", "sz": "1.22", "bkPx": "77379.1", "ts": "1789704175823"}
    short_liq = okx_liquidations.to_observation("BTC", row, Decimal("0.01"), cfg)
    # 1.22 contracts x 0.01 BTC per contract x $77,379.1 — contracts are not coins.
    assert short_liq.value == Decimal("944.02502")
    long_liq = okx_liquidations.to_observation("BTC", {**row, "side": "sell", "posSide": "long"}, Decimal("0.01"), cfg)
    assert long_liq.value == Decimal("-944.02502")


def test_overlapping_okx_polls_never_store_a_print_twice(monkeypatch):
    cfg = load_config()
    prints = [
        obs(cfg, Metric.LIQUIDATION, v, Unit.USD, AS_OF + timedelta(seconds=s), venue="okx", source_id="okx_futures")
        for s, v in ((0, 100), (10, -200), (20, 300))
    ]
    polls = iter([prints[:2], prints[1:]])  # the second page overlaps the first by one row

    async def fake_fetch_since(instrument, cfg, *, client, since):
        return [p for p in next(polls) if since is None or p.observed_at >= since]

    monkeypatch.setattr(okx_liquidations, "fetch_since", fake_fetch_since)
    assert asyncio.run(collector.collect_okx_liquidations("ZEC", cfg, client=None)) == 2
    assert asyncio.run(collector.collect_okx_liquidations("ZEC", cfg, client=None)) == 1
    with db.get_connection() as conn:
        stored = conn.execute("SELECT value FROM observations WHERE metric = 'liquidation' ORDER BY observed_at").fetchall()
    assert [r[0] for r in stored] == ["100", "-200", "300"]
