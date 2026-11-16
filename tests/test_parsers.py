"""Regression tests for parsers of input this tool doesn't control: cases fuzz/fuzz.py found.

Run: python3 -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import colorlight  # noqa: E402
import fleet  # noqa: E402


class SyslogTimes(unittest.TestCase):
    def test_any_fraction_length_parses(self):
        # Python 3.9's fromisoformat takes only 3 or 6 fraction digits.
        for frac in ("", ".1", ".123", ".12456", ".123456", ".1234567"):
            t, off = colorlight.syslog_time(f"2026-01-02T03:04:05{frac}-08:00 fpp kernel: x", 0)
            self.assertIsNotNone(t, frac)
            self.assertEqual(off, -8 * 3600)

    def test_impossible_dates_are_skipped_not_raised(self):
        for line in ("2026-13-45T03:04:05-08:00 fpp kernel: eth0: Link is Down",
                     "2026-02-30T99:04:05+00:00 fpp x", "Feb 30 03:04:05 fpp kernel: x", "Xyz  1 03:04:05 fpp x"):
            self.assertEqual(colorlight.syslog_time(line, 0), (None, 0), line)
        events, first, last, _ = colorlight.parse_syslog("2026-13-45T03:04:05-08:00 fpp kernel: eth0: Link is Down\n", 0)
        self.assertEqual((events, first, last), ([], None, None))

    def test_fppd_log_with_a_bad_time_is_skipped(self):
        events, first, _ = colorlight.parse_fppd("2026-01-02 25:04:05.123 fppd(1) Found 1 ColorLight receiver\n"
                                                 "2026-01-02 03:04:06.12 fppd(1) Found 2 ColorLight receiver\n", 0)
        self.assertEqual([e.get("v") for e in events], [2])
        self.assertIsNotNone(first)


class PolicyPattern(unittest.TestCase):
    def test_bad_secret_pattern_is_a_value_error(self):
        # Anything else escapes ui/server.py's handler and drops the request with no answer.
        for bad in ("pass|\\H", "(unclosed", 42, ["pass"], "x" * 501):
            with self.assertRaisesRegex(ValueError, "secret_pattern", msg=repr(bad)):
                fleet.validate_policy({"match": {}, "secret_pattern": bad})
        self.assertEqual(fleet.validate_policy({"match": {}, "secret_pattern": "pass|psk"})["secret_pattern"], "pass|psk")


if __name__ == "__main__":
    unittest.main()
