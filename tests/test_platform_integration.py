"""Wiring: wrapper reset, source preflight/health, scripts, installer."""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from src.hal.platform import health
from src.hal.platform.chip_probe import ConcentratorHardwareError
from src.hal.platform.cli import parse_mesh_header
from src.hal.platform.profiles import BOBCAT_G285, RASPBERRY_PI
from src.hal.sx1302_wrapper import SX1302Wrapper

REPO = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


# ── SX1302Wrapper.reset ────────────────────────────────────────────

def test_reset_on_pi_still_uses_pinctrl_17_and_25():
    wrapper = SX1302Wrapper(lib_path="/nonexistent")
    with mock.patch("src.hal.platform.get_active_profile", return_value=RASPBERRY_PI), \
            mock.patch("subprocess.run") as run, \
            mock.patch("time.sleep"):
        wrapper.reset()
    cmds = [c.args[0] for c in run.call_args_list]
    assert ["pinctrl", "set", "17", "op", "dh"] in cmds
    assert ["pinctrl", "set", "25", "op", "dl"] in cmds


def test_reset_on_bobcat_as_root_runs_the_profile_sequence():
    wrapper = SX1302Wrapper(lib_path="/nonexistent")
    with mock.patch("src.hal.platform.get_active_profile", return_value=BOBCAT_G285), \
            mock.patch("os.geteuid", create=True, return_value=0), \
            mock.patch("src.hal.platform.sequencer.run") as run, \
            mock.patch("subprocess.run") as sub:
        wrapper.reset()
    assert run.call_args.args[:2] == (BOBCAT_G285, "start")
    sub.assert_not_called()  # no pinctrl on Bobcat


def test_reset_on_bobcat_as_service_user_goes_through_sudo_script():
    wrapper = SX1302Wrapper(lib_path="/nonexistent")
    with mock.patch("src.hal.platform.get_active_profile", return_value=BOBCAT_G285), \
            mock.patch("os.geteuid", create=True, return_value=999), \
            mock.patch("subprocess.run") as sub:
        wrapper.reset()
    argv = sub.call_args.args[0]
    assert argv[:3] == ["sudo", "-n", "/bin/bash"]
    assert argv[3].endswith("reset_concentrator.sh")


def test_bobcat_reset_failure_is_logged_not_raised(caplog):
    wrapper = SX1302Wrapper(lib_path="/nonexistent")
    with mock.patch("src.hal.platform.get_active_profile", return_value=BOBCAT_G285), \
            mock.patch("os.geteuid", create=True, return_value=0), \
            mock.patch("src.hal.platform.sequencer.run", side_effect=OSError("denied")):
        wrapper.reset()
    assert "GPIO reset failed" in caplog.text


# ── capture source: preflight + health ─────────────────────────────

def make_source():
    from src.capture.concentrator_source import ConcentratorCaptureSource

    src = ConcentratorCaptureSource(spi_path="/dev/spidev1.0")
    src._wrapper = mock.Mock()
    return src


def test_start_fails_loudly_and_marks_radio_failed_when_chip_is_dead():
    health.reset_for_tests()
    src = make_source()
    dead = ConcentratorHardwareError("SX1302 not responding - 0x00")
    with mock.patch.object(src, "preflight", side_effect=dead):
        with pytest.raises(ConcentratorHardwareError):
            asyncio.run(src.start())
    snap = health.snapshot()
    assert snap["state"] == health.FAILED
    assert "0x00" in snap["error"]
    src._wrapper.start.assert_not_called()  # HAL never started on a dead bus
    assert not src.is_running


def test_start_marks_radio_ok_when_chip_answers():
    health.reset_for_tests()
    src = make_source()
    with mock.patch.object(src, "preflight"):
        asyncio.run(src.start())
    assert health.snapshot()["state"] == health.OK
    assert src.is_running
    src._wrapper.start.assert_called_once()


def test_unsupported_platform_refuses_to_start():
    from src.hal.platform.detect import Detection
    from src.hal.platform.profiles import BOBCAT_UNKNOWN

    health.reset_for_tests()
    src = make_source()
    det = Detection(BOBCAT_UNKNOWN, "autodetect", "low", [], ["set the model"])
    with mock.patch("src.hal.platform.get_detection", return_value=det):
        with pytest.raises(ConcentratorHardwareError, match="not supported"):
            asyncio.run(src.start())
    assert health.snapshot()["state"] == health.FAILED
    src._wrapper.load.assert_not_called()


def test_device_status_reports_degraded_when_radio_failed():
    pytest.importorskip("fastapi")
    from src.api.routes import device

    health.reset_for_tests()
    health.set_failed("SX1302 not responding")
    device._identity = mock.Mock(device_id="x")
    device._ws_manager = mock.Mock(client_count=0)
    device._relay_manager = None
    body = asyncio.run(device.device_status())
    assert body["status"] == "degraded"
    assert body["radio"]["state"] == "failed"
    assert body["radio"]["error"] == "SX1302 not responding"
    health.set_ok()
    assert asyncio.run(device.device_status())["status"] == "running"


