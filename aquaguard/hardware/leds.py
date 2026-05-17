"""WS281x LED ring driver — 34 LEDs on GPIO 18.

Layout:
  Positions 0-29:  ring
    - Valve open:    solid green
    - Valve closed:  solid red
    - Opening:       spinning green animation
    - Closing:       spinning red animation

  Position 30 (ON):    solid white when valve open, off otherwise
  Position 31 (OFF):   solid white when valve closed, off otherwise
  Position 32 (ALARM): blinking red when alarm active, off otherwise
  Position 33 (LINK):  always solid blue
"""

from __future__ import annotations

import asyncio
import logging

from aquaguard.event_bus import EventBus

log = logging.getLogger(__name__)

try:
    from rpi_ws281x import PixelStrip, Color, ws

    STRIP_TYPE = ws.WS2811_STRIP_RGB
except ImportError:
    PixelStrip = None  # type: ignore[assignment,misc]
    STRIP_TYPE = None

    def Color(r: int, g: int, b: int) -> int:  # type: ignore[misc]
        return (r << 16) | (g << 8) | b

# Colours
RED = Color(0xD8, 0x00, 0x10)
GREEN = Color(0x66, 0xFF, 0x33)
BLUE = Color(0x00, 0xDD, 0xFF)
WHITE = Color(0xFF, 0xFF, 0xFF)
OFF = Color(0, 0, 0)

# Dim tail colours for spin animation
GREEN_DIM = Color(0x20, 0x50, 0x10)
RED_DIM = Color(0x40, 0x00, 0x05)

# Status LED positions
POS_ON = 30
POS_OFF = 31
POS_ALARM = 32
POS_LINK = 33

# Ring size
RING_COUNT = 30

LED_PIN = 18
LED_FREQ = 800000
LED_DMA = 10
LED_BRIGHTNESS = 50
LED_INVERT = False
LED_CHANNEL = 0

SPIN_INTERVAL = 0.05  # 50ms per step


class LedRing:
    """WS281x LED ring with status indicators and animations."""

    def __init__(self, event_bus: EventBus, led_count: int = 34):
        self._bus = event_bus
        self._count = led_count
        self._strip: PixelStrip | None = None  # type: ignore[assignment]
        self._valve_state = "open"
        self._alarm_active = False
        self._spin_pos = 0
        self._alarm_blink_on = False
        self._dirty = True  # Force initial render

    async def init(self) -> None:
        if PixelStrip is None:
            log.warning("rpi_ws281x not available — LEDs disabled")
            return
        try:
            self._strip = PixelStrip(
                self._count, LED_PIN, LED_FREQ, LED_DMA,
                LED_INVERT, LED_BRIGHTNESS, LED_CHANNEL,
                strip_type=STRIP_TYPE,
            )
            self._strip.begin()
            self._clear()
            log.info("LED ring initialised (%d LEDs, RGB strip type)", self._count)
        except Exception:
            log.exception("Failed to initialise LED ring")
            self._strip = None

        self._bus.subscribe("valve_state_changed", self._on_valve_state)
        self._bus.subscribe("alarm_triggered", self._on_alarm)
        self._bus.subscribe("alarm_cleared", self._on_alarm_cleared)

    def _clear(self) -> None:
        if self._strip is None:
            return
        for i in range(self._count):
            self._strip.setPixelColor(i, OFF)
        self._strip.show()

    def _set_ring_solid(self, color: int) -> None:
        if self._strip is None:
            return
        for i in range(RING_COUNT):
            self._strip.setPixelColor(i, color)

    def _set_ring_spin(self, color: int, dim_color: int) -> None:
        if self._strip is None:
            return
        for i in range(RING_COUNT):
            self._strip.setPixelColor(i, OFF)
        pos = self._spin_pos % RING_COUNT
        self._strip.setPixelColor(pos, color)
        self._strip.setPixelColor((pos - 1) % RING_COUNT, dim_color)

    def _render_frame(self) -> None:
        if self._strip is None:
            return

        # Ring (0-29)
        if self._valve_state == "open":
            self._set_ring_solid(GREEN)
        elif self._valve_state == "closed":
            self._set_ring_solid(RED)
        elif self._valve_state == "opening":
            self._set_ring_spin(GREEN, GREEN_DIM)
        elif self._valve_state == "closing":
            self._set_ring_spin(RED, RED_DIM)

        # Status LEDs
        valve_open = self._valve_state in ("open", "opening")
        valve_closed = self._valve_state in ("closed", "closing")
        self._strip.setPixelColor(POS_ON, WHITE if valve_open else OFF)
        self._strip.setPixelColor(POS_OFF, WHITE if valve_closed else OFF)
        self._strip.setPixelColor(POS_LINK, BLUE)

        # ALARM blink
        if self._alarm_active:
            self._strip.setPixelColor(
                POS_ALARM, RED if self._alarm_blink_on else OFF
            )
        else:
            self._strip.setPixelColor(POS_ALARM, OFF)

        self._strip.show()

    async def _on_valve_state(self, state: str) -> None:
        self._valve_state = state
        self._spin_pos = 0
        self._dirty = True

    async def _on_alarm(self, alarm_type: str, message: str) -> None:
        self._alarm_active = True
        self._dirty = True

    async def _on_alarm_cleared(self) -> None:
        self._alarm_active = False
        self._dirty = True

    async def run_animation_loop(self) -> None:
        """Main LED loop — fast during animations, idle when static."""
        tick = 0
        while True:
            if self._strip is not None:
                animating = self._valve_state in ("opening", "closing")
                blinking = self._alarm_active

                if animating:
                    self._spin_pos += 1
                    self._render_frame()
                    await asyncio.sleep(SPIN_INTERVAL)
                elif blinking:
                    # Toggle alarm blink every ~500ms
                    if tick % 10 == 0:
                        self._alarm_blink_on = not self._alarm_blink_on
                        self._render_frame()
                    tick += 1
                    await asyncio.sleep(SPIN_INTERVAL)
                elif self._dirty:
                    # State changed — render once then go idle
                    self._render_frame()
                    self._dirty = False
                    await asyncio.sleep(0.5)
                else:
                    # Static — just sleep, no re-rendering
                    await asyncio.sleep(0.5)
            else:
                await asyncio.sleep(1.0)
