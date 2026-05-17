"""YAML configuration with dataclass defaults."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


@dataclass
class DeviceConfig:
    name: str = "AquaGuard"
    id: str = "aquaguard01"


@dataclass
class MqttConfig:
    host: str = "192.168.1.10"
    port: int = 1883
    username: str = ""
    password: str = ""


@dataclass
class WebConfig:
    port: int = 8080


@dataclass
class HardwareConfig:
    i2c_bus: int = 1
    valve_address: int = 0x60
    pressure_address: int = 0x48
    temp_device_id: str = "28-0301a2791e31"
    motor_poweroff_delay: int = 8
    led_count: int = 34
    buzzer_pin: int = 23
    button_on_pin: int = 19
    button_off_pin: int = 26
    button_reset_pin: int = 24


@dataclass
class AlarmsConfig:
    low_temp_threshold: float = 1.6
    low_pressure_threshold: float = 1.5
    leak_drop_threshold: float = 1.0


@dataclass
class PressureTestConfig:
    schedule_hour: int = 2
    schedule_minute: int = 0
    schedule_weekday: int = 6  # 0=Monday, 6=Sunday
    schedule_interval_weeks: int = 2


@dataclass
class ConsumptionConfig:
    long_episode_minutes: int = 60
    episode_volume_liters: float = 300.0
    drip_min_flow: float = 5.0
    drip_max_flow: float = 25.0
    drip_duration_minutes: int = 120


@dataclass
class StorageConfig:
    db_path: str = "/var/lib/aquaguard/aquaguard.db"
    state_path: str = "/var/lib/aquaguard/state.json"


@dataclass
class AppConfig:
    device: DeviceConfig = field(default_factory=DeviceConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    web: WebConfig = field(default_factory=WebConfig)
    hardware: HardwareConfig = field(default_factory=HardwareConfig)
    alarms: AlarmsConfig = field(default_factory=AlarmsConfig)
    pressure_test: PressureTestConfig = field(default_factory=PressureTestConfig)
    consumption: ConsumptionConfig = field(default_factory=ConsumptionConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)


def _apply_dict(dc: object, data: dict) -> None:
    """Recursively apply dict values onto a dataclass instance."""
    for key, value in data.items():
        if not hasattr(dc, key):
            log.warning("Unknown config key: %s", key)
            continue
        current = getattr(dc, key)
        if isinstance(value, dict) and hasattr(current, "__dataclass_fields__"):
            _apply_dict(current, value)
        else:
            setattr(dc, key, value)


def load_config(path: str | Path) -> AppConfig:
    """Load config from YAML, then overlay a sibling 'config-secrets.yaml' if present.

    The secrets file holds values that must not be committed (MQTT password etc.).
    It uses the same dataclass schema; only the keys present in it are overridden.
    """
    cfg = AppConfig()
    path = Path(path)
    if path.exists():
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        _apply_dict(cfg, data)
        log.info("Loaded config from %s", path)
    else:
        log.warning("Config file %s not found, using defaults", path)

    secrets_path = path.parent / "config-secrets.yaml"
    if secrets_path.exists():
        with open(secrets_path) as f:
            secrets = yaml.safe_load(f) or {}
        _apply_dict(cfg, secrets)
        log.info("Overlaid secrets from %s", secrets_path)
    return cfg
