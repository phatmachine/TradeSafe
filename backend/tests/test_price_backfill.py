"""4h price backfill: it fills bars the collector missed (a missing bar shifts which closes
count as swing points), never stores a bar that hasn't closed, and makes no request when
nothing is missing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from backend.core.config import load_config
from backend.core.observation import Metric, Observation, Tier, Unit
from backend.scripts import backfill_history as bh
from backend.store import db

BAR_MS = bh.BAR_SECONDS * 1000


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeKlines:
    """Binance klines: the last LIMIT bars, the newest still forming, close = bar index."""

    def __init__(self):
        self.calls = 0

    def get(self, url, params, timeout):
        self.calls += 1
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        newest_open = now_ms // BAR_MS * BAR_MS
        rows = []
        for i in range(params["limit"]):
            open_ms = newest_open - (params["limit"] - 1 - i) * BAR_MS
            rows.append([open_ms, "0", "0", "0", str(open_ms // BAR_MS), "0", open_ms + BAR_MS - 1])
        return _FakeResponse(rows)


def live_tick(cfg, at):
    return Observation.build(
        metric=Metric.PRICE, instrument="ZEC", value=Decimal("1500"), unit=Unit.USD, venue="binance",
        source_id="binance_futures", tier=Tier.T1, observed_at=at, cfg=cfg,
    )


def stored_bars():
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT observed_at FROM observations WHERE metric = 'price' AND source_id = ?", (bh.SOURCE_ID,)
        ).fetchall()
    return {bh._bar(datetime.fromisoformat(r[0])) for r in rows}


def test_first_run_fills_the_window_but_never_the_forming_bar():
    cfg = load_config()
    client = _FakeKlines()
    now = datetime.now(timezone.utc)
    assert bh.backfill("ZEC", cfg, client=client) > 0
    bars = stored_bars()
    assert bh._bar(now) not in bars  # still forming: never stored
    assert bh._bar(now) - 1 in bars
    # Complete now: a second call makes no request.
    assert bh.backfill("ZEC", cfg, client=client) == 0
    assert client.calls == 1


def test_a_gap_left_by_a_stopped_collector_is_filled_and_nothing_else():
    cfg = load_config()
    client = _FakeKlines()
    bh.backfill("ZEC", cfg, client=client)
    now = datetime.now(timezone.utc)
    current = bh._bar(now)
    gap = set(range(current - 8, current - 2))  # six bars, like the ZEC gap
    with db.get_connection() as conn:
        conn.execute(
            "DELETE FROM observations WHERE source_id = ? AND observed_at >= ?",
            (bh.SOURCE_ID, datetime.fromtimestamp((current - 8) * bh.BAR_SECONDS, timezone.utc).isoformat()),
        )
        # The collector covered the last two closed bars live.
        for b in (current - 2, current - 1):
            db.insert_observation(conn, live_tick(cfg, datetime.fromtimestamp(b * bh.BAR_SECONDS + 600, timezone.utc)))

    assert bh.backfill("ZEC", cfg, client=client) == len(gap)
    assert gap <= stored_bars()
    assert not {current - 2, current - 1, current} & stored_bars()  # live bars left alone


def test_a_forming_bar_stored_by_an_older_version_is_removed():
    cfg = load_config()
    now = datetime.now(timezone.utc)
    closes_later = datetime.fromtimestamp((bh._bar(now) + 1) * bh.BAR_SECONDS, timezone.utc) - timedelta(milliseconds=1)
    stale = Observation.build(
        metric=Metric.PRICE, instrument="ZEC", value=Decimal("1476.40"), unit=Unit.USD, venue="binance",
        source_id=bh.SOURCE_ID, tier=Tier.T1, observed_at=closes_later, cfg=cfg, collected_at=now,
    )
    with db.get_connection() as conn:
        db.insert_observation(conn, stale)
    bh.backfill("ZEC", cfg, client=_FakeKlines())
    with db.get_connection() as conn:
        left = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE source_id = ? AND observed_at > collected_at", (bh.SOURCE_ID,)
        ).fetchone()[0]
    assert left == 0
