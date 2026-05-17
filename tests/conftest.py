"""Test fixtures with mocked hardware."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from aquaguard.config import AlarmsConfig, AppConfig
from aquaguard.event_bus import EventBus
from aquaguard.hardware.i2c_bus import I2CBus
from aquaguard.hardware.pressure import PressureSensor
from aquaguard.hardware.valve import ValveDriver
from aquaguard.storage.database import Database
from aquaguard.storage.state import StateStore


@pytest.fixture
def event_bus():
    return EventBus()


@pytest.fixture
def mock_i2c():
    """I2C bus mock that records all calls."""
    bus = AsyncMock(spec=I2CBus)
    bus.read_i2c_block_data.return_value = [0, 0]
    return bus


@pytest.fixture
def valve_driver(mock_i2c):
    return ValveDriver(mock_i2c, address=0x60, poweroff_delay=0.1)


@pytest.fixture
def pressure_sensor(mock_i2c):
    return PressureSensor(mock_i2c, address=0x48)


@pytest.fixture
def state_store(tmp_path):
    return StateStore(str(tmp_path / "state.json"))


@pytest.fixture
async def database(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.open()
    yield db
    await db.close()


@pytest.fixture
def alarms_config():
    return AlarmsConfig()
