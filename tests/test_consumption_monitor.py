"""Tests for ConsumptionMonitor — long episode, large volume, drip, burst."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from aquaguard.config import ConsumptionConfig
from aquaguard.event_bus import EventBus
from aquaguard.hardware.flow import FlowState
from aquaguard.services.consumption_monitor import ConsumptionMonitor


class FakeClock:
    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def consumption_config():
    # Smaller thresholds for fast tests — same logic at scale
    return ConsumptionConfig(
        long_episode_minutes=1,       # 60 s
        episode_volume_liters=50.0,
        drip_min_flow=5.0,
        drip_max_flow=25.0,
        drip_duration_minutes=2,      # 120 s
    )


@pytest.fixture
def monitor_and_events(consumption_config):
    bus = EventBus()
    received: list[tuple[str, dict]] = []

    async def capture(event_name):
        async def cb(**kwargs):
            received.append((event_name, kwargs))
        return cb

    # Subscribe synchronously without awaiting capture()
    async def make_handler(name):
        async def cb(**kwargs):
            received.append((name, kwargs))
        return cb

    # EventBus.subscribe needs an async callable
    async def on_long(**kw):
        received.append(("consumption_warning_long_episode", kw))

    async def on_volume(**kw):
        received.append(("consumption_warning_large_volume", kw))

    async def on_drip(**kw):
        received.append(("consumption_warning_drip", kw))

    async def on_burst(**kw):
        received.append(("consumption_warning_burst", kw))

    bus.subscribe("consumption_warning_long_episode", on_long)
    bus.subscribe("consumption_warning_large_volume", on_volume)
    bus.subscribe("consumption_warning_drip", on_drip)
    bus.subscribe("consumption_warning_burst", on_burst)

    clock = FakeClock()
    mon = ConsumptionMonitor(consumption_config, bus, time_fn=clock)
    return mon, received, clock


class TestLongEpisode:
    async def test_warns_when_duration_exceeds_threshold(self, monitor_and_events):
        mon, events, _ = monitor_and_events
        # 59 s — below threshold
        await mon.on_measurement(
            flow_lph=100.0, state=FlowState.FLOW,
            episode_volume=10.0, episode_duration=59.0,
        )
        assert not any(e[0] == "consumption_warning_long_episode" for e in events)
        # 61 s — over threshold (60s)
        await mon.on_measurement(
            flow_lph=100.0, state=FlowState.FLOW,
            episode_volume=10.0, episode_duration=61.0,
        )
        long_events = [e for e in events if e[0] == "consumption_warning_long_episode"]
        assert len(long_events) == 1
        assert long_events[0][1]["duration_seconds"] == 61.0

    async def test_only_warns_once_per_episode(self, monitor_and_events):
        mon, events, _ = monitor_and_events
        for d in (61.0, 70.0, 120.0):
            await mon.on_measurement(
                flow_lph=100.0, state=FlowState.FLOW,
                episode_volume=10.0, episode_duration=d,
            )
        assert sum(1 for e in events if e[0] == "consumption_warning_long_episode") == 1

    async def test_re_arms_after_episode_ends(self, monitor_and_events):
        mon, events, _ = monitor_and_events
        await mon.on_measurement(
            flow_lph=100.0, state=FlowState.FLOW,
            episode_volume=10.0, episode_duration=61.0,
        )
        # Episode ends — transition to IDLE
        await mon.on_measurement(
            flow_lph=0.0, state=FlowState.IDLE,
            episode_volume=0.0, episode_duration=0.0,
        )
        # New episode reaches threshold
        await mon.on_measurement(
            flow_lph=100.0, state=FlowState.FLOW,
            episode_volume=10.0, episode_duration=61.0,
        )
        assert sum(1 for e in events if e[0] == "consumption_warning_long_episode") == 2


class TestLargeVolume:
    async def test_warns_when_volume_exceeds_threshold(self, monitor_and_events):
        mon, events, _ = monitor_and_events
        await mon.on_measurement(
            flow_lph=100.0, state=FlowState.FLOW,
            episode_volume=49.0, episode_duration=10.0,
        )
        assert not any(e[0] == "consumption_warning_large_volume" for e in events)
        await mon.on_measurement(
            flow_lph=100.0, state=FlowState.FLOW,
            episode_volume=51.0, episode_duration=11.0,
        )
        vol_events = [e for e in events if e[0] == "consumption_warning_large_volume"]
        assert len(vol_events) == 1
        assert vol_events[0][1]["volume_liters"] == 51.0


class TestDripDetection:
    async def test_warns_after_sustained_flow_in_band(self, monitor_and_events):
        mon, events, clock = monitor_and_events
        # Drip-band flow at t=0
        await mon.on_measurement(
            flow_lph=10.0, state=FlowState.IDLE,
            episode_volume=0.0, episode_duration=0.0,
        )
        assert not any(e[0] == "consumption_warning_drip" for e in events)
        # Advance to just below threshold (119s of 120s)
        clock.advance(119)
        await mon.on_measurement(
            flow_lph=10.0, state=FlowState.IDLE,
            episode_volume=0.0, episode_duration=0.0,
        )
        assert not any(e[0] == "consumption_warning_drip" for e in events)
        # Now past threshold
        clock.advance(2)
        await mon.on_measurement(
            flow_lph=10.0, state=FlowState.IDLE,
            episode_volume=0.0, episode_duration=0.0,
        )
        drip_events = [e for e in events if e[0] == "consumption_warning_drip"]
        assert len(drip_events) == 1
        assert drip_events[0][1]["flow_lph"] == 10.0

    async def test_zero_flow_does_not_trigger_drip(self, monitor_and_events):
        mon, events, clock = monitor_and_events
        for _ in range(10):
            await mon.on_measurement(
                flow_lph=0.0, state=FlowState.IDLE,
                episode_volume=0.0, episode_duration=0.0,
            )
            clock.advance(30)
        assert not any(e[0] == "consumption_warning_drip" for e in events)

    async def test_flow_above_band_does_not_trigger_drip(self, monitor_and_events):
        mon, events, clock = monitor_and_events
        # 27 L/h — above band (25), below episode threshold (30)
        await mon.on_measurement(
            flow_lph=27.0, state=FlowState.IDLE,
            episode_volume=0.0, episode_duration=0.0,
        )
        clock.advance(200)
        await mon.on_measurement(
            flow_lph=27.0, state=FlowState.IDLE,
            episode_volume=0.0, episode_duration=0.0,
        )
        assert not any(e[0] == "consumption_warning_drip" for e in events)

    async def test_brief_excursion_resets_streak(self, monitor_and_events):
        mon, events, clock = monitor_and_events
        # Drip flow for 119s
        await mon.on_measurement(
            flow_lph=10.0, state=FlowState.IDLE,
            episode_volume=0.0, episode_duration=0.0,
        )
        clock.advance(119)
        # Single reading drops to 0 (e.g. valve closed momentarily)
        await mon.on_measurement(
            flow_lph=0.0, state=FlowState.IDLE,
            episode_volume=0.0, episode_duration=0.0,
        )
        clock.advance(2)
        # Back in band — should be a fresh streak, not warning yet
        await mon.on_measurement(
            flow_lph=10.0, state=FlowState.IDLE,
            episode_volume=0.0, episode_duration=0.0,
        )
        assert not any(e[0] == "consumption_warning_drip" for e in events)

    async def test_flow_state_resets_drip_streak(self, monitor_and_events):
        """An active episode (FLOW) must invalidate any pending drip streak."""
        mon, events, clock = monitor_and_events
        # Drip flow for 119s
        await mon.on_measurement(
            flow_lph=10.0, state=FlowState.IDLE,
            episode_volume=0.0, episode_duration=0.0,
        )
        clock.advance(119)
        # An episode starts — drip streak should reset
        await mon.on_measurement(
            flow_lph=200.0, state=FlowState.FLOW,
            episode_volume=5.0, episode_duration=1.0,
        )
        clock.advance(2)
        # Episode ends, back to IDLE drip flow
        await mon.on_measurement(
            flow_lph=10.0, state=FlowState.IDLE,
            episode_volume=0.0, episode_duration=0.0,
        )
        # Not yet — streak just restarted
        assert not any(e[0] == "consumption_warning_drip" for e in events)

    async def test_drip_only_warns_once(self, monitor_and_events):
        mon, events, clock = monitor_and_events
        await mon.on_measurement(
            flow_lph=10.0, state=FlowState.IDLE,
            episode_volume=0.0, episode_duration=0.0,
        )
        clock.advance(125)
        for _ in range(5):
            await mon.on_measurement(
                flow_lph=10.0, state=FlowState.IDLE,
                episode_volume=0.0, episode_duration=0.0,
            )
            clock.advance(30)
        assert sum(1 for e in events if e[0] == "consumption_warning_drip") == 1


class TestBurstDetection:
    async def test_warns_after_sustained_high_flow(self, monitor_and_events):
        mon, events, clock = monitor_and_events
        await mon.on_measurement(
            flow_lph=3000.0, state=FlowState.FLOW,
            episode_volume=1.0, episode_duration=1.0,
        )
        assert not any(e[0] == "consumption_warning_burst" for e in events)
        # 19 s — still below the 20 s threshold
        clock.advance(19)
        await mon.on_measurement(
            flow_lph=3000.0, state=FlowState.FLOW,
            episode_volume=16.0, episode_duration=20.0,
        )
        assert not any(e[0] == "consumption_warning_burst" for e in events)
        clock.advance(2)
        await mon.on_measurement(
            flow_lph=3000.0, state=FlowState.FLOW,
            episode_volume=18.0, episode_duration=22.0,
        )
        burst = [e for e in events if e[0] == "consumption_warning_burst"]
        assert len(burst) == 1
        assert burst[0][1]["flow_lph"] == 3000.0

    async def test_brief_spike_does_not_warn(self, monitor_and_events):
        """A single high reading must not trip the burst alarm."""
        mon, events, clock = monitor_and_events
        await mon.on_measurement(
            flow_lph=5000.0, state=FlowState.FLOW,
            episode_volume=2.0, episode_duration=1.0,
        )
        clock.advance(5)
        # Flow returns to normal — streak resets
        await mon.on_measurement(
            flow_lph=200.0, state=FlowState.FLOW,
            episode_volume=3.0, episode_duration=6.0,
        )
        clock.advance(60)
        await mon.on_measurement(
            flow_lph=200.0, state=FlowState.FLOW,
            episode_volume=8.0, episode_duration=66.0,
        )
        assert not any(e[0] == "consumption_warning_burst" for e in events)

    async def test_uses_raw_flow_not_smoothed(self, monitor_and_events):
        """Burst must key off the unsmoothed reading, so EMA lag can't delay it."""
        mon, events, clock = monitor_and_events
        # Smoothed value still ramping up (below threshold), raw already high
        await mon.on_measurement(
            flow_lph=900.0, raw_flow_lph=3000.0, state=FlowState.FLOW,
            episode_volume=1.0, episode_duration=1.0,
        )
        clock.advance(21)
        await mon.on_measurement(
            flow_lph=1800.0, raw_flow_lph=3000.0, state=FlowState.FLOW,
            episode_volume=18.0, episode_duration=22.0,
        )
        assert sum(1 for e in events if e[0] == "consumption_warning_burst") == 1

    async def test_detected_before_episode_starts(self, monitor_and_events):
        """Burst is checked in IDLE too — episode debounce must not delay it."""
        mon, events, clock = monitor_and_events
        await mon.on_measurement(
            flow_lph=3000.0, state=FlowState.IDLE,
            episode_volume=0.0, episode_duration=0.0,
        )
        clock.advance(21)
        await mon.on_measurement(
            flow_lph=3000.0, state=FlowState.IDLE,
            episode_volume=0.0, episode_duration=0.0,
        )
        assert sum(1 for e in events if e[0] == "consumption_warning_burst") == 1


