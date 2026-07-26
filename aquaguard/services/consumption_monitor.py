"""Detect anomalous water consumption patterns.

Four independent detectors on two tiers.

Warning tier — emits an event and does nothing else, leaving it to Home
Assistant to decide whether to notify:
  1. long_episode  — single FLOW state lasted longer than configured minutes
  2. large_volume  — single episode accumulated more than configured liters
  3. drip          — sustained flow inside [min, max] L/h band for configured
                     minutes while in IDLE state. Designed to catch WC leaks
                     and dripping safety valves that never reach the 30 L/h
                     episode-start threshold.
  4. burst         — flow above `burst_flow_lph` sustained for
                     `burst_duration_seconds`, i.e. a pipe failure.

Shutoff tier — closes the valve and raises a latched alarm. Each condition
has its own `enable_shutoff_*` flag and **all of them default off**: shutting
off the water to a house is the owner's decision, not a default. Enable a
condition only after watching its warning behave in Home Assistant.

Warnings are latched per-episode (or until the flow leaves the band) so we
don't spam HA with repeated events for the same condition.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from aquaguard.config import ConsumptionConfig
from aquaguard.event_bus import EventBus
from aquaguard.hardware.flow import FlowState

log = logging.getLogger(__name__)


class ConsumptionMonitor:
    def __init__(
        self,
        config: ConsumptionConfig,
        event_bus: EventBus,
        *,
        valve_service=None,
        alarm_manager=None,
        time_fn: Callable[[], float] = time.monotonic,
    ):
        self._config = config
        self._bus = event_bus
        self._valve = valve_service
        self._alarms = alarm_manager
        self._time = time_fn
        self._long_episode_warned = False
        self._large_volume_warned = False
        self._drip_streak_start: float | None = None
        self._drip_warned = False
        self._burst_streak_start: float | None = None
        self._burst_warned = False
        # Shutoff latches — separate from the warning latches so that raising
        # the warning first does not disarm the shutoff check.
        self._long_episode_shutoff = False
        self._large_volume_shutoff = False

    @property
    def warnings(self) -> dict[str, bool]:
        """Current active state of each warning — used by HA discovery."""
        return {
            "long_episode": self._long_episode_warned,
            "large_volume": self._large_volume_warned,
            "drip": self._drip_warned,
            "burst": self._burst_warned,
        }

    async def on_measurement(
        self,
        flow_lph: float,
        state: FlowState,
        episode_volume: float,
        episode_duration: float,
        raw_flow_lph: float | None = None,
        **_kwargs,
    ) -> None:
        """Process one FlowSensor measurement (~1 Hz).

        `flow_lph` is EMA-smoothed; `raw_flow_lph` is the unsmoothed reading
        and falls back to the smoothed value when the caller omits it.
        """
        # Burst is checked in every state: at 45 L/min the episode has not
        # necessarily cleared FLOW_DEBOUNCE_COUNT yet, and those seconds matter.
        # It uses the raw reading — EMA smoothing would add ~7 s of lag before
        # the threshold is even crossed.
        await self._check_burst(
            raw_flow_lph if raw_flow_lph is not None else flow_lph
        )

        if state == FlowState.FLOW:
            await self._check_long_episode(episode_duration)
            await self._check_large_volume(episode_volume)
            # No drip detection during a real episode — reset the streak.
            self._drip_streak_start = None
        elif state == FlowState.IDLE:
            # Re-arm episode latches now that the episode is over.
            self._long_episode_warned = False
            self._large_volume_warned = False
            self._long_episode_shutoff = False
            self._large_volume_shutoff = False
            await self._check_drip(flow_lph)

    async def _check_long_episode(self, duration_s: float) -> None:
        if not self._long_episode_warned and (
            duration_s >= self._config.long_episode_minutes * 60
        ):
            self._long_episode_warned = True
            await self._bus.emit(
                "consumption_warning_long_episode",
                duration_seconds=duration_s,
                threshold_minutes=self._config.long_episode_minutes,
            )
            log.warning(
                "Long flow episode: %.0f min (threshold %d min)",
                duration_s / 60, self._config.long_episode_minutes,
            )

        if (
            self._config.enable_shutoff_long_episode
            and not self._long_episode_shutoff
            and duration_s >= self._config.shutoff_episode_minutes * 60
        ):
            self._long_episode_shutoff = True
            await self._shut_off_water(
                "flow_long_episode",
                f"Flow ran for {duration_s / 60:.0f} min "
                f"(shutoff threshold {self._config.shutoff_episode_minutes} min)",
            )

    async def _check_large_volume(self, volume_l: float) -> None:
        if not self._large_volume_warned and (
            volume_l >= self._config.episode_volume_liters
        ):
            self._large_volume_warned = True
            await self._bus.emit(
                "consumption_warning_large_volume",
                volume_liters=volume_l,
                threshold_liters=self._config.episode_volume_liters,
            )
            log.warning(
                "Large episode volume: %.1f L (threshold %.0f L)",
                volume_l, self._config.episode_volume_liters,
            )

        if (
            self._config.enable_shutoff_large_volume
            and not self._large_volume_shutoff
            and volume_l >= self._config.shutoff_episode_volume_liters
        ):
            self._large_volume_shutoff = True
            await self._shut_off_water(
                "flow_large_volume",
                f"Episode reached {volume_l:.0f} L (shutoff threshold "
                f"{self._config.shutoff_episode_volume_liters:.0f} L)",
            )

    async def _check_drip(self, flow_lph: float) -> None:
        in_band = (
            self._config.drip_min_flow <= flow_lph <= self._config.drip_max_flow
        )
        if not in_band:
            self._drip_streak_start = None
            self._drip_warned = False
            return
        if self._drip_streak_start is None:
            self._drip_streak_start = self._time()
            log.debug("Drip streak started (flow=%.2f L/h)", flow_lph)
            return
        if self._drip_warned:
            return
        elapsed = self._time() - self._drip_streak_start
        if elapsed >= self._config.drip_duration_minutes * 60:
            self._drip_warned = True
            await self._bus.emit(
                "consumption_warning_drip",
                flow_lph=flow_lph,
                duration_seconds=elapsed,
                threshold_minutes=self._config.drip_duration_minutes,
            )
            log.warning(
                "Drip detected: %.2f L/h sustained for %.0f min",
                flow_lph, elapsed / 60,
            )

    async def _check_burst(self, flow_lph: float) -> None:
        """Sustained very high flow — a failed pipe or coupling.

        Legacy `largeWaterFlowAlarm` gated on total episode duration, so a
        burst starting late in a long episode fired instantly. Here the streak
        times the high flow itself, which is what the threshold describes.
        """
        if flow_lph < self._config.burst_flow_lph:
            self._burst_streak_start = None
            self._burst_warned = False
            return
        if self._burst_streak_start is None:
            self._burst_streak_start = self._time()
            log.debug("Burst streak started (flow=%.0f L/h)", flow_lph)
            return
        if self._burst_warned:
            return

        elapsed = self._time() - self._burst_streak_start
        if elapsed < self._config.burst_duration_seconds:
            return

        self._burst_warned = True
        await self._bus.emit(
            "consumption_warning_burst",
            flow_lph=flow_lph,
            duration_seconds=elapsed,
            threshold_lph=self._config.burst_flow_lph,
        )
        log.warning(
            "Burst flow: %.0f L/h (%.1f L/min) sustained for %.0f s",
            flow_lph, flow_lph / 60, elapsed,
        )
        if self._config.enable_shutoff_burst:
            await self._shut_off_water(
                "flow_burst",
                f"Burst flow {flow_lph / 60:.1f} L/min sustained for "
                f"{elapsed:.0f} s",
            )

    async def _shut_off_water(self, alarm_type: str, message: str) -> None:
        """Close the valve and latch an alarm for a shutoff-tier condition.

        Emits `flow_shutoff` regardless of whether a valve or alarm manager is
        wired in, so the event is visible even in a degraded configuration.
        """
        log.warning("Flow shutoff (%s): %s", alarm_type, message)
        if self._valve is not None and self._valve.is_open:
            try:
                await self._valve.close_valve()
            except Exception:
                log.exception("Failed to close valve for %s", alarm_type)
        if self._alarms is not None:
            await self._alarms.trigger_alarm(alarm_type, message)
        await self._bus.emit(
            "flow_shutoff", alarm_type=alarm_type, message=message
        )
