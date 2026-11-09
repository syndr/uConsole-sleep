"""CPU governor daemon and its command line.

Without arguments this is the sleep-cpu-governor daemon: it keeps
scaling_governor at either an automatic choice (AC / battery percentage with
hysteresis) or a profile the user selected for this boot.

    sleep_cpu_governor --set <auto|governor>   select a profile (until reboot)
    sleep_cpu_governor --get [--text]          show what the daemon is doing

The selection lives in RUN_DIR/cpu-profile (one word) and the daemon reports
its decision in RUN_DIR/cpu-governor.state (JSON). Both are in /run, so a
selection lasts exactly one boot.
"""

import argparse
import grp
import json
import os
import shlex
import signal
import subprocess
import sys
import time

CPU_POLICY_PATH = "/sys/devices/system/cpu/cpufreq/policy0"
POWER_SUPPLY_PATH = "/sys/class/power_supply"
BATTERY_CAPACITY_PATH = "/sys/class/power_supply/axp20x-battery/capacity"
RUN_DIR = "/run/uconsole-sleep"
CONTROL_NAME = "cpu-profile"
STATE_NAME = "cpu-governor.state"

AC_SUPPLY_TYPES = ("Mains", "USB", "USB_PD")
AC_BATTERY_STATUSES = ("Charging", "Full")

# The control file is re-read this often, so a selection applies within ~2s
# even though the battery is only polled every POLL_INTERVAL_SEC.
SLICE_SEC = 2
PCT_CMD_TIMEOUT_SEC = 5
SET_WAIT_SEC = 3
CONTROL_MAX_BYTES = 64

NORMAL_GOVERNOR = os.environ.get("CPU_GOVERNOR_NORMAL") or "schedutil"
LOW_BATTERY_GOVERNOR = os.environ.get("CPU_GOVERNOR_LOW_BATTERY") or "powersave"
LOW_BATTERY_ENTER_PCT = int(os.environ.get("CPU_LOW_BATTERY_ENTER_PCT") or 20)
LOW_BATTERY_EXIT_PCT = int(os.environ.get("CPU_LOW_BATTERY_EXIT_PCT") or 25)
POLL_INTERVAL_SEC = int(os.environ.get("CPU_GOVERNOR_POLL_SEC") or 30)
AC_NORMAL = (os.environ.get("CPU_GOVERNOR_AC_NORMAL") or "yes").strip().lower() != "no"
BATTERY_PCT_CMD = (os.environ.get("CPU_BATTERY_PCT_CMD") or "").strip()
PROFILE_GROUP = (os.environ.get("CPU_PROFILE_GROUP") or "").strip().strip("\"'")

_pct_cmd_failed = False


def control_path():
    return os.path.join(RUN_DIR, CONTROL_NAME)


def state_path():
    return os.path.join(RUN_DIR, STATE_NAME)


def read_file(path):
    with open(path, "r") as f:
        return f.read().strip()


def read_governor():
    return read_file(os.path.join(CPU_POLICY_PATH, "scaling_governor"))


def write_governor(governor):
    with open(os.path.join(CPU_POLICY_PATH, "scaling_governor"), "w") as f:
        f.write(governor)


def available_governors():
    return read_file(os.path.join(CPU_POLICY_PATH, "scaling_available_governors")).split()


def ac_online():
    """True when a mains/USB supply is online or the battery says it is charging."""
    try:
        names = os.listdir(POWER_SUPPLY_PATH)
    except OSError:
        return False
    for name in names:
        base = os.path.join(POWER_SUPPLY_PATH, name)
        try:
            psy_type = read_file(os.path.join(base, "type"))
            if psy_type in AC_SUPPLY_TYPES:
                if read_file(os.path.join(base, "online")) == "1":
                    return True
            elif psy_type == "Battery":
                if read_file(os.path.join(base, "status")) in AC_BATTERY_STATUSES:
                    return True
        except OSError:
            continue
    return False


def read_capacity():
    """Battery percentage: CPU_BATTERY_PCT_CMD when set, else the sysfs gauge."""
    global _pct_cmd_failed
    if BATTERY_PCT_CMD:
        try:
            out = subprocess.run(
                shlex.split(BATTERY_PCT_CMD),
                capture_output=True,
                text=True,
                timeout=PCT_CMD_TIMEOUT_SEC,
                check=True,
            ).stdout
            value = int(out.split()[0].rstrip("%"))
            if not 0 <= value <= 100:
                raise ValueError(f"{value} is not a percentage")
            if _pct_cmd_failed:
                print("CPU_BATTERY_PCT_CMD is working again")
                _pct_cmd_failed = False
            return value
        except Exception as e:
            if not _pct_cmd_failed:
                print(f"CPU_BATTERY_PCT_CMD failed ({e!r}); using sysfs capacity")
                _pct_cmd_failed = True
    return int(read_file(BATTERY_CAPACITY_PATH))


