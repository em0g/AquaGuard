"""Tests for pressure sensor driver."""

from __future__ import annotations

import pytest

from aquaguard.hardware.pressure import PressureSensor


class TestPressureSensor:
    async def test_init_configures_ads1015(self, pressure_sensor, mock_i2c):
        await pressure_sensor.init()
        mock_i2c.write_i2c_block_data.assert_called_with(
            0x48, 0x01, [0xC4, 0x03]
        )

    async def test_read_pressure_zero(self, pressure_sensor, mock_i2c):
        """With raw bytes [0, 0] → pressure should be -2.5 bar (no pressure)."""
        mock_i2c.read_i2c_block_data.return_value = [0x00, 0x00]
        pressure = await pressure_sensor.read_pressure()
        assert pressure == pytest.approx(-2.5, abs=0.01)

    async def test_read_pressure_known_value(self, pressure_sensor, mock_i2c):
        """Test with a known raw value.

        ADS1015 returns MSB first. smbus read_i2c_block_data returns
        device order [MSB, LSB]. No byte swap needed.

        raw = (0x3C << 8) | 0x00 = 0x3C00 = 15360
        pressure = 15360 * 0.000390625 - 2.5 = 6.0 - 2.5 = 3.5 bar
        """
        mock_i2c.read_i2c_block_data.return_value = [0x3C, 0x00]
        pressure = await pressure_sensor.read_pressure()
        assert pressure == pytest.approx(3.5, abs=0.01)

    async def test_read_pressure_typical_value(self, pressure_sensor, mock_i2c):
        """Test with a typical reading from real hardware.

        raw = (0x4B << 8) | 0x60 = 0x4B60 = 19296
        pressure = 19296 * 0.000390625 - 2.5 = 7.5375 - 2.5 = 5.0375 bar
        """
        mock_i2c.read_i2c_block_data.return_value = [0x4B, 0x60]
        pressure = await pressure_sensor.read_pressure()
        assert pressure == pytest.approx(5.0375, abs=0.01)
