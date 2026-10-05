"""Explicit platform detection.

Rules (deliberately conservative - a wrong guess here drives the wrong
GPIOs):

1. An operator override always wins and is validated:
   ``MESHPOINT_PLATFORM`` in the environment or in
   ``/etc/meshpoint/platform.env``.
2. A Rockchip RK3566 device tree means "Bobcat-class host". The *model*
   is then taken only from explicit markers:
     - ``/etc/bobcat-version``  (Bobcat Debian image; 280 / 285 / 29x)
     - hostname ``bobcat-285`` / ``bobcat-29x`` (set by the Bobcat-Armbian
       images; both reference installers key on it)
     - a model number in ``/proc/device-tree/model`` (UNKNOWN whether
       the Bobcat DTS carries one - never required)
   Conflicting markers -> ``bobcat_unknown``.
3. Which ``/dev/spidev*`` nodes exist is NOT sufficient to pick a model
   (a G29x without the overlay could still expose another node). It is
   reported as supporting evidence and used for warnings only.
4. Anything that is not RK3566 keeps the legacy Raspberry Pi behaviour.
"""

from __future__ import annotations

import os
import platform as _stdlib_platform
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Mapping, Optional

from src.hal.platform.profiles import (
    BOBCAT_G280,
    BOBCAT_G285,
    BOBCAT_G29X,
    BOBCAT_UNKNOWN,
    PROFILES,
    RASPBERRY_PI,
    SELECTABLE_IDS,
    PlatformProfile,
)

ENV_PLATFORM = "MESHPOINT_PLATFORM"
PLATFORM_ENV_FILE = "etc/meshpoint/platform.env"

_MODEL_BY_TAG = {
    "280": BOBCAT_G280,
    "285": BOBCAT_G285,
    "29x": BOBCAT_G29X,
}


@dataclass
class Detection:
    profile: PlatformProfile
    source: str                      # "override" | "autodetect"
    confidence: str                  # "explicit" | "high" | "medium" | "low"
    evidence: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    spidev_nodes: list[str] = field(default_factory=list)

    @property
    def platform_id(self) -> str:
        return self.profile.id

    @property
    def is_bobcat(self) -> bool:
        return self.profile.id.startswith("bobcat_")


def _read(root: Path, rel: str) -> Optional[str]:
    try:
        return (root / rel).read_bytes().decode("utf-8", "replace")
    except OSError:
        return None


