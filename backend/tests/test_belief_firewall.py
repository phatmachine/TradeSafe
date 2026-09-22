"""T4 belief data (Stocktwits) must be structurally incapable of influencing a gate, a
classification, a setup or the verdict (doctrine 0.1, Layer 1: "sentiment indices used as
timing tools" are excluded). These are the tests the Stocktwits brief asks for, plus the
walls that make them hold: the storage split, the Observation invariants, the registry,
and belief config kept out of config_hash.
"""
from __future__ import annotations

import dataclasses
import importlib
import inspect
import json
import pkgutil
import shutil
from datetime import timedelta
from decimal import Decimal

import pytest

from backend.compute import cohort, funding, oi, regime, volatility
from backend.core import config as config_mod
from backend.core.config import load_belief_config, load_config, reload_config
from backend.core.firewall import BeliefDataInDecisionPath, GuardedSource, decision_path
from backend.core.observation import BELIEF_METRICS, Metric, Observation, Tier, Unit
from backend.core.registry import SourceRegistry
from backend.gates import gate_u, layer_0
from backend.replay.source import ReplaySource
from backend.report import belief, exit_monitor, factors
from backend.report.contract import persist_report, run_analysis
from backend.service import collector
from backend.setups import cascade, continuation, event, exhaustion
from backend.sources import stocktwits
from backend.store import db
from backend.tests.belief_fixtures import AS_OF, INSTRUMENT, fill_decision_store, stream_page

CFG = load_config()


def st_cfg(**overrides) -> stocktwits.StocktwitsConfig:
    return dataclasses.replace(stocktwits.StocktwitsConfig.load(CFG, load_belief_config()), **overrides)


def belief_obs(metric=Metric.ST_MSG_COUNT, value=Decimal(30), at=AS_OF) -> Observation:
    return Observation(
        metric=metric, instrument=INSTRUMENT, value=value, unit=Unit.COUNT, venue="stocktwits",
        source_id="stocktwits", tier=Tier.T4, collected_at=at, observed_at=at, expires_at=at + timedelta(hours=1),
    )


def t1_price(at=AS_OF) -> Observation:
    return Observation.build(
        metric=Metric.PRICE, instrument=INSTRUMENT, value=Decimal(100), unit=Unit.USD, venue="binance",
        source_id="binance_futures", tier=Tier.T1, observed_at=at, collected_at=at, cfg=CFG,
    )


def store_fetch(body: bytes, *, now=AS_OF, instrument=INSTRUMENT, cfg=None) -> stocktwits.BeliefFetch:
    result = stocktwits.collect(instrument, cfg or st_cfg(), lambda url: (200, body), now=now)
    with db.get_connection() as conn:
        db.insert_belief_fetch(
            conn, source_id=stocktwits.SOURCE_ID, instrument=instrument, collected_at=result.collected_at,
            body=result.body, observations=result.observations,
        )
    return result


# 22 tagged (15 Bullish, 7 Bearish) and 3 untagged: enough for st_bull_share at t4_min_tagged 20.
TAGS = ["Bullish"] * 15 + ["Bearish"] * 7 + [None] * 3
BODIES = (
    ["$SOL.X alpenglow is close", "$SOL.X Votor finality", "$SOL.X Solana Price Prediction 2026 https://x.example/p"]
    + ["$SOL.X 100x gem incoming"]
    + [f"$SOL.X note {i}" for i in range(21)]
)


# --- 1. A T4 observation handed to any decision-path entry point raises --------------------


class StubSource:
    """A DataSource whose decision reads return belief data, as a corrupted store would."""

    def __init__(self, rows):
        self._rows = rows

    def get_as_of(self):
        return AS_OF

    def observations(self, instrument, *, metric=None, lookback_seconds=None):
        return list(self._rows)

    def observations_including_expired(self, instrument, *, lookback_seconds, metrics=None):
        return list(self._rows)

    def belief_observations(self, instrument):
        return list(self._rows)


