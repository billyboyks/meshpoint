"""Minimal sysfs GPIO access, injectable for tests.

Why sysfs: it is the interface the G295 community validation (Armbian
6.18.4-current-rockchip64) and the G285 TTN reset script both use, and
exported lines keep their value after the process exits, which the
"hold the chip in reset on shutdown" behaviour depends on.

UNKNOWN - requires physical G285 validation: that the G285 Armbian image
ships ``CONFIG_GPIO_SYSFS``. ``describe_environment()`` and
``meshpoint hwcheck`` report this explicitly instead of assuming it. The
GPIO character device (libgpiod) is not implemented here because its
lines are released when the owning process exits, which would let the
reset line float on service stop.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Protocol

SYSFS_GPIO = "/sys/class/gpio"


class GpioError(RuntimeError):
    """A GPIO operation failed (message is operator-facing)."""


class _Fs(Protocol):
    def exists(self, path: str) -> bool: ...
    def write(self, path: str, data: str) -> None: ...
    def read(self, path: str) -> str: ...
    def listdir(self, path: str) -> list[str]: ...


class RealFs:
    def exists(self, path: str) -> bool:
        return os.path.exists(path)

    def write(self, path: str, data: str) -> None:
        with open(path, "w", encoding="ascii") as fh:
            fh.write(data)

    def read(self, path: str) -> str:
        with open(path, "r", encoding="ascii") as fh:
            return fh.read().strip()

    def listdir(self, path: str) -> list[str]:
        return sorted(os.listdir(path))


@dataclass(frozen=True)
class GpioChip:
    name: str
    base: int
    ngpio: int
    label: str

    def covers(self, number: int) -> bool:
        return self.base <= number < self.base + self.ngpio


class SysfsGpio:
    def __init__(
        self,
        base: str = SYSFS_GPIO,
        fs: _Fs | None = None,
        sleep=time.sleep,
    ) -> None:
        self._base = base
        self._fs = fs or RealFs()
        self._sleep = sleep

    # -- introspection -------------------------------------------------

    def available(self) -> bool:
        return self._fs.exists(f"{self._base}/export")

    def chips(self) -> list[GpioChip]:
        out: list[GpioChip] = []
        if not self._fs.exists(self._base):
            return out
        for entry in self._fs.listdir(self._base):
            if not entry.startswith("gpiochip"):
                continue
            root = f"{self._base}/{entry}"
            try:
                out.append(
                    GpioChip(
                        name=entry,
                        base=int(self._fs.read(f"{root}/base")),
                        ngpio=int(self._fs.read(f"{root}/ngpio")),
                        label=self._fs.read(f"{root}/label"),
                    )
                )
            except (OSError, ValueError):
                continue
        return out

    def chip_for(self, number: int) -> GpioChip | None:
        for chip in self.chips():
            if chip.covers(number):
                return chip
        return None

    # -- line operations -----------------------------------------------

    def _pin(self, number: int) -> str:
        return f"{self._base}/gpio{number}"

    def is_exported(self, number: int) -> bool:
        return self._fs.exists(self._pin(number))

    def export(self, number: int) -> None:
        if self.is_exported(number):
            return
        if not self.available():
            raise GpioError(
                f"{self._base}/export is missing: this kernel has no sysfs "
                "GPIO (CONFIG_GPIO_SYSFS). UNKNOWN for the G285 image - see "
                "docs/BOBCAT-G285.md 'Failure matrix'."
            )
        try:
            self._fs.write(f"{self._base}/export", str(number))
        except OSError as exc:
            raise GpioError(f"export GPIO {number} failed: {exc}") from exc
        for _ in range(20):
            if self.is_exported(number):
                return
            self._sleep(0.05)
        raise GpioError(
            f"GPIO {number} did not appear under {self._base} after export "
            f"(chip covering it: {self._describe_cover(number)})"
        )

    def _describe_cover(self, number: int) -> str:
        chip = self.chip_for(number)
        if chip is None:
            known = ", ".join(
                f"{c.name}[{c.base}..{c.base + c.ngpio - 1}]"
                for c in self.chips()
            )
            return f"none (known chips: {known or 'none'})"
        return f"{chip.name} label={chip.label!r}"

    def direction(self, number: int, direction: str) -> None:
        try:
            self._fs.write(f"{self._pin(number)}/direction", direction)
        except OSError as exc:
            raise GpioError(
                f"set GPIO {number} direction={direction} failed: {exc}"
            ) from exc

    def write(self, number: int, value: int) -> None:
        try:
            self._fs.write(f"{self._pin(number)}/value", "1" if value else "0")
        except OSError as exc:
            raise GpioError(f"write GPIO {number}={value} failed: {exc}") from exc

    def read(self, number: int) -> int:
        try:
            return int(self._fs.read(f"{self._pin(number)}/value"))
        except (OSError, ValueError) as exc:
            raise GpioError(f"read GPIO {number} failed: {exc}") from exc

    def unexport(self, number: int) -> None:
        if not self.is_exported(number):
            return
        try:
            self._fs.write(f"{self._base}/unexport", str(number))
        except OSError as exc:
            raise GpioError(f"unexport GPIO {number} failed: {exc}") from exc
