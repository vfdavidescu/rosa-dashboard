"""Pulls data from Toyota Connected Services (EU) via pytoyoda and turns it
into one plain JSON-able snapshot for the dashboard.

Design notes
- One MyT client lives for the whole process, so pytoyoda's token cache and
  refresh-token flow mean we log in once, not on every poll.
- Calls are made one after another (never gathered) because Toyota's gateway
  answers bursts with HTTP 429.
- Every field is read defensively: what's available depends on model/year,
  and a missing field should blank one tile, not break the page.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from app import remote
from app.images import CarImage
from app.maintenance import MaintenanceStore
from app.trips import TripStore

log = logging.getLogger("rosa.collector")

TRIP_HISTORY_DAYS = 365  # Toyota keeps about a year
ROUTES_PER_BATCH = 20


class CommandCooldown(Exception):
    """Raised when a command arrives too soon after the previous one."""

    def __init__(self, wait: int) -> None:
        super().__init__(f"Wait {wait}s before the next command")
        self.wait = wait


def _get(fn: Callable[[], Any], default: Any = None) -> Any:
    try:
        value = fn()
    except Exception:  # noqa: BLE001 - optional data, never fatal
        return default
    return default if value is None else value


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return None


def _minutes(value: Any) -> float | None:
    return round(value.total_seconds() / 60, 1) if isinstance(value, timedelta) else None


def _round(value: Any, ndigits: int = 1) -> float | None:
    return round(value, ndigits) if isinstance(value, (int, float)) else None


def _uk_mpg(value: Any, use_metric: bool) -> float | None:
    """pytoyoda 6.0.0 says UK mpg but divides by the US-gallon factor (235.215)."""
    value = _round(value)
    return value if use_metric or not value else round(value * 282.481 / 235.215, 1)


def _door(door: Any) -> dict | None:
    if door is None:
        return None
    return {"closed": _get(lambda: door.closed), "locked": _get(lambda: door.locked)}


def _window(window: Any) -> dict | None:
    if window is None:
        return None
    return {"closed": _get(lambda: window.closed)}


def _summary(summary: Any, use_metric: bool) -> dict | None:
    if summary is None:
        return None
    return {
        "from": _iso(_get(lambda: summary.from_date)),
        "to": _iso(_get(lambda: summary.to_date)),
        "distance": _round(_get(lambda: summary.distance)),
        "ev_distance": _round(_get(lambda: summary.ev_distance)),
        "duration_min": _minutes(_get(lambda: summary.duration)),
        "avg_speed": _round(_get(lambda: summary.average_speed)),
        "avg_consumption": _uk_mpg(_get(lambda: summary.average_fuel_consumed), use_metric),
        "hybrid_score": _round(_get(lambda: summary.hybrid_score), 0),
    }


def _service_record(s: Any) -> dict:
    """One Toyota service record, with the mileage in Toyota's own unit."""
    raw = getattr(s, "_service_history", None)
    notes = _get(lambda: s.notes)
    ops = _get(lambda: s.operations_performed)
    return {
        "id": _get(lambda: raw.service_history_id),
        "date": _iso(_get(lambda: s.service_date)),
        "mileage": _get(lambda: float(raw.mileage)),
        "unit": _get(lambda: raw.unit),
        "category": _get(lambda: s.service_category),
        "provider": _get(lambda: s.service_provider),
        "notes": str(notes) if notes else None,
        "operations": str(ops) if ops else None,
        "source": "toyota",
    }


def _trip_record(t: Any) -> dict:
    """One trip from Toyota's trip list, always in km and L/100km (see trips.py)."""
    from pytoyoda.models.trips import Trip

    trip = Trip(t, True)
    return {
        "id": _get(lambda: str(t.id)),
        "start": _iso(_get(lambda: trip.start_time)),
        "end": _iso(_get(lambda: trip.end_time)),
        "duration_min": _minutes(_get(lambda: trip.duration)),
        "distance_km": _round(_get(lambda: trip.distance), 2),
        "ev_distance_km": _round(_get(lambda: trip.ev_distance), 2),
        "avg_consumption": _round(_get(lambda: trip.average_fuel_consumed), 2) or None,
        "score": _get(lambda: trip.score) or None,
    }


