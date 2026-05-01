# uConsole-sleep v1.5
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

The service consists of three background processes:

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

## How to package and install

The repo includes a `Makefile` that wraps the build and install steps:

```
make install   # installs apt deps, builds the .deb, and runs `dpkg -i`
```

Other useful targets:

```
make           # build only (default; alias for `make build`)
make deps      # apt-install runtime dependencies
make uninstall # remove the installed package
make clean     # remove build artifacts
make status    # systemctl status for all three services
make logs      # journalctl -f across all three services
make help      # list targets
```

The version baked into the package can be overridden:

```
make VERSION=1.6 build
```

If you prefer to drive the build script directly:

```
sudo apt install python3-inotify python3-uinput
ENV_VERSION=1.5 ./make_uconsole-sleep_package.sh
sudo dpkg -i uconsole-sleep.deb
```

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

## More Information

Discussion and updates are available on the ClockworkPi forum:  
https://forum.clockworkpi.com/t/uconsole-sleep-v1-2/15612?u=paragonnov

---

<a href="https://www.buymeacoffee.com/paragonnov" target="_blank">
<img src="https://cdn.buymeacoffee.com/buttons/v2/default-red.png" alt="Buy Me A Coffee" style="height: 60px !important;width: 217px !important;">
</a>
