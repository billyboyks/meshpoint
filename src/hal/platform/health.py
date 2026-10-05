"""Process-wide concentrator health, so "running" never hides a dead radio.

Written by the concentrator capture source; read by ``/api/device/status``,
``meshpoint status`` and the dashboard sidebar.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Optional

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


def _update(**fields) -> None:
    with _lock:
        _state.update(fields)
        _state["updated_at"] = datetime.now(timezone.utc).isoformat()


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
