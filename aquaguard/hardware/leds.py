"""WS281x LED ring driver — 34 LEDs on GPIO 18.

Layout:
  Positions 0-29:  ring — pixel 0 sits at the top and the strip is wired
                   counter-clockwise from there (verified on hardware)
    - Valve open:    solid green
    - Valve closed:  solid red
    - Opening:       spinning green animation
    - Closing:       spinning red animation

  Position 30 (ON):    solid white when valve open, off otherwise
  Position 31 (OFF):   solid white when valve closed, off otherwise
  Position 32 (ALARM): blinking red when alarm active, off otherwise
  Position 33 (LINK):  always solid blue

Debug mode (driven from the web UI) suspends all of the above: the animation
loop stops rendering and the caller drives individual pixels. Leaving debug
mode marks the frame dirty so the normal state is restored on the next tick.
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

# Debug mode
DEBUG_IDLE_INTERVAL = 0.2  # Loop tick while debug mode holds the strip
DEBUG_ANIMATION_SECONDS = 3.0  # Duration of a single debug spin animation


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

        # Debug mode — web UI takes over the strip
        self._debug = False
        self._debug_pixels: list[int] = [OFF] * led_count
        self._debug_task: asyncio.Task[None] | None = None

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

    # ------------------------------------------------------------------
    # Debug mode
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """True when a real WS281x strip is attached (False off-device)."""
        return self._strip is not None

    @property
    def debug_active(self) -> bool:
        return self._debug

    def debug_status(self) -> dict:
        """Full debug state for the web UI (also used to sync new clients)."""
        return {
            "available": self.available,
            "debug": self._debug,
            "count": self._count,
            "ring_count": RING_COUNT,
            "animating": self._debug_task is not None and not self._debug_task.done(),
            "positions": {
                "on": POS_ON,
                "off": POS_OFF,
                "alarm": POS_ALARM,
                "link": POS_LINK,
            },
            "pixels": [f"#{c:06x}" for c in self._debug_pixels],
        }

    async def enter_debug(self) -> None:
        """Take the strip over: stop normal rendering and blank all pixels."""
        if self._debug:
            return
        self._debug = True
        self._debug_pixels = [OFF] * self._count
        self._clear()
        log.info("LED debug mode entered")

    async def exit_debug(self) -> None:
        """Hand the strip back to the normal state renderer."""
        if not self._debug:
            return
        await self._cancel_debug_animation()
        self._debug = False
        self._dirty = True  # Loop re-renders the real state on the next tick
        log.info("LED debug mode exited")

    async def debug_set_pixel(self, index: int, color: int) -> None:
        """Set a single pixel. Implicitly enters debug mode."""
        if not 0 <= index < self._count:
            raise ValueError(f"LED index {index} out of range (0-{self._count - 1})")
        await self.enter_debug()
        await self._cancel_debug_animation()
        self._debug_pixels[index] = color
        self._push_debug_frame()

    async def debug_set_all(self, color: int) -> None:
        """Set every pixel to one colour. Implicitly enters debug mode."""
        await self.enter_debug()
        await self._cancel_debug_animation()
        self._debug_pixels = [color] * self._count
        self._push_debug_frame()

    async def debug_run_animation(self, direction: str) -> None:
        """Run the real opening/closing spin for DEBUG_ANIMATION_SECONDS.

        Uses the production `_set_ring_spin` code path so this exercises what
        the valve animation actually renders, not a lookalike. Returns as soon
        as the animation is scheduled; it runs as a background task.
        """
        if direction not in ("opening", "closing"):
            raise ValueError(f"Unknown animation direction: {direction!r}")
        await self.enter_debug()
        await self._cancel_debug_animation()
        self._debug_task = asyncio.create_task(self._debug_animate(direction))

    async def _debug_animate(self, direction: str) -> None:
        color, dim = (
            (GREEN, GREEN_DIM) if direction == "opening" else (RED, RED_DIM)
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + DEBUG_ANIMATION_SECONDS
        self._spin_pos = 0
        try:
            while loop.time() < deadline:
                if self._strip is not None:
                    self._set_ring_spin(color, dim)
                    # Status LEDs keep whatever debug set them to
                    for i in range(RING_COUNT, self._count):
                        self._strip.setPixelColor(i, self._debug_pixels[i])
                    self._strip.show()
                self._spin_pos += 1
                await asyncio.sleep(SPIN_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("LED debug animation failed")
        finally:
            # Restore the pixels the user had set before the animation
            if self._debug:
                self._push_debug_frame()

    async def _cancel_debug_animation(self) -> None:
        task, self._debug_task = self._debug_task, None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def _push_debug_frame(self) -> None:
        if self._strip is None:
            return
        for i, color in enumerate(self._debug_pixels):
            self._strip.setPixelColor(i, color)
        self._strip.show()

    def cleanup(self) -> None:
        """Blank the strip on shutdown — a dark ring means the service is
        down; the last rendered frame would show stale valve state."""
        try:
            self._clear()
        except Exception:
            pass

    async def run_animation_loop(self) -> None:
        """Main LED loop — fast during animations, idle when static."""
        tick = 0
        while True:
            if self._debug:
                # Debug mode owns the strip — stay off it entirely, including
                # the periodic static refresh below, which would otherwise
                # overwrite debug pixels within a second.
                await asyncio.sleep(DEBUG_IDLE_INTERVAL)
            elif self._strip is not None:
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
                    # State changed — render immediately, then keep refreshing
                    self._render_frame()
                    self._dirty = False
                    await asyncio.sleep(0.5)
                else:
                    # Static — re-send the same frame ~1x/s. A single corrupted
                    # WS281x transmission (marginal data-line logic level on some
                    # boards) would otherwise LATCH a wrong colour until the next
                    # state change; periodic refresh lets it self-heal on the next
                    # frame. Cost is negligible (34 pixels). Root fix for a marginal
                    # signal is still hardware (level shifter / clean 5V data-in).
                    self._render_frame()
                    await asyncio.sleep(1.0)
            else:
                await asyncio.sleep(1.0)