def _entry_points():
    # Hidden at the end of an otherwise clean list: the guard reads every element.
    T4 = [t1_price(), belief_obs()]
    ds = StubSource(T4)
    reg = SourceRegistry.from_config(CFG)
    position = {"position_id": "p", "instrument": INSTRUMENT, "setup": "cascade_absorption",
                "opened_at": AS_OF.isoformat(), "trapped_cohort": {"cohort": "trapped_longs"}}
    return {
        "gate_u.evaluate": lambda: gate_u.evaluate(INSTRUMENT, ds, CFG),
        "layer_0.evaluate": lambda: layer_0.evaluate(INSTRUMENT, ds, CFG, reg),
        "regime.classify": lambda: regime.classify(T4, cfg=CFG),
        "regime.resample_closes": lambda: regime.resample_closes(T4, 3600),
        "cohort.classify": lambda: cohort.classify(T4, cfg=CFG),
        "oi.latest_per_venue": lambda: oi.latest_per_venue(T4),
        "oi.aggregate_oi_coin": lambda: oi.aggregate_oi_coin(T4),
        "oi.aggregate_oi_series": lambda: oi.aggregate_oi_series(T4, 900),
        "funding.period_means": lambda: funding.period_means(T4, 900),
        "volatility.daily_closes": lambda: volatility.daily_closes(T4),
        "cascade.evaluate": lambda: cascade.evaluate(
            INSTRUMENT, oi_history=[], price_history=[], funding_history=[], liquidation_history=T4,
            as_of=AS_OF, cfg=CFG, side="long"),
        "squeeze.evaluate": lambda: cascade.evaluate(
            INSTRUMENT, oi_history=T4, price_history=[], funding_history=[], liquidation_history=[],
            as_of=AS_OF, cfg=CFG, side="short"),
        "continuation.evaluate": lambda: continuation.evaluate(
            INSTRUMENT, oi_history=[], price_history=T4, funding_history=[], perp_volume_current=None,
            spot_volume_current=None, cfg=CFG),
        "event.evaluate": lambda: event.evaluate(INSTRUMENT, event_history=[], funding_history=T4, as_of=AS_OF, cfg=CFG),
        "exhaustion.evaluate": lambda: exhaustion.evaluate(
            INSTRUMENT, oi_history=T4, price_history=[], funding_current=None, price_current=None, cfg=CFG),
        "factors.oi_price_factor": lambda: factors.oi_price_factor(T4, [], AS_OF, CFG),
        "factors.perp_spot_factor": lambda: factors.perp_spot_factor(
            perp_now={}, spot_now={}, perp_history=T4, spot_history=[], price_history=[], as_of=AS_OF, cfg=CFG),
        "factors.directional_factors": lambda: factors.directional_factors(
            regime=regime.Regime.UNDETERMINED, cohort=cohort.Cohort.UNNAMED, funding_current=None, oi_history=[],
            price_history=T4, perp_now={}, spot_now={}, perp_history=[], spot_history=[], as_of=AS_OF, cfg=CFG),
        "exit_monitor.evaluate_exit": lambda: exit_monitor.evaluate_exit(position, ds, CFG),
        "contract.run_analysis": lambda: run_analysis(INSTRUMENT, ds, CFG, reg),
        "a single observation argument": lambda: regime.classify(belief_obs(), cfg=CFG),
    }


@pytest.mark.parametrize("name", list(_entry_points()))
def test_t4_observation_raises_at_every_entry_point(name):
    with pytest.raises(BeliefDataInDecisionPath):
        _entry_points()[name]()


def test_every_decision_path_entry_point_is_guarded():
    """Any public gate/compute/setup/report function taking observations or a DataSource
    must carry @decision_path, so one added later can't skip the firewall."""
    modules = []
    for pkg in ("backend.gates", "backend.compute", "backend.setups"):
        package = importlib.import_module(pkg)
        modules += [importlib.import_module(f"{pkg}.{m.name}") for m in pkgutil.iter_modules(package.__path__)]
    modules += [importlib.import_module(f"backend.report.{m}") for m in ("contract", "factors", "exit_monitor")]
    unguarded = []
    for module in modules:
        for name, fn in inspect.getmembers(module, inspect.isfunction):
            original = inspect.unwrap(fn)
            if name.startswith("_") or original.__module__ != module.__name__:
                continue
            annotations = " ".join(str(a) for a in original.__annotations__.values())
            if ("Observation" in annotations or "DataSource" in annotations) and not getattr(fn, "__decision_path__", False):
                unguarded.append(f"{module.__name__}.{name}")
    assert unguarded == []


