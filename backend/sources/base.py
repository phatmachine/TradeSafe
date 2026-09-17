"""Shared plumbing for venue/chain source modules. Every source module exposes:

    async def fetch(instrument: str, cfg: Config, *, client: httpx.AsyncClient) -> list[Observation]

and nothing else is called by the collector. A source that cannot answer for an
instrument (unlisted symbol, HTTP error, malformed payload) raises SourceError — it never
returns a guessed or partial Observation. Fail closed (implementation spec, build
principles).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx


class SourceError(Exception):
    """Raised whenever a source cannot produce a trustworthy Observation. Caught only by
    the collector, which records a collector_event and a source-registry failure — never
    caught by compute/gates, which only ever see Observations that already exist."""


def to_decimal(value: Any, *, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SourceError(f"could not parse {field!r} as a decimal: {value!r}") from exc


def parse_ms_timestamp(ms: Any) -> datetime:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except (TypeError, ValueError) as exc:
        raise SourceError(f"could not parse millisecond timestamp: {ms!r}") from exc


@dataclass(frozen=True)
class VenueSymbol:
    """A venue's own symbol convention for a canonical instrument (e.g. 'ZEC').
    Doctrine 0.10 ("never mix venues within a single computation") is enforced by every
    fetch() staying entirely within one venue's own symbols and its own mark price —
    this dataclass exists so that boundary is explicit in every source module."""

    futures: str
    spot: str


def default_usdt_symbols(instrument: str) -> VenueSymbol:
    base = instrument.upper()
    return VenueSymbol(futures=f"{base}USDT", spot=f"{base}USDT")


async def get_json(client: httpx.AsyncClient, url: str, *, params: dict | None = None) -> Any:
    try:
        resp = await client.get(url, params=params, timeout=10.0)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        raise SourceError(f"GET {url} failed: {exc}") from exc


def sum_depth_within_band(levels: list[list[Any]], mid: Decimal, band_pct: Decimal) -> Decimal:
    """levels: [[price, qty], ...] as returned by any venue's order-book endpoint.
    Sums base-asset quantity across all levels within +/-band_pct of mid — the resting
    liquidity Gate U condition 1 checks intended size against. Same-venue mid price only;
    never called with a mid from a different venue than the levels."""
    lo = mid * (1 - band_pct)
    hi = mid * (1 + band_pct)
    total = Decimal(0)
    for level in levels:
        price = to_decimal(level[0], field="depth_price")
        qty = to_decimal(level[1], field="depth_qty")
        if lo <= price <= hi:
            total += qty
    return total


async def post_json(client: httpx.AsyncClient, url: str, *, json_body: dict) -> Any:
    try:
        resp = await client.post(url, json=json_body, timeout=10.0)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        raise SourceError(f"POST {url} failed: {exc}") from exc
