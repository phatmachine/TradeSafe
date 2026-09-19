"""Scheduled scan: a setup alerts once when it starts qualifying, not on every scan while
it keeps qualifying, and a scan that couldn't check the setups never ends an episode.
"""
from __future__ import annotations

from types import SimpleNamespace

from backend.service import scanner
from backend.store import db


def report(verdict="NO_SETUP", passing=(), failing=("cascade_absorption",), bias=None):
    setups = [{"gate": s, "passed": True, "case": "short"} for s in passing]
    setups += [{"gate": s, "passed": False, "case": "long"} for s in failing]
    return SimpleNamespace(
        instrument="ZEC", verdict=verdict, verdict_bias=bias, setup_evaluation=setups,
        run_id="run", as_of="2026-09-19T06:00:00+00:00",
    )


def scan(r):
    with db.get_connection() as conn:
        return scanner.record_scan(conn, r)


def test_a_setup_alerts_once_per_episode():
    assert scan(report()) == []

    alerts = scan(report("ELIGIBLE_SETUP", passing=["squeeze_absorption"], bias="unclear"))
    assert [(a["instrument"], a["setup"], a["setup_case"], a["verdict_bias"]) for a in alerts] == [
        ("ZEC", "squeeze_absorption", "short", "unclear")
    ]
    # Still qualifying on the next scan: the same episode, no second alert.
    assert scan(report("ELIGIBLE_SETUP", passing=["squeeze_absorption"])) == []

    # Stops qualifying, then qualifies again: a new episode, a new alert.
    assert scan(report()) == []
    assert len(scan(report("ELIGIBLE_SETUP", passing=["squeeze_absorption"]))) == 1


def test_a_failed_trust_gate_does_not_end_an_episode():
    assert len(scan(report("ELIGIBLE_SETUP", passing=["squeeze_absorption"]))) == 1
    # Data briefly stale: the setups weren't checked at all, so nothing is known to have ended.
    assert scan(report("GATE_FAIL", failing=())) == []
    assert scan(report("ELIGIBLE_SETUP", passing=["squeeze_absorption"])) == []


def test_alerts_are_listed_newest_first_after_the_last_one_seen():
    scan(report("ELIGIBLE_SETUP", passing=["squeeze_absorption"]))
    with db.get_connection() as conn:
        seen = db.latest_alert_id(conn)
        db.insert_alert(conn, {"instrument": "TEST", "setup": "test_alert", "verdict": "TEST",
                               "as_of": "2026-09-19T06:05:00+00:00", "is_test": True})
        newer = db.list_alerts(conn, after_id=seen)
        assert [(a["setup"], a["is_test"]) for a in newer] == [("test_alert", True)]
        assert [a["setup"] for a in db.list_alerts(conn)] == ["test_alert", "squeeze_absorption"]
