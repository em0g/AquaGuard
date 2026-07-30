"""Centralised alarm management.

Alarm types:
  - pressure_low: pressure test stage 1 failed (line below threshold)
  - temp_low: temperature below frost threshold
  - leak_detected: pressure test detected leak
  - external: external alarm GPIO triggered
  - flow_burst: sustained very high flow, valve closed by ConsumptionMonitor
  - flow_long_episode: flow ran past the shutoff duration (opt-in)
  - flow_large_volume: episode passed the shutoff volume (opt-in)

There is deliberately no low-pressure check during normal operation: with the
valve open the sensor sees mains pressure, which varies with supply, and the
pressure sensor is already published to Home Assistant where an automation
can alert on any threshold without a code change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from aquaguard.config import AlarmsConfig
from aquaguard.event_bus import EventBus
from aquaguard.hardware.buzzer import Buzzer
from aquaguard.hardware.gpio_alarm import GpioAlarm
from aquaguard.storage.state import StateStore

log = logging.getLogger(__name__)


@dataclass
class AlarmState:
    active: bool = False
    alarm_type: str | None = None
    message: str = ""
    pressure_alarm: bool = False
    temp_alarm: bool = False


class AlarmManager:
    """Manages all alarm conditions and coordinates responses."""

    def __init__(
        self,
        config: AlarmsConfig,
        event_bus: EventBus,
        buzzer: Buzzer,
        gpio_alarm: GpioAlarm,
        state_store: StateStore,
    ):
        self._config = config
        self._bus = event_bus
        self._buzzer = buzzer
        self._gpio_alarm = gpio_alarm
        self._state_store = state_store
        self.state = AlarmState()

    async def init(self) -> None:
        # Restore persisted alarm state
        self.state.active = self._state_store.get("alarm_active", False)
        self.state.alarm_type = self._state_store.get("alarm_type")
        # Subscribe to events
        self._bus.subscribe("sensor_update", self._on_sensor_update)
        self._bus.subscribe("external_alarm", self._on_external_alarm)
        self._bus.subscribe("button_reset", self._on_reset)
        log.info("Alarm manager initialised, active=%s", self.state.active)

    async def trigger_alarm(self, alarm_type: str, message: str = "") -> None:
        """Activate an alarm condition."""
        self.state.active = True
        self.state.alarm_type = alarm_type
        self.state.message = message
        if alarm_type in ("pressure_low", "leak_detected"):
            self.state.pressure_alarm = True
        if alarm_type == "temp_low":
            self.state.temp_alarm = True
        await self._state_store.set("alarm_active", True)
        await self._state_store.set("alarm_type", alarm_type)
        await self._gpio_alarm.set_alarm_output(True)
        await self._buzzer.alarm_pattern()
        await self._bus.emit(
            "alarm_triggered",
            alarm_type=alarm_type,
            message=message,
        )
        log.warning("ALARM: %s — %s", alarm_type, message)

    async def clear_alarm(self) -> None:
        """Clear the current alarm condition."""
        self.state.active = False
        self.state.alarm_type = None
        self.state.message = ""
        self.state.pressure_alarm = False
        self.state.temp_alarm = False
        await self._state_store.set("alarm_active", False)
        await self._state_store.set("alarm_type", None)
        await self._gpio_alarm.set_alarm_output(False)
        await self._bus.emit("alarm_cleared")
        log.info("Alarm cleared")

    async def _on_sensor_update(
        self, pressure: float, temperature: float, flow_rate: float
    ) -> None:
        """Check sensor values against alarm thresholds."""
        # Temperature alarm
        if temperature < self._config.low_temp_threshold and temperature != 99.0:
            if not self.state.temp_alarm:
                await self.trigger_alarm(
                    "temp_low",
                    f"Temperature {temperature:.1f}°C below threshold "
                    f"{self._config.low_temp_threshold}°C",
                )

    async def _on_external_alarm(self) -> None:
        await self.trigger_alarm("external", "External alarm input triggered")

    async def _on_reset(self) -> None:
        if self.state.active:
            await self.clear_alarm()
