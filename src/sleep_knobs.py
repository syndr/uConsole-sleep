"""Optional sleep-time power knobs for sleep-power-control.

Each knob is applied in the deferred sleep step (after KB_SLEEP_DELAY_SEC) and
reverted on wake. All are off by default, never raise, and remember what they
changed so wake restores only what sleep touched.

  WifiPowerSave  - `iw dev <wlan> set power_save on` on every 802.11 interface,
                   prior state restored on wake (needs the `iw` package)
  BluetoothOff   - rfkill soft-block of bluetooth when no device is connected,
                   unblocked at wake only if this blocked it
  CpuQuota       - runtime CPUQuota=<pct>% on every user-<uid>.slice, cleared
                   on wake; processes keep running, just capped in aggregate
"""

import glob
import os
import shutil
import struct
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
    """Soft-block Bluetooth through the kernel's rfkill interface while asleep.

    rfkill rather than `bluetoothctl power off`: BlueZ powers the adapter down
    either way, but desktop applets (blueman's PowerManager) treat rfkill as the
    source of truth and react to an adapter that goes off on its own by trying
    to set rfkill themselves, which pops a polkit password prompt.
    """

    RFKILL_TYPE_BLUETOOTH = 2
    RFKILL_OP_CHANGE_ALL = 3

    def __init__(self):
        self.blocked = False
        self.warned = False

    @staticmethod
    def bt_rfkills():
        out = []
        for d in glob.glob("/sys/class/rfkill/rfkill*"):
            try:
                with open(os.path.join(d, "type")) as f:
                    if f.read().strip() == "bluetooth":
                        with open(os.path.join(d, "soft")) as g:
                            out.append((d, g.read().strip() == "1"))
            except OSError:
                pass
        return out

    def _set_soft_block(self, block):
        # struct rfkill_event { u32 idx; u8 type; u8 op; u8 soft; u8 hard; }
        ev = struct.pack("IBBBB", 0, self.RFKILL_TYPE_BLUETOOTH, self.RFKILL_OP_CHANGE_ALL,
                         1 if block else 0, 0)
        fd = os.open("/dev/rfkill", os.O_WRONLY)
        try:
            os.write(fd, ev)
        finally:
            os.close(fd)

    def sleep(self):
        self.blocked = False
        rfk = self.bt_rfkills()
        if not rfk:
            if not self.warned:
                print("bluetooth: no rfkill switch of type bluetooth, skipped")
                self.warned = True
            return
        if all(soft for _, soft in rfk):
            print("bluetooth: already blocked")
            return
        if shutil.which("bluetoothctl"):
            rc, out = _run(["bluetoothctl", "devices", "Connected"])
            connected = [l for l in out.splitlines() if l.startswith("Device ")]
            if connected:
                print(f"bluetooth: {len(connected)} device(s) connected, left on")
                return
        try:
            self._set_soft_block(True)
            self.blocked = True
            print("bluetooth: rfkill soft-blocked")
        except OSError as e:
            print(f"bluetooth: rfkill block failed: {e}")

    def wake(self):
        if not self.blocked:
            return
        try:
            self._set_soft_block(False)
            print("bluetooth: rfkill unblocked")
        except OSError as e:
            print(f"bluetooth: rfkill unblock failed: {e}")
        self.blocked = False


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
