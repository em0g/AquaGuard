"""Async pub/sub event bus for inter-component communication."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Callable, Coroutine

log = logging.getLogger(__name__)

Callback = Callable[..., Coroutine[Any, Any, None]]


class EventBus:
    """Simple async event bus with topic-based pub/sub."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callback]] = defaultdict(list)

    def subscribe(self, event: str, callback: Callback) -> None:
        """Register an async callback for an event topic."""
        self._subscribers[event].append(callback)

    def unsubscribe(self, event: str, callback: Callback) -> None:
        """Remove a callback from an event topic."""
        try:
            self._subscribers[event].remove(callback)
        except ValueError:
            pass

    async def emit(self, event: str, **kwargs: Any) -> None:
        """Emit an event, calling all subscribers concurrently."""
        subscribers = self._subscribers.get(event, [])
        if not subscribers:
            return
        tasks = []
        for cb in subscribers:
            try:
                tasks.append(asyncio.ensure_future(cb(**kwargs)))
            except Exception:
                log.exception("Error scheduling callback for event %s", event)
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    # log.exception would log the *current* exception (none
                    # here); exc_info=result attaches the handler's traceback.
                    log.error(
                        "Error in event handler for %s: %r",
                        event, result, exc_info=result,
                    )
