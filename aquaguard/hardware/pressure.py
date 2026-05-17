"""ADS1015 pressure sensor driver via I2C at address 0x48.

Protocol (reverse-engineered from pipePressure.js / helpers.js:94-112):
  1. Configure: write [0xC4, 0x03] to reg 0x01, then [0x00] (pointer reg)
  2. Read: 2 bytes from reg 0x00 — MSB first (big-endian from device)
  3. Formula: pressure_bar = raw * 0.000390625 - 2.5

Note: The original JS used readWordSync (little-endian) and then swapByte16.
Our read_i2c_block_data returns device-order (MSB first), so NO swap needed.
"""

from __future__ import annotations

import logging

from aquaguard.hardware.i2c_bus import I2CBus

log = logging.getLogger(__name__)

_REG_CONFIG = 0x01
_REG_CONVERSION = 0x00
_CONFIG_DATA = [0xC4, 0x03]
_RAW_FACTOR = 0.000390625
_RAW_OFFSET = -2.5


class PressureSensor:
    """Low-level ADS1015 pressure reading."""

    def __init__(self, i2c: I2CBus, address: int = 0x48):
        self._i2c = i2c
        self._addr = address

    async def init(self) -> None:
        """Configure the ADS1015 for single-shot conversion."""
        await self._i2c.write_i2c_block_data(
            self._addr, _REG_CONFIG, _CONFIG_DATA
        )
        await self._i2c.write_byte(self._addr, _REG_CONVERSION)
        log.info("ADS1015 pressure sensor initialised at 0x%02X", self._addr)

    async def read_pressure(self) -> float:
        """Read pressure in bar. Returns negative values if no pressure."""
        # Trigger single-shot conversion
        await self._i2c.write_i2c_block_data(
            self._addr, _REG_CONFIG, _CONFIG_DATA
        )
        await self._i2c.write_byte(self._addr, _REG_CONVERSION)
        # Read 2 bytes — ADS1015 sends MSB first
        data = await self._i2c.read_i2c_block_data(
            self._addr, _REG_CONVERSION, 2
        )
        # Combine big-endian (no swap needed — smbus returns device order)
        raw = (data[0] << 8) | data[1]
        pressure = raw * _RAW_FACTOR + _RAW_OFFSET
        log.debug("Pressure raw=0x%04X (%d) => %.3f bar", raw, raw, pressure)
        return pressure
