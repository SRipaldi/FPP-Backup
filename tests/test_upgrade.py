"""Tests for tools/upgrade.py: the release-notes advice, board mapping, and the
whole in-place run against tests/fake_fpp.py (no real controller is touched).

Run: python3 -m unittest discover -s tests
"""
import contextlib
import io
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ["FPP_BACKUP_KEYCHAIN"] = "FPP-Backup-tests"  # never send a real stored password to the fake
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import upgrade  # noqa: E402
from fake_fpp import FakeFPP  # noqa: E402

NOTES_10_4 = """# FPP 10.4

FPP 10.4 is a point release on top of 10.3.

**NOTE**: The OS level moves from 2026-10b to **2026-11**, but an "OS Level" upgrade is **not required** to run 10.4. The only changes at the OS level are kernel updates and security patches.

**Upgrading from 9.x or older**

Due to incompatibilities with the underlying libraries, an OS Level upgrade is required, and moving from 32-bit to 64-bit requires re-imaging.
"""

FAST = {"start": 3, "run_fpp": 20, "run_os": 20, "stall": 2, "reboot_fpp": 8, "reboot_os": 8, "fppd": 3,
        "poll": 0.2, "show_margin": 60}


def release(size=0):
    return {"tag": "10.4", "assets": {"Pi64-10.4_2026-11.fppos": {"url": "https://example/Pi64-10.4_2026-11.fppos", "size": size, "sha256": None},
                                      "BBB-10.4_2026-11.fppos": {"url": "https://example/BBB-10.4_2026-11.fppos", "size": size, "sha256": None}},
            "os": upgrade.os_advice(NOTES_10_4)}


class Advice(unittest.TestCase):
    def test_os_optional(self):
        a = upgrade.os_advice(NOTES_10_4)
        self.assertEqual((a["from"], a["level"], a["required"]), ("2026-10b", "2026-11", False))
        self.assertIn("not required", a["note"])

    def test_os_required(self):
        a = upgrade.os_advice("The OS level moves from 2025-08 to **2026-11**. An OS Level upgrade is required to run 11.0.")
        self.assertTrue(a["required"])

    def test_not_stated(self):
        self.assertIsNone(upgrade.os_advice("Bug fixes only.")["required"])

    def test_board_family(self):
        f = upgrade.board_family
        self.assertEqual(f({"Platform": "Raspberry Pi", "SubPlatform": "Pi 4", "OSVersion": "v2026-10b (64bit)"}), "Pi64")
        self.assertEqual(f({"Platform": "Raspberry Pi", "SubPlatform": "Pi 4", "OSVersion": "v2026-10b (32bit)"}), "Pi")
        self.assertEqual(f({"Platform": "BeagleBone Black", "SubPlatform": "TI AM335x BeagleBone Black"}), "BBB")
        self.assertEqual(f({"Platform": "BeagleBone 64", "SubPlatform": "BeagleBoard.org PocketBeagle2"}), "BB64")
        self.assertEqual(upgrade.fppos_for("Pi", release()["assets"]), None)  # never the 64-bit image for a 32-bit Pi
        self.assertEqual(upgrade.fppos_for("Pi64", release()["assets"]), "Pi64-10.4_2026-11.fppos")


