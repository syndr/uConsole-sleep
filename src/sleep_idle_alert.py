import grp
import os
import random
import subprocess
import sys
import threading
import time

from find_backlight import find_backlight


INITIAL_DELAY = int(os.environ.get("IDLE_INITIAL_DELAY") or 900)
INTERVAL = int(os.environ.get("IDLE_INTERVAL") or 600)
INTERVAL_DUMBASS = int(os.environ.get("IDLE_INTERVAL_DUMBASS") or 120)
TICK = int(os.environ.get("IDLE_TICK") or 30)

SOUND_DIR = os.environ.get("IDLE_SOUND_DIR") or os.path.join(
    os.path.expanduser("~"), "Music", "notifications"
)

LOCK_PROCESSES = (os.environ.get("IDLE_LOCK_PROCESSES") or "").split()

FLASH_ENABLED = (os.environ.get("IDLE_FLASH_ENABLED") or "true").lower() == "true"
FLASH_INITIAL_ON_SEC = float(os.environ.get("IDLE_FLASH_INITIAL_ON_SEC") or 5.0)
FLASH_PULSES = int(os.environ.get("IDLE_FLASH_PULSES") or 5)
FLASH_PULSE_START_SEC = float(os.environ.get("IDLE_FLASH_PULSE_START_SEC") or 2.0)
FLASH_PULSE_DECAY = float(os.environ.get("IDLE_FLASH_PULSE_DECAY") or 0.25)
FLASH_PULSE_OFF_SEC = float(os.environ.get("IDLE_FLASH_PULSE_OFF_SEC") or 0.25)
FLASH_PULSE_MIN_SEC = float(os.environ.get("IDLE_FLASH_PULSE_MIN_SEC") or 0.1)

# Optional DPMS hooks. When the compositor has already DPMS-off'd the panel
# by the time a squawk fires, bl_power pulses are invisible — the panel
# signal is cut regardless of backlight state. These shell commands let the
# alerter wake the display for the flash and put it back to sleep afterward.
# Both empty by default to preserve prior behavior on hosts that don't need
# them. Per-DE values live in config.default.
DPMS_WAKE_CMD = os.environ.get("IDLE_DPMS_WAKE_CMD") or ""
DPMS_SLEEP_CMD = os.environ.get("IDLE_DPMS_SLEEP_CMD") or ""
DPMS_WAKE_SETTLE_SEC = float(os.environ.get("IDLE_DPMS_WAKE_SETTLE_SEC") or 0.1)

AUDIO_EXTS = (".ogg", ".oga", ".wav")

# Per-tier curated filename lists (pipe-separated env vars; entries are
# relative to IDLE_SOUND_DIR). When set, this layer wins over the tier
# subdir / flat directory / freedesktop fallback layers.
def _parse_curated(env_name):
    raw = os.environ.get(env_name) or ""
    return [p.strip() for p in raw.split("|") if p.strip()]


CURATED_SOUNDS = {
    "warn":    _parse_curated("IDLE_SOUNDS_WARN"),
    "gentle":  _parse_curated("IDLE_SOUNDS_GENTLE"),
    "nag":     _parse_curated("IDLE_SOUNDS_NAG"),
    "dumbass": _parse_curated("IDLE_SOUNDS_DUMBASS"),
}

FALLBACK_DIR = "/usr/share/sounds/freedesktop/stereo"
FALLBACK_SOUNDS = {
    "warn":    ["bell.oga", "dialog-information.oga", "message.oga"],
    "gentle":  ["bell.oga", "message.oga", "complete.oga"],
    "nag":     ["dialog-warning.oga", "phone-outgoing-busy.oga"],
    "dumbass": ["alarm-clock-elapsed.oga", "phone-incoming-call.oga", "suspend-error.oga"],
}

BL_POWER = os.path.join(find_backlight(), "bl_power")


def log(msg):
    print(msg, flush=True)


