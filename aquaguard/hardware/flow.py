"""MAX35101 ultrasonic flow sensor via SPI.

Reverse-engineered from flow-app: support.c, flow.c, max3510x.c/h.

SPI protocol:
  Mode 1 (CPOL=0, CPHA=1), MSB first, CS0, ~1 MHz
  Opcodes: TOF_DIFF=0x02, RESET=0x04, INITIALIZE=0x05, FLASH=0x06
  Register read: 0x80|reg, register write: reg directly
  Registers are 16-bit big-endian

Measurement sequence:
  1. Reset chip, wait for reset complete
  2. Write TOF1-TOF7, EVENT_TIMING, DELAY, CALIBRATION, RTC registers
  3. Flash config, Initialize
  4. Loop: send TOF_DIFF → poll interrupt status → read tof_diff → compute flow

Physics:
  speed = tof_diff_seconds * Factor  (m/s)
  Factor = Soundspeed² / (2 * Pipe_diameter)
  volume_flow = speed * Probe_area * 3600 * 1000  (L/h)
"""

from __future__ import annotations

import asyncio
import logging
import math
import struct
import time
from enum import Enum

log = logging.getLogger(__name__)

# ── Physics constants (from support.c) ──────────────────────────────────
PIPE_DIAMETER = 0.020  # m
SOUNDSPEED_SQUARED = 2193361.0
PROBE_AREA = math.pi / 4 * PIPE_DIAMETER**2
FACTOR = SOUNDSPEED_SQUARED / (2 * PIPE_DIAMETER)
FREQ_REF = 4_000_000.0  # MAX35101 reference clock
FIXED_TO_FLOAT_CONST = 1.0 / (FREQ_REF * 65536.0)

# Calibration curve (from kalibrering.png, R² = 0.9993)
CAL_SLOPE = 1.1863
CAL_INTERCEPT = -9.693

# Flow thresholds (L/h)
FLOW_START_THRESHOLD = 30.0
FLOW_END_THRESHOLD = 20.0

# Measurement
SAMPLES_PER_READING = 30  # matches original C code
MAX_TOF_DIFF = 1e-7  # sanity check — ~2 L/s max
FLOW_DEBOUNCE_COUNT = 3  # consecutive readings above threshold to start episode

# Display smoothing — EMA (exponential moving average)
EMA_ALPHA = 0.3  # weight of new sample (0.3 = slow response, good noise rejection)
DISPLAY_FLOOR = 5.0  # L/h — values below this shown as 0 on dashboard

# ── MAX35101 register addresses ─────────────────────────────────────────
_REG_TOF1 = 0x38
_REG_TOF2 = 0x39
_REG_TOF3 = 0x3A
_REG_TOF4 = 0x3B
_REG_TOF5 = 0x3C
_REG_TOF6 = 0x3D
_REG_TOF7 = 0x3E
_REG_EVENT_TIMING_1 = 0x3F
_REG_EVENT_TIMING_2 = 0x40
_REG_TOF_MEASUREMENT_DELAY = 0x41
_REG_CALIBRATION_CONTROL = 0x42
_REG_RTC = 0x43
_REG_INTERRUPT_STATUS = 0xFE

# Interrupt status bits
_ISR_TOF = 1 << 12
_ISR_TIMEOUT = 1 << 15
_ISR_INVALID = 0xFFFF
_ISR_POR = 1 << 2
_ISR_FLASH = 1 << 7

# Opcodes
_OP_TOF_DIFF = 0x02
_OP_RESET = 0x04
_OP_INITIALIZE = 0x05
_OP_FLASH = 0x06

# TOF results: tof_diff is at byte offset 61 in the full result struct
# up_measurement (30 bytes) + down_measurement (30 bytes) = 60 bytes after start_register
# tof_diff = 4 bytes (int16 integer + uint16 fraction)
_TOF_DIFF_OFFSET = 61  # from start of read (after the opcode byte)

