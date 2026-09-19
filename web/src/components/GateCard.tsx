import { ConditionResult, GateCase, GateResultJSON } from "../api";
import { readable } from "../format";

const MARKER: Record<string, string> = { pass: "✓", fail: "✕", unknown: "?" };

const CASE_LABEL: Record<GateCase, string> = {
  long: "Long-type setup",
  short: "Short-type setup",
  unclear: "Direction unclear",
  not_directional: "Trust check",
};

// Plain-English "what is this looking for", so a card can be read without the doctrine.
const ABOUT: Record<string, string> = {
  gate_u:
    "Is this coin fit to analyse at all? Checks it has enough spot liquidity, isn't dominated by derivatives or overloaded with leverage, has sources that can be queried directly, and its volatility is in a workable range.",
  layer_0:
    "Is the data itself trustworthy? Checks it's fresh, confirmed by independent venues, and consistent across them.",
  cascade_absorption:
    "After a sell-off forces leveraged longs out, looks for the selling to be finished: open interest has dropped and stayed down, price has calmed and holds above the sell-off low, funding has reset to zero or below, and liquidations have stopped.",
  squeeze_absorption:
    "The mirror of cascade absorption. After a short squeeze forces leveraged shorts out, looks for the buying to be finished: open interest has dropped and stayed down, price has calmed and stays below the squeeze high, funding has reset to zero or above, and liquidations have stopped.",
  trend_continuation_leverage_reset:
    "In a confirmed uptrend, looks for a pullback that flushes leverage out: price gives back part of the last up-leg without breaking the prior higher low, open interest falls, funding resets, and spot volume holds up against perp.",
  downtrend_continuation_leverage_reset:
    "The mirror for a confirmed downtrend: price bounces back part of the last down-leg without breaking the prior lower high, open interest falls, funding resets, and spot volume holds up against perp.",
  positioning_exhaustion:
    "Looks for traders piling into a losing position: holding it costs a lot relative to the expected move, open interest is rising while price falls, and market structure has broken. It shows positioning is stretched, not which way it resolves.",
  event_decompression:
    "Looks for one-sided positioning around a dated event, read after the event has happened. No event calendar is connected yet, so the event check shows ? until one is.",
};

const CASE_NOTE: Partial<Record<GateCase, string>> = {
  not_directional: "Checks whether this report can be trusted at all, not which way price goes.",
  unclear: "This setup identifies stretched positioning but doesn't encode which way it resolves.",
};

// Per-check tag. A met check inside a Long- or Short-type setup says what it means for a
// long; an unmet one says whether the data said no or there wasn't enough data to check.
function conditionTag(gateCase: GateCase | undefined, c: ConditionResult) {
  if (c.status === "fail") return { className: "lean-tag unmet", label: "Not met" };
  if (c.status === "unknown") return { className: "lean-tag unmet", label: "No data yet" };
  if (gateCase === "long") return { className: "lean-tag supports_long", label: "▲ Supports long" };
  if (gateCase === "short") return { className: "lean-tag against_long", label: "▼ Against long" };
  return null;
}

function Score({ gate, isSetup }: { gate: GateResultJSON; isSetup: boolean }) {
  const total = gate.conditions.length;
  const met = gate.conditions.filter((c) => c.status === "pass").length;
  const notMet = gate.conditions.filter((c) => c.status === "fail").length;
  const noData = total - met - notMet;

  const parts = [`${met} of ${total} checks met`];
  if (notMet) parts.push(`${notMet} not met`);
  if (noData) parts.push(`${noData} no data yet`);

  let summary: string;
  if (gate.passed) {
    summary = isSetup ? "Present: every check is met." : "Passed: every check is met.";
  } else if (isSetup) {
    summary = `Not present. A setup counts only when all ${total} checks are met.`;
  } else {
    summary = "Failed. Every check must pass before any setup is evaluated.";
  }

  return (
    <div className="score">
      <div className="score-row">
        <span className="pips" aria-hidden="true">
          {gate.conditions.map((c) => (
            <span className={`pip ${c.status}`} key={c.name} />
          ))}
        </span>
        <span className="score-text">{parts.join(" · ")}</span>
      </div>
      <div className="score-summary">{summary}</div>
    </div>
  );
}

export function GateCard({ gate, title }: { gate: GateResultJSON; title?: string }) {
  const isSetup = gate.case !== "not_directional";
  const about = ABOUT[gate.gate] ?? (gate.case ? CASE_NOTE[gate.case] : undefined);
  const pill = gate.passed
    ? { className: "pass", label: isSetup ? "Present" : "Pass" }
    : { className: isSetup ? "absent" : "fail", label: isSetup ? "Not present" : "Fail" };

  return (
    <div className="card">
      <div className="gate-header">
        <span className="gate-name">
          {title || gate.gate.replace(/_/g, " ")}
          {gate.case && <span className={`case-tag ${gate.case}`}>{CASE_LABEL[gate.case]}</span>}
        </span>
        <span className={`pill ${pill.className}`}>{pill.label}</span>
      </div>
      {about && <div className="case-note">{about}</div>}
      <Score gate={gate} isSetup={isSetup} />
      {gate.conditions.map((c) => {
        const tag = conditionTag(gate.case, c);
        return (
          <div className="condition-row" key={c.name}>
            <span className={`condition-marker ${c.status}`}>{MARKER[c.status]}</span>
            <div className="condition-body">
              <div className="condition-name">
                {c.name.replace(/_/g, " ")}
                {tag && <span className={tag.className}>{tag.label}</span>}
              </div>
              <div className="condition-values mono" title={`${c.computed_value ?? "—"} vs ${c.threshold ?? "—"}`}>
                now {readable(c.computed_value)} <span style={{ opacity: 0.6 }}>· threshold</span> {readable(c.threshold)}
              </div>
              <div className="condition-detail">{c.detail}</div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
