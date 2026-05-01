import os
import time

CPU_POLICY_PATH = "/sys/devices/system/cpu/cpufreq/policy0"
BATTERY_CAPACITY_PATH = "/sys/class/power_supply/axp20x-battery/capacity"

NORMAL_GOVERNOR = os.environ.get("CPU_GOVERNOR_NORMAL") or "schedutil"
LOW_BATTERY_GOVERNOR = os.environ.get("CPU_GOVERNOR_LOW_BATTERY") or "powersave"
LOW_BATTERY_ENTER_PCT = int(os.environ.get("CPU_LOW_BATTERY_ENTER_PCT") or 20)
LOW_BATTERY_EXIT_PCT = int(os.environ.get("CPU_LOW_BATTERY_EXIT_PCT") or 25)
POLL_INTERVAL_SEC = int(os.environ.get("CPU_GOVERNOR_POLL_SEC") or 30)


def read_file(path):
    with open(path, "r") as f:
        return f.read().strip()


def write_governor(governor):
    with open(os.path.join(CPU_POLICY_PATH, "scaling_governor"), "w") as f:
        f.write(governor)


def available_governors():
    return read_file(os.path.join(CPU_POLICY_PATH, "scaling_available_governors")).split()


def decide_governor(current, capacity):
    # Hysteresis band: keep current when between exit and enter thresholds
    if capacity < LOW_BATTERY_ENTER_PCT:
        return LOW_BATTERY_GOVERNOR
    if capacity >= LOW_BATTERY_EXIT_PCT:
        return NORMAL_GOVERNOR
    return current


avail = available_governors()
for gov in (NORMAL_GOVERNOR, LOW_BATTERY_GOVERNOR):
    if gov not in avail:
        raise Exception(f"governor '{gov}' not available; have: {avail}")

original_governor = read_file(os.path.join(CPU_POLICY_PATH, "scaling_governor"))
print(f"original governor: {original_governor}")
print(f"normal: {NORMAL_GOVERNOR}, low-battery: {LOW_BATTERY_GOVERNOR}")
print(f"thresholds: enter<{LOW_BATTERY_ENTER_PCT}%, exit>={LOW_BATTERY_EXIT_PCT}%")

current_governor = original_governor
try:
    while True:
        try:
            capacity = int(read_file(BATTERY_CAPACITY_PATH))
            target = decide_governor(current_governor, capacity)
            if target != current_governor:
                write_governor(target)
                print(f"governor: {current_governor} -> {target} (cap={capacity}%)")
                current_governor = target
        except Exception as e:
            print(f"Error in poll loop: {e}")
        time.sleep(POLL_INTERVAL_SEC)
finally:
    try:
        write_governor(original_governor)
        print(f"restored governor: {original_governor}")
    except Exception as e:
        print(f"Error restoring governor: {e}")