def decide_governor(capacity, ac, current):
    """Automatic choice: (governor, reason).

    `current` is the previous *automatic* decision when that was the
    low-battery governor, else None. It is not the live governor, which may
    be a manual selection or a stray write. `capacity` is not looked at when
    AC decides.
    """
    if ac and AC_NORMAL:
        return NORMAL_GOVERNOR, "ac"
    if capacity < LOW_BATTERY_ENTER_PCT:
        return LOW_BATTERY_GOVERNOR, "low-battery"
    if capacity >= LOW_BATTERY_EXIT_PCT:
        return NORMAL_GOVERNOR, "normal"
    # Hysteresis band: stay low only if we were already low.
    if current == LOW_BATTERY_GOVERNOR:
        return LOW_BATTERY_GOVERNOR, "hold"
    return NORMAL_GOVERNOR, "normal"


def parse_request(raw, available):
    """Validate control-file content. Returns 'auto', a governor name, or None."""
    word = (raw or "").strip()
    if word == "auto" or word in available:
        return word
    return None


def format_state_text(state):
    governor = state.get("governor", "?")
    if state.get("mode") == "manual":
        return f"{governor} (set manually, until reboot)"
    reason = state.get("reason")
    if reason == "low-battery":
        detail = "automatic: low battery"
    elif reason == "hold":
        detail = f"automatic: holding until {state.get('exit_pct')}%"
    elif reason == "ac":
        detail = "automatic: on AC"
    else:
        detail = "automatic"
    capacity = state.get("capacity")
    if capacity is not None:
        detail += f", {capacity}%"
    return f"{governor} ({detail})"


# --- control and state files -------------------------------------------------

