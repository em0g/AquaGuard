"""Two-stage pressure test (reverse-engineered from pipePressure.js:80-146).

Stage 1: 15 seconds (60 readings × 250ms)
  - If final reading < 1.5 bar → ALARM 1 (low pressure), abort

Stage 2: 15 minutes (3600 readings × 250ms)
  - If pressure drop > 1.0 bar vs initial sustained for LEAK_DEBOUNCE_READINGS
    consecutive samples → ALARM 2 (leak), abort early
  - Otherwise: TEST OK

The debounce on stage 2 rejects single-sample spikes from the ADS1015 ADC.
Why: historically the leak check fired on the first sample crossing the
threshold, and a spurious ~0 bar reading from the I2C ADC (observed twice
in production) triggered a false leak alarm. A real leak produces a
sustained drop over many samples, so requiring N=5 consecutive samples
(~1.25 s) preserves prompt detection while filtering glitches.
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import Any

from aquaguard.config import AlarmsConfig
from aquaguard.event_bus import EventBus
from aquaguard.hardware.pressure import PressureSensor
from aquaguard.services.alarm_manager import AlarmManager
from aquaguard.storage.database import Database

log = logging.getLogger(__name__)


class PressureTestResult(Enum):
    PASS = "pass"
    ALARM1 = "alarm1"
    ALARM2 = "alarm2"
    ABORTED = "aborted"


class PressureTestService:
    """Runs the two-stage pressure test."""

    STAGE1_READINGS = 60
    STAGE1_INTERVAL = 0.25  # 250ms
    STAGE2_READINGS = 3600
    STAGE2_INTERVAL = 0.25  # 250ms
    LEAK_DEBOUNCE_READINGS = 5  # Consecutive over-threshold samples before ALARM2

    def __init__(
        self,
        pressure: PressureSensor,
        config: AlarmsConfig,
        event_bus: EventBus,
        database: Database,
        alarm_manager: AlarmManager,
    ):
        self._pressure = pressure
        self._config = config
        self._bus = event_bus
        self._db = database
        self._alarms = alarm_manager
        self._running = False
        self._abort = False

    @property
    def is_running(self) -> bool:
        return self._running

    def abort(self) -> None:
        """Request abort of a running test."""
        self._abort = True

    async def run_test(self, test_type: str = "manual") -> PressureTestResult:
        """Execute the full two-stage pressure test.

        Returns PressureTestResult indicating outcome.
        """
        if self._running:
            log.warning("Pressure test already running")
            return PressureTestResult.ABORTED

        self._running = True
        self._abort = False
        result = PressureTestResult.ABORTED
        stage1_values: list[float] = []
        stage2_values: list[float] = []
        initial_pressure: float | None = None
        final_pressure: float | None = None

        try:
            await self._bus.emit("pressure_test_started", test_type=test_type)
            log.info("Pressure test started (type=%s)", test_type)

            # --- Stage 1: 15 seconds ---
            log.info("Stage 1: collecting %d readings", self.STAGE1_READINGS)
            for i in range(self.STAGE1_READINGS):
                if self._abort:
                    log.info("Pressure test aborted during stage 1")
                    result = PressureTestResult.ABORTED
                    break
                p = await self._pressure.read_pressure()
                stage1_values.append(p)
                await self._bus.emit(
                    "pressure_test_progress",
                    stage=1,
                    reading=i + 1,
                    total=self.STAGE1_READINGS,
                    pressure=p,
                )
                await asyncio.sleep(self.STAGE1_INTERVAL)
            else:
                # Stage 1 complete — check final reading
                final_stage1 = stage1_values[-1]
                if final_stage1 < self._config.low_pressure_threshold:
                    log.warning(
                        "Stage 1 ALARM: pressure %.3f bar < %.1f bar",
                        final_stage1, self._config.low_pressure_threshold,
                    )
                    result = PressureTestResult.ALARM1
                    final_pressure = final_stage1
                    # Latch via AlarmManager (buzzer, GPIO out, persistence,
                    # HA binary sensor) — it re-emits alarm_triggered itself.
                    await self._alarms.trigger_alarm(
                        "pressure_low",
                        f"Pressure test stage 1 failed: {final_stage1:.2f} bar",
                    )
                else:
                    # --- Stage 2: 15 minutes ---
                    initial_pressure = final_stage1
                    log.info(
                        "Stage 2: initial pressure %.3f bar, collecting %d readings",
                        initial_pressure, self.STAGE2_READINGS,
                    )
                    drop_streak = 0
                    for i in range(self.STAGE2_READINGS):
                        if self._abort:
                            log.info("Pressure test aborted during stage 2")
                            result = PressureTestResult.ABORTED
                            break
                        p = await self._pressure.read_pressure()
                        stage2_values.append(p)
                        drop = initial_pressure - p
                        if drop > self._config.leak_drop_threshold:
                            drop_streak += 1
                            if drop_streak >= self.LEAK_DEBOUNCE_READINGS:
                                log.warning(
                                    "Stage 2 ALARM: drop %.3f bar > %.1f bar sustained for %d samples",
                                    drop, self._config.leak_drop_threshold, drop_streak,
                                )
                                result = PressureTestResult.ALARM2
                                final_pressure = p
                                await self._alarms.trigger_alarm(
                                    "leak_detected",
                                    f"Leak detected: pressure drop {drop:.2f} bar",
                                )
                                break
                            log.debug(
                                "Stage 2 over-threshold sample %d/%d: drop=%.3f",
                                drop_streak, self.LEAK_DEBOUNCE_READINGS, drop,
                            )
                        else:
                            if drop_streak > 0:
                                log.debug(
                                    "Stage 2 drop streak reset after %d samples (drop=%.3f)",
                                    drop_streak, drop,
                                )
                            drop_streak = 0
                        if (i + 1) % 120 == 0:  # Progress every 30s
                            await self._bus.emit(
                                "pressure_test_progress",
                                stage=2,
                                reading=i + 1,
                                total=self.STAGE2_READINGS,
                                pressure=p,
                            )
                        await asyncio.sleep(self.STAGE2_INTERVAL)
                    else:
                        # Stage 2 completed without alarm
                        final_pressure = stage2_values[-1] if stage2_values else initial_pressure
                        result = PressureTestResult.PASS
                        log.info("Pressure test PASSED")

        except Exception:
            log.exception("Pressure test error")
            result = PressureTestResult.ABORTED
        finally:
            self._running = False
            await self._db.save_pressure_test(
                test_type=test_type,
                result=result.value,
                initial_pressure=initial_pressure,
                final_pressure=final_pressure,
                values_stage1=stage1_values or None,
                values_stage2=stage2_values or None,
            )
            await self._bus.emit(
                "pressure_test_completed",
                result=result.value,
                initial_pressure=initial_pressure,
                final_pressure=final_pressure,
            )
            log.info("Pressure test finished: %s", result.value)

        return result
