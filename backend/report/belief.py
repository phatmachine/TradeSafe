"""Report section 7, "Belief context (T4, not evaluated)", and the report builder that adds
it. The only place belief data meets a report.

build_report runs the decision (contract.run_analysis) first and to completion, then
attaches this section to the finished report. run_analysis never receives belief data: it
is handed a DataSource but sees it through core/firewall.GuardedSource, which has no
belief read. So nothing here can move the verdict, a gate, the classification, a setup,
config_hash or run_id; tests/test_belief_firewall.py checks that removing or changing
every belief observation leaves all of them byte-identical.

The section shows one fetch per source, whole (doctrine 0.10: never mix fields from
different fetches): the latest one collected by as_of whose metrics haven't expired.
Same instrument, same as_of, same store: same section, live or replay.
"""
from __future__ import annotations

import dataclasses

from backend.core.config import BeliefConfig, Config, load_belief_config
from backend.core.observation import Metric, Observation
from backend.core.registry import SourceRegistry
from backend.replay.source import DataSource
from backend.report.contract import AnalysisReport, run_analysis
from backend.sources import stocktwits

TITLE = "Belief context (T4, not evaluated)"
CAPTION = "Belief data. Not an input to any gate or verdict."

# Display order and wording. Factual descriptions of what was counted, nothing more.
_LABELS = {
    Metric.ST_MSG_COUNT: "Messages on the latest stream page",
    Metric.ST_WINDOW_MINUTES: "Minutes between the oldest and newest of them",
    Metric.ST_BULL_TAGGED: "Tagged Bullish by their authors",
    Metric.ST_BEAR_TAGGED: "Tagged Bearish by their authors",
    Metric.ST_BULL_SHARE: "Bullish share of tagged messages",
    Metric.ST_WATCHERS: "Watchlists holding the symbol",
    Metric.ST_CATALYST_MENTIONS: "Messages mentioning a listed catalyst",
    Metric.ST_SPAM_SHARE: "Share matching promotional patterns",
}


def _metric_row(o: Observation) -> dict:
    note = None
    if o.metric == Metric.ST_BULL_SHARE and o.value is None:
        note = f"UNKNOWN: {o.raw.get('tagged')} tagged, fewer than the {o.raw.get('min_tagged')} needed"
    elif o.metric == Metric.ST_CATALYST_MENTIONS:
        note = "keywords: " + ", ".join(o.raw.get("keywords", []))
    return {
        "metric": o.metric.value,
        "label": _LABELS[o.metric],
        "value": None if o.value is None else str(o.value),
        "unit": o.unit.value,
        "note": note,
    }


def _stocktwits_entry(instrument: str, current: list[Observation], st_cfg: stocktwits.StocktwitsConfig) -> dict:
    symbol = st_cfg.symbol_map.get(instrument)
    mine = [o for o in current if o.source_id == stocktwits.SOURCE_ID]
    entry: dict = {"source_id": stocktwits.SOURCE_ID, "symbol": symbol}
    if mine:
        latest = max(o.collected_at for o in mine)
        fetch = [o for o in mine if o.collected_at == latest]
        order = list(_LABELS)
        fetch.sort(key=lambda o: order.index(o.metric))
        return {
            **entry,
            "status": "current",
            "detail": None,
            "collected_at": latest.isoformat(),
            "observed_at": fetch[0].observed_at.isoformat(),
            "expires_at": fetch[0].expires_at.isoformat(),
            "metrics": [_metric_row(o) for o in fetch],
        }
    if symbol is None:
        status, detail = "not_mapped", f"No Stocktwits symbol is mapped for {instrument} (config/sources.yaml)."
    elif not st_cfg.enabled:
        status, detail = "disabled", "Stocktwits collection is off (config/sources.yaml, see the source's note)."
    else:
        status, detail = "none_current", (
            "No unexpired reading: the latest fetch failed, or the newest message on the stream is "
            f"older than the {st_cfg.expiry_minutes}-minute belief half-life."
        )
    return {**entry, "status": status, "detail": detail, "collected_at": None, "observed_at": None,
            "expires_at": None, "metrics": []}


def belief_context(instrument: str, ds: DataSource, cfg: Config, belief_cfg: BeliefConfig | None = None) -> dict:
    belief_cfg = belief_cfg or load_belief_config()
    current = ds.belief_observations(instrument)
    return {
        "title": TITLE,
        "caption": CAPTION,
        "config_hash": belief_cfg.config_hash,
        "sources": [_stocktwits_entry(instrument, current, stocktwits.StocktwitsConfig.load(cfg, belief_cfg))],
        "error": None,
    }


def build_report(
    instrument: str, ds: DataSource, cfg: Config, registry: SourceRegistry, belief_cfg: BeliefConfig | None = None
) -> AnalysisReport:
    """The decision, then section 7 attached to it. What the API and CLI show; the scanner
    and the DecisionRecord use the decision alone. Section 7 failing (a bad belief config,
    say) leaves the decision standing and says so in section 7: belief data doesn't get to
    decide whether there is a report either."""
    decision = run_analysis(instrument, ds, cfg, registry)
    try:
        context = belief_context(instrument, ds, cfg, belief_cfg)
    except Exception as exc:  # noqa: BLE001
        context = {"title": TITLE, "caption": CAPTION, "config_hash": None, "sources": [],
                   "error": f"Belief context unavailable: {type(exc).__name__}: {exc}"}
    return dataclasses.replace(decision, belief_context=context)