async def vehicle_snapshot(vehicle: Any, use_metric: bool) -> dict:
    """Fetch everything for one car, sequentially."""
    # Skip pytoyoda's built-in "trip_history" endpoint: in 6.0.0 it calls
    # Api.get_trips(route=...), which that function doesn't accept, and the
    # TypeError aborts the whole update. We fetch trips ourselves below.
    await vehicle.update(skip=["trip_history"])

    dash = _get(lambda: vehicle.dashboard)
    loc = _get(lambda: vehicle.location)
    lock = _get(lambda: vehicle.lock_status)
    info = _get(lambda: vehicle._vehicle_info)  # static details from the vehicle list

    # These each cost one API call; a gap between them keeps Toyota happy.
    async def call(coro_fn, default=None):
        try:
            result = await coro_fn()
        except Exception as exc:  # noqa: BLE001
            log.warning("optional call failed: %s", exc)
            result = default
        await asyncio.sleep(1)
        return result

    week = await call(vehicle.get_current_week_summary)
    month = await call(vehicle.get_current_month_summary)

    doors = _get(lambda: lock.doors)
    windows = _get(lambda: lock.windows)

    return {
        "vin": _get(lambda: vehicle.vin),
        "alias": _get(lambda: vehicle.alias),
        "type": _get(lambda: vehicle.type),
        "model": {
            "name": _get(lambda: info.car_line_name),
            "year": _get(lambda: info.car_model_year),
            "color": _get(lambda: info.color),
            "description": _get(lambda: info.display_model_description),
        },
        "image_url": _get(lambda: info.image),
        "remote_commands": remote.supported(info),
        # Every record, merged into maintenance.json by the collector (not kept in the snapshot).
        "_service_records": [_service_record(s) for s in (_get(lambda: vehicle.service_history) or [])],
        "units": {
            "distance": "km" if use_metric else "mi",
            "consumption": "L/100km" if use_metric else "mpg",
        },
        "dashboard": {
            "odometer": _round(_get(lambda: dash.odometer), 0),
            "fuel_level": _get(lambda: dash.fuel_level),
            "fuel_range": _round(_get(lambda: dash.fuel_range), 0),
            "battery_level": _get(lambda: dash.battery_level),
            "total_range": _round(_get(lambda: dash.range), 0),
            "warning_lights": _get(lambda: [str(w) for w in dash.warning_lights], []),
        },
        "location": {
            "lat": _get(lambda: loc.latitude),
            "lon": _get(lambda: loc.longitude),
            "timestamp": _iso(_get(lambda: loc.timestamp)),
            "label": _get(lambda: loc.state),
        },
        "locks": {
            "updated": _iso(_get(lambda: lock.last_updated)),
            "doors": {
                "driver": _door(_get(lambda: doors.driver_seat)),
                "passenger": _door(_get(lambda: doors.passenger_seat)),
                "rear_left": _door(_get(lambda: doors.driver_rear_seat)),
                "rear_right": _door(_get(lambda: doors.passenger_rear_seat)),
                "boot": _door(_get(lambda: doors.trunk)),
                "bonnet": _door(_get(lambda: lock.hood)),
            },
            "windows": {
                "driver": _window(_get(lambda: windows.driver_seat)),
                "passenger": _window(_get(lambda: windows.passenger_seat)),
                "rear_left": _window(_get(lambda: windows.driver_rear_seat)),
                "rear_right": _window(_get(lambda: windows.passenger_rear_seat)),
            },
        },
        "summaries": {"week": _summary(week, use_metric), "month": _summary(month, use_metric)},
    }


