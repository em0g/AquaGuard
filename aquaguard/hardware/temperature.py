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
_FALLBACK_TEMP = 99.0


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
                self._device_id, _FALLBACK_TEMP,
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
            return _FALLBACK_TEMP
        try:
            loop = asyncio.get_running_loop()
            text = await loop.run_in_executor(
                None, partial(self._path.read_text, encoding="utf-8")
            )
            return self._parse(text)
        except Exception:
            log.exception("Failed to read temperature sensor")
            return _FALLBACK_TEMP

    @staticmethod
    def _parse(text: str) -> float:
        """Parse w1_slave output for temperature value."""
        for line in text.splitlines():
            if "t=" in line:
                _, _, t_str = line.partition("t=")
                return int(t_str) / 1000.0
        return _FALLBACK_TEMP
