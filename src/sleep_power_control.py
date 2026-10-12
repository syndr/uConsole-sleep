import os
import time
import inotify.adapters
import inotify.constants

# from find_drm_panel import find_drm_panel
from find_backlight import find_backlight
from find_internal_kb import find_internal_kb
import kb_hid_led
import sleep_knobs

SAVING_CPU_FREQ = os.environ.get("SAVING_CPU_FREQ")
DISABLE_POWER_OFF_KB = os.environ.get("DISABLE_POWER_OFF_KB") == "yes"
DISABLE_CPU_MIN_FREQ = os.environ.get("DISABLE_CPU_MIN_FREQ") == "yes"
POLL_AC_INTERVAL_SEC = int(os.environ.get("POLL_AC_INTERVAL_SEC") or 30)
KB_SLEEP_LED = os.environ.get("KB_SLEEP_LED", "kana")
KB_SLEEP_LED_WAIT_SEC = float(os.environ.get("KB_SLEEP_LED_WAIT_SEC") or 2)
KB_SLEEP_USB_SUSPEND = (os.environ.get("KB_SLEEP_USB_SUSPEND") or "no") == "yes"
KB_SLEEP_DELAY_SEC = float(os.environ.get("KB_SLEEP_DELAY_SEC") or 1.0)
SLEEP_WIFI_POWERSAVE = os.environ.get("SLEEP_WIFI_POWERSAVE") == "yes"
SLEEP_BT_OFF = os.environ.get("SLEEP_BT_OFF") == "yes"
SLEEP_CPU_QUOTA = (os.environ.get("SLEEP_CPU_QUOTA") or "").strip().rstrip("%")

AC_ONLINE_PATH = "/sys/class/power_supply/axp22x-ac/online"


def is_ac_plugged():
    try:
        with open(AC_ONLINE_PATH, "r") as f:
            return f.read().strip() == "1"
    except Exception:
        return False


def apply_offscreen_cpu_freq():
    if DISABLE_CPU_MIN_FREQ:
        return
    if is_ac_plugged():
        with open(os.path.join(cpu_policy_path, "scaling_max_freq"), "w") as f:
            f.write(default_cpu_freq_max)
        with open(os.path.join(cpu_policy_path, "scaling_min_freq"), "w") as f:
            f.write(default_cpu_freq_min)
        print(f"cpu freq: unclamped (ac plugged, max={default_cpu_freq_max})")
    else:
        with open(os.path.join(cpu_policy_path, "scaling_min_freq"), "w") as f:
            f.write(saving_cpu_freq_min)
        with open(os.path.join(cpu_policy_path, "scaling_max_freq"), "w") as f:
            f.write(saving_cpu_freq_max)
        print(f"cpu freq: clamped (max={saving_cpu_freq_max})")


def apply_onscreen_cpu_freq():
    if DISABLE_CPU_MIN_FREQ:
        return
    with open(os.path.join(cpu_policy_path, "scaling_max_freq"), "w") as f:
        f.write(default_cpu_freq_max)
    print(f"cpu freq max: {default_cpu_freq_max}")
    with open(os.path.join(cpu_policy_path, "scaling_min_freq"), "w") as f:
        f.write(default_cpu_freq_min)
    print(f"cpu freq min: {default_cpu_freq_min}")


# --- keyboard -----------------------------------------------------------------

kb_led_usage = kb_hid_led.led_usage(KB_SLEEP_LED)
kb_led_unsupported_logged = False


def kb_is_bound():
    return os.path.islink(os.path.join(kb_device_path, "driver"))


def kb_set_led(on):
    """Set/clear the sleep LED bit. Never raises; logs the outcome."""
    global kb_led_unsupported_logged
    if kb_led_usage is None:
        return
    try:
        if on:
            dev = kb_hid_led.set_led(kb_device_path, kb_led_usage, True)
        else:
            found = kb_hid_led.wait_for_hid(kb_device_path, KB_SLEEP_LED_WAIT_SEC)
            dev = kb_hid_led.set_led(kb_device_path, kb_led_usage, False) if found else None
        if dev is None:
            if not kb_led_unsupported_logged:
                print(f"kb sleep led: keyboard has no {KB_SLEEP_LED} LED or no hidraw node, signal skipped")
                kb_led_unsupported_logged = True
            return
        kb_led_unsupported_logged = False
        print(f"kb sleep led: {KB_SLEEP_LED} {'set' if on else 'cleared'} ({os.path.basename(dev)})")
    except Exception as e:
        print(f"kb sleep led: failed to {'set' if on else 'clear'} {KB_SLEEP_LED}: {e}")


def kb_write_power_control(value):
    with open(os.path.join(kb_device_path, "power/control"), "w") as f:
        f.write(value)


