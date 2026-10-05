"""Platform abstraction: detection, GPIO sequencing, SX1302 chip probe.

Stdlib-only on purpose, so ``python -m src.hal.platform check`` works on
a freshly flashed Armbian before Meshpoint's virtualenv exists.
"""

from src.hal.platform.chip_probe import (
    ConcentratorHardwareError,
    ProbeResult,
    ProbeState,
    probe_chip,
    require_chip,
)
from src.hal.platform.detect import (
    Detection,
    detect_platform,
    effective_env,
    get_active_profile,
    get_detection,
)
from src.hal.platform.profiles import PROFILES, PlatformProfile, get_profile

__all__ = [
    "ConcentratorHardwareError",
    "Detection",
    "PROFILES",
    "PlatformProfile",
    "ProbeResult",
    "ProbeState",
    "detect_platform",
    "effective_env",
    "get_active_profile",
    "get_detection",
    "get_profile",
    "probe_chip",
    "require_chip",
]
