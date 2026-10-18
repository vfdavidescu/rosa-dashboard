"""Logging: to the console (docker logs) and to a rotating file in LOG_DIR.

The file is what you open when something goes wrong: rosa.log, plus up to
five older rosa.log.1 … rosa.log.5 (5 MB each).

pytoyoda logs through loguru, and at DEBUG it writes full login request
bodies (your password and access tokens). It is capped at TOYOTA_LOG_LEVEL
(default WARNING) for both the console and the file.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from loguru import logger as toyota_logger

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup() -> Path | None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    toyota_level = os.environ.get("TOYOTA_LOG_LEVEL", "WARNING").upper()
    log_dir = Path(os.environ.get("LOG_DIR") or Path(os.environ.get("DATA_DIR", "./data")) / "logs")

    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter(FORMAT)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    root.handlers = [console]

    log_file: Path | None = log_dir / "rosa.log"
    file_handler = None
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as exc:
        # Never stop the app because the log folder isn't writable.
        root.warning("file logging disabled, can't write to %s: %s", log_dir, exc)
        log_file = None

    # uvicorn's own loggers don't pass through root by default.
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    # Access lines for every 60s page poll would bury everything else.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # pytoyoda (loguru) -> same console and file, capped (see module docstring).
    toyota_logger.remove()
    toyota_fmt = "{time:YYYY-MM-DD HH:mm:ss,SSS} {level: <7} pytoyoda: {message}"
    toyota_logger.add(sys.stderr, level=toyota_level, format=toyota_fmt)
    if log_file is not None:
        toyota_logger.add(log_file.with_name("pytoyoda.log"), level=toyota_level, format=toyota_fmt,
                          rotation="5 MB", retention=5, encoding="utf-8")

    if log_file is not None:
        root.info("logging to %s", log_file)
    return log_file
