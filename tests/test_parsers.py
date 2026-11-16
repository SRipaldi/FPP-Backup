"""Regression tests for parsers of input this tool doesn't control: cases fuzz/fuzz.py found.

Run: python3 -m unittest discover -s tests
"""
import os
import sys
import unittest
from pathlib import Path

os.environ["FPP_BACKUP_KEYCHAIN"] = "off"  # tests never touch the Keychain (see tools/fpp_http.py)
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

    def test_feb_29_in_a_non_leap_year_is_last_years(self):
        import datetime as dt
        real = colorlight.datetime

        class Fixed(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return real(2029, 3, 1, 12, 0, 0)
        real_time = colorlight.time.time
        colorlight.datetime = Fixed
        colorlight.time.time = lambda: real(2029, 3, 1, 12, 0, 0, tzinfo=dt.timezone.utc).timestamp()
        try:
            t, _ = colorlight.syslog_time("Feb 29 03:04:05 fpp kernel: x", 0)
        finally:
            colorlight.datetime = real
            colorlight.time.time = real_time
        self.assertIsNotNone(t)
        self.assertEqual(dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d"), "2028-02-29")

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

    def test_any_type_confusion_is_a_value_error(self):
        # Every field of the real template swapped for a value of the wrong type: only ValueError may come out.
        import copy
        import json
        template = json.loads((Path(__file__).resolve().parent.parent / "templates/fleet-policy.json").read_text())
        wrong = [[1], 1, 1e999, None, {"x": 1}, "text", True]

        def paths(obj, at=()):
            yield at
            if isinstance(obj, dict):
                for k, v in obj.items():
                    yield from paths(v, at + (k,))

        tried = 0
        for at in paths(template):
            for w in wrong:
                pol = copy.deepcopy(template)
                if not at:
                    pol = w
                else:
                    node = pol
                    for k in at[:-1]:
                        node = node[k]
                    node[at[-1]] = w
                try:
                    fleet.validate_policy(pol)
                except ValueError:
                    pass
                tried += 1
        self.assertGreater(tried, 100)



class KeychainOff(unittest.TestCase):
    def test_off_applies_even_when_set_after_import(self):
        # Test modules load alphabetically; fpp_http is imported (through fleet) before test_upgrade sets the
        # variable. Read at import, the upgrade tests would have used the real "FPP-Backup" Keychain entries.
        import os
        import fpp_http
        saved = os.environ.get("FPP_BACKUP_KEYCHAIN")
        try:
            os.environ["FPP_BACKUP_KEYCHAIN"] = "FPP-Backup"
            os.environ["FPP_BACKUP_KEYCHAIN"] = "off"
            self.assertFalse(fpp_http.available())
            self.assertIsNone(fpp_http.keychain_get("never-cached-account"))
        finally:
            os.environ["FPP_BACKUP_KEYCHAIN"] = saved if saved is not None else "off"

if __name__ == "__main__":
    unittest.main()
