import { useEffect, useState } from "react";
import { BeliefContextJSON, BeliefMetric, BeliefSource } from "../api";

// Report section 7: what participants say (T4), never what is true. Nothing here feeds the
// verdict or any check, so it is drawn in neutral colours only: no green or red, which on
// this page mean evidence for or against a side.

const SOURCE_NAME: Record<string, string> = { stocktwits: "Stocktwits" };

function value(m: BeliefMetric): string {
  if (m.value == null) return "Unknown";
  const x = Number(m.value);
  if (m.unit === "ratio") return `${(x * 100).toFixed(0)}%`;
  if (m.unit === "minutes") return `${x.toLocaleString()} min`;
  return x.toLocaleString();
}

function when(iso: string) {
  return new Date(iso).toLocaleString([], { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });
}

function Source({ s, now }: { s: BeliefSource; now: number }) {
  const name = `${SOURCE_NAME[s.source_id] ?? s.source_id}${s.symbol ? ` · ${s.symbol}` : ""}`;
  // The server never returns an expired reading; this covers a page left open past expiry.
  const expired = s.expires_at != null && Date.parse(s.expires_at) <= now;
  return (
    <div className="belief-source">
      <div className="factor-head">
        <span className="factor-name">{name}</span>
        {s.status === "current" && !expired && s.observed_at && s.expires_at && (
          <span className="belief-when mono">
            newest message {when(s.observed_at)} · expires {when(s.expires_at)}
          </span>
        )}
      </div>
      {expired ? (
        <div className="condition-detail">Expired at {when(s.expires_at!)}. Reload the report for a current reading.</div>
      ) : s.status === "current" ? (
        s.metrics.map((m) => (
          <div className="belief-metric" key={m.metric}>
            <span className="belief-label">{m.label}</span>
            <span className={`belief-value mono ${m.value == null ? "unknown" : ""}`}>{value(m)}</span>
            {m.note && <div className="condition-detail belief-note">{m.note}</div>}
          </div>
        ))
      ) : (
        <div className="condition-detail">{s.detail}</div>
      )}
    </div>
  );
}

export function BeliefContext({ context }: { context: BeliefContextJSON }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const tick = window.setInterval(() => setNow(Date.now()), 60_000);
    return () => window.clearInterval(tick);
  }, []);

  return (
    <div className="section">
      <div className="section-title">
        Belief context <span className="belief-tag">T4 · not evaluated</span>
      </div>
      <div className="card belief-card">
        <div className="belief-caption">{context.caption}</div>
        {context.error && <div className="condition-detail">{context.error}</div>}
        {context.sources.map((s) => (
          <Source s={s} now={now} key={s.source_id} />
        ))}
        {context.config_hash && <div className="belief-meta mono">belief config {context.config_hash}</div>}
      </div>
    </div>
  );
}