def test_no_decision_module_names_a_belief_read():
    """The guard checks what a decision function is handed; this checks none of them goes
    and gets belief data some other way."""
    names = ["belief_observations", "belief_snapshot", "belief_context", "stocktwits", "BELIEF_METRICS", "Metric.ST_"]
    names += [m.value for m in BELIEF_METRICS]
    modules = []
    for pkg in ("backend.gates", "backend.compute", "backend.setups"):
        package = importlib.import_module(pkg)
        modules += [f"{pkg}.{m.name}" for m in pkgutil.iter_modules(package.__path__)]
    modules += ["backend.report.factors", "backend.report.exit_monitor"]
    offenders = [
        f"{mod}: {n}" for mod in modules for n in names if n in inspect.getsource(importlib.import_module(mod))
    ]
    # contract.py carries the section 7 field it never fills (report/belief.py does); no reads.
    contract = inspect.getsource(importlib.import_module("backend.report.contract"))
    offenders += [f"backend.report.contract: {n}" for n in names if n != "belief_context" and n in contract]
    assert offenders == []


def test_guarded_source_has_no_belief_read():
    seen = {}

    @decision_path
    def a_gate(ds):
        seen["type"] = type(ds)
        return ds.belief_observations(INSTRUMENT)

    with db.get_connection() as conn, pytest.raises(AttributeError):
        a_gate(ReplaySource(conn, AS_OF))
    assert seen["type"] is GuardedSource


# --- 2. Removing every T4 observation leaves the DecisionRecord byte-identical ---------------


def _decision_record_bytes(conn, report) -> bytes:
    persist_report(conn, report)
    row = conn.execute(
        "SELECT run_id, run_at, gate_results, classification, setups_evaluated, verdict, config_hash "
        "FROM decision_records WHERE run_id = ?", (report.run_id,),
    ).fetchone()
    return json.dumps(tuple(row)).encode()


def _without_belief(report) -> dict:
    d = report.to_dict()
    d.pop("belief_context")
    return d


def test_decision_record_is_byte_identical_without_t4():
    fill_decision_store(CFG)
    store_fetch(stream_page(newest=AS_OF - timedelta(minutes=2), tags=TAGS, bodies=BODIES))
    registry = SourceRegistry.from_config(CFG)
    with db.get_connection() as conn:
        with_t4 = belief.build_report(INSTRUMENT, ReplaySource(conn, AS_OF), CFG, registry)
        record_with = _decision_record_bytes(conn, with_t4)
        conn.execute("DELETE FROM belief_observations")
        conn.execute("DELETE FROM belief_snapshots")
        without_t4 = belief.build_report(INSTRUMENT, ReplaySource(conn, AS_OF), CFG, registry)
        record_without = _decision_record_bytes(conn, without_t4)

    # The test is only worth something if the report went deep and the belief data was there.
    assert with_t4.verdict != "GATE_FAIL" and len(with_t4.setup_evaluation) == 4
    assert with_t4.belief_context["sources"][0]["status"] == "current"
    assert without_t4.belief_context["sources"][0]["status"] == "disabled"

    assert record_with == record_without
    assert json.dumps(_without_belief(with_t4)).encode() == json.dumps(_without_belief(without_t4)).encode()


def test_different_belief_data_changes_only_section_7():
    fill_decision_store(CFG)
    registry = SourceRegistry.from_config(CFG)
    reports = []
    for tags in (["Bullish"] * 25, ["Bearish"] * 25):
        with db.get_connection() as conn:
            conn.execute("DELETE FROM belief_observations")
            conn.execute("DELETE FROM belief_snapshots")
        store_fetch(stream_page(newest=AS_OF - timedelta(minutes=2), tags=tags))
        with db.get_connection() as conn:
            reports.append(belief.build_report(INSTRUMENT, ReplaySource(conn, AS_OF), CFG, registry))
    assert reports[0].belief_context != reports[1].belief_context
    assert _without_belief(reports[0]) == _without_belief(reports[1])


def test_a_broken_belief_config_leaves_the_decision_standing():
    """Belief data doesn't get to decide whether there is a report either."""
    fill_decision_store(CFG)
    store_fetch(stream_page(newest=AS_OF - timedelta(minutes=2), tags=TAGS))
    sources = json.loads(json.dumps(CFG.sources))
    sources["sources"] = [e for e in sources["sources"] if e["source_id"] != "stocktwits"]
    broken = dataclasses.replace(CFG, sources=sources)
    registry = SourceRegistry.from_config(CFG)
    with db.get_connection() as conn:
        good = belief.build_report(INSTRUMENT, ReplaySource(conn, AS_OF), CFG, registry)
        bad = belief.build_report(INSTRUMENT, ReplaySource(conn, AS_OF), broken, registry)
    assert bad.belief_context["error"].startswith("Belief context unavailable")
    assert bad.belief_context["sources"] == []
    assert _without_belief(good) == _without_belief(bad)


