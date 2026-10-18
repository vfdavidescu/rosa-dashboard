"""Remote commands: which ones the car supports, and sending them.

Only commands listed in COMMANDS can ever be sent, and only when the car's
own capability flags (from the MyToyota vehicle profile) say it supports
them. Engine start, headlights, find-vehicle, hazards-off and the window-open
commands are deliberately left out: either the car reports them unsupported or
the EU backend rejects them (CTP-REMOTE-40006; pytoyoda issue #274).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("rosa.remote")


class OutcomeUnknown(Exception):
    """The command may have reached the car even though we got no clean answer."""


@dataclass(frozen=True)
class Command:
    id: str            # stable id used by the page and API
    label: str
    group: str         # "security" | "signals" | "comfort"
    capability: str    # pytoyoda extended_capabilities attribute that must be True
    kind: str          # "command" (CommandType value) or "climate"
    action: str        # CommandType value, or "start"/"stop" for climate
    caution: bool = False  # styled as a riskier action on the page


COMMANDS: tuple[Command, ...] = (
    Command("lock", "Lock", "security", "door_lock_unlock_capable", "command", "door-lock"),
    Command("unlock", "Unlock", "security", "door_lock_unlock_capable", "command", "door-unlock", caution=True),
    Command("boot-lock", "Lock boot", "security", "trunk_lock_unlock_capable", "command", "trunk-lock"),
    Command("boot-unlock", "Unlock boot", "security", "trunk_lock_unlock_capable", "command", "trunk-unlock", caution=True),
    Command("hazards-on", "Hazards on", "signals", "hazard_capable", "command", "hazard-on"),
    Command("horn", "Sound horn", "signals", "horn_capable", "command", "sound-horn"),
    Command("climate-start", "Start climate", "comfort", "climate_capable", "climate", "start"),
    Command("climate-stop", "Stop climate", "comfort", "climate_capable", "climate", "stop"),
    Command("windows-close", "Close windows", "comfort", "power_windows_capable", "command", "power-window-close"),
)
BY_ID = {c.id: c for c in COMMANDS}

# What the climate return codes mean (from pytoyoda's docs).
CLIMATE_CODES = {
    "000000": "accepted",
    "118003": "car not ready: check it's locked, doors shut and the key isn't inside",
    "600000": "car not ready: check it's locked, doors shut and the key isn't inside",
    "200000": "climate is already running",
}


def supported(vehicle_info: Any) -> list[dict]:
    """Commands this car says it can do, as plain dicts for the page."""
    caps = getattr(vehicle_info, "extended_capabilities", None)
    out = []
    for cmd in COMMANDS:
        if caps is not None and getattr(caps, cmd.capability, False) is True:
            out.append({"id": cmd.id, "label": cmd.label, "group": cmd.group, "caution": cmd.caution})
    return out


def _describe_status(result: Any) -> str:
    """Pull a human message out of pytoyoda's StatusModel, if there is one."""
    status = getattr(result, "status", None)
    messages = getattr(status, "messages", None) or []
    for msg in messages:
        text = getattr(msg, "description", None) or getattr(msg, "detailed_description", None)
        if text:
            return str(text)
    if isinstance(status, str) and status:
        return status
    return "accepted"


async def _post_once(vehicle: Any, endpoint: str, body: dict) -> dict:
    """POST exactly once. pytoyoda retries 429/5xx, which would repeat a command.

    Uses pytoyoda 6.x internals (Controller), hence the version pin.
    """
    import httpx

    ctl = vehicle._api.controller
    if not ctl._is_token_valid():
        await ctl._update_token()
    if ctl._client is None:
        ctl._client = httpx.AsyncClient(timeout=ctl._timeout, verify=await ctl._get_ssl_context())
    try:
        resp = await ctl._client.request(
            "POST", f"{ctl._api_base_url}{endpoint}",
            headers=ctl._prepare_headers(vehicle.vin), json=body, follow_redirects=True,
        )
    except (httpx.ConnectError, httpx.ConnectTimeout):
        raise  # never connected: nothing was sent
    except httpx.TransportError as exc:
        raise OutcomeUnknown(f"No answer from Toyota ({type(exc).__name__})") from exc
    if resp.status_code >= 500:
        raise OutcomeUnknown(f"Toyota answered {resp.status_code}: {resp.text[:200]}")
    if resp.status_code not in (200, 202):
        raise RuntimeError(f"Toyota answered {resp.status_code}: {resp.text[:300]}")
    log.info("Toyota replied %s: %s", resp.status_code, resp.text[:500])
    return resp.json() if resp.content else {}


async def execute(vehicle: Any, cmd: Command) -> tuple[bool, str]:
    """Send one command. Returns (accepted, message). Raises on transport errors."""
    from pytoyoda.api import VEHICLE_CLIMATE_CONTROL_ENDPOINT, VEHICLE_COMMAND_ENDPOINT

    if cmd.kind == "climate":
        from pytoyoda.models.endpoints.climate import (
            RemoteClimateControlResponseModel,
            V2RemoteClimateControlRequestModel,
        )

        # "start" with no settings uses the car's saved preset (temperature, duration).
        body = V2RemoteClimateControlRequestModel(command=cmd.action).model_dump(exclude_none=True, by_alias=True)
        resp = RemoteClimateControlResponseModel(**await _post_once(vehicle, VEHICLE_CLIMATE_CONTROL_ENDPOINT, body))
        code = str(getattr(getattr(resp, "payload", None), "return_code", "") or "")
        if code == "000000":
            return True, "accepted"
        return False, CLIMATE_CODES.get(code, f"rejected (code {code or 'unknown'})")

    from pytoyoda.models.endpoints.command import CommandType, RemoteCommandModel
    from pytoyoda.models.endpoints.common import StatusModel

    body = RemoteCommandModel(beep_count=0, command=CommandType(cmd.action)).model_dump(exclude_unset=True, by_alias=True)
    result = StatusModel(**await _post_once(vehicle, VEHICLE_COMMAND_ENDPOINT, body))
    # Toyota's reply for ordinary commands isn't documented: only explicit errors count as refusal.
    if result.errors:
        return False, f"Toyota refused it: {str(result.errors[0])[:200]}"
    return True, _describe_status(result)
