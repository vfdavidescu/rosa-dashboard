"""The car picture shown on the dashboard.

Two sources, in order of preference:
1. Your own photo: a file named car.png / car.jpg / car.jpeg / car.webp in
   CAR_IMAGE_DIR (mounted from ./images by docker-compose).
2. Toyota's render: the URL that comes back from the MyToyota vehicle list,
   downloaded once into DATA_DIR and refreshed only if the URL changes.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import httpx

log = logging.getLogger("rosa.images")

CUSTOM_NAMES = ("car.png", "car.webp", "car.jpg", "car.jpeg")
EXT_BY_TYPE = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}
TYPE_BY_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}
MAX_BYTES = 10 * 1024 * 1024


class CarImage:
    def __init__(self, data_dir: Path, custom_dir: Path) -> None:
        self.data_dir = data_dir
        self.custom_dir = custom_dir
        self.meta_file = data_dir / "car-image.json"

    # -- your own photo --------------------------------------------------------
    def _custom(self) -> Path | None:
        for name in CUSTOM_NAMES:
            path = self.custom_dir / name
            if path.is_file():
                return path
        return None

    # -- Toyota's render ---------------------------------------------------------
    def _meta(self) -> dict:
        try:
            return json.loads(self.meta_file.read_text())
        except (OSError, ValueError):
            return {}

    async def sync(self, url: str | None) -> None:
        """Download Toyota's image if we don't have this exact URL yet."""
        if not url or not url.startswith("https://"):
            return
        meta = self._meta()
        cached = self.data_dir / meta.get("file", "")
        if meta.get("url") == url and cached.is_file():
            return
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("could not download car image: %s", exc)
            return
        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype not in EXT_BY_TYPE or len(resp.content) > MAX_BYTES:
            log.warning("ignoring car image: type=%r size=%d", ctype, len(resp.content))
            return
        name = "car-image" + EXT_BY_TYPE[ctype]
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.data_dir / (name + ".tmp")
        tmp.write_bytes(resp.content)
        tmp.replace(self.data_dir / name)
        self.meta_file.write_text(json.dumps({"url": url, "file": name, "type": ctype}))
        log.info("car image saved (%s, %d KB)", ctype, len(resp.content) // 1024)

    # -- what to serve ---------------------------------------------------------
    def current(self) -> tuple[Path, str] | None:
        custom = self._custom()
        if custom is not None:
            return custom, TYPE_BY_EXT.get(custom.suffix.lower(), "application/octet-stream")
        meta = self._meta()
        path = self.data_dir / meta.get("file", "")
        if meta.get("file") and path.is_file():
            return path, meta.get("type", "image/png")
        return None

    def version(self) -> str | None:
        """Changes whenever the served image changes, for cache-busting."""
        cur = self.current()
        if cur is None:
            return None
        path, _ = cur
        stat = path.stat()
        return hashlib.sha1(f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest()[:10]

    def is_custom(self) -> bool:
        return self._custom() is not None
