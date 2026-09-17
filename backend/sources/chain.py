"""Free-float sources (Gate U condition 5). The doctrine's ideal is a full node or
indexer per chain ("only a node states it truthfully" — Layer 1 source stack). Standing
that up for an arbitrary universe of chains is an infrastructure project of its own
(implementation spec, "Required, cost depends on chain") and is explicitly out of scope
for this build's first pass — see docs/doctrine-summary.md.

What is implemented:
  - a generic EVM `totalSupply()` JSON-RPC call, T1 (chain_rpc), for any instrument with
    a contract_address configured in config/instruments.yaml
  - a CoinGecko circulating-supply fallback, T2, for cross-checking

Gate U's free-float check requires either a configured EVM contract (T1) or, failing
that, two independent supply readings. Where neither exists, the metric is UNKNOWN and
Gate U fails closed — correct behaviour, not a bug (see config/universe.yaml).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import httpx

from backend.core.config import Config, CONFIG_DIR
from backend.core.observation import Metric, Observation, Tier, Unit
from backend.sources.base import SourceError, get_json, to_decimal

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
COINGECKO_SOURCE_ID = "coingecko"
CHAIN_SOURCE_ID = "chain_evm_generic"

_ERC20_TOTAL_SUPPLY_SELECTOR = "0x18160ddd"  # keccak256("totalSupply()")[:4]


def _instrument_meta(instrument: str) -> dict:
    import yaml

    path = CONFIG_DIR / "instruments.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return (data.get("instruments") or {}).get(instrument.upper(), {})


async def fetch_evm_supply(instrument: str, cfg: Config, *, client: httpx.AsyncClient) -> list[Observation]:
    meta = _instrument_meta(instrument)
    contract = meta.get("contract_address")
    rpc_url = meta.get("rpc_url")
    decimals = int(meta.get("decimals", 18))
    if not contract or not rpc_url:
        raise SourceError(f"no EVM contract/rpc configured for {instrument!r} in instruments.yaml")

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": contract, "data": _ERC20_TOTAL_SUPPLY_SELECTOR}, "latest"],
    }
    try:
        resp = await client.post(rpc_url, json=payload, timeout=10.0)
        resp.raise_for_status()
        body = resp.json()
    except httpx.HTTPError as exc:
        raise SourceError(f"EVM RPC call failed for {instrument}: {exc}") from exc

    result_hex = body.get("result")
    if not result_hex or not isinstance(result_hex, str):
        raise SourceError(f"EVM RPC totalSupply() returned no result for {instrument}: {body}")

    raw_supply = int(result_hex, 16)
    supply = Decimal(raw_supply) / (Decimal(10) ** decimals)
    now = datetime.now(timezone.utc)
    return [
        Observation.build(
            metric=Metric.SUPPLY_TOTAL,
            instrument=instrument,
            value=supply,
            unit=Unit.COINS,
            venue=meta.get("chain", "evm"),
            source_id=CHAIN_SOURCE_ID,
            tier=Tier.T1,
            observed_at=now,
            cfg=cfg,
            raw={"contract": contract, "raw_hex": result_hex},
        )
    ]


async def fetch_coingecko_supply(instrument: str, cfg: Config, *, client: httpx.AsyncClient) -> list[Observation]:
    coin_id = _instrument_meta(instrument).get("coingecko_id", instrument.lower())
    data = await get_json(
        client,
        f"{COINGECKO_BASE}/coins/{coin_id}",
        params={
            "localization": "false",
            "tickers": "false",
            "market_data": "true",
            "community_data": "false",
            "developer_data": "false",
        },
    )
    market_data = data.get("market_data")
    if not market_data or "circulating_supply" not in market_data:
        raise SourceError(f"coingecko missing market_data.circulating_supply for {coin_id}: keys={list(data.keys())}")

    now = datetime.now(timezone.utc)
    out = [
        Observation.build(
            metric=Metric.SUPPLY_CIRCULATING,
            instrument=instrument,
            value=to_decimal(market_data["circulating_supply"], field="circulating_supply"),
            unit=Unit.COINS,
            venue="coingecko",
            source_id=COINGECKO_SOURCE_ID,
            tier=Tier.T2,
            observed_at=now,
            cfg=cfg,
            raw={"circulating_supply": market_data["circulating_supply"]},
        )
    ]
    if market_data.get("total_supply") is not None:
        out.append(
            Observation.build(
                metric=Metric.SUPPLY_TOTAL,
                instrument=instrument,
                value=to_decimal(market_data["total_supply"], field="total_supply"),
                unit=Unit.COINS,
                venue="coingecko",
                source_id=COINGECKO_SOURCE_ID,
                tier=Tier.T2,
                observed_at=now,
                cfg=cfg,
                raw={"total_supply": market_data["total_supply"]},
            )
        )
    if market_data.get("market_cap", {}).get("usd") is not None:
        out.append(
            Observation.build(
                metric=Metric.MARKET_CAP,
                instrument=instrument,
                value=to_decimal(market_data["market_cap"]["usd"], field="market_cap.usd"),
                unit=Unit.USD,
                venue="coingecko",
                source_id=COINGECKO_SOURCE_ID,
                tier=Tier.T2,
                observed_at=now,
                cfg=cfg,
                raw={"market_cap_usd": market_data["market_cap"]["usd"]},
            )
        )
    return out


async def fetch(instrument: str, cfg: Config, *, client: httpx.AsyncClient) -> list[Observation]:
    """Best-effort aggregate: try the T1 chain path first, always also pull the T2
    cross-check where configured. Never merges the two into a single number — Layer 0's
    independence and tier-sufficiency checks decide what, if anything, is usable."""
    out: list[Observation] = []
    meta = _instrument_meta(instrument)
    if meta.get("contract_address"):
        try:
            out.extend(await fetch_evm_supply(instrument, cfg, client=client))
        except SourceError:
            pass
    try:
        out.extend(await fetch_coingecko_supply(instrument, cfg, client=client))
    except SourceError:
        pass
    if not out:
        raise SourceError(f"no free-float source available for {instrument!r}")
    return out
