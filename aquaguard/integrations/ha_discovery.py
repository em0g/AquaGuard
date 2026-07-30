"""Home Assistant MQTT Discovery entities.

Publishes discovery configs with retain=True on MQTT connect.
Pushes state updates every 30 seconds (and on change via events).

Entity map:
  switch:      aquaguard/{id}/valve/state
  sensor:      aquaguard/{id}/sensor/{pressure,temperature,flow_rate}/state
  binary_sensor: aquaguard/{id}/binary_sensor/{pressure_alarm,temp_alarm}/state
  button:      aquaguard/{id}/button/pressure_test/press

Availability: aquaguard/{id}/availability (LWT = "offline")
Discovery prefix: homeassistant/<component>/aquaguard_{id}/<entity>/config
"""

from __future__ import annotations

import asyncio
import json
import logging

from aquaguard.config import DeviceConfig
from aquaguard.event_bus import EventBus
from aquaguard.integrations.mqtt_client import MqttClient
from aquaguard.services.alarm_manager import AlarmManager
from aquaguard.services.consumption_monitor import ConsumptionMonitor
from aquaguard.services.sensor_service import SensorService
from aquaguard.services.valve_service import ValveService

log = logging.getLogger(__name__)


class HADiscovery:
    """Manages Home Assistant MQTT Discovery and state publishing."""

    def __init__(
        self,
        device_config: DeviceConfig,
        mqtt: MqttClient,
        event_bus: EventBus,
        valve: ValveService,
        sensors: SensorService,
        alarms: AlarmManager,
        consumption: ConsumptionMonitor,
    ):
        self._device = device_config
        self._mqtt = mqtt
        self._bus = event_bus
        self._valve = valve
        self._sensors = sensors
        self._alarms = alarms
        self._consumption = consumption
        self._id = device_config.id
        self._pressure_test_callback: callable | None = None
        self._last_successful_test: str | None = None
        # References to fire-and-forget tasks — a bare ensure_future can be
        # garbage-collected mid-flight.
        self._bg_tasks: set[asyncio.Task] = set()

        # Listen for successful scheduled tests
        self._bus.subscribe("scheduled_test_passed", self._on_scheduled_test_passed)
        # Push state immediately when it changes — the 30 s loop alone makes
        # the HA valve switch bounce back visually after every command.
        for event in (
            "valve_state_changed",
            "alarm_triggered",
            "alarm_cleared",
            "consumption_warning_long_episode",
            "consumption_warning_large_volume",
            "consumption_warning_drip",
            "consumption_warning_burst",
            "flow_shutoff",
            "scheduled_test_failed",
        ):
            self._bus.subscribe(event, self._on_state_event)

    def set_pressure_test_callback(self, cb: callable) -> None:
        """Register callback for pressure test button press."""
        self._pressure_test_callback = cb

    def _base_topic(self, component: str, entity: str) -> str:
        return f"aquaguard/{self._id}/{component}/{entity}"

    def _discovery_topic(self, component: str, entity: str) -> str:
        return f"homeassistant/{component}/aquaguard_{self._id}/{entity}/config"

    def _device_info(self) -> dict:
        return {
            "identifiers": [f"aquaguard_{self._id}"],
            "name": self._device.name,
            "manufacturer": "AquaGuard",
            "model": "VFB Replacement",
            "sw_version": "0.1.0",
        }

    async def publish_discovery(self) -> None:
        """Publish all HA discovery configs (retained)."""
        await self._mqtt._connected.wait()
        avail = {"availability_topic": self._mqtt.availability_topic}
        device = self._device_info()

        # Valve switch
        await self._mqtt.publish(
            self._discovery_topic("switch", "valve"),
            {
                **avail,
                "device": device,
                "name": f"{self._device.name} Valve",
                "unique_id": f"aquaguard_{self._id}_valve",
                "state_topic": self._base_topic("valve", "state"),
                "command_topic": self._base_topic("valve", "set"),
                "payload_on": "ON",
                "payload_off": "OFF",
                "state_on": "ON",
                "state_off": "OFF",
                "icon": "mdi:valve",
            },
            retain=True,
        )

        # Pressure sensor
        await self._mqtt.publish(
            self._discovery_topic("sensor", "pressure"),
            {
                **avail,
                "device": device,
                "name": f"{self._device.name} Pressure",
                "unique_id": f"aquaguard_{self._id}_pressure",
                "state_topic": self._base_topic("sensor/pressure", "state"),
                "unit_of_measurement": "bar",
                "device_class": "pressure",
                "state_class": "measurement",
                "icon": "mdi:gauge",
            },
            retain=True,
        )

        # Temperature sensor
        await self._mqtt.publish(
            self._discovery_topic("sensor", "temperature"),
            {
                **avail,
                "device": device,
                "name": f"{self._device.name} Temperature",
                "unique_id": f"aquaguard_{self._id}_temperature",
                "state_topic": self._base_topic("sensor/temperature", "state"),
                "unit_of_measurement": "°C",
                "device_class": "temperature",
                "state_class": "measurement",
            },
            retain=True,
        )

        # Flow rate sensor
        await self._mqtt.publish(
            self._discovery_topic("sensor", "flow_rate"),
            {
                **avail,
                "device": device,
                "name": f"{self._device.name} Flow Rate",
                "unique_id": f"aquaguard_{self._id}_flow_rate",
                "state_topic": self._base_topic("sensor/flow_rate", "state"),
                "unit_of_measurement": "L/h",
                "icon": "mdi:water-pump",
                "state_class": "measurement",
            },
            retain=True,
        )

        # Pressure alarm binary sensor
        await self._mqtt.publish(
            self._discovery_topic("binary_sensor", "pressure_alarm"),
            {
                **avail,
                "device": device,
                "name": f"{self._device.name} Pressure Alarm",
                "unique_id": f"aquaguard_{self._id}_pressure_alarm",
                "state_topic": self._base_topic("binary_sensor/pressure_alarm", "state"),
                "device_class": "problem",
                "payload_on": "ON",
                "payload_off": "OFF",
            },
            retain=True,
        )

        # Temperature alarm binary sensor
        await self._mqtt.publish(
            self._discovery_topic("binary_sensor", "temp_alarm"),
            {
                **avail,
                "device": device,
                "name": f"{self._device.name} Temperature Alarm",
                "unique_id": f"aquaguard_{self._id}_temp_alarm",
                "state_topic": self._base_topic("binary_sensor/temp_alarm", "state"),
                "device_class": "cold",
                "payload_on": "ON",
                "payload_off": "OFF",
            },
            retain=True,
        )

        # Consumption warning binary sensors (one per detector)
        for entity, label, dev_class in (
            ("consumption_long_episode", "Long Flow Episode", "problem"),
            ("consumption_large_volume", "Large Episode Volume", "problem"),
            ("consumption_drip", "Drip Detected", "problem"),
            ("consumption_burst", "Burst Flow", "problem"),
        ):
            await self._mqtt.publish(
                self._discovery_topic("binary_sensor", entity),
                {
                    **avail,
                    "device": device,
                    "name": f"{self._device.name} {label}",
                    "unique_id": f"aquaguard_{self._id}_{entity}",
                    "state_topic": self._base_topic(
                        f"binary_sensor/{entity}", "state"
                    ),
                    "device_class": dev_class,
                    "payload_on": "ON",
                    "payload_off": "OFF",
                },
                retain=True,
            )

        # Pressure test button
        await self._mqtt.publish(
            self._discovery_topic("button", "pressure_test"),
            {
                **avail,
                "device": device,
                "name": f"{self._device.name} Pressure Test",
                "unique_id": f"aquaguard_{self._id}_pressure_test",
                "command_topic": self._base_topic("button/pressure_test", "press"),
                "icon": "mdi:test-tube",
            },
            retain=True,
        )

        # Last successful test sensor
        await self._mqtt.publish(
            self._discovery_topic("sensor", "last_successful_test"),
            {
                **avail,
                "device": device,
                "name": f"{self._device.name} Last Successful Test",
                "unique_id": f"aquaguard_{self._id}_last_successful_test",
                "state_topic": self._base_topic("sensor/last_successful_test", "state"),
                "device_class": "timestamp",
                "icon": "mdi:check-circle-outline",
            },
            retain=True,
        )

        # Subscribe to command topics
        self._mqtt.subscribe(
            self._base_topic("valve", "set"),
            self._on_valve_command,
        )
        self._mqtt.subscribe(
            self._base_topic("button/pressure_test", "press"),
            self._on_pressure_test_command,
        )

        log.info("HA Discovery configs published")

    async def _on_valve_command(self, topic: str, payload: str) -> None:
        if payload == "ON":
            await self._valve.open_valve()
        elif payload == "OFF":
            await self._valve.close_valve()

    async def _on_pressure_test_command(self, topic: str, payload: str) -> None:
        if self._pressure_test_callback:
            task = asyncio.ensure_future(self._pressure_test_callback())
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)

    async def _on_state_event(self, **kwargs) -> None:
        """Event-driven state push; the 30 s loop remains as a safety net."""
        if not self._mqtt.is_connected:
            return
        try:
            await self.publish_state()
        except Exception:
            log.exception("Error publishing HA state on event")

    async def _on_scheduled_test_passed(self, **kwargs) -> None:
        ts = kwargs.get("timestamp", "")
        self._last_successful_test = ts
        await self._publish_last_test_state(ts)

    async def _publish_last_test_state(self, timestamp: str) -> None:
        await self._mqtt.publish(
            self._base_topic("sensor/last_successful_test", "state"),
            timestamp,
            retain=True,
        )

    async def publish_state(self) -> None:
        """Publish current state of all entities."""
        # Valve
        valve_state = "ON" if self._valve.is_open else "OFF"
        await self._mqtt.publish(
            self._base_topic("valve", "state"), valve_state, retain=True
        )

        # Sensors
        r = self._sensors.readings
        await self._mqtt.publish(
            self._base_topic("sensor/pressure", "state"),
            f"{r.pressure:.3f}",
        )
        await self._mqtt.publish(
            self._base_topic("sensor/temperature", "state"),
            f"{r.temperature:.1f}",
        )
        await self._mqtt.publish(
            self._base_topic("sensor/flow_rate", "state"),
            f"{r.flow_rate:.1f}",
        )

        # Alarms
        await self._mqtt.publish(
            self._base_topic("binary_sensor/pressure_alarm", "state"),
            "ON" if self._alarms.state.pressure_alarm else "OFF",
        )
        await self._mqtt.publish(
            self._base_topic("binary_sensor/temp_alarm", "state"),
            "ON" if self._alarms.state.temp_alarm else "OFF",
        )

        # Consumption warnings
        warnings = self._consumption.warnings
        for key, entity in (
            ("long_episode", "consumption_long_episode"),
            ("large_volume", "consumption_large_volume"),
            ("drip", "consumption_drip"),
            ("burst", "consumption_burst"),
        ):
            await self._mqtt.publish(
                self._base_topic(f"binary_sensor/{entity}", "state"),
                "ON" if warnings[key] else "OFF",
            )

    async def publish_state_loop(self) -> None:
        """Publish state every 30 seconds."""
        await self._mqtt._connected.wait()
        await self.publish_discovery()
        while True:
            try:
                await self.publish_state()
            except Exception:
                log.exception("Error publishing HA state")
            await asyncio.sleep(30)
