"""Optional sleep-time power knobs for sleep-power-control.

Each knob is applied in the deferred sleep step (after KB_SLEEP_DELAY_SEC) and
reverted on wake. All are off by default, never raise, and remember what they
changed so wake restores only what sleep touched.

  WifiPowerSave  - `iw dev <wlan> set power_save on` on every 802.11 interface,
                   prior state restored on wake (needs the `iw` package)
  BluetoothOff   - `bluetoothctl power off` when no device is connected,
                   powered back on at wake only if this turned it off
  CpuQuota       - runtime CPUQuota=<pct>% on every user-<uid>.slice, cleared
                   on wake; processes keep running, just capped in aggregate
"""

import glob
import os
import shutil
import subprocess


def _run(cmd, timeout=5):
    """Run a command; return (returncode, stdout) and never raise."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return -1, str(e)


class WifiPowerSave:
    def __init__(self):
        self.prev = {}  # iface -> "on"/"off" before sleep
        self.warned = False

    @staticmethod
    def interfaces():
        return sorted(os.path.basename(os.path.dirname(p))
                      for p in glob.glob("/sys/class/net/*/phy80211"))

    def sleep(self):
        if not shutil.which("iw"):
            if not self.warned:
                print("wifi powersave: `iw` not installed, skipped")
                self.warned = True
            return
        for dev in self.interfaces():
            rc, out = _run(["iw", "dev", dev, "get", "power_save"])
            prev = "on" if rc == 0 and "on" in out.lower() else "off"
            if prev == "on":
                print(f"wifi powersave: {dev} already on")
                continue
            rc, out = _run(["iw", "dev", dev, "set", "power_save", "on"])
            if rc == 0:
                self.prev[dev] = prev
                print(f"wifi powersave: {dev} on")
            else:
                print(f"wifi powersave: {dev} failed: {out.strip()}")

    def wake(self):
        for dev, prev in list(self.prev.items()):
            rc, out = _run(["iw", "dev", dev, "set", "power_save", prev])
            print(f"wifi powersave: {dev} restored to {prev}" if rc == 0
                  else f"wifi powersave: {dev} restore failed: {out.strip()}")
        self.prev = {}


class BluetoothOff:
    def __init__(self):
        self.turned_off = False
        self.warned = False

    def sleep(self):
        self.turned_off = False
        if not shutil.which("bluetoothctl"):
            if not self.warned:
                print("bluetooth: bluetoothctl not installed, skipped")
                self.warned = True
            return
        rc, out = _run(["bluetoothctl", "show"])
        if rc != 0 or "Powered: yes" not in out:
            print("bluetooth: already off or no adapter")
            return
        rc, out = _run(["bluetoothctl", "devices", "Connected"])
        connected = [l for l in out.splitlines() if l.startswith("Device ")]
        if connected:
            print(f"bluetooth: {len(connected)} device(s) connected, left on")
            return
        rc, out = _run(["bluetoothctl", "power", "off"])
        if rc == 0:
            self.turned_off = True
            print("bluetooth: powered off")
        else:
            print(f"bluetooth: power off failed: {out.strip()}")

    def wake(self):
        if not self.turned_off:
            return
        rc, out = _run(["bluetoothctl", "power", "on"])
        print("bluetooth: powered on" if rc == 0 else f"bluetooth: power on failed: {out.strip()}")
        self.turned_off = False


class CpuQuota:
    def __init__(self, percent):
        self.percent = percent
        self.slices = []

    @staticmethod
    def user_slices():
        return sorted(os.path.basename(p)
                      for p in glob.glob("/sys/fs/cgroup/user.slice/user-*.slice"))

    def sleep(self):
        self.slices = []
        for unit in self.user_slices():
            rc, out = _run(["systemctl", "set-property", "--runtime", unit, f"CPUQuota={self.percent}%"])
            if rc == 0:
                self.slices.append(unit)
                print(f"cpu quota: {unit} capped at {self.percent}%")
            else:
                print(f"cpu quota: {unit} failed: {out.strip()}")

    def wake(self):
        for unit in self.slices:
            rc, out = _run(["systemctl", "set-property", "--runtime", unit, "CPUQuota="])
            print(f"cpu quota: {unit} uncapped" if rc == 0
                  else f"cpu quota: {unit} uncap failed: {out.strip()}")
        self.slices = []

    @classmethod
    def clear_all(cls):
        """Drop any runtime quota on every user slice (startup hygiene)."""
        for unit in cls.user_slices():
            _run(["systemctl", "set-property", "--runtime", unit, "CPUQuota="])
