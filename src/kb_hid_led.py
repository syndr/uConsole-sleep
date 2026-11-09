"""Host -> keyboard sleep signal over the standard HID LED output report.

Unbinding the keyboard's USB driver stops input, but the keyboard MCU keeps
running and never learns the host is asleep. This module sets one LED bit
(default: Kana, which nothing on a Linux host ever drives) in the keyboard's
LED output report before the unbind and clears it after the rebind. Firmware
that understands the bit (uConsole-tweaks/zmk-uc9900) darkens its lights and
drops to low power while it is set; other firmware ignores it.

Pure stdlib. The report layout is read from the HID report descriptor, so it
works for any keyboard that exposes the requested LED usage.

CLI (root):  kb_hid_led.py {on|off|status} [ledname]
"""

import glob
import os
import sys
import time

LED_USAGE_PAGE = 0x08

# HID LED usage IDs (usage page 0x08).
LED_USAGES = {
    "numlock": 0x01,
    "capslock": 0x02,
    "scrolllock": 0x03,
    "compose": 0x04,
    "kana": 0x05,
}


def led_usage(name):
    """Map a config name to a LED usage ID; None for 'none'/empty/unknown."""
    if not name:
        return None
    return LED_USAGES.get(name.strip().lower())


def find_hid(kb_device_path):
    """Return (hid_sysfs_dir, /dev/hidrawN) for the keyboard, or None.

    The HID instance suffix (0003:1D50:615E.000N) changes on every rebind, so
    this is resolved fresh on each call.
    """
    pattern = os.path.join(kb_device_path, "*:*", "[0-9A-Fa-f]*:*", "hidraw", "hidraw*")
    for node in sorted(glob.glob(pattern)):
        hid_dir = os.path.dirname(os.path.dirname(node))
        dev = os.path.join("/dev", os.path.basename(node))
        if os.path.exists(dev):
            return hid_dir, dev
    return None


def wait_for_hid(kb_device_path, timeout_s):
    """Poll find_hid() until it succeeds or timeout_s elapses."""
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while True:
        found = find_hid(kb_device_path)
        if found or time.monotonic() >= deadline:
            return found
        time.sleep(0.05)


def find_led_output(descriptor, usage):
    """Locate an LED usage in the Output reports of a HID report descriptor.

    Returns (report_id, payload_len, bit_offset) or None. payload_len is the
    byte length of that report excluding the report-ID byte; bit_offset is
    the position of the usage within the payload.
    """
    out_bits = {}  # report_id -> output bits seen so far
    usage_page = 0
    report_id = 0
    report_size = 0
    report_count = 0
    usages = []
    usage_min = None
    usage_max = None
    found = None

    i = 0
    n = len(descriptor)
    while i < n:
        prefix = descriptor[i]
        i += 1
        if prefix == 0xFE:  # long item: skip
            if i < n:
                i += 1 + descriptor[i] + 1
            continue
        size = (0, 1, 2, 4)[prefix & 0x03]
        itype = (prefix >> 2) & 0x03
        tag = prefix >> 4
        data = int.from_bytes(descriptor[i:i + size], "little") if size else 0
        i += size

        if itype == 1:  # global
            if tag == 0x0:
                usage_page = data
            elif tag == 0x7:
                report_size = data
            elif tag == 0x8:
                report_id = data
            elif tag == 0x9:
                report_count = data
        elif itype == 2:  # local
            if tag == 0x0:
                usages.append(data & 0xFFFF)
            elif tag == 0x1:
                usage_min = data & 0xFFFF
            elif tag == 0x2:
                usage_max = data & 0xFFFF
        elif itype == 0:  # main
            if tag == 0x9:  # Output
                field_usages = list(usages)
                if usage_min is not None and usage_max is not None:
                    field_usages.extend(range(usage_min, usage_max + 1))
                start = out_bits.get(report_id, 0)
                if found is None and usage_page == LED_USAGE_PAGE and usage in field_usages:
                    idx = field_usages.index(usage)
                    if idx < report_count:
                        found = (report_id, start + idx * report_size)
                out_bits[report_id] = start + report_size * report_count
            if tag in (0x8, 0x9, 0xB, 0xA, 0xC):  # Input/Output/Feature/Collection/End
                usages = []
                usage_min = None
                usage_max = None

    if found is None:
        return None
    rid, bit = found
    payload_len = (out_bits[rid] + 7) // 8
    return rid, payload_len, bit


def resolve(kb_device_path, usage):
    """Return (hidraw_dev, report_id, payload_len, bit_offset) or None."""
    found = find_hid(kb_device_path)
    if not found:
        return None
    hid_dir, dev = found
    with open(os.path.join(hid_dir, "report_descriptor"), "rb") as f:
        descriptor = f.read()
    layout = find_led_output(descriptor, usage)
    if layout is None:
        return None
    return (dev,) + layout


def set_led(kb_device_path, usage, on):
    """Write the LED output report with only `usage` set (if on) or all clear.

    Returns the hidraw device path on success, None if the keyboard has no
    hidraw node or no such LED. Raises OSError if the write itself fails.
    """
    resolved = resolve(kb_device_path, usage)
    if resolved is None:
        return None
    dev, report_id, payload_len, bit = resolved
    payload = bytearray(payload_len)
    if on:
        payload[bit // 8] |= 1 << (bit % 8)
    report = bytes([report_id]) + bytes(payload)
    fd = os.open(dev, os.O_WRONLY)
    try:
        os.write(fd, report)
    finally:
        os.close(fd)
    return dev


def _main(argv):
    from find_internal_kb import find_internal_kb

    if len(argv) < 2 or argv[1] not in ("on", "off", "status"):
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    action = argv[1]
    name = argv[2] if len(argv) > 2 else "kana"
    usage = led_usage(name)
    if usage is None:
        print(f"unknown led name: {name} (one of {', '.join(LED_USAGES)})", file=sys.stderr)
        return 2
    kb = find_internal_kb()
    if not kb:
        print("no internal keyboard found", file=sys.stderr)
        return 1
    resolved = resolve(kb, usage)
    if resolved is None:
        print(f"{kb}: no hidraw node or no {name} LED in its output reports")
        return 1
    dev, report_id, payload_len, bit = resolved
    print(f"{kb}: {dev} report_id={report_id} payload_len={payload_len} bit={bit}")
    if action != "status":
        set_led(kb, usage, action == "on")
        print(f"{name} {action}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