def test_a_broken_belief_config_does_not_stop_the_collector(monkeypatch):
    import asyncio

    def boom():
        raise ValueError("t4_min_tagged missing")

    monkeypatch.setattr(collector, "load_belief_config", boom)
    asyncio.run(collector.stocktwits_poll_loop(asyncio.Event()))  # returns, doesn't raise


def test_enabling_the_source_changes_no_decision():
    """The registry entry itself: switching Stocktwits on can't change what the decision
    path counts as a source (data integrity, independence)."""
    fill_decision_store(CFG)
    sources = json.loads(json.dumps(CFG.sources))
    next(e for e in sources["sources"] if e["source_id"] == "stocktwits")["enabled"] = True
    enabled_cfg = dataclasses.replace(CFG, sources=sources)
    with db.get_connection() as conn:
        off = run_analysis(INSTRUMENT, ReplaySource(conn, AS_OF), CFG, SourceRegistry.from_config(CFG))
        on = run_analysis(INSTRUMENT, ReplaySource(conn, AS_OF), enabled_cfg, SourceRegistry.from_config(enabled_cfg))
    assert off.to_dict() == on.to_dict()


def test_t4_source_never_counts_toward_independence():
    registry = SourceRegistry.from_config(CFG)
    assert registry.independent_upstream_count(["binance_futures", "stocktwits"]) == 1
    assert "stocktwits" not in {r.source_id for r in registry.enabled_sources()}


def test_belief_config_is_not_in_config_hash(tmp_path, monkeypatch):
    shutil.copytree(config_mod.CONFIG_DIR, tmp_path / "config")
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path / "config")
    try:
        before, belief_before = reload_config().config_hash, load_belief_config().config_hash
        path = tmp_path / "config" / "belief.yaml"
        path.write_text(path.read_text(encoding="utf-8").replace("t4_min_tagged: 20", "t4_min_tagged: 5"), encoding="utf-8")
        after, belief_after = reload_config().config_hash, load_belief_config()
        assert belief_after.min_tagged == 5
        assert after == before
        assert belief_after.config_hash != belief_before
    finally:
        monkeypatch.undo()
        reload_config()


# --- 3. Storage walls and Observation invariants -----------------------------------------------


def test_decision_table_refuses_t4_and_belief_table_refuses_the_rest():
    with db.get_connection() as conn:
        with pytest.raises(ValueError):
            db.insert_observation(conn, belief_obs())
        with pytest.raises(ValueError):
            db.insert_belief_fetch(conn, source_id="stocktwits", instrument=INSTRUMENT, collected_at=AS_OF,
                                   body=b"{}", observations=[t1_price()])
        assert conn.execute("SELECT COUNT(*) FROM belief_snapshots").fetchone()[0] == 0


def test_observation_tier_and_metric_must_agree():
    with pytest.raises(ValueError):  # a belief metric labelled T1 would slip past a tier check
        dataclasses.replace(belief_obs(), tier=Tier.T1)
    with pytest.raises(ValueError):  # T4 carrying a decision metric
        dataclasses.replace(t1_price(), tier=Tier.T4)
    with pytest.raises(ValueError):  # only T4 may be UNKNOWN
        dataclasses.replace(t1_price(), value=None)


# --- 4. Expired T4 is never returned and never rendered -----------------------------------------


def test_expired_t4_is_not_returned_or_rendered():
    expiry = timedelta(minutes=load_belief_config().expiry_minutes)
    # Newest message just over the half-life before AS_OF: born inside it, expired by AS_OF.
    store_fetch(stream_page(newest=AS_OF - expiry - timedelta(minutes=1), tags=TAGS), now=AS_OF - timedelta(minutes=10))
    with db.get_connection() as conn:
        ds = ReplaySource(conn, AS_OF)
        assert db.query_belief_observations(conn, instrument=INSTRUMENT, as_of=AS_OF) == []
        assert ds.belief_observations(INSTRUMENT) == []
        section = belief.belief_context(INSTRUMENT, ds, CFG)
        # Still inside the half-life a minute earlier, so the rows are really there.
        assert ReplaySource(conn, AS_OF - timedelta(minutes=5)).belief_observations(INSTRUMENT)
    entry = section["sources"][0]
    assert entry["metrics"] == [] and entry["observed_at"] is None