def parse_env_file(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip("\"'")
    return out


def effective_env(
    root: Path | str = "/", env: Mapping[str, str] | None = None
) -> dict[str, str]:
    """platform.env values, overlaid by the real environment."""
    root = Path(root)
    merged: dict[str, str] = {}
    text = _read(root, PLATFORM_ENV_FILE)
    if text:
        merged.update(parse_env_file(text))
    merged.update(dict(os.environ if env is None else env))
    return merged


def _classify_tag(text: str) -> Optional[str]:
    """Map '285', '29x', 'g295', 'bobcat-285'... to a model tag."""
    t = text.strip().lower()
    m = re.search(r"(?<![0-9a-z])g?(280|285|29[0-9x])(?![0-9a-z])", t)
    if not m:
        return None
    tag = m.group(1)
    return "29x" if tag.startswith("29") else tag


def _spidev_nodes(root: Path) -> list[str]:
    dev = root / "dev"
    try:
        return sorted(
            f"/dev/{p.name}" for p in dev.iterdir() if p.name.startswith("spidev")
        )
    except OSError:
        return []


def detect_platform(
    root: Path | str = "/",
    env: Mapping[str, str] | None = None,
    machine: str | None = None,
) -> Detection:
    root = Path(root)
    eff = effective_env(root, env)
    machine = machine or _stdlib_platform.machine()
    nodes = _spidev_nodes(root)

    override = (eff.get(ENV_PLATFORM) or "").strip().lower()
    if override:
        if override not in SELECTABLE_IDS:
            det = Detection(
                profile=BOBCAT_UNKNOWN,
                source="override",
                confidence="explicit",
                evidence=[f"{ENV_PLATFORM}={override!r}"],
                warnings=[
                    f"{ENV_PLATFORM}={override!r} is not valid; "
                    f"choose one of {', '.join(SELECTABLE_IDS)}"
                ],
                spidev_nodes=nodes,
            )
            return det
        det = Detection(
            profile=PROFILES[override],
            source="override",
            confidence="explicit",
            evidence=[f"{ENV_PLATFORM}={override}"],
            spidev_nodes=nodes,
        )
        _warn_spi_mismatch(det)
        return det

    compatible = (_read(root, "proc/device-tree/compatible") or "").split("\x00")
    model = (_read(root, "proc/device-tree/model") or "").strip("\x00 \n")
    evidence: list[str] = []
    warnings: list[str] = []

    is_rk3566 = any(c.strip() == "rockchip,rk3566" for c in compatible)
    if not is_rk3566:
        if any("raspberrypi" in c for c in compatible) or "Raspberry Pi" in model:
            return Detection(
                RASPBERRY_PI, "autodetect", "high",
                [f"device-tree model: {model or 'raspberrypi'}"],
                spidev_nodes=nodes,
            )
        return Detection(
            RASPBERRY_PI, "autodetect", "low",
            ["no RK3566 or Raspberry Pi device tree found; "
             "keeping legacy Raspberry Pi behaviour"],
            spidev_nodes=nodes,
        )

    evidence.append("device-tree compatible includes rockchip,rk3566")
    if model:
        evidence.append(f"device-tree model: {model}")
    if machine not in ("aarch64", "arm64"):
        warnings.append(f"expected aarch64, found {machine!r}")

    tags: dict[str, str] = {}
    bv = _read(root, "etc/bobcat-version")
    if bv and _classify_tag(bv):
        tags["/etc/bobcat-version"] = _classify_tag(bv)  # type: ignore[assignment]
    hostname = (_read(root, "etc/hostname") or "").strip()
    if hostname.lower().startswith("bobcat") and _classify_tag(hostname):
        tags["hostname"] = _classify_tag(hostname)  # type: ignore[assignment]
    if model and _classify_tag(model):
        tags["device-tree model"] = _classify_tag(model)  # type: ignore[assignment]

    for src, tag in tags.items():
        evidence.append(f"{src} => model {tag}")
    if nodes:
        evidence.append("spidev nodes: " + ", ".join(nodes))

    distinct = set(tags.values())
    if len(distinct) == 1:
        tag = next(iter(distinct))
        strong = any(k in tags for k in ("/etc/bobcat-version", "hostname"))
        det = Detection(
            _MODEL_BY_TAG[tag], "autodetect",
            "high" if strong else "medium",
            evidence, warnings, nodes,
        )
        _warn_spi_mismatch(det)
        return det

    if len(distinct) > 1:
        warnings.append(
            "conflicting model markers: "
            + ", ".join(f"{k}={v}" for k, v in tags.items())
        )
    else:
        warnings.append(
            "no model marker found (/etc/bobcat-version, hostname bobcat-285 / "
            "bobcat-29x). Refusing to guess GPIOs from the SPI node alone."
        )
        if "/dev/spidev1.0" in nodes and "/dev/spidev5.0" not in nodes:
            warnings.append("only /dev/spidev1.0 present: G285-shaped")
        elif "/dev/spidev5.0" in nodes and "/dev/spidev1.0" not in nodes:
            warnings.append("only /dev/spidev5.0 present: G29x-shaped")
    warnings.append(
        "set it explicitly: MESHPOINT_PLATFORM=bobcat_g285 (or bobcat_g29x) "
        "in /etc/meshpoint/platform.env"
    )
    return Detection(BOBCAT_UNKNOWN, "autodetect", "low", evidence, warnings, nodes)


def _warn_spi_mismatch(det: Detection) -> None:
    want = det.profile.spi_device
    if want and det.spidev_nodes and want not in det.spidev_nodes:
        det.warnings.append(
            f"{det.profile.id} expects {want} but found: "
            f"{', '.join(det.spidev_nodes)}"
        )
    elif want and not det.spidev_nodes:
        det.warnings.append(
            f"{want} not present (SPI not enabled yet? see docs/BOBCAT-G285.md)"
        )


@lru_cache(maxsize=1)
def get_detection() -> Detection:
    return detect_platform()


def get_active_profile() -> PlatformProfile:
    return get_detection().profile


def clear_cache() -> None:
    get_detection.cache_clear()


def resolve_spi_device(configured: str | None) -> str:
    """Resolve ``capture.concentrator_spi_device``.

    ``"auto"`` (or empty) means "the active platform's SPI node"; any
    other value is used verbatim. There is no silent remapping: a stale
    ``/dev/spidev0.0`` on a Bobcat fails the chip preflight with a clear
    message rather than being rewritten behind the operator's back.
    """
    value = (configured or "").strip()
    if value and value.lower() != "auto":
        return value
    det = get_detection()
    if det.profile.spi_device:
        return det.profile.spi_device
    if det.profile.supported:
        return "/dev/spidev0.0"
    raise ValueError(
        f"capture.concentrator_spi_device is 'auto' but platform "
        f"{det.profile.id} has no known SPI device: "
        + "; ".join(det.warnings or det.profile.notes)
    )
