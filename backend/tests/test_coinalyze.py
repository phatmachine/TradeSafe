"""Coinalyze liquidations: per-minute totals become signed rows stamped at the end of a
closed minute, and a poll that overlaps the last one (or sees a revised total) never
counts a minute twice.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backend.core.config import load_config
from backend.core.observation import Metric
from backend.service import collector
from backend.sources import coinalyze
from backend.sources.base import SourceError
from backend.store import db

NOW = datetime(2026, 9, 19, 12, 0, 30, tzinfo=timezone.utc)
T = int(datetime(2026, 9, 19, 11, 50, tzinfo=timezone.utc).timestamp())


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeCoinalyze:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def get(self, url, headers, params, timeout):
        self.calls.append(params)
        return _Response(self.payload)


def payload(zec_short=5493.54):
    return [
        {"symbol": "ZECUSDT_PERP.A", "history": [
            {"t": T, "l": 2091.21, "s": zec_short},           # closed: both sides stored
            {"t": T + 60, "l": 0, "s": 1536.44},              # closed: one side
            {"t": int(NOW.timestamp()) - 90, "l": 12.5, "s": 0},  # closed under 2 min ago: too recent
        ]},
        {"symbol": "ZECUSDT.6", "history": [{"t": T, "l": 0, "s": 5493.54}]},  # same value, other venue
    ]


def stored():
    with db.get_connection() as conn:
        return conn.execute(
            "SELECT venue, observed_at, value FROM observations WHERE metric = ? AND source_id = ? ORDER BY observed_at, venue, value",
            (Metric.LIQUIDATION.value, coinalyze.SOURCE_ID),
        ).fetchall()


def test_minutes_become_signed_rows_at_the_end_of_a_closed_minute():
    obs = coinalyze.to_observations(payload(), coinalyze.symbols_for(["ZEC"]), load_config(), now=NOW)
    first_minute_end = datetime.fromtimestamp(T + 60, timezone.utc) - timedelta(milliseconds=1)
    binance_first = sorted((o.value for o in obs if o.venue == "binance" and o.observed_at == first_minute_end))
    assert binance_first == [Decimal("-2091.21"), Decimal("5493.54")]  # longs negative, shorts positive
    assert len(obs) == 4  # the too-recent minute is left for a later poll
    assert all(o.observed_at <= NOW for o in obs)


def test_an_overlapping_poll_or_revised_total_never_counts_a_minute_twice(monkeypatch):
    monkeypatch.setenv(coinalyze.API_KEY_ENV, "test-key")
    cfg = load_config()
    n = asyncio.run(collector.collect_coinalyze_liquidations(["ZEC"], cfg, client=_FakeCoinalyze(payload()), now=NOW))
    assert n == 4
    # Binance and Bybit printed the same value in the same minute: both kept, one each.
    assert sum(1 for v, _, val in stored() if val == "5493.54") == 2

    again = asyncio.run(collector.collect_coinalyze_liquidations(
        ["ZEC"], cfg, client=_FakeCoinalyze(payload(zec_short=6000)), now=NOW + timedelta(minutes=1)))
    assert again == 0
    assert len(stored()) == 4


def test_no_api_key_is_an_error_not_an_empty_tape(monkeypatch):
    monkeypatch.delenv(coinalyze.API_KEY_ENV, raising=False)
    with pytest.raises(SourceError):
        asyncio.run(coinalyze.fetch_since(["ZEC"], load_config(), client=_FakeCoinalyze([]), since=NOW - timedelta(hours=1)))
