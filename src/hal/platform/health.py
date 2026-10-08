"""Process-wide concentrator health, so "running" never hides a dead radio.

Written by the concentrator capture source; read by ``/api/device/status``,
``meshpoint status`` and the dashboard sidebar.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ENV_HEALTH_FILE = "MESHPOINT_HEALTH_FILE"
DEFAULT_HEALTH_FILE = "data/radio_health.json"

UNCONFIGURED = "unconfigured"  # no concentrator source in this config
STARTING = "starting"
OK = "ok"
FAILED = "failed"

_lock = threading.Lock()
_state: dict = {
    "state": UNCONFIGURED,
    "error": None,
    "platform": None,
    "spi_device": None,
    "chip_version": None,
    "updated_at": None,
}


def health_file_path() -> Path:
    """Where the snapshot is mirrored (relative to the service cwd)."""
    return Path(os.environ.get(ENV_HEALTH_FILE) or DEFAULT_HEALTH_FILE)


def _write_snapshot(snapshot: dict) -> None:
    """Mirror the state to disk so `meshpoint status` works without a login.

    The dashboard API is behind authentication, so the CLI cannot read
    ``/api/device/status``. Best effort: never let this break the radio.
    """
    path = health_file_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({**snapshot, "pid": os.getpid()}), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def read_snapshot_file(path: Path | None = None) -> Optional[dict]:
    """Last snapshot written by the service, or None."""
    try:
        return json.loads((path or health_file_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _update(**fields) -> None:
    with _lock:
        _state.update(fields)
        _state["updated_at"] = datetime.now(timezone.utc).isoformat()
        snapshot = dict(_state)
    _write_snapshot(snapshot)


def set_starting(spi_device: str, platform: Optional[str] = None) -> None:
    _update(state=STARTING, error=None, spi_device=spi_device, platform=platform)


def set_chip_version(version: Optional[int]) -> None:
    _update(chip_version=None if version is None else f"0x{version:02X}")


def set_ok() -> None:
    _update(state=OK, error=None)


def set_failed(error: str) -> None:
    _update(state=FAILED, error=error)


def snapshot() -> dict:
    with _lock:
        return dict(_state)


def reset_for_tests() -> None:
    with _lock:
        _state.update(
            state=UNCONFIGURED, error=None, platform=None,
            spi_device=None, chip_version=None, updated_at=None,
        )