class TestShutoff:
    async def test_burst_closes_valve_and_alarms(self, consumption_config):
        bus = EventBus()
        clock = FakeClock()
        valve = AsyncMock()
        valve.is_open = True
        alarms = AsyncMock()
        mon = ConsumptionMonitor(
            consumption_config, bus,
            valve_service=valve, alarm_manager=alarms, time_fn=clock,
        )
        await mon.on_measurement(
            flow_lph=3000.0, state=FlowState.FLOW,
            episode_volume=1.0, episode_duration=1.0,
        )
        clock.advance(21)
        await mon.on_measurement(
            flow_lph=3000.0, state=FlowState.FLOW,
            episode_volume=18.0, episode_duration=22.0,
        )
        valve.close_valve.assert_awaited_once()
        alarms.trigger_alarm.assert_awaited_once()
        assert alarms.trigger_alarm.await_args.args[0] == "flow_burst"

    async def test_no_shutoff_when_disabled(self, consumption_config):
        consumption_config.enable_shutoff_burst = False
        bus = EventBus()
        clock = FakeClock()
        valve = AsyncMock()
        valve.is_open = True
        mon = ConsumptionMonitor(
            consumption_config, bus, valve_service=valve, time_fn=clock,
        )
        await mon.on_measurement(
            flow_lph=3000.0, state=FlowState.FLOW,
            episode_volume=1.0, episode_duration=1.0,
        )
        clock.advance(21)
        await mon.on_measurement(
            flow_lph=3000.0, state=FlowState.FLOW,
            episode_volume=18.0, episode_duration=22.0,
        )
        valve.close_valve.assert_not_awaited()

    async def test_long_episode_warns_but_does_not_shut_off_by_default(
        self, consumption_config
    ):
        """Default config must never close the valve on a long shower."""
        bus = EventBus()
        valve = AsyncMock()
        valve.is_open = True
        mon = ConsumptionMonitor(
            consumption_config, bus, valve_service=valve, time_fn=FakeClock(),
        )
        # Well past both the warn (60 s) and shutoff (120 s) thresholds
        await mon.on_measurement(
            flow_lph=100.0, state=FlowState.FLOW,
            episode_volume=10.0, episode_duration=9999.0,
        )
        assert mon.warnings["long_episode"] is True
        valve.close_valve.assert_not_awaited()

    async def test_long_episode_shuts_off_when_enabled(self, consumption_config):
        consumption_config.enable_shutoff_long_episode = True
        consumption_config.shutoff_episode_minutes = 2  # 120 s
        bus = EventBus()
        valve = AsyncMock()
        valve.is_open = True
        alarms = AsyncMock()
        mon = ConsumptionMonitor(
            consumption_config, bus,
            valve_service=valve, alarm_manager=alarms, time_fn=FakeClock(),
        )
        # 90 s — past the warning (60 s), short of the shutoff (120 s)
        await mon.on_measurement(
            flow_lph=100.0, state=FlowState.FLOW,
            episode_volume=10.0, episode_duration=90.0,
        )
        valve.close_valve.assert_not_awaited()
        await mon.on_measurement(
            flow_lph=100.0, state=FlowState.FLOW,
            episode_volume=10.0, episode_duration=121.0,
        )
        valve.close_valve.assert_awaited_once()
        assert alarms.trigger_alarm.await_args.args[0] == "flow_long_episode"

    async def test_already_closed_valve_is_not_reclosed(self, consumption_config):
        bus = EventBus()
        clock = FakeClock()
        valve = AsyncMock()
        valve.is_open = False
        alarms = AsyncMock()
        mon = ConsumptionMonitor(
            consumption_config, bus,
            valve_service=valve, alarm_manager=alarms, time_fn=clock,
        )
        await mon.on_measurement(
            flow_lph=3000.0, state=FlowState.FLOW,
            episode_volume=1.0, episode_duration=1.0,
        )
        clock.advance(21)
        await mon.on_measurement(
            flow_lph=3000.0, state=FlowState.FLOW,
            episode_volume=18.0, episode_duration=22.0,
        )
        valve.close_valve.assert_not_awaited()
        # The alarm must still be raised
        alarms.trigger_alarm.assert_awaited_once()


class TestWarningsProperty:
    async def test_reflects_current_state(self, monitor_and_events):
        mon, _events, clock = monitor_and_events
        assert mon.warnings == {
            "long_episode": False, "large_volume": False,
            "drip": False, "burst": False,
        }
        # Trigger long episode
        await mon.on_measurement(
            flow_lph=100.0, state=FlowState.FLOW,
            episode_volume=10.0, episode_duration=61.0,
        )
        assert mon.warnings["long_episode"] is True
        # Episode ends — long episode resets
        await mon.on_measurement(
            flow_lph=0.0, state=FlowState.IDLE,
            episode_volume=0.0, episode_duration=0.0,
        )
        assert mon.warnings["long_episode"] is False
