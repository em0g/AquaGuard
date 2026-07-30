"""Scheduled pressure tests (default every other Sunday at 02:00).

The interval is enforced by calendar arithmetic: a test fires on the
configured weekday, inside the trigger window, when at least
`schedule_interval_weeks * 7` days have passed since the last scheduled
run. The last-run date persists in the StateStore and is seeded from the
pressure-test table on first run, so the cadence survives restarts and
upgrades. (The previous `iso_week % interval` rule broke around New Year:
a 53-week ISO year gave a 3-week gap.)

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
from datetime import date, datetime, timedelta

from aquaguard.config import PressureTestConfig
from aquaguard.event_bus import EventBus
from aquaguard.services.pressure_test import PressureTestService, PressureTestResult
from aquaguard.services.valve_service import ValveService
from aquaguard.storage.database import Database
from aquaguard.storage.state import StateStore

log = logging.getLogger(__name__)

_STATE_KEY = "last_scheduled_test_date"

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
        state_store: StateStore | None = None,
        database: Database | None = None,
    ):
        self._config = config
        self._pressure_test = pressure_test
        self._valve = valve_service
        self._bus = event_bus
        self._state_store = state_store
        self._db = database
        self._last_scheduled: date | None = None

    async def _load_last_scheduled(self) -> None:
        """Restore the last scheduled-run date: StateStore first, then the
        pressure-test history (pre-upgrade installs have no state key)."""
        raw = self._state_store.get(_STATE_KEY) if self._state_store else None
        if not raw and self._db is not None:
            try:
                raw = await self._db.get_last_scheduled_test_date()
            except Exception:
                log.exception("Could not seed last scheduled date from database")
        if raw:
            try:
                self._last_scheduled = date.fromisoformat(str(raw))
                log.info("Last scheduled test date: %s", self._last_scheduled)
            except ValueError:
                log.warning("Unparseable last scheduled date: %r", raw)

    async def run(self) -> None:
        """Main scheduler loop — runs forever, checks every 30s."""
        await self._load_last_scheduled()
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

    def _interval_elapsed(self, today: date) -> bool:
        """True when the configured number of weeks has passed since the
        last scheduled run (or none is known). Pure date arithmetic — no
        ISO-week anomalies at year boundaries, and a run missed because the
        device was off is made up on the next matching weekday."""
        if self._last_scheduled is None:
            return True
        elapsed = (today - self._last_scheduled).days
        return elapsed >= self._config.schedule_interval_weeks * 7

    async def _check_schedule(self) -> None:
        now = datetime.now()

        if now.weekday() != self._config.schedule_weekday:
            return

        target = datetime(
            now.year, now.month, now.day,
            self._config.schedule_hour, self._config.schedule_minute,
        )
        deadline = target + timedelta(minutes=TRIGGER_WINDOW_MIN)

        # Only trigger within the 30-minute window after scheduled time
        if not (target <= now <= deadline):
            return

        if not self._interval_elapsed(now.date()):
            return

        # Record before running: a same-day retrigger (or a failed run)
        # must not restart the sequence within the window.
        self._last_scheduled = now.date()
        if self._state_store is not None:
            await self._state_store.set(_STATE_KEY, self._last_scheduled.isoformat())
        await self._run_scheduled_test()

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
