# TradeSafe — project memory

What we've learned building and running this app: how to run and deploy it, what the
data can and can't do, and decisions already made (with the numbers behind them).
Claude Code loads this file automatically in this folder; it's also meant for people.
This repository is **public** — never put passwords, secrets or API keys in this file.

## What the app is

A deterministic, no-LLM evidence tool for crypto perpetual futures. Every gate, setup
and threshold is plain arithmetic over stored observations and **fails closed**: missing
or stale data gives `unknown` / `GATE_FAIL`, never a guess. It reports evidence, never
advice — no orders, sizes or targets; alert and UI wording stays factual, never imperative.

- Trust gates run first: Gate U (is the coin fit to analyse) and Layer 0 (is the data
  fresh, independent, consistent). If either fails, nothing below is evaluated.
- The regime (`trending_up` / `trending_down` / `mean_reverting` / `undetermined`)
  decides which setups run. `undetermined` blocks every setup.
- A setup is present only when **every** check passes. The "N of M checks met" count
  in the UI is distance to a setup, not a confidence score.

## Running locally (Windows)

Three processes, all from the repo root (imports are `backend.…`, so not from `backend/`):

```bash
# API (needs TRADESAFE_PASSWORD + TRADESAFE_SECRET; INSECURE_COOKIE for plain http)
TRADESAFE_PASSWORD=devpass TRADESAFE_SECRET=devsecret TRADESAFE_INSECURE_COOKIE=true \
  .venv/Scripts/python.exe -m uvicorn backend.service.api:app --port 8000
# Collector (the only writer of observations). COINALYZE_API_KEY enables Binance/Bybit
# liquidations; FRED_API_KEY enables CPI/jobs/PCE dates (FOMC dates need no key).
TRADESAFE_PASSWORD=devpass TRADESAFE_SECRET=devsecret COINALYZE_API_KEY=<key> FRED_API_KEY=<key> \
  .venv/Scripts/python.exe -m backend.service.collector
# Web: http://localhost:5173 (Vite proxies /api and /health to :8000), log in with devpass
cd web && npm run dev
```

- Tests: `.venv/Scripts/python.exe -m pytest -q` (repo root). Web type-check: `cd web && npx tsc -b`.
- Neither backend process auto-restarts. A dead collector makes every report `GATE_FAIL`.
- **Don't use `uvicorn --reload` here.** The `.venv` python is a uv trampoline that
  spawns the real interpreter; `--reload` logs "Reloading…" but never kills the old
  worker, so stale code keeps serving. Restart the process tree after backend edits.
  Seeing two python.exe per process (one `.venv`, one `AppData\Roaming\uv\python`) is normal.
- Local `config_hash` differs from the server's only because Windows checks the YAML
  config out with CRLF line endings and the hash is over raw bytes — same config.
- **The local store has gaps** (the collector only runs while someone runs it); the
  server's doesn't. When local and live disagree, check local 4h bar gaps first — six
  missing bars once flipped ZEC's regime. The price backfill now fills gaps on its own.
- Scratch files: use the session scratchpad, not `$TMPDIR` (empty in Git Bash here).

## Deploying (live: <https://tradesafe.srv1612559.hstgr.cloud>)

Hostinger VPS 1612559, Docker Compose project `tradesafe` (Traefik in front; the
standalone Caddy variant is `deploy/docker-compose.standalone.yml`).

1. Push to **both** `main` and `master` (Hostinger clones the public GitHub repo).
2. Read the current environment with `VPS_getProjectContentsV1` — it holds
   `TRADESAFE_PASSWORD`, `TRADESAFE_SECRET`, `COINALYZE_API_KEY` and `FRED_API_KEY`, which
   live only there.
3. `VPS_createNewProjectV1` with the **same** name `tradesafe`, `content` = the bare repo
   URL, and that **same** environment (leaving a value out changes the login or turns a
   feed off). This rebuilds in place and keeps the `tradesafe_data` volume.
