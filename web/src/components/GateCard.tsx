import { ConditionResult, GateCase, GateResultJSON } from "../api";

const MARKER: Record<string, string> = { pass: "✓", fail: "✕", unknown: "?" };

const CASE_LABEL: Record<GateCase, string> = {
  long: "Long case",
  short: "Short case",
  unclear: "Direction unclear",
  not_directional: "Not directional",
};

const CASE_NOTE: Partial<Record<GateCase, string>> = {
  not_directional: "Checks whether this report can be trusted at all, not which way price goes.",
  unclear: "This setup identifies stretched positioning but doesn't encode which way it resolves.",
};

// For a check inside a Long- or Short-case setup: what its current result means for a long.
function conditionLean(gateCase: GateCase | undefined, c: ConditionResult) {
  if (gateCase !== "long" && gateCase !== "short") return null;
  if (c.status !== "pass") {
    return { className: "lean-tag unmet", label: gateCase === "long" ? "Long case not met" : "Short case not met" };
  }
  return gateCase === "long"
    ? { className: "lean-tag supports_long", label: "▲ Supports long" }
    : { className: "lean-tag against_long", label: "▼ Against long" };
}

export function GateCard({ gate, title }: { gate: GateResultJSON; title?: string }) {
  const note = gate.case ? CASE_NOTE[gate.case] : undefined;
  return (
    <div className="card">
      <div className="gate-header">
        <span className="gate-name">
          {title || gate.gate.replace(/_/g, " ")}
          {gate.case && <span className={`case-tag ${gate.case}`}>{CASE_LABEL[gate.case]}</span>}
        </span>
        <span className={`pill ${gate.passed ? "pass" : "fail"}`}>{gate.passed ? "Pass" : "Fail"}</span>
      </div>
      {note && <div className="case-note">{note}</div>}
      {gate.conditions.map((c) => {
        const lean = conditionLean(gate.case, c);
        return (
          <div className="condition-row" key={c.name}>
            <span className={`condition-marker ${c.status}`}>{MARKER[c.status]}</span>
            <div className="condition-body">
              <div className="condition-name">
                {c.name.replace(/_/g, " ")}
                {lean && <span className={lean.className}>{lean.label}</span>}
              </div>
              <div className="condition-values mono">
                {c.computed_value ?? "—"} <span style={{ opacity: 0.6 }}>vs</span> {c.threshold ?? "—"}
              </div>
              <div className="condition-detail">{c.detail}</div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
