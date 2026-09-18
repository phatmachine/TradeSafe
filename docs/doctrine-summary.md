# Adversarial Trading Doctrine v2 — summary

This is a working summary of the full doctrine for engineers touching this codebase.
The complete doctrine and implementation spec PDFs are the source of truth; where this
summary and the code disagree with them, the PDFs win. Keep copies of both alongside
this repo.

## Mission

This is **not** a trading app. It never places an order, never emits a position size, an
entry price, a target, or a directional recommendation, and never writes to an exchange.
It is an evidence-reporting tool: given a target instrument, it returns an honest report of
what the data supports, what it does not, which of a small set of permitted setups
qualify, and — most often — why none do. The user reads the evidence and carries every
consequence of every decision themselves.

## Core premise

Every input is hostile until proven otherwise. Verification is a trading activity, not an
administrative one. The absence of verified data produces "unknown", never a smaller
position or a lower-confidence guess. The tool's own behaviour must not be predictable
bait for anyone modelling it (out of scope for v1 of this build — noted as a known
limitation).

## Four corruption classes

| Class | Mechanism | Defense |
|---|---|---|
| Stale | true once, false now | half-life expiry (`observed_at` + per-metric TTL) |
| Synthetic | no underlying observation | synthetic content filter, T0 rejection |
| Adversarial | manufactured to induce a decision | "adversary's question" — human judgement, tool only flags |
| Structurally distorted | real measurement, wrong construction | ground-truth set (Layer 1), coin-denominated ratios |

## Gate U — Universe (decided while flat, before any run)

An instrument is eligible only if **all** hold:
1. Spot depth sufficient for intended size to be a small fraction of resting liquidity
2. Perp/spot volume ratio below a configured ceiling
3. Open interest / market cap below a configured ceiling
4. T1 data available (a venue API and a chain source, queryable directly)
5. Free float computable (locked/staked/fund-held supply identifiable)
6. Realised volatility inside a band where ATR-derived stops leave a position worth taking

**The bypass is the failure mode.** An instrument named by a person, not found by
screening, must run through Gate U like anything else — the API takes any symbol,
subjects it to the same gate, and shows the failure if it fails.

## Layer 0 — Data warfare protocol (binding gate; nothing downstream runs without it)

- **0.1 Provenance tiers**: T1 verified (venue API / chain / filing) → actionable alone. T2
  derived (aggregators) → actionable only after independent cross-confirmation. T3
  interpreted → hypotheses only, never triggers anything. T4 belief (news/social) →
  evidence of belief, never of fact. T0 rejected → excluded entirely, not down-weighted.
- **0.2 Half-life**: every observation has `observed_at` + an expiry. Expired data is
  deleted from the query path, never used "as old data".
- **0.3 Independence, not count**: sources sharing an `upstream_id` count as one.
- **0.4 Dispersion is data**: cross-venue disagreement is logged as a signal, never
  averaged away.
- **0.5 Synthetic content filter**: auto-T0 on internal contradiction, no named author, no
  stated methodology, implausible cadence, or failure to reconcile against a primary
  source. One failure condemns the whole source.
- **0.6 Adversary's question**: before acting, ask what a hostile actor would plant to
  provoke this exact strategy. Human/LLM-summary judgement only — never blocks the
  deterministic gate itself, always surfaced to the reader.
- **0.7 Asymmetric verification burden**: confirming evidence is audited harder than
  disconfirming evidence.
- **0.8 Epistemic kill switch**: any data-layer degradation → the whole run reports
  `GATE_FAIL`/`UNKNOWN`. There is no reduced-confidence intermediate state.
- **0.9 Source rejection ≠ datum falsity**: rejecting a source says nothing about whether
  its number was true. Where a rejected source is the sole carrier, the datum is
  `UNKNOWN`, not "probably fine".
- **0.10 A discarded record is discarded whole**: no cherry-picking fields from a
  snapshot that failed on another field.

## Layer 1 — Ground truth set

Only these may trigger a decision (T1, or T2 cross-confirmed): coin-denominated
aggregated OI + rate of change; OI-weighted, vol-normalised funding; perp/spot volume;
basis/term structure; realised volatility (multi-window); completed liquidations with
dominant side; free float; dated primary-sourced events.

**Explicitly excluded, always**: whale/large-transfer alerts, single-venue long/short
ratios, sentiment indices as timing tools, sub-hourly charts, any oscillator as an entry
trigger.

**Open interest is counted in coins, never dollars.** Dollar OI conflates position change
with price change. **Reported liquidation totals are throttled samples** — coin-OI delta
substitutes for cascade-absorption confirmation.

