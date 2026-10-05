"""Read the SX1302 chip-version register over SPI, without the HAL.

This reproduces exactly the transaction ``lgw_connect()`` performs
(sx1302_hal ``loragw_spi.c`` ``lgw_spi_r`` + ``loragw_reg.c``):

    SPI mode 0, MSB first, 8 bit/word, 2 MHz (SPI_SPEED)
    tx = [mux=0x00 (SX1302), 0x56 | READ(0x00), 0x06, 0x00, 0x00]
    rx[4] = COMMON_VERSION_VERSION, register default 0x10 (v1.0)

where register address 0x5606 = SX1302_REG_COMMON_BASE_ADDR (0x5600) + 6.

It is read-only. Never run it while the Meshpoint service (and so the
HAL) holds the concentrator: stop the service first.

A powered, reset-released, correctly wired SX1302 answers 0x10. A dead
bus (no chip, held in reset, no power) typically reads 0x00; a floating
MISO line reads 0xFF. Both must fail loudly.
"""

from __future__ import annotations

import os
import struct
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterator, Protocol

SPI_SPEED_HZ = 2_000_000
MUX_SX1302 = 0x00
VERSION_REG_ADDR = 0x5606
EXPECTED_VERSION = 0x10
READ_FRAME = bytes(
    [MUX_SX1302, (VERSION_REG_ADDR >> 8) & 0x7F, VERSION_REG_ADDR & 0xFF, 0x00, 0x00]
)

# linux/spi/spidev.h (identical on arm and arm64)
_SPI_IOC_WR_MODE = 0x40016B01
_SPI_IOC_WR_LSB_FIRST = 0x40016B02
_SPI_IOC_WR_BITS_PER_WORD = 0x40016B03
_SPI_IOC_WR_MAX_SPEED_HZ = 0x40046B04
_SPI_IOC_MESSAGE_1 = 0x40206B00  # _IOW('k', 0, 32-byte spi_ioc_transfer)


class ProbeState(str, Enum):
    OK = "ok"
    DEAD = "dead"                # 0x00
    FLOATING = "floating"        # 0xFF
    UNSTABLE = "unstable"        # reads disagree
    UNEXPECTED = "unexpected"    # alive, not 0x10 (SX1303? new silicon?)
    MISSING = "missing"          # device node absent
    NO_PERMISSION = "no_permission"
    IO_ERROR = "io_error"


@dataclass
class ProbeResult:
    state: ProbeState
    path: str
    values: list[int] = field(default_factory=list)
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.state is ProbeState.OK

    @property
    def alive(self) -> bool:
        """Chip answered with something plausible (OK or UNEXPECTED)."""
        return self.state in (ProbeState.OK, ProbeState.UNEXPECTED)

    def message(self) -> str:
        vals = ",".join(f"0x{v:02X}" for v in self.values) or "-"
        return f"{self.path}: {self.state.value} (reads: {vals}) {self.detail}".rstrip()


class _Spi(Protocol):
    def xfer(self, tx: bytes) -> bytes: ...


class SpidevDevice:
    """Raw spidev ioctl access (Linux only; no third-party packages)."""

    def __init__(self, path: str, speed_hz: int = SPI_SPEED_HZ) -> None:
        import fcntl  # noqa: PLC0415  (Linux only)

        self._fcntl = fcntl
        self._speed = speed_hz
        self._fd = os.open(path, os.O_RDWR)
        try:
            fcntl.ioctl(self._fd, _SPI_IOC_WR_MODE, struct.pack("B", 0))
            fcntl.ioctl(self._fd, _SPI_IOC_WR_LSB_FIRST, struct.pack("B", 0))
            fcntl.ioctl(self._fd, _SPI_IOC_WR_BITS_PER_WORD, struct.pack("B", 8))
            fcntl.ioctl(self._fd, _SPI_IOC_WR_MAX_SPEED_HZ, struct.pack("I", speed_hz))
        except OSError:
            os.close(self._fd)
            raise

    def xfer(self, tx: bytes) -> bytes:
        import ctypes  # noqa: PLC0415

        tx_buf = ctypes.create_string_buffer(bytes(tx), len(tx))
        rx_buf = ctypes.create_string_buffer(len(tx))
        msg = struct.pack(
            "=QQIIHBBBBBB",
            ctypes.addressof(tx_buf),
            ctypes.addressof(rx_buf),
            len(tx),
            self._speed,
            0,  # delay_usecs
            8,  # bits_per_word
            0,  # cs_change
            0, 0, 0, 0,  # tx_nbits, rx_nbits, word_delay_usecs, pad
        )
        self._fcntl.ioctl(self._fd, _SPI_IOC_MESSAGE_1, msg)
        return bytes(rx_buf.raw)

    def close(self) -> None:
        os.close(self._fd)


