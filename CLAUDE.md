# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Rosa Dash: a small self-hosted FastAPI dashboard for a single Toyota, backed by
[pytoyoda](https://github.com/pytoyoda/pytoyoda) (the community library behind the
Home Assistant Toyota integration, unofficial — Toyota can change their API at any
time). It logs into MyToyota (Europe) once, polls in the background, and serves the
last-known snapshot so the page never blocks on a live API call.

**Start here:** read `docs/HANDOFF.md` for how the project got here, the open
issue (a failed remote Unlock) and ideas not yet built.

## Commands

```bash
# Run without Docker
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
set -a; source .env; set +a
uvicorn app.main:app --port 8080

# UI work without calling the real Toyota API
MOCK=true uvicorn app.main:app --reload --port 8080

# Run via Docker
cp .env.example .env      # fill in TOYOTA_USERNAME / TOYOTA_PASSWORD
docker compose up -d --build
```

There is no test suite, linter, or build step configured in this repo.

## Architecture

- `app/main.py` — FastAPI app. Defines the lifespan hook that starts
  `Collector.run_forever()` as a background task on startup, optional HTTP Basic
  auth (`guard`, only enforced when `DASHBOARD_PASSWORD` is set), the pages
  (`/`, `/service`, `/static/*`) and the JSON API: `/api/state`, `/api/car-image`,
  `POST /api/refresh`, `POST /api/command`, `/api/trips*`, `/api/stats`, `/api/backup` and `/api/maintenance/*`.
  A middleware sets `Cache-Control: no-cache` on everything that doesn't set its
  own, so browsers never pair a new page with a stale `common.js`.
  **Every POST/PUT/DELETE must depend on `same_site_write`** (requires an
  `X-Rosa: 1` header, a CSRF guard); keep that on any new write route.
- `app/remote.py` — the fixed allow-list of remote commands (`COMMANDS`), each
  tied to a pytoyoda `extended_capabilities` flag. Never send anything not in
  this list. Remote control is opt-in via `REMOTE_CONTROL=true` and refused
  unless `DASHBOARD_PASSWORD` is set and 12+ characters (`PASSWORD_OK`); the server also requires `X-Rosa-Hold: 3s`
  (sent by the page after a 3 s button hold) and enforces
  `COMMAND_COOLDOWN_SECONDS` inside `Collector._command_lock`. Commands go
  through `remote._post_once()`, not pytoyoda's request path, because pytoyoda
  retries 429/5xx and would repeat a command. Commands also hold `Collector._lock`
  (one Toyota call at a time), and `remote.OutcomeUnknown` marks "may have
  reached the car" (dropped connection, 5xx): logged as `unknown`, followed by a refresh.
  `guard` answers wrong passwords one per second globally (Docker hides client IPs).
- `app/maintenance.py` — `MaintenanceStore`, a JSON file in `DATA_DIR`
  (`maintenance.json`): Toyota service records merged by id (never deleted),
  user-logged tyre rotations (add/edit/delete), the service interval, and the fixed tyre
  rule `TYRE_RULE` (5,000 mi or 6 months, shown as "Due", never changeable). Toyota gives no
  next-service date, so the Service page works it out from the last service record
  (not the `VDE` delivery one) plus the `service_*` settings (default 12 months / 10,000 mi). Distances are stored
  with their own unit and converted only for display.
- `app/trips.py` — `TripStore`: every trip Toyota has reported (it keeps ~12
  months), in `DATA_DIR/trips.json` (km, L/100km; converted per request) plus
  full GPS routes in `DATA_DIR/routes/<id>.json`. Routes cost one API call each:
  the collector stores new trips on each refresh, then `backfill_routes()` fetches
  missing routes in batches of 20, holding the refresh lock so calls never overlap.
  The all-roads map uses RDP-simplified routes, cached in memory. `stats()`
  totals trips per week/month for the Statistics page (litres = km × L/100km ÷ 100).
- `app/images.py` — the car picture: user photo in `CAR_IMAGE_DIR` wins,
  otherwise Toyota's render downloaded once into `DATA_DIR`.
