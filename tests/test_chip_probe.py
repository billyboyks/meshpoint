"""SX1302 chip-version probe: frame, classification, loud failures."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from src.hal.platform import chip_probe as cp
from src.hal.platform.chip_probe import (
    ConcentratorHardwareError,
    ProbeState,
    probe_chip,
    require_chip,
)


def opener_returning(values, frames=None):
    """Fake spidev whose Nth transfer returns values[N] in the last byte."""
    it = iter(values)

    class Dev:
        def xfer(self, tx: bytes) -> bytes:
            if frames is not None:
                frames.append(bytes(tx))
            return bytes(len(tx) - 1) + bytes([next(it)])

    @contextmanager
    def opener(path):
        yield Dev()

    return opener


def probe(values, **kw):
    return probe_chip(
        "/dev/spidev1.0", attempts=len(values), delay=0,
        opener=opener_returning(values, kw.pop("frames", None)),
        sleep=lambda s: None, **kw,
    )


def test_frame_matches_hal_lgw_spi_r_version_read():
    """loragw_spi.c lgw_spi_r + loragw_reg.c: mux 0, READ|0x56, 0x06, 0, 0."""
    assert cp.READ_FRAME == bytes([0x00, 0x56, 0x06, 0x00, 0x00])
    assert cp.EXPECTED_VERSION == 0x10
    assert cp.SPI_SPEED_HZ == 2_000_000


def test_healthy_chip_reports_ok_and_sends_the_hal_frame():
    frames: list[bytes] = []
    res = probe([0x10] * 5, frames=frames)
    assert res.state is ProbeState.OK and res.ok and res.alive
    assert frames == [cp.READ_FRAME] * 5


def test_zero_is_dead_and_not_alive():
    res = probe([0x00] * 3)
    assert res.state is ProbeState.DEAD
    assert not res.alive
    assert "no chip answering" in res.detail


def test_ff_is_floating():
    assert probe([0xFF] * 3).state is ProbeState.FLOATING


def test_changing_reads_are_unstable():
    assert probe([0x10, 0x00, 0x10]).state is ProbeState.UNSTABLE


def test_other_stable_value_is_alive_but_flagged():
    res = probe([0x11] * 3)
    assert res.state is ProbeState.UNEXPECTED
    assert res.alive and not res.ok


def test_missing_node():
    res = probe_chip("/dev/spidev1.0", exists=lambda p: False)
    assert res.state is ProbeState.MISSING


def test_permission_denied():
    @contextmanager
    def opener(path):
        raise PermissionError("denied")
        yield  # pragma: no cover

    res = probe_chip("/dev/spidev1.0", opener=opener)
    assert res.state is ProbeState.NO_PERMISSION


def test_io_error():
    @contextmanager
    def opener(path):
        raise OSError("ioctl failed")
        yield  # pragma: no cover

    assert probe_chip("/x", opener=opener).state is ProbeState.IO_ERROR


def test_require_chip_raises_loudly_on_dead_bus():
    with pytest.raises(ConcentratorHardwareError) as err:
        require_chip(
            "/dev/spidev1.0", attempts=2, delay=0,
            opener=opener_returning([0, 0]), sleep=lambda s: None,
        )
    assert "SX1302 not responding" in str(err.value)
    assert "meshpoint hwcheck" in str(err.value)


def test_require_chip_passes_on_healthy_chip():
    res = require_chip(
        "/dev/spidev1.0", attempts=2, delay=0,
        opener=opener_returning([0x10, 0x10]), sleep=lambda s: None,
    )
    assert res.ok


def test_require_chip_rides_out_a_settling_chip():
    seq = iter([[0x00] * 2, [0x10] * 2])

    def opener(path):
        return opener_returning(next(seq))(path)

    res = require_chip(
        "/dev/spidev1.0", attempts=2, delay=0, retries=3,
        opener=opener, sleep=lambda s: None, retry_sleep=lambda s: None,
    )
    assert res.ok


def test_require_chip_does_not_retry_permanent_errors():
    calls = []

    def opener(path):
        calls.append(path)
        raise PermissionError("denied")

    with pytest.raises(ConcentratorHardwareError):
        require_chip("/x", retries=5, opener=contextmanager(opener),
                     retry_sleep=lambda s: None)
    assert len(calls) == 1
