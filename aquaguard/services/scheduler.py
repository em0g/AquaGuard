"""Scheduled pressure tests (default every other Sunday at 02:00).

On failure: up to 3 retries with 60-minute intervals.
Sequence: close valve → run test → open valve.

If every retry fails, `scheduled_test_failed` is emitted and the valve is
restored to its pre-test position — EXCEPT when the final result is ALARM2
(sustained pressure drop, i.e. a measured leak), where the valve stays
closed as a failsafe. ALARM1/ABORTED mean low supply pressure or a sensor
problem; leaving the house without water over those helps nobody.

The scheduler only triggers within a 30-minute window after the scheduled
time to prevent accidental tests on service restart during the day.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from aquaguard.config import PressureTestConfig
from aquaguard.event_bus import EventBus
from aquaguard.services.pressure_test import PressureTestService, PressureTestResult
from aquaguard.services.valve_service import ValveService

log = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_INTERVAL_MIN = 60
TRIGGER_WINDOW_MIN = 30


class Scheduler:
    """Checks every 30 seconds whether a scheduled pressure test is due."""

    def __init__(
        self,
        config: PressureTestConfig,
        pressure_test: PressureTestService,
        valve_service: ValveService,
        event_bus: EventBus,
    ):
        self._config = config
        self._pressure_test = pressure_test
        self._valve = valve_service
        self._bus = event_bus
        self._last_test_date: str | None = None
        # Track which week-number we last ran in to enforce interval
        self._last_test_iso_week: int | None = None

    async def run(self) -> None:
        """Main scheduler loop — runs forever, checks every 30s."""
        weekday_name = [
            "Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday", "Sunday",
        ][self._config.schedule_weekday]
        log.info(
            "Scheduler started, test scheduled every %d week(s) on %s at %02d:%02d",
            self._config.schedule_interval_weeks,
            weekday_name,
            self._config.schedule_hour,
            self._config.schedule_minute,
        )
        while True:
            await asyncio.sleep(30)
            try:
                await self._check_schedule()
            except Exception:
                log.exception("Scheduler error")

    def _is_scheduled_day(self, now: datetime) -> bool:
        """Check if today is the right weekday and week interval."""
        if now.weekday() != self._config.schedule_weekday:
            return False

        # Use ISO week number to determine interval.
        # Run on weeks where (iso_week % interval) == 0.
        iso_week = now.isocalendar()[1]
        return (iso_week % self._config.schedule_interval_weeks) == 0

    async def _check_schedule(self) -> None:
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")

        # Already ran today?
        if self._last_test_date == today_str:
            return

        # Not the right day?
        if not self._is_scheduled_day(now):
            return

        target = datetime(
            now.year, now.month, now.day,
            self._config.schedule_hour, self._config.schedule_minute,
        )
        deadline = target + timedelta(minutes=TRIGGER_WINDOW_MIN)

        # Only trigger within the 30-minute window after scheduled time
        if target <= now <= deadline:
            self._last_test_date = today_str
            await self._run_scheduled_test()
        elif now > deadline:
            # Missed the window today — mark as done so we don't trigger later
            self._last_test_date = today_str
            log.debug("Missed today's test window, skipping")

    async def _run_scheduled_test(self) -> None:
        """Run a scheduled pressure test with retry logic.

        Valve state contract: capture the pre-test valve position ONCE
        before the retry loop. If a previous attempt failed and left the
        valve closed, a later PASS must still restore the original state.
        """
        log.info("Starting scheduled pressure test")
        was_open = self._valve.is_open
        result = PressureTestResult.ABORTED

        for attempt in range(1, MAX_RETRIES + 1):
            log.info("Scheduled test attempt %d/%d", attempt, MAX_RETRIES)

            # Only close if currently open — on retry the valve is already closed.
            if self._valve.is_open:
                await self._valve.close_valve()
                await asyncio.sleep(10)  # Settle time before test starts

            result = await self._pressure_test.run_test(test_type="scheduled")

            if result == PressureTestResult.PASS:
                log.info("Scheduled pressure test passed")
                if was_open:
                    await self._valve.open_valve()
                await self._bus.emit(
                    "scheduled_test_passed",
                    timestamp=datetime.now().isoformat(),
                )
                return

            if attempt < MAX_RETRIES:
                log.warning(
                    "Scheduled test failed (%s), retrying in %d min",
                    result.value, RETRY_INTERVAL_MIN,
                )
                await asyncio.sleep(RETRY_INTERVAL_MIN * 60)

        log.error(
            "Scheduled pressure test failed after %d attempts (last result: %s)",
            MAX_RETRIES, result.value,
        )
        # Restore water unless the last attempt measured an actual leak.
        # ALARM2 keeps the valve closed as a failsafe; the latched alarm
        # (buzzer/GPIO/HA) tells the owner why the water is off.
        if result != PressureTestResult.ALARM2 and was_open:
            log.warning(
                "Restoring water despite failed test — %s is not a confirmed leak",
                result.value,
            )
            await self._valve.open_valve()
        await self._bus.emit(
            "scheduled_test_failed",
            result=result.value,
            attempts=MAX_RETRIES,
            valve_open=self._valve.is_open,
            timestamp=datetime.now().isoformat(),
        )
