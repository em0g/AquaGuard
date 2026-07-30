"""SQLite database for pressure test history and flow episode data."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS episode (
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    volume REAL NOT NULL DEFAULT 0,
    duration INTEGER NOT NULL DEFAULT 0,
    flowmeter INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS event (
    timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    eventName TEXT,
    threshold INTEGER,
    action TEXT,
    text TEXT
);

CREATE TABLE IF NOT EXISTS pressure_test (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    type TEXT NOT NULL,
    result TEXT NOT NULL,
    initial_pressure REAL,
    final_pressure REAL,
    values_stage1 TEXT,
    values_stage2 TEXT
);
"""

_MAX_EVENTS = 75
# Retention: episodes older than this are pruned on insert.
_EPISODE_RETENTION_DAYS = 365
# Full 3600-sample stage-2 curves are ~50 kB per test; keep them for the
# most recent tests only. Summary rows (result, pressures) are tiny and
# kept forever.
_PRESSURE_TEST_CURVES_KEPT = 20


class Database:
    """Async wrapper around SQLite for AquaGuard data."""

    def __init__(self, db_path: str = "/var/lib/aquaguard/aquaguard.db"):
        self._path = Path(db_path)
        self._conn: sqlite3.Connection | None = None

    async def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        loop = asyncio.get_running_loop()
        self._conn = await loop.run_in_executor(
            None, partial(sqlite3.connect, str(self._path), check_same_thread=False)
        )
        self._conn.row_factory = sqlite3.Row
        await loop.run_in_executor(None, self._conn.executescript, _SCHEMA)
        log.info("Database opened at %s", self._path)

    async def close(self) -> None:
        if self._conn:
            conn = self._conn
            self._conn = None
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, conn.close)

    async def _execute(
        self, sql: str, params: tuple = ()
    ) -> list[sqlite3.Row]:
        assert self._conn is not None
        loop = asyncio.get_running_loop()

        def _run() -> list[sqlite3.Row]:
            cur = self._conn.execute(sql, params)  # type: ignore[union-attr]
            self._conn.commit()  # type: ignore[union-attr]
            return cur.fetchall()

        return await loop.run_in_executor(None, _run)

    async def save_pressure_test(
        self,
        test_type: str,
        result: str,
        initial_pressure: float | None,
        final_pressure: float | None,
        values_stage1: list[float] | None = None,
        values_stage2: list[float] | None = None,
    ) -> None:
        await self._execute(
            """INSERT INTO pressure_test
               (type, result, initial_pressure, final_pressure, values_stage1, values_stage2)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                test_type,
                result,
                initial_pressure,
                final_pressure,
                json.dumps(values_stage1) if values_stage1 else None,
                json.dumps(values_stage2) if values_stage2 else None,
            ),
        )
        # Drop raw curves outside the retention window; keep the summary rows.
        await self._execute(
            """UPDATE pressure_test SET values_stage1 = NULL, values_stage2 = NULL
               WHERE id NOT IN
               (SELECT id FROM pressure_test ORDER BY id DESC LIMIT ?)""",
            (_PRESSURE_TEST_CURVES_KEPT,),
        )

    async def get_pressure_tests(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = await self._execute(
            "SELECT * FROM pressure_test ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in rows]

    async def save_event(
        self, event_name: str, threshold: int | None = None,
        action: str | None = None, text: str | None = None,
    ) -> None:
        await self._execute(
            "INSERT INTO event (eventName, threshold, action, text) VALUES (?, ?, ?, ?)",
            (event_name, threshold, action, text),
        )
        # FIFO: keep only last N events
        await self._execute(
            """DELETE FROM event WHERE rowid NOT IN
               (SELECT rowid FROM event ORDER BY rowid DESC LIMIT ?)""",
            (_MAX_EVENTS,),
        )

    async def save_episode(
        self, volume: float, duration: int, flowmeter: int = 1
    ) -> None:
        await self._execute(
            "INSERT INTO episode (volume, duration, flowmeter) VALUES (?, ?, ?)",
            (volume, duration, flowmeter),
        )
        await self._execute(
            "DELETE FROM episode WHERE timestamp < datetime('now', ?)",
            (f"-{_EPISODE_RETENTION_DAYS} days",),
        )

    async def get_last_scheduled_test_date(self) -> str | None:
        """ISO date (YYYY-MM-DD, UTC) of the most recent scheduled test.

        Seeds the scheduler's interval tracking on first run after upgrade,
        so the every-other-Sunday cadence carries over without a state file.
        """
        rows = await self._execute(
            "SELECT timestamp FROM pressure_test WHERE type = 'scheduled' "
            "ORDER BY id DESC LIMIT 1"
        )
        if not rows:
            return None
        return str(rows[0]["timestamp"])[:10]

    async def get_episodes(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self._execute(
            "SELECT * FROM episode ORDER BY rowid DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in rows]
