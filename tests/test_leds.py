"""LED ring tests — debug mode and its interaction with the normal render loop."""

from __future__ import annotations

import asyncio

import pytest

from aquaguard.hardware import leds as leds_mod
from aquaguard.hardware.leds import GREEN, OFF, RED, RING_COUNT, LedRing


class FakeStrip:
    """Stand-in for PixelStrip that records what was pushed to the wire."""

    def __init__(self, count: int):
        self.pixels = [0] * count
        self.frames: list[list[int]] = []

    def begin(self) -> None:
        pass

    def setPixelColor(self, index: int, color: int) -> None:  # noqa: N802
        self.pixels[index] = color

    def show(self) -> None:
        self.frames.append(list(self.pixels))


@pytest.fixture
def ring(event_bus):
    r = LedRing(event_bus, led_count=34)
    r._strip = FakeStrip(34)
    return r


async def test_available_reflects_strip_presence(event_bus):
    r = LedRing(event_bus, led_count=34)
    assert r.available is False
    r._strip = FakeStrip(34)
    assert r.available is True


async def test_set_pixel_enters_debug_and_writes_colour(ring):
    await ring.debug_set_pixel(5, GREEN)

    assert ring.debug_active is True
    assert ring._strip.pixels[5] == GREEN
    assert ring.debug_status()["pixels"][5] == f"#{GREEN:06x}"


async def test_set_pixel_rejects_out_of_range_index(ring):
    with pytest.raises(ValueError):
        await ring.debug_set_pixel(34, GREEN)
    with pytest.raises(ValueError):
        await ring.debug_set_pixel(-1, GREEN)


async def test_set_all_sets_every_pixel(ring):
    await ring.debug_set_all(RED)
    assert ring._strip.pixels == [RED] * 34


async def test_entering_debug_blanks_the_strip(ring):
    ring._strip.pixels = [GREEN] * 34
    await ring.enter_debug()
    assert ring._strip.pixels == [OFF] * 34


async def test_loop_does_not_overwrite_debug_pixels(ring):
    """The static-refresh path must not clobber debug colours."""
    await ring.debug_set_pixel(0, GREEN)
    frames_before = len(ring._strip.frames)

    task = asyncio.create_task(ring.run_animation_loop())
    await asyncio.sleep(1.2)  # Longer than the 1 s static refresh interval
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(ring._strip.frames) == frames_before
    assert ring._strip.pixels[0] == GREEN


async def test_exit_debug_restores_normal_rendering(ring):
    await ring.debug_set_all(RED)
    ring._valve_state = "open"
    await ring.exit_debug()

    assert ring.debug_active is False

    task = asyncio.create_task(ring.run_animation_loop())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Ring is back to the valve-open colour, not the debug red
    assert ring._strip.pixels[0] == GREEN


async def test_animation_runs_for_configured_duration(ring, monkeypatch):
    monkeypatch.setattr(leds_mod, "DEBUG_ANIMATION_SECONDS", 0.2)

    await ring.debug_run_animation("opening")
    assert ring.debug_status()["animating"] is True

    await asyncio.sleep(0.4)
    assert ring._debug_task.done()
    # Several spin frames were pushed
    assert len(ring._strip.frames) > 2


async def test_animation_restores_debug_pixels_when_done(ring, monkeypatch):
    monkeypatch.setattr(leds_mod, "DEBUG_ANIMATION_SECONDS", 0.1)

    await ring.debug_set_all(RED)
    await ring.debug_run_animation("closing")
    await asyncio.sleep(0.3)

    assert ring._strip.pixels == [RED] * 34


async def test_animation_leaves_status_leds_untouched(ring, monkeypatch):
    monkeypatch.setattr(leds_mod, "DEBUG_ANIMATION_SECONDS", 0.2)

    await ring.debug_set_pixel(leds_mod.POS_LINK, GREEN)
    await ring.debug_run_animation("opening")
    await asyncio.sleep(0.1)

    # Mid-animation: ring is being driven, status LED keeps its debug colour
    assert ring._strip.pixels[leds_mod.POS_LINK] == GREEN
    await ring._cancel_debug_animation()


async def test_animation_rejects_unknown_direction(ring):
    with pytest.raises(ValueError):
        await ring.debug_run_animation("sideways")


async def test_new_command_cancels_running_animation(ring, monkeypatch):
    monkeypatch.setattr(leds_mod, "DEBUG_ANIMATION_SECONDS", 5.0)

    await ring.debug_run_animation("opening")
    first = ring._debug_task
    await asyncio.sleep(0.1)

    await ring.debug_set_all(OFF)

    assert first.cancelled()
    assert ring._strip.pixels == [OFF] * 34


async def test_exit_debug_cancels_running_animation(ring, monkeypatch):
    monkeypatch.setattr(leds_mod, "DEBUG_ANIMATION_SECONDS", 5.0)

    await ring.debug_run_animation("opening")
    task = ring._debug_task
    await asyncio.sleep(0.1)

    await ring.exit_debug()

    assert task.cancelled()
    assert ring.debug_active is False


async def test_debug_works_without_hardware(event_bus):
    """Off-device the calls must be harmless no-ops, not exceptions."""
    r = LedRing(event_bus, led_count=34)

    await r.debug_set_pixel(0, GREEN)
    await r.debug_set_all(RED)
    await r.debug_run_animation("opening")
    await r._cancel_debug_animation()
    await r.exit_debug()

    assert r.debug_status()["available"] is False


async def test_status_reports_layout(ring):
    status = ring.debug_status()
    assert status["count"] == 34
    assert status["ring_count"] == RING_COUNT
    assert status["positions"] == {"on": 30, "off": 31, "alarm": 32, "link": 33}
    assert len(status["pixels"]) == 34
