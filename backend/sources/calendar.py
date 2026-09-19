"""Scheduled US macro events for the event-decompression setup: dated, primary-sourced,
market-wide (stored under instrument MACRO, one Observation per event, with the kind as
its venue).

- CPI, the jobs report (Employment Situation) and PCE (Personal Income and Outlays):
  release dates from FRED's release calendar (St. Louis Fed; it republishes the BLS and
  BEA schedules, and past dates are the actual ones, delays included). Needs
  FRED_API_KEY. FRED's own "FOMC Press Release" entry updates daily, so it can't give
  meeting dates.
- FOMC decisions: the Federal Reserve's meeting calendar page. The decision is the last
  day of a meeting ("Jan/Feb 31-1" decides on 1 February). Entries that aren't a regular
  meeting, such as a notation vote, are skipped: they have no 2 p.m. statement.

Both sources give dates only. The times are the agencies' fixed release times in New York
(RELEASE_TIMES), converted to UTC with daylight saving applied. Only events whose time
has passed are returned: an event is a fact once it has happened, and a rescheduled
future date can never be left behind in the store.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, time, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx

from backend.core.config import Config
from backend.core.observation import MACRO, Metric, Observation, Tier, Unit
from backend.sources.base import SourceError

FRED_API = "https://api.stlouisfed.org/fred"
FRED_KEY_ENV = "FRED_API_KEY"
FOMC_CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
NEW_YORK = ZoneInfo("America/New_York")

# kind -> FRED release id
FRED_RELEASES = {"us_cpi": 10, "us_jobs": 50, "us_pce": 54}
# The agencies' fixed release times, New York time: BLS and BEA at 8:30 a.m., the FOMC
# statement at 2:00 p.m. on the meeting's last day.
RELEASE_TIMES = {"us_cpi": time(8, 30), "us_jobs": time(8, 30), "us_pce": time(8, 30), "fomc": time(14, 0)}
SOURCE_IDS = {"us_cpi": "fred", "us_jobs": "fred", "us_pce": "fred", "fomc": "fed_fomc_calendar"}
EVENT_KINDS = tuple(RELEASE_TIMES)

_MONTHS = {m: i for i, m in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"), start=1)}
_MONTH_ABBR = {m[:3]: i for m, i in _MONTHS.items()}


def fred_key() -> str | None:
    return os.environ.get(FRED_KEY_ENV) or None


def event_time(kind: str, day: date) -> datetime:
    return datetime.combine(day, RELEASE_TIMES[kind], tzinfo=NEW_YORK).astimezone(timezone.utc)


def parse_fomc_calendar(html: str) -> list[date]:
    """Decision dates of every regular meeting on the Fed's calendar page, oldest first."""
    out: list[date] = []
    parts = re.split(r"<h4><a id=\"\d+\">(\d{4}) FOMC Meetings</a></h4>", html)
    for i in range(1, len(parts), 2):
        year, body = int(parts[i]), parts[i + 1]
        months = re.findall(r'fomc-meeting__month[^"]*"><strong>([^<]+)</strong>', body)
        days = re.findall(r'fomc-meeting__date[^"]*">([^<]+)<', body)
        for month_text, day_text in zip(months, days):
            m = re.fullmatch(r"(\d{1,2})(?:-(\d{1,2}))?\*?", day_text.strip())
            if not m:
                continue  # notation vote, unscheduled meeting: not a 2 p.m. decision
            last_day = int(m.group(2) or m.group(1))
            month_names = month_text.strip().lower().split("/")
            month = _MONTHS.get(month_names[-1]) or _MONTH_ABBR.get(month_names[-1][:3])
            if month is None:
                continue
            decision_year = year + 1 if len(month_names) == 2 and month == 1 else year
            out.append(date(decision_year, month, last_day))
    return sorted(set(out))


def fetch_fomc(client: httpx.Client) -> list[date]:
    try:
        resp = client.get(FOMC_CALENDAR_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=20.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise SourceError(f"GET FOMC calendar failed: {exc}") from exc
    dates = parse_fomc_calendar(resp.text)
    if not dates:
        raise SourceError("FOMC calendar page parsed to no meetings: its layout may have changed")
    return dates


def fetch_fred_release(client: httpx.Client, release_id: int, *, since: date) -> list[date]:
    key = fred_key()
    if not key:
        raise SourceError(f"{FRED_KEY_ENV} is not set")
    try:
        resp = client.get(
            f"{FRED_API}/release/dates",
            params={
                "api_key": key, "file_type": "json", "release_id": release_id,
                "realtime_start": since.isoformat(), "realtime_end": "9999-12-31",
                "include_release_dates_with_no_data": "true", "sort_order": "asc", "limit": 10000,
            },
            timeout=20.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise SourceError(f"GET FRED release {release_id} dates failed: {exc}") from exc
    rows = resp.json().get("release_dates")
    if not isinstance(rows, list):
        raise SourceError(f"FRED release {release_id} returned no release_dates")
    return [date.fromisoformat(r["date"]) for r in rows]


def to_observation(kind: str, at: datetime, cfg: Config) -> Observation:
    return Observation.build(
        metric=Metric.EVENT,
        instrument=MACRO,
        value=Decimal(1),
        unit=Unit.COUNT,
        venue=kind,
        source_id=SOURCE_IDS[kind],
        tier=Tier.T1,
        observed_at=at,
        cfg=cfg,
        raw={"kind": kind},
    )


def fetch_events(client: httpx.Client, *, since: date, now: datetime) -> tuple[list[tuple[str, datetime]], list[str]]:
    """(kind, time) of every event from `since` whose time has passed, oldest first, plus
    one error per source that failed — one source failing doesn't hide the others.
    Without FRED_API_KEY only FOMC decisions are returned; that's reported by the caller
    once, not as an error on every call."""
    found: list[tuple[str, datetime]] = []
    errors: list[str] = []
    for kind, release_id in FRED_RELEASES.items() if fred_key() else ():
        try:
            found += [(kind, event_time(kind, d)) for d in fetch_fred_release(client, release_id, since=since)]
        except SourceError as exc:
            errors.append(str(exc))
    try:
        found += [("fomc", event_time("fomc", d)) for d in fetch_fomc(client) if d >= since]
    except SourceError as exc:
        errors.append(str(exc))
    return sorted(((k, at) for k, at in set(found) if at <= now), key=lambda e: (e[1], e[0])), errors
