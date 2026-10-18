"""Every trip Toyota has reported, kept in DATA_DIR so history outlives Toyota's
12-month window.

- trips.json: one summary per trip id, stored in km and L/100km.
- routes/<id>.json: the full GPS route as [[lat, lon], ...].

Routes cost one API call each, so they're fetched separately and gradually.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger("rosa.trips")

KM_PER_MI = 1.609344
MPG_UK_L100 = 282.481
# ~10 m: plenty for the all-trips map, and about a tenth of the points.
OVERVIEW_TOLERANCE = 0.0001
_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")


def simplify(points: list[list[float]], tolerance: float = OVERVIEW_TOLERANCE) -> list[list[float]]:
    """Ramer-Douglas-Peucker, iterative so long routes can't hit the recursion limit."""
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        (y1, x1), (y2, x2) = points[a], points[b]
        dx, dy = x2 - x1, y2 - y1
        norm = (dx * dx + dy * dy) ** 0.5
        far, far_i = 0.0, -1
        for i in range(a + 1, b):
            y, x = points[i]
            d = abs(dy * x - dx * y + x2 * y1 - y2 * x1) / norm if norm else ((x - x1) ** 2 + (y - y1) ** 2) ** 0.5
            if d > far:
                far, far_i = d, i
        if far > tolerance:
            keep[far_i] = True
            stack += [(a, far_i), (far_i, b)]
    return [p for p, k in zip(points, keep) if k]


class TripStore:
    def __init__(self, data_dir: Path) -> None:
        self.file = data_dir / "trips.json"
        self.routes_dir = data_dir / "routes"
        self.trips: dict[str, dict] = {}
        self._overview: list[list[list[float]]] | None = None
        try:
            self.trips = json.loads(self.file.read_text())
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            log.error("trips.json unreadable (%s); starting empty", exc)

    def _save(self) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.trips))
        tmp.replace(self.file)

    # -- writes ----------------------------------------------------------------------
    def merge(self, trips: list[dict]) -> int:
        """Add trips not seen before. Returns how many were new."""
        new = [t for t in trips if t.get("id") and _ID.match(t["id"]) and t["id"] not in self.trips]
        for t in new:
            self.trips[t["id"]] = t
        if new:
            self._save()
        return len(new)

    def known(self, trip_id: str) -> bool:
        return trip_id in self.trips

    def missing_routes(self) -> list[str]:
        """Trips without a stored route, newest first."""
        ids = [i for i, t in self.trips.items() if not t.get("has_route") and not t.get("no_route")]
        return sorted(ids, key=lambda i: self.trips[i].get("start") or "", reverse=True)

    def save_route(self, trip_id: str, points: list[list[float]]) -> None:
        trip = self.trips.get(trip_id)
        if trip is None:
            return
        if points:
            self.routes_dir.mkdir(parents=True, exist_ok=True)
            path = self.routes_dir / f"{trip_id}.json"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(points))
            tmp.replace(path)
            trip["has_route"] = True
            if self._overview is not None:
                self._overview.append(simplify(points))
        else:
            trip["no_route"] = True  # Toyota has none; don't ask again
        self._save()

    # -- reads -----------------------------------------------------------------------
    def route(self, trip_id: str) -> list[list[float]] | None:
        if not _ID.match(trip_id) or not self.trips.get(trip_id, {}).get("has_route"):
            return None
        try:
            return json.loads((self.routes_dir / f"{trip_id}.json").read_text())
        except (OSError, ValueError):
            return None

    def overview(self) -> list[list[list[float]]]:
        """Every stored route, simplified. Built once, then kept up to date."""
        if self._overview is None:
            self._overview = [simplify(r) for i in list(self.trips) if (r := self.route(i))]
        return self._overview

    def page(self, page: int, per_page: int, metric: bool) -> dict:
        ordered = sorted(self.trips.values(), key=lambda t: t.get("start") or "", reverse=True)
        pages = max(1, -(-len(ordered) // per_page))
        page = min(max(1, page), pages)
        items = [self._display(t, metric) for t in ordered[(page - 1) * per_page: page * per_page]]
        return {
            "trips": items,
            "total": len(ordered),
            "page": page,
            "pages": pages,
            "routes_pending": len(self.missing_routes()),
        }

    def stats(self, period: str, metric: bool) -> dict:
        """Totals per week (Monday start) or calendar month, newest first, no gaps."""
        def bucket(d: date) -> date:
            return d - timedelta(days=d.weekday()) if period == "week" else d.replace(day=1)

        def step_back(d: date) -> date:
            return d - timedelta(days=7) if period == "week" else (d - timedelta(days=1)).replace(day=1)

        totals: dict[date, dict] = {}
        first: date | None = None
        for t in self.trips.values():
            try:
                day = datetime.fromisoformat(t["start"]).astimezone().date()  # local (TZ) date
            except (KeyError, TypeError, ValueError):
                continue
            first = day if first is None or day < first else first
            b = totals.setdefault(bucket(day), {"trips": 0, "km": 0.0, "ev_km": 0.0, "litres": 0.0})
            km = t.get("distance_km") or 0.0
            b["trips"] += 1
            b["km"] += km
            b["ev_km"] += t.get("ev_distance_km") or 0.0
            b["litres"] += km * (t.get("avg_consumption") or 0.0) / 100

        current = bucket(date.today())
        periods = []
        if first is not None:
            start, d = bucket(first), current
            while d >= start:
                b = totals.get(d, {"trips": 0, "km": 0.0, "ev_km": 0.0, "litres": 0.0})
                l100 = b["litres"] / b["km"] * 100 if b["km"] and b["litres"] else None
                periods.append({
                    "start": d.isoformat(),
                    "current": d == current,
                    "trips": b["trips"],
                    "distance": round(b["km"] if metric else b["km"] / KM_PER_MI, 1),
                    "ev_distance": round(b["ev_km"] if metric else b["ev_km"] / KM_PER_MI, 1),
                    "fuel_litres": round(b["litres"], 1),
                    "avg_consumption": None if l100 is None else round(l100 if metric else MPG_UK_L100 / l100, 1),
                })
                d = step_back(d)
        return {"period": period, "since": first.isoformat() if first else None, "periods": periods}

    @staticmethod
    def _display(t: dict, metric: bool) -> dict:
        """A stored trip in the dashboard's units (stored values are metric)."""
        dist = lambda v: None if v is None else round(v if metric else v / KM_PER_MI, 1)  # noqa: E731
        cons = t.get("avg_consumption")
        if cons is not None and not metric:
            cons = round(MPG_UK_L100 / cons, 1) if cons else None
        return {
            "id": t["id"],
            "start": t.get("start"),
            "end": t.get("end"),
            "duration_min": t.get("duration_min"),
            "distance": dist(t.get("distance_km")),
            "ev_distance": dist(t.get("ev_distance_km")),
            "avg_consumption": cons,
            "score": t.get("score"),
            "has_route": bool(t.get("has_route")),
        }
