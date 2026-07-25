# AquaGuard

Standalone Python replacement for the Fairtrail VFB cloud water-management service.
Runs on a Raspberry Pi, drives the original VFB hardware (motorized valve over I2C,
pressure ADC, 1-wire temperature sensor, SPI flow sensor, WS281x LED ring, buzzer,
physical buttons) and integrates with Home Assistant via MQTT discovery.

## Hardware

- Raspberry Pi 3A+ (or any Pi with I2C/SPI/1-Wire and enough GPIO)
- VFB valve actuator on I2C (default address `0x60`)
- Pressure ADC on I2C (default address `0x48`)
- DS18B20 temperature sensor on 1-Wire
- SPI flow sensor (default bus 0, device 0)
- WS281x LED ring on GPIO 18 (34 LEDs by default)
- Buzzer on GPIO 23
- Buttons (On/Off/Reset) on GPIO 19/26/24, read via `libgpiod`
- Optional external alarm in/out on GPIO 17/27

Pin/address defaults live in `config.yaml` and can be overridden there.

## Installing on a blank Raspberry Pi OS (Bookworm)

These steps assume a fresh Raspberry Pi OS Lite (64-bit) install, flashed with
Raspberry Pi Imager, with SSH enabled and on your network.

### 1. Enable I2C, SPI and 1-Wire

```bash
sudo raspi-config nonint do_i2c 0
sudo raspi-config nonint do_spi 0
sudo raspi-config nonint do_onewire 0
sudo reboot
```

(Or manually add to `/boot/firmware/config.txt`: `dtparam=i2c_arm=on`,
`dtparam=spi=on`, `dtoverlay=w1-gpio`.)

Verify after reboot. These device nodes only appear once the kernel has the
interface enabled, so "no `No such file or directory`" means I2C and SPI are on:

```bash
ls /dev/i2c-1 /dev/spidev0.0 /dev/gpiochip0
ls /sys/bus/w1/devices/            # should show a 28-xxxxxxxxxxxx entry — that's your sensor's device_id
```

Two caveats: `/dev/gpiochip0` exists on every Pi regardless (GPIO needs no
enabling), so only `i2c-1` and `spidev0.0` actually confirm anything here. And a
node existing only means the bus is exposed, not that a device answers on it — to
check the hardware actually responds, use `sudo i2cdetect -y 1` and look for the
expected addresses in the grid.

### 2. Install system dependencies

```bash
sudo apt update
sudo apt install -y git python3-venv python3-pip i2c-tools
```

(The `gpiod` PyPI wheel used for the buttons bundles libgpiod statically, so no
`libgpiod2`/`libgpiod3` system package is required. If you want the `gpioinfo` /
`gpioget` CLI tools for debugging, install `gpiod` — but the app does not need it.)

### 3. Clone and install

```bash
git clone <this-repo-url> aquaguard-src
cd aquaguard-src
sudo ./deploy/install.sh
```

`deploy/install.sh` creates `/opt/aquaguard` (app + venv), `/etc/aquaguard`
(config) and `/var/lib/aquaguard` (state/db), installs the systemd unit, and
enables it. It will not overwrite an existing `/etc/aquaguard/config.yaml`.

### 4. Configure

Edit `/etc/aquaguard/config.yaml`:

- `mqtt.host` — your MQTT broker's LAN IP or hostname
- `hardware.temp_device_id` — the `28-...` ID from step 1
- `hardware.valve_address` / `pressure_address` — only if your VFB unit uses
  different I2C addresses
- Adjust `alarms` / `pressure_test` / `consumption` thresholds to taste

Create the MQTT password file (not part of the main config, kept `0600`):

```bash
sudo cp config-secrets.example.yaml /etc/aquaguard/config-secrets.yaml
sudo nano /etc/aquaguard/config-secrets.yaml   # set mqtt.password
sudo chmod 600 /etc/aquaguard/config-secrets.yaml
```

### 5. Start it

```bash
sudo systemctl start aquaguard
sudo journalctl -u aquaguard -f
```

Look for `Overlaid secrets` in the log — if it's missing, the secrets file
wasn't read and MQTT auth will fail silently.

Dashboard: `http://<pi-ip>:8080`. Home Assistant should pick up the device
automatically via MQTT discovery once its MQTT integration is configured
against the same broker.

## Local development (off-device)

```bash
pip install -e ".[dev]"
pytest
```

Hardware modules (`RPi.GPIO`, `rpi-ws281x`, `spidev`, `gpiod`) only import on
a Pi; tests mock them (see `tests/conftest.py`), so the suite runs anywhere.
Running the app itself off-device needs `config-local.yaml` (gitignored) and
will simulate hardware access rather than fail outright — see log warnings.

## Architecture

See `CLAUDE.md` for the internal architecture (event bus, service layers,
pressure-test/scheduler logic, config loading) — it's written as developer
documentation and is the fastest way to understand how the pieces fit
together.