4. Verify: poll the live `assets/index-*.js` for a string from the new code, check the
   containers, and log in once.

- `VPS_updateProjectV1` only restarts old images — it does not rebuild.
- Delete + create wipes the data volume. Don't.
- There is no remote shell: one-time setup has to run from application startup code.
- Claude Code's auto mode blocks the redeploy (it carries secrets) until the user
  explicitly says to redeploy — "push to prod" alone has been refused.

## Changing thresholds (`backend/config/thresholds.yaml`)

`meta.validated: false` — every value is a placeholder until measured.

- Never loosen a threshold to make a report pass. A failing gate is usually a data or
  config problem; check the data first (stale collector, missing source config, units).
- Do change a value that measurement shows is **structurally unreachable** (no market
  state satisfies it), and record the numbers and reasoning inline next to the value.
- Separate *structurally unreachable* (a defect) from *currently unmet* (correct
  fail-closed behaviour). Measure before concluding.

## Data sources and what they can't do

- Venues polled directly: Binance, Bybit, OKX, Hyperliquid (perps); Coinbase, Kraken,
  Binance/Bybit/OKX spot; CoinGecko (T2, cross-check only); chain RPC for supply.
- Collector cadence: 10 s for price and order book (Binance/Bybit/OKX), 60 s for
  everything else, 15 s for OKX liquidations, 60 s for Coinalyze. Reports never fetch
  live: they read the store. Rows older than 2 h are compacted to one per 15 min
  (liquidations and events never are).
- **Websockets don't deliver frames in this environment** (handshakes succeed, nothing
  arrives), so Binance/Bybit liquidation streams are useless here. Liquidations come
  from OKX's REST endpoint (direct, T1) and **Coinalyze** (Binance + Bybit per minute,
  T2). Coinalyze is free, 40 calls/min, one call per symbol, and keeps only ~1,500–2,000
  intraday points per series (1-min ≈ 1 day, 15-min ≈ 2–3 weeks, 1-hour ≈ 3 months,
  daily forever) — finer history only exists if we collect it as it happens.
- Backfills (Binance, free): 4h price closes for 35 days (fills gaps; never stores the
  still-forming bar), 30 days of 15-min open interest, and a one-off Coinalyze 15-min
  liquidation backfill so the 7-day liquidation baseline works on a fresh start.
- Binance's free archive (data.binance.vision) has futures order-book depth (±0.2–5%,
  ~30 s, since 2023) and OI/long-short metrics (since 2020), but no liquidation history.
- Judged not worth it: FreeCryptoAPI (resells the same exchanges, 3-hour-old derivatives
  data), CoinGlass (4-hour granularity on the $29 plan; fine data costs $299/mo).
- **Macro event calendar** (`backend/sources/calendar.py`, polled every 15 min, stored
  once under instrument `MACRO`): CPI, jobs report and PCE release dates from **FRED**
  (free key; past dates are the actual ones, next ~3 months scheduled), and FOMC
  decisions scraped from the Fed's calendar page (FRED's "FOMC Press Release" entry
  updates daily, so it can't give meeting dates; notation votes are skipped). Both give
  dates only: times are the agencies' fixed releases, 8:30 a.m. and 2:00 p.m. New York,
  converted with daylight saving (needs the `tzdata` package — Windows and the slim
  Docker image have no zone data). Only events that have happened are stored. The US
  CPI schedule site (bls.gov) blocks automated access; FRED is the way in.
- `python -m backend.research events` loads the same calendar into the research store
  (since 2023: 47 CPI, 47 jobs, 46 PCE, 31 FOMC).

## What the history has shown (research harness)

`python -m backend.research baseline BTC ETH SOL ZEC --step-hours 4` replays the regime
and setup code over ~3.7 years of free history (~3 min). Findings, 2026-09-19:

