"""Stocktwits collector. T4 belief data only (doctrine 0.1): evidence of what participants
say, never of what is true. It never reaches the decision path: its observations are
stored apart (store table belief_observations) and core/firewall.py raises if one is
handed to a gate, compute, setup or classifier. They appear only in report section 7.

Every metric is a count or a ratio over the one page of the symbol stream a fetch returns,
or a match against a config list (config/catalysts.yaml, config/t4_spam_patterns.yaml).
No model, no scoring, nothing that couldn't be recomputed from the stored response body,
which is kept exactly as received (store.insert_belief_fetch) so it can be.

Fails closed: an HTTP error, a rate limit, a payload that doesn't parse, or an empty
stream raises StocktwitsFailure, and the collector records the failure and stores
nothing. A message that can't be read discards the whole fetch (doctrine 0.10), and a
metric too thin to state is stored as UNKNOWN, never as a zero.

The endpoint is in config/sources.yaml. As of 2026-09-22 it could not be read from this
deployment (Cloudflare challenge), so the source is registered disabled; see its note.
The parser follows the documented v2 payload and has only been run against a fixture.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable

import httpx

from backend.core.config import BeliefConfig, Config
from backend.core.observation import Metric, Observation, Tier, Unit
from backend.sources.base import SourceError

SOURCE_ID = "stocktwits"
VENUE = "stocktwits"
USER_AGENT = "TradeSafe/1.0 (evidence collector)"

# (status, body). Injected so tests and replays can stub the network.
Fetcher = Callable[[str], tuple[int, bytes]]


class StocktwitsFailure(SourceError):
    """reason is one of no_symbol_mapping, request_failed, http_<status>,
    malformed_payload, empty_stream."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)

    @property
    def refused(self) -> bool:
        """The endpoint turned the request away (blocked, rate-limited, down), as opposed
        to answering with something unusable: the collector backs off rather than
        polling again on schedule."""
        return self.reason in ("http_403", "http_429") or self.reason.startswith("http_5")


@dataclass(frozen=True)
class StocktwitsConfig:
    enabled: bool
    stream_url: str                  # sources.yaml endpoints.stream, contains {symbol}
    symbol_map: dict[str, str]       # TradeSafe instrument -> Stocktwits symbol
    poll_minutes: int
    max_backoff_minutes: int
    expiry_minutes: int              # belief.yaml t4_expiry_minutes
    min_tagged: int                  # belief.yaml t4_min_tagged
    catalysts: dict[str, list[str]]  # catalysts.yaml
    spam_patterns: tuple[re.Pattern, ...]  # t4_spam_patterns.yaml, compiled so a bad one fails at load

    @classmethod
    def load(cls, cfg: Config, belief: BeliefConfig) -> "StocktwitsConfig":
        entry = next((e for e in cfg.sources.get("sources", []) if e["source_id"] == SOURCE_ID), None)
        if entry is None:
            raise KeyError(f"no {SOURCE_ID!r} entry in config/sources.yaml")
        return cls(
            enabled=bool(entry.get("enabled", True)),
            stream_url=str(entry["endpoints"]["stream"]),
            symbol_map={str(k).upper(): str(v) for k, v in (entry.get("symbol_map") or {}).items()},
            poll_minutes=int(entry["poll_minutes"]),
            max_backoff_minutes=int(entry["max_backoff_minutes"]),
            expiry_minutes=belief.expiry_minutes,
            min_tagged=belief.min_tagged,
            catalysts=belief.catalysts,
            spam_patterns=tuple(re.compile(p, re.IGNORECASE) for p in belief.spam_patterns),
        )


@dataclass(frozen=True)
class BeliefFetch:
    instrument: str
    symbol: str
    collected_at: datetime
    body: bytes                      # the response body exactly as received
    observations: list[Observation]


def http_fetcher(client: httpx.Client) -> Fetcher:
    def fetch(url: str) -> tuple[int, bytes]:
        try:
            resp = client.get(url, headers={"User-Agent": USER_AGENT}, timeout=20.0)
        except httpx.HTTPError as exc:
            raise StocktwitsFailure("request_failed", str(exc)) from exc
        return resp.status_code, resp.content

    return fetch


