# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

AquaGuard is a standalone Python replacement for the Fairtrail VFB cloud water-management service. It runs on a Raspberry Pi 3A+ (target host referred to below as `<pi-host>` — substitute your device's hostname or LAN IP), drives the original VFB hardware (motorized valve over I2C, pressure ADC, 1-wire temp sensor, SPI flow sensor, WS281x LED ring, buzzer, buttons), and integrates with Home Assistant via MQTT discovery. The legacy Node.js codebase lives under `fairtrail/` for reference only (it is git-ignored).

## Commands

```bash
# Local install (editable) — RPi.GPIO/rpi-ws281x will not import off-device, but tests use mocks
pip install -e ".[dev]"

# Run the full app locally (will fail on hardware import unless on Pi)
python -m aquaguard --config config-local.yaml

# Tests — pytest-asyncio is in `auto` mode (no @pytest.mark.asyncio needed)
pytest                         # all tests
pytest tests/test_valve.py     # one file
pytest -k pressure_drop -xvs   # one test by name

# Deploy to RPi (root owns /opt/aquaguard, so rsync as root)
# --exclude '__pycache__': the local .pyc files are built for the dev machine's
# Python and are dead weight on the Pi, which may run a different minor version.
rsync -av --exclude '__pycache__' --rsync-path="sudo rsync" \
    aquaguard/ pi@<pi-host>:/opt/aquaguard/aquaguard/
ssh pi@<pi-host> 'sudo systemctl restart aquaguard'
ssh pi@<pi-host> 'sudo journalctl -u aquaguard -f'

# Before touching config on the device, back it up:
sudo cp /etc/aquaguard/config.yaml /etc/aquaguard/config.yaml.bak
```

Host-level settings that the app and `install.sh` do *not* manage (WiFi powersave off,
persistent journal, and what is deliberately left stock — audio, clocks, governor) are
documented in README under "Host system settings", with the drop-ins in `deploy/system/`.
Check there before blaming the code for a hardware or connectivity symptom.

Entry point is `python -m aquaguard` (see `aquaguard/__main__.py`). The systemd unit (`deploy/aquaguard.service`) runs as **root** — required for rpi-ws281x DMA/PWM and GPIO. `ProtectSystem=strict` with `ReadWritePaths=/var/lib/aquaguard`, so the app cannot write outside the data dir.

## Architecture

Single async process. `AquaGuardApp` in `aquaguard/app.py` is the composition root — it constructs every component, wires them together, and runs everything inside one `asyncio.TaskGroup`. There is no DI framework; dependencies are passed explicitly in `__init__`. To add a new long-running loop, instantiate it in `__init__`, init it in `_init_all`, and `tg.create_task(...)` it in `run`.

**EventBus (`aquaguard/event_bus.py`) is the only inter-component channel.** Components don't call each other directly for state changes — they emit events and subscribe to them. Two rules that callers and subscribers both rely on:

- `subscribe(event, callback)` — callback **must be `async` and accept `**kwargs`**, because `emit` always calls subscribers with keyword args (`await cb(**kwargs)`).
- `await emit(event, **kwargs)` — fan-out is concurrent via `asyncio.gather(..., return_exceptions=True)`, so one subscriber's exception does not block the others, but exceptions are only logged. Emitters cannot rely on subscriber success.

Common event names (grep for `emit(` to find producers, `subscribe(` for consumers): `sensor_update`, `valve_state_changed`, `alarm_triggered`, `alarm_cleared`, `pressure_test_started/progress/completed`, `scheduled_test_passed`, button events from `Buttons`.

**Three layers, with strict direction of dependency (top → bottom):**

1. `hardware/` — thin wrappers over I2C/SPI/GPIO. `I2CBus`, `ValveDriver`, `PressureSensor`, `TemperatureSensor` (1-wire), `FlowSensor` (SPI), `LedRing` (WS281x), `Buzzer`, `Buttons`, `GpioAlarm`. These are the only modules that touch real hardware. Tests mock them via `tests/conftest.py` (`AsyncMock(spec=I2CBus)` etc.).
2. `services/` — domain logic. `ValveService`, `SensorService`, `AlarmManager`, `PressureTestService`, `Scheduler`. Services hold state, emit events, and call hardware.
3. `integrations/` + `web/` — outward-facing. `MqttClient`, `HADiscovery`, `WebServer` (FastAPI + WebSocket). They subscribe to events and translate them outward; they never own state. The web dashboard is deliberately unauthenticated (trusted-LAN decision, see README "Security model") — don't re-flag it or add auth without being asked.

**Storage** (`aquaguard/storage/`): `StateStore` is a JSON file (`/var/lib/aquaguard/state.json`) for valve open/closed and latched alarms — survives restart. `Database` is SQLite (`/var/lib/aquaguard/aquaguard.db`) for pressure-test history and flow episodes.

**Pressure test (`services/pressure_test.py`)** is reverse-engineered from the legacy `pipePressure.js`:
- Stage 1: 60 readings × 250 ms (15 s). Final reading < `low_pressure_threshold` (default 1.5 bar) → `ALARM1`, abort.
- Stage 2: 3600 readings × 250 ms (15 min). Drop > `leak_drop_threshold` (1.0 bar) → `ALARM2`, abort.
- Otherwise `PASS`.

**Scheduler (`services/scheduler.py`)** polls every 30 s. A test fires only when (a) `now.weekday() == config.schedule_weekday`, (b) at least `schedule_interval_weeks * 7` days have passed since the last scheduled run (persisted in StateStore as `last_scheduled_test_date`, seeded from the pressure-test table on first run), and (c) `now` is inside the 30-minute window after the scheduled HH:MM. The window guard prevents accidental mid-day tests on service restart. Default schedule is **every other Sunday at 02:00** (configurable via `pressure_test` block in `config.yaml`). On failure, up to 3 retries 60 min apart; sequence is *close valve → test → open valve* with a 10 s wait for the valve motor.

**HA discovery (`integrations/ha_discovery.py`)** publishes MQTT discovery configs with `retain=True` on connect. Topic shape: `homeassistant/<component>/aquaguard_{id}/<entity>/config` for discovery, `aquaguard/{id}/<component>/<entity>/state` for state, `aquaguard/{id}/availability` for LWT. State is republished every 30 s and on event.

## Config

`aquaguard/config.py` defines nested `@dataclass` defaults; `load_config(path)` overlays a YAML file onto them via `_apply_dict`, warning on unknown keys. The same shape is used for `config.yaml` (deployed) and `config-local.yaml` (local dev — gitignored). When adding a new config field, add it to the dataclass first; YAML keys without a dataclass field are dropped with a warning.
