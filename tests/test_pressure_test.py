"""Tests for pressure test service."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from aquaguard.config import AlarmsConfig
from aquaguard.event_bus import EventBus
from aquaguard.hardware.pressure import PressureSensor
from aquaguard.services.pressure_test import PressureTestService, PressureTestResult


@pytest.fixture
def mock_pressure():
    sensor = AsyncMock(spec=PressureSensor)
    sensor.read_pressure = AsyncMock(return_value=3.5)
    return sensor


@pytest.fixture
async def pressure_test_svc(mock_pressure, alarms_config, event_bus, database):
    svc = PressureTestService(
        mock_pressure, alarms_config, event_bus, database
    )
    # Speed up tests by reducing intervals and readings
    svc.STAGE1_READINGS = 4
    svc.STAGE1_INTERVAL = 0.01
    svc.STAGE2_READINGS = 4
    svc.STAGE2_INTERVAL = 0.01
    return svc


class TestPressureTest:
    async def test_pass(self, pressure_test_svc, mock_pressure):
        """Test should pass when pressure stays stable above threshold."""
        mock_pressure.read_pressure.return_value = 3.5
        result = await pressure_test_svc.run_test("manual")
        assert result == PressureTestResult.PASS

    async def test_alarm1_low_pressure(self, pressure_test_svc, mock_pressure):
        """Stage 1 alarm when pressure below 1.5 bar."""
        mock_pressure.read_pressure.return_value = 1.0
        result = await pressure_test_svc.run_test("manual")
        assert result == PressureTestResult.ALARM1

    async def test_alarm2_leak(self, pressure_test_svc, mock_pressure):
        """Stage 2 alarm when pressure drops more than 1.0 bar sustained."""
        call_count = 0

        async def dropping_pressure():
            nonlocal call_count
            call_count += 1
            # Stage 1: stable at 3.5 bar (first 4 readings)
            if call_count <= 4:
                return 3.5
            # Stage 2: sustained drop to 2.0 bar (>1.0 bar drop, every sample)
            return 2.0

        mock_pressure.read_pressure = dropping_pressure
        # Need at least LEAK_DEBOUNCE_READINGS stage-2 samples to confirm
        pressure_test_svc.STAGE2_READINGS = pressure_test_svc.LEAK_DEBOUNCE_READINGS + 2
        result = await pressure_test_svc.run_test("manual")
        assert result == PressureTestResult.ALARM2

    async def test_alarm2_ignores_single_sensor_spike(
        self, pressure_test_svc, mock_pressure
    ):
        """A single spurious 0-bar reading must NOT trigger ALARM2.

        Reproduces the production failure mode where ADS1015 occasionally
        returns a glitched value. Pressure is stable 3.5 bar, with one
        spike to 0.0 bar mid-stage-2. Result must be PASS.
        """
        call_count = 0
        spike_at = 6  # mid stage 2

        async def with_spike():
            nonlocal call_count
            call_count += 1
            if call_count == spike_at:
                return 0.0  # glitch — drop is 3.5 bar, far above threshold
            return 3.5

        mock_pressure.read_pressure = with_spike
        pressure_test_svc.STAGE2_READINGS = 20
        result = await pressure_test_svc.run_test("manual")
        assert result == PressureTestResult.PASS

    async def test_alarm2_requires_consecutive_drops(
        self, pressure_test_svc, mock_pressure
    ):
        """N-1 over-threshold samples followed by a normal one must reset streak."""
        call_count = 0
        n = pressure_test_svc.LEAK_DEBOUNCE_READINGS
        # Stage 1: 4 stable. Stage 2: (n-1) low, 1 normal, then stable again.
        stage1 = 4

        async def pattern():
            nonlocal call_count
            call_count += 1
            if call_count <= stage1:
                return 3.5  # stage 1 stable
            idx = call_count - stage1 - 1  # 0-based stage-2 index
            if 0 <= idx < n - 1:
                return 0.0  # under threshold n-1 times
            if idx == n - 1:
                return 3.5  # streak break
            return 3.5

        mock_pressure.read_pressure = pattern
        pressure_test_svc.STAGE2_READINGS = n + 5
        result = await pressure_test_svc.run_test("manual")
        assert result == PressureTestResult.PASS

    async def test_abort(self, pressure_test_svc, mock_pressure):
        """Test can be aborted."""
        original_read = mock_pressure.read_pressure

        call_count = 0

        async def slow_read():
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                pressure_test_svc.abort()
            return 3.5

        mock_pressure.read_pressure = slow_read
        result = await pressure_test_svc.run_test("manual")
        assert result == PressureTestResult.ABORTED

    async def test_saves_to_database(self, pressure_test_svc, mock_pressure, database):
        """Test results are persisted to the database."""
        mock_pressure.read_pressure.return_value = 3.5
        await pressure_test_svc.run_test("manual")
        tests = await database.get_pressure_tests()
        assert len(tests) == 1
        assert tests[0]["result"] == "pass"
        assert tests[0]["type"] == "manual"
