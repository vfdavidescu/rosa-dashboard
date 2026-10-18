"""Rosa Dash: a small self-hosted dashboard for a Toyota on MyToyota (EU)."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import logsetup
from app.collector import CommandCooldown, Collector

# Console + rotating file in LOG_DIR; also caps pytoyoda's logging, which at
# DEBUG would write your password and tokens (see app/logsetup.py).
logsetup.setup()

MANUAL_REFRESH_COOLDOWN_S = int(os.environ.get("REFRESH_COOLDOWN_SECONDS", "300"))
COMMAND_COOLDOWN_S = int(os.environ.get("COMMAND_COOLDOWN_SECONDS", "15"))
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
MIN_REMOTE_PASSWORD = 12  # remote control is refused below this length
PASSWORD_OK = len(DASHBOARD_PASSWORD) >= MIN_REMOTE_PASSWORD
STATIC = Path(__file__).parent / "static"

collector = Collector()
BACKGROUND: set[asyncio.Task] = set()  # keeps fire-and-forget tasks from being garbage-collected


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(collector.run_forever())
    yield
    task.cancel()
    await collector.close()


app = FastAPI(title="Rosa Dash", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.middleware("http")
async def revalidate(request: Request, call_next):
    """Pages and scripts must always match: without this, browsers guess a cache
    lifetime and can pair a new page with an old common.js."""
    response = await call_next(request)
    response.headers.setdefault("Cache-Control", "no-cache")
    # Stop other sites embedding the dashboard (a hidden "press and hold" trick).
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response


app.mount("/static", StaticFiles(directory=STATIC), name="static")
security = HTTPBasic(auto_error=False)


_guess_lock = asyncio.Lock()
_guesses_waiting = 0
MAX_WAITING_GUESSES = 20


async def guard(creds: HTTPBasicCredentials | None = Depends(security)) -> None:
    """Optional HTTP basic auth: on only when DASHBOARD_PASSWORD is set.

    The right password is accepted at once. Wrong ones are handled one per second
    for everyone together (Docker hides each client's address, so this can't be
    per-client), which makes guessing hopeless while never locking the owner out.
    """
    global _guesses_waiting
    if not DASHBOARD_PASSWORD:
        return
    if creds is not None and secrets.compare_digest(creds.password.encode(), DASHBOARD_PASSWORD.encode()):
        return
    headers = {"WWW-Authenticate": 'Basic realm="rosa"'}
    if creds is None:
        # The browser's first request, before it asks for a password: not a guess.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, headers=headers)
    if _guesses_waiting >= MAX_WAITING_GUESSES:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts", headers={"Retry-After": "30"})
    _guesses_waiting += 1
    try:
        async with _guess_lock:
            await asyncio.sleep(1)
    finally:
        _guesses_waiting -= 1
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, headers=headers)


def same_site_write(x_rosa: str | None = Header(default=None)) -> None:
    """Every request that changes something must carry an `X-Rosa: 1` header.

    Our own pages always send it. A web page on another site can't add a custom
    header to a request to this server without the browser asking this server
    first (a CORS preflight), which we never approve, so a malicious site can't
    make your browser unlock the car or delete records behind your back.
    """
    if x_rosa != "1":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing X-Rosa header")


# -- pages --------------------------------------------------------------------------------
@app.get("/", dependencies=[Depends(guard)])
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/service", dependencies=[Depends(guard)])
async def service_page() -> FileResponse:
    return FileResponse(STATIC / "service.html")


@app.get("/trips", dependencies=[Depends(guard)])
async def trips_page() -> FileResponse:
    return FileResponse(STATIC / "trips.html")


@app.get("/stats", dependencies=[Depends(guard)])
async def stats_page() -> FileResponse:
    return FileResponse(STATIC / "stats.html")


# -- live data ------------------------------------------------------------------------------
@app.get("/api/state", dependencies=[Depends(guard)])
async def state() -> dict:
    return {
        **collector.snapshot,
        "poll_minutes": collector.poll_minutes,
        "mock": collector.mock,
        "refreshing": collector._lock.locked(),
        "car_image": f"api/car-image?v={v}" if (v := collector.images.version()) else None,
        "car_image_custom": collector.images.is_custom(),
        # Toyota calls every Corolla estate/hatch "Corolla HB/TS"; let the owner say which.
        "car_model_name": os.environ.get("CAR_MODEL_NAME", "").strip() or None,
        "remote": {
            "enabled": collector.remote_enabled,
            "password_set": bool(DASHBOARD_PASSWORD),
            "password_ok": PASSWORD_OK,
            "min_password": MIN_REMOTE_PASSWORD,
            "last_command": collector.last_command,
            "cooldown_seconds": COMMAND_COOLDOWN_S,
        },
    }


@app.get("/api/car-image", dependencies=[Depends(guard)])
async def car_image() -> FileResponse:
    current = collector.images.current()
    if current is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No car image yet")
    path, media_type = current
    # The URL carries a version, so browsers can keep it for a long time.
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, max-age=604800"})


@app.post("/api/refresh", dependencies=[Depends(guard), Depends(same_site_write)])
async def refresh() -> dict:
    last = collector.last_attempt
    if last is not None:
        wait = MANUAL_REFRESH_COOLDOWN_S - (datetime.now(timezone.utc) - last).total_seconds()
        if wait > 0:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, f"Try again in {int(wait)}s")
    snapshot = await collector.refresh()
    # Routes for any new trips, without waiting for the next scheduled refresh.
    task = asyncio.create_task(collector.backfill_routes())
    BACKGROUND.add(task)
    task.add_done_callback(BACKGROUND.discard)
    return snapshot


@app.get("/api/backup", dependencies=[Depends(guard)])
async def backup() -> JSONResponse:
    data = await asyncio.to_thread(collector.backup)
    name = f"rosa-backup-{date.today().isoformat()}.json"
    return JSONResponse(data, headers={"Content-Disposition": f'attachment; filename="{name}"'})


# -- remote control ---------------------------------------------------------------------------
class CommandRequest(BaseModel):
    command: str = Field(max_length=40)


@app.post("/api/command", dependencies=[Depends(guard), Depends(same_site_write)])
async def command(body: CommandRequest, request: Request, x_rosa_hold: str | None = Header(default=None)) -> dict:
    if not collector.remote_enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Remote control is turned off (REMOTE_CONTROL=true enables it)")
    if not PASSWORD_OK:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Remote control needs a DASHBOARD_PASSWORD of at least {MIN_REMOTE_PASSWORD} characters",
        )
    # The page only sends this after the button has been held for 3 seconds.
    if x_rosa_hold != "3s":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Hold the button to confirm")
    try:
        entry = await collector.run_command(
            body.command,
            client_ip=request.client.host if request.client else None,
            min_gap=COMMAND_COOLDOWN_S,
        )
    except CommandCooldown as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return entry


# -- trips ----------------------------------------------------------------------------------------
@app.get("/api/trips", dependencies=[Depends(guard)])
async def trips(page: int = Query(1, ge=1), per_page: int = Query(20, ge=1, le=100)) -> dict:
    metric = collector.use_metric
    return {
        **collector.trips.page(page, per_page, metric),
        "units": {"distance": "km" if metric else "mi", "consumption": "L/100km" if metric else "mpg"},
        "mock": collector.mock,
    }


@app.get("/api/stats", dependencies=[Depends(guard)])
async def stats(period: str = Query("week", pattern="^(week|month)$")) -> dict:
    metric = collector.use_metric
    return {
        **collector.trips.stats(period, metric),
        "units": {"distance": "km" if metric else "mi", "consumption": "L/100km" if metric else "mpg"},
        "mock": collector.mock,
    }


@app.get("/api/trips/routes", dependencies=[Depends(guard)])
async def trip_routes() -> dict:
    # Simplifying every route takes a few seconds the first time; keep the server responsive.
    return {"routes": await asyncio.to_thread(collector.trips.overview)}


@app.get("/api/trips/{trip_id}/route", dependencies=[Depends(guard)])
async def trip_route(trip_id: str) -> dict:
    points = collector.trips.route(trip_id)
    if points is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No route for this trip yet")
    return {"points": points}


# -- service & maintenance ----------------------------------------------------------------------
def _current_odometer() -> dict | None:
    vehicles = collector.snapshot.get("vehicles") or []
    if not vehicles:
        return None
    v = vehicles[0]
    value = (v.get("dashboard") or {}).get("odometer")
    return {"value": value, "unit": (v.get("units") or {}).get("distance", "mi")} if value is not None else None


@app.get("/api/maintenance", dependencies=[Depends(guard)])
async def maintenance() -> dict:
    vehicles = collector.snapshot.get("vehicles") or []
    return {
        **collector.maintenance.view(),
        "odometer": _current_odometer(),
        "units": (vehicles[0].get("units") if vehicles else None) or {"distance": "km" if collector.use_metric else "mi"},
        "alias": vehicles[0].get("alias") if vehicles else None,
        "today": date.today().isoformat(),
        "mock": collector.mock,
    }


class RotationRequest(BaseModel):
    date: date
    odometer: float = Field(ge=0, le=2_000_000)
    unit: str = Field(pattern="^(mi|km)$")
    notes: str = Field(default="", max_length=500)


@app.post("/api/maintenance/tyre-rotations", dependencies=[Depends(guard), Depends(same_site_write)])
async def add_rotation(body: RotationRequest) -> dict:
    if body.date > date.today():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That date is in the future")
    return collector.maintenance.add_rotation(on=body.date, odometer=body.odometer, unit=body.unit, notes=body.notes)


@app.delete("/api/maintenance/tyre-rotations/{rid}", dependencies=[Depends(guard), Depends(same_site_write)])
async def delete_rotation(rid: str) -> dict:
    if not collector.maintenance.delete_rotation(rid):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such entry")
    return {"ok": True}


class SettingsRequest(BaseModel):
    rotation_interval: float = Field(gt=0, le=100_000)
    rotation_interval_unit: str = Field(pattern="^(mi|km)$")
    rotation_months: int = Field(gt=0, le=60)


@app.put("/api/maintenance/settings", dependencies=[Depends(guard), Depends(same_site_write)])
async def update_settings(body: SettingsRequest) -> dict:
    return collector.maintenance.update_settings(**body.model_dump())


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}