def test_a_fetch_collected_after_as_of_is_not_visible():
    """observed_at is the newest message, which can predate the fetch that saw it."""
    store_fetch(stream_page(newest=AS_OF - timedelta(minutes=5), tags=TAGS), now=AS_OF + timedelta(minutes=5))
    with db.get_connection() as conn:
        assert ReplaySource(conn, AS_OF).belief_observations(INSTRUMENT) == []
        assert ReplaySource(conn, AS_OF + timedelta(minutes=6)).belief_observations(INSTRUMENT)


def test_section_7_shows_only_the_latest_current_fetch_whole():
    store_fetch(stream_page(newest=AS_OF - timedelta(minutes=20), tags=["Bullish"] * 25), now=AS_OF - timedelta(minutes=15))
    store_fetch(stream_page(newest=AS_OF - timedelta(minutes=2), tags=["Bearish"] * 25), now=AS_OF)
    with db.get_connection() as conn:
        entry = belief.belief_context(INSTRUMENT, ReplaySource(conn, AS_OF), CFG)["sources"][0]
    values = {m["metric"]: m["value"] for m in entry["metrics"]}
    assert entry["collected_at"] == AS_OF.isoformat()
    assert values["st_bull_tagged"] == "0" and values["st_bear_tagged"] == "25"


# --- 5. A failed fetch writes a failure event and zero observations ------------------------------

CLOUDFLARE = b'<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title></head></html>'


def _raise_request_failed(url):
    raise stocktwits.StocktwitsFailure("request_failed", "connection reset")


@pytest.mark.parametrize(
    "fetch, reason",
    [
        (lambda url: (403, CLOUDFLARE), "http_403"),
        (lambda url: (429, b"rate limited"), "http_429"),
        (lambda url: (503, b""), "http_503"),
        (_raise_request_failed, "request_failed"),
        (lambda url: (200, CLOUDFLARE), "malformed_payload"),
        (lambda url: (200, b'{"response": {"status": 200}}'), "malformed_payload"),
        (lambda url: (200, stream_page(newest=AS_OF, tags=[])), "empty_stream"),
        (lambda url: (200, stream_page(newest=AS_OF, tags=TAGS).replace(b'"2026-', b'"not-a-date-', 1)), "malformed_payload"),
        (lambda url: (200, stream_page(newest=AS_OF, tags=["Bullish"], watchlist_count=None).replace(
            b'"title": "Solana"}}', b'"title": "Solana", "watchlist_count": "many"}}')), "malformed_payload"),
    ],
)
def test_failed_fetch_writes_an_event_and_no_observation(fetch, reason):
    stored, _ = collector.collect_stocktwits([INSTRUMENT], st_cfg(), fetch, now=AS_OF)
    with db.get_connection() as conn:
        events = conn.execute("SELECT source_id, event_type, detail FROM collector_events").fetchall()
        belief_rows = conn.execute("SELECT COUNT(*) FROM belief_observations").fetchone()[0]
        snapshots = conn.execute("SELECT COUNT(*) FROM belief_snapshots").fetchone()[0]
        decision_rows = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    assert stored == 0
    assert [(e[0], e[1]) for e in events] == [("stocktwits", "fetch_failed")]
    assert events[0][2].startswith(f"{INSTRUMENT}: {reason}")
    assert (belief_rows, snapshots, decision_rows) == (0, 0, 0)


def test_unmapped_instrument_fails_closed():
    with pytest.raises(stocktwits.StocktwitsFailure) as exc:
        stocktwits.collect("NOTACOIN", st_cfg(), lambda url: (200, b""), now=AS_OF)
    assert exc.value.reason == "no_symbol_mapping"


def test_a_refusal_stops_the_round_and_other_failures_do_not():
    calls = []

    def refuse(url):
        calls.append(url)
        return 429, b""

    stored, refused = collector.collect_stocktwits(["SOL", "BTC"], st_cfg(), refuse, now=AS_OF)
    assert (stored, refused.reason, len(calls)) == (0, "http_429", 1)

    calls.clear()

    def garbled_then_fine(url):
        calls.append(url)
        return (200, b"not json") if "SOL" in url else (200, stream_page(newest=AS_OF, tags=TAGS, symbol="BTC.X"))

    stored, refused = collector.collect_stocktwits(["SOL", "BTC"], st_cfg(), garbled_then_fine, now=AS_OF)
    assert (stored, refused, len(calls)) == (1, None, 2)


# --- 6. The metrics, and the same section 7 on re-run -----------------------------------------