def tier_for(n):
    if n == 1:
        return "gentle"
    if n == 2:
        return "nag"
    return "dumbass"


def target_for(n):
    if n == 1:
        return INITIAL_DELAY
    if n == 2:
        return INTERVAL
    return INTERVAL_DUMBASS


def is_on_ac():
    # exit equivalent of OnBattery.sh: True == on AC, False == on battery (or unknown)
    psy_root = "/sys/class/power_supply"
    try:
        entries = os.listdir(psy_root)
    except OSError:
        return False
    ac_found = False
    for name in entries:
        type_path = os.path.join(psy_root, name, "type")
        try:
            with open(type_path) as f:
                psy_type = f.read().strip()
        except OSError:
            continue
        if psy_type != "Mains":
            continue
        ac_found = True
        online_path = os.path.join(psy_root, name, "online")
        try:
            with open(online_path) as f:
                if f.read().strip() == "1":
                    return True
        except OSError:
            continue
    if ac_found:
        return False
    # No Mains found — fall back to battery status heuristics.
    for name in entries:
        type_path = os.path.join(psy_root, name, "type")
        status_path = os.path.join(psy_root, name, "status")
        try:
            with open(type_path) as f:
                if f.read().strip() != "Battery":
                    continue
            with open(status_path) as f:
                status = f.read().strip()
        except OSError:
            continue
        if status in ("Charging", "Full", "Not charging"):
            return True
    return False


def lock_gate_open():
    # When LOCK_PROCESSES is empty the gate is always open. Otherwise require
    # at least one matching process to be alive.
    if not LOCK_PROCESSES:
        return True
    targets = set(LOCK_PROCESSES)
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return False
    for pid in pids:
        try:
            with open(f"/proc/{pid}/comm") as f:
                if f.read().strip() in targets:
                    return True
        except OSError:
            continue
    return False


def pick_sound(tier):
    # 1. curated filename list for this tier (IDLE_SOUNDS_<TIER>)
    curated = CURATED_SOUNDS.get(tier, [])
    if curated:
        existing = [n for n in curated if os.path.isfile(os.path.join(SOUND_DIR, n))]
        if existing:
            return os.path.join(SOUND_DIR, random.choice(existing))
    # 2. tier-specific subdir of SOUND_DIR
    tier_dir = os.path.join(SOUND_DIR, tier)
    if os.path.isdir(tier_dir):
        files = [f for f in os.listdir(tier_dir) if f.lower().endswith(AUDIO_EXTS)]
        if files:
            return os.path.join(tier_dir, random.choice(files))
    # 3. flat SOUND_DIR
    if os.path.isdir(SOUND_DIR):
        files = [f for f in os.listdir(SOUND_DIR) if f.lower().endswith(AUDIO_EXTS)]
        if files:
            return os.path.join(SOUND_DIR, random.choice(files))
    # 4. freedesktop fallback
    for name in FALLBACK_SOUNDS.get(tier, []):
        path = os.path.join(FALLBACK_DIR, name)
        if os.path.isfile(path):
            return path
    return None


