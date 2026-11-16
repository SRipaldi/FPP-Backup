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
        upgrade.PRE_DIR = Path(self.tmp.name) / "pre"
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

    def test_startup_timeout_gets_one_more_reboot(self):
        f = self.fake(mode="startup-fails-once")
        _, err, out = self.go(f, use_os=True)
        self.assertIsNone(err, out)
        self.assertIn("rebooting once more", out)
        self.assertEqual(f.reboots, 2)

    def test_startup_failing_twice_stops(self):
        f = self.fake(mode="startup-fails")
        _, err, _ = self.go(f, use_os=True)
        self.assertIn("failed again after a second reboot", err)
        self.assertEqual(f.reboots, 2, "only one extra reboot")

    def test_fpp_update_that_quiets_fppd_is_rebooted_not_failed(self):
        # Seen on a BeagleBone Black remote: an fpp update applies, then takes fppd (and its progress feed) down to restart its
        # services and does not reboot on its own. The tool must read the quiet feed as "restarting" and reboot
        # to finish — not call it a failure and leave fppd stopped.
        f = self.fake(mode="fpp-restart")
        p, err, out = self.go(f)
        self.assertEqual(p["method"], "fpp")
        self.assertIsNone(err, out)
        self.assertEqual(f.version, "10.4")
        self.assertIn(("POST", "/api/system/reboot"), f.requests, "the tool rebooted to finish the update")
        self.assertIn("PASS  version: 10.3 -> 10.4", out)

    def test_fppd_down_after_reboot_tells_you_to_power_cycle(self):
        # The update took and the board is back on the network, but fppd never comes up. Distinct from a box that
        # vanishes (NotReturned): the message says it updated, isn't playing yet, and to power-cycle then recheck.
        f = self.fake(mode="fppd-down")
        _, err, out = self.go(f)
        self.assertIsNotNone(err, out)
        self.assertIn("fppd is stopped", err)
        self.assertIn("Power-cycle", err)
        self.assertIn("recheck --hosts FakeUnit", err)

    def test_os_upgrade_rides_over_a_missed_progress_check(self):
        # Mid-copy the board can miss a progress read or two; that is not the reboot starting.
        f = self.fake(mode="os-blip")
        _, err, out = self.go(f, use_os=True)
        self.assertIsNone(err, out)
        # Before the fix the first missed read ended follow() as "rebooting", so FPP's own Rebooting stage never showed.
        self.assertIn("stage: Rebooting", out, "follow() kept following after the missed reads")

    def test_os_upgrade_chosen_over_fpp_still_needs_the_space(self):
        f = self.fake()
        f.root_free = 1e9
        p, err, _ = self.go(f, release(1e9), use_os=True)
        self.assertEqual((p["method"], p["blockers"]), ("fpp", []), "the FPP update itself is not blocked")
        self.assertIn("needs 2.0 GB free", err)
        self.assertFalse(any(r[1].startswith("/upgrade") for r in f.requests))

    def test_failed_os_push_removes_its_image(self):
        f = self.fake(platform="BeagleBone Black", sub="TI AM335x BeagleBone Black", os_version="v2026-10b (32bit)",
                      internet=False, mode="fail")
        upgrade.FPPOS_DIR.mkdir(parents=True)
        (upgrade.FPPOS_DIR / "BBB-10.4_2026-11.fppos").write_bytes(b"x" * 300000)
        _, err, out = self.go(f, release(300000))
        self.assertIn("failed", err)
        self.assertEqual(f.uploads, {}, "the uploaded image is deleted after a failure too\n" + out)

    def test_refused_reboot_is_not_reported_as_off_the_network(self):
        f = self.fake(mode="reboot-refused")
        _, err, _ = self.go(f)
        self.assertIn("did not reboot", err)
        self.assertIn("the reboot request failed", err)
        self.assertNotIn("rejoin", err)

    def test_arch_and_image_agree_on_a_hyphenated_64_bit(self):
        f = self.fake(os_version="v2026-10b (64-bit)")
        p = upgrade.unit_plan(f.unit(), release(), time.time())
        self.assertEqual((p["family"], p["arch"], p["fppos"]), ("Pi64", "64-bit", "Pi64-10.4_2026-11.fppos"))

    def test_discover_ignores_malformed_announced_names(self):
        import fleet
        f = self.fake()
        f.systems = [{"hostname": "../../evil", "address": "127.0.0.2"}, {"hostname": "Good", "address": "127.0.0.3"}]
        hosts = {u["host"] for u in fleet.discover(f.addr)}
        self.assertIn("Good", hosts)
        self.assertNotIn("../../evil", hosts)

    def test_only_plain_github_images_are_accepted(self):
        gh = "https://github.com/FalconChristmas/fpp/releases/download/10.4/"
        self.assertTrue(upgrade.asset_ok("Pi64-10.4_2026-11.fppos", gh + "Pi64-10.4_2026-11.fppos"))
        self.assertFalse(upgrade.asset_ok("../Pi64-10.4.fppos", gh + "x"))
        self.assertFalse(upgrade.asset_ok("Pi64-10.4.fppos", "https://example.com/Pi64-10.4.fppos"))
        with self.assertRaises(RuntimeError):
            upgrade.fetch_fppos("../../escape.fppos", {"size": 1, "sha256": None, "url": gh})

    def test_copied_file_paths_are_not_shown(self):
        for line in ("usr/share/vim/vim91/autoload/htmlcomplete.vim", "var/log/apache2/error.log",
                     'rsync: [generator] chown "/mnt/boot/firmware/tispl.bin" failed: Operation not permitted (1)'):
            self.assertTrue(upgrade.NOISE.search(line), line)
        for line in ("===== Rebooting =====", "Download complete...", "ERROR: not enough space"):
            self.assertTrue(upgrade.SHOW.search(line) and not upgrade.NOISE.search(line), line)

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
        self.assertIn("did not rejoin the network", err)

    def test_os_master_not_returning_gives_power_cycle_guidance(self):
        # The master is reached over one link (its Wi-Fi); a first-boot stall after an OS upgrade makes it invisible.
        # The tool must say the upgrade may have succeeded and tell the user to power-cycle and re-check — not "failed".
        _, err, _ = self.go(self.fake(mode="no-return"), use_os=True)
        self.assertIn("may well have completed", err)
        self.assertIn("Power-cycle", err)
        self.assertIn("recheck --hosts", err)
        self.assertIn("only if it stays on the old version", err)

    def test_recheck_confirms_a_recovered_controller(self):
        import fleet
        f = self.fake()
        pre = upgrade.capture(f.unit())                 # state as of 10.3
        upgrade.save_pre(f.unit(), pre, "10.4", "os")   # as `run` would, before the reboot
        f.version = "10.4"                              # the upgrade did take; the box is back after a power cycle
        saved = fleet.discover
        fleet.discover = lambda *a, **k: [f.unit()]
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = upgrade.recheck(f.addr, ["FakeUnit"])
        finally:
            fleet.discover = saved
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("FakeUnit: OK", out.getvalue())
        self.assertIn("PASS  version: 10.3 -> 10.4", out.getvalue())

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


