"""Fake data with the same shape as a real snapshot, for working on the UI
without touching Toyota's API (MOCK=true)."""

from datetime import datetime, timedelta, timezone


def mock_snapshot(use_metric: bool) -> dict:
    now = datetime.now(timezone.utc)
    k = 1.609 if use_metric else 1.0
    closed_locked = {"closed": True, "locked": True}
    trips = []
    t = now - timedelta(hours=3)
    for i, (dist, mins, score) in enumerate(
        [(14.2, 28, 82), (61.5, 74, 76), (3.1, 9, 91), (14.0, 31, 79), (8.7, 18, 85)]
    ):
        trips.append(
            {
                "start": (t - timedelta(minutes=mins)).isoformat(),
                "end": t.isoformat(),
                "duration_min": mins,
                "distance": round(dist * k, 1),
                "ev_distance": round(dist * k * 0.48, 1),
                "avg_consumption": 4.3 if use_metric else 65.7,
                "score": score,
                "from": {"lat": 52.2053 + i * 0.004, "lon": 0.1218},
                "to": {"lat": 52.1951, "lon": 0.1313 - i * 0.003},
            }
        )
        t -= timedelta(hours=7 + i * 5)
    return {
        "vin": "MOCKVIN0000000000",
        "alias": "Rosa",
        "type": "hybrid",
        "model": {"name": "Corolla HB/TS", "year": "2025", "color": "Emotional Red 2", "description": None},
        "image_url": None,
        # Same set Rosa's capability flags allow.
        "remote_commands": [
            {"id": "lock", "label": "Lock", "group": "security", "caution": False},
            {"id": "unlock", "label": "Unlock", "group": "security", "caution": True},
            {"id": "boot-lock", "label": "Lock boot", "group": "security", "caution": False},
            {"id": "boot-unlock", "label": "Unlock boot", "group": "security", "caution": True},
            {"id": "hazards-on", "label": "Hazards on", "group": "signals", "caution": False},
            {"id": "hazards-off", "label": "Hazards off", "group": "signals", "caution": False},
            {"id": "horn", "label": "Sound horn", "group": "signals", "caution": False},
            {"id": "climate-start", "label": "Start climate", "group": "comfort", "caution": False},
            {"id": "climate-stop", "label": "Stop climate", "group": "comfort", "caution": False},
            {"id": "windows-close", "label": "Close windows", "group": "comfort", "caution": False},
        ],
        "_service_records": [
            {"id": "m1", "date": "2026-05-13", "mileage": 9600.0, "unit": "km", "category": "MNT", "provider": "Sample Dealer", "notes": None, "operations": None, "source": "toyota"},
            {"id": "m2", "date": "2026-05-13", "mileage": 9600.0, "unit": "km", "category": "HSC", "provider": "Sample Dealer", "notes": None, "operations": None, "source": "toyota"},
            {"id": "m3", "date": "2025-06-12", "mileage": 0.0, "unit": "km", "category": "VDE", "provider": "Sample Dealer", "notes": None, "operations": None, "source": "toyota"},
        ],
        "units": {
            "distance": "km" if use_metric else "mi",
            "consumption": "L/100km" if use_metric else "mpg",
        },
        "dashboard": {
            "odometer": round(8412 * k),
            "fuel_level": 62,
            "fuel_range": round(318 * k),
            "battery_level": None,
            "total_range": round(318 * k),
            "warning_lights": [],
        },
        "location": {
            "lat": 52.1951,
            "lon": 0.1313,
            "timestamp": (now - timedelta(hours=3)).isoformat(),
            "label": "Cambridge",
        },
        "locks": {
            "updated": (now - timedelta(hours=3)).isoformat(),
            "doors": {
                "driver": closed_locked,
                "passenger": closed_locked,
                "rear_left": closed_locked,
                "rear_right": closed_locked,
                "boot": closed_locked,
                "bonnet": {"closed": True, "locked": None},
            },
            "windows": {
                "driver": {"closed": True},
                "passenger": {"closed": True},
                "rear_left": {"closed": True},
                "rear_right": {"closed": False},
            },
        },
        "service": {
            "date": "2026-06-14",
            "odometer": round(6020 * k),
            "category": "Annual service",
            "provider": "Dealer",
        },
        "trips": trips,
        "summaries": {
            "week": {
                "from": (now - timedelta(days=now.weekday())).date().isoformat(),
                "to": now.date().isoformat(),
                "distance": round(101.5 * k, 1),
                "ev_distance": round(47.9 * k, 1),
                "duration_min": 160,
                "avg_speed": round(38 * k),
                "avg_consumption": 4.4 if use_metric else 64.2,
                "hybrid_score": 81,
            },
            "month": {
                "from": now.replace(day=1).date().isoformat(),
                "to": now.date().isoformat(),
                "distance": round(512.3 * k, 1),
                "ev_distance": round(231.0 * k, 1),
                "duration_min": 790,
                "avg_speed": round(41 * k),
                "avg_consumption": 4.6 if use_metric else 61.4,
                "hybrid_score": 79,
            },
        },
    }