def kb_sleep():
    """Signal the keyboard, detach it, and suspend the USB link."""
    kb_set_led(True)
    if DISABLE_POWER_OFF_KB:
        # Only set autosuspend when not unbinding — unbinding handles power-off
        # and writing "auto" with autosuspend_delay_ms=0 before unbind triggers
        # a failed USB suspend (EPROTO, error -71).
        kb_write_power_control("auto")
        return
    if kb_is_bound():
        with open(os.path.join(usb_driver_path, "unbind"), "w") as f:
            f.write(kb_device_id)
        print("kb power state: unbind")
    else:
        print("kb power state: already unbound")
    if KB_SLEEP_USB_SUSPEND:
        # With no driver and no interfaces the core does a generic suspend
        # (no remote-wakeup requirement), so the keyboard can park its USB link.
        try:
            kb_write_power_control("auto")
            time.sleep(0.2)
            with open(os.path.join(kb_device_path, "power/runtime_status"), "r") as f:
                status = f.read().strip()
            if status == "suspended":
                print("kb usb: suspended")
            else:
                print(f"kb usb: suspend not reached ({status})")
        except Exception as e:
            print(f"kb usb: suspend failed: {e}")


def kb_wake():
    """Resume the USB link, re-attach the keyboard, and clear the signal."""
    if DISABLE_POWER_OFF_KB:
        kb_write_power_control("on")
    else:
        if KB_SLEEP_USB_SUSPEND:
            try:
                kb_write_power_control("on")
                with open(os.path.join(kb_device_path, "power/runtime_status"), "r") as f:
                    status = f.read().strip()
                if status != "active":
                    print(f"kb usb: resume failed (runtime_status={status}); the keyboard will not "
                          "come back without a reboot (re-probing the hub wedges the full-speed "
                          "ports with error -71). Set KB_SLEEP_USB_SUSPEND=no.")
            except Exception as e:
                print(f"kb usb: resume failed: {e}")
        if kb_is_bound():
            print("kb power state: already bound")
        else:
            with open(os.path.join(usb_driver_path, "bind"), "w") as f:
                f.write(kb_device_id)
            print("kb power state: bind")
        kb_write_power_control("on")
    kb_set_led(False)


# --- sleep knobs (applied with the deferred keyboard sleep, reverted on wake) --

knobs = []
if SLEEP_WIFI_POWERSAVE:
    knobs.append(sleep_knobs.WifiPowerSave())
if SLEEP_BT_OFF:
    knobs.append(sleep_knobs.BluetoothOff())
if SLEEP_CPU_QUOTA:
    try:
        quota = int(SLEEP_CPU_QUOTA)
        if quota <= 0:
            raise ValueError
        knobs.append(sleep_knobs.CpuQuota(quota))
    except ValueError:
        print(f"sleep knobs: ignoring SLEEP_CPU_QUOTA={SLEEP_CPU_QUOTA!r} (want a positive percent)")
knobs_applied = False


def knobs_sleep():
    global knobs_applied
    for k in knobs:
        try:
            k.sleep()
        except Exception as e:
            print(f"sleep knobs: {type(k).__name__} sleep failed: {e}")
    knobs_applied = True


def knobs_wake():
    global knobs_applied
    if not knobs_applied:
        return
    for k in reversed(knobs):
        try:
            k.wake()
        except Exception as e:
            print(f"sleep knobs: {type(k).__name__} wake failed: {e}")
    knobs_applied = False


# --- state --------------------------------------------------------------------

kb_asleep = False
kb_sleep_due = None  # monotonic time at which a pending keyboard sleep fires


def screen_on():
    global kb_asleep, kb_sleep_due
    apply_onscreen_cpu_freq()
    kb_sleep_due = None
    knobs_wake()
    if kb_asleep:
        kb_wake()
        kb_asleep = False


def screen_off():
    global kb_sleep_due
    if not kb_asleep and kb_sleep_due is None:
        if KB_SLEEP_DELAY_SEC > 0:
            kb_sleep_due = time.monotonic() + KB_SLEEP_DELAY_SEC
        else:
            run_pending_kb_sleep(force=True)
    apply_offscreen_cpu_freq()


def run_pending_kb_sleep(force=False):
    global kb_asleep, kb_sleep_due
    if not force and (kb_sleep_due is None or time.monotonic() < kb_sleep_due):
        return
    kb_sleep_due = None
    if not kb_asleep:
        kb_sleep()
        kb_asleep = True
        knobs_sleep()


def control_by_state(state):
    if state:
        screen_on()
    else:
        screen_off()


backlight_path = find_backlight()
# drm_panel_path = find_drm_panel()
kb_device_path = find_internal_kb()
kb_device_id = os.path.basename(kb_device_path)
usb_driver_path = "/sys/bus/usb/drivers/usb"
cpu_policy_path = "/sys/devices/system/cpu/cpufreq/policy0"

