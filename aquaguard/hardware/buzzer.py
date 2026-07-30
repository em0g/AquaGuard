"""Buzzer driver on GPIO 23."""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

try:
    import RPi.GPIO as GPIO
except (ImportError, RuntimeError):
    # RuntimeError: RPi.GPIO imports fine off-device but refuses to initialise
    # ("This module can only be run on a Raspberry Pi!"), so tests can import us.
    GPIO = None  # type: ignore[assignment]


class Buzzer:
    """Simple GPIO buzzer with async beep patterns."""

    def __init__(self, pin: int = 23):
        self._pin = pin

    async def init(self) -> None:
        if GPIO is None:
            log.warning("RPi.GPIO not available — buzzer disabled")
            return
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self._pin, GPIO.OUT, initial=GPIO.LOW)
        log.info("Buzzer initialised on GPIO %d", self._pin)

    async def beep(self, on_ms: int = 200, off_ms: int = 200, count: int = 1) -> None:
        """Play a beep pattern."""
        if GPIO is None:
            return
        for i in range(count):
            GPIO.output(self._pin, GPIO.HIGH)
            await asyncio.sleep(on_ms / 1000)
            GPIO.output(self._pin, GPIO.LOW)
            if i < count - 1:
                await asyncio.sleep(off_ms / 1000)

    async def alarm_pattern(self) -> None:
        """Alarm signal: 3 rapid beeps, played once per trigger."""
        await self.beep(on_ms=100, off_ms=100, count=3)

    def cleanup(self) -> None:
        if GPIO is not None:
            GPIO.output(self._pin, GPIO.LOW)
