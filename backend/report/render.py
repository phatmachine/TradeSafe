"""Terminal rendering of an AnalysisReport. JSON is canonical (implementation spec,
"Formats") — this is a view of it and must never contain anything the JSON lacks.
"""
from __future__ import annotations

from backend.report.contract import AnalysisReport


_CASE_LABEL = {
    "long": "long-type setup",
    "short": "short-type setup",
    "unclear": "direction unclear",
    "not_directional": "not directional — can this report be trusted?",
}


def _render_gate(gate: dict) -> str:
    case = gate.get("case")
    met = sum(1 for c in gate["conditions"] if c["status"] == "pass")
    header = f"  [{gate['gate']}] {'PASS' if gate['passed'] else 'FAIL'}  {met}/{len(gate['conditions'])} checks met"
    lines = [header + (f"  ({_CASE_LABEL[case]})" if case else "")]
    for c in gate["conditions"]:
        marker = {"pass": "OK ", "fail": "FAIL", "unknown": "?  "}[c["status"]]
        lines.append(f"    {marker} {c['name']}: {c['computed_value']} (threshold {c['threshold']}) — {c['detail']}")
    return "\n".join(lines)


def _pct(x) -> str:
    return "unknown" if x is None else f"{float(x) * 100:.1f}%"


_BIAS_LABEL = {"long": "LONG", "short": "SHORT", "unclear": "NO CLEAR DIRECTION"}
_LEAN_LABEL = {"supports_long": "+ supports long", "against_long": "- against long", "neutral": "  neutral", "unknown": "? unknown"}


def render_text(report: AnalysisReport) -> str:
    d = report.to_dict()
    bias = d["verdict_bias"]
    verdict_line = f"=== {d['instrument']} — {d['verdict']}"
    if bias:
        verdict_line += f" ({_BIAS_LABEL[bias]})"
    verdict_line += " ==="
    lines = [
        verdict_line,
        f"as_of: {d['as_of']}   config_hash: {d['config_hash']}"
        + ("   [UNVALIDATED THRESHOLDS]" if not d["config_validated"] else ""),
        "",
        "-- Gate status --",
    ]
    for g in d["gate_status"]:
        lines.append(_render_gate(g))

    if d["data_integrity"]:
        di = d["data_integrity"]
        lines += [
            "",
            "-- Data integrity --",
            f"  sources used: {', '.join(di['sources_used']) or '(none)'}",
            f"  sources rejected: {di['sources_rejected']}",
            f"  independent upstream count by metric: {di['independent_upstream_count_by_metric']}",
            f"  venue dispersion observed: {di['venue_dispersion_observed']}",
        ]

    if d["state_classification"]:
        sc = d["state_classification"]
        lines += [
            "",
            "-- State classification --",
            f"  regime: {sc.get('regime')}",
            f"  trapped cohort: {sc.get('trapped_cohort')}",
            f"  constraint ratios: {sc.get('constraint_ratios')}",
        ]

    if d["directional_factors"]:
        lines += ["", "-- Directional factors (context for a long, not a verdict) --"]
        for f in d["directional_factors"]:
            lines.append(f"  {_LEAN_LABEL[f['lean']]:<16} {f['factor']}: {f['value'] or '—'} — {f['reason']}")

    if d["setup_evaluation"]:
        lines += ["", "-- Setup evaluation --"]
        for setup in d["setup_evaluation"]:
            lines.append(_render_gate(setup))

    if d["structural_reads"]:
        lines += ["", "-- Structural read (evidence, not a recommendation) --"]
        for item in d["structural_reads"]:
            lines.append(f"  [{item['setup']}] ({item['direction']}) {item['read']}")

    if d["distance_to_flip"]:
        lines += ["", "-- Distance to flip --"]
        for item in d["distance_to_flip"]:
            lines.append(
                f"  [{item['gate']}] {item['condition']}: currently {item['current_value']}, "
                f"needs {item['required']} — {item['detail']}"
            )

    if d["belief_context"]:
        bc = d["belief_context"]
        lines += ["", f"-- {bc['title']} --", f"  {bc['caption']}"]
        if bc.get("error"):
            lines.append(f"  {bc['error']}")
        for src in bc["sources"]:
            head = f"  [{src['source_id']}{' ' + src['symbol'] if src['symbol'] else ''}] {src['status']}"
            if src["status"] == "current":
                head += f"  observed {src['observed_at']}, expires {src['expires_at']}"
            lines.append(head)
            if src["detail"]:
                lines.append(f"    {src['detail']}")
            for m in src["metrics"]:
                value = "UNKNOWN" if m["value"] is None else f"{m['value']} {m['unit']}"
                lines.append(f"    {m['metric']}: {value} — {m['label']}" + (f" ({m['note']})" if m["note"] else ""))
        if bc["config_hash"]:
            lines.append(f"  belief config {bc['config_hash']}")

    lr = d.get("leverage_risk")
    if lr:
        lines += ["", f"-- {lr['title']} --", f"  {lr['caption']}"]
        if lr.get("error"):
            lines.append(f"  {lr['error']}")
        elif lr["status"] != "ok":
            lines.append(
                f"  Not enough stored price history: {lr['history_days']} days, "
                f"needs a {lr['hold_days']:g}-day window plus a week of start points."
            )
        else:
            lines.append(
                f"  {lr['windows']} {lr['hold_days']:g}-day windows over {lr['history_days']} days of "
                f"{lr['venue']} closes; typical {lr['hold_days']:g}-day move now {_pct(lr['typical_hold_move'])}"
            )
        for row in lr.get("rows", []):
            lines.append(
                f"  {row['leverage']:g}x: liquidated by a {_pct(row['liquidating_move'])} move; "
                f"long would have been liquidated in {_pct(row['long_breach_rate'])} of windows, "
                f"short in {_pct(row['short_breach_rate'])}"
            )
        if lr.get("note"):
            lines.append(f"  {lr['note']}")

    tr = d.get("track_records")
    if tr and (tr.get("setups") or tr.get("error")):
        lines += ["", f"-- {tr['title']} --", f"  {tr['caption']}"]
        if tr.get("error"):
            lines.append(f"  {tr['error']}")
        for s in tr.get("setups", []):
            lines.append(f"  [{s['setup']}] {s['status_label']}")
            lines.append(f"    {s['summary']}")
            if s["record"] and s["record"].get("note"):
                lines.append(f"    {s['record']['note']}")

    return "\n".join(lines)
