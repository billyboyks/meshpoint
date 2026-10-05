"""Capture source for the SX1302 LoRa concentrator.

Requires a supported host (Raspberry Pi + RAK2287-class carrier, or a
Bobcat Miner 300) with the concentrator on SPI, and the patched
libloragw.so compiled and installed.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING, AsyncIterator, Optional

from src.capture.base import CaptureSource
from src.hal.concentrator_config import ConcentratorChannelPlan
from src.hal.platform import health
from src.hal.platform.chip_probe import (
    ConcentratorHardwareError,
    require_chip,
)
from src.hal.platform.detect import resolve_spi_device
from src.hal.sx1302_wrapper import BW_MAP, SX1302Wrapper
from src.models.packet import Protocol, RawCapture
from src.models.signal import SignalMetrics

if TYPE_CHECKING:
    from src.config import RadioConfig

logger = logging.getLogger(__name__)


class ConcentratorCaptureSource(CaptureSource):
    """Captures LoRa packets via the RAK2287 SX1302 concentrator."""

    def __init__(
        self,
        spi_path: str = "/dev/spidev0.0",
        lib_path: Optional[str] = None,
        channel_plan: Optional[ConcentratorChannelPlan] = None,
        poll_interval_ms: int = 10,
        syncword: int = 0x2B,
        radio_config: Optional[RadioConfig] = None,
        sx1261_spi_path: str = "",
    ):
        spi_path = resolve_spi_device(spi_path)
        self._spi_path = spi_path
        self._wrapper = SX1302Wrapper(
            lib_path=lib_path,
            spi_path=spi_path,
            sx1261_spi_path=sx1261_spi_path,
        )
        self._channel_plan = self._resolve_channel_plan(
            channel_plan, radio_config
        )
        self._poll_interval = poll_interval_ms / 1000.0
        self._syncword = syncword
        self._running = False
        self._restart_lock = asyncio.Lock()

    @staticmethod
    def _resolve_channel_plan(
        channel_plan: Optional[ConcentratorChannelPlan],
        radio_config: Optional[RadioConfig],
    ) -> ConcentratorChannelPlan:
        if radio_config is not None:
            return ConcentratorChannelPlan.from_radio_config(
                region=radio_config.region,
                frequency_mhz=radio_config.frequency_mhz,
                spreading_factor=radio_config.spreading_factor,
                bandwidth_khz=radio_config.bandwidth_khz,
            )
        if channel_plan is not None:
            return channel_plan
        return ConcentratorChannelPlan.meshtastic_us915_default()

    @property
    def name(self) -> str:
        return "concentrator"

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def rx_crc_stats(self) -> tuple[int, int]:
        """(crc_bad_total, no_crc_total) since concentrator start."""
        return self._wrapper.crc_bad_count, self._wrapper.no_crc_count

    def check_platform(self) -> None:
        """Refuse to start on a host we cannot drive safely."""
        from src.hal.platform import get_detection

        det = get_detection()
        if not det.profile.supported:
            raise ConcentratorHardwareError(
                f"platform {det.profile.id} is not supported: "
                + "; ".join(det.warnings or det.profile.notes)
            )
        for w in det.warnings:
            logger.warning("Platform warning (%s): %s", det.profile.id, w)

    def preflight(self) -> None:
        """Fail loudly if the SX1302 does not answer on SPI.

        Reads the chip-version register (the same transaction
        ``lgw_connect`` performs) after the reset and before
        ``lgw_start``. A dead (0x00) or floating (0xFF) bus raises
        ``ConcentratorHardwareError`` instead of letting the service
        report "running" with no radio.
        """
        result = require_chip(self._spi_path, retries=6, retry_delay=0.5)
        if result.values:
            health.set_chip_version(result.values[-1])
        logger.info("SX1302 preflight OK: %s", result.message())

    async def start(self) -> None:
        try:
            await self._start_impl()
        except Exception as exc:
            health.set_failed(f"{type(exc).__name__}: {exc}")
            raise
        health.set_ok()

    async def _start_impl(self) -> None:
        from src.hal.platform import get_detection

        health.set_starting(self._spi_path, get_detection().profile.id)
        self.check_platform()
        self._wrapper.load()

        late_reset = os.environ.get("CONCENTRATOR_LATE_RESET", "0") == "1"

        if not late_reset:
            self._wrapper.reset()
            self.preflight()

        self._wrapper.configure(self._channel_plan)

        if late_reset:
            # Perform reset as late as possible, right before lgw_start().
            # This is required on some sensitive RAK Hotspot V2 / RAK7248
            # carriers where an earlier reset (from the pre-script or early
            # Python call) can leave the chip in a bad state.
            self._wrapper.reset()
            self.preflight()

        self._wrapper.start()
        self._wrapper.set_syncword(self._syncword)
        self._running = True
        logger.info(
            "Concentrator capture started (syncword=0x%02X)",
            self._syncword,
        )

    async def stop(self) -> None:
        self._running = False
        self._wrapper.stop()
        logger.info("Concentrator capture stopped")

    async def restart_pipeline(self) -> None:
        """Stop RX, reset the SX1302, and restart without restarting meshpoint."""
        async with self._restart_lock:
            self._running = False
            await asyncio.sleep(0.15)
            self._wrapper.stop()
            await self.start()

    async def packets(self) -> AsyncIterator[RawCapture]:
        poll_count = 0
        while self._running:
            raw_packets = self._wrapper.receive()
            poll_count += 1
            if poll_count == 1 or poll_count % 50000 == 0:
                logger.info(
                    "Receive loop alive (poll #%d, %d pkt this cycle)",
                    poll_count, len(raw_packets),
                )

            for pkt in raw_packets:
                signal = SignalMetrics(
                    rssi=pkt.rssi,
                    snr=pkt.snr,
                    frequency_mhz=pkt.frequency_hz / 1_000_000.0,
                    spreading_factor=pkt.spreading_factor,
                    bandwidth_khz=BW_MAP.get(pkt.bandwidth, 125.0),
                    timestamp=datetime.now(timezone.utc),
                )

                yield RawCapture(
                    payload=pkt.payload,
                    signal=signal,
                    capture_source="concentrator",
                    timestamp=datetime.now(timezone.utc),
                    protocol_hint=Protocol.MESHTASTIC,
                )

            await asyncio.sleep(self._poll_interval)
