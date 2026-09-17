# Deploying TradeSafe

Three containers: an always-on **collector** (polls venues, only ever writes), an
on-demand **api** (reads the store, runs the doctrine's gates, never talks to a venue),
and **web** (Caddy: serves the built frontend and reverse-proxies `/api/*` + `/health` to
the api container).

There are two deployment tracks, depending on what's already running on the box:

- **Track A — behind an existing Traefik reverse proxy** (the root `docker-compose.yml`):
  for a VPS that already runs Traefik in front of other projects, e.g. Hostinger's
  "Ubuntu 24.04 with Docker and Traefik" template. `web` has no Caddy-managed TLS and
  binds no host port; Traefik terminates TLS and routes to it purely off the
  `traefik.*` labels.
- **Track B — standalone box** (`deploy/docker-compose.standalone.yml`): for a plain VPS
  with nothing else on it. Caddy provisions its own TLS certificate and binds 80/443
  directly.

## 1. Provision a small VPS

Any small Linux box works (this is a personal, low-traffic tool — the cheapest tier of
DigitalOcean, Hetzner, Linode, or similar is plenty, and Track A doesn't even need a new
box if you already have one running Traefik). You need:

- A public IPv4 address
- Docker + the Docker Compose plugin installed (`curl -fsSL https://get.docker.com | sh`
  covers most distros)
- Track B only: a domain (or subdomain) with an A record pointed at the VPS's IP, if you
  want HTTPS — see step 4 for the localhost-only alternative. Track A doesn't need this
  configured separately — it uses whatever domain Traefik is already fronting.

**Check egress before you commit to a region.** Several exchanges restrict their futures
endpoints by source IP, including blocking some US-based addresses (doctrine
appendix; implementation spec, "Host requirements"). From the VPS itself, before
deploying:

```
curl -s -o /dev/null -w "%{http_code}\n" https://fapi.binance.com/fapi/v1/ping
curl -s -o /dev/null -w "%{http_code}\n" https://api.bybit.com/v5/market/time
curl -s -o /dev/null -w "%{http_code}\n" https://www.okx.com/api/v5/public/time
curl -s -o /dev/null -w "%{http_code}\n" https://api.hyperliquid.xyz/info
```

All four should return `200`. If one doesn't, pick a different region/provider before
going further — the collector will otherwise run indefinitely with a permanent gap for
that venue and you won't notice until a report's data-integrity section looks thin.

**Verify NTP sync.** The doctrine's half-life logic keys off the gap between
`observed_at` and `collected_at`; clock drift corrupts expiry and sync-tolerance checks
silently (implementation spec, "Host requirements"). Most cloud images ship with
`systemd-timesyncd` or `chrony` enabled by default — confirm with `timedatectl` (look for
`System clock synchronized: yes`).

## 2. Configure

```
git clone <your fork/remote of this repo>
cd TradeSafe
```

### Track A — behind Traefik

Create a `.env` next to the root `docker-compose.yml`:

```
TRADESAFE_PASSWORD=change-me-to-something-long-and-random
TRADESAFE_SECRET=change-me-too   # python3 -c "import secrets; print(secrets.token_hex(32))"
```

No `DOMAIN` variable here — edit the `Host(...)` rule directly in the `web` service's
`traefik.http.routers.tradesafe.rule` label in `docker-compose.yml` instead, since Caddy
is no longer the one provisioning TLS for it.

Also requires, on the Traefik side:

- Traefik started with `--providers.docker.exposedbydefault=false`, entrypoints named
  `web`/`websecure`, and a certificate resolver named `letsencrypt` (matching the labels
  in `docker-compose.yml`) — adjust the label names if your Traefik setup differs.
- An external Docker network named `traefik-proxy` that both Traefik and this stack join
  (create it once with `docker network create traefik-proxy` if it doesn't already
  exist).

### Track B — standalone

```
cp deploy/.env.example deploy/.env
```

Edit `deploy/.env`:

- `DOMAIN` — the domain pointed at this VPS. Leave as `localhost` only if you're testing
  without a domain (see step 4 — you lose HTTPS and the session cookie won't be sent).
- `TRADESAFE_PASSWORD` — the single access password for the whole app. This is a
  personal, single-user tool (no accounts), so make it long; there's no login rate
  limiting yet.
- `TRADESAFE_SECRET` — signs the session cookie. Generate one with:
  `python3 -c "import secrets; print(secrets.token_hex(32))"`

Never commit `deploy/.env`.

## 3. Bring it up

Track A:

```
docker compose up -d --build
```

Track B:

```
docker compose -f deploy/docker-compose.standalone.yml up -d --build
```

Watch the collector's logs the first time — it should start writing observations within
a minute or two (add `-f deploy/docker-compose.standalone.yml` for Track B):

```
docker compose logs -f collector
```

Then check the API is reachable:

```
curl https://<your-domain>/health
```

## 4. Local / no-domain testing (Track B only)

Set `DOMAIN=localhost` in `deploy/.env`, then
`docker compose -f deploy/docker-compose.standalone.yml up -d --build` and open
`http://localhost`. The login cookie is `Secure`-flagged by default, which plain HTTP
won't accept — for this case only, also set `TRADESAFE_INSECURE_COOKIE=true` in
`deploy/.env` and restart the `api` service. Never set that on a real deployment.

Track A is always served over real HTTPS via Traefik/Let's Encrypt, so this doesn't
apply there.

## 5. Building up history before trusting a threshold

Every threshold in `backend/config/thresholds.yaml` starts as a documented placeholder
(`meta.validated: false`) — the doctrine's own validation protocol says no capital should
move on this tool's output before it's been measured against real history. Once the
collector has been running for a while against an instrument you care about:

```
docker compose exec api python -m backend.scripts.calibrate ZEC cascade.flush_oi_pct 0.08 0.10 0.12 0.15 0.20
```

This sweeps a threshold across everything stored so far and prints, per value, the
Layer 0 pass rate, how many setups fired per month, and — split into separate columns,
never blended — how many refusals for that setup would have been protective vs. costly.
Pick a value that sits in a stable plateau across that table, not a sharp one-off peak,
update `thresholds.yaml`, and flip `meta.validated: true` once you've done this for the
thresholds you actually rely on.

## 6. Day-to-day operations

- **Updating**: `git pull && docker compose up -d --build` (add
  `-f deploy/docker-compose.standalone.yml` for Track B).
- **Backups**: the whole store is one SQLite file inside the `tradesafe_data` Docker
  volume (`records.db`). `docker compose exec api sqlite3 /data/records.db ".backup
  /data/backup.db"` and copy that off the box periodically — it's small (implementation
  spec, "Storage is trivial").
- **Adding an instrument to ongoing collection**: happens automatically the first time
  you look it up in the app (`POST /api/instruments/{symbol}`, which the frontend calls
  for you) — from then on the collector polls it every cycle so history accumulates for
  calibration later.
- **No exchange credentials ever live on this box.** Every required source is a public
  endpoint (implementation spec) — there is nothing here worth stealing beyond the
  access password.
