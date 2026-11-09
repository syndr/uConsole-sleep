import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import sleep_cpu_governor as gov  # noqa: E402

AVAILABLE = ["conservative", "ondemand", "userspace", "powersave", "performance", "schedutil"]


class DecideGovernorTest(unittest.TestCase):
    def setUp(self):
        self.saved = {
            name: getattr(gov, name)
            for name in ("NORMAL_GOVERNOR", "LOW_BATTERY_GOVERNOR", "LOW_BATTERY_ENTER_PCT",
                         "LOW_BATTERY_EXIT_PCT", "AC_NORMAL")
        }
        gov.NORMAL_GOVERNOR = "schedutil"
        gov.LOW_BATTERY_GOVERNOR = "powersave"
        gov.LOW_BATTERY_ENTER_PCT = 20
        gov.LOW_BATTERY_EXIT_PCT = 25
        gov.AC_NORMAL = True

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(gov, name, value)

    def test_below_enter_is_low_battery(self):
        self.assertEqual(gov.decide_governor(19, False, None), ("powersave", "low-battery"))
        self.assertEqual(gov.decide_governor(0, False, "powersave"), ("powersave", "low-battery"))

    def test_at_exit_is_normal(self):
        self.assertEqual(gov.decide_governor(25, False, "powersave"), ("schedutil", "normal"))
        self.assertEqual(gov.decide_governor(100, False, None), ("schedutil", "normal"))

    def test_band_holds_low_only_when_already_low(self):
        for capacity in (20, 24):
            self.assertEqual(gov.decide_governor(capacity, False, "powersave"),
                             ("powersave", "hold"))
            self.assertEqual(gov.decide_governor(capacity, False, None),
                             ("schedutil", "normal"))

    def test_ac_overrides_percentage(self):
        self.assertEqual(gov.decide_governor(5, True, "powersave"), ("schedutil", "ac"))
        self.assertEqual(gov.decide_governor(90, True, None), ("schedutil", "ac"))
        # capacity is not needed when AC decides
        self.assertEqual(gov.decide_governor(None, True, None), ("schedutil", "ac"))

    def test_ac_override_can_be_disabled(self):
        gov.AC_NORMAL = False
        self.assertEqual(gov.decide_governor(5, True, None), ("powersave", "low-battery"))
        self.assertEqual(gov.decide_governor(22, True, "powersave"), ("powersave", "hold"))
        self.assertEqual(gov.decide_governor(90, True, None), ("schedutil", "normal"))

    def test_nothing_held_over_from_ac(self):
        # The daemon passes current=None after an AC decision, so unplugging in
        # the band re-evaluates from the percentage alone.
        self.assertEqual(gov.decide_governor(22, False, None), ("schedutil", "normal"))


class ParseRequestTest(unittest.TestCase):
    def test_accepts_auto_and_available_governors(self):
        self.assertEqual(gov.parse_request("auto\n", AVAILABLE), "auto")
        self.assertEqual(gov.parse_request(" performance ", AVAILABLE), "performance")

    def test_rejects_everything_else(self):
        for raw in ("bogus", "", None, "auto performance", "Performance", "x" * 200):
            self.assertIsNone(gov.parse_request(raw, AVAILABLE), raw)


class FormatStateTextTest(unittest.TestCase):
    def state(self, **kw):
        base = {"governor": "powersave", "mode": "auto", "reason": "low-battery",
                "capacity": 18, "exit_pct": 25}
        base.update(kw)
        return base

    def test_lines(self):
        self.assertEqual(gov.format_state_text(self.state()),
                         "powersave (automatic: low battery, 18%)")
        self.assertEqual(gov.format_state_text(self.state(reason="hold", capacity=22)),
                         "powersave (automatic: holding until 25%, 22%)")
        self.assertEqual(
            gov.format_state_text(self.state(governor="schedutil", reason="ac", capacity=12)),
            "schedutil (automatic: on AC, 12%)")
        self.assertEqual(
            gov.format_state_text(self.state(governor="schedutil", reason="normal", capacity=80)),
            "schedutil (automatic, 80%)")
        self.assertEqual(
            gov.format_state_text(self.state(governor="schedutil", reason="normal",
                                             capacity=None)),
            "schedutil (automatic)")

    def test_manual_does_not_mention_battery(self):
        self.assertEqual(
            gov.format_state_text(self.state(governor="performance", mode="manual",
                                             reason="manual")),
            "performance (set manually, until reboot)")


if __name__ == "__main__":
    unittest.main()
