"""External alarm GPIO — input on GPIO 17, output on GPIO 27.

The input uses libgpiod (gpiod) for edge detection — RPi.GPIO edge-detect is
broken on newer kernels (6.12+), where it exports the wrong sysfs line because
it assumes a gpiochip base of 0 (the base is 512 on this kernel). Same reason
`buttons.py` uses libgpiod. The gpiod fd is integrated with asyncio via
add_reader() for non-blocking event handling.

The output stays on RPi.GPIO: it writes the SoC registers via /dev/gpiomem and
is unaffected by the sysfs problem, matching how `buzzer.py` drives GPIO 23.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from aquaguard.event_bus import EventBus

log = logging.getLogger(__name__)

try:
    import RPi.GPIO as GPIO
except (ImportError, RuntimeError):
    # RuntimeError: RPi.GPIO imports fine off-device but refuses to initialise
    # ("This module can only be run on a Raspberry Pi!"), so tests can import us.
    GPIO = None  # type: ignore[assignment]

try:
    import gpiod
    from gpiod.line import Bias, Edge
except ImportError:
    gpiod = None  # type: ignore[assignment]

_DEBOUNCE_MS = 500


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
        self._request = None
        self._chip = None

    async def init(self) -> None:
        self._init_output()
        self._init_input()

    def _init_output(self) -> None:
        if GPIO is None:
            log.warning("RPi.GPIO not available — alarm output disabled")
            return
        try:
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self._output_pin, GPIO.OUT, initial=GPIO.LOW)
            log.info("Alarm output initialised on GPIO %d", self._output_pin)
        except Exception:
            log.exception("Failed to initialise alarm output — disabled")

    def _init_input(self) -> None:
        if gpiod is None:
            log.error(
                "gpiod not available — external alarm input GPIO %d is DEAD",
                self._input_pin,
            )
            return
        try:
            self._chip = gpiod.Chip("/dev/gpiochip0")
            self._request = self._chip.request_lines(
                consumer="aquaguard-alarm",
                config={
                    self._input_pin: gpiod.LineSettings(
                        edge_detection=Edge.FALLING,
                        bias=Bias.PULL_UP,
                        debounce_period=timedelta(milliseconds=_DEBOUNCE_MS),
                    ),
                },
            )
            log.info(
                "Alarm input initialised (gpiod): IN=%d falling-edge",
                self._input_pin,
            )
        except Exception:
            # Loud: a silent failure here means external alarms are never seen.
            log.exception(
                "Failed to initialise external alarm input GPIO %d — input is DEAD",
                self._input_pin,
            )
            self._request = None

    async def run(self) -> None:
        """Main loop: wait for falling edges on the alarm input via asyncio fd."""
        if self._request is None:
            # No hardware — just sleep forever so TaskGroup doesn't exit
            while True:
                await asyncio.sleep(3600)

        loop = asyncio.get_running_loop()
        fd = self._request.fd

        while True:
            # Wait for the fd to become readable (edge event pending)
            event = asyncio.Event()
            loop.add_reader(fd, event.set)
            try:
                await event.wait()
            finally:
                loop.remove_reader(fd)

            # Read and dispatch events
            try:
                for edge_event in self._request.read_edge_events():
                    if edge_event.line_offset == self._input_pin:
                        log.info(
                            "External alarm input triggered (GPIO %d)",
                            self._input_pin,
                        )
                        await self._bus.emit("external_alarm")
            except Exception:
                log.exception("Error reading external alarm events")

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
        if self._request is not None:
            self._request.release()
        if self._chip is not None:
            self._chip.close()
