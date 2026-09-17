import { AnalysisReport } from "../api";
import { GateCard } from "./GateCard";

const VERDICT_COPY: Record<string, { label: string; className: string }> = {
  ELIGIBLE_SETUP: { label: "Eligible setup found", className: "eligible" },
  NO_SETUP: { label: "No setup qualifies", className: "no_setup" },
  GATE_FAIL: { label: "Gate failed — data or universe", className: "gate_fail" },
  CLASSIFIER_CONFLICT: { label: "Classifier conflict — trading nothing", className: "classifier_conflict" },
};

const BIAS_LABEL: Record<string, string> = {
  long: "Long",
  short: "Short",
  unclear: "No clear direction",
};

export function ReportView({ report }: { report: AnalysisReport }) {
  const verdict = VERDICT_COPY[report.verdict] || { label: report.verdict, className: "no_setup" };
  const di = report.data_integrity;
  const sc = report.state_classification;

  return (
    <div>
      <div className={`verdict-banner ${verdict.className}`}>
        <div className="verdict-label">{report.instrument}</div>
        <div className="verdict-value">{verdict.label}</div>
        <div className="verdict-meta mono">
          as of {new Date(report.as_of).toLocaleString()} · config {report.config_hash}
        </div>
        {report.verdict_bias && (
          <div className={`bias-flag ${report.verdict_bias}`}>{BIAS_LABEL[report.verdict_bias]}</div>
        )}
        {!report.config_validated && <div className="unvalidated-flag">Unvalidated thresholds — not yet calibrated</div>}
      </div>

      <div className="section">
        <div className="section-title">Gate status</div>
        {report.gate_status.map((g) => (
          <GateCard gate={g} key={g.gate} />
        ))}
      </div>

      {di && di.sources_used && (
        <div className="section">
          <div className="section-title">Data integrity</div>
          <div className="card">
            <div className="kv-grid">
              <div className="kv-item">
                <div className="kv-label">Sources used</div>
                <div className="kv-value mono" style={{ fontSize: 12 }}>{di.sources_used?.join(", ") || "—"}</div>
              </div>
              <div className="kv-item">
                <div className="kv-label">Sources rejected</div>
                <div className="kv-value mono" style={{ fontSize: 12 }}>
                  {di.sources_rejected?.length ? di.sources_rejected.map((r) => r.source_id).join(", ") : "none"}
                </div>
              </div>
            </div>
            {di.independent_upstream_count_by_metric && (
              <div style={{ marginTop: 10 }}>
                <div className="kv-label">Independent upstreams per metric</div>
                <div className="condition-values mono">
                  {Object.entries(di.independent_upstream_count_by_metric)
                    .map(([k, v]) => `${k}: ${v}`)
                    .join("  ·  ")}
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {sc && sc.regime && (
        <div className="section">
          <div className="section-title">State classification</div>
          <div className="card">
            <div className="kv-grid">
              <div className="kv-item">
                <div className="kv-label">Regime</div>
                <div className="kv-value">{sc.regime.replace(/_/g, " ")}</div>
              </div>
              <div className="kv-item">
                <div className="kv-label">Trapped cohort</div>
                <div className="kv-value">{sc.trapped_cohort?.replace(/_/g, " ") || "unnamed"}</div>
              </div>
            </div>
            {sc.constraint_ratios && (
              <div style={{ marginTop: 10 }}>
                <div className="kv-label">Constraint ratios</div>
                <div className="condition-values mono">
                  {Object.entries(sc.constraint_ratios)
                    .map(([k, v]) => `${k}: ${v ?? "unknown"}`)
                    .join("  ·  ")}
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {report.setup_evaluation.length > 0 && (
        <div className="section">
          <div className="section-title">Setup evaluation</div>
          {report.setup_evaluation.map((s) => (
            <GateCard gate={s} key={s.gate} />
          ))}
        </div>
      )}

      {report.structural_reads.length > 0 && (
        <div className="section">
          <div className="section-title">Structural read</div>
          <div className="card">
            <div className="flip-detail" style={{ marginBottom: 10 }}>
              Evidence from the qualifying setup and the trapped-cohort classification above — not a recommendation. You still decide entry, size, and direction.
            </div>
            {report.structural_reads.map((s, i) => (
              <div className="flip-item" key={i}>
                <div className="flip-title">
                  {s.setup.replace(/_/g, " ")}{" "}
                  <span className={`bias-flag ${s.direction}`} style={{ marginTop: 0, verticalAlign: "middle" }}>
                    {BIAS_LABEL[s.direction]}
                  </span>
                </div>
                <div className="flip-detail">{s.read}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {report.distance_to_flip.length > 0 && (
        <div className="section">
          <div className="section-title">Distance to flip</div>
          <div className="card">
            {report.distance_to_flip.map((f, i) => (
              <div className="flip-item" key={i}>
                <div className="flip-title">
                  [{f.gate.replace(/_/g, " ")}] {f.condition.replace(/_/g, " ")}
                </div>
                <div className="flip-detail mono">
                  currently {f.current_value ?? "unknown"} · needs {f.required ?? "—"}
                </div>
                <div className="flip-detail">{f.detail}</div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
