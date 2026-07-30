"""Tests for event-driven HA state publishing."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from aquaguard.config import DeviceConfig
from aquaguard.integrations.ha_discovery import HADiscovery
from aquaguard.services.sensor_service import SensorReadings


@pytest.fixture
def mock_mqtt():
    mqtt = MagicMock()
    mqtt.is_connected = True
    mqtt.availability_topic = "aquaguard/test01/availability"
    mqtt.publish = AsyncMock()
    return mqtt


@pytest.fixture
def ha_discovery(event_bus, mock_mqtt):
    valve = MagicMock()
    valve.is_open = True
    sensors = MagicMock()
    sensors.readings = SensorReadings(pressure=4.9, temperature=21.4, flow_rate=0.0)
    alarms = MagicMock()
    alarms.state.pressure_alarm = False
    alarms.state.temp_alarm = False
    consumption = MagicMock()
    consumption.warnings = {
        "long_episode": False,
        "large_volume": False,
        "drip": False,
        "burst": False,
    }
    return HADiscovery(
        DeviceConfig(name="Test", id="test01"),
        mock_mqtt, event_bus, valve, sensors, alarms, consumption,
    )


class TestEventDrivenState:
    async def test_valve_event_publishes_immediately(
        self, ha_discovery, event_bus, mock_mqtt
    ):
        """No 30 s wait: a valve state change must push state right away."""
        await event_bus.emit("valve_state_changed", state="open")

        topics = [c.args[0] for c in mock_mqtt.publish.await_args_list]
        assert "aquaguard/test01/valve/state" in topics

    async def test_alarm_event_publishes_immediately(
        self, ha_discovery, event_bus, mock_mqtt
    ):
        await event_bus.emit(
            "alarm_triggered", alarm_type="leak_detected", message="test"
        )

        topics = [c.args[0] for c in mock_mqtt.publish.await_args_list]
        assert "aquaguard/test01/binary_sensor/pressure_alarm/state" in topics

    async def test_no_publish_before_mqtt_connected(
        self, ha_discovery, event_bus, mock_mqtt
    ):
        """Events fired during init (before connect) must not publish."""
        mock_mqtt.is_connected = False

        await event_bus.emit("valve_state_changed", state="open")

        mock_mqtt.publish.assert_not_awaited()


class TestTemperatureSentinel:
    async def test_fallback_temp_published_as_unknown(
        self, ha_discovery, mock_mqtt
    ):
        """99.0 means 'no reading' — HA must show unknown, not 99 °C."""
        ha_discovery._sensors.readings.temperature = 99.0

        await ha_discovery.publish_state()

        temp_publishes = [
            c for c in mock_mqtt.publish.await_args_list
            if c.args[0] == "aquaguard/test01/sensor/temperature/state"
        ]
        assert temp_publishes[0].args[1] == "None"

    async def test_real_temperature_published_numeric(
        self, ha_discovery, mock_mqtt
    ):
        await ha_discovery.publish_state()

        temp_publishes = [
            c for c in mock_mqtt.publish.await_args_list
            if c.args[0] == "aquaguard/test01/sensor/temperature/state"
        ]
        assert temp_publishes[0].args[1] == "21.4"
