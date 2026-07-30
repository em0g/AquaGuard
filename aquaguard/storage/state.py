"""JSON state file for persisting valve state and other runtime state."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from functools import partial
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_DEFAULTS: dict[str, Any] = {
    "valve_open": True,
    "alarm_active": False,
    "alarm_type": None,
}


class StateStore:
    """Persist key/value state to a JSON file."""

    def __init__(self, path: str = "/var/lib/aquaguard/state.json"):
        self._path = Path(path)
        self._data: dict[str, Any] = dict(_DEFAULTS)

    async def load(self) -> None:
        if not self._path.exists():
            log.info("No state file found at %s, using defaults", self._path)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            await self.save()
            return
        try:
            loop = asyncio.get_running_loop()
            text = await loop.run_in_executor(
                None, partial(self._path.read_text, encoding="utf-8")
            )
            loaded = json.loads(text)
            self._data.update(loaded)
            log.info("State loaded from %s", self._path)
        except Exception:
            log.exception("Failed to load state, using defaults")

    async def save(self) -> None:
        try:
            text = json.dumps(self._data, indent=2)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._write_atomic, text)
        except Exception:
            log.exception("Failed to save state")

    def _write_atomic(self, text: str) -> None:
        """Tempfile + rename: power loss mid-write must never corrupt the
        file — load() falls back to defaults on parse errors, which would
        silently report the valve open and any latched alarm cleared."""
        tmp = self._path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    async def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        await self.save()

    @property
    def data(self) -> dict[str, Any]:
        return dict(self._data)
