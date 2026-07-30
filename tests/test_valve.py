"""Tests for valve driver and valve service."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

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

    async def test_concurrent_commands_serialise(
        self, valve_driver, state_store, event_bus
    ):
        """A close issued mid-open must wait for the motor movement, not
        interleave — the lock holds through the full OPENING window."""
        await state_store.load()
        svc = ValveService(valve_driver, state_store, event_bus, motor_delay=0.05)
        await svc.init()
        await svc.close_valve()

        states_seen: list[str] = []

        async def record(state: str) -> None:
            states_seen.append(state)

        event_bus.subscribe("valve_state_changed", record)

        await asyncio.gather(svc.open_valve(), svc.close_valve())

        # Fully sequential: open completes (opening → open) before close
        # even starts (closing → closed).
        assert states_seen == ["opening", "open", "closing", "closed"]
        assert svc.state == ValveState.CLOSED


class TestValveTestInterlock:
    async def test_valve_command_aborts_running_test(
        self, valve_driver, state_store, event_bus
    ):
        await state_store.load()
        svc = ValveService(valve_driver, state_store, event_bus, motor_delay=0)
        await svc.init()
        await svc.close_valve()

        test = MagicMock()
        test.is_running = True
        svc.set_test_interlock(test)

        await svc.open_valve()

        test.abort.assert_called_once()
        assert svc.state == ValveState.OPEN

    async def test_no_abort_when_test_idle(
        self, valve_driver, state_store, event_bus
    ):
        await state_store.load()
        svc = ValveService(valve_driver, state_store, event_bus, motor_delay=0)
        await svc.init()

        test = MagicMock()
        test.is_running = False
        svc.set_test_interlock(test)

        await svc.close_valve()

        test.abort.assert_not_called()
