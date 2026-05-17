"""DS18B20 temperature sensor via 1-wire sysfs.

Reference: pipeTemp.js
  - Read /sys/bus/w1/devices/{id}/w1_slave
  - Parse t=15750 → 15.75°C
  - Return 99.0 if sensor is missing (legacy convention)
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

    def __init__(self, device_id: str = "28-0301a2791e31"):
        self._device_id = device_id
        self._path = _W1_BASE / device_id / "w1_slave"
        self.available = self._path.exists()

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
