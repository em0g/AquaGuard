"""Tests for TemperatureSensor — device discovery and parsing."""

from __future__ import annotations

import pytest

from aquaguard.hardware import temperature
from aquaguard.hardware.temperature import TemperatureSensor


@pytest.fixture
def w1_base(tmp_path, monkeypatch):
    """Redirect the 1-wire sysfs root at a temp dir."""
    monkeypatch.setattr(temperature, "_W1_BASE", tmp_path)
    return tmp_path


def _make_device(base, device_id: str, millidegrees: int = 15750) -> None:
    d = base / device_id
    d.mkdir()
    (d / "w1_slave").write_text(
        f"aa bb cc : crc=cc YES\naa bb cc t={millidegrees}\n"
    )


class TestDiscovery:
    def test_empty_id_picks_the_only_sensor(self, w1_base):
        _make_device(w1_base, "28-000000000001")
        sensor = TemperatureSensor()
        assert sensor.available
        assert sensor._device_id == "28-000000000001"

    def test_explicit_id_is_respected(self, w1_base):
        _make_device(w1_base, "28-aaaaaaaaaaaa")
        _make_device(w1_base, "28-bbbbbbbbbbbb")
        sensor = TemperatureSensor("28-bbbbbbbbbbbb")
        assert sensor._device_id == "28-bbbbbbbbbbbb"

    def test_multiple_sensors_picks_first_deterministically(self, w1_base):
        _make_device(w1_base, "28-bbbbbbbbbbbb")
        _make_device(w1_base, "28-aaaaaaaaaaaa")
        assert TemperatureSensor()._device_id == "28-aaaaaaaaaaaa"

    def test_no_sensor_is_unavailable(self, w1_base):
        sensor = TemperatureSensor()
        assert not sensor.available
        assert sensor._device_id == ""

    def test_non_ds18b20_devices_ignored(self, w1_base):
        (w1_base / "w1_bus_master1").mkdir()
        assert not TemperatureSensor().available


class TestReading:
    async def test_reads_and_parses(self, w1_base):
        _make_device(w1_base, "28-000000000001", millidegrees=15750)
        assert await TemperatureSensor().read_temperature() == pytest.approx(15.75)

    async def test_missing_sensor_returns_fallback(self, w1_base):
        assert await TemperatureSensor().read_temperature() == 99.0

    async def test_negative_temperature(self, w1_base):
        _make_device(w1_base, "28-000000000001", millidegrees=-4125)
        assert await TemperatureSensor().read_temperature() == pytest.approx(-4.125)

    async def test_failed_crc_returns_fallback(self, w1_base):
        """A 'NO' CRC line means the t= value is garbage — never trust it."""
        _make_device(w1_base, "28-000000000001")
        sensor = TemperatureSensor()
        (w1_base / "28-000000000001" / "w1_slave").write_text(
            "aa bb cc : crc=cc NO\naa bb cc t=1250\n"
        )
        assert await sensor.read_temperature() == 99.0

    async def test_power_on_reset_value_discarded(self, w1_base):
        """85.000 °C is the DS18B20 power-on scratchpad default, not a
        reading — on a water pipe it can only mean the sensor never
        converted."""
        _make_device(w1_base, "28-000000000001", millidegrees=85000)
        assert await TemperatureSensor().read_temperature() == 99.0

    async def test_near_85_is_a_real_reading(self, w1_base):
        """Only the exact power-on value is discarded."""
        _make_device(w1_base, "28-000000000001", millidegrees=84937)
        assert await TemperatureSensor().read_temperature() == pytest.approx(84.937)
