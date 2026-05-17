"""Shared I2C bus access with asyncio lock for concurrency safety."""

from __future__ import annotations

import asyncio
import logging
from functools import partial

log = logging.getLogger(__name__)

try:
    import smbus2
except ImportError:
    smbus2 = None  # type: ignore[assignment]


class I2CBus:
    """Async-safe wrapper around smbus2 with a shared lock."""

    def __init__(self, bus_number: int = 1) -> None:
        self._bus_number = bus_number
        self._lock = asyncio.Lock()
        self._bus: smbus2.SMBus | None = None

    async def open(self) -> None:
        if smbus2 is None:
            log.warning("smbus2 not available — I2C operations will be simulated")
            return
        loop = asyncio.get_running_loop()
        self._bus = await loop.run_in_executor(None, smbus2.SMBus, self._bus_number)
        log.info("Opened I2C bus %d", self._bus_number)

    async def close(self) -> None:
        if self._bus is not None:
            self._bus.close()
            self._bus = None

    async def write_byte_data(self, addr: int, register: int, value: int) -> None:
        async with self._lock:
            if self._bus is None:
                log.debug(
                    "I2C SIM write addr=0x%02X reg=0x%02X val=0x%02X",
                    addr, register, value,
                )
                return
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                partial(self._bus.write_byte_data, addr, register, value),
            )

    async def write_i2c_block_data(
        self, addr: int, register: int, data: list[int]
    ) -> None:
        async with self._lock:
            if self._bus is None:
                log.debug(
                    "I2C SIM block_write addr=0x%02X reg=0x%02X data=%s",
                    addr, register, data,
                )
                return
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                partial(self._bus.write_i2c_block_data, addr, register, data),
            )

    async def read_i2c_block_data(
        self, addr: int, register: int, length: int
    ) -> list[int]:
        async with self._lock:
            if self._bus is None:
                log.debug(
                    "I2C SIM block_read addr=0x%02X reg=0x%02X len=%d",
                    addr, register, length,
                )
                return [0] * length
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None,
                partial(self._bus.read_i2c_block_data, addr, register, length),
            )

    async def write_byte(self, addr: int, value: int) -> None:
        async with self._lock:
            if self._bus is None:
                log.debug("I2C SIM write_byte addr=0x%02X val=0x%02X", addr, value)
                return
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                partial(self._bus.write_byte, addr, value),
            )