- Regime `undetermined` ~58–62% of hours; trending regimes only 1–3%.
- As a 7-day direction call the trend regime was **contrarian** on BTC/ETH/SOL (after
  `trending_up`, price was higher 7 days later only 35–37% of the time); ZEC was the
  exception. Don't build a "direction" headline on the regime without testing.
- Setups vs a random entry held as long: trend continuation 43% vs 52% (7 signals),
  downtrend continuation 44% vs 48%, cascade absorption 47% vs 52%, positioning
  exhaustion 53% vs 52% (no edge). Only squeeze absorption was ahead: 56% vs 48%, but
  with a negative average (large losses). Cascade/squeeze were scored without their
  liquidation check. No setup has shown an edge worth trading yet.
- Historically ~2 directional setups a week across 4 coins, ~12/month of positioning
  exhaustion (no direction).
- On an hourly grid (more, shorter episodes) squeeze absorption's lead narrows: 773
  signals, 52.5% vs 47.7%, mean −2.4%.
- **Event decompression** (hourly grid, 171 macro events since 2023, 5-day hold, scored
  as fading whoever was crowded going into the event): 32 signals (~0.7/month across 4
  coins), 59.4% vs 49.4% random, median +2.7% but mean +0.0% (large losses offset the
  typical win); BTC/ETH/ZEC ahead, SOL behind. Suggestive at this sample size, not
  proven — the live report still calls it "direction unclear". Its bottleneck is
  one-sided funding going into the event (1.1% of hours).

## Decisions made (and why)

- **Liquidation "settled"** = the last 60 min of liquidations ≤ 1.0× the median hour
  over the previous 7 days, on the same exchanges on both sides (exchanges with the full
  7 days and a print in the last day). The old rule, "no liquidation for 60 minutes",
  held in 2–10% of hours across Binance/Bybit/OKX and got rarer with each exchange
  added, so it measured coverage, not the market. Numbers are inline in thresholds.yaml.
- **Event decompression** reads the doctrine's "traded after the event resolves" as: the
  most recent scheduled event happened within `event.resolved_within_hours` (24; 6–24h
  is a plateau in the sweep, numbers inline in thresholds.yaml), and positioning as the
  funding of the 3 periods going INTO that event, not now. Before this, any event in
  the last 30 days counted, so with monthly releases it would have fired on funding alone.
- Perp/spot ratio ceiling 3.0 → 20.0 (measured 0% pass rate); funding dispersion made
  absolute; OI dispersion loosened (venues legitimately differ by size).
- Long/short labels: setups carry a Long-type / Short-type / Direction unclear tag;
  the verdict shows Long/Short only when every qualifying setup agrees *and* the trapped
  cohort agrees for cascade/squeeze. Cascade and squeeze share 3 of their 5 checks.
- UI colours: inside setup cards green/red mean long/short only (a met check's bar is
  green if it supports a long, red if against); unmet checks are grey. Trust-check cards
  keep green/red for pass/fail.
- **Alerts**: a scanner in the API process (not the collector, which holds no decision
  logic) runs every watched coin every `TRADESAFE_SCAN_INTERVAL_MINUTES` (default 15)
  and records one alert per episode; a failed trust gate doesn't end an episode. Delivery
  is browser-only while a tab is open (system notification, chime, banner). Not built:
  push/email/Telegram for a closed tab or phone.

## Open items

- `gate_u.reference_intended_size_coins: 1.0` is a placeholder for the real trade size;
  until set, the order-book depth check passes trivially.
- The research harness has no liquidation history, so cascade/squeeze haven't been
  back-tested with the new "settled" rule; Coinalyze hourly data covers ~3 months.
- Swing-timeframe direction signals (daily-chart trend, multi-day funding, OI trend,
  spot share of volume) need testing in the harness before any UI shows them.
- Layer 0's partial-snapshot rule is effectively off on Linux (no shared `collected_at`
  per fetch); fixing it changes live behaviour and needs a decision.
- `regime.find_swings` counts the still-forming 4h bar as a confirming bar.
