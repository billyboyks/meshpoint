"""Query the running Meshpoint service and display health info."""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path

DASHBOARD_URL = "http://localhost:8080"
STATUS_ENDPOINT = f"{DASHBOARD_URL}/api/device/status"
DEVICE_ENDPOINT = f"{DASHBOARD_URL}/api/device"
LOCAL_CONFIG = Path("config/local.yaml")


def show_status() -> None:
    """Print a consolidated status report for the Meshpoint."""
    print()
    print("  Meshpoint Status")
    print("  " + "=" * 40)

    _show_service_state()
    _show_platform()
    _show_radio_health()
    _show_config_state()
    _show_api_status()

    print()


def _show_service_state() -> None:
    """Check systemd service state."""
    state = _systemctl_prop("ActiveState")
    sub_state = _systemctl_prop("SubState")

    if state == "active":
        label = f"running ({sub_state})"
    elif state == "inactive":
        label = "stopped"
    elif state == "failed":
        label = "FAILED"
    elif state is None:
        label = "not installed"
    else:
        label = f"{state} ({sub_state})"

    print(f"  Service:         {label}")

    if state == "active":
        pid = _systemctl_prop("MainPID")
        if pid and pid != "0":
            print(f"  PID:             {pid}")


def _show_platform() -> None:
    """Passive platform facts (no SPI or GPIO access)."""
    from src.hal.platform import get_detection

    det = get_detection()
    flag = "" if det.profile.supported else "  (UNSUPPORTED)"
    print(f"  Platform:        {det.profile.id} [{det.confidence}]{flag}")
    for warning in det.warnings:
        print(f"                   ! {warning}")


def _show_radio_health() -> None:
    """Radio health mirrored to disk by the service (no login needed)."""
    from src.hal.platform import health

    snap = health.read_snapshot_file()
    if snap is None:
        print("  Radio:           no data yet (service not started, or no concentrator)")
        return
    label = {
        "ok": "OK",
        "failed": "FAILED",
        "starting": "starting",
        "unconfigured": "no concentrator configured",
    }.get(snap.get("state", ""), snap.get("state", "unknown"))
    chip = f", SX1302 {snap['chip_version']}" if snap.get("chip_version") else ""
    spi = f" on {snap['spi_device']}" if snap.get("spi_device") else ""
    print(f"  Radio:           {label}{chip}{spi}")
    if snap.get("error"):
        print(f"                   ! {snap['error']}")
    if snap.get("state") == "failed":
        print("  Overall:         DEGRADED (service up, concentrator not running)")
    if snap.get("updated_at"):
        print(f"  Radio updated:   {snap['updated_at']}")


def _show_config_state() -> None:
    """Check whether local.yaml exists."""
    if LOCAL_CONFIG.exists():
        print(f"  Config:          {LOCAL_CONFIG}")
    else:
        print("  Config:          NOT configured (run 'meshpoint setup')")


def _show_api_status() -> None:
    """Query the running service's status API."""
    try:
        req = urllib.request.Request(STATUS_ENDPOINT, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            print(f"  API:             up, login required (HTTP {exc.code}); "
                  f"open {DASHBOARD_URL} in a browser for details")
        else:
            print(f"  API:             error (HTTP {exc.code})")
        return
    except Exception:
        print("  API:             unreachable")
        return

    uptime = timedelta(seconds=data.get("uptime_seconds", 0))
    device_id = data.get("device_id", "unknown")
    ws_clients = data.get("websocket_clients", 0)

    print(f"  Uptime:          {_format_uptime(uptime)}")
    print(f"  Device ID:       {device_id}")
    print(f"  Dashboard:       {DASHBOARD_URL}")
    print(f"  WS clients:      {ws_clients}")

    relay = data.get("relay", {})
    if relay:
        enabled = relay.get("enabled", False)
        relayed = relay.get("relayed", 0)
        rejected = relay.get("rejected", 0)
        print(f"  Relay:           {'enabled' if enabled else 'disabled'} "
              f"(relayed={relayed}, rejected={rejected})")

    _show_device_info()


def _show_device_info() -> None:
    """Fetch device identity details."""
    try:
        req = urllib.request.Request(DEVICE_ENDPOINT, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            device = json.loads(resp.read().decode())
    except Exception:
        return

    name = device.get("device_name", "")
    lat = device.get("latitude")
    lon = device.get("longitude")

    if name:
        print(f"  Name:            {name}")
    if lat is not None and lon is not None:
        print(f"  Location:        {lat}, {lon}")


def _format_uptime(td: timedelta) -> str:
    """Format a timedelta into a readable string."""
    total_seconds = int(td.total_seconds())
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60

    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def _systemctl_prop(prop: str) -> str | None:
    """Read a single systemd property for the meshpoint service."""
    try:
        result = subprocess.run(
            ["systemctl", "show", "meshpoint", f"--property={prop}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            line = result.stdout.strip()
            if "=" in line:
                return line.split("=", 1)[1]
    except Exception:
        pass
    return None