class VerifyPlugins(unittest.TestCase):
    """The 'Plugins must be reinstalled' warning is expected after an OS upgrade and should read that way."""

    def state(self, **over):
        s = {"version": "10.3", "host": "player", "fppd": "running", "mode": "player", "os_level": "2026-10b",
             "ipv4": ["10.0.0.10"], "gateway": "", "outputs": {}, "sequences": [], "playlists": [], "settings": {},
             "warnings": [], "plugins": [], "colorlight": False}
        s.update(over)
        return s

    def find(self, results, name):
        return next((r for r in results if r[1] == name), None)

    def test_plugin_warning_is_its_own_check_after_an_os_upgrade(self):
        pre = self.state()
        post = self.state(version="10.4", warnings=[upgrade.PLUGINS_WARNING], plugins=["fpp-FPPMon", "remote-falcon"])
        results = upgrade.verify(None, pre, post, "10.4", os_upgrade=True)
        plug = self.find(results, "plugins")
        self.assertEqual(plug[0], "WARN")
        self.assertIn("expected after an OS upgrade", plug[2])
        self.assertIn("fpp-FPPMon", plug[2])
        self.assertIn("remote-falcon", plug[2])
        # and it is NOT also dumped into the generic warnings line
        self.assertEqual(self.find(results, "FPP warnings")[2], "no new warnings")

    def test_plugin_warning_on_an_fpp_update_is_an_ordinary_warning(self):
        pre = self.state()
        post = self.state(version="10.4", warnings=[upgrade.PLUGINS_WARNING])
        results = upgrade.verify(None, pre, post, "10.4", os_upgrade=False)
        self.assertIsNone(self.find(results, "plugins"))
        self.assertIn(upgrade.PLUGINS_WARNING, self.find(results, "FPP warnings")[2])

    def test_unexpected_warning_still_flagged_alongside_the_plugin_note(self):
        pre = self.state()
        post = self.state(version="10.4", warnings=[upgrade.PLUGINS_WARNING, "Disk space is low"])
        results = upgrade.verify(None, pre, post, "10.4", os_upgrade=True)
        self.assertIsNotNone(self.find(results, "plugins"))
        self.assertIn("Disk space is low", self.find(results, "FPP warnings")[2])


if __name__ == "__main__":
    unittest.main()
