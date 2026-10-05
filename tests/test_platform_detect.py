"""Platform detection: explicit, conservative, never guesses GPIOs."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.hal.platform import detect
from src.hal.platform.profiles import (
    BOBCAT_G280,
    BOBCAT_G285,
    BOBCAT_G29X,
    BOBCAT_UNKNOWN,
    RASPBERRY_PI,
)

RK3566 = "rockchip,rk3566"


def make_root(
    tmp_path: Path,
    *,
    compatible: str | None = RK3566,
    model: str | None = None,
    bobcat_version: str | None = None,
    hostname: str | None = None,
    spidev: tuple[str, ...] = (),
    platform_env: str | None = None,
) -> Path:
    root = tmp_path
    (root / "proc/device-tree").mkdir(parents=True)
    (root / "etc/meshpoint").mkdir(parents=True)
    (root / "dev").mkdir()
    if compatible is not None:
        (root / "proc/device-tree/compatible").write_bytes(
            compatible.encode() + b"\x00"
        )
    if model is not None:
        (root / "proc/device-tree/model").write_bytes(model.encode() + b"\x00")
    if bobcat_version is not None:
        (root / "etc/bobcat-version").write_text(bobcat_version + "\n")
    if hostname is not None:
        (root / "etc/hostname").write_text(hostname + "\n")
    for node in spidev:
        (root / "dev" / node).write_text("")
    if platform_env is not None:
        (root / "etc/meshpoint/platform.env").write_text(platform_env)
    return root


def run(root: Path, env: dict[str, str] | None = None, machine="aarch64"):
    return detect.detect_platform(root=root, env=env or {}, machine=machine)


def test_bobcat_version_file_selects_g285(tmp_path):
    det = run(make_root(tmp_path, bobcat_version="285", spidev=("spidev1.0",)))
    assert det.profile is BOBCAT_G285
    assert det.confidence == "high"
    assert det.source == "autodetect"
    assert det.warnings == []


def test_hostname_selects_g285(tmp_path):
    det = run(make_root(tmp_path, hostname="bobcat-285", spidev=("spidev1.0",)))
    assert det.profile is BOBCAT_G285


@pytest.mark.parametrize("host", ["bobcat-29x", "bobcat-g295", "bobcat-290"])
def test_hostname_selects_g29x(tmp_path, host):
    det = run(make_root(tmp_path, hostname=host, spidev=("spidev5.0",)))
    assert det.profile is BOBCAT_G29X


def test_g280_is_recognised_and_unsupported(tmp_path):
    det = run(make_root(tmp_path, bobcat_version="280"))
    assert det.profile is BOBCAT_G280
    assert not det.profile.supported


def test_conflicting_markers_do_not_guess(tmp_path):
    root = make_root(tmp_path, bobcat_version="285", hostname="bobcat-29x")
    det = run(root)
    assert det.profile is BOBCAT_UNKNOWN
    assert any("conflicting" in w for w in det.warnings)


def test_spidev_alone_is_not_enough_to_pick_a_model(tmp_path):
    det = run(make_root(tmp_path, spidev=("spidev1.0",), hostname="armbian"))
    assert det.profile is BOBCAT_UNKNOWN
    assert any("Refusing to guess" in w for w in det.warnings)
    assert any("G285-shaped" in w for w in det.warnings)
    assert not det.profile.supported


def test_device_tree_model_string_is_medium_confidence(tmp_path):
    det = run(make_root(tmp_path, model="Bobcat Miner 300 G285"))
    assert det.profile is BOBCAT_G285
    assert det.confidence == "medium"


def test_rk3566_board_number_in_model_is_not_a_bobcat_tag(tmp_path):
    det = run(make_root(tmp_path, model="Rockchip RK3566 EVB2 LP4X V10 Board"))
    assert det.profile is BOBCAT_UNKNOWN


def test_env_override_wins_and_is_explicit(tmp_path):
    root = make_root(tmp_path, bobcat_version="285")
    det = run(root, env={"MESHPOINT_PLATFORM": "bobcat_g29x"})
    assert det.profile is BOBCAT_G29X
    assert det.source == "override"
    assert det.confidence == "explicit"


def test_platform_env_file_override_and_env_beats_file(tmp_path):
    root = make_root(
        tmp_path, platform_env="# pinned\nMESHPOINT_PLATFORM=bobcat_g285\n"
    )
    assert run(root).profile is BOBCAT_G285
    assert (
        run(root, env={"MESHPOINT_PLATFORM": "raspberry_pi"}).profile
        is RASPBERRY_PI
    )


def test_invalid_override_is_rejected_loudly(tmp_path):
    det = run(make_root(tmp_path), env={"MESHPOINT_PLATFORM": "bobcat_g295"})
    assert det.profile is BOBCAT_UNKNOWN
    assert any("not valid" in w for w in det.warnings)


def test_override_warns_when_spi_node_missing(tmp_path):
    det = run(
        make_root(tmp_path, spidev=("spidev5.0",)),
        env={"MESHPOINT_PLATFORM": "bobcat_g285"},
    )
    assert det.profile is BOBCAT_G285
    assert any("expects /dev/spidev1.0" in w for w in det.warnings)


def test_raspberry_pi_model_is_high_confidence(tmp_path):
    root = make_root(
        tmp_path,
        compatible="raspberrypi,4-model-b\x00brcm,bcm2711",
        model="Raspberry Pi 4 Model B Rev 1.4",
    )
    det = run(root)
    assert det.profile is RASPBERRY_PI
    assert det.confidence == "high"


def test_unknown_host_keeps_legacy_pi_behaviour(tmp_path):
    det = run(make_root(tmp_path, compatible=None))
    assert det.profile is RASPBERRY_PI
    assert det.confidence == "low"


def test_non_aarch64_bobcat_warns(tmp_path):
    det = run(make_root(tmp_path, bobcat_version="285"), machine="armv7l")
    assert det.profile is BOBCAT_G285
    assert any("aarch64" in w for w in det.warnings)


@pytest.mark.parametrize(
    "configured,expected",
    [("/dev/spidev3.1", "/dev/spidev3.1"), ("auto", "/dev/spidev1.0"),
     ("", "/dev/spidev1.0")],
)
def test_resolve_spi_device(monkeypatch, tmp_path, configured, expected):
    det = run(make_root(tmp_path, bobcat_version="285"))
    monkeypatch.setattr(detect, "get_detection", lambda: det)
    assert detect.resolve_spi_device(configured) == expected


def test_resolve_spi_device_auto_refuses_unsupported(monkeypatch, tmp_path):
    det = run(make_root(tmp_path))
    monkeypatch.setattr(detect, "get_detection", lambda: det)
    with pytest.raises(ValueError, match="no known SPI device"):
        detect.resolve_spi_device("auto")
