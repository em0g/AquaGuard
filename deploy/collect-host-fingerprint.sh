#!/bin/bash
# Collects the host-side state that could plausibly differ between a Pi whose
# WS281x ring is clean and one that glitches at the data-in seam.
#
# Run on both devices and diff the output:
#   sudo ./deploy/collect-host-fingerprint.sh > my-fingerprint.txt
#   diff deploy/led-ring-host-fingerprint.txt my-fingerprint.txt
#
# Prints no secrets. Hostname, board serial and root PARTUUID are redacted so
# the output is safe to send to someone else.
set -u

sec() { printf '\n===== %s =====\n' "$1"; }
scrub() { sed -E "s/$(hostname)/<host>/g; s/PARTUUID=[0-9a-fA-F-]+/PARTUUID=<redacted>/g"; }

{
sec "boot config.txt (comments stripped)"
grep -vE '^\s*(#|$)' /boot/firmware/config.txt 2>/dev/null || grep -vE '^\s*(#|$)' /boot/config.txt 2>/dev/null

sec "boot cmdline.txt"
cat /boot/firmware/cmdline.txt 2>/dev/null || cat /boot/cmdline.txt 2>/dev/null

sec "board"
grep -E '^(Model|Revision)' /proc/cpuinfo

sec "kernel / os / firmware"
uname -srvm
grep -E '^PRETTY_NAME=' /etc/os-release
vcgencmd version 2>/dev/null | tail -2

sec "GPIO 18 (LED data pin) — want: a5 = PWM0"
if command -v pinctrl >/dev/null; then pinctrl get 18
elif command -v raspi-gpio >/dev/null; then raspi-gpio get 18
else echo "neither pinctrl nor raspi-gpio installed"; fi

sec "firmware config ints (clocks / audio pwm)"
vcgencmd get_config int 2>/dev/null |
  grep -E '^(arm_freq|core_freq|gpu_freq|force_turbo|over_voltage|audio_pwm_mode|arm_boost)'

sec "sound / pwm modules loaded"
lsmod | awk '{print $1}' | grep -E '^(snd_bcm2835|snd_pcm|snd_soc_core|pwm_)' | sort

sec "audio actually in use (must be empty — PWM0 is shared with the ring)"
fuser -v /dev/snd/* 2>&1 | grep -v '^$' || echo "(nothing holding /dev/snd)"

sec "cpu governor"
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null

sec "rpi-ws281x / python"
/opt/aquaguard/venv/bin/pip show rpi-ws281x 2>/dev/null | grep -E '^(Name|Version)'
/opt/aquaguard/venv/bin/python --version 2>/dev/null

sec "leds.py checksum (must match between devices)"
sha256sum /opt/aquaguard/aquaguard/hardware/leds.py 2>/dev/null

sec "led config in effect"
grep -A8 -E '^\s*leds:' /etc/aquaguard/config.yaml 2>/dev/null || echo "(no leds block — defaults apply)"

sec "service user (rpi-ws281x needs root for DMA/PWM)"
systemctl show aquaguard -p User -p Group 2>/dev/null
} | scrub
