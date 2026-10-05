"""Hardware platform profiles: the single place platform facts live.

A profile answers, for one physical host + concentrator carrier:
  * which spidev node the SX1302 is on
  * which GPIOs power / reset / enable it, and in which order
  * whether Meshpoint can drive that GPIO itself (``native_gpio``)

Everything else in Meshpoint asks the *active profile* instead of
branching on "is this a Bobcat". Add a new board by adding a profile.

Evidence levels (``Evidence``) are recorded next to every hardware fact
so the diagnostics and docs can state honestly what is proven and what
still needs a physical unit:

  SOURCE   - read directly from a repository file (cited in ``source``)
  FIELD    - community field report (Meshpoint docs/BOBCAT-300.md, G295)
  DERIVED  - computed from SOURCE facts (e.g. Rockchip bank/pin names)
  UNKNOWN  - not established; requires physical G285 validation
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Evidence(str, Enum):
    SOURCE = "source"
    FIELD = "field-report"
    DERIVED = "derived"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class GpioLine:
    """One GPIO line, addressed by its global (sysfs) number.

    On Rockchip, sysfs number = bank * 32 + group * 8 + pin, with
    group A=0..D=3. ``bank`` / ``rockchip_name`` are derived from that.
    """

    name: str
    number: int
    role: str  # "reset" | "power" | "pa_enable"
    evidence: Evidence
    source: str

    @property
    def bank(self) -> int:
        return self.number // 32

    @property
    def offset(self) -> int:
        return self.number % 32

    @property
    def rockchip_name(self) -> str:
        group = "ABCD"[self.offset // 8]
        return f"GPIO{self.bank}_{group}{self.offset % 8}"


class Op(str, Enum):
    OUT = "out"          # export (if needed) and set direction=out
    SET = "set"          # write value
    RELEASE = "release"  # direction=in and unexport
    SLEEP = "sleep"


@dataclass(frozen=True)
class Step:
    op: Op
    line: Optional[str] = None  # GpioLine.name
    value: Optional[int] = None
    seconds: float = 0.0


def _out(line: str) -> Step:
    return Step(Op.OUT, line=line)


def _set(line: str, value: int) -> Step:
    return Step(Op.SET, line=line, value=value)


def _sleep(seconds: float) -> Step:
    return Step(Op.SLEEP, seconds=seconds)


# Timings. ``hold`` mirrors CONCENTRATOR_RESET_HOLD_SEC on the Pi path.
RESET_HOLD_SEC = 0.3
POST_RESET_SETTLE_SEC = 1.5
POWER_CYCLE_OFF_SEC = 1.0
POWER_CYCLE_ON_SETTLE_SEC = 0.3


@dataclass(frozen=True)
class PlatformProfile:
    id: str
    label: str
    hardware_description: str
    supported: bool
    # spidev node of the SX1302 (None => keep the user's configured value)
    spi_device: Optional[str] = None
    # "native": Meshpoint drives the lines below via sysfs GPIO.
    # "pinctrl": legacy Raspberry Pi path (scripts/reset_concentrator.sh).
    gpio_mode: str = "pinctrl"
    lines: tuple[GpioLine, ...] = ()
    # Sequence run before every concentrator start.
    start_steps: tuple[Step, ...] = ()
    # Sequence run on service stop: leave the chip held in reset.
    hold_steps: tuple[Step, ...] = ()
    # Optional lines (name -> reason) that are NOT driven unless enabled.
    optional_lines: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def native_gpio(self) -> bool:
        return self.gpio_mode == "native"

    def line(self, name: str) -> GpioLine:
        for ln in self.lines:
            if ln.name == name:
                return ln
        raise KeyError(f"{self.id}: no GPIO line named {name!r}")


# ── Raspberry Pi (legacy behaviour, unchanged) ───────────────────────

RASPBERRY_PI = PlatformProfile(
    id="raspberry_pi",
    label="Raspberry Pi + SX1302/SX1303 carrier",
    hardware_description="RAK2287 + Raspberry Pi 4",
    supported=True,
    spi_device=None,
    gpio_mode="pinctrl",
    notes=(
        "Reset is handled by scripts/reset_concentrator.sh (pinctrl/"
        "gpioset on BCM 17 and 25) and SX1302Wrapper.reset().",
    ),
)


# ── Bobcat Miner 300 G285 ────────────────────────────────────────────
#
# SOURCE facts (all in this workspace's cloned repos):
#   spidev1.0 ........ Bobcat300-TTN/install_ttn_udp.sh (bobcat-285 case),
#                      Bobcat-Armbian/install_helium.sh (bobcat-285 case)
#   GPIO 149/125/122 . Bobcat300-TTN/packet_forwarder/packet_forwarder/
#                      tools/reset_lgw.sh.bobcat (RESET / POWER / EXTRA),
#                      byte-identical to metrafonic/Bobcat300-DebianMinimalDocker
#                      and referenced by both the G285 and G29X compose files
#   active-high reset  reset_lgw.sh.bobcat: write 1 then 0, chip runs at 0
#
# The same reset script serves G285 and G29X, so the *GPIO numbers* are
# shared; what differs per model is the SPI node (and the I2C bus for
# the ATECC key chip, which Meshpoint does not use).
#
# 147 (PA enable) is a G295 FIELD claim (docs/BOBCAT-300.md). It does not
# appear in any G285 source and is therefore NOT driven on G285 unless
# the operator opts in (MESHPOINT_PA_GPIO=147). See docs/BOBCAT-G285.md.

_RESET = GpioLine(
    "reset", 149, "reset", Evidence.SOURCE,
    "Bobcat300-TTN reset_lgw.sh.bobcat DEFAULT_RESET_PIN=149",
)
_POWER = GpioLine(
    "power", 125, "power", Evidence.SOURCE,
    "Bobcat300-TTN reset_lgw.sh.bobcat POWER_GPIO=125 (name 'power' is the "
    "script's; the physical rail it gates is UNKNOWN)",
)
_EXTRA = GpioLine(
    "extra", 122, "power", Evidence.SOURCE,
    "Bobcat300-TTN reset_lgw.sh.bobcat EXTRA_GPIO=122 (function UNKNOWN; "
    "script credits r00t1ng with the 'missing LDO' fix)",
)
_PA_G285 = GpioLine(
    "pa_enable", 147, "pa_enable", Evidence.UNKNOWN,
    "Meshpoint docs/BOBCAT-300.md (G295 field report). Not in any G285 "
    "source. Opt-in only on G285.",
)
_PA_G29X = GpioLine(
    "pa_enable", 147, "pa_enable", Evidence.FIELD,
    "Meshpoint docs/BOBCAT-300.md Step 6, validated on G295 (TX+RX)",
)

_G285_RESET_PULSE = (
    _out("reset"),
    _set("reset", 1),
    _sleep(RESET_HOLD_SEC),
    _set("reset", 0),
    _sleep(POST_RESET_SETTLE_SEC),
)

BOBCAT_G285 = PlatformProfile(
    id="bobcat_g285",
    label="Bobcat Miner 300 G285 (RK3566, SD-boot Armbian)",
    hardware_description="Bobcat Miner 300 G285 (SX1302, RK3566)",
    supported=True,
    spi_device="/dev/spidev1.0",
    gpio_mode="native",
    lines=(_RESET, _POWER, _EXTRA, _PA_G285),
    start_steps=(
        # Power-cycle the concentrator rails first (reset_lgw.sh.bobcat
        # `start`: power+extra low for 1 s, then high), which also lets a
        # latched SX1302 recover after a hard power loss.
        _out("power"),
        _out("extra"),
        _set("power", 0),
        _set("extra", 0),
        _sleep(POWER_CYCLE_OFF_SEC),
        _set("power", 1),
        _set("extra", 1),
        _sleep(POWER_CYCLE_ON_SETTLE_SEC),
        # Active-high reset pulse (HIGH = held in reset).
        *_G285_RESET_PULSE,
    ),
    hold_steps=(
        # Assert reset and leave it asserted; rails are left alone so a
        # `systemctl restart` does not drop power twice.
        _out("reset"),
        _set("reset", 1),
    ),
    optional_lines=("pa_enable",),
    notes=(
        "Optional PA line 147 is NOT driven by default (no G285 evidence).",
    ),
)


# ── Bobcat Miner 300 G290 / G295 ─────────────────────────────────────
#
# Reproduces, in code, the manual systemd drop-in that the Meshpoint
# community validated on a G295 (docs/BOBCAT-300.md Step 6): SPI on
# spidev5.0 via the Armbian `spi5-m1` overlay, 147 high, 149 pulsed.

BOBCAT_G29X = PlatformProfile(
    id="bobcat_g29x",
    label="Bobcat Miner 300 G290/G295 (RK3566, eMMC Armbian)",
    hardware_description="Bobcat Miner 300 G29x (SX1302, RK3566)",
    supported=True,
    spi_device="/dev/spidev5.0",
    gpio_mode="native",
    lines=(_RESET, _PA_G29X),
    start_steps=(
        _out("pa_enable"),
        _set("pa_enable", 1),
        _out("reset"),
        _set("reset", 0),
        _sleep(0.3),
        _set("reset", 1),
        _sleep(RESET_HOLD_SEC),
        _set("reset", 0),
        _sleep(POST_RESET_SETTLE_SEC),
    ),
    hold_steps=(
        _out("reset"),
        _set("reset", 1),
    ),
    notes=(
        "Sequence copied from the validated G295 drop-in; 125/122 are not "
        "driven because the validated recipe does not use them.",
    ),
)


# ── Bobcat G280: SX1301, not an SX1302 ───────────────────────────────

BOBCAT_G280 = PlatformProfile(
    id="bobcat_g280",
    label="Bobcat Miner 300 G280 (SX1301)",
    hardware_description="Bobcat Miner 300 G280 (SX1301)",
    supported=False,
    notes=(
        "G280 uses an SX1301 concentrator (heliumdiy/Bobcat300-"
        "DebianMinimalDocker README: 'SX1301 Packet Forwarder for G280'). "
        "Meshpoint's HAL is SX1302-only. Not supported.",
    ),
)

# Detected as a Bobcat-class RK3566 host but the model could not be
# established without guessing.
BOBCAT_UNKNOWN = PlatformProfile(
    id="bobcat_unknown",
    label="Bobcat-class RK3566 host (model not established)",
    hardware_description="RK3566 host (Bobcat model unknown)",
    supported=False,
    notes=(
        "Set the model explicitly: MESHPOINT_PLATFORM=bobcat_g285 "
        "(or bobcat_g29x) in /etc/meshpoint/platform.env.",
    ),
)


PROFILES: dict[str, PlatformProfile] = {
    p.id: p
    for p in (
        RASPBERRY_PI,
        BOBCAT_G285,
        BOBCAT_G29X,
        BOBCAT_G280,
        BOBCAT_UNKNOWN,
    )
}

# Ids an operator may force. 'unknown' and 'g280' are results, not choices.
SELECTABLE_IDS = ("raspberry_pi", "bobcat_g285", "bobcat_g29x")


def get_profile(profile_id: str) -> PlatformProfile:
    try:
        return PROFILES[profile_id]
    except KeyError as exc:
        raise ValueError(
            f"Unknown platform {profile_id!r}; "
            f"choose one of {', '.join(SELECTABLE_IDS)}"
        ) from exc