# ── Default register values (from flow.c handler defaults) ──────────────
# TOF1: no_pulses=15, pulse_launch_divider=1 (1MHz), edge=0, bias_charge=0
_DEF_TOF1 = (0x0F, 0x10)
# TOF2: stop_hits=5 (6 hits), wave_selection=3, duty_cycle=5 (976µs), 1MHz leading edge
_DEF_TOF2 = (0xA1, 0xD1)
# TOF3: hit1=6, hit2=7
_DEF_TOF3 = (0x06, 0x07)
# TOF4: hit3=8, hit4=9
_DEF_TOF4 = (0x08, 0x09)
# TOF5: hit5=10, hit6=11
_DEF_TOF5 = (0x0A, 0x0B)
# TOF6: 0, 0
_DEF_TOF6 = (0x00, 0x00)
# TOF7: 0, 0
_DEF_TOF7 = (0x00, 0x00)
# Event timing 1: 0, 0
_DEF_EVT1 = (0x00, 0x00)
# Event timing 2: 0, 0
_DEF_EVT2 = (0x00, 0x00)
# Measurement delay: 0, 170 (0xAA)
_DEF_DELAY = (0x00, 0xAA)
# Calibration control: 0, 66 (0x42)
_DEF_CAL = (0x00, 0x42)
# RTC: 0, 0
_DEF_RTC = (0x00, 0x00)

_REGISTER_WRITE_TRIES = 3


class FlowState(Enum):
    STARTUP = "startup"
    IDLE = "idle"
    FLOW = "flow"


