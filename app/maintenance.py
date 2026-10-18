"""Service & maintenance records, kept in DATA_DIR/maintenance.json.

- services: every service record Toyota has ever returned, merged by id.
  Kept even if Toyota later drops one, so this is your permanent history.
- tyre_rotations: entries you add yourself; Toyota knows nothing about them.
- settings: the tyre-rotation interval used to work out when the next is due.

Distances are stored with their own unit ("mi"/"km") exactly as entered or
as Toyota sent them, and converted only for display.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("rosa.maintenance")

DEFAULT_SETTINGS = {
    # Toyota's general guidance is roughly every 5,000 miles; months is a fallback
    # for low-mileage years. Both can be changed from the page.
    "rotation_interval": 5000,
    "rotation_interval_unit": "mi",
    "rotation_months": 6,
}

# Toyota's service category codes, as seen in the MyToyota API.
# VDE/HSC are best guesses from context (VDE = the 0-mile record at handover).
CATEGORY_LABELS = {
    "MNT": "Maintenance service",
    "HSC": "Hybrid service check",
    "VDE": "Vehicle delivery",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MaintenanceStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = {"services": {}, "tyre_rotations": [], "settings": dict(DEFAULT_SETTINGS)}
        self._load()

    # -- persistence ------------------------------------------------------------
    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            # Don't silently overwrite a file we couldn't read: keep a copy.
            log.error("maintenance.json unreadable (%s); keeping a backup and starting fresh", exc)
            try:
                self.path.replace(self.path.with_suffix(f".broken-{int(datetime.now().timestamp())}.json"))
            except OSError:
                pass
            return
        self.data["services"] = raw.get("services") or {}
        self.data["tyre_rotations"] = raw.get("tyre_rotations") or []
        self.data["settings"] = {**DEFAULT_SETTINGS, **(raw.get("settings") or {})}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.replace(self.path)

    # -- Toyota service records -------------------------------------------------------
    def merge_services(self, records: list[dict]) -> int:
        """Add/refresh records from Toyota. Returns how many were new."""
        new = 0
        services = self.data["services"]
        for rec in records:
            rid = str(rec.get("id") or "")
            if not rid:
                key = f"{rec.get('date')}|{rec.get('category')}|{rec.get('mileage')}"
                rid = "h" + hashlib.sha1(key.encode()).hexdigest()[:12]
            existing = services.get(rid)
            entry = {**(existing or {}), **{k: v for k, v in rec.items() if k != "id"}}
            entry.setdefault("first_seen", _now())
            if existing is None:
                new += 1
            if entry != existing:
                services[rid] = entry
        if new or records:
            self._save()
        return new

    # -- tyre rotations ---------------------------------------------------------------
    def add_rotation(self, *, on: date, odometer: float, unit: str, notes: str = "") -> dict:
        entry = {
            "id": secrets.token_hex(4),
            "date": on.isoformat(),
            "odometer": round(float(odometer), 1),
            "unit": unit,
            "notes": notes.strip()[:500],
            "created": _now(),
        }
        self.data["tyre_rotations"].append(entry)
        self._save()
        return entry

    def delete_rotation(self, rid: str) -> bool:
        before = len(self.data["tyre_rotations"])
        self.data["tyre_rotations"] = [r for r in self.data["tyre_rotations"] if r.get("id") != rid]
        if len(self.data["tyre_rotations"]) == before:
            return False
        self._save()
        return True

    def update_settings(self, **changes: Any) -> dict:
        self.data["settings"].update({k: v for k, v in changes.items() if v is not None})
        self._save()
        return self.data["settings"]

    # -- read ------------------------------------------------------------------------------
    def view(self) -> dict:
        services = [
            {"id": rid, **rec, "category_label": CATEGORY_LABELS.get(rec.get("category") or "", rec.get("category"))}
            for rid, rec in self.data["services"].items()
        ]
        services.sort(key=lambda s: (s.get("date") or "", s.get("mileage") or 0), reverse=True)
        rotations = sorted(self.data["tyre_rotations"], key=lambda r: (r["date"], r["odometer"]), reverse=True)
        return {"services": services, "tyre_rotations": rotations, "settings": self.data["settings"]}
