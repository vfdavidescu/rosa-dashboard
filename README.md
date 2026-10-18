# Rosa Dash

> **Vibe-coded.** Built entirely with Claude out of frustration with the
> official MyToyota app (re-prompts for your password on every launch, no
> autofill) and the lack of developer support. I reviewed the code; I didn't
> write it. Use at your own risk.

A small self-hosted dashboard for a Toyota on the MyToyota (Europe) service.
It logs in once, keeps the token, and polls in the background, so opening the
page never asks for a password.

Built on [pytoyoda](https://github.com/pytoyoda/pytoyoda), the community
library behind the Home Assistant Toyota integration. Unofficial: Toyota can
change their API at any time.

## What it shows

- A picture of the car (Toyota's render, or your own photo, see `images/`)
- Fuel level and estimated range
- Doors, windows, bonnet and boot (closed / locked)
- Where the car was last parked (OpenStreetMap)
- This week's and this month's distance, consumption, hybrid score, EV share
- The last 10 trips
- **Trips** page (`/trips`): every trip, paged, saved locally so history
  outlives Toyota's 12 months. A map of every road driven; click a trip for its
  route. Routes download gradually (one API call each) after the first start.
- **Statistics** page (`/stats`): litres of petrol and distance per week or
  month, with charts and a table of every period.
- Odometer, last service, warning lights
- **Remote control** (opt-in): lock/unlock, boot, hazards, horn, climate,
  close windows, but only the ones your car reports it supports
- **Service & Maintenance** page (`/service`): Toyota's service history saved
  locally for good, plus tyre rotations you log yourself, with a "next due"
  tracker

What's actually populated depends on the car and Toyota's backend; missing
fields just show "–".

## Run it

```bash
cp .env.example .env      # put your MyToyota email + password in here
docker compose up -d --build
```

Open http://localhost:8080 (or http://<server-ip>:8080 from your phone).

The first fetch happens on start-up and takes 10–20 seconds. The last good
snapshot is saved in a volume, so restarts show data straight away.

### Without Docker

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
set -a; source .env; set +a
uvicorn app.main:app --port 8080
```

### UI work without touching Toyota

```bash
MOCK=true uvicorn app.main:app --reload --port 8080
```

## Remote control

Off by default, because it can unlock the car. To turn it on, set
`REMOTE_CONTROL=true` in `.env`, and set `DASHBOARD_PASSWORD` too.

Safety measures:

- Commands are refused unless `DASHBOARD_PASSWORD` is set.
- Each command is sent to Toyota exactly once (pytoyoda's retries are bypassed).
- Each button has to be **held for 3 seconds**; letting go early sends nothing.
- Only commands your car's own capability flags allow are shown or accepted.
  Engine start, headlights and window-open are never sent.
- One command per `COMMAND_COOLDOWN_SECONDS` (default 15), enforced on the server.
- Write requests need an `X-Rosa` header, which other websites can't add, so a
  malicious page can't trigger commands through your browser.
- Every command is logged to `command-log.json` in the data volume (time,
  command, result, IP).

About a minute after a command, the dashboard refreshes so you can see the result.

## Service & Maintenance

Stored in `maintenance.json` in the data volume:

- **Service history**: every record Toyota has returned, merged on each refresh.
  Records stay even if Toyota later drops them.
- **Tyre rotations**: logged by you on the page (date, odometer, notes).
  "Next due" uses whichever comes first: distance (default 5,000 mi) or time
  (default 6 months), both editable on the page.

Back it up with:

```bash
docker compose cp rosa-dash:/data/maintenance.json ./maintenance-backup.json
```

## Logs

Written to `./logs` next to this README (and to `docker compose logs`):

- `rosa.log`: the app (refreshes, commands and their results, errors)
- `pytoyoda.log`: warnings and errors from the Toyota library

Both rotate at 5 MB and keep 5 old files. `LOG_LEVEL` (default `INFO`) sets
the app's detail. `TOYOTA_LOG_LEVEL` (default `WARNING`) sets pytoyoda's; at
`DEBUG` it writes your password and tokens, so only use that briefly.

## Phone

In Safari, Share → Add to Home Screen. It opens full-screen like an app.

## API

| Method | Path           | What                                              |
| ------ | -------------- | ------------------------------------------------- |
| GET    | `/api/state`   | Latest snapshot as JSON                           |
| GET    | `/api/car-image` | The car picture (your photo if present, else Toyota's) |
| POST   | `/api/refresh` | Fetch now (rate-limited by `REFRESH_COOLDOWN_SECONDS`) |
| POST   | `/api/command` | Remote command (needs `REMOTE_CONTROL=true`, `X-Rosa: 1` and `X-Rosa-Hold: 3s`) |
| GET    | `/api/trips?page=&per_page=` | Stored trips, newest first |
| GET    | `/api/trips/routes` | Every stored route, simplified |
| GET    | `/api/stats?period=week\|month` | Petrol and distance totals per period |
| GET    | `/api/trips/{id}/route` | One trip's full route |
| GET    | `/api/maintenance` | Service history, tyre rotations, interval |
| POST   | `/api/maintenance/tyre-rotations` | Log a rotation |
| DELETE | `/api/maintenance/tyre-rotations/{id}` | Remove one |
| PUT    | `/api/maintenance/settings` | Rotation interval |
| GET    | `/healthz`     | Liveness                                          |

`/api/state` is handy for other things too (e.g. feeding odometer and fuel into
another project). All POST/PUT/DELETE calls need an `X-Rosa: 1` header.

## Being a good API citizen

- Requests to Toyota are made one at a time with small gaps; bursts get HTTP 429.
- Default poll is every 30 minutes, minimum 10.
- Keep this on your LAN or behind Tailscale/WireGuard. If you do expose it,
  set `DASHBOARD_PASSWORD` and put it behind HTTPS.

## Contributing

Commits follow [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/)
(`feat:`, `fix:`, `docs:`, etc).

## Layout

```
app/
  main.py         FastAPI app, background poller, optional basic auth
  collector.py    pytoyoda calls → one JSON snapshot
  mock.py         sample data with the same shape
  images.py       car picture: download/cache Toyota's, or use your own
  remote.py       which remote commands exist, capability checks, sending
  maintenance.py  maintenance.json: service history, tyre rotations
  trips.py        trips.json + routes/: trip history and GPS routes
  static/
    index.html    dashboard page
    trips.html    Trips page
    stats.html    Statistics page
    service.html  Service & Maintenance page
    app.css       shared styles
    common.js     shared JS: theme, formatting, units, API helper
```