class FlowSensor:
    """MAX35101 ultrasonic flow sensor via SPI."""

    def __init__(
        self,
        spi_bus: int = 0,
        spi_device: int = 0,
        spi_speed_hz: int = 1_000_000,
        measure_interval: float = 1.0,
    ):
        self._spi_bus = spi_bus
        self._spi_device = spi_device
        self._spi_speed = spi_speed_hz
        self._measure_interval = measure_interval
        self._spi = None
        self._state = FlowState.STARTUP
        self._flow_rate = 0.0
        self._episode_volume = 0.0
        self._episode_duration = 0.0
        self._episode_start: float | None = None
        self._episode_callback = None
        self._measurement_callback = None
        self._above_threshold_count = 0
        self._ema_flow = 0.0  # smoothed flow for display
        self.available = False

    def set_episode_callback(self, callback) -> None:
        """Register async callback(volume, duration) for completed episodes."""
        self._episode_callback = callback

    def set_measurement_callback(self, callback) -> None:
        """Register async callback invoked after each measurement cycle.

        Signature: callback(flow_lph, state, episode_volume, episode_duration)
        where flow_lph is the EMA-smoothed flow rate (unfloored — may be sub-5
        for low-flow leak detection).
        """
        self._measurement_callback = callback

    @property
    def flow_rate(self) -> float:
        """Smoothed flow rate in L/h for dashboard display.

        Uses EMA filtering and a display floor to suppress noise.
        """
        if self._ema_flow < DISPLAY_FLOOR:
            return 0.0
        return round(self._ema_flow, 1)

    @property
    def state(self) -> FlowState:
        return self._state

    # ── SPI primitives ──────────────────────────────────────────────────

    def _spi_xfer(self, data: list[int]) -> list[int]:
        """Synchronous SPI transfer."""
        return self._spi.xfer2(data)

    def _send_opcode(self, opcode: int) -> None:
        self._spi_xfer([opcode])

    def _read_register(self, reg: int) -> int:
        """Read a 16-bit register (big-endian)."""
        result = self._spi_xfer([0x80 | reg, 0x00, 0x00])
        return (result[1] << 8) | result[2]

    def _write_register(self, reg: int, byte1: int, byte2: int) -> bool:
        """Write a 16-bit register and verify. Returns True on success."""
        for _ in range(_REGISTER_WRITE_TRIES):
            self._spi_xfer([reg, byte1, byte2])
            time.sleep(0.01)
            result = self._spi_xfer([0x80 | reg, 0x00, 0x00])
            time.sleep(0.01)
            if result[1] == byte1 and result[2] == byte2:
                return True
        return False

    def _read_interrupt_status(self) -> int:
        return self._read_register(_REG_INTERRUPT_STATUS)

    def _poll_interrupt_status(self, timeout: float = 1.0) -> int:
        """Poll until interrupt status is non-zero or timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self._read_interrupt_status()
            if status != 0:
                return status
            time.sleep(0.001)
        return 0

    def _read_tof_diff(self) -> tuple[int, int] | None:
        """Read tof_diff from full TOF results (sequential read from WVRUP).

        Must read sequentially from WVRUP (0xC4) — random-access may not work.
        Result layout: up(30 bytes) + down(30 bytes) + tof_diff(4 bytes) = 64 bytes.
        Returns (int16 integer, uint16 fraction) or None.
        """
        # Read 65 bytes: 1 address + 64 data (up + down + tof_diff)
        buf = [0x80 | 0xC4] + [0] * 64
        result = self._spi_xfer(buf)
        # Data starts at index 1 (index 0 is junk during address byte)
        # tof_diff at byte offset 60: up(30) + down(30)
        data = result[1:]  # 64 bytes of actual data
        integer = struct.unpack(">h", bytes(data[60:62]))[0]   # signed int16
        fraction = struct.unpack(">H", bytes(data[62:64]))[0]  # unsigned uint16
        return integer, fraction

    # ── Initialization ──────────────────────────────────────────────────

    async def init(self) -> None:
        """Initialize SPI and configure MAX35101."""
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._init_spi)
            self.available = True
            self._state = FlowState.IDLE
            log.info("MAX35101 flow sensor initialised via SPI%d.%d",
                     self._spi_bus, self._spi_device)
        except Exception:
            log.warning("Flow sensor SPI init failed — running in stub mode",
                        exc_info=True)
            self.available = False
            self._state = FlowState.IDLE

    def _init_spi(self) -> None:
        """Synchronous SPI init + MAX35101 configuration."""
        import spidev

        # Deactivate second MAX35101 on CE1 (GPIO 7) — must be held HIGH
        try:
            import RPi.GPIO as GPIO
            GPIO.setwarnings(False)
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(7, GPIO.OUT, initial=GPIO.HIGH)
            log.debug("GPIO 7 (CE1) set HIGH — second MAX35101 disabled")
        except Exception:
            log.debug("Could not set GPIO 7, continuing")

        spi = spidev.SpiDev()
        spi.open(self._spi_bus, self._spi_device)
        spi.mode = 1  # SPI Mode 1 (CPOL=0, CPHA=1)
        spi.max_speed_hz = self._spi_speed
        spi.bits_per_word = 8
        self._spi = spi

        # Reset
        log.debug("MAX35101: sending reset")
        self._send_opcode(_OP_RESET)
        time.sleep(0.01)

        # Wait for reset complete (ISR becomes valid)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            isr = self._read_interrupt_status()
            if isr != _ISR_INVALID:
                break
            time.sleep(0.01)
        else:
            raise RuntimeError("MAX35101 reset timeout")

        log.debug("MAX35101: reset complete, ISR=0x%04X", isr)

        # Write TOF configuration registers
        regs = [
            (_REG_TOF1, *_DEF_TOF1),
            (_REG_TOF2, *_DEF_TOF2),
            (_REG_TOF3, *_DEF_TOF3),
            (_REG_TOF4, *_DEF_TOF4),
            (_REG_TOF5, *_DEF_TOF5),
            (_REG_TOF6, *_DEF_TOF6),
            (_REG_TOF7, *_DEF_TOF7),
            (_REG_EVENT_TIMING_1, *_DEF_EVT1),
            (_REG_EVENT_TIMING_2, *_DEF_EVT2),
            (_REG_TOF_MEASUREMENT_DELAY, *_DEF_DELAY),
            (_REG_CALIBRATION_CONTROL, *_DEF_CAL),
            (_REG_RTC, *_DEF_RTC),
        ]
        for reg, b1, b2 in regs:
            if not self._write_register(reg, b1, b2):
                raise RuntimeError(
                    f"MAX35101: failed to write register 0x{reg:02X}"
                )

        log.debug("MAX35101: TOF registers configured")
        time.sleep(0.1)

        # Flash configuration to non-volatile memory
        self._send_opcode(_OP_FLASH)
        time.sleep(1.0)
        isr = self._read_interrupt_status()
        log.debug("MAX35101: flash done, ISR=0x%04X", isr)

        # Initialize
        self._send_opcode(_OP_INITIALIZE)
        isr = self._poll_interrupt_status(timeout=2.0)
        log.debug("MAX35101: init done, ISR=0x%04X", isr)

    # ── Measurement ─────────────────────────────────────────────────────

    def _do_single_measurement(self) -> float | None:
        """Trigger TOF_DIFF and read result. Returns tof_diff in seconds or None."""
        self._send_opcode(_OP_TOF_DIFF)
        status = self._poll_interrupt_status(timeout=0.5)

        if not (status & _ISR_TOF) or (status & _ISR_TIMEOUT):
            return None

        result = self._read_tof_diff()
        if result is None:
            return None

        integer, fraction = result
        # Convert fixed-point to float: (integer << 16 | fraction) / (FREQ_REF * 65536)
        raw = (integer << 16) | fraction
        tof_diff = raw * FIXED_TO_FLOAT_CONST

        # Sanity check
        if abs(tof_diff) > MAX_TOF_DIFF:
            return None

        return tof_diff

    def _measure_averaged(self) -> float | None:
        """Take SAMPLES_PER_READING measurements and return averaged tof_diff."""
        total = 0.0
        valid = 0
        for _ in range(SAMPLES_PER_READING):
            td = self._do_single_measurement()
            if td is not None:
                total += td
                valid += 1

        if valid < 10:
            return None

        return total / valid

    @staticmethod
    def tof_diff_to_flow_rate(tof_diff: float) -> float:
        """Convert averaged tof_diff (seconds) to calibrated flow rate in L/h."""
        speed = tof_diff * FACTOR  # m/s
        raw_flow = speed * PROBE_AREA * 3600 * 1000  # L/h (uncalibrated)
        # Apply calibration: y = 1.1863x - 9.693 (from kalibrering.png)
        calibrated = CAL_SLOPE * raw_flow + CAL_INTERCEPT
        return max(0.0, calibrated)

    # ── Main loop ───────────────────────────────────────────────────────

    async def run(self) -> None:
        """Continuous measurement loop with flow episode state machine."""
        if not self.available:
            log.info("Flow sensor not available, measurement loop inactive")
            while True:
                await asyncio.sleep(60)

        log.info("Flow sensor measurement loop started (interval=%.1fs)",
                 self._measure_interval)
        loop = asyncio.get_running_loop()
        last_time = time.monotonic()

        while True:
            await asyncio.sleep(self._measure_interval)

            try:
                tof_diff = await loop.run_in_executor(None, self._measure_averaged)
            except Exception:
                log.exception("Flow measurement error")
                self._ema_flow *= (1 - EMA_ALPHA)  # decay toward zero
                continue

            now = time.monotonic()
            dt = now - last_time
            last_time = now

            if tof_diff is None:
                # Not enough valid samples — decay EMA toward zero
                self._ema_flow *= (1 - EMA_ALPHA)
                continue

            flow_lph = self.tof_diff_to_flow_rate(tof_diff)
            speed_ms = tof_diff * FACTOR
            # Update EMA for display smoothing
            self._ema_flow = EMA_ALPHA * flow_lph + (1 - EMA_ALPHA) * self._ema_flow

            log.debug("Flow: %.1f L/h (speed=%.2f mm/s)", flow_lph, speed_ms * 1000)

            # State machine
            if self._state == FlowState.IDLE:
                if flow_lph > FLOW_START_THRESHOLD:
                    self._above_threshold_count += 1
                    if self._above_threshold_count >= FLOW_DEBOUNCE_COUNT:
                        self._state = FlowState.FLOW
                        self._episode_start = time.time()
                        volume_liters = speed_ms * PROBE_AREA * 1000 * dt
                        self._episode_volume = volume_liters
                        self._episode_duration = dt
                        log.info("Flow episode started (%.1f L/h)", flow_lph)
                else:
                    self._above_threshold_count = 0

            elif self._state == FlowState.FLOW:
                volume_liters = speed_ms * PROBE_AREA * 1000 * dt
                self._episode_volume += volume_liters
                self._episode_duration += dt

                if flow_lph < FLOW_END_THRESHOLD:
                    log.info("Flow episode ended: %.3f L in %.0f s",
                             self._episode_volume, self._episode_duration)
                    if self._episode_callback:
                        try:
                            await self._episode_callback(
                                self._episode_volume,
                                round(self._episode_duration),
                            )
                        except Exception:
                            log.exception("Episode callback error")
                    self._episode_volume = 0.0
                    self._episode_duration = 0.0
                    self._episode_start = None
                    self._state = FlowState.IDLE

            if self._measurement_callback:
                try:
                    await self._measurement_callback(
                        flow_lph=self._ema_flow,
                        state=self._state,
                        episode_volume=self._episode_volume,
                        episode_duration=self._episode_duration,
                    )
                except Exception:
                    log.exception("Measurement callback error")

    # ── Cleanup ─────────────────────────────────────────────────────────

    def cleanup(self) -> None:
        if self._spi is not None:
            try:
                self._spi.close()
            except Exception:
                pass
            self._spi = None
