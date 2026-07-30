"""Tests for the scheduler — focus on valve state restoration across retries."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from aquaguard.config import PressureTestConfig
from aquaguard.event_bus import EventBus
from aquaguard.services.pressure_test import PressureTestResult
from aquaguard.services.scheduler import Scheduler


@pytest.fixture
def fast_scheduler(monkeypatch, event_bus):
    """Build a Scheduler with retry/sleep collapsed to near-zero."""
    import aquaguard.services.scheduler as sched_mod

    monkeypatch.setattr(sched_mod, "RETRY_INTERVAL_MIN", 0)
    # Patch the asyncio.sleep so the 10s valve-settle and retry waits don't block tests
    real_sleep = sched_mod.asyncio.sleep

    async def fast_sleep(seconds):
        await real_sleep(0)

    monkeypatch.setattr(sched_mod.asyncio, "sleep", fast_sleep)

    config = PressureTestConfig()
    valve = MagicMock()
    valve.is_open = True
    valve.close_valve = AsyncMock()
    valve.open_valve = AsyncMock()

    pressure_test = MagicMock()
    pressure_test.run_test = AsyncMock()

    sched = Scheduler(config, pressure_test, valve, event_bus)
    return sched, valve, pressure_test


class TestSchedulerValveRestoration:
    """The behaviour that broke in production on 2026-05-17."""

    async def test_first_attempt_pass_reopens_valve(self, fast_scheduler):
        sched, valve, pressure_test = fast_scheduler
        valve.is_open = True
        pressure_test.run_test.return_value = PressureTestResult.PASS

        await sched._run_scheduled_test()

        valve.close_valve.assert_awaited_once()
        valve.open_valve.assert_awaited_once()

    async def test_alarm2_then_pass_on_retry_still_reopens(self, fast_scheduler):
        """REGRESSION: was_open must be captured pre-loop.

        Pre-fix: attempt 1 closed the valve (alarm2), attempt 2 passed but
        saw is_open=False and never reopened. House lost water until
        manual button press.
        """
        sched, valve, pressure_test = fast_scheduler
        valve.is_open = True

        # Simulate valve.close_valve actually toggling state
        async def close_side_effect():
            valve.is_open = False

        async def open_side_effect():
            valve.is_open = True

        valve.close_valve.side_effect = close_side_effect
        valve.open_valve.side_effect = open_side_effect

        # Attempt 1: alarm2 (false positive); attempt 2: pass
        pressure_test.run_test.side_effect = [
            PressureTestResult.ALARM2,
            PressureTestResult.PASS,
        ]

        await sched._run_scheduled_test()

        # Valve should be re-opened after the successful retry
        valve.open_valve.assert_awaited_once()
        assert valve.is_open is True
        # Close should only have run on attempt 1 (attempt 2 sees valve already closed)
        valve.close_valve.assert_awaited_once()

    async def test_valve_initially_closed_stays_closed_on_pass(self, fast_scheduler):
        """If user manually closed the valve before the test, don't auto-open it."""
        sched, valve, pressure_test = fast_scheduler
        valve.is_open = False
        pressure_test.run_test.return_value = PressureTestResult.PASS

        await sched._run_scheduled_test()

        valve.close_valve.assert_not_awaited()
        valve.open_valve.assert_not_awaited()

    async def test_all_retries_alarm2_leaves_valve_closed(self, fast_scheduler):
        """Confirmed leak across all retries: valve must remain closed (failsafe)."""
        sched, valve, pressure_test = fast_scheduler
        valve.is_open = True

        async def close_side_effect():
            valve.is_open = False

        valve.close_valve.side_effect = close_side_effect
        pressure_test.run_test.return_value = PressureTestResult.ALARM2

        await sched._run_scheduled_test()

        valve.open_valve.assert_not_awaited()
        assert valve.is_open is False


class TestSchedulerFailureReporting:
    """Total failure must be visible (event) and must not strand the house
    without water unless the failure is a confirmed leak."""

    async def test_all_retries_alarm1_restores_water_and_emits(
        self, fast_scheduler, event_bus
    ):
        """ALARM1 = low supply pressure / sensor issue, not a measured leak:
        the valve must be reopened and scheduled_test_failed emitted."""
        sched, valve, pressure_test = fast_scheduler
        valve.is_open = True

        async def close_side_effect():
            valve.is_open = False

        async def open_side_effect():
            valve.is_open = True

        valve.close_valve.side_effect = close_side_effect
        valve.open_valve.side_effect = open_side_effect
        pressure_test.run_test.return_value = PressureTestResult.ALARM1

        events = []

        async def on_failed(**kwargs):
            events.append(kwargs)

        event_bus.subscribe("scheduled_test_failed", on_failed)

        await sched._run_scheduled_test()

        valve.open_valve.assert_awaited_once()
        assert valve.is_open is True
        assert len(events) == 1
        assert events[0]["result"] == "alarm1"
        assert events[0]["valve_open"] is True

    async def test_all_retries_alarm2_emits_failed_event(
        self, fast_scheduler, event_bus
    ):
        """Even when the valve stays closed, the failure must be announced."""
        sched, valve, pressure_test = fast_scheduler
        valve.is_open = True

        async def close_side_effect():
            valve.is_open = False

        valve.close_valve.side_effect = close_side_effect
        pressure_test.run_test.return_value = PressureTestResult.ALARM2

        events = []

        async def on_failed(**kwargs):
            events.append(kwargs)

        event_bus.subscribe("scheduled_test_failed", on_failed)

        await sched._run_scheduled_test()

        assert len(events) == 1
        assert events[0]["result"] == "alarm2"
        assert events[0]["valve_open"] is False

    async def test_failure_with_initially_closed_valve_stays_closed(
        self, fast_scheduler
    ):
        """Valve manually closed before the test: a non-leak failure must not
        open it — restore means restore, not force-open."""
        sched, valve, pressure_test = fast_scheduler
        valve.is_open = False
        pressure_test.run_test.return_value = PressureTestResult.ALARM1

        await sched._run_scheduled_test()

        valve.open_valve.assert_not_awaited()
