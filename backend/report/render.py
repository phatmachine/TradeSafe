"""Terminal rendering of an AnalysisReport. JSON is canonical (implementation spec,
"Formats") — this is a view of it and must never contain anything the JSON lacks.
"""
from __future__ import annotations

from backend.report.contract import AnalysisReport


def _render_gate(gate: dict) -> str:
    lines = [f"  [{gate['gate']}] {'PASS' if gate['passed'] else 'FAIL'}"]
    for c in gate["conditions"]:
        marker = {"pass": "OK ", "fail": "FAIL", "unknown": "?  "}[c["status"]]
        lines.append(f"    {marker} {c['name']}: {c['computed_value']} (threshold {c['threshold']}) — {c['detail']}")
    return "\n".join(lines)


def render_text(report: AnalysisReport) -> str:
    d = report.to_dict()
    lines = [
        f"=== {d['instrument']} — {d['verdict']} ===",
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

    if d["setup_evaluation"]:
        lines += ["", "-- Setup evaluation --"]
        for setup in d["setup_evaluation"]:
            lines.append(_render_gate(setup))

    if d["structural_reads"]:
        lines += ["", "-- Structural read (evidence, not a recommendation) --"]
        for item in d["structural_reads"]:
            lines.append(f"  [{item['setup']}] {item['read']}")

    if d["distance_to_flip"]:
        lines += ["", "-- Distance to flip --"]
        for item in d["distance_to_flip"]:
            lines.append(
                f"  [{item['gate']}] {item['condition']}: currently {item['current_value']}, "
                f"needs {item['required']} — {item['detail']}"
            )

    return "\n".join(lines)