def play_sound(path):
    try:
        subprocess.Popen(
            ["pw-play", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as e:
        log(f"audio: failed to spawn pw-play: {e}")


def user_in_video_group():
    try:
        video_gid = grp.getgrnam("video").gr_gid
    except KeyError:
        return None  # group doesn't exist on this system
    return video_gid in (set(os.getgroups()) | {os.getgid()})


def check_flash_runtime():
    # Returns True iff flashing is configured AND we have write access to
    # bl_power. Logs a clear diagnostic the first time we discover we
    # don't, so the per-flash write-error spam stays out of the journal.
    if not FLASH_ENABLED:
        return False
    if not os.path.exists(BL_POWER):
        log(f"flash: disabled — {BL_POWER} does not exist")
        return False
    if os.access(BL_POWER, os.W_OK):
        return True
    in_video = user_in_video_group()
    if in_video is False:
        log(
            f"flash: disabled — {BL_POWER} is not writable and the user is not in "
            "the 'video' group. The shipped udev rule "
            "(99-uconsole-sleep-backlight.rules) grants write access to that group; "
            "fix with `sudo usermod -aG video $USER` then log out + back in."
        )
    elif in_video is None:
        log(
            f"flash: disabled — {BL_POWER} is not writable and the 'video' group "
            "does not exist on this system; the shipped udev rule cannot grant "
            "access without it."
        )
    else:
        log(
            f"flash: disabled — {BL_POWER} is not writable even though the user is in "
            "the 'video' group. The udev rule may not have applied; try "
            "`sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=backlight`."
        )
    return False


FLASH_RUNTIME_OK = check_flash_runtime()


def write_bl_power(value):
    try:
        with open(BL_POWER, "w") as f:
            f.write(str(value))
        return True
    except OSError as e:
        log(f"flash: cannot write {BL_POWER}: {e}")
        return False


def _run_dpms_cmd(cmd, label):
    if not cmd:
        return
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=5,
        )
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode(errors="replace").strip()
            log(f"dpms {label}: '{cmd}' exited {proc.returncode}{': ' + err if err else ''}")
    except subprocess.TimeoutExpired:
        log(f"dpms {label}: '{cmd}' timed out after 5s")
    except OSError as e:
        log(f"dpms {label}: failed to spawn '{cmd}': {e}")


def flash_screen():
    if not FLASH_RUNTIME_OK:
        return
    # Capture the screen's pre-flash state so we can leave it the way we
    # found it. Without this, the pulse-train ends with bl_power=4 and
    # blanks a session that was visibly on.
    try:
        with open(BL_POWER) as f:
            initial = f.read().strip()
    except OSError as e:
        log(f"flash: cannot read {BL_POWER}: {e}")
        return
    try:
        # Wake the panel first so a DPMS-off compositor can't swallow the
        # flash. Small settle delay lets the panel come up before pulsing.
        if DPMS_WAKE_CMD:
            _run_dpms_cmd(DPMS_WAKE_CMD, "wake")
            if DPMS_WAKE_SETTLE_SEC > 0:
                time.sleep(DPMS_WAKE_SETTLE_SEC)
        # Initial long hold: screen on for FLASH_INITIAL_ON_SEC.
        if not write_bl_power(0):
            return
        time.sleep(FLASH_INITIAL_ON_SEC)
        if not write_bl_power(4):
            return
        # Follow-up pulses: on-time decays geometrically, off-time fixed.
        dur = FLASH_PULSE_START_SEC
        for i in range(FLASH_PULSES):
            if not write_bl_power(0):
                return
            time.sleep(dur)
            if not write_bl_power(4):
                return
            if i < FLASH_PULSES - 1:
                time.sleep(FLASH_PULSE_OFF_SEC)
            dur = max(FLASH_PULSE_MIN_SEC, dur * FLASH_PULSE_DECAY)
    finally:
        write_bl_power(initial)
        # Restore DPMS-off so we leave the display the way the compositor
        # had it. If the user only configured a wake hook, leave the screen on.
        if DPMS_SLEEP_CMD:
            _run_dpms_cmd(DPMS_SLEEP_CMD, "sleep")


def squawk(tier):
    sound = pick_sound(tier)
    if sound:
        play_sound(sound)
    else:
        log(f"squawk[{tier}]: no sound available, flashing only")
    threading.Thread(target=flash_screen, daemon=True).start()


# ---------------------------------------------------------------------------
# Idle source: X11 (poll) or Wayland (swayidle subprocess driven flag).
# Both expose a current_is_idle() callable.
# ---------------------------------------------------------------------------

def make_x11_idle_source():
    import Xlib.display  # noqa: F401  -- imported lazily so Wayland-only users don't need python3-xlib at runtime

    disp = Xlib.display.Display()
    root = disp.screen().root
    threshold_ms = TICK * 1000

    def current_is_idle():
        try:
            info = root.screensaver_query_info()
        except Exception as e:
            log(f"x11 idle query failed: {e}")
            return False
        return info.idle >= threshold_ms

    log(f"idle source: X11 (DISPLAY={os.environ.get('DISPLAY')}, threshold={TICK}s)")
    return current_is_idle


def make_wayland_idle_source():
    state = {"idle": False}
    proc = subprocess.Popen(
        [
            "swayidle", "-w",
            "timeout", str(TICK), "echo IDLE",
            "resume", "echo ACTIVE",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )

    def reader():
        try:
            for line in proc.stdout:
                line = line.strip()
                if line == "IDLE":
                    state["idle"] = True
                elif line == "ACTIVE":
                    state["idle"] = False
        except Exception as e:
            log(f"swayidle reader failed: {e}")

    threading.Thread(target=reader, daemon=True).start()

    def current_is_idle():
        if proc.poll() is not None:
            log(f"swayidle exited with code {proc.returncode}; treating as not idle")
            return False
        return state["idle"]

    log(f"idle source: Wayland via swayidle (WAYLAND_DISPLAY={os.environ.get('WAYLAND_DISPLAY')}, threshold={TICK}s)")
    return current_is_idle


def make_idle_source():
    if os.environ.get("WAYLAND_DISPLAY"):
        return make_wayland_idle_source()
    if os.environ.get("DISPLAY"):
        return make_x11_idle_source()
    log("no DISPLAY or WAYLAND_DISPLAY set; cannot detect idle. exiting.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

current_is_idle = make_idle_source()

log(f"backlight bl_power: {BL_POWER}")
if DPMS_WAKE_CMD or DPMS_SLEEP_CMD:
    log(f"dpms hooks: wake={DPMS_WAKE_CMD!r} sleep={DPMS_SLEEP_CMD!r} settle={DPMS_WAKE_SETTLE_SEC}s")
else:
    log("dpms hooks: not configured (set IDLE_DPMS_WAKE_CMD / IDLE_DPMS_SLEEP_CMD if the compositor blanks the panel before squawks fire)")
log(f"sound dir: {SOUND_DIR} (fallback: {FALLBACK_DIR})")
log(f"thresholds: gentle@{INITIAL_DELAY}s, nag@+{INTERVAL}s, dumbass@+{INTERVAL_DUMBASS}s")
log(f"lock gating: {LOCK_PROCESSES if LOCK_PROCESSES else 'off (idle-time only)'}")
for _tier, _names in CURATED_SOUNDS.items():
    if _names:
        log(f"curated sounds [{_tier}]: {len(_names)} entries")

n = 1
on_batt_seconds = 0
target = target_for(n)

while True:
    time.sleep(TICK)

    if not current_is_idle():
        if on_batt_seconds or n != 1:
            log(f"idle reset (active): tier {tier_for(n)} -> gentle, accum {on_batt_seconds}s -> 0")
        on_batt_seconds = 0
        n = 1
        target = target_for(n)
        continue

    if not lock_gate_open():
        # Don't accumulate while gate is closed; don't reset either (the
        # locker may come back). Same skip semantics as the bash version's
        # any_monitor_dpms_on() branch.
        continue

    if is_on_ac():
        if on_batt_seconds or n != 1:
            log(f"on AC: reset tier {tier_for(n)} -> gentle, accum {on_batt_seconds}s -> 0")
        on_batt_seconds = 0
        n = 1
        target = target_for(n)
        continue

    on_batt_seconds += TICK

    if on_batt_seconds >= target:
        tier = tier_for(n)
        log(f"squawk[{tier}] (n={n}, accumulated {on_batt_seconds}s on battery)")
        squawk(tier)
        n += 1
        on_batt_seconds = 0
        target = target_for(n)
