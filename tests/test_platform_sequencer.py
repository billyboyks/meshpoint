"""Bobcat GPIO power/reset sequences, executed against a fake sysfs."""

from __future__ import annotations

import pytest

from src.hal.platform import profiles as P
from src.hal.platform import sequencer
from src.hal.platform.gpio import GpioError, SysfsGpio
from src.hal.platform.profiles import (
    BOBCAT_G285,
    BOBCAT_G29X,
    RASPBERRY_PI,
    GpioLine,
)

BASE = "/sys/class/gpio"


class FakeFs:
    """Just enough sysfs: export creates gpioN/, writes are recorded."""

    def __init__(self, with_export: bool = True, chips=None) -> None:
        self.exists_set: set[str] = set()
        self.values: dict[str, str] = {}
        self.ops: list[tuple[str, str]] = []
        self.chips = chips if chips is not None else [
            ("gpiochip0", 0, 32, "gpio0"), ("gpiochip96", 96, 32, "gpio3"),
            ("gpiochip128", 128, 32, "gpio4"),
        ]
        if with_export:
            self.exists_set |= {f"{BASE}/export", f"{BASE}/unexport", BASE}
        for name, base, n, label in self.chips:
            self.values[f"{BASE}/{name}/base"] = str(base)
            self.values[f"{BASE}/{name}/ngpio"] = str(n)
            self.values[f"{BASE}/{name}/label"] = label

    def exists(self, path: str) -> bool:
        return path in self.exists_set or path in self.values

    def write(self, path: str, data: str) -> None:
        self.ops.append((path, data))
        if path == f"{BASE}/export":
            self.exists_set.add(f"{BASE}/gpio{data}")
        elif path == f"{BASE}/unexport":
            self.exists_set.discard(f"{BASE}/gpio{data}")
        else:
            self.values[path] = data

    def read(self, path: str) -> str:
        return self.values[path]

    def listdir(self, path: str) -> list[str]:
        return sorted({p.split("/")[4] for p in self.values if "gpiochip" in p})

    # helpers for assertions
    def value_writes(self, n: int) -> list[str]:
        return [d for p, d in self.ops if p == f"{BASE}/gpio{n}/value"]

    def exported(self, n: int) -> bool:
        return f"{BASE}/gpio{n}" in self.exists_set


def execute(profile, mode="start", env=None):
    fs = FakeFs()
    slept: list[float] = []
    log = sequencer.run(
        profile, mode,
        options=sequencer.SequenceOptions.from_env(env or {}),
        gpio=SysfsGpio(fs=fs, sleep=lambda s: None),
        sleep=slept.append,
    )
    return fs, slept, log


# ── GPIO identity (derived, not guessed) ───────────────────────────

@pytest.mark.parametrize(
    "number,name",
    [(149, "GPIO4_C5"), (147, "GPIO4_C3"), (125, "GPIO3_D5"), (122, "GPIO3_D2")],
)
def test_rockchip_names_derived_from_sysfs_number(number, name):
    line = GpioLine("x", number, "reset", None, "")  # type: ignore[arg-type]
    assert line.rockchip_name == name


def test_g285_lines_are_the_reset_script_numbers():
    assert BOBCAT_G285.line("reset").number == 149
    assert BOBCAT_G285.line("power").number == 125
    assert BOBCAT_G285.line("extra").number == 122


# ── G285 sequence ──────────────────────────────────────────────────

def test_g285_start_power_cycles_then_pulses_active_high_reset():
    fs, slept, _ = execute(BOBCAT_G285)
    assert fs.value_writes(125) == ["0", "1"]
    assert fs.value_writes(122) == ["0", "1"]
    # reset: asserted HIGH then released LOW, and left LOW (chip runs)
    assert fs.value_writes(149) == ["1", "0"]
    assert fs.values[f"{BASE}/gpio149/direction"] == "out"
    # rails come up before reset is asserted
    order = [p for p, _ in fs.ops if p.endswith("/value")]
    assert order.index(f"{BASE}/gpio125/value") < order.index(f"{BASE}/gpio149/value")
    # 1 s rail-off time from reset_lgw.sh.bobcat is preserved
    assert 1.0 in slept
    assert slept.count(P.RESET_HOLD_SEC) == 2  # rail settle + reset assert