## Layer 2 — State classification

- **2.1 Regime**: trending / mean-reverting / `undetermined` (blocks all setups) from
  realised-vol structure + the OI/price quadrant. Mean-reversion setups lock out in a
  trending regime.
- **2.2 Trapped cohort**: name who is forced to act, which way, by what deadline, from
  the sign of net liquidations. If it can't be named, there is no trade.
- **2.3 Constraint ratios**: coin OI + rate of change vs price; OI/market-cap; perp/spot
  volume; carry cost vs expected move. Crowding is fragility only when carry is
  genuinely expensive relative to the move being faded.

## Layer 3 — Permitted setups (exactly six — four, plus mirrors of the two directional ones; anything else is declined)

1. **Cascade absorption** — enter only after forced selling has *completed*: OI collapse
   confirmed and held, funding reset, liquidation print settled, price stabilising above
   the flush wick. Never anticipatory.
2. **Positioning exhaustion** — requires all three: material carry cost, OI rising into a
   failing price, a structural break (lower high / higher low). Never on funding alone.
3. **Event decompression** — a dated, primary-sourced event with verifiable one-sided
   positioning, traded after it resolves, never into it.
4. **Trend continuation on leverage reset** — in a confirmed uptrend, buy the pullback
   where all four hold: 15–25% retrace of the last swing leg without breaking the prior
   higher low; coin OI falls during the retrace; funding normalises to ≤0; spot volume
   holds up vs perp. Invalidated by a close below the prior higher low with OI rising.
5. **Squeeze absorption** (mirror of 1, added 2026-09-18) — enter short only after forced
   *buying* has completed: OI collapse confirmed and held, funding reset to ≥0,
   liquidation print settled, price stabilising below the squeeze wick. Reads Short only
   when the trapped cohort is `trapped_shorts`.
6. **Downtrend continuation on leverage reset** (mirror of 4, added 2026-09-18) — in a
   confirmed downtrend, sell the rally where all four hold: 15–25% retrace of the last
   swing leg without breaking the prior lower high; coin OI falls during the rally;
   funding normalises to ≥0; spot volume holds up vs perp. Invalidated by a close above
   the prior lower high with OI rising.

Trend continuation (either direction) and positioning exhaustion can never both qualify —
if both do, the regime classifier is wrong and the run reports `CLASSIFIER_CONFLICT`, not
a trade.

## Out of scope for this tool (by design)

Layer 4 (sizing), Layer 5 (kill switches tied to account equity/drawdown) and Layer 6's
P&L side are **manual trading disciplines**, not something a report-only, no-account-state
tool computes — the doctrine is explicit that the tool never emits a size or manages a
position. What the tool *does* carry from those layers:
- **Cost model** inputs (funding vs expected move) feed the 2.3 constraint ratios and the
  positioning-exhaustion setup.
- **Layer 6 source scoring** (reliability prior, T0 demotion on repeat failure) is
  implemented as a persistent `SourceRegistry` field, updated by a scoring loop.
- **Exit monitoring** is a separate, explicitly-manual command: the user records an open
  position's setup, entry evidence and trapped-cohort claim; the tool re-evaluates
  thesis-invalidation, time-stop and premise-spent status against fresh data. It reports;
  it never closes anything.

## Validation posture

No capital should be committed on this tool's output until it has been run write-only and
measured (see `/replay`). Every threshold above lives in `config/thresholds.yaml`, is
versioned via a config hash on every report, and should be calibrated from real history
before being trusted — reports produced with placeholder thresholds are labelled
`unvalidated` until a calibration sweep has been run.

Two calibration routes, used together (added 2026-09-18):
- **Full replay** (`backend/replay/sweep.py`) runs the whole pipeline, gates included, at
  past instants. Exact, but only over periods the collector itself recorded, because the
  gates need order-book depth and fresh per-venue readings that no venue publishes
  historically.
- **Offline history** (`python -m backend.research`) runs the same regime classifier and
  setup code over years of free exchange history (price, volume, funding, Bybit open
  interest, back to 2023) and scores each setup's signals against what price then did.
  A **stated departure** from "same code path": it skips Gate U and Layer 0 (evaluating
  every instant as if the data were trustworthy), uses narrower inputs than live (single-
  venue OI and volume, settled rather than running funding), and has no liquidation
  history — so cascade/squeeze absorption are scored without their liquidation check and
  no trapped cohort is named. Thresholds those gaps touch keep calibrating from live
  collection.