- `app/collector.py` — all Toyota I/O and the one `Collector` class that owns the
  pytoyoda `MyT` client, the latest in-memory `snapshot`, and the poll loop. Key
  constraints baked into its design (see its module docstring):
  - One `MyT` client instance lives for the whole process so pytoyoda's cached
    token/refresh flow means login happens once, not every poll.
  - Per-vehicle API calls (week/month summaries, trip pages, route details) are made
    **sequentially with a 1s sleep between them**, never gathered concurrently —
    Toyota's gateway returns HTTP 429 on bursts. Preserve this when touching
    `vehicle_snapshot()`.
  - Every field read off a pytoyoda model goes through the `_get()` helper, which
    swallows exceptions and returns a default. Availability varies by car
    model/year, so a missing field should blank one dashboard tile, not raise.
  - On refresh failure, the previous good `snapshot` is kept and merged with
    `error`/`stale` flags rather than being discarded — the UI always has
    something to show.
  - The snapshot is persisted to `{DATA_DIR}/snapshot.json` (atomic tmp-file
    replace) and reloaded on startup so a restart shows data immediately instead
    of a blank page.
- `app/mock.py` — `mock_snapshot()` produces fake data in **exactly the same
  shape** as `vehicle_snapshot()`'s return value. When changing the real
  snapshot's shape, update this too, or `MOCK=true` mode will desync from reality.
- `app/static/` — the frontend, vanilla JS, no build step: `index.html`
  (dashboard + remote control, polls `/api/state`), `trips.html` (all trips,
  paged, with route maps), `stats.html` (weekly/monthly petrol and distance,
  hand-built SVG charts), `service.html` (Service & Maintenance), `app.css` (shared styles, light/dark tokens) and `common.js`
  (theme toggle, formatting, unit conversion, `api()` helper that adds `X-Rosa`,
  `lastSeen` per-tab cache for instant page switches, and `setupHeader()` /
  `syncUpdated()` for the Backup + Refresh buttons and "Updated … ago" line that
  every page header shares).
- Other files written to `DATA_DIR` at runtime: `snapshot.json`,
  `maintenance.json`, `trips.json`, `routes/`, `command-log.json` (audit log of remote commands),
  `car-image.*`, and pytoyoda's HTTP cache in `.cache/` (the container's working
  directory is `/data` for that reason).

## Conventions specific to this repo

- Snapshot shape is the contract between `collector.py`, `mock.py`, and
  `index.html`. Changes to one of the nested dicts (`dashboard`, `location`,
  `locks`, `summaries`, `model`, `remote_commands`) need
  matching changes in the other two places. `_service_records` is popped by the
  collector before saving; it never reaches the page.
- Units: `USE_METRIC` picks the primary unit (km/L-100km vs mi/mpg) and pytoyoda
  converts snapshot values server-side. The frontend then shows the *other* unit
  in grey via `both()`/`bigDual()`/`inlineDual()` in `common.js`, and converts
  maintenance distances (stored with their own unit) for display.
- pytoyoda 6.0.0 converts to "UK mpg" with the US-gallon factor (235.215);
  `collector._uk_mpg()` corrects it. Use 282.481 for UK mpg everywhere.
- Never run pytoyoda in a one-off script without `loguru.logger.remove()` first:
  its default DEBUG output prints the login password and tokens.
- pytoyoda 6.0.0's built-in `trip_history` endpoint is broken (passes `route=`),
  so `vehicle.update(skip=["trip_history"])` is deliberate.
- Logging is set up in `app/logsetup.py` (called at the top of `main.py`): console
  plus rotating `LOG_DIR/rosa.log` and `LOG_DIR/pytoyoda.log`. In Docker `LOG_DIR`
  is `/logs`, bind-mounted to `./logs` in the repo, so the logs can be read straight
  from the project folder when debugging.
- pytoyoda logs full login bodies (password, tokens) at DEBUG; `logsetup` caps its
  loguru output at `TOYOTA_LOG_LEVEL` (default WARNING). Don't lower that by default.
- Config is entirely env vars (see `.env.example`), read directly via `os.environ`
  in `main.py` and `Collector.__init__`; there's no settings/config module.
