"""Tests for valve driver and valve service."""

from __future__ import annotations

import asyncio

import pytest

from aquaguard.hardware.valve import ValveDriver
from aquaguard.services.valve_service import ValveService, ValveState


class TestValveDriver:
    async def test_init_sends_motor_off(self, valve_driver, mock_i2c):
        await valve_driver.init()
        mock_i2c.write_byte_data.assert_called_with(0x60, 0x01, 0x04)

    async def test_open_valve(self, valve_driver, mock_i2c):
        await valve_driver.open_valve()
        mock_i2c.write_byte_data.assert_called_with(0x60, 0x01, 0x0C)

    async def test_close_valve(self, valve_driver, mock_i2c):
        await valve_driver.close_valve()
        mock_i2c.write_byte_data.assert_called_with(0x60, 0x01, 0x14)

    async def test_motor_off(self, valve_driver, mock_i2c):
        await valve_driver.motor_off()
        mock_i2c.write_byte_data.assert_called_with(0x60, 0x01, 0x04)

    async def test_poweroff_guard_supersedes(self, valve_driver, mock_i2c):
        """Rapid open+close should only fire the last power-off timer."""
        await valve_driver.open_valve()
        await valve_driver.close_valve()
        # Wait for the short poweroff delay
        await asyncio.sleep(0.2)
        # Motor off should have been called exactly once (the latest timer)
        motor_off_calls = [
            c for c in mock_i2c.write_byte_data.call_args_list
            if c.args == (0x60, 0x01, 0x04)
        ]
        assert len(motor_off_calls) == 1


class TestValveService:
    async def test_open_close(self, valve_driver, state_store, event_bus):
        await state_store.load()
        svc = ValveService(valve_driver, state_store, event_bus, motor_delay=0)
        await svc.init()

        await svc.open_valve()
        assert svc.state == ValveState.OPEN
        assert svc.is_open is True

        await svc.close_valve()
        assert svc.state == ValveState.CLOSED
        assert svc.is_open is False

    async def test_idempotent_open(self, valve_driver, state_store, event_bus, mock_i2c):
        await state_store.load()
        svc = ValveService(valve_driver, state_store, event_bus, motor_delay=0)
        await svc.init()

        await svc.open_valve()
        call_count = mock_i2c.write_byte_data.call_count
        await svc.open_valve()  # Should be a no-op
        assert mock_i2c.write_byte_data.call_count == call_count

    async def test_persists_state(self, valve_driver, state_store, event_bus):
        await state_store.load()
        svc = ValveService(valve_driver, state_store, event_bus, motor_delay=0)
        await svc.init()

        await svc.close_valve()
        assert state_store.get("valve_open") is False

        await svc.open_valve()
        assert state_store.get("valve_open") is True
