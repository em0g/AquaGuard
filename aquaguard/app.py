"""Async orchestrator — starts all components in a single TaskGroup."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

import uvicorn

from aquaguard.config import AppConfig
from aquaguard.event_bus import EventBus
from aquaguard.hardware.buzzer import Buzzer
from aquaguard.hardware.buttons import Buttons
from aquaguard.hardware.flow import FlowSensor
from aquaguard.hardware.gpio_alarm import GpioAlarm
from aquaguard.hardware.i2c_bus import I2CBus
from aquaguard.hardware.leds import LedRing
from aquaguard.hardware.pressure import PressureSensor
from aquaguard.hardware.temperature import TemperatureSensor
from aquaguard.hardware.valve import ValveDriver
from aquaguard.integrations.ha_discovery import HADiscovery
from aquaguard.integrations.mqtt_client import MqttClient
from aquaguard.services.alarm_manager import AlarmManager
from aquaguard.services.consumption_monitor import ConsumptionMonitor
from aquaguard.services.pressure_test import PressureTestService
from aquaguard.services.scheduler import Scheduler
from aquaguard.services.sensor_service import SensorService
from aquaguard.services.valve_service import ValveService
from aquaguard.storage.database import Database
from aquaguard.storage.state import StateStore
from aquaguard.web.server import WebServer

log = logging.getLogger(__name__)

# How long to let uvicorn unwind on its own before the TaskGroup tears
# everything down. Bounded so a stuck web server can never hold up the
# service stop; systemd's own limit is 90 s.
WEB_SHUTDOWN_TIMEOUT = 5.0


class _ShutdownRequested(Exception):
    """Raised inside the TaskGroup when SIGTERM/SIGINT arrives, so the group
    cancels every task and `run` falls through to cleanup."""


class _SignalFreeUvicornServer(uvicorn.Server):
    """uvicorn.Server.serve() replaces the process's SIGTERM/SIGINT handlers
    with its own, which only stops the web server — the rest of the TaskGroup
    would run on until systemd gives up and SIGKILLs us without cleanup.
    AquaGuardApp owns process signals, so uvicorn must not capture them."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


