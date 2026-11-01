"""Fill-ups and fuel-level history, kept in DATA_DIR/fuel.json.

Toyota only reports the fuel level as a whole percentage, never litres. To turn
that into litres, every fill-up you log is matched to the jump in fuel level it
caused (e.g. 18% -> 100% for 35.2 L), which gives litres per 1% for this car.
Until there's a usable fill-up, FUEL_TANK_LITRES / 100 is used instead.

- readings: fuel level after each refresh, stored only when it changes.
- fillups: entries you add yourself (date, litres, cost, odometer, fuel type).
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("rosa.fuel")

MAX_READINGS = 20_000
MIN_JUMP = 5          # % rise that counts as a refuel, above sensor noise
MIN_CALIBRATION = 15  # % rise needed before a fill-up is used to calibrate
MATCH_DAYS = 3        # how long after the fill-up date the car may report the new level


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _day(iso: str) -> date:
    """Local calendar day of a stored UTC timestamp."""
    return datetime.fromisoformat(iso).astimezone().date()


class FuelStore:
    def __init__(self, path: Path, tank_litres: float | None) -> None:
        self.path = path
        self.tank_litres = tank_litres
        self.data: dict = {"readings": [], "fillups": []}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.error("fuel.json unreadable (%s); keeping a backup and starting fresh", exc)
            try:
                self.path.replace(self.path.with_suffix(f".broken-{int(datetime.now().timestamp())}.json"))
            except OSError:
                pass
            return
        self.data["readings"] = raw.get("readings") or []
        self.data["fillups"] = raw.get("fillups") or []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.replace(self.path)

    # -- readings ---------------------------------------------------------------------
    def record(self, at: str, pct: float | None) -> None:
        """Store the fuel level from a refresh, if it changed."""
        if pct is None:
            return
        readings = self.data["readings"]
        if readings and readings[-1]["pct"] == pct:
            return
        readings.append({"t": at, "pct": pct})
        del readings[:-MAX_READINGS]
        self._save()

    def _jumps(self) -> list[tuple[dict, dict]]:
        r = self.data["readings"]
        return [(a, b) for a, b in zip(r, r[1:]) if b["pct"] - a["pct"] >= MIN_JUMP]

    # -- fill-ups -----------------------------------------------------------------------
    def _entry(self, *, on: date, litres: float, cost: float | None, odometer: float | None, unit: str,
               fuel_type: str | None, pct_before: float | None, pct_after: float | None, notes: str) -> dict:
        return {
            "date": on.isoformat(),
            "litres": round(float(litres), 2),
            "cost": None if cost is None else round(float(cost), 2),
            "odometer": None if odometer is None else round(float(odometer), 1),
            "unit": unit,
            "fuel_type": fuel_type,  # "E10" (regular 95) or "E5" (premium 97-99)
            # Only set when typed in; otherwise worked out from readings on each view.
            "pct_before": pct_before,
            "pct_after": pct_after,
            "notes": notes.strip()[:500],
        }

    def add_fillup(self, **fields) -> dict:
        entry = {"id": secrets.token_hex(4), **self._entry(**fields), "created": _now()}
        self.data["fillups"].append(entry)
        self._save()
        return entry

    def update_fillup(self, fid: str, **fields) -> dict | None:
        for entry in self.data["fillups"]:
            if entry.get("id") == fid:
                entry.update(self._entry(**fields), edited=_now())
                self._save()
                return entry
        return None

    def delete_fillup(self, fid: str) -> bool:
        before = len(self.data["fillups"])
        self.data["fillups"] = [f for f in self.data["fillups"] if f.get("id") != fid]
        if len(self.data["fillups"]) == before:
            return False
        self._save()
        return True

    # -- derived ------------------------------------------------------------------------
    def _matched(self) -> list[dict]:
        """Fill-ups, newest first, each with the fuel level before/after and where it came from."""
        jumps = self._jumps()
        used: set[int] = set()
        out = []
        for f in sorted(self.data["fillups"], key=lambda f: (f["date"], f.get("created", "")), reverse=True):
            item = {**f, "level_source": None}
            if f.get("pct_before") is not None and f.get("pct_after") is not None:
                item["level_source"] = "entered"
            else:
                on = date.fromisoformat(f["date"])
                for i, (a, b) in enumerate(jumps):
                    if i not in used and _day(a["t"]) <= on <= _day(b["t"]) <= on + timedelta(days=MATCH_DAYS):
                        used.add(i)
                        item.update(pct_before=a["pct"], pct_after=b["pct"], level_source="detected")
                        break
            out.append(item)
        return out

    def calibration(self, fillups: list[dict] | None = None) -> dict:
        """Litres per 1% of the gauge: from fill-ups if possible, else the tank size."""
        fillups = self._matched() if fillups is None else fillups
        litres = rise = 0.0
        used = 0
        for f in fillups:
            if f["level_source"] and f["pct_after"] - f["pct_before"] >= MIN_CALIBRATION:
                litres += f["litres"]
                rise += f["pct_after"] - f["pct_before"]
                used += 1
        if used:
            per_pct = litres / rise
            return {"litres_per_pct": per_pct, "tank_litres": per_pct * 100, "calibrated": True, "fillups_used": used}
        if self.tank_litres:
            return {"litres_per_pct": self.tank_litres / 100, "tank_litres": self.tank_litres,
                    "calibrated": False, "fillups_used": 0}
        return {"litres_per_pct": None, "tank_litres": None, "calibrated": False, "fillups_used": 0}

    def view(self) -> dict:
        fillups = self._matched()
        today = date.today()
        month = today.replace(day=1).isoformat()
        year = today.replace(month=1, day=1).isoformat()
        priced = [f for f in fillups if f.get("cost") and f.get("litres")]

        def spent(since: str) -> float:
            return round(sum(f["cost"] for f in fillups if f.get("cost") and f["date"] >= since), 2)

        return {
            "fillups": fillups,
            "calibration": self.calibration(fillups),
            "spend": {
                "month": spent(month),
                "year": spent(year),
                "last_price_per_litre": round(priced[0]["cost"] / priced[0]["litres"], 3) if priced else None,
            },
            "today": today.isoformat(),
        }
