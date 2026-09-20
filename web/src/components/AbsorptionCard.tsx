import { GateResultJSON } from "../api";
import { ConditionRow } from "./GateCard";

/**
 * Cascade absorption and squeeze absorption as one card.
 *
 * They are mirrors, and three of their five checks — oi_collapse_confirmed_and_held,
 * price_range_contracted and liquidation_print_settled — are the same computation on the
 * same inputs (backend/setups/cascade.py evaluates both sides through one function; only
 * the wick test and funding_reset branch on `side`). Rendered as two GateCards they
 * printed two "N of 5" scores that could never be independent readings, so a reader saw
 * a balance the app was not measuring.
 *
 * Replayed hourly over 2023-01-01..2026-09-18 on BTC/ETH/SOL/ZEC (45,499 mean-reverting
 * instants): the two scores were identical 39.1% of the time, and short was ahead 49.9%
 * against long's 10.9% — almost entirely because funding_reset's short side is `>= 0`
 * against funding that is positive 70-86% of the time, so it passed 81.4% of instants
 * against the long side's 16.6%. The two checks that do differ separated 7-day forward
 * returns by -0.0pp: no directional information. Hence one card, the shared preconditions
 * stated once, and the side-specific checks shown as a split rather than as two scores.
 */

export const SHARED = ["oi_collapse_confirmed_and_held", "price_range_contracted", "liquidation_print_settled"];

const ABOUT =
  "After a flush forces leveraged traders out, looks for the move to be finished: open interest has dropped " +
  "and stayed down, price has calmed, and liquidations are back to their usual level. Those three checks are " +
  "the same for both sides. Only the last two differ — which way price is holding against the flush bar, and " +
  "which way funding has reset.";

export function AbsorptionCard({ long, short }: { long: GateResultJSON; short: GateResultJSON }) {
  const shared = long.conditions.filter((c) => SHARED.includes(c.name));
  const sideOnly = (g: GateResultJSON) => g.conditions.filter((c) => !SHARED.includes(c.name));
  const sharedMet = shared.filter((c) => c.status === "pass").length;

  const pill = long.passed
    ? { className: "present", label: "Long-type present" }
    : short.passed
      ? { className: "present", label: "Short-type present" }
      : { className: "absent", label: "Not present" };

  return (
    <div className="card setup-card">
      <div className="gate-header">
        <span className="gate-name">absorption — cascade / squeeze</span>
        <span className={`pill ${pill.className}`}>{pill.label}</span>
      </div>
      <div className="case-note">{ABOUT}</div>

      <div className="score">
        <div className="score-row">
          <span className="pips" aria-hidden="true">
            {shared.map((c) => (
              <span className={`pip ${c.status}`} key={c.name} />
            ))}
          </span>
          <span className="score-text">
            {sharedMet} of {shared.length} shared preconditions met
          </span>
        </div>
        <div className="score-summary">
          These gate both sides equally — they are the same measurement, not two readings. A side counts as present
          only when all three are met <i>and</i> both of its own checks below are met.
        </div>
      </div>
      {shared.map((c) => (
        <ConditionRow gateCase={undefined} c={c} key={c.name} />
      ))}

      <div className="split-heading">The two checks that differ by side</div>
      <div className="side-split">
        <div className="side-column">
          <div className="side-label long">Long — cascade absorption</div>
          {sideOnly(long).map((c) => (
            <ConditionRow gateCase="long" c={c} key={c.name} />
          ))}
        </div>
        <div className="side-column">
          <div className="side-label short">Short — squeeze absorption</div>
          {sideOnly(short).map((c) => (
            <ConditionRow gateCase="short" c={c} key={c.name} />
          ))}
        </div>
      </div>
      <div className="case-note side-split-note">
        Perp funding is positive most of the time, so the short side's funding check is met far more often than the
        long side's. More met checks on one side is not by itself evidence for that side; which way a present setup
        points is set by the trapped cohort in the structural read.
      </div>
    </div>
  );
}
