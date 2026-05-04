# uConsole-sleep v1.6
### Sleep Service Package for uConsole

This service is built for **Ubuntu 22.04** and implemented in **Python**.

It monitors power key events and controls the screen power (on/off).  
Initially, power key events were detected using a polling loop, but this was later replaced with **epoll** to reduce CPU usage.

When the screen turns off for any reason (e.g., screensaver activation, desktop lock, or system sleep), the service detects the screen-off state and applies several power-saving measures:

- Sets the CPU maximum frequency to the minimum (skipped when on AC power)
- Disables power to the built-in keyboard
- Disables the keyboard wakeup trigger

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

## How to package and install

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

Audio: out of the box the service uses `sound-theme-freedesktop` (declared as
a Debian dependency) — no setup needed for working sounds. To override with
custom audio, drop `.ogg` / `.oga` / `.wav` files into
`~/Music/notifications/`. Per-tier subdirs (`warn/`, `gentle/`, `nag/`,
`dumbass/`) win over flat files in the directory; flat files win over the
freedesktop fallback. Override the directory location with `IDLE_SOUND_DIR`.

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
| `IDLE_LOCK_PROCESSES` | (empty) | Space-separated process names; non-empty gates squawks on at least one being alive |
| `IDLE_FLASH_ENABLED` | `true` | Pulse the backlight alongside each squawk |
| `IDLE_FLASH_INITIAL_ON_SEC` | `5.0` | Initial long ON-hold before the pulse train (seconds) |
| `IDLE_FLASH_PULSES` | `5` | Number of follow-up pulses after the initial hold |
| `IDLE_FLASH_PULSE_START_SEC` | `2.0` | ON duration of the first pulse (seconds) |
| `IDLE_FLASH_PULSE_DECAY` | `0.25` | Geometric decay applied to each pulse's ON time |
| `IDLE_FLASH_PULSE_OFF_SEC` | `0.25` | Fixed OFF gap between pulses (seconds) |
| `IDLE_FLASH_PULSE_MIN_SEC` | `0.1` | Floor for the decaying ON duration |

## More Information

Discussion and updates are available on the ClockworkPi forum:  
https://forum.clockworkpi.com/t/uconsole-sleep-v1-2/15612?u=paragonnov

---

<a href="https://www.buymeacoffee.com/paragonnov" target="_blank">
<img src="https://cdn.buymeacoffee.com/buttons/v2/default-red.png" alt="Buy Me A Coffee" style="height: 60px !important;width: 217px !important;">
</a>