@contextmanager
def _open_real(path: str) -> Iterator[_Spi]:
    dev = SpidevDevice(path)
    try:
        yield dev
    finally:
        dev.close()


def classify(values: list[int]) -> tuple[ProbeState, str]:
    if not values:
        return ProbeState.IO_ERROR, "no reads completed"
    if len(set(values)) > 1:
        return ProbeState.UNSTABLE, "version register read changes between reads"
    v = values[0]
    if v == EXPECTED_VERSION:
        return ProbeState.OK, "SX1302 chip version 0x10"
    if v == 0x00:
        return (
            ProbeState.DEAD,
            "reads 0x00: no chip answering (held in reset, unpowered, "
            "wrong SPI bus/CS, or latched after hard power loss)",
        )
    if v == 0xFF:
        return (
            ProbeState.FLOATING,
            "reads 0xFF: MISO floating (no device, wrong bus/CS, or SPI pins "
            "not muxed)",
        )
    return (
        ProbeState.UNEXPECTED,
        f"alive but version 0x{v:02X} != 0x{EXPECTED_VERSION:02X}",
    )


def probe_chip(
    path: str,
    *,
    attempts: int = 5,
    delay: float = 0.1,
    opener: Callable[[str], "contextmanager"] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    exists: Callable[[str], bool] = os.path.exists,
) -> ProbeResult:
    """Read the version register ``attempts`` times and classify it."""
    if opener is None:
        if not exists(path):
            return ProbeResult(
                ProbeState.MISSING, path, [],
                "device node not found (SPI overlay/device tree not applied?)",
            )
        opener = _open_real
    values: list[int] = []
    try:
        with opener(path) as spi:
            for i in range(attempts):
                rx = spi.xfer(READ_FRAME)
                values.append(rx[len(READ_FRAME) - 1])
                if i != attempts - 1:
                    sleep(delay)
    except PermissionError as exc:
        return ProbeResult(
            ProbeState.NO_PERMISSION, path, values,
            f"{exc}. Run as root, or add the user to the spidev group.",
        )
    except OSError as exc:
        return ProbeResult(ProbeState.IO_ERROR, path, values, str(exc))
    state, detail = classify(values)
    return ProbeResult(state, path, values, detail)


class ConcentratorHardwareError(RuntimeError):
    """The SX1302 is not answering; do not start the HAL."""


def require_chip(
    path: str,
    *,
    retries: int = 0,
    retry_delay: float = 0.5,
    retry_sleep: Callable[[float], None] = time.sleep,
    **kwargs,
) -> ProbeResult:
    """Probe and raise ``ConcentratorHardwareError`` unless the chip is alive.

    ``retries`` re-probes while the answer is a transient-looking failure
    (dead / floating / unstable), to ride out the SX1302 still settling
    after a reset. Permanent problems (missing node, permission) are not
    retried.
    """
    transient = (ProbeState.DEAD, ProbeState.FLOATING, ProbeState.UNSTABLE)
    result = probe_chip(path, **kwargs)
    for _ in range(retries):
        if result.alive or result.state not in transient:
            break
        retry_sleep(retry_delay)
        result = probe_chip(path, **kwargs)
    if not result.alive:
        raise ConcentratorHardwareError(
            "SX1302 not responding - " + result.message()
            + " | run: sudo systemctl stop meshpoint && sudo meshpoint hwcheck"
        )
    return result