if not backlight_path:
    raise Exception("there's no matched backlight")

# if not drm_panel_path:
#    raise Exception("there's no matched drm panel")

if not kb_device_path:
    raise Exception("there's no matched kb")

with open(os.path.join(kb_device_path, "power/autosuspend_delay_ms"), "w") as f:
    f.write("0")
    print(f"{kb_device_path}/power/autosuspend_delay_ms = 0")

if kb_led_usage is None:
    print(f"kb sleep led: disabled (KB_SLEEP_LED={KB_SLEEP_LED!r})")
else:
    print(f"kb sleep led: {KB_SLEEP_LED}, usb suspend: {'yes' if KB_SLEEP_USB_SUSPEND else 'no'}, sleep delay: {KB_SLEEP_DELAY_SEC}s")
print("sleep knobs: " + (", ".join(type(k).__name__ for k in knobs) or "none"))

if not SAVING_CPU_FREQ:
    with open(os.path.join(cpu_policy_path, "cpuinfo_min_freq"), "r") as f:
        saving_cpu_freq_min = f.read().strip()
        saving_cpu_freq_max = saving_cpu_freq_min
else:
    saving_cpu_freq_min, saving_cpu_freq_max = SAVING_CPU_FREQ.split(",")
    saving_cpu_freq_min = f"{saving_cpu_freq_min}000"
    saving_cpu_freq_max = f"{saving_cpu_freq_max}000"
print(f"saving_cpu_freq_min: {saving_cpu_freq_min}")
print(f"saving_cpu_freq_max: {saving_cpu_freq_max}")

with open(os.path.join(cpu_policy_path, "scaling_min_freq"), "r") as f:
    default_cpu_freq_min = f.read().strip()
    print(f"default_cpu_freq_min: {default_cpu_freq_min}")

with open(os.path.join(cpu_policy_path, "scaling_max_freq"), "r") as f:
    default_cpu_freq_max = f.read().strip()
    print(f"default_cpu_freq_max: {default_cpu_freq_max}")

backlight_bl_path = os.path.join(backlight_path, "bl_power")
with open(backlight_bl_path, "r") as f:
    screen_state = f.read().strip()

# drm_enabled_path = os.path.join(drm_panel_path, "enabled")
# with open(drm_enabled_path, "r") as f:
#    screen_state = f.read().strip()

# A service restart may find the keyboard already detached from an earlier
# sleep; treat that as asleep so the wake path re-attaches it.
kb_asleep = not DISABLE_POWER_OFF_KB and not kb_is_bound()

try:
    if screen_state == "4":
        # Already dark at startup: no flash is in progress, sleep right away.
        apply_offscreen_cpu_freq()
        run_pending_kb_sleep(force=True)
    else:
        # Screen on: make sure the keyboard is attached and its sleep signal
        # is clear, whatever state a previous run left it in.
        apply_onscreen_cpu_freq()
        kb_wake()
        kb_asleep = False
        # A crashed earlier run may have left a runtime CPU quota behind.
        sleep_knobs.CpuQuota.clear_all()
#    control_by_state(screen_state != "disabled")
except Exception as e:
    print(f"Error occurred: {e}, on init. ignored")

i = inotify.adapters.Inotify()
i.add_watch(backlight_bl_path, mask=inotify.constants.IN_MODIFY)

print(f"Monitoring {backlight_bl_path} for changes...")

last_screen_state = screen_state
last_ac_plugged = is_ac_plugged() if screen_state == "4" else None
last_ac_check = time.time()
while True:
    try:
        timeout = 1.0
        if kb_sleep_due is not None:
            timeout = max(0.05, min(timeout, kb_sleep_due - time.monotonic()))
        list(i.event_gen(yield_nones=False, timeout_s=timeout))

        with open(backlight_bl_path, "r") as f:
            screen_state = f.read().strip()
        screen_off_now = screen_state == "4"

        if screen_state != last_screen_state:
            last_screen_state = screen_state
            control_by_state(not screen_off_now)
            last_ac_plugged = is_ac_plugged() if screen_off_now else None
            last_ac_check = time.time()
            continue

        if screen_off_now:
            run_pending_kb_sleep()
            now = time.time()
            if now - last_ac_check >= POLL_AC_INTERVAL_SEC:
                last_ac_check = now
                ac = is_ac_plugged()
                if ac != last_ac_plugged:
                    print(f"AC state changed while screen off: {last_ac_plugged} -> {ac}")
                    last_ac_plugged = ac
                    apply_offscreen_cpu_freq()

    except Exception as e:
        print(f"Error occurred: {e}")
