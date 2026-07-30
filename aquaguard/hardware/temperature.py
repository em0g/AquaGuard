"""DS18B20 temperature sensor via 1-wire sysfs.

Reference: pipeTemp.js
  - Read /sys/bus/w1/devices/{id}/w1_slave
  - Parse t=15750 → 15.75°C
  - Return 99.0 if sensor is missing (legacy convention)

The device id is a per-chip serial, so it is not committed to the repo. Leave
`hardware.temp_device_id` empty and the first 28-* device on the bus is used,
which is the right answer whenever there is only one sensor.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from pathlib import Path

log = logging.getLogger(__name__)

_W1_BASE = Path("/sys/bus/w1/devices")
# Legacy sentinel for "no reading" (pipeTemp.js convention). Outward-facing
# layers translate it: HA publishes the sensor as unknown instead of 99 °C.
FALLBACK_TEMP = 99.0
# DS18B20 power-on-reset value: the scratchpad reads 85 °C until the first
# conversion completes, so an 85.000 reading is indistinguishable from a
# sensor that never converted — treat it as invalid.
_POWER_ON_RESET_MILLIDEGREES = 85000


class TemperatureSensor:
    """DS18B20 1-wire temperature sensor."""

    def __init__(self, device_id: str = ""):
        self._device_id = device_id or self._discover()
        self._path = _W1_BASE / self._device_id / "w1_slave"
        self.available = bool(self._device_id) and self._path.exists()
        if not self.available:
            log.warning(
                "Temperature sensor unavailable (device_id=%r) — "
                "readings will report %.1f",
                self._device_id, FALLBACK_TEMP,
            )

    @staticmethod
    def _discover() -> str:
        """Return the first DS18B20 on the 1-wire bus, or '' if none."""
        try:
            found = sorted(p.name for p in _W1_BASE.glob("28-*"))
        except OSError:
            return ""
        if not found:
            return ""
        if len(found) > 1:
            log.warning(
                "Multiple 1-wire sensors found (%s) — using %s. "
                "Set hardware.temp_device_id to pick a specific one.",
                ", ".join(found), found[0],
            )
        log.info("Auto-detected 1-wire temperature sensor: %s", found[0])
        return found[0]

    async def read_temperature(self) -> float:
        """Read temperature in °C. Returns 99.0 if sensor is unavailable."""
        if not self.available:
            return FALLBACK_TEMP
        try:
            loop = asyncio.get_running_loop()
            text = await loop.run_in_executor(
                None, partial(self._path.read_text, encoding="utf-8")
            )
            return self._parse(text)
        except Exception:
            log.exception("Failed to read temperature sensor")
            return FALLBACK_TEMP

    @staticmethod
    def _parse(text: str) -> float:
        """Parse w1_slave output for temperature value.

        The first line ends in "YES" when the on-sensor CRC matched; on "NO"
        the t= value is garbage and must not be trusted.
        """
        lines = text.splitlines()
        if not lines or not lines[0].strip().endswith("YES"):
            log.warning("Temperature read failed CRC check")
            return FALLBACK_TEMP
        for line in lines:
            if "t=" in line:
                _, _, t_str = line.partition("t=")
                millidegrees = int(t_str)
                if millidegrees == _POWER_ON_RESET_MILLIDEGREES:
                    log.warning(
                        "Temperature read 85.0 °C (power-on-reset value) — "
                        "discarding"
                    )
                    return FALLBACK_TEMP
                return millidegrees / 1000.0
        return FALLBACK_TEMP
