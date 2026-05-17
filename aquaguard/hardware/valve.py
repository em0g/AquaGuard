"""DRV8847 valve driver via I2C at address 0x60.

Register protocol (reverse-engineered from motor.js / support.c):
  Init:           reg 0x02 = 0xC0  (once at startup)
  Open valve:     reg 0x01 = 0x0C
  Close valve:    reg 0x01 = 0x14
  Motor power off: reg 0x01 = 0x04  (MUST run 8s after open/close)

Critical: The 8-second power-off timer uses an ID guard so that rapid
presses only honour the most recent command (like currentPowerOffId in
motor.js:36-49).
"""

from __future__ import annotations

import asyncio
import logging

from aquaguard.hardware.i2c_bus import I2CBus

log = logging.getLogger(__name__)

_REG_INIT = 0x02
_VAL_INIT = 0xC0

_REG_MOTOR = 0x01
_VAL_OPEN = 0x0C
_VAL_CLOSE = 0x14
_VAL_OFF = 0x04


class ValveDriver:
    """Low-level DRV8847 valve control."""

    def __init__(self, i2c: I2CBus, address: int = 0x60, poweroff_delay: int = 8):
        self._i2c = i2c
        self._addr = address
        self._poweroff_delay = poweroff_delay
        self._poweroff_id = 0

    async def init(self) -> None:
        """Driver init — only ensures motor power is off.

        NOTE: The 0x02=0xC0 write from support.c is intentionally skipped.
        motor.js never uses it, and it disturbs the valve on service restart.
        We only send power-off (0x04) to ensure the motor isn't drawing
        current, without changing the valve's physical position.
        """
        await self._i2c.write_byte_data(self._addr, _REG_MOTOR, _VAL_OFF)
        log.info("DRV8847 initialised at 0x%02X (motor power off, valve untouched)", self._addr)

    async def open_valve(self) -> None:
        """Command valve to open and schedule motor power-off after delay."""
        await self._i2c.write_byte_data(self._addr, _REG_MOTOR, _VAL_OPEN)
        log.info("Valve OPEN command sent")
        self._schedule_poweroff()

    async def close_valve(self) -> None:
        """Command valve to close and schedule motor power-off after delay."""
        await self._i2c.write_byte_data(self._addr, _REG_MOTOR, _VAL_CLOSE)
        log.info("Valve CLOSE command sent")
        self._schedule_poweroff()

    async def motor_off(self) -> None:
        """Immediately cut motor power."""
        await self._i2c.write_byte_data(self._addr, _REG_MOTOR, _VAL_OFF)
        log.debug("Motor power OFF")

    def _schedule_poweroff(self) -> None:
        """Schedule motor power-off with ID guard (only latest timer fires)."""
        self._poweroff_id += 1
        current_id = self._poweroff_id
        asyncio.ensure_future(self._delayed_poweroff(current_id))

    async def _delayed_poweroff(self, guard_id: int) -> None:
        """Wait then cut power — but only if no newer command has been issued."""
        await asyncio.sleep(self._poweroff_delay)
        if guard_id == self._poweroff_id:
            await self.motor_off()
        else:
            log.debug("Power-off #%d superseded by #%d", guard_id, self._poweroff_id)