# ── Meshtastic header (matches src/decode/meshtastic_decoder.py) ───

def test_parse_mesh_header_layout():
    import struct

    flags = (3 << 5) | 3 | 0x08  # hop_start 3, hop_limit 3, want_ack
    raw = struct.pack("<III", 0xFFFFFFFF, 0x12345678, 0xDEADBEEF) + bytes(
        [flags, 0x08, 0x00, 0x78]
    ) + b"payload"
    hdr = parse_mesh_header(raw)
    assert hdr["dest"] == "!ffffffff"
    assert hdr["sender"] == "!12345678"
    assert hdr["packet_id"] == "0xdeadbeef"
    assert (hdr["hop_limit"], hdr["hop_start"], hdr["want_ack"]) == (3, 3, True)
    assert hdr["channel_hash"] == "0x08"
    assert hdr["relay_node"] == "0x78"
    assert parse_mesh_header(b"short") is None


# ── scripts / installer invariants ─────────────────────────────────

def _executed_upgrades(text: str) -> list[str]:
    """Lines that actually run `apt-get upgrade` (not comments/echo text)."""
    return [
        ln for ln in text.splitlines()
        if re.match(r"\s*apt(-get)?\s+(full-|dist-)?upgrade", ln)
    ]


def test_installer_never_upgrades_on_bobcat():
    sh = read("scripts/install.sh")
    start = sh.index("apt-get update -qq")
    end = sh.index('info "Installing build tools')
    bob, _, other = sh[start:end].partition("\nelse\n")
    assert "bobcat_hold_kernel" in bob
    assert _executed_upgrades(bob) == []
    assert len(_executed_upgrades(other)) == 1
    assert len(_executed_upgrades(sh)) == 1  # nowhere else in the installer


def test_installer_guards_every_apt_install_on_bobcat():
    sh = read("scripts/install.sh")
    assert "apt-get -s install" in sh
    assert re.search(r"linux-image\|linux-dtb\|linux-u-boot", sh)
    assert "apt-get install -y -qq gpsd" not in sh  # goes through apt_install


def test_installer_holds_kernel_dtb_and_uboot_by_pattern():
    sh = read("scripts/install.sh")
    for pat in ("linux-image-*", "linux-dtb-*", "linux-u-boot-*"):
        assert pat in sh
    assert "apt-mark hold" in sh


def test_installer_adds_groups_individually():
    sh = read("scripts/install.sh")
    assert "usermod -a -G spi,gpio,dialout,i2c" not in sh


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
@pytest.mark.parametrize(
    "script", ["scripts/install.sh", "scripts/reset_concentrator.sh"]
)
def test_shell_scripts_parse(script):
    r = subprocess.run(
        ["bash", "-n"], input=read(script).encode("utf-8"), capture_output=True
    )
    assert r.returncode == 0, r.stderr.decode(errors="replace")


def test_reset_script_dispatches_bobcat_to_platform_layer_and_keeps_pi_path():
    sh = read("scripts/reset_concentrator.sh")
    assert "src.hal.platform detect --shell" in sh
    assert "bobcat_*)" in sh
    assert 'pinctrl set "$1" op dh' in sh  # Pi path untouched


def test_service_loads_platform_env_optionally():
    assert "EnvironmentFile=-/etc/meshpoint/platform.env" in read(
        "scripts/meshpoint.service"
    )


def test_sudoers_allows_exact_reset_commands_only():
    lines = [
        ln for ln in read("config/sudoers-meshpoint").splitlines()
        if "reset_concentrator.sh" in ln and not ln.startswith("#")
    ]
    assert len(lines) == 2
    assert all("*" not in ln for ln in lines)


# ── `meshpoint hwcheck --through chip` must reach the platform CLI ──

@pytest.mark.parametrize(
    "argv,expected",
    [
        (["hwcheck", "--through", "chip"], ["check", "--through", "chip"]),
        (["hwcheck", "--through", "rx", "--region", "US", "--seconds", "5"],
         ["check", "--through", "rx", "--region", "US", "--seconds", "5"]),
        (["hwcheck"], ["check"]),
        (["hwcheck", "detect", "--json"], ["detect", "--json"]),
    ],
)
def test_meshpoint_hwcheck_passes_options_through(argv, expected):
    from src.cli import main as cli_main

    with mock.patch("sys.argv", ["meshpoint", *argv]), \
            mock.patch("src.hal.platform.cli.main", return_value=0) as hw, \
            pytest.raises(SystemExit) as exit_info:
        cli_main.main()
    hw.assert_called_once_with(expected)
    assert exit_info.value.code == 0
