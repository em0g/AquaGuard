"""Physical button inputs on GPIO 19 (On), 26 (Off), 24 (Reset).

Uses libgpiod (gpiod) for edge detection — RPi.GPIO edge-detect is broken
on newer kernels (6.12+). The gpiod fd is integrated with asyncio via
add_reader() for non-blocking event handling.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from aquaguard.event_bus import EventBus

log = logging.getLogger(__name__)

try:
    import gpiod
    from gpiod.line import Bias, Edge
except ImportError:
    gpiod = None  # type: ignore[assignment]

_DEBOUNCE_MS = 300


class Buttons:
    """Physical button handler using libgpiod edge detection."""

    def __init__(
        self,
        event_bus: EventBus,
        on_pin: int = 19,
        off_pin: int = 26,
        reset_pin: int = 24,
    ):
        self._bus = event_bus
        self._on_pin = on_pin
        self._off_pin = off_pin
        self._reset_pin = reset_pin
        self._pin_map: dict[int, str] = {
            on_pin: "button_on",
            off_pin: "button_off",
            reset_pin: "button_reset",
        }
        self._request = None
        self._chip = None

    async def init(self) -> None:
        if gpiod is None:
            log.warning("gpiod not available — buttons disabled")
            return
        try:
            self._chip = gpiod.Chip("/dev/gpiochip0")
            settings = gpiod.LineSettings(
                edge_detection=Edge.FALLING,
                bias=Bias.PULL_UP,
                debounce_period=timedelta(milliseconds=_DEBOUNCE_MS),
            )
            self._request = self._chip.request_lines(
                consumer="aquaguard-buttons",
                config={
                    self._on_pin: settings,
                    self._off_pin: settings,
                    self._reset_pin: settings,
                },
            )
            log.info(
                "Buttons initialised (gpiod): ON=%d OFF=%d RESET=%d",
                self._on_pin, self._off_pin, self._reset_pin,
            )
        except Exception:
            log.exception("Failed to initialise buttons — disabled")
            self._request = None

    async def run(self) -> None:
        """Main loop: wait for edge events via asyncio fd integration."""
        if self._request is None:
            # No hardware — just sleep forever so TaskGroup doesn't exit
            while True:
                await asyncio.sleep(3600)
            return

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
                    name = self._pin_map.get(edge_event.line_offset)
                    if name:
                        log.info("Button pressed: %s (GPIO %d)", name, edge_event.line_offset)
                        await self._bus.emit(name)
            except Exception:
                log.exception("Error reading button events")

    def cleanup(self) -> None:
        if self._request is not None:
            self._request.release()
        if self._chip is not None:
            self._chip.close()
