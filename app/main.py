"""Rosa Dash: a small self-hosted dashboard for a Toyota on MyToyota (EU)."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse
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
STATIC = Path(__file__).parent / "static"

collector = Collector()


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(collector.run_forever())
    yield
    task.cancel()
    await collector.close()


app = FastAPI(title="Rosa Dash", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
security = HTTPBasic(auto_error=False)


def guard(creds: HTTPBasicCredentials | None = Depends(security)) -> None:
    """Optional HTTP basic auth: on only when DASHBOARD_PASSWORD is set."""
    if not DASHBOARD_PASSWORD:
        return
    if creds is None or not secrets.compare_digest(creds.password.encode(), DASHBOARD_PASSWORD.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": 'Basic realm="rosa"'},
        )


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
    return await collector.refresh()


# -- remote control ---------------------------------------------------------------------------
class CommandRequest(BaseModel):
    command: str = Field(max_length=40)


@app.post("/api/command", dependencies=[Depends(guard), Depends(same_site_write)])
async def command(body: CommandRequest, request: Request, x_rosa_hold: str | None = Header(default=None)) -> dict:
    if not collector.remote_enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Remote control is turned off (REMOTE_CONTROL=true enables it)")
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
