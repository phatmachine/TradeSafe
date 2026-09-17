import { GateResultJSON } from "../api";

const MARKER: Record<string, string> = { pass: "✓", fail: "✕", unknown: "?" };

export function GateCard({ gate, title }: { gate: GateResultJSON; title?: string }) {
  return (
    <div className="card">
      <div className="gate-header">
        <span className="gate-name">{title || gate.gate.replace(/_/g, " ")}</span>
        <span className={`pill ${gate.passed ? "pass" : "fail"}`}>{gate.passed ? "Pass" : "Fail"}</span>
      </div>
      {gate.conditions.map((c) => (
        <div className="condition-row" key={c.name}>
          <span className={`condition-marker ${c.status}`}>{MARKER[c.status]}</span>
          <div className="condition-body">
            <div className="condition-name">{c.name.replace(/_/g, " ")}</div>
            <div className="condition-values mono">
              {c.computed_value ?? "—"} <span style={{ opacity: 0.6 }}>vs</span> {c.threshold ?? "—"}
            </div>
            <div className="condition-detail">{c.detail}</div>
          </div>
        </div>
      ))}
    </div>
  );
}
