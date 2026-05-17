"""Sensor polling service — reads all sensors at 1 Hz and emits events."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from aquaguard.event_bus import EventBus
from aquaguard.hardware.pressure import PressureSensor
from aquaguard.hardware.temperature import TemperatureSensor

log = logging.getLogger(__name__)


@dataclass
class SensorReadings:
    pressure: float = 0.0
    temperature: float = 99.0
    flow_rate: float = 0.0


class SensorService:
    """Polls sensors at 1 Hz and publishes readings via event bus."""

    def __init__(
        self,
        pressure: PressureSensor,
        temperature: TemperatureSensor,
        event_bus: EventBus,
    ):
        self._pressure = pressure
        self._temperature = temperature
        self._bus = event_bus
        self.readings = SensorReadings()
        self._flow_rate_getter: callable | None = None

    def set_flow_rate_getter(self, getter: callable) -> None:
        """Register a callable that returns current flow rate."""
        self._flow_rate_getter = getter

    async def init(self) -> None:
        await self._pressure.init()
        log.info("Sensor service initialised")

    async def run_polling_loop(self) -> None:
        """Main polling loop — runs forever at ~1 Hz."""
        log.info("Sensor polling started")
        while True:
            try:
                self.readings.pressure = await self._pressure.read_pressure()
                self.readings.temperature = await self._temperature.read_temperature()
                if self._flow_rate_getter is not None:
                    self.readings.flow_rate = self._flow_rate_getter()

                await self._bus.emit(
                    "sensor_update",
                    pressure=self.readings.pressure,
                    temperature=self.readings.temperature,
                    flow_rate=self.readings.flow_rate,
                )
            except Exception:
                log.exception("Error in sensor polling")
            await asyncio.sleep(1.0)
