"""The offline calibration harness: the plumbing that turns stored history into the inputs
the live setup code expects, and the scoring that turns qualifying instants into signals.
"""
from __future__ import annotations

from decimal import Decimal

from backend.core.config import load_config
from backend.research import harness, store

T0 = 1_700_000_100 // 900 * 900  # a 15-minute boundary


def _history(prices: list[tuple[int, str]], funding=None, volumes=None) -> harness.History:
    funding = funding or {}
    volumes = volumes or []
    vol_ts, perp_cum = harness._cumulative([(t, Decimal(p)) for t, p, _ in volumes])
    _, spot_cum = harness._cumulative([(t, Decimal(s)) for t, _, s in volumes])
    return harness.History(
        instrument="ZEC",
        price=[harness.Point(harness.Metric.PRICE, "binance", t, Decimal(v)) for t, v in prices],
        price_ts=[t for t, _ in prices],
        oi=[], oi_ts=[],
        funding={v: ([t for t, _ in rows], [Decimal(x) for _, x in rows]) for v, rows in funding.items()},
        volume_ts=vol_ts, perp_cum=perp_cum, spot_cum=spot_cum,
    )


def test_store_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADESAFE_RESEARCH_DB_PATH", str(tmp_path / "research.db"))
    with store.connection() as conn:
        store.write(conn, "ZEC", store.PRICE, "binance", [(T0, "100.5"), (T0 + 900, "101")])
        store.write(conn, "ZEC", store.PRICE, "binance", [(T0 + 900, "101.25")])  # re-fetch replaces
    with store.connection() as conn:
        assert store.load(conn, "ZEC", store.PRICE, "binance") == [(T0, Decimal("100.5")), (T0 + 900, Decimal("101.25"))]
        assert store.latest_ts(conn, "ZEC", store.PRICE, "binance") == T0 + 900


def test_funding_is_the_last_settled_rate_carried_forward():
    h = _history([(T0, "1")], funding={"binance": [(T0, "0.01"), (T0 + 8 * 3600, "-0.02")]})
    t = T0 + 8 * 3600 + 30 * 60  # 30 minutes after the second settlement
    points = harness._funding_points(h, t, periods=4)
    assert [p.value for p in points] == [Decimal("0.01"), Decimal("-0.02"), Decimal("-0.02"), Decimal("-0.02")]
    assert harness._funding_current(h, t) == Decimal("-0.02")
    assert harness._funding_points(h, T0 - 1) == []  # nothing before the first settlement


def test_rolling_volume_sums_the_last_24_hours_only():
    bars = [(T0 + i * 900, "10", "2") for i in range(200)]
    h = _history([(T0, "1")], volumes=bars)
    end = T0 + 199 * 900
    assert harness._rolling_sum(h.volume_ts, h.perp_cum, end, 86400) == Decimal(10 * 96)
    assert harness._rolling_sum(h.volume_ts, h.spot_cum, end, 86400) == Decimal(2 * 96)
    assert harness._rolling_sum(h.volume_ts, h.perp_cum, T0 + 50 * 900, 86400) is None  # not a full day yet


def test_a_run_of_qualifying_hours_is_one_signal_entered_at_its_start():
    cfg = load_config()
    day = 86400
    # Price rises 1% a day for 40 days: every entry has a known 10-day forward return.
    prices = [(T0 + d * day, str(100 * 1.01 ** d)) for d in range(40)]
    h = _history(prices)
    name = harness.cascade.SETUP_NAME
    q = (True, {"x": "pass", harness.UNSCORED_CONDITION: "unknown"})
    no = (False, {"x": "fail"})
    pattern = [q, q, q, no, q, no, no]  # two episodes: days 0-2 and day 4
    instants = [harness.Instant(t=T0 + d * day, regime="mean_reverting", setups={name: s})
                for d, s in enumerate(pattern)]
    result = harness.score(h, cfg, instants)
    s = result.setups[name]
    assert (s.evaluated, s.qualified_instants, s.episodes) == (7, 4, 2)
    assert len(s.scored) == 2 and all(r > Decimal("0.10") for r in s.scored)  # ~+10.5% over 10 days, long side
    assert s.condition_unknown == {harness.UNSCORED_CONDITION: 4}


def test_signals_too_close_to_the_end_of_the_data_are_not_scored():
    cfg = load_config()
    prices = [(T0 + d * 86400, "100") for d in range(5)]
    h = _history(prices)
    name = harness.continuation.SETUP_NAME
    instants = [harness.Instant(t=T0, regime="trending_up", setups={name: (True, {})})]
    s = harness.score(h, cfg, instants).setups[name]
    assert s.episodes == 1 and s.scored == []  # the 14-day hold runs past the data