class Collector:
    """Owns the Toyota client, the latest snapshot and the poll loop."""

    def __init__(self) -> None:
        self.username = os.environ.get("TOYOTA_USERNAME", "")
        self.password = os.environ.get("TOYOTA_PASSWORD", "")
        self.use_metric = os.environ.get("USE_METRIC", "false").lower() in ("1", "true", "yes")
        self.poll_minutes = max(10, int(os.environ.get("POLL_MINUTES", "30")))
        self.mock = os.environ.get("MOCK", "").lower() in ("1", "true", "yes")
        self.data_file = Path(os.environ.get("DATA_DIR", "./data")) / "snapshot.json"
        self.images = CarImage(
            data_dir=self.data_file.parent,
            custom_dir=Path(os.environ.get("CAR_IMAGE_DIR", "./images")),
        )

        self.maintenance = MaintenanceStore(self.data_file.parent / "maintenance.json")
        self.trips = TripStore(self.data_file.parent)

        # Remote control is opt-in: it can unlock the car.
        self.remote_enabled = os.environ.get("REMOTE_CONTROL", "").lower() in ("1", "true", "yes")
        self.command_log_file = self.data_file.parent / "command-log.json"
        self.last_command: dict | None = self._read_last_command()
        self._last_command_done: float | None = None  # time.monotonic(), for the cooldown

        self.snapshot: dict = {"vehicles": [], "fetched_at": None, "error": None}
        self.last_attempt: datetime | None = None
        self._client = None
        self._cars: list = []  # pytoyoda Vehicle objects from the last refresh, for commands
        self._lock = asyncio.Lock()
        self._command_lock = asyncio.Lock()
        self._followup: asyncio.Task | None = None
        self._load_cached()

    # -- persistence: the page shows last-known data immediately on restart --
    def _load_cached(self) -> None:
        try:
            self.snapshot = json.loads(self.data_file.read_text())
            self.snapshot["stale"] = True
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        try:
            self.data_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.data_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.snapshot, indent=2))
            tmp.replace(self.data_file)
        except OSError as exc:
            log.warning("could not persist snapshot: %s", exc)

    async def _client_instance(self):
        if self._client is None:
            from pytoyoda.client import MyT

            if not self.username or not self.password:
                raise RuntimeError("TOYOTA_USERNAME / TOYOTA_PASSWORD are not set")
            self._client = MyT(
                username=self.username, password=self.password, use_metric=self.use_metric
            )
        await self._client.login()  # no-op while the cached token is still valid
        return self._client

    async def refresh(self) -> dict:
        async with self._lock:
            self.last_attempt = datetime.now(timezone.utc)
            try:
                if self.mock:
                    from app.mock import mock_snapshot, mock_trips

                    vehicles = [mock_snapshot(self.use_metric)]
                    if not self.trips.trips:
                        trips = mock_trips()
                        self.trips.merge([t for t, _ in trips])
                        for t, route in trips:
                            self.trips.save_route(t["id"], route)
                else:
                    client = await self._client_instance()
                    cars = [c for c in await client.get_vehicles() if c is not None]
                    vehicles = []
                    for car in cars:
                        vehicles.append(await vehicle_snapshot(car, self.use_metric))
                    self._cars = cars
                    if vehicles:
                        await self.images.sync(vehicles[0].get("image_url"))
                    if cars:
                        await self._sync_trip_list(cars[0])
                for v in vehicles:
                    records = v.pop("_service_records", None) or []
                    if records:
                        new = self.maintenance.merge_services(records)
                        if new:
                            log.info("stored %d new service record(s)", new)
                self.snapshot = {
                    "vehicles": vehicles,
                    "fetched_at": self.last_attempt.isoformat(),
                    "error": None,
                    "stale": False,
                }
                self._save()
                log.info("refreshed %d vehicle(s)", len(vehicles))
            except Exception as exc:  # noqa: BLE001
                log.exception("refresh failed")
                # Keep last good data on screen; just flag the error.
                self.snapshot = {**self.snapshot, "error": f"{type(exc).__name__}: {exc}", "stale": True}
            return self.snapshot

    # -- trip history -------------------------------------------------------------------
    async def _sync_trip_list(self, car: Any) -> None:
        """Store new trips, newest first, stopping at the first page we already have."""
        today = date.today()
        offset: int | None = 0
        try:
            while offset is not None:
                resp = await car._api.get_trips(
                    car.vin, today - timedelta(days=TRIP_HISTORY_DAYS), today,
                    summary=False, limit=50, offset=offset,
                )
                await asyncio.sleep(1)
                page = _get(lambda: resp.payload.trips) or []
                records = [_trip_record(t) for t in page]
                seen_all_new = not any(self.trips.known(r["id"]) for r in records if r["id"])
                new = self.trips.merge(records)
                if new:
                    log.info("stored %d new trip(s)", new)
                if not seen_all_new:
                    break
                offset = _get(lambda: resp.payload.metadata.pagination.next_offset)
        except Exception as exc:  # noqa: BLE001 - trip history is optional
            log.warning("trip list sync failed: %s", exc)

    async def _fetch_routes(self, car: Any, limit: int) -> int:
        """Fetch up to `limit` missing routes, one call at a time. Returns how many."""
        done = 0
        for tid in self.trips.missing_routes()[:limit]:
            try:
                resp = await car._api.get_trip_details(car.vin, tid)
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                if "429" in msg or re.search(r"Failed\. 5\d\d", msg):
                    log.warning("route fetch paused: %s", msg[:200])
                    break  # Toyota is busy: try again next round
                log.warning("no route for trip %s: %s", tid, msg[:200])
                self.trips.save_route(tid, [])
            else:
                points = _get(lambda: resp.payload.route) or []
                self.trips.save_route(tid, [[round(p.lat, 5), round(p.lon, 5)] for p in points
                                            if p.lat is not None and p.lon is not None])
            done += 1
            await asyncio.sleep(1)
        return done

    async def backfill_routes(self) -> None:
        """Fetch every missing route in small batches, sharing the refresh lock."""
        while not self.mock and self._cars and self.trips.missing_routes():
            async with self._lock:
                await self._client_instance()
                done = await self._fetch_routes(self._cars[0], ROUTES_PER_BATCH)
            if not done:
                return
            log.info("fetched %d route(s), %d to go", done, len(self.trips.missing_routes()))
            await asyncio.sleep(5)

    def backup(self) -> dict:
        """Everything stored locally, as one JSON-able dict. Runs in a worker thread."""
        try:
            command_log = json.loads(self.command_log_file.read_text())
        except (OSError, ValueError):
            command_log = []
        trip_ids = list(self.trips.trips)
        return {
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "snapshot": self.snapshot,
            "maintenance": self.maintenance.data,
            "trips": {i: self.trips.trips[i] for i in trip_ids},
            "routes": {i: r for i in trip_ids if (r := self.trips.route(i))},
            "command_log": command_log,
        }

    # -- remote commands ---------------------------------------------------------------
    def _read_last_command(self) -> dict | None:
        try:
            entries = json.loads(self.command_log_file.read_text())
            return entries[-1] if entries else None
        except (OSError, ValueError, IndexError):
            return None

    def _log_command(self, entry: dict) -> None:
        """Append to an audit log of every command sent (last 200 kept)."""
        try:
            entries = json.loads(self.command_log_file.read_text())
        except (OSError, ValueError):
            entries = []
        entries = (entries + [entry])[-200:]
        try:
            self.command_log_file.parent.mkdir(parents=True, exist_ok=True)
            self.command_log_file.write_text(json.dumps(entries, indent=2))
        except OSError as exc:
            log.warning("could not write command log: %s", exc)
        self.last_command = entry

    def _seconds_until_next_command(self, min_gap: int) -> int:
        if self._last_command_done is None:
            return 0
        return max(0, math.ceil(min_gap - (time.monotonic() - self._last_command_done)))

    async def run_command(self, cmd_id: str, client_ip: str | None = None, min_gap: int = 15) -> dict:
        """Send one remote command to the (first) car. Caller has already checked
        that remote control is enabled and the request carried the hold header."""
        cmd = remote.BY_ID.get(cmd_id)
        if cmd is None:
            raise ValueError("unknown command")
        vehicles = self.snapshot.get("vehicles") or []
        allowed = {c["id"] for c in (vehicles[0].get("remote_commands") or [])} if vehicles else set()
        if cmd_id not in allowed:
            raise ValueError("this car doesn't support that command")

        async with self._command_lock:
            # Checked inside the lock, so two quick taps can't both get through.
            wait = self._seconds_until_next_command(min_gap)
            if wait:
                raise CommandCooldown(wait)
            started = datetime.now(timezone.utc)
            unknown = False
            try:
                if self.mock:
                    await asyncio.sleep(1.2)
                    ok, message = True, "accepted (mock, nothing was sent)"
                else:
                    # Same lock as refresh: one Toyota call at a time, and no clash over the login token.
                    async with self._lock:
                        if not self._cars:
                            client = await self._client_instance()
                            self._cars = [c for c in await client.get_vehicles() if c is not None]
                        if not self._cars:
                            raise RuntimeError("no vehicle on this account")
                        await self._client_instance()  # make sure the token is fresh
                        ok, message = await remote.execute(self._cars[0], cmd)
            except remote.OutcomeUnknown as exc:
                log.warning("command %s: outcome unknown: %s", cmd_id, exc)
                ok, unknown = False, True
                message = f"{exc}. The car may still have received it: check its status."
            except Exception as exc:  # noqa: BLE001
                log.warning("command %s failed: %s", cmd_id, exc)
                ok, message = False, f"{type(exc).__name__}: {exc}"
            entry = {
                "at": started.isoformat(timespec="seconds"),
                "command": cmd.id,
                "label": cmd.label,
                "ok": ok,
                "unknown": unknown,
                "message": message,
                "from": client_ip,
            }
            self._log_command(entry)
            self._last_command_done = time.monotonic()
            log.info("command %s -> %s (%s)", cmd.id, "ok" if ok else "failed", message)

        if (ok or unknown) and not self.mock:
            self._schedule_followup()
        return entry

    def _schedule_followup(self, delay: int = 60) -> None:
        """The car takes a little while to report its new state; refresh once after."""
        if self._followup and not self._followup.done():
            self._followup.cancel()

        async def later() -> None:
            await asyncio.sleep(delay)
            await self.refresh()

        self._followup = asyncio.create_task(later())

    async def run_forever(self) -> None:
        while True:
            await self.refresh()
            try:
                await self.backfill_routes()
            except Exception:  # noqa: BLE001
                log.exception("route backfill failed")
            await asyncio.sleep(self.poll_minutes * 60)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