def read_control():
    """Raw control-file content ('' if empty), or None when it is not there."""
    try:
        fd = os.open(control_path(), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        data = os.read(fd, CONTROL_MAX_BYTES + 1)
    finally:
        os.close(fd)
    return data.decode("ascii", "replace").strip()


def write_control(word):
    """(Re)create the control file with `word` and the configured permissions."""
    path = control_path()
    if os.path.islink(path) or (os.path.exists(path) and not os.path.isfile(path)):
        os.unlink(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    try:
        os.write(fd, (word + "\n").encode())
        mode = 0o666
        if PROFILE_GROUP:
            try:
                os.fchown(fd, -1, grp.getgrnam(PROFILE_GROUP).gr_gid)
                mode = 0o664
            except (KeyError, OSError) as e:
                print(f"CPU_PROFILE_GROUP '{PROFILE_GROUP}' unusable ({e!r}); "
                      "profile selection is root-only")
                mode = 0o644
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def write_state(state):
    tmp = state_path() + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, (json.dumps(state) + "\n").encode())
        os.fchmod(fd, 0o644)
    finally:
        os.close(fd)
    os.replace(tmp, state_path())


def read_state():
    with open(state_path(), "r") as f:
        return json.load(f)


# --- daemon ------------------------------------------------------------------

class Daemon:
    def __init__(self):
        self.available = available_governors()
        for gov in (NORMAL_GOVERNOR, LOW_BATTERY_GOVERNOR):
            if gov not in self.available:
                raise Exception(f"governor '{gov}' not available; have: {self.available}")
        self.requested = "auto"
        self.low_latch = None       # LOW_BATTERY_GOVERNOR while auto is holding low
        self.last_auto = (NORMAL_GOVERNOR, "normal")
        self.empty_seen = False

    def setup_run_dir(self):
        os.makedirs(RUN_DIR, mode=0o755, exist_ok=True)
        os.chmod(RUN_DIR, 0o755)
        # Keep a selection made earlier this boot; (re)apply permissions.
        request = parse_request(read_control(), self.available)
        write_control(request or "auto")

    def poll_request(self):
        """Current request from the control file; repairs it when invalid."""
        raw = read_control()
        if raw == "" and not self.empty_seen:
            # Probably a writer between truncate and write: look again next slice.
            self.empty_seen = True
            return self.requested
        self.empty_seen = False
        if raw is None:
            write_control("auto")
            return "auto"
        request = parse_request(raw, self.available)
        if request is None:
            print(f"invalid profile request {raw[:CONTROL_MAX_BYTES]!r}; using auto")
            write_control("auto")
            return "auto"
        return request

    def evaluate(self):
        ac = ac_online()
        try:
            capacity = read_capacity()
        except Exception as e:
            print(f"cannot read battery capacity: {e}")
            capacity = None

        if capacity is not None or (ac and AC_NORMAL):
            self.last_auto = decide_governor(capacity, ac, self.low_latch)
        auto_governor, auto_reason = self.last_auto
        self.low_latch = (
            LOW_BATTERY_GOVERNOR if auto_reason in ("low-battery", "hold") else None
        )

        if self.requested == "auto":
            mode, desired, reason = "auto", auto_governor, auto_reason
        else:
            mode, desired, reason = "manual", self.requested, "manual"

        actual = read_governor()
        if actual != desired:
            cap = "?" if capacity is None else f"{capacity}%"
            try:
                write_governor(desired)
                print(f"governor: {actual} -> {desired} ({reason}, cap={cap}, ac={ac})")
            except OSError as e:
                print(f"cannot set governor {desired}: {e}")
            actual = read_governor()

        write_state({
            "governor": actual,
            "mode": mode,
            "reason": reason,
            "requested": self.requested,
            "capacity": capacity,
            "ac_online": ac,
            "available": self.available,
            "normal": NORMAL_GOVERNOR,
            "low_battery": LOW_BATTERY_GOVERNOR,
            "enter_pct": LOW_BATTERY_ENTER_PCT,
            "exit_pct": LOW_BATTERY_EXIT_PCT,
            "ts": time.time(),
        })

    def run(self):
        original_governor = read_governor()
        print(f"original governor: {original_governor}")
        print(f"normal: {NORMAL_GOVERNOR}, low-battery: {LOW_BATTERY_GOVERNOR}, "
              f"normal on AC: {'yes' if AC_NORMAL else 'no'}")
        print(f"thresholds: enter<{LOW_BATTERY_ENTER_PCT}%, exit>={LOW_BATTERY_EXIT_PCT}%")
        if LOW_BATTERY_EXIT_PCT < LOW_BATTERY_ENTER_PCT:
            print("warning: CPU_LOW_BATTERY_EXIT_PCT is below CPU_LOW_BATTERY_ENTER_PCT")

        # systemd stops us with SIGTERM; turn it into an exit so `finally` runs.
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

        self.setup_run_dir()
        last_eval = None
        try:
            while True:
                try:
                    request = self.poll_request()
                    now = time.monotonic()
                    if (request != self.requested or last_eval is None
                            or now - last_eval >= POLL_INTERVAL_SEC):
                        if request != self.requested:
                            print(f"profile request: {self.requested} -> {request}")
                        self.requested = request
                        last_eval = now
                        self.evaluate()
                except Exception as e:
                    print(f"Error in poll loop: {e}")
                time.sleep(max(0.1, min(SLICE_SEC, POLL_INTERVAL_SEC)))
        finally:
            # Leave the kernel's governor behind, and no state file claiming we
            # are running. The control file stays: a selection lasts the boot.
            try:
                write_governor(original_governor)
                print(f"restored governor: {original_governor}")
            except Exception as e:
                print(f"Error restoring governor: {e}")
            try:
                os.unlink(state_path())
            except OSError:
                pass


# --- command line ------------------------------------------------------------

def print_state(state, text):
    print(format_state_text(state) if text else json.dumps(state))


def cli_get(text):
    try:
        state = read_state()
    except (OSError, ValueError):
        print("no governor state: sleep-cpu-governor is not running or older than 1.8",
              file=sys.stderr)
        return 1
    print_state(state, text)
    return 0


def cli_set(word, text):
    try:
        available = available_governors()
    except OSError as e:
        print(f"cannot read available governors: {e}", file=sys.stderr)
        return 1
    if parse_request(word, available) is None:
        print(f"unknown profile '{word}'; choose auto or one of: {' '.join(available)}",
              file=sys.stderr)
        return 2

    started = time.time()
    try:
        fd = os.open(control_path(), os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
    except FileNotFoundError:
        print(f"{control_path()} does not exist: sleep-cpu-governor is not running "
              "or older than 1.8", file=sys.stderr)
        return 1
    except PermissionError:
        print(f"no permission to write {control_path()} (see CPU_PROFILE_GROUP in "
              "/etc/uconsole-sleep/config)", file=sys.stderr)
        return 1
    try:
        os.write(fd, (word + "\n").encode())
    finally:
        os.close(fd)

    deadline = time.monotonic() + SET_WAIT_SEC
    while time.monotonic() < deadline:
        try:
            state = read_state()
        except (OSError, ValueError):
            state = None
        if (state and state.get("requested") == word and state.get("ts", 0) >= started
                and (word == "auto" or state.get("governor") == word)):
            print_state(state, text)
            return 0
        time.sleep(0.1)
    print(f"'{word}' was saved but not applied within {SET_WAIT_SEC}s; "
          "is sleep-cpu-governor running? It applies the selection when it starts.",
          file=sys.stderr)
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="sleep_cpu_governor",
        description="CPU governor daemon (no arguments) and profile control.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--set", metavar="PROFILE",
                       help="select 'auto' or a governor name for this boot")
    group.add_argument("--get", action="store_true",
                       help="print the daemon's state (JSON)")
    parser.add_argument("--text", action="store_true",
                        help="with --get/--set: print one readable line instead of JSON")
    args = parser.parse_args(argv)

    if args.set is not None:
        return cli_set(args.set, args.text)
    if args.get:
        return cli_get(args.text)
    Daemon().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
