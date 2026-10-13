import { LeverageRiskJSON, RecordStatus, TrackRecordsJSON } from "../api";

// Report section 8 and the setup track records: context added after the decision. Drawn
// without green or red, which on this page mean evidence for or against a side; risk is
// shown in amber, by how often a position at that leverage would have been liquidated.

const venueName = (v?: string | null) => (v ? v.charAt(0).toUpperCase() + v.slice(1) : "stored");

const pct = (x: number | null | undefined, digits = 0) => (x == null ? "unknown" : `${(x * 100).toFixed(digits)}%`);

function RateCell({ rate }: { rate: number | null }) {
  if (rate == null) return <td className="lev-rate unknown">unknown</td>;
  return (
    <td className="lev-rate">
      <span className="lev-bar" style={{ width: `${Math.max(2, rate * 100)}%`, opacity: 0.25 + 0.75 * rate }} />
      <span className="lev-num mono">{pct(rate)}</span>
    </td>
  );
}

export function LeverageRisk({ risk }: { risk: LeverageRiskJSON }) {
  const ok = risk.status === "ok";
  return (
    <div className="section" id="leverage-risk">
      <div className="section-title">{risk.title}</div>
      <div className="section-intro">{risk.caption}</div>
      <div className="card">
        {risk.error ? (
          <div className="condition-detail">{risk.error}</div>
        ) : (
          <>
            <div className="lev-intro">
              {ok ? (
                <>
                  Over the last <b>{risk.history_days} days</b> of {venueName(risk.venue)} prices, {risk.windows} overlapping{" "}
                  {risk.hold_days}-day windows. The typical {risk.hold_days}-day move right now is about{" "}
                  <b>{pct(risk.typical_hold_move, 1)}</b>.
                </>
              ) : (
                <>
                  Not enough stored price history yet ({risk.history_days} days) to count past windows. The liquidating
                  moves below still apply.
                </>
              )}
            </div>
            <table className="lev-table">
              <thead>
                <tr>
                  <th>Leverage</th>
                  <th>Liquidating move</th>
                  <th>Long liquidated in</th>
                  <th>Short liquidated in</th>
                </tr>
              </thead>
              <tbody>
                {risk.rows?.map((r) => (
                  <tr key={r.leverage}>
                    <td className="mono">{r.leverage}x</td>
                    <td className="mono">
                      {pct(r.liquidating_move, 1)}
                      {r.move_vs_typical != null && (
                        <span className="lev-sub">{r.move_vs_typical.toFixed(1)}× typical</span>
                      )}
                    </td>
                    <RateCell rate={r.long_breach_rate} />
                    <RateCell rate={r.short_breach_rate} />
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="condition-detail" style={{ marginTop: 8 }}>
              "Liquidated in" is the share of past {risk.hold_days}-day windows in which price moved that far against
              the position at some point. {risk.note}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

const STATUS_CLASS: Record<RecordStatus, string> = {
  supported: "on",
  unproven: "on",
  no_direction: "on",
  no_record: "on",
  losing: "off",
};

export function TrackRecords({ records }: { records: TrackRecordsJSON }) {
  if (!records.error && records.setups.length === 0) return null;
  return (
    <div className="section" id="track-records">
      <div className="section-title">{records.title}</div>
      <div className="section-intro">{records.caption}</div>
      <div className="card">
        {records.error && <div className="condition-detail">{records.error}</div>}
        {records.setups.map((s) => (
          <div className="factor-row" key={s.setup}>
            <div className="factor-head">
              <span className="factor-name">{s.setup.replace(/_/g, " ")}</span>
              <span className={`record-pill ${STATUS_CLASS[s.status]}`}>{s.status_label}</span>
            </div>
            <div className="condition-detail">{s.summary}</div>
            {s.record?.note && <div className="condition-detail">{s.record.note}</div>}
          </div>
        ))}
        {records.measured_at && (
          <details className="record-method">
            <summary>How these were measured ({records.measured_at})</summary>
            <div className="condition-detail">{records.method}</div>
            <ul>
              {records.caveats?.map((c) => (
                <li className="condition-detail" key={c}>
                  {c}
                </li>
              ))}
            </ul>
          </details>
        )}
      </div>
    </div>
  );
}
