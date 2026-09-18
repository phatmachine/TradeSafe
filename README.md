# TradeSafe

*Localised trade analysis.*

A mobile-first evidence-analysis app for crypto perpetual futures. You name an
instrument; it runs that instrument through a deterministic, adversarial-market data
pipeline and returns an honest report of what the data supports, what it does not, which
of a small set of permitted setups qualify — and, most often, why none do.

**This is not a trading app.** It never places an order, never emits a position size, an
entry price, a target, or a directional recommendation, and never writes to an exchange.
See `docs/doctrine-summary.md` for the full reasoning behind every gate and check in this
codebase, and the original Doctrine / Implementation spec documents this build was
generated from.

## How it's built

```
backend/
  config/        thresholds.yaml, universe.yaml, sources.yaml, instruments.yaml
  core/          Observation model, half-life expiry, config loading, SourceRegistry
  sources/       venue/chain/liquidation fetchers — the only code that talks to the internet
  compute/       pure arithmetic: OI aggregation, volatility, regime, cohort, ratios
  gates/         Gate U (universe) and Layer 0 (data integrity) — fail-closed, no LLM
  setups/        the four permitted Layer 3 setups, per-condition evaluation
  replay/        DataSource abstraction (live vs. replay share the exact same code path),
                 the calibration sweep, and its scoring
  report/        the report contract, terminal rendering, exit monitoring
  store/         SQLite persistence — the only place SQL lives
  service/       the always-on collector and the on-demand FastAPI analysis service
  scripts/       report_cli.py and calibrate.py — run either from a terminal, no API needed
web/             the mobile-first React frontend
deploy/          Docker Compose + Caddy + a from-scratch VPS deployment guide
```

**No LLM anywhere in the decision path.** Every gate, threshold and classification in
`gates/`, `compute/` and `setups/` is arithmetic over stored observations — deterministic,
reproducible, and auditable line by line. That's a design constraint from the
implementation spec, not an incidental choice: a language model in that path would
reintroduce exactly the unaccountable reasoning the doctrine exists to eliminate.

## Local development

Backend:

```
cd backend
pip install -r requirements.txt
TRADESAFE_PASSWORD=devpass TRADESAFE_SECRET=devsecret TRADESAFE_INSECURE_COOKIE=true \
  uvicorn service.api:app --reload --port 8000
```

In another terminal, run the collector so the store actually fills with data:

```
cd backend
TRADESAFE_PASSWORD=devpass TRADESAFE_SECRET=devsecret python -m service.collector
```

Frontend:

```
cd web
npm install
npm run dev
```

Open `http://localhost:5173`, log in with `devpass`, and name an instrument. Vite's dev
proxy forwards `/api` and `/health` to `localhost:8000` (see `web/vite.config.ts`).

Run a report without the API at all:

```
cd backend
TRADESAFE_DB_PATH=./store/records.db python -m scripts.report_cli BTC
```

## Deploying for real

See `deploy/README.md` — a small VPS running the collector, the API, and the frontend
behind Caddy (automatic HTTPS), plus how to check exchange-endpoint reachability and
clock sync from the host before you commit to it, and how to run the calibration sweep
once enough history has accumulated.

## Known limitations (read before trusting a report)

- **Thresholds are unvalidated by default.** Every number in
  `backend/config/thresholds.yaml` starts as a documented placeholder
  (`meta.validated: false`). The doctrine's own validation protocol says run write-only
  for 60–90 days and calibrate before any of this influences a real decision — see
  `backend/replay/sweep.py` and `deploy/README.md` §5.
- **Free-float coverage is partial.** A full per-chain node/indexer (the doctrine's own
  ideal) isn't standing behind every chain — see `backend/sources/chain.py` and
  `backend/config/instruments.yaml`. An instrument on an unconfigured chain fails Gate U
  condition 5 rather than guessing; extend `instruments.yaml` to add real coverage.
- **Event/calendar ingestion (FOMC, filings, unlock schedules) isn't wired up.** The
  event-decompression setup evaluates correctly against whatever `EVENT` observations
  exist, but will honestly report `unknown` until a real calendar source is registered —
  see `backend/sources/issuer.py` and `backend/setups/event.py`.
- **Liquidations come from a single venue in practice.** OKX is polled over REST
  (`backend/sources/okx_liquidations.py`, ~24h backfilled on a cold start); Binance's
  websocket listener (`backend/sources/liquidations.py`) also runs but websocket frames
  don't flow in the current deployment environment. Either way these are — by the
  doctrine's own admission — samples, not a census; cascade/squeeze detection is
  deliberately built on coin-denominated OI delta, per the doctrine's substitution rule,
  not on liquidation totals. The trapped-cohort classification needs activity in each of
  three 24h buckets, so it stays `unnamed` for roughly the first two days of collection.
- **OI-weighted funding is approximated as a simple cross-venue mean** pending
  calibration (doctrine 2.3 asks for OI-weighting specifically) — see the note in
  `backend/setups/cascade.py`.
