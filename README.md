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
`dtparam=spi=on`, `dtoverlay=w1-gpio`. If you go the manual route, also add
`i2c-dev` to `/etc/modules` — `raspi-config` does that for you, and without the
module `/dev/i2c-1` never appears even though the `dtparam` is set.)

On a fresh install, also set the WiFi country, or the radio stays rfkill-blocked
and `wlan0` never associates:

```bash
sudo raspi-config nonint do_wifi_country SE      # your ISO country code
```

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

## Host system settings (outside the app)

Two Raspberry Pi OS defaults caused production failures on the reference
device. They are host-level settings, so neither `install.sh` nor `config.yaml`
covers them — apply them by hand on every new build. The drop-ins live in
`deploy/system/`.

### WiFi powersave off — required for unattended operation

With the default powersave on, the device drops off the LAN after roughly 7–14
days of uptime and does not come back without a power cycle: `brcmfmac` loses
the association and fails to re-associate. Since this box is what shuts the
water off, a silently unreachable Pi is the worst failure mode there is.

```bash
sudo cp deploy/system/aquaguard-wifi-powersave-off.conf \
        /etc/NetworkManager/conf.d/
sudo systemctl restart NetworkManager

# takes effect immediately without a restart, if you prefer:
sudo /sbin/iw dev wlan0 set power_save off
```

Verify (`iw` is in `/sbin`, which is not on the `pi` user's `PATH`):

```bash
sudo /sbin/iw dev wlan0 get power_save     # must print: Power save: off
```

### Persistent journal — required to diagnose anything

Raspberry Pi OS ships `Storage=volatile`, so all logs vanish on reboot. That
makes post-mortems of exactly the intermittent faults you care about (network
drops, hardware glitches, failed self-tests) impossible.

```bash
sudo cp deploy/system/aquaguard-persistent-journal.conf \
        /etc/systemd/journald.conf.d/aquaguard-persistent.conf
sudo systemctl restart systemd-journald
sudo journalctl --flush
```

The `--flush` matters: without it the existing logs are not migrated and the
journal appears to still be volatile until the next boot.

Verify:

```bash
sudo journalctl --header | grep -m1 "File path"
# must point at /var/log/journal/... — /run/log/journal/... means still volatile
```

### Deliberately stock (do not "fix" these)

Worth recording, because the usual internet advice says otherwise and the
reference device contradicts it. On the working unit:

- **Onboard audio is enabled** (`dtparam=audio=on`, `snd_bcm2835` loaded) and the
  WS281x ring on GPIO 18 is stable anyway. The common "you must blacklist
  `snd_bcm2835` for rpi-ws281x" advice is *not* what makes this build work. Do
  not go chasing it as an LED fix — but do avoid actually playing audio, since
  the headphone output and the LED PWM channel are the same peripheral.
- **No clock pinning** — no `core_freq`, no `force_turbo`, no `over_voltage`, and
  the CPU governor is the default `ondemand`.
- **No watchdog, no cron jobs, no rc.local**, and no udev rules of our own.

In other words **nothing exotic was added to this host** to make the LED ring
work. That is a description of the working device, not proof that host config is
irrelevant — see below.

### The ring glitch: what has been ruled out

Symptom: wrong colour on the pixels around the data-in seam, flicker, or the
ring freezing in the wrong colour.

Ruled out so far:

- **The power supply.** Tested 2026-07-26 by running the unit on a different AC
  adapter: behaviour was identical to the original adapter. The glitch is not
  caused by the supply, so do not go looking for a "wrong PSU" or a 12 V vs 5 V
  feed explanation.
- **The LED code.** `aquaguard/hardware/leds.py` was verified against the
  original firmware: same ring indices (`0–29`), same status LEDs (30–33), same
  colours, same 800 kHz / DMA 10 / `WS2811_STRIP_RGB` setup. A second device
  running a byte-identical clone of this tree shows the glitch while this one
  does not — identical code cannot be the difference.

Not ruled out — and the only lead left:

- **Host/OS configuration on the Pi itself.** The section above records what the
  *working* device looks like, which is not the same as saying host config is
  irrelevant: it only says nothing exotic was added here. The reference device
  showed this exact glitch early in the project and stopped showing it after
  changes made **on the device**, not in the code. Nobody has yet compared the
  two Pis side by side, so that comparison is the next step, not another theory.

`deploy/collect-host-fingerprint.sh` collects that comparison:

```bash
sudo ./deploy/collect-host-fingerprint.sh > my-fingerprint.txt
```

It prints no secrets — hostname and root PARTUUID are redacted — so the output
is safe to hand to someone else. Then diff it against a capture from a device
whose ring is known good. Those captures are **not checked in**: they describe
one specific machine down to its board revision and firmware build, which does
not belong in a public repo (`deploy/*fingerprint*.txt` is gitignored). Ask
whoever runs a working unit for theirs.

Things in there that are worth looking at first if the diff is not empty:
the board revision, `GPIO 18` reporting `a5` (PWM0) rather than an input,
the `rpi_ws281x` version, and whether anything is actually *playing* audio —
PWM0 is shared between the ring and the headphone output, and a running
PipeWire graph holding `/dev/snd` is not the same thing as audio being played.

Until that diff exists the cause is **unidentified**. Do not write a new theory
into this file until it has been tested on hardware.

Unrelated but worth knowing when someone reports a dead ring rather than a
glitching one: `rpi_ws281x` drives PWM/DMA directly, which behaves differently
on a Pi 4 and does not work at all on a Pi 5.

The app does carry one mitigation for the *symptom*: the static frame is
re-sent roughly once a second (`run_animation_loop`), so a single corrupt
transmission self-heals instead of sticking until the next valve change. That is
a workaround, not a fix.

## Updating to a newer version

If you installed with `git clone` + `deploy/install.sh`, updating is just
pulling the new code and re-running the installer from your clone directory on
the Pi:

```bash
cd aquaguard-src                 # wherever you cloned it
git pull
sudo ./deploy/install.sh         # overwrites the app, keeps config + data
sudo systemctl restart aquaguard
```

`install.sh` is idempotent: it overwrites `/opt/aquaguard` but **will not touch**
an existing `/etc/aquaguard/config.yaml` (or your `config-secrets.yaml`) or the
state/database in `/var/lib/aquaguard`. It also refreshes the venv, so a pull
that changed dependencies is handled too.

Verify the restart picked up the new code:

```bash
journalctl -u aquaguard -f       # look for "LED ring initialised", "Overlaid secrets", no tracebacks
```

To update just one file without re-running the installer (e.g. a single hotfix),
copy it into place and restart:

```bash
cd aquaguard-src && git pull
sudo cp aquaguard/hardware/leds.py /opt/aquaguard/aquaguard/hardware/leds.py
sudo systemctl restart aquaguard
```

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
