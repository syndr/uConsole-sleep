# uConsole-sleep v1.7
### Sleep Service Package for uConsole

This service is built for **Debian** and implemented in **Python**. It is primarily developed against the community "Rex" Debian image for the uConsole, and should also work on other Debian-based distributions (including Ubuntu).

It monitors power key events and controls the screen power (on/off).  
Initially, power key events were detected using a polling loop, but this was later replaced with **epoll** to reduce CPU usage.

When the screen turns off for any reason (e.g., screensaver activation, desktop lock, or system sleep), the service detects the screen-off state and applies several power-saving measures:

- Sets the CPU maximum frequency to the minimum (skipped when on AC power)
- Detaches the built-in keyboard
- Tells the keyboard firmware the host is asleep (see [Keyboard sleep signal](#keyboard-sleep-signal))

While the screen is off, AC plug state is polled every 30 seconds so the freq clamp catches up to plug/unplug without needing to wake the screen.

I also experimented with switching the CPU governor to `powersave`, but observed a slight delay during state transitions, so the screen-driven path adjusts the maximum CPU frequency instead. A separate `sleep-cpu-governor` service handles governor switching on slower battery-state events (where the transition delay is acceptable): it drops to `powersave` below 20% battery and restores `schedutil` at or above 25%, with hysteresis to prevent flapping.

Similarly, screen-off detection was originally implemented using a polling loop, but it was replaced with **inotify** to further reduce CPU load.

## Components

The package ships three system-level services (root, on the system bus) plus one optional user-level service for the idle alerter:

### sleep-remap-powerkey
`/usr/local/bin/sleep_remap_powerkey`

Detects power key events and controls the screen power state.

### sleep-power-control
`/usr/local/bin/sleep_power_control`

Handles power-saving behavior based on the current screen state. Skips the CPU freq clamp when on AC, and polls AC state while the screen is off so plug/unplug during sleep is picked up without waking the screen.

### sleep-cpu-governor
`/usr/local/bin/sleep_cpu_governor`

Switches the cpufreq governor based on battery capacity. Polls every 30 seconds; uses hysteresis (drop at <20%, restore at ≥25%) to avoid flapping. Touches only `scaling_governor`, so it does not conflict with sleep-power-control's `scaling_min_freq` / `scaling_max_freq` writes.

### sleep_display_control
`/usr/local/lib/uconsole-sleep/sleep_display_control.py` (shared library used by sleep-power-control and sleep-remap-powerkey)

Manages DRM/framebuffer display power and backlight control.

### sleep-idle-alert (user service)
`/usr/local/bin/sleep_idle_alert` (installed as `/usr/local/lib/systemd/user/sleep-idle-alert.service`)

DE-agnostic post-idle escalator that fires audio cues + a backlight-flash pattern after the user has been idle on battery long enough. Works on X11 (Openbox, rpd-x, any X session) via `python3-xlib`'s screensaver query, and on Wayland (labwc, rpd-labwc, Hyprland, sway) via a `swayidle` subprocess. Squawks escalate through three tiers (gentle → nag → dumbass), anchored to *accumulated on-battery time* — plugging in or detecting input activity resets the ladder. Uses sounds from `~/Music/notifications/` if present (with optional `warn/`, `gentle/`, `nag/`, `dumbass/` per-tier subdirs) and falls back to `sound-theme-freedesktop` for working defaults out of the box. Optional process-name lock gating (`IDLE_LOCK_PROCESSES`) restricts squawks to when a configured locker is running.

The screen flash writes to `/sys/class/backlight/*/bl_power`. The package ships a udev rule that grants the `video` group write access on that path so the service does not need root.

If your compositor turns the panel off via DPMS before the first squawk fires, the backlight pulses are invisible — bl_power has no effect on a panel that the compositor has parked. Set `IDLE_DPMS_WAKE_CMD` and `IDLE_DPMS_SLEEP_CMD` to your compositor's DPMS commands so each flash wakes the panel, pulses, then restores the DPMS-off state. Examples in `config.default` cover Hyprland (`hyprctl dispatch dpms on/off`), sway (`swaymsg "output * power on/off"`), and X11 (`xset dpms force on/off`). Both default to empty so hosts that don't need this stay on the original behavior.

## Keyboard detection

The power-management services identify the uConsole's built-in keyboard by USB
`VID:PID`, matched against an allowlist in `src/find_internal_kb.py`:

```python
def find_internal_kb(ids=["1eaf:0024", "feed:0000", "1eaf:0003", "1d50:615e"]):
```

The shipped list covers the official keyboard firmware plus the ZMK `bb9900`
(`1d50:615e`). If you run a different third-party keyboard, find its `VID:PID`
with `lsusb` and add it to that list, then rebuild and reinstall (`make
reinstall`). `sleep-power-control` exits at startup if it can't find a matching
keyboard, so an unrecognized keyboard must be added here before the service
will run.

## Keyboard sleep signal

Detaching the keyboard's USB driver stops input, but the keyboard's own
microcontroller keeps running: on the ZMK `bb9900` that meant the trackpad light
stayed lit and a keypress during sleep still woke the key backlight. So on sleep
`sleep-power-control` now:

1. sets one bit in the keyboard's standard HID **LED output report** (the one the
   host uses for Num/Caps/Scroll Lock). The default is the **Kana** indicator,
   which nothing on a Linux host ever drives, so it is free to mean "host asleep";
2. unbinds the USB driver as before;
3. optionally (`KB_SLEEP_USB_SUSPEND=yes`) puts the USB link into selective
   suspend, so the keyboard can park its USB clock. **Off by default:** on the
   uConsole's internal hub (Genesys `05e3:0608` behind `dwc2`) runtime PM of the
   bb9900 ends in `runtime_status=error`, the rebind fails with "can't set
   config #1, error -22", and the keyboard stays dead until a reboot. Do not try
   to recover by re-probing the hub: that wedges both full-speed ports (keyboard
   and fingerprint reader) with "device descriptor read/64, error -71".

On wake it rebinds the driver, waits for the keyboard's `hidraw` node and clears
the bit. The report layout (report ID and bit position) is read
from the keyboard's HID report descriptor, so any keyboard that exposes the LED
works; keyboards without it just log a note once.

The firmware has to act on the bit. The `zmk-uc9900` firmware in
[uConsole-tweaks](https://github.com/syndr/uConsole-tweaks) treats it as "host
asleep": both lights off, no reports, trackpad sensor parked. Stock firmware
ignores it, in which case the signal is harmless.

Keyboard sleep is deferred by `KB_SLEEP_DELAY_SEC` (1 s) after the screen goes
off, and cancelled if the screen comes back first. That keeps the keyboard awake
across `sleep-idle-alert`'s backlight flash (whose pulses toggle `bl_power` with
0.25 s gaps) instead of detaching and re-attaching it on every pulse: the
keyboard wakes once at the start of a flash and goes back to sleep a second
after it ends.

Manual test (root), useful when working on the firmware:

```bash
python3 /usr/local/lib/uconsole-sleep/kb_hid_led.py status kana   # show report id / bit
python3 /usr/local/lib/uconsole-sleep/kb_hid_led.py on kana       # keyboard -> asleep
python3 /usr/local/lib/uconsole-sleep/kb_hid_led.py off kana      # keyboard -> awake
```

Config: `KB_SLEEP_LED`, `KB_SLEEP_LED_WAIT_SEC`, `KB_SLEEP_USB_SUSPEND`,
`KB_SLEEP_DELAY_SEC` (see [Configuration](#configuration)).

## Sleep-time knobs: WiFi power save, Bluetooth, CPU quota

Three optional measures, all off by default, applied in the same deferred step as
the keyboard sleep and reverted on wake. Like the CPU frequency clamp they apply
**only on battery**: plugged in means full performance, and a plug or unplug while
asleep is picked up by the AC poll (`POLL_AC_INTERVAL_SEC`).

| Option | What it does | Measured / expected gain |
|---|---|---|
| `SLEEP_WIFI_POWERSAVE=yes` | `iw dev <wlan> set power_save on` on every wireless interface; previous state restored on wake. The link stays up, so SSH keeps working; first-packet latency rises. | none on the CM5 uConsole: brcmfmac already runs with power save on. Kept for hosts where it is off. |
| `SLEEP_BT_OFF=yes` | rfkill soft-blocks Bluetooth if no device is connected; unblocked at wake only if the service blocked it. | not measurable on the radio rail; idle BT is tiny |
| `SLEEP_CPU_QUOTA=<pct>` | Runtime `CPUQuota=<pct>%` on every `user-<uid>.slice`; cleared on wake. Processes keep running, capped in aggregate. | ~0.2 W measured at 50% on this uConsole: the CPU rail averaged 0.80 A asleep with the cap against 1.10 A without, with the same background apps |

If you run the blueman applet, it answers any Bluetooth state change it did not
make itself by setting rfkill through its mechanism, which asks polkit for
`org.blueman.rfkill.setstate` and pops a password prompt. The package ships
`/etc/polkit-1/rules.d/50-uconsole-sleep-rfkill.rules`, which allows that action
for an active local session, so the prompt does not appear. Harmless without
blueman.

Why these and not more: the BCM2712 kernel has no suspend states at all
(`/sys/power/state` is empty) and no cpuidle driver, so "sleep" is screen-off with
the system running. Measured on a CM5 uConsole on battery: ~4.2 W awake idle,
~3.2 W asleep, of which the CPU is ~0.45 W and the panel plus display pipeline
(already off) were ~1.0 W. The Pi 5 firmware does not allow `arm_freq_min` below
1500 MHz, so the `SAVING_CPU_FREQ` clamp is already at its floor. The fan costs
~0.2 W only while it spins and stops by itself once the CPU cools below its
hysteresis; a sleep-specific fan curve is not worth it.



The repo includes a `Makefile` that wraps the build and install steps:

```
make install   # installs apt deps, builds the .deb, and runs `dpkg -i`
```

Other useful targets:

```
make             # build only (default; alias for `make build`)
make deps        # apt-install runtime dependencies
make uninstall   # remove the installed package
make clean       # remove build artifacts
make status      # systemctl status for the three system services
make logs        # journalctl -f across the three system services
make user-status # systemctl --user status for sleep-idle-alert
make user-logs   # journalctl --user -f for sleep-idle-alert
make help        # list targets
```

The version baked into the package can be overridden:

```
make VERSION=1.7 build
```

The idle alerter is optional at build time. To produce a slimmer package
without the user-level service, the udev rule, and the
`python3-xlib` / `swayidle` / `sound-theme-freedesktop` dependencies:

```
make WITH_IDLE_ALERTER=0 build
```

If you prefer to drive the build script directly:

```
sudo apt install python3-inotify python3-uinput python3-xlib swayidle sound-theme-freedesktop
ENV_VERSION=1.6 ./make_uconsole-sleep_package.sh
sudo dpkg -i uconsole-sleep.deb
```

## Setup (idle alerter)

The three system services start automatically on install. The user-level
`sleep-idle-alert.service` cannot be enabled from a root postinst — enable it
once, per user, after the package is installed:

```
systemctl --user daemon-reload
systemctl --user enable --now sleep-idle-alert.service
```

Audio resolution per squawk (first match wins):

1. `IDLE_SOUNDS_<TIER>` — pipe-separated curated filename list (relative to
   `IDLE_SOUND_DIR`); missing files in the list are silently skipped.
2. `IDLE_SOUND_DIR/<tier>/` subdirectory of `.ogg` / `.oga` / `.wav` files
   (random pick).
3. `IDLE_SOUND_DIR` itself, flat (random pick across whatever is in there).
4. `sound-theme-freedesktop` fallback (`/usr/share/sounds/freedesktop/stereo/`).

The shipped `config.default` sets `IDLE_SOUNDS_<TIER>` to the same per-tier
filename lists this device's pre-v1.6 hypridle wrapper used. To use the
freedesktop fallback for everything instead, blank the four `IDLE_SOUNDS_*`
lines in `/etc/uconsole-sleep/config`. Override `IDLE_SOUND_DIR` to point
elsewhere if you want a system-wide path or per-tier subdirs.

Screen flash: the shipped udev rule (`/lib/udev/rules.d/99-uconsole-sleep-backlight.rules`)
grants the `video` group write access on `/sys/class/backlight/*/bl_power`.
Confirm your user is in `video`:

```
id -nG | grep -qw video || sudo usermod -aG video $USER
```

(Log out and back in for new group membership to take effect.) On a stock
uConsole / Raspberry Pi image the default user is already in `video`.

Required runtime tools: `pipewire` (for `pw-play`), `python3-xlib`, `swayidle`,
`sound-theme-freedesktop`. The latter three are declared Debian dependencies;
`pipewire` is typically already installed on uConsole and Pi images.

## Service management

```
# Enable and start services (done automatically on install)
sudo systemctl enable sleep-remap-powerkey sleep-power-control sleep-cpu-governor
sudo systemctl start  sleep-remap-powerkey sleep-power-control sleep-cpu-governor

# Check status (or: `make status`)
systemctl status sleep-remap-powerkey
systemctl status sleep-power-control
systemctl status sleep-cpu-governor

# View logs (or: `make logs`)
journalctl -u sleep-remap-powerkey -f
journalctl -u sleep-power-control -f
journalctl -u sleep-cpu-governor -f

# Stop and disable
sudo systemctl stop    sleep-remap-powerkey sleep-power-control sleep-cpu-governor
sudo systemctl disable sleep-remap-powerkey sleep-power-control sleep-cpu-governor
```

## Configuration

Configuration file: `/etc/uconsole-sleep/config`

| Option | Default | Description |
|---|---|---|
| `HOLD_TRIGGER_SEC` | `0.7` | Hold duration (seconds) to trigger power interactive menu |
| `SAVING_CPU_FREQ` | `100,100` | CPU frequency (MHz) `<min,max>` applied during power saving |
| `DISABLE_POWER_OFF_DRM` | `no` | Disable turning off DRM on sleep (set `yes` if screen recovery has issues) |
| `DISABLE_POWER_OFF_KB` | `no` | Disable turning off keyboard on sleep (set `yes` to allow keyboard to control its backlight) |
| `KB_SLEEP_LED` | `kana` | HID LED bit set on the keyboard while the host sleeps (`none` disables); see [Keyboard sleep signal](#keyboard-sleep-signal) |
| `KB_SLEEP_LED_WAIT_SEC` | `2` | Wait (seconds) for the keyboard's hidraw node after re-attach before clearing the LED |
| `KB_SLEEP_USB_SUSPEND` | `no` | Experimental: USB-suspend the detached keyboard while the screen is off (breaks on the uConsole hub, needs a reboot) |
| `KB_SLEEP_DELAY_SEC` | `1.0` | Grace period (seconds) before keyboard sleep; cancelled if the screen comes back (keeps the keyboard awake across a backlight flash) |
| `SLEEP_WIFI_POWERSAVE` | `no` | 802.11 power save on all wireless interfaces while asleep; restored on wake |
| `SLEEP_BT_OFF` | `no` | rfkill-block Bluetooth while asleep if nothing is connected; unblocked at wake |
| `SLEEP_CPU_QUOTA` | (empty) | Runtime systemd CPUQuota (percent of one CPU) on user slices while asleep; cleared on wake |
| `DISABLE_CPU_MIN_FREQ` | `no` | Disable setting CPU max frequency to minimum during sleep |
| `POLL_AC_INTERVAL_SEC` | `30` | AC poll interval (seconds) while screen is off — picks up plug/unplug without waking the screen |
| `CPU_GOVERNOR_NORMAL` | `schedutil` | Governor used at or above the exit threshold |
| `CPU_GOVERNOR_LOW_BATTERY` | `powersave` | Governor used below the enter threshold |
| `CPU_LOW_BATTERY_ENTER_PCT` | `20` | Drop to low-battery governor below this % |
| `CPU_LOW_BATTERY_EXIT_PCT` | `25` | Restore normal governor at or above this % (must be ≥ ENTER) |
| `CPU_GOVERNOR_POLL_SEC` | `30` | Battery poll interval (seconds) for the governor service |
| `IDLE_INITIAL_DELAY` | `900` | Seconds of accumulated idle-on-battery before the first (gentle) squawk |
| `IDLE_INTERVAL` | `600` | Seconds between gentle and nag |
| `IDLE_INTERVAL_DUMBASS` | `120` | Seconds between dumbass-tier squawks |
| `IDLE_TICK` | `30` | Poll resolution and idle threshold for the X11 / swayidle backend |
| `IDLE_SOUND_DIR` | (empty) | Sound directory; empty resolves to `$HOME/Music/notifications/` at start |
| `IDLE_SOUNDS_WARN` | curated | Pipe-separated filename list for the pre-lock warn tier (relative to `IDLE_SOUND_DIR`) |
| `IDLE_SOUNDS_GENTLE` | curated | Pipe-separated filename list for the gentle tier |
| `IDLE_SOUNDS_NAG` | curated | Pipe-separated filename list for the nag tier |
| `IDLE_SOUNDS_DUMBASS` | curated | Pipe-separated filename list for the dumbass tier |
| `IDLE_LOCK_PROCESSES` | (empty) | Space-separated process names; non-empty gates squawks on at least one being alive |
| `IDLE_FLASH_ENABLED` | `true` | Pulse the backlight alongside each squawk |
| `IDLE_FLASH_INITIAL_ON_SEC` | `5.0` | Initial long ON-hold before the pulse train (seconds) |
| `IDLE_FLASH_PULSES` | `5` | Number of follow-up pulses after the initial hold |
| `IDLE_FLASH_PULSE_START_SEC` | `2.0` | ON duration of the first pulse (seconds) |
| `IDLE_FLASH_PULSE_DECAY` | `0.25` | Geometric decay applied to each pulse's ON time |
| `IDLE_FLASH_PULSE_OFF_SEC` | `0.25` | Fixed OFF gap between pulses (seconds) |
| `IDLE_FLASH_PULSE_MIN_SEC` | `0.1` | Floor for the decaying ON duration |
| `IDLE_DPMS_WAKE_CMD` | (empty) | Shell command to wake the panel before each flash (e.g. `hyprctl dispatch dpms on`) |
| `IDLE_DPMS_SLEEP_CMD` | (empty) | Shell command to put the panel back to sleep after each flash |
| `IDLE_DPMS_WAKE_SETTLE_SEC` | `0.1` | Delay (seconds) between the wake command and the first bl_power pulse |

## More Information

Discussion and updates are available on the ClockworkPi forum:  
https://forum.clockworkpi.com/t/uconsole-sleep-v1-2/15612?u=paragonnov

---

<a href="https://www.buymeacoffee.com/paragonnov" target="_blank">
<img src="https://cdn.buymeacoffee.com/buttons/v2/default-red.png" alt="Buy Me A Coffee" style="height: 60px !important;width: 217px !important;">
</a>