def test_metrics_from_a_stream_page():
    newest = AS_OF - timedelta(minutes=2)
    result = stocktwits.collect(INSTRUMENT, st_cfg(), lambda url: (200, stream_page(newest=newest, tags=TAGS, bodies=BODIES)), now=AS_OF)
    values = {o.metric: o.value for o in result.observations}
    assert values == {
        Metric.ST_MSG_COUNT: Decimal(25),
        Metric.ST_WINDOW_MINUTES: Decimal(72),  # 24 gaps of 3 minutes
        Metric.ST_BULL_TAGGED: Decimal(15),
        Metric.ST_BEAR_TAGGED: Decimal(7),
        Metric.ST_BULL_SHARE: Decimal(15) / Decimal(22),
        Metric.ST_SPAM_SHARE: Decimal(2) / Decimal(25),  # the price-prediction link and the "100x gem"
        Metric.ST_CATALYST_MENTIONS: Decimal(2),  # alpenglow, Votor
        Metric.ST_WATCHERS: Decimal(12345),
    }
    assert all(o.tier == Tier.T4 and o.observed_at == newest for o in result.observations)
    assert all(o.expires_at == newest + timedelta(minutes=load_belief_config().expiry_minutes) for o in result.observations)


def test_thin_tagging_is_unknown_not_zero_and_no_keywords_is_no_reading():
    page = stream_page(newest=AS_OF, tags=["Bullish"] * 5 + [None] * 20, symbol="BTC.X", watchlist_count=None)
    store_fetch(page, instrument="BTC")
    with db.get_connection() as conn:
        rows = {o.metric: o for o in ReplaySource(conn, AS_OF).belief_observations("BTC")}
        entry = belief.belief_context("BTC", ReplaySource(conn, AS_OF), CFG)["sources"][0]
    assert rows[Metric.ST_BULL_SHARE].value is None
    assert Metric.ST_CATALYST_MENTIONS not in rows  # BTC has no catalyst keywords configured
    assert Metric.ST_WATCHERS not in rows           # absent from the payload, so no reading
    share = next(m for m in entry["metrics"] if m["metric"] == "st_bull_share")
    assert share["value"] is None and share["note"].startswith("UNKNOWN: 5 tagged")


def test_stored_body_recomputes_the_stored_metrics():
    result = store_fetch(stream_page(newest=AS_OF - timedelta(minutes=2), tags=TAGS, bodies=BODIES))
    with db.get_connection() as conn:
        snapshot_id = conn.execute("SELECT snapshot_id FROM belief_observations LIMIT 1").fetchone()[0]
        body = db.belief_snapshot_body(conn, snapshot_id)
        stored = ReplaySource(conn, AS_OF).belief_observations(INSTRUMENT)
    assert body == result.body
    recomputed = stocktwits.metrics_from_body(INSTRUMENT, "SOL.X", body, st_cfg(), collected_at=AS_OF)
    assert {(o.metric, o.value) for o in recomputed} == {(o.metric, o.value) for o in stored}


def test_section_7_is_identical_on_replay_rerun():
    fill_decision_store(CFG)
    store_fetch(stream_page(newest=AS_OF - timedelta(minutes=2), tags=TAGS, bodies=BODIES))
    registry = SourceRegistry.from_config(CFG)
    with db.get_connection() as conn:
        a = belief.build_report(INSTRUMENT, ReplaySource(conn, AS_OF), CFG, registry)
        b = belief.build_report(INSTRUMENT, ReplaySource(conn, AS_OF), CFG, registry)
    assert a.belief_context["sources"][0]["status"] == "current"
    assert json.dumps(a.belief_context).encode() == json.dumps(b.belief_context).encode()
    assert a.to_dict() == b.to_dict()


def test_section_7_stays_out_of_the_verdict_line():
    from backend.report.render import render_text

    fill_decision_store(CFG)
    store_fetch(stream_page(newest=AS_OF - timedelta(minutes=2), tags=TAGS, bodies=BODIES))
    with db.get_connection() as conn:
        report = belief.build_report(INSTRUMENT, ReplaySource(conn, AS_OF), CFG, SourceRegistry.from_config(CFG))
    text = render_text(report)
    verdict_line = text.splitlines()[0]
    assert "st_" not in verdict_line and "Belief" not in verdict_line and "stocktwits" not in verdict_line
    assert text.index("Belief context (T4, not evaluated)") > text.index("-- Distance to flip --")
    assert "Belief data. Not an input to any gate or verdict." in text