def test_g285_does_not_drive_unproven_pa_line_by_default():
    fs, _, _ = execute(BOBCAT_G285)
    assert not fs.exported(147)
    assert fs.value_writes(147) == []


def test_g285_pa_line_is_opt_in_and_set_before_reset():
    fs, _, _ = execute(BOBCAT_G285, env={"MESHPOINT_PA_GPIO": "147"})
    assert fs.value_writes(147) == ["1"]
    order = [p for p, _ in fs.ops if p.endswith("/value")]
    assert order.index(f"{BASE}/gpio147/value") < order.index(f"{BASE}/gpio149/value")


def test_pa_gpio_override_number_is_used():
    fs, _, _ = execute(BOBCAT_G285, env={"MESHPOINT_PA_GPIO": "99"})
    assert fs.value_writes(99) == ["1"]
    assert not fs.exported(147)


def test_g285_hold_asserts_reset_and_leaves_rails_alone():
    fs, _, _ = execute(BOBCAT_G285, mode="hold")
    assert fs.value_writes(149)[-1] == "1"
    assert fs.value_writes(125) == []
    assert fs.value_writes(122) == []


# ── G29x sequence = the validated G295 drop-in ─────────────────────

def test_g29x_matches_validated_dropin():
    fs, slept, _ = execute(BOBCAT_G29X)
    assert fs.value_writes(147) == ["1"]
    assert fs.value_writes(149) == ["0", "1", "0"]
    assert fs.value_writes(125) == [] and fs.value_writes(122) == []
    assert slept[-1] == P.POST_RESET_SETTLE_SEC


def test_g29x_pa_can_be_disabled():
    fs, _, _ = execute(BOBCAT_G29X, env={"MESHPOINT_PA_GPIO": "off"})
    assert not fs.exported(147)
    assert fs.value_writes(149) == ["0", "1", "0"]


def test_reset_hold_override_replaces_assert_duration():
    _, slept, _ = execute(BOBCAT_G285, env={"CONCENTRATOR_RESET_HOLD_SEC": "0.75"})
    assert 0.75 in slept
    assert slept.count(P.RESET_HOLD_SEC) == 1  # only the rail settle remains


# ── failure modes are loud ─────────────────────────────────────────

def test_missing_sysfs_gpio_error_names_the_unknown():
    fs = FakeFs(with_export=False)
    with pytest.raises(GpioError, match="CONFIG_GPIO_SYSFS"):
        sequencer.run(
            BOBCAT_G285, "start", gpio=SysfsGpio(fs=fs, sleep=lambda s: None),
            sleep=lambda s: None,
        )


def test_failure_mentions_profile_and_line():
    fs = FakeFs()
    fs.write = lambda path, data: (_ for _ in ()).throw(OSError("denied"))  # type: ignore
    with pytest.raises(GpioError, match="bobcat_g285 start sequence failed at power"):
        sequencer.run(
            BOBCAT_G285, "start", gpio=SysfsGpio(fs=fs, sleep=lambda s: None),
            sleep=lambda s: None,
        )


def test_raspberry_pi_has_no_native_sequence():
    with pytest.raises(ValueError):
        sequencer.plan(RASPBERRY_PI, "start")


@pytest.mark.parametrize("bad", ["abc", "14.5"])
def test_bad_pa_env_is_rejected(bad):
    with pytest.raises(ValueError):
        sequencer.SequenceOptions.from_env({"MESHPOINT_PA_GPIO": bad})


def test_gpiochip_coverage_reports_unknown_numbering():
    fs = FakeFs(chips=[("gpiochip0", 0, 32, "gpio0")])
    g = SysfsGpio(fs=fs)
    assert g.chip_for(149) is None
    assert g.chip_for(5).label == "gpio0"
