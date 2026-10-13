"""Report section 8 (leverage risk) and the setup track records: context added after the
decision. Neither may change a verdict, a setup, config_hash or a DecisionRecord; the
track records only decide whether a qualifying setup raises an alert.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backend.compute.leverage import leverage_risk, liquidation_move
from backend.core.config import load_config
from backend.core.observation import Metric, Observation, Tier, Unit
from backend.core.registry import SourceRegistry
from backend.replay.source import LiveSource, ReplaySource
from backend.report import context
from backend.report.belief import build_report
from backend.report.contract import persist_report, run_analysis
from backend.service import scanner
from backend.store import db

CFG = load_config()
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def prices(values, *, step_hours=4, venue="binance"):
    return [
        Observation.build(
            metric=Metric.PRICE, instrument="ZEC", value=Decimal(str(v)), unit=Unit.USD, venue=venue,
            source_id="binance_futures", tier=Tier.T1, observed_at=T0 + timedelta(hours=step_hours * i), cfg=CFG,
        )
        for i, v in enumerate(values)
    ]


def risk(values, **kw):
    args = dict(leverages=[2, 3, 5, 10], maintenance_margin_rate=0.01, hold_days=10, bar_seconds=14400,
                min_history_days=17)
    args.update(kw)
    return leverage_risk(prices(values), **args)


# --- Leverage arithmetic ----------------------------------------------------------------------


def test_liquidating_move():
    assert liquidation_move(5, 0.01) == pytest.approx(0.19)
    assert liquidation_move(2, 0.01) == pytest.approx(0.49)
    assert liquidation_move(1, 0.01) == pytest.approx(0.99)
    with pytest.raises(ValueError):
        liquidation_move(0, 0.01)


def test_flat_price_never_breaches():
    r = risk([100.0] * 180)  # 30 days of 4h bars
    assert r.status == "ok" and r.windows == 180 - 60
    assert all(row.long_breach_rate == 0 and row.short_breach_rate == 0 for row in r.rows)


def test_a_drop_breaches_the_leverage_it_exceeds_and_no_lower():
    # Flat, then a 30% drop held: a long opened before the drop loses 30% inside its window.
    r = risk([100.0] * 100 + [70.0] * 80)
    by_lev = {row.leverage: row for row in r.rows}
    assert by_lev[5].long_breach_rate > 0      # 5x is liquidated by 19%
    assert by_lev[10].long_breach_rate >= by_lev[5].long_breach_rate
    assert by_lev[3].long_breach_rate == 0     # 3x needs 32.3%
    assert by_lev[2].long_breach_rate == 0
    assert all(row.short_breach_rate == 0 for row in r.rows)  # nothing ever rose


def test_a_rise_breaches_shorts_not_longs():
    r = risk([100.0] * 100 + [130.0] * 80)
    by_lev = {row.leverage: row for row in r.rows}
    assert by_lev[5].short_breach_rate > 0 and by_lev[5].long_breach_rate == 0


def test_too_little_history_is_unknown_not_zero():
    r = risk([100.0] * 60)  # 10 days
    assert r.status == "insufficient_history"
    assert all(row.long_breach_rate is None and row.short_breach_rate is None for row in r.rows)
    assert r.rows[2].liquidating_move == pytest.approx(0.19)  # the arithmetic still stands


def test_volatility_scales_to_the_hold_window():
    r = risk([100.0 * (1.01 if i % 2 else 0.99) for i in range(180)])
    assert r.daily_volatility and r.typical_hold_move == pytest.approx(r.daily_volatility * 10 ** 0.5)
    assert all(row.move_vs_typical == pytest.approx(row.liquidating_move / r.typical_hold_move) for row in r.rows)


# --- Track records and the alert rule ---------------------------------------------------------


def test_the_measured_records_set_each_setups_alert():
    status = {s: context.setup_record(s)["status"] for s in (
        "cascade_absorption", "squeeze_absorption", "downtrend_continuation_leverage_reset",
        "trend_continuation_leverage_reset", "positioning_exhaustion", "event_decompression")}
    assert status == {
        "cascade_absorption": "losing",                     # typical signal lost (median below zero)
        "squeeze_absorption": "losing",                     # average below zero
        "downtrend_continuation_leverage_reset": "losing",
        "trend_continuation_leverage_reset": "unproven",    # 7 signals, under min_signals
        "positioning_exhaustion": "no_direction",
        "event_decompression": "no_direction",
    }
    assert [s for s, st in status.items() if not context.alerts_on(s)] == [
        "cascade_absorption", "squeeze_absorption", "downtrend_continuation_leverage_reset"]


def test_a_setup_with_no_record_still_alerts():
    rec = context.setup_record("some_future_setup")
    assert rec["status"] == "no_record" and rec["alerts"] is True


def test_rule_needs_both_average_and_typical_result_above_zero():
    def ctx(mean, median, signals=50, side="long"):
        raw = {"alert_policy": {"min_signals": 20}, "track_records": {"setups": {"s": {
            "side": side, "hold_days": 10, "signals": signals, "went_its_way": 0.5, "random_entry": 0.5,
            "mean_return": mean, "median_return": median, "worst_return": -0.2, "best_return": 0.2}}}}
        return context.ContextConfig(raw=raw, context_hash="test")
    assert context.setup_record("s", ctx(0.02, 0.01))["status"] == "supported"
    assert context.setup_record("s", ctx(0.02, -0.01))["status"] == "losing"
    assert context.setup_record("s", ctx(-0.02, 0.01))["status"] == "losing"
    assert context.setup_record("s", ctx(-0.02, -0.01, signals=19))["status"] == "unproven"


def test_summary_is_factual_wording():
    text = " ".join(context.setup_record(s)["summary"] for s in (
        "cascade_absorption", "positioning_exhaustion"))
    for word in ("buy", "sell", "should", "enter", "recommend"):
        assert word not in text.lower()


def _qualifying(setup):
    from types import SimpleNamespace
    return SimpleNamespace(instrument="ZEC", verdict="ELIGIBLE_SETUP", verdict_bias="long",
                           setup_evaluation=[{"gate": setup, "passed": True, "case": "long"}],
                           run_id="run", as_of="2026-10-13T00:00:00+00:00")


def test_a_losing_setup_qualifies_without_an_alert_and_keeps_its_episode():
    with db.get_connection() as conn:
        assert scanner.record_scan(conn, _qualifying("cascade_absorption")) == []
        assert db.get_qualifying_setups(conn, "ZEC") == {"cascade_absorption"}
        # Same episode, even with alerts switched on: no late alert for an old episode.
        assert scanner.record_scan(conn, _qualifying("cascade_absorption"), alerts_on=lambda s: True) == []


def test_an_unproven_setup_still_alerts():
    with db.get_connection() as conn:
        alerts = scanner.record_scan(conn, _qualifying("trend_continuation_leverage_reset"))
    assert [a["setup"] for a in alerts] == ["trend_continuation_leverage_reset"]


# --- Context never reaches the decision ---------------------------------------------------------


def _decision_bytes(conn, report) -> bytes:
    persist_report(conn, report)
    row = conn.execute(
        "SELECT run_id, run_at, gate_results, classification, setups_evaluated, verdict, config_hash "
        "FROM decision_records WHERE run_id = ?", (report.run_id,),
    ).fetchone()
    return json.dumps(tuple(row)).encode()


def test_context_leaves_the_decision_byte_identical():
    with db.get_connection() as conn:
        for o in prices([100.0 + i % 7 for i in range(200)]):
            db.insert_observation(conn, o)
        ds = ReplaySource(conn, T0 + timedelta(days=34))  # fixed as_of: same run_id both times
        reg = SourceRegistry.from_config(CFG, db.get_all_source_state(conn))
        decision = run_analysis("ZEC", ds, CFG, reg)
        full = build_report("ZEC", ds, CFG, reg)
        assert full.leverage_risk is not None and full.track_records is not None
        strip = ("belief_context", "leverage_risk", "track_records")
        a = {k: v for k, v in decision.to_dict().items() if k not in strip}
        b = {k: v for k, v in full.to_dict().items() if k not in strip}
        assert a == b
        assert _decision_bytes(conn, decision) == _decision_bytes(conn, full)


def test_context_file_is_not_in_config_hash():
    from backend.core import config as config_mod
    import inspect
    assert "context.yaml" not in inspect.getsource(config_mod.load_config)


def test_a_broken_context_config_leaves_the_decision_standing(monkeypatch):
    def boom():
        raise RuntimeError("bad context.yaml")
    monkeypatch.setattr(context, "load_context_config", boom)
    with db.get_connection() as conn:
        ds = LiveSource(conn)
        reg = SourceRegistry.from_config(CFG, db.get_all_source_state(conn))
        report = build_report("ZEC", ds, CFG, reg)
    assert report.verdict and "bad context.yaml" in report.leverage_risk["error"]
