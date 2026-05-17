"""Valve state machine with persistence.

States: OPEN, CLOSED, OPENING, CLOSING
The OPENING/CLOSING states persist for the motor movement duration
(~8 seconds) so that LED animations show correctly.
"""

from __future__ import annotations

import asyncio
import enum
import logging

from aquaguard.event_bus import EventBus
from aquaguard.hardware.valve import ValveDriver
from aquaguard.storage.state import StateStore

log = logging.getLogger(__name__)


class ValveState(enum.Enum):
    OPEN = "open"
    CLOSED = "closed"
    OPENING = "opening"
    CLOSING = "closing"


class ValveService:
    """High-level valve control with state tracking and persistence."""

    def __init__(
        self,
        driver: ValveDriver,
        state_store: StateStore,
        event_bus: EventBus,
        motor_delay: int = 8,
    ):
        self._driver = driver
        self._state_store = state_store
        self._bus = event_bus
        self._state = ValveState.CLOSED
        self._motor_delay = motor_delay

    @property
    def state(self) -> ValveState:
        return self._state

    @property
    def is_open(self) -> bool:
        return self._state in (ValveState.OPEN, ValveState.OPENING)

    async def init(self) -> None:
        """Initialise driver and restore persisted state."""
        await self._driver.init()
        persisted = self._state_store.get("valve_open", True)
        if persisted:
            self._state = ValveState.OPEN
        else:
            self._state = ValveState.CLOSED
        await self._bus.emit("valve_state_changed", state=self._state.value)
        log.info("Valve service init, persisted state: %s", self._state.value)
        self._bus.subscribe("button_on", self._on_button_on)
        self._bus.subscribe("button_off", self._on_button_off)

    async def open_valve(self) -> None:
        """Open the water valve."""
        if self._state in (ValveState.OPEN, ValveState.OPENING):
            log.debug("Valve already open/opening, ignoring")
            return
        self._state = ValveState.OPENING
        await self._bus.emit("valve_state_changed", state=self._state.value)
        await self._driver.open_valve()
        await self._state_store.set("valve_open", True)
        # Stay in OPENING for motor movement duration, then transition
        await asyncio.sleep(self._motor_delay)
        self._state = ValveState.OPEN
        await self._bus.emit("valve_state_changed", state=self._state.value)
        log.info("Valve opened")

    async def close_valve(self) -> None:
        """Close the water valve."""
        if self._state in (ValveState.CLOSED, ValveState.CLOSING):
            log.debug("Valve already closed/closing, ignoring")
            return
        self._state = ValveState.CLOSING
        await self._bus.emit("valve_state_changed", state=self._state.value)
        await self._driver.close_valve()
        await self._state_store.set("valve_open", False)
        # Stay in CLOSING for motor movement duration, then transition
        await asyncio.sleep(self._motor_delay)
        self._state = ValveState.CLOSED
        await self._bus.emit("valve_state_changed", state=self._state.value)
        log.info("Valve closed")

    async def _on_button_on(self) -> None:
        await self.open_valve()

    async def _on_button_off(self) -> None:
        await self.close_valve()
