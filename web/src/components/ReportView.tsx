import { AnalysisReport } from "../api";
import { readable } from "../format";
import { GateCard } from "./GateCard";

const VERDICT_COPY: Record<string, { label: string; className: string; explain: string }> = {
  ELIGIBLE_SETUP: {
    label: "Eligible setup found",
    className: "eligible",
    explain:
      "The data passed every trust check and at least one setup has all its checks met. See which one under Setup evaluation, and what it implies under Structural read.",
  },
  NO_SETUP: {
    label: "No setup qualifies",
    className: "no_setup",
    explain:
      "The data passed every trust check, but none of the setups has all its checks met right now. This is a real \"no\", not missing data.",
  },
  GATE_FAIL: {
    label: "Gate failed — data or universe",
    className: "gate_fail",
    explain:
      "A trust check failed, so no setups were evaluated. Either this coin doesn't meet the basic bar for analysis, or the data isn't trustworthy right now. See Gate status.",
  },
  CLASSIFIER_CONFLICT: {
    label: "Classifier conflict — trading nothing",
    className: "classifier_conflict",
    explain:
      "Two setups that should never qualify together both did, so the report declines to pick either.",
  },
};

// With the regime undetermined the report stops before any setup runs, so "no setup
// qualifies" means something different: nothing was checked, rather than nothing passed.
const UNDETERMINED_EXPLAIN =
  "The data passed every trust check, but the market regime couldn't be classified, so no setups were checked.";

const LEAN_LABEL: Record<string, string> = {
  supports_long: "▲ Supports long",
  against_long: "▼ Against long",
  neutral: "● Neutral",
  unknown: "? Not enough data",
};

const BIAS_LABEL: Record<string, string> = {
  long: "Long",
  short: "Short",
  unclear: "No clear direction",
};

export function ReportView({ report }: { report: AnalysisReport }) {
  const verdict = VERDICT_COPY[report.verdict] || { label: report.verdict, className: "no_setup", explain: "" };
  const di = report.data_integrity;
  const sc = report.state_classification;
  const explain =
    report.verdict === "NO_SETUP" && report.setup_evaluation.length === 0 ? UNDETERMINED_EXPLAIN : verdict.explain;
  const gatesPassed = report.gate_status.filter((g) => g.passed).length;
  const setupsPresent = report.setup_evaluation.filter((s) => s.passed).length;

  return (
    <div>
      <div className={`verdict-banner ${verdict.className}`}>
        <div className="verdict-label">{report.instrument}</div>
        <div className="verdict-value">{verdict.label}</div>
        {explain && <div className="verdict-explain">{explain}</div>}
        <div className="verdict-tally">
          <span>
            Trust checks <b>{gatesPassed} of {report.gate_status.length}</b> passed
          </span>
          <span>
            Setups present{" "}
            <b>{report.setup_evaluation.length ? `${setupsPresent} of ${report.setup_evaluation.length}` : "none checked"}</b>
          </span>
        </div>
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
        <div className="section-intro">
          Trust checks that run first. Both must pass before any setup is looked at. If one fails, nothing further down
          can be relied on.
        </div>
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

      {report.directional_factors && report.directional_factors.length > 0 && (
        <div className="section">
          <div className="section-title">Directional factors</div>
          <div className="card">
            <div className="flip-detail" style={{ marginBottom: 10 }}>
              What each piece of directional evidence currently means for a long. Context only — these don't change the verdict above.
            </div>
            {report.directional_factors.map((f) => (
              <div className="factor-row" key={f.factor}>
                <div className="factor-head">
                  <span className="factor-name">{f.factor}</span>
                  <span className={`lean-pill ${f.lean}`}>{LEAN_LABEL[f.lean]}</span>
                </div>
                {f.value && <div className="condition-values mono">{f.value}</div>}
                <div className="condition-detail">{f.reason}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {report.setup_evaluation.length > 0 && (
        <div className="section">
          <div className="section-title">Setup evaluation</div>
          <div className="section-intro">
            Each setup is a market pattern this app looks for. It counts as present only when <b>every</b> check is met.
            The tag shows which way the pattern points <i>if</i> it's present. It isn't a result.
            <div className="legend setup-legend">
              <span><span className="condition-marker pass">✓</span> met</span>
              <span><span className="condition-marker fail">✕</span> checked, not met</span>
              <span><span className="condition-marker unknown">?</span> not enough data to check</span>
            </div>
            <div>
              In each card's bar, a met check is <span className="lean-word supports_long">green</span> if it supports a
              long and <span className="lean-word against_long">red</span> if it counts against one. Unmet checks are
              grey: a missing piece of one setup isn't evidence for the other side.
            </div>
            {sc?.regime && (
              <div>
                Which setups run depends on the market regime, currently <b>{sc.regime.replace(/_/g, " ")}</b>.
              </div>
            )}
          </div>
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
                <div className="flip-detail mono" title={`currently ${f.current_value ?? "unknown"} · needs ${f.required ?? "—"}`}>
                  currently {f.current_value == null ? "unknown" : readable(f.current_value)} · needs {readable(f.required)}
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
