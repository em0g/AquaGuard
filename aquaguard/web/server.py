"""FastAPI web server with REST API and WebSocket for real-time updates."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from aquaguard.event_bus import EventBus
from aquaguard.hardware.leds import LedRing
from aquaguard.services.alarm_manager import AlarmManager
from aquaguard.services.sensor_service import SensorService
from aquaguard.services.valve_service import ValveService
from aquaguard.storage.database import Database

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def parse_color(value: Any) -> int:
    """Parse '#rrggbb' (or 'rrggbb') into the packed int rpi_ws281x expects."""
    if isinstance(value, int):
        color = value
    else:
        text = str(value).strip().lstrip("#")
        if len(text) != 6:
            raise ValueError(f"Invalid colour: {value!r}")
        color = int(text, 16)
    if not 0 <= color <= 0xFFFFFF:
        raise ValueError(f"Colour out of range: {value!r}")
    return color


class WebServer:
    """FastAPI application with WebSocket broadcast."""

    def __init__(
        self,
        event_bus: EventBus,
        valve: ValveService,
        sensors: SensorService,
        alarms: AlarmManager,
        database: Database,
        leds: LedRing,
    ):
        self._bus = event_bus
        self._valve = valve
        self._sensors = sensors
        self._alarms = alarms
        self._db = database
        self._leds = leds
        self._clients: set[WebSocket] = set()
        self._pressure_test_callback: callable | None = None
        # References to fire-and-forget tasks — a bare ensure_future can be
        # garbage-collected mid-flight.
        self._bg_tasks: set[asyncio.Task] = set()

        self.app = FastAPI(title="AquaGuard", version="0.1.0")
        self._setup_routes()
        self._setup_events()

    def set_pressure_test_callback(self, cb: callable) -> None:
        self._pressure_test_callback = cb

    def _setup_events(self) -> None:
        """Subscribe to event bus for WebSocket broadcasts."""
        self._bus.subscribe("sensor_update", self._broadcast_sensors)
        self._bus.subscribe("valve_state_changed", self._broadcast_valve)
        self._bus.subscribe("alarm_triggered", self._broadcast_alarm)
        self._bus.subscribe("alarm_cleared", self._broadcast_alarm_cleared)
        self._bus.subscribe("pressure_test_started", self._broadcast_test_started)
        self._bus.subscribe("pressure_test_completed", self._broadcast_test_completed)
        self._bus.subscribe("pressure_test_progress", self._broadcast_test_progress)

    def _setup_routes(self) -> None:
        app = self.app

        @app.get("/", response_class=HTMLResponse)
        async def index():
            html_file = STATIC_DIR / "index.html"
            return HTMLResponse(html_file.read_text(encoding="utf-8"))

        @app.get("/api/status")
        async def status():
            r = self._sensors.readings
            return {
                "valve": self._valve.state.value,
                "pressure": round(r.pressure, 3),
                "temperature": round(r.temperature, 1),
                "flow_rate": round(r.flow_rate, 1),
                "alarm": {
                    "active": self._alarms.state.active,
                    "type": self._alarms.state.alarm_type,
                    "message": self._alarms.state.message,
                },
            }

        @app.post("/api/valve/open")
        async def valve_open():
            await self._valve.open_valve()
            return {"status": "ok", "valve": "open"}

        @app.post("/api/valve/close")
        async def valve_close():
            await self._valve.close_valve()
            return {"status": "ok", "valve": "closed"}

        @app.post("/api/pressure-test")
        async def run_pressure_test():
            if self._pressure_test_callback:
                self._spawn(self._pressure_test_callback())
                return {"status": "started"}
            return {"status": "error", "message": "Pressure test not available"}

        @app.post("/api/alarm/reset")
        async def reset_alarm():
            await self._alarms.clear_alarm()
            return {"status": "ok"}

        @app.get("/api/pressure-tests")
        async def get_pressure_tests():
            tests = await self._db.get_pressure_tests(limit=20)
            return {"tests": tests}

        @app.get("/api/leds")
        async def led_status():
            return self._leds.debug_status()

        @app.post("/api/leds/debug")
        async def led_debug(payload: dict):
            if payload.get("enabled"):
                await self._leds.enter_debug()
            else:
                await self._leds.exit_debug()
            await self._broadcast_leds()
            return self._leds.debug_status()

        @app.post("/api/leds/pixel")
        async def led_pixel(payload: dict):
            try:
                index = int(payload["index"])
                color = parse_color(payload["color"])
            except (KeyError, TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            try:
                await self._leds.debug_set_pixel(index, color)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            await self._broadcast_leds()
            return self._leds.debug_status()

        @app.post("/api/leds/all")
        async def led_all(payload: dict):
            try:
                color = parse_color(payload["color"])
            except (KeyError, TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            await self._leds.debug_set_all(color)
            await self._broadcast_leds()
            return self._leds.debug_status()

        @app.post("/api/leds/animation")
        async def led_animation(payload: dict):
            try:
                await self._leds.debug_run_animation(str(payload.get("direction")))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            await self._broadcast_leds()
            return self._leds.debug_status()

        @app.websocket("/ws")
        async def websocket_endpoint(ws: WebSocket):
            await ws.accept()
            self._clients.add(ws)
            log.debug("WebSocket client connected (%d total)", len(self._clients))
            try:
                # Send initial state
                r = self._sensors.readings
                await ws.send_json({
                    "type": "full_state",
                    "valve": self._valve.state.value,
                    "pressure": round(r.pressure, 3),
                    "temperature": round(r.temperature, 1),
                    "flow_rate": round(r.flow_rate, 1),
                    "alarm": {
                        "active": self._alarms.state.active,
                        "type": self._alarms.state.alarm_type,
                        "message": self._alarms.state.message,
                    },
                    "leds": self._leds.debug_status(),
                })
                # Keep alive — read incoming messages (commands)
                while True:
                    data = await ws.receive_text()
                    await self._handle_ws_message(data)
            except WebSocketDisconnect:
                pass
            finally:
                self._clients.discard(ws)
                log.debug("WebSocket client disconnected (%d remaining)", len(self._clients))
                # Nobody is watching — don't leave the ring stuck in debug
                # colours where it can no longer show valve/alarm state.
                if not self._clients and self._leds.debug_active:
                    await self._leds.exit_debug()

    def _spawn(self, coro) -> None:
        """Run a coroutine in the background, keeping a reference until done."""
        task = asyncio.ensure_future(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _handle_ws_message(self, data: str) -> None:
        try:
            msg = json.loads(data)
            cmd = msg.get("command")
            if cmd == "valve_open":
                await self._valve.open_valve()
            elif cmd == "valve_close":
                await self._valve.close_valve()
            elif cmd == "pressure_test":
                if self._pressure_test_callback:
                    self._spawn(self._pressure_test_callback())
            elif cmd == "alarm_reset":
                await self._alarms.clear_alarm()
            elif cmd == "led_debug":
                if msg.get("enabled"):
                    await self._leds.enter_debug()
                else:
                    await self._leds.exit_debug()
                await self._broadcast_leds()
            elif cmd == "led_set":
                await self._leds.debug_set_pixel(
                    int(msg["index"]), parse_color(msg["color"])
                )
                await self._broadcast_leds()
            elif cmd == "led_set_all":
                await self._leds.debug_set_all(parse_color(msg["color"]))
                await self._broadcast_leds()
            elif cmd == "led_animation":
                await self._leds.debug_run_animation(str(msg["direction"]))
                await self._broadcast_leds()
        except Exception:
            log.exception("Error handling WebSocket message")

    async def _broadcast(self, message: dict) -> None:
        dead: list[WebSocket] = []
        # Snapshot: clients connect/disconnect concurrently with the awaits
        # below, and mutating the set mid-iteration raises RuntimeError.
        for ws in list(self._clients):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    async def _broadcast_leds(self) -> None:
        """Push full LED debug state so every open tab stays in sync."""
        await self._broadcast({"type": "leds", **self._leds.debug_status()})

    async def _broadcast_sensors(
        self, pressure: float, temperature: float, flow_rate: float
    ) -> None:
        await self._broadcast({
            "type": "sensor_update",
            "pressure": round(pressure, 3),
            "temperature": round(temperature, 1),
            "flow_rate": round(flow_rate, 1),
        })

    async def _broadcast_valve(self, state: str) -> None:
        await self._broadcast({"type": "valve_state", "state": state})

    async def _broadcast_alarm(self, alarm_type: str, message: str) -> None:
        await self._broadcast({
            "type": "alarm",
            "active": True,
            "alarm_type": alarm_type,
            "message": message,
        })

    async def _broadcast_alarm_cleared(self) -> None:
        await self._broadcast({"type": "alarm", "active": False})

    async def _broadcast_test_started(self, test_type: str) -> None:
        await self._broadcast({
            "type": "pressure_test",
            "status": "running",
            "test_type": test_type,
        })

    async def _broadcast_test_completed(
        self, result: str, initial_pressure: float | None, final_pressure: float | None
    ) -> None:
        await self._broadcast({
            "type": "pressure_test",
            "status": "completed",
            "result": result,
            "initial_pressure": initial_pressure,
            "final_pressure": final_pressure,
        })

    async def _broadcast_test_progress(
        self, stage: int, reading: int, total: int, pressure: float
    ) -> None:
        await self._broadcast({
            "type": "pressure_test_progress",
            "stage": stage,
            "reading": reading,
            "total": total,
            "pressure": round(pressure, 3),
        })
