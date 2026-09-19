"""The macro event calendar and the event-decompression setup built on it: events land at
their real release time in UTC (daylight saving included), only once they've happened,
and the setup reads positioning going INTO the most recent event, which must be recent.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from backend.core.config import load_config
from backend.core.observation import MACRO, Metric, Observation, Tier, Unit
from backend.core.registry import SourceRegistry
from backend.report.contract import _data_integrity_summary
from backend.service import collector
from backend.setups import event
from backend.sources import calendar
from backend.store import db

FOMC_PAGE = """
<h4><a id="1">2025 FOMC Meetings</a></h4>
<div class="fomc-meeting__month col-xs-5"><strong>July</strong></div>
<div class="fomc-meeting__date col-xs-4">29-30</div>
<div class="fomc-meeting__month col-xs-5"><strong>August</strong></div>
<div class="fomc-meeting__date col-xs-4">22 (notation vote)</div>
<div class="fomc-meeting--shaded fomc-meeting__month col-xs-5"><strong>September</strong></div>
<div class="fomc-meeting__date col-xs-4">16-17*</div>
<h4><a id="2">2024 FOMC Meetings</a></h4>
<div class="fomc-meeting__month col-xs-5"><strong>Apr/May</strong></div>
<div class="fomc-meeting__date col-xs-4">30-1</div>
<h4><a id="3">2023 FOMC Meetings</a></h4>
<div class="fomc-meeting__month col-xs-5"><strong>Jan/Feb</strong></div>
<div class="fomc-meeting__date col-xs-4">31-1</div>
"""


def test_fomc_decisions_fall_on_a_meetings_last_day_and_a_notation_vote_is_skipped():
    assert calendar.parse_fomc_calendar(FOMC_PAGE) == [
        date(2023, 2, 1), date(2024, 5, 1), date(2025, 7, 30), date(2025, 9, 17),
    ]


def test_release_times_are_new_york_times_with_daylight_saving():
    utc = lambda *a: datetime(*a, tzinfo=timezone.utc)  # noqa: E731
    assert calendar.event_time("fomc", date(2026, 1, 28)) == utc(2026, 1, 28, 19, 0)   # 2 p.m. EST
    assert calendar.event_time("fomc", date(2026, 7, 29)) == utc(2026, 7, 29, 18, 0)   # 2 p.m. EDT
    assert calendar.event_time("us_cpi", date(2026, 9, 11)) == utc(2026, 9, 11, 12, 30)
    assert calendar.event_time("us_cpi", date(2026, 12, 10)) == utc(2026, 12, 10, 13, 30)


class _Resp:
    def __init__(self, *, json=None, text=""):
        self._json, self.text = json, text

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


class _FakeSources:
    def get(self, url, params=None, headers=None, timeout=None):
        if url == calendar.FOMC_CALENDAR_URL:
            return _Resp(text=FOMC_PAGE)
        dates = {10: ["2025-09-11", "2025-10-15"], 50: ["2025-09-05"], 54: ["2025-09-26"]}[params["release_id"]]
        return _Resp(json={"release_dates": [{"date": d} for d in dates]})


NOW = datetime(2025, 9, 20, tzinfo=timezone.utc)


def test_only_events_that_have_happened_are_returned_oldest_first(monkeypatch):
    monkeypatch.setenv(calendar.FRED_KEY_ENV, "test-key")
    found, errors = calendar.fetch_events(_FakeSources(), since=date(2025, 9, 1), now=NOW)
    assert errors == []
    assert [k for k, _ in found] == ["us_jobs", "us_cpi", "fomc"]  # CPI of 15 Oct not yet
    assert [at for _, at in found] == sorted(at for _, at in found)

    monkeypatch.delenv(calendar.FRED_KEY_ENV)
    found, errors = calendar.fetch_events(_FakeSources(), since=date(2025, 9, 1), now=NOW)
    assert [k for k, _ in found] == ["fomc"] and errors == []


def test_each_event_is_stored_once(monkeypatch):
    monkeypatch.setenv(calendar.FRED_KEY_ENV, "test-key")
    cfg = load_config()
    # 60 days back from 20 Sep: jobs, CPI and the 30 Jul and 17 Sep FOMC decisions.
    assert collector.collect_calendar_events(cfg, client=_FakeSources(), now=NOW)[0] == 4
    assert collector.collect_calendar_events(cfg, client=_FakeSources(), now=NOW)[0] == 0
    with db.get_connection() as conn:
        rows = conn.execute("SELECT instrument, venue FROM observations WHERE metric = 'event'").fetchall()
    assert sorted(v for _, v in rows) == ["fomc", "fomc", "us_cpi", "us_jobs"] and {i for i, _ in rows} == {MACRO}


class _RescheduledCpi(_FakeSources):
    def get(self, url, params=None, headers=None, timeout=None):
        if params and params.get("release_id") == 10:
            return _Resp(json={"release_dates": [{"date": "2025-09-11"}, {"date": "2025-10-24"}]})
        return super().get(url, params=params, headers=headers, timeout=timeout)


def test_upcoming_releases_are_kept_apart_from_evidence_and_follow_a_reschedule(monkeypatch):
    monkeypatch.setenv(calendar.FRED_KEY_ENV, "test-key")
    cfg = load_config()
    collector.collect_calendar_events(cfg, client=_FakeSources(), now=NOW)
    with db.get_connection() as conn:
        upcoming = db.upcoming_events(conn, now=NOW)
        stored = conn.execute("SELECT COUNT(*) FROM observations WHERE observed_at > ?", (NOW.isoformat(),)).fetchone()[0]
    assert [(e["kind"], e["at"]) for e in upcoming] == [
        ("us_pce", "2025-09-26T12:30:00+00:00"), ("us_cpi", "2025-10-15T12:30:00+00:00"),
    ]
    assert stored == 0  # a date that hasn't happened is never evidence

    collector.collect_calendar_events(cfg, client=_RescheduledCpi(), now=NOW)
    with db.get_connection() as conn:
        cpi = [e["at"] for e in db.upcoming_events(conn, now=NOW) if e["kind"] == "us_cpi"]
    assert cpi == ["2025-10-24T12:30:00+00:00"]

    monkeypatch.delenv(calendar.FRED_KEY_ENV)  # CPI unreadable: its last schedule stands
    collector.collect_calendar_events(cfg, client=_FakeSources(), now=NOW)
    with db.get_connection() as conn:
        assert [e["kind"] for e in db.upcoming_events(conn, now=NOW)] == ["us_pce", "us_cpi"]


# --- the setup ---------------------------------------------------------------------------

AS_OF = datetime(2025, 9, 11, 15, 30, tzinfo=timezone.utc)
CPI_AT = datetime(2025, 9, 11, 12, 30, tzinfo=timezone.utc)


def ev(at, kind="us_cpi"):
    return calendar.to_observation(kind, at, load_config())


def funding(value, at, venue="binance"):
    return Observation.build(
        metric=Metric.FUNDING_8H, instrument="ZEC", value=Decimal(str(value)), unit=Unit.PCT_8H, venue=venue,
        source_id=f"{venue}_futures", tier=Tier.T1, observed_at=at, cfg=load_config(),
    )


def crowded_long_into_cpi_then_flipped():
    before = [funding(0.03, CPI_AT - timedelta(minutes=15 * k + 5)) for k in range(1, 5)]
    after = [funding(-0.05, CPI_AT + timedelta(minutes=15 * k)) for k in range(1, 8)]
    return before + after


def test_positioning_is_read_going_into_a_recent_event():
    result = event.evaluate("ZEC", event_history=[ev(CPI_AT)], funding_history=crowded_long_into_cpi_then_flipped(),
                            as_of=AS_OF, cfg=load_config())
    assert result.passed, result.conditions
    assert result.conditions[0].computed_value == "US CPI, 3.0h ago"


def test_an_old_event_or_mixed_positioning_does_not_qualify():
    cfg = load_config()
    old = event.evaluate("ZEC", event_history=[ev(CPI_AT)], funding_history=crowded_long_into_cpi_then_flipped(),
                         as_of=CPI_AT + timedelta(hours=30), cfg=cfg)
    assert old.conditions[0].status == "fail"

    mixed = [funding(v, CPI_AT - timedelta(minutes=15 * k + 5)) for k, v in enumerate((0.03, -0.03, 0.03, 0.03), 1)]
    result = event.evaluate("ZEC", event_history=[ev(CPI_AT)], funding_history=mixed, as_of=AS_OF, cfg=cfg)
    assert result.conditions[1].status == "fail"


def test_calendar_sources_are_not_listed_as_rejected_for_a_coin():
    summary = _data_integrity_summary("BTC", None, SourceRegistry.from_config(load_config()), clean=[])
    rejected = {r["source_id"] for r in summary["sources_rejected"]}
    assert "binance_futures" in rejected  # a price source with no current reading still is
    assert not rejected & {"fred", "fed_fomc_calendar"}


def test_no_event_is_unknown_not_a_pass():
    result = event.evaluate("ZEC", event_history=[], funding_history=crowded_long_into_cpi_then_flipped(),
                            as_of=AS_OF, cfg=load_config())
    assert [c.status for c in result.conditions] == ["unknown", "unknown"]
