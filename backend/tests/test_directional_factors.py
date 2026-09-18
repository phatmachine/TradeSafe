"""The "for or against a long" read on each piece of directional evidence. These are
presentation only, but a label pointing the wrong way is worse than no label, so each
lean is pinned to the market state that should produce it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from backend.compute.cohort import Cohort
from backend.compute.regime import Regime
from backend.core.config import load_config
from backend.core.observation import Metric, Observation, Tier, Unit
from backend.gates.common import ConditionResult, GateResult
from backend.report import factors
from backend.report.contract import _gate_dict, _setup_dicts
from backend.setups import cascade, continuation, exhaustion

AS_OF = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


def obs(cfg, metric, value, at, *, venue="binance", unit=Unit.COINS):
    return Observation.build(
        metric=metric, instrument="ZEC", value=Decimal(str(value)), unit=unit, venue=venue,
        source_id=f"{venue}_x", tier=Tier.T1, observed_at=at, cfg=cfg,
    )


def prices(cfg, start, end):
    return [obs(cfg, Metric.PRICE, start, AS_OF - timedelta(hours=24), unit=Unit.USD),
            obs(cfg, Metric.PRICE, end, AS_OF, unit=Unit.USD)]


def test_funding_lean():
    cfg = load_config()
    assert factors.funding_factor(Decimal("0.03"), cfg)["lean"] == factors.AGAINST
    assert factors.funding_factor(Decimal("-0.005"), cfg)["lean"] == factors.SUPPORTS
    # The venues' default balanced rate is not crowding.
    assert factors.funding_factor(Decimal("0.01"), cfg)["lean"] == factors.NEUTRAL
    assert factors.funding_factor(None, cfg)["lean"] == factors.UNKNOWN


def test_oi_price_quadrants():
    cfg = load_config()

    def lean(oi_start, oi_end, px_start, px_end):
        oi = [obs(cfg, Metric.OI_COIN, oi_start, AS_OF - timedelta(hours=24)), obs(cfg, Metric.OI_COIN, oi_end, AS_OF)]
        return factors.oi_price_factor(oi, prices(cfg, px_start, px_end), AS_OF, cfg)["lean"]

    assert lean(100, 110, 100, 105) == factors.SUPPORTS   # new longs into a rising price
    assert lean(100, 110, 100, 95) == factors.AGAINST     # new shorts into a falling price
    assert lean(100, 90, 100, 105) == factors.NEUTRAL     # short covering
    assert lean(100, 90, 100, 95) == factors.NEUTRAL      # longs exiting
    assert lean(100, 100.5, 100, 100.2) == factors.NEUTRAL  # no meaningful move


def test_oi_from_a_venue_that_only_just_appeared_is_not_counted_as_new_positions():
    cfg = load_config()
    oi = [
        obs(cfg, Metric.OI_COIN, 100, AS_OF - timedelta(hours=24)),
        obs(cfg, Metric.OI_COIN, 100, AS_OF),
        obs(cfg, Metric.OI_COIN, 500, AS_OF, venue="okx"),  # no reading at the window start
    ]
    result = factors.oi_price_factor(oi, prices(cfg, 100, 105), AS_OF, cfg)
    assert result["lean"] == factors.NEUTRAL
    assert result["value"].startswith("OI +0.0%")


def _volume_history(cfg, perp, spot, hours):
    hist_perp, hist_spot = [], []
    for h in range(1, hours + 1):
        at = AS_OF - timedelta(hours=h)
        hist_perp.append(obs(cfg, Metric.PERP_VOLUME, perp, at))
        hist_spot.append(obs(cfg, Metric.SPOT_VOLUME, spot, at))
    return hist_perp, hist_spot


def _perp_spot(cfg, perp_now, px_end, *, history_hours=72):
    hist_perp, hist_spot = _volume_history(cfg, 1000, 100, history_hours)  # usual mix: 10x
    return factors.perp_spot_factor(
        perp_now={"binance": Decimal(perp_now)}, spot_now={"binance": Decimal(100)},
        perp_history=hist_perp, spot_history=hist_spot, price_history=prices(cfg, 100, px_end),
        as_of=AS_OF, cfg=cfg,
    )


def test_perp_spot_is_read_together_with_the_price_move():
    cfg = load_config()
    assert _perp_spot(cfg, 1500, 103)["lean"] == factors.AGAINST   # perp-heavy rally: leverage-driven
    assert _perp_spot(cfg, 1500, 97)["lean"] == factors.SUPPORTS   # perp-heavy selloff: leverage flush
    assert _perp_spot(cfg, 600, 103)["lean"] == factors.SUPPORTS    # spot-heavy rally: real buying
    assert _perp_spot(cfg, 600, 97)["lean"] == factors.AGAINST      # spot-heavy selloff: real selling
    assert _perp_spot(cfg, 1050, 103)["lean"] == factors.NEUTRAL    # usual mix
    assert _perp_spot(cfg, 1500, 100.5)["lean"] == factors.NEUTRAL  # no real move


def test_perp_spot_without_a_baseline_is_unknown_not_a_guess():
    cfg = load_config()
    result = _perp_spot(cfg, 1500, 103, history_hours=10)
    assert result["lean"] == factors.UNKNOWN
    assert result["value"] == "15.0x"


def test_regime_and_cohort_leans():
    assert factors.regime_factor(Regime.TRENDING_UP)["lean"] == factors.SUPPORTS
    assert factors.regime_factor(Regime.TRENDING_DOWN)["lean"] == factors.AGAINST
    assert factors.regime_factor(Regime.MEAN_REVERTING)["lean"] == factors.NEUTRAL
    assert factors.regime_factor(Regime.UNDETERMINED)["lean"] == factors.UNKNOWN
    assert factors.cohort_factor(Cohort.TRAPPED_SHORTS)["lean"] == factors.SUPPORTS
    assert factors.cohort_factor(Cohort.TRAPPED_LONGS)["lean"] == factors.AGAINST
    assert factors.cohort_factor(Cohort.UNNAMED)["lean"] == factors.UNKNOWN


def test_gates_are_not_directional_and_setups_carry_their_side():
    g = GateResult(gate="gate_u", conditions=(ConditionResult("x", "pass", 1, 2, "d"),))
    assert _gate_dict(g)["case"] == "not_directional"
    cases = {
        d["gate"]: d["case"]
        for d in _setup_dicts({name: GateResult(gate=name, conditions=g.conditions) for name in (
            cascade.SETUP_NAME, cascade.SHORT_SETUP_NAME, continuation.SETUP_NAME,
            continuation.SHORT_SETUP_NAME, exhaustion.SETUP_NAME,
        )})
    }
    assert cases == {
        cascade.SETUP_NAME: "long",
        cascade.SHORT_SETUP_NAME: "short",
        continuation.SETUP_NAME: "long",
        continuation.SHORT_SETUP_NAME: "short",
        exhaustion.SETUP_NAME: "unclear",
    }
