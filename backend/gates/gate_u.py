"""Gate U — Universe eligibility (doctrine, "Gate U — Universe"). Evaluated identically
for every instrument, whether it came from a screen or was typed into the app by a
person — "the bypass is the failure mode". This module is called before Layer 0 and
before any observation is used for a decision (implementation spec, gate/layer logic
pseudocode): a run that fails here never even reaches the data-integrity checks.
"""
from __future__ import annotations

from decimal import Decimal

from backend.compute.oi import aggregate_oi_coin
from backend.compute.ratios import oi_to_market_cap, perp_to_spot_volume
from backend.compute.volatility import daily_closes, realised_volatility_annualised
from backend.core.config import Config
from backend.core.observation import Metric
from backend.gates.common import ConditionResult, GateResult
from backend.replay.source import DataSource


def _sum_metric(ds: DataSource, instrument: str, metric: Metric) -> Decimal | None:
    obs = ds.observations(instrument, metric=metric)
    if not obs:
        return None
    from backend.compute.oi import latest_per_venue

    per_venue = latest_per_venue(obs)
    return sum((o.value for o in per_venue.values()), Decimal(0))


def evaluate(instrument: str, ds: DataSource, cfg: Config) -> GateResult:
    universe_cfg = cfg.universe
    conditions: list[ConditionResult] = []

    # --- Condition 1: spot depth sufficient -------------------------------------
    depth_total = _sum_metric(ds, instrument, Metric.ORDER_BOOK_DEPTH)
    intended_size = Decimal(str(cfg.get("gate_u", "reference_intended_size_coins", default=1.0)))
    multiple = Decimal(str(cfg.get("gate_u", "min_spot_depth_multiple", default=20)))
    required_depth = intended_size * multiple
    if depth_total is None:
        conditions.append(
            ConditionResult(
                name="spot_depth_sufficient",
                status="unknown",
                computed_value=None,
                threshold=required_depth,
                detail="no order-book depth observation available for this instrument",
            )
        )
    else:
        conditions.append(
            ConditionResult(
                name="spot_depth_sufficient",
                status="pass" if depth_total >= required_depth else "fail",
                computed_value=depth_total,
                threshold=required_depth,
                detail=f"resting depth within band across venues vs {multiple}x intended size",
            )
        )

    # --- Condition 2: perp/spot volume ceiling ----------------------------------
    perp_vol = _sum_metric(ds, instrument, Metric.PERP_VOLUME)
    spot_vol = _sum_metric(ds, instrument, Metric.SPOT_VOLUME)
    ratio = perp_to_spot_volume(perp_vol, spot_vol)
    ceiling = Decimal(str(cfg.get("gate_u", "perp_spot_ratio_ceiling", default=3.0)))
    if ratio is None:
        conditions.append(
            ConditionResult(
                name="perp_spot_ratio_below_ceiling",
                status="unknown",
                computed_value=None,
                threshold=ceiling,
                detail="insufficient perp or spot volume observations",
            )
        )
    else:
        conditions.append(
            ConditionResult(
                name="perp_spot_ratio_below_ceiling",
                status="pass" if ratio <= ceiling else "fail",
                computed_value=ratio,
                threshold=ceiling,
                detail="price discovery must not sit entirely in derivatives",
            )
        )

    # --- Condition 3: OI / market cap ceiling -----------------------------------
    oi_agg = aggregate_oi_coin(ds.observations(instrument, metric=Metric.OI_COIN))
    price_obs = ds.observations(instrument, metric=Metric.PRICE)
    price = price_obs[-1].value if price_obs else None
    mcap_obs = ds.observations(instrument, metric=Metric.MARKET_CAP)
    market_cap = mcap_obs[-1].value if mcap_obs else None
    oi_mcap = oi_to_market_cap(oi_agg.total_coins if oi_agg.venue_count else None, price, market_cap)
    oi_ceiling = Decimal(str(cfg.get("gate_u", "oi_to_mcap_ceiling", default=0.15)))
    if oi_mcap is None:
        conditions.append(
            ConditionResult(
                name="oi_to_mcap_below_ceiling",
                status="unknown",
                computed_value=None,
                threshold=oi_ceiling,
                detail="missing coin-OI, price, or market cap",
            )
        )
    else:
        conditions.append(
            ConditionResult(
                name="oi_to_mcap_below_ceiling",
                status="pass" if oi_mcap <= oi_ceiling else "fail",
                computed_value=oi_mcap,
                threshold=oi_ceiling,
                detail="coin-denominated OI as a share of market capitalisation",
            )
        )

    # --- Condition 4: T1 connectivity -------------------------------------------
    venues_reporting = {o.venue for o in ds.observations(instrument)}
    min_venues = int(universe_cfg.get("min_venues_reporting", 2))
    has_chain = bool(ds.observations(instrument, metric=Metric.SUPPLY_TOTAL)) or bool(
        ds.observations(instrument, metric=Metric.SUPPLY_CIRCULATING)
    )
    conditions.append(
        ConditionResult(
            name="t1_connectivity",
            status="pass" if len(venues_reporting) >= min_venues and has_chain else "fail",
            computed_value=f"{len(venues_reporting)} venues, chain={has_chain}",
            threshold=f">= {min_venues} venues and a chain source",
            detail="a venue API and a chain source that can be queried directly",
        )
    )

    # --- Condition 5: free float computable -------------------------------------
    t1_supply = ds.observations(instrument, metric=Metric.SUPPLY_TOTAL)
    t2_supply = ds.observations(instrument, metric=Metric.SUPPLY_CIRCULATING)
    if t1_supply:
        conditions.append(
            ConditionResult(
                name="free_float_computable",
                status="pass",
                computed_value=t1_supply[-1].value,
                threshold="T1 chain source present",
                detail="on-chain total supply queried directly",
            )
        )
    elif len(t2_supply) >= 2:
        # Two T2 supply readings from independent upstreams satisfies 0.1's
        # cross-confirmation requirement for a derived tier.
        conditions.append(
            ConditionResult(
                name="free_float_computable",
                status="pass",
                computed_value=t2_supply[-1].value,
                threshold=">= 2 independent T2 readings",
                detail="cross-confirmed aggregator supply figures",
            )
        )
    else:
        conditions.append(
            ConditionResult(
                name="free_float_computable",
                status="unknown",
                computed_value=None,
                threshold="T1 chain source, or >=2 independent T2 readings",
                detail="no chain contract configured and fewer than 2 independent supply sources",
            )
        )

    # --- Condition 6: realised volatility band ----------------------------------
    rv_min = Decimal(str(cfg.get("gate_u", "rv_band_min", default=0.20)))
    rv_max = Decimal(str(cfg.get("gate_u", "rv_band_max", default=3.0)))
    rv_long_days = int(cfg.get("rv_long_days", default=30))
    # A historical series is not the same query as "what is true right now": each past
    # print was a real observation at its own observed_at, so this deliberately uses the
    # non-expiry-filtered primitive rather than ds.observations() (see
    # replay/source.py's observations_including_expired docstring).
    price_history = ds.observations_including_expired(
        instrument, lookback_seconds=rv_long_days * 86400, metrics=(Metric.PRICE,)
    )
    by_venue: dict[str, list] = {}
    for o in price_history:
        by_venue.setdefault(o.venue, []).append(o)
    best_series = max(by_venue.values(), key=len, default=[])
    closes = daily_closes(best_series)
    rv = realised_volatility_annualised(closes)
    if rv is None:
        conditions.append(
            ConditionResult(
                name="realised_vol_in_band",
                status="unknown",
                computed_value=None,
                threshold=f"[{rv_min}, {rv_max}]",
                detail="insufficient daily price history for a realised-volatility estimate",
            )
        )
    else:
        rv_dec = Decimal(str(rv))
        conditions.append(
            ConditionResult(
                name="realised_vol_in_band",
                status="pass" if rv_min <= rv_dec <= rv_max else "fail",
                computed_value=rv_dec,
                threshold=f"[{rv_min}, {rv_max}]",
                detail="annualised realised volatility must leave an ATR-derived stop worth taking",
            )
        )

    return GateResult(gate="gate_u", conditions=tuple(conditions))