class Run(unittest.TestCase):
    def setUp(self):
        self.saved = dict(upgrade.LIMITS)
        upgrade.LIMITS.update(FAST)
        self.tmp = tempfile.TemporaryDirectory()
        upgrade.SNAPSHOTS = Path(self.tmp.name) / "snapshots"
        upgrade.FPPOS_DIR = Path(self.tmp.name) / "fppos"
        self.fakes = []

    def tearDown(self):
        upgrade.LIMITS.clear()
        upgrade.LIMITS.update(self.saved)
        for f in self.fakes:
            f.close()
        self.tmp.cleanup()

    def fake(self, **kw):
        f = FakeFPP(**kw)
        self.fakes.append(f)
        return f

    def go(self, f, rel=None, use_os=False):
        rel = rel or release()
        u = f.unit()
        p = upgrade.unit_plan(u, rel, time.time())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                upgrade.run_unit(u, p, rel, f.addr, use_os, dry=False)
                err = None
            except RuntimeError as e:
                err = str(e)
        return p, err, out.getvalue()

    def test_fpp_update(self):
        f = self.fake()
        p, err, out = self.go(f)
        self.assertEqual(p["method"], "fpp")
        self.assertIsNone(err, out)
        self.assertEqual(f.version, "10.4")
        self.assertIn(("POST", "/api/system/reboot"), f.requests)
        self.assertIn("PASS  version: 10.3 -> 10.4", out)

    def test_os_push_uploads_applies_and_removes(self):
        f = self.fake(platform="BeagleBone Black", sub="TI AM335x BeagleBone Black", os_version="v2026-10b (32bit)", internet=False)
        upgrade.FPPOS_DIR.mkdir(parents=True)
        (upgrade.FPPOS_DIR / "BBB-10.4_2026-11.fppos").write_bytes(b"x" * 300000)
        p, err, out = self.go(f, release(300000))
        self.assertEqual(p["method"], "os-push")
        self.assertIsNone(err, out)
        self.assertEqual(f.version, "10.4")
        self.assertTrue(any(r[0] == "POST" and r[1].startswith("/jqupload.php") for r in f.requests))
        self.assertEqual(f.uploads, {}, "the image is removed afterwards")
        self.assertNotIn(("POST", "/api/system/reboot"), f.requests, "an OS upgrade reboots by itself")

    def test_os_upgrade_from_github_removes_its_image(self):
        f = self.fake()
        f.uploads["Pi64-10.4_2026-11.fppos"] = 10  # where FPP's own download lands
        _, err, out = self.go(f, use_os=True)
        self.assertIsNone(err, out)
        self.assertEqual(f.uploads, {})

    def test_reported_failure_stops_without_reboot(self):
        f = self.fake(mode="fail")
        _, err, _ = self.go(f)
        self.assertIn("failed", err)
        self.assertNotIn(("POST", "/api/system/reboot"), f.requests)

    def test_stall(self):
        _, err, _ = self.go(self.fake(mode="stall"))
        self.assertIn("no progress", err)

    def test_controller_never_returns(self):
        _, err, _ = self.go(self.fake(mode="no-return"))
        self.assertIn("did not come back", err)

    def test_verification_catches_lost_content(self):
        _, err, out = self.go(self.fake(mode="lose-sequence"))
        self.assertIn("verification failed: sequences", err)
        self.assertIn("missing b.fseq", out)

    def test_refuses_while_show_plays(self):
        f = self.fake()
        f.playing = True
        _, err, _ = self.go(f)
        self.assertIn("playing", err)
        self.assertEqual(f.version, "10.3")

    def test_refuses_when_show_starts_soon(self):
        f = self.fake()
        f.next_show = time.time() + 10 * 60
        _, err, _ = self.go(f)
        self.assertIn("starts in", err)
        self.assertFalse(any(r[1].startswith("/upgrade") for r in f.requests))

    def test_os_upgrade_without_an_image_is_refused(self):
        f = self.fake(platform="BeagleBone 64", sub="BeagleBoard.org PocketBeagle2")  # release() has no BB64 image
        _, err, _ = self.go(f, use_os=True)
        self.assertIn("no OS image", err)
        self.assertFalse(any(r[1].startswith("/upgrade") for r in f.requests))

    def test_cleanup_deletes_images_and_skips_a_busy_controller(self):
        import fleet
        idle, working = self.fake(), self.fake()
        idle.uploads.update({"Pi64-10.3_2026-10b.fppos": 100, "song.mp3": 5})
        working.uploads["Pi64-10.4_2026-11.fppos"] = 100
        working.act["inProgress"] = True
        units = [dict(idle.unit("Idle"), addresses=[]), dict(working.unit("Working"), addresses=[])]
        saved = fleet.discover
        fleet.discover = lambda *a, **k: units
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = upgrade.cleanup(idle.addr, ["Idle", "Working"])
        finally:
            fleet.discover = saved
        self.assertEqual(rc, 0, out.getvalue())
        self.assertEqual(idle.uploads, {"song.mp3": 5}, "only .fppos files are deleted")
        self.assertIn("Pi64-10.4_2026-11.fppos", working.uploads)
        self.assertIn("Working: an update is running", out.getvalue())

    def test_plan_flags_backup_and_old_images(self):
        f = self.fake()
        f.uploads["Pi64-10.3_2026-10b.fppos"] = 1000
        p = upgrade.unit_plan(f.unit(), release(), time.time())
        self.assertTrue(p["backup_due"])
        self.assertTrue(any("old OS image" in n for n in p["notes"]))


if __name__ == "__main__":
    unittest.main()
