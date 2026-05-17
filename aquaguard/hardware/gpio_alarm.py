"""External alarm GPIO — input on GPIO 17, output on GPIO 27."""

from __future__ import annotations

import asyncio
import logging

from aquaguard.event_bus import EventBus

log = logging.getLogger(__name__)

try:
    import RPi.GPIO as GPIO
except ImportError:
    GPIO = None  # type: ignore[assignment]


class GpioAlarm:
    """External alarm interface: read alarm-in on GPIO 17, drive alarm-out on GPIO 27."""

    def __init__(
        self,
        event_bus: EventBus,
        input_pin: int = 17,
        output_pin: int = 27,
    ):
        self._bus = event_bus
        self._input_pin = input_pin
        self._output_pin = output_pin

    async def init(self) -> None:
        if GPIO is None:
            log.warning("RPi.GPIO not available — GPIO alarm disabled")
            return
        try:
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self._input_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            GPIO.setup(self._output_pin, GPIO.OUT, initial=GPIO.LOW)

            loop = asyncio.get_running_loop()

            def _ext_alarm_callback(_channel: int) -> None:
                loop.call_soon_threadsafe(
                    asyncio.ensure_future,
                    self._bus.emit("external_alarm"),
                )

            try:
                GPIO.add_event_detect(
                    self._input_pin, GPIO.FALLING,
                    callback=_ext_alarm_callback,
                    bouncetime=500,
                )
            except RuntimeError:
                log.warning("Failed to add edge detect on alarm input GPIO %d", self._input_pin)

            log.info(
                "GPIO alarm initialised: IN=%d OUT=%d",
                self._input_pin, self._output_pin,
            )
        except Exception:
            log.exception("Failed to initialise GPIO alarm — disabled")

    async def set_alarm_output(self, active: bool) -> None:
        """Drive the external alarm output pin."""
        if GPIO is None:
            return
        try:
            GPIO.output(self._output_pin, GPIO.HIGH if active else GPIO.LOW)
            log.debug("Alarm output %s", "ON" if active else "OFF")
        except Exception:
            log.debug("Could not set alarm output — GPIO not configured")

    def cleanup(self) -> None:
        try:
            if GPIO is not None:
                GPIO.output(self._output_pin, GPIO.LOW)
        except Exception:
            pass