def collect(instrument: str, cfg: StocktwitsConfig, fetch: Fetcher, *, now: datetime) -> BeliefFetch:
    symbol = cfg.symbol_map.get(instrument)
    if symbol is None:
        raise StocktwitsFailure("no_symbol_mapping", instrument)
    status, body = fetch(cfg.stream_url.format(symbol=symbol))
    if status != 200:
        raise StocktwitsFailure(f"http_{status}")
    observations = metrics_from_body(instrument, symbol, body, cfg, collected_at=now)
    return BeliefFetch(instrument=instrument, symbol=symbol, collected_at=now, body=body, observations=observations)


def metrics_from_body(
    instrument: str, symbol: str, body: bytes, cfg: StocktwitsConfig, *, collected_at: datetime
) -> list[Observation]:
    """The metrics one stream page supports. Pure: the same body and config always give
    the same observations, which is how a stored snapshot is recomputed."""
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise StocktwitsFailure("malformed_payload", "not JSON") from exc
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(messages, list):
        raise StocktwitsFailure("malformed_payload", "no messages list")
    if not messages:
        raise StocktwitsFailure("empty_stream")

    times: list[datetime] = []
    bodies: list[str] = []
    tags: list[str | None] = []
    for m in messages:
        if not isinstance(m, dict) or not isinstance(m.get("body", ""), str):
            raise StocktwitsFailure("malformed_payload", "a message is not an object with a text body")
        try:
            times.append(_parse_ts(m["created_at"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise StocktwitsFailure("malformed_payload", f"created_at {m.get('created_at')!r}") from exc
        bodies.append(m.get("body", ""))
        tags.append(_tag(m))

    observed_at = max(times)  # newest message: when this page was last true
    expires_at = observed_at + timedelta(minutes=cfg.expiry_minutes)
    bull = sum(1 for t in tags if t == "Bullish")
    bear = sum(1 for t in tags if t == "Bearish")
    tagged = bull + bear
    lowered = [b.lower() for b in bodies]
    spam = sum(1 for b in bodies if any(p.search(b) for p in cfg.spam_patterns))

    metrics: list[tuple[Metric, Decimal | None, Unit, dict]] = [
        (Metric.ST_MSG_COUNT, Decimal(len(messages)), Unit.COUNT, {}),
        (Metric.ST_WINDOW_MINUTES, Decimal(int((observed_at - min(times)).total_seconds() // 60)), Unit.MINUTES, {}),
        (Metric.ST_BULL_TAGGED, Decimal(bull), Unit.COUNT, {}),
        (Metric.ST_BEAR_TAGGED, Decimal(bear), Unit.COUNT, {}),
        (
            Metric.ST_BULL_SHARE,
            Decimal(bull) / Decimal(tagged) if tagged and tagged >= cfg.min_tagged else None,
            Unit.RATIO,
            {"tagged": tagged, "min_tagged": cfg.min_tagged},
        ),
        (Metric.ST_SPAM_SHARE, Decimal(spam) / Decimal(len(bodies)), Unit.RATIO, {}),
    ]
    keywords = [k.lower() for k in cfg.catalysts.get(instrument, [])]
    if keywords:  # no keywords configured is no reading, not zero mentions
        mentions = sum(1 for b in lowered if any(k in b for k in keywords))
        metrics.append((Metric.ST_CATALYST_MENTIONS, Decimal(mentions), Unit.COUNT, {"keywords": keywords}))
    symbol_obj = payload.get("symbol")
    watchers = symbol_obj.get("watchlist_count") if isinstance(symbol_obj, dict) else None
    if watchers is not None:
        if not isinstance(watchers, int) or isinstance(watchers, bool):
            raise StocktwitsFailure("malformed_payload", f"watchlist_count {watchers!r}")
        metrics.append((Metric.ST_WATCHERS, Decimal(watchers), Unit.COUNT, {}))

    return [
        Observation(
            metric=metric,
            instrument=instrument,
            value=value,  # None is UNKNOWN, stored as NULL and never coalesced
            unit=unit,
            venue=VENUE,
            source_id=SOURCE_ID,
            tier=Tier.T4,
            collected_at=collected_at,
            observed_at=observed_at,
            expires_at=expires_at,
            raw={"symbol": symbol, **extra},
        )
        for metric, value, unit, extra in metrics
    ]


def _tag(m: dict) -> str | None:
    """The author's own Bullish/Bearish tag (documented v2: entities.sentiment.basic)."""
    entities = m.get("entities")
    sentiment = entities.get("sentiment") if isinstance(entities, dict) else None
    return sentiment.get("basic") if isinstance(sentiment, dict) else None


def _parse_ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
