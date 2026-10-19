# Rosa Dashboard

> **Vibe-coded.** Built entirely with Claude out of frustration with the
> official MyToyota app (re-prompts for your password on every launch, no
> autofill) and the lack of developer support. I reviewed the code; I didn't
> write it. Use at your own risk.

A small self-hosted dashboard for a Toyota on the MyToyota (Europe) service.
It logs in once, polls Toyota in the background and keeps its own history, so
opening the page is instant and never asks for a password.

Built on [pytoyoda](https://github.com/pytoyoda/pytoyoda), the community
library behind the Home Assistant Toyota integration. Unofficial: Toyota can
change their API at any time.

## What it shows

### Dashboard (`/`)
- Car picture (Toyota's render or your own photo in `images/`), odometer, fuel level and range
- Any warning (open door, low fuel, warning light) as a message in the top card
- Doors, windows, bonnet and boot (closed / locked)
- Where the car was last parked, on a map
- This week's and this month's distance, consumption, hybrid score and EV share
- Remote control (opt-in): lock/unlock, boot, hazards, horn, climate, close windows

### Trips (`/trips`)
- Every trip, paged, saved locally so history outlives Toyota's 12 months
- A map of every road driven; click a trip to see its own route

### Statistics (`/stats`)
- Litres of petrol and distance per week or month, with charts and a table

### Service & Maintenance (`/service`)
- Last and next service (next is worked out from an interval you can change)
- Toyota's service history, kept for good
- Tyre rotation history you add, edit and delete yourself; due after 5,000 miles or 6 months

Every page has Refresh and Backup (one JSON file of everything stored) buttons.
Missing data shows "–": what's available depends on your car and Toyota.

## How to run it

```bash
cp .env.example .env      # put your MyToyota email + password in here
docker compose up -d --build
```

Open http://localhost:8080 (or `http://<server-ip>:8080` from your phone). Change the
port with `PORT` in `.env`.

The first fetch takes 10–20 seconds; the last snapshot is kept in a volume, so
restarts show data straight away. Logs go to `./logs`. Keep it on your LAN or behind a
VPN; if you expose it, set `DASHBOARD_PASSWORD` and use HTTPS.

Remote control is off by default. To enable it set `REMOTE_CONTROL=true` and a
`DASHBOARD_PASSWORD` of 12+ characters; buttons must be held for 3 seconds.

## Behind the scenes

The app talks to Toyota's MyToyota Europe servers through pytoyoda, with the
account in `.env`.

**How it uses pytoyoda.** One `MyT` client lives for the whole process. It logs in
once (username + password, OAuth against Toyota's login service), caches the token
and refreshes it when needed. Then each poll is roughly:

```python
await client.login()                       # no-op while the token is valid
cars = await client.get_vehicles()         # vehicle list
await car.update()                         # status, location, health, service history...
await car.get_current_week_summary()       # week totals
await car.get_current_month_summary()      # month totals
```

Trips are fetched separately (`/v1/trips`), because pytoyoda's built-in trip history
is broken in 6.0.0.

**Toyota endpoints.** All on `ctpa-oneapi.tceu-ctp-prd.toyotaconnectedeurope.io`,
with the OAuth bearer token and the vehicle's VIN in the headers:

| Endpoint | Used for |
| -------- | -------- |
| `GET /v1/vehicle-association/vehicle` | Your vehicles (VIN, model, colour, picture, supported remote commands) |
| `GET /v1/vehicle/status` | Doors, windows, bonnet and boot (open / locked) |
| `GET /v3/telemetry` | Odometer, fuel level, range |
| `GET /v1/vehiclehealth/status` | Warning lights |
| `GET /v1/location` | Where the car was last parked |
| `GET /v1/servicehistory/vehicle/summary` | Service records |
| `GET /v1/trips?from&to&summary&limit&offset` | Trip list and week/month summaries |
| `GET /v1/trips/{id}` | One trip's GPS route |
| `POST /v1/global/remote/command` | Lock, unlock, boot, hazards, horn, close windows |
| `POST /v2/remote/climate-control` | Climate start / stop |

Login goes to `b2c-login.toyota-europe.com`. pytoyoda also reads electric, climate and
notification endpoints during `update()`; this app doesn't show them.

**Cadence.** Every `POLL_MINUTES` (default 30, minimum 10), or when you press Refresh.
New trips are requested until a page contains one already stored; each new trip's route
is then fetched with one request, in batches of 20.

**Being polite.** Requests are sent one at a time with a gap between them, because
Toyota answers bursts with HTTP 429. If Toyota fails or refuses, the last good data
stays on screen. Trips and routes are stored locally, so only new ones are requested.

**Sending commands.** Remote control posts to Toyota's command endpoint (and the
climate endpoint for climate start/stop). Only a fixed list of commands is
allowed, and only those your car reports as supported. Each is sent exactly once,
with no automatic retry, so a flaky connection can't repeat it. If Toyota gives no
clean answer the page says the car *may* have done it, and a refresh follows a
minute later.

Never sent: engine start, headlights, window-open.
