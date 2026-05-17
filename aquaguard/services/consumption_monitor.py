"""Detect anomalous water consumption patterns.

Three independent detectors that emit warnings via the event bus without
taking any automatic action (no valve close, no buzzer). The intent is for
Home Assistant to pick up the events and decide whether to notify the user.

Detectors:
  1. long_episode  — single FLOW state lasted longer than configured minutes
  2. large_volume  — single episode accumulated more than configured liters
  3. drip          — sustained flow inside [min, max] L/h band for configured
                     minutes while in IDLE state. Designed to catch WC leaks
                     and dripping safety valves that never reach the 30 L/h
                     episode-start threshold.

All warnings are latched per-episode (or until the drip flow leaves the band)
so we don't spam HA with repeated events for the same condition.
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
        time_fn: Callable[[], float] = time.monotonic,
    ):
        self._config = config
        self._bus = event_bus
        self._time = time_fn
        self._long_episode_warned = False
        self._large_volume_warned = False
        self._drip_streak_start: float | None = None
        self._drip_warned = False

    @property
    def warnings(self) -> dict[str, bool]:
        """Current active state of each warning — used by HA discovery."""
        return {
            "long_episode": self._long_episode_warned,
            "large_volume": self._large_volume_warned,
            "drip": self._drip_warned,
        }

    async def on_measurement(
        self,
        flow_lph: float,
        state: FlowState,
        episode_volume: float,
        episode_duration: float,
        **_kwargs,
    ) -> None:
        """Process one FlowSensor measurement (~1 Hz)."""
        if state == FlowState.FLOW:
            await self._check_long_episode(episode_duration)
            await self._check_large_volume(episode_volume)
            # No drip detection during a real episode — reset the streak.
            self._drip_streak_start = None
        elif state == FlowState.IDLE:
            # Re-arm episode latches now that the episode is over.
            self._long_episode_warned = False
            self._large_volume_warned = False
            await self._check_drip(flow_lph)

    async def _check_long_episode(self, duration_s: float) -> None:
        if self._long_episode_warned:
            return
        if duration_s >= self._config.long_episode_minutes * 60:
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

    async def _check_large_volume(self, volume_l: float) -> None:
        if self._large_volume_warned:
            return
        if volume_l >= self._config.episode_volume_liters:
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