class AquaGuardApp:
    """Main application — wires everything together and runs as one process."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config

        # Event bus
        self.event_bus = EventBus()

        # Hardware
        self.i2c = I2CBus(config.hardware.i2c_bus)
        self.valve_driver = ValveDriver(
            self.i2c,
            address=config.hardware.valve_address,
            poweroff_delay=config.hardware.motor_poweroff_delay,
        )
        self.pressure_sensor = PressureSensor(
            self.i2c, address=config.hardware.pressure_address
        )
        self.temp_sensor = TemperatureSensor(
            device_id=config.hardware.temp_device_id
        )
        self.flow_sensor = FlowSensor(
            spi_bus=config.hardware.spi_bus,
            spi_device=config.hardware.spi_device,
            spi_speed_hz=config.hardware.spi_speed_hz,
        )
        self.buzzer = Buzzer(pin=config.hardware.buzzer_pin)
        self.buttons = Buttons(
            self.event_bus,
            on_pin=config.hardware.button_on_pin,
            off_pin=config.hardware.button_off_pin,
            reset_pin=config.hardware.button_reset_pin,
        )
        self.gpio_alarm = GpioAlarm(self.event_bus)
        self.leds = LedRing(self.event_bus, led_count=config.hardware.led_count)

        # Storage
        self.state_store = StateStore(config.storage.state_path)
        self.database = Database(config.storage.db_path)

        # Services
        self.valve_service = ValveService(
            self.valve_driver, self.state_store, self.event_bus,
            motor_delay=config.hardware.motor_poweroff_delay,
        )
        self.sensor_service = SensorService(
            self.pressure_sensor, self.temp_sensor, self.event_bus
        )
        self.sensor_service.set_flow_rate_getter(lambda: self.flow_sensor.flow_rate)
        self.alarm_manager = AlarmManager(
            config.alarms, self.event_bus, self.buzzer,
            self.gpio_alarm, self.state_store,
        )
        self.pressure_test = PressureTestService(
            self.pressure_sensor, config.alarms, self.event_bus, self.database,
            self.alarm_manager,
        )
        self.valve_service.set_test_interlock(self.pressure_test)
        self.scheduler = Scheduler(
            config.pressure_test, self.pressure_test, self.valve_service,
            self.event_bus, self.state_store, self.database,
        )
        self.consumption_monitor = ConsumptionMonitor(
            config.consumption, self.event_bus,
            valve_service=self.valve_service,
            alarm_manager=self.alarm_manager,
        )

        # Integrations
        self.mqtt = MqttClient(config.mqtt, config.device)
        self.ha_discovery = HADiscovery(
            config.device, self.mqtt, self.event_bus,
            self.valve_service, self.sensor_service, self.alarm_manager,
            self.consumption_monitor,
        )
        self.ha_discovery.set_pressure_test_callback(self._run_manual_test)

        # Web
        self.web_server = WebServer(
            self.event_bus, self.valve_service, self.sensor_service,
            self.alarm_manager, self.database, self.leds,
        )
        self.web_server.set_pressure_test_callback(self._run_manual_test)

    async def _run_manual_test(self) -> None:
        """Run a manual pressure test (triggered via web/MQTT)."""
        await self.pressure_test.run_test(test_type="manual")

    async def _init_all(self) -> None:
        """Initialise all components."""
        await self.i2c.open()
        await self.state_store.load()
        await self.database.open()

        await self.valve_service.init()
        await self.sensor_service.init()
        await self.flow_sensor.init()
        await self.buzzer.init()
        await self.buttons.init()
        await self.gpio_alarm.init()
        await self.leds.init()
        await self.alarm_manager.init()

        # Connect flow episode callback to database storage
        self.flow_sensor.set_episode_callback(self.database.save_episode)
        # Per-measurement callback feeds the consumption monitor
        self.flow_sensor.set_measurement_callback(
            self.consumption_monitor.on_measurement
        )

        await self.mqtt.connect()

        log.info("All components initialised")

    async def _cleanup(self) -> None:
        """Graceful shutdown."""
        log.info("Shutting down...")
        await self.mqtt.disconnect()
        await self.database.close()
        await self.i2c.close()
        self.flow_sensor.cleanup()
        self.buzzer.cleanup()
        self.gpio_alarm.cleanup()
        self.buttons.cleanup()
        # Dark ring = service not running; leaving the last frame lit would
        # show stale valve state.
        self.leds.cleanup()

    async def run(self) -> None:
        """Start all async tasks and run until SIGTERM/SIGINT."""
        await self._init_all()

        # Configure uvicorn for embedding
        uvi_config = uvicorn.Config(
            self.web_server.app,
            host="0.0.0.0",
            port=self.config.web.port,
            log_level="warning",
        )
        uvi_server = _SignalFreeUvicornServer(uvi_config)

        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)

        async def wait_for_shutdown(serve_task: asyncio.Task[None]) -> None:
            await stop.wait()
            # Ask uvicorn to close its listener and run its lifespan shutdown
            # first. Cancelling serve() outright works, but starlette logs the
            # resulting lifespan CancelledError as an ERROR traceback on every
            # single restart, which buries real errors in the journal.
            uvi_server.should_exit = True
            await asyncio.wait({serve_task}, timeout=WEB_SHUTDOWN_TIMEOUT)
            raise _ShutdownRequested

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self.sensor_service.run_polling_loop())
                tg.create_task(self.mqtt.run())
                tg.create_task(self.ha_discovery.publish_state_loop())
                tg.create_task(self.scheduler.run())
                tg.create_task(self.buttons.run())
                tg.create_task(self.gpio_alarm.run())
                tg.create_task(self.leds.run_animation_loop())
                tg.create_task(self.flow_sensor.run())
                serve_task = tg.create_task(uvi_server.serve())
                tg.create_task(wait_for_shutdown(serve_task))
                log.info(
                    "AquaGuard running — dashboard at http://0.0.0.0:%d",
                    self.config.web.port,
                )
        except* _ShutdownRequested:
            log.info("Shutdown signal received, stopping")
        finally:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(sig)
            await self._cleanup()
