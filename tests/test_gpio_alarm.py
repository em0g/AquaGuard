"""External alarm GPIO tests — libgpiod input events and the RPi.GPIO output."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import MagicMock

import pytest

from aquaguard.hardware import gpio_alarm as gpio_alarm_mod
from aquaguard.hardware.gpio_alarm import GpioAlarm


class FakeEdgeEvent:
    def __init__(self, line_offset: int):
        self.line_offset = line_offset


class FakeRequest:
    """Stand-in for a gpiod LineRequest backed by a real pipe fd.

    Writing to `trigger_fd` makes `fd` readable, which is exactly what the
    asyncio add_reader() integration in GpioAlarm.run() waits for.
    """

    def __init__(self):
        self.fd, self.trigger_fd = os.pipe()
        self._pending: list[FakeEdgeEvent] = []
        self.released = False

    def fire(self, *line_offsets: int) -> None:
        self._pending.extend(FakeEdgeEvent(off) for off in line_offsets)
        os.write(self.trigger_fd, b"x")

    def read_edge_events(self) -> list[FakeEdgeEvent]:
        os.read(self.fd, 1)  # drain so the fd stops being readable
        events, self._pending = self._pending, []
        return events

    def release(self) -> None:
        self.released = True

    def close(self) -> None:
        os.close(self.fd)
        os.close(self.trigger_fd)


@pytest.fixture
def alarm(event_bus):
    a = GpioAlarm(event_bus, input_pin=17, output_pin=27)
    request = FakeRequest()
    a._request = request
    yield a
    request.close()


async def _run_briefly(alarm: GpioAlarm) -> asyncio.Task:
    task = asyncio.create_task(alarm.run())
    await asyncio.sleep(0)  # let run() register its reader
    return task


async def test_falling_edge_emits_external_alarm(alarm, event_bus):
    seen = []

    async def on_alarm(**kwargs):
        seen.append(kwargs)

    event_bus.subscribe("external_alarm", on_alarm)

    task = await _run_briefly(alarm)
    alarm._request.fire(17)
    await asyncio.sleep(0.05)
    task.cancel()

    assert len(seen) == 1


async def test_events_for_other_lines_are_ignored(alarm, event_bus):
    seen = []

    async def on_alarm(**kwargs):
        seen.append(kwargs)

    event_bus.subscribe("external_alarm", on_alarm)

    task = await _run_briefly(alarm)
    alarm._request.fire(19, 26)  # button lines, not ours
    await asyncio.sleep(0.05)
    task.cancel()

    assert seen == []


async def test_repeated_edges_each_emit(alarm, event_bus):
    seen = []

    async def on_alarm(**kwargs):
        seen.append(kwargs)

    event_bus.subscribe("external_alarm", on_alarm)

    task = await _run_briefly(alarm)
    alarm._request.fire(17)
    await asyncio.sleep(0.05)
    alarm._request.fire(17)
    await asyncio.sleep(0.05)
    task.cancel()

    assert len(seen) == 2


async def test_init_without_gpiod_leaves_input_dead(event_bus, monkeypatch):
    monkeypatch.setattr(gpio_alarm_mod, "gpiod", None)
    monkeypatch.setattr(gpio_alarm_mod, "GPIO", None)

    a = GpioAlarm(event_bus)
    await a.init()

    assert a._request is None


async def test_output_drives_pin_high_and_low(event_bus, monkeypatch):
    fake_gpio = MagicMock()
    fake_gpio.HIGH = 1
    fake_gpio.LOW = 0
    monkeypatch.setattr(gpio_alarm_mod, "GPIO", fake_gpio)

    a = GpioAlarm(event_bus, output_pin=27)
    await a.set_alarm_output(True)
    fake_gpio.output.assert_called_with(27, 1)

    await a.set_alarm_output(False)
    fake_gpio.output.assert_called_with(27, 0)


async def test_cleanup_releases_line_and_drops_output(event_bus, monkeypatch):
    fake_gpio = MagicMock()
    fake_gpio.LOW = 0
    monkeypatch.setattr(gpio_alarm_mod, "GPIO", fake_gpio)

    a = GpioAlarm(event_bus, output_pin=27)
    request = FakeRequest()
    a._request = request
    a._chip = MagicMock()

    a.cleanup()

    fake_gpio.output.assert_called_with(27, 0)
    assert request.released is True
    a._chip.close.assert_called_once()
    request.close()
