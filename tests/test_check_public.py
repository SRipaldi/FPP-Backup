"""Tests for tools/check_public.py, against a made-up installation (never this one's real names).

Run: python3 -m unittest discover -s tests
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import check_public  # noqa: E402


class CheckPublic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "config").mkdir()
        (root / "snapshots" / "Barn-Lights").mkdir(parents=True)
        (root / "config" / "fleet-inventory.json").write_text(json.dumps({"controllers": [
            {"host": "FPP-Gazebo", "ip": "10.44.7.20"}, {"host": "FPP-Shed", "ip": "10.44.7.2"}]}))
        (root / "config" / "private-terms.txt").write_text("# show names\nWinter Wonder 2031\n")
        self.saved = check_public.CONFIG, check_public.ROOT
        check_public.CONFIG, check_public.ROOT = root / "config", root
        self.rules = check_public.private_terms() + check_public.LEAK_PATTERNS

    def tearDown(self):
        check_public.CONFIG, check_public.ROOT = self.saved
        self.tmp.cleanup()

    def hits(self, text):
        return check_public.scan_text(text, "t", self.rules)

    def test_names_addresses_and_terms_are_found(self):
        for text in ("upgraded FPP-Gazebo", "the Gazebo unit failed", "player at 10.44.7.2",
                     "anything on 10.44.7.x", "a remote at 10.44.7.99", "backup of Barn-Lights",
                     "playlist Winter Wonder 2031"):
            self.assertTrue(self.hits(text), text)

    def test_ordinary_words_and_other_addresses_pass(self):
        for text in ("fleet.py --master <player-ip>", "the master reboots last", "LEDPanelMatrix outputs",
                     "e.g. 10.0.0.10 or fpp.local", "the gazebo in the garden", "subnet 10.44.70.1",
                     'needs_password ? "password" : "offline"', "password=<your password>",
                     "Steve <123+someone@users.noreply.github.com>"):
            self.assertFalse(self.hits(text), text)

    def test_secrets_and_personal_data_are_found(self):
        # Built from pieces so this file itself passes the pre-commit check.
        for text in ("pass" 'word = "hunter22x"', '{"ps' 'k": "correcthorse"}', "wpa_" "passphrase=MyHomeWifi1",
                     "token ghp_" + "a" * 36, "-----BEGIN OPENSSH " "PRIVATE KEY-----", "mail me at jo" "@" "mailhost.net"):
            self.assertTrue(self.hits(text), text)
            self.assertFalse(any("hunter22x" in h or "MyHomeWifi1" in h for h in self.hits(text)),
                             "a secret's value is never echoed")

    def test_text_checks_markdown_headings_but_commit_msg_skips_git_comments(self):
        root = Path(self.tmp.name)
        (root / "body.md").write_text("## Runs on FPP-Gazebo\nfine\n")
        (root / "MSG").write_text("Fix a thing\n# git comment: on FPP-Gazebo\n")
        import contextlib, io
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_main("--text", str(root / "body.md")), 1)
            self.assertEqual(self.run_main("--commit-msg", str(root / "MSG")), 0)

    def test_binary_or_non_utf8_files_are_still_checked(self):
        f = Path(self.tmp.name) / "blob.bin"
        f.write_bytes(b"\x89PNG\xd2\xff on FPP-Gazebo \x00")
        import contextlib, io
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_main("--text", str(f)), 1)

    def test_all_skips_binary_files_but_checks_odd_text(self):
        import subprocess
        root = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        (root / "icon.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00 FPP-Gazebo")   # binary: skipped
        (root / "notes.txt").write_bytes(b"caf\xe9 on FPP-Gazebo\n")                 # Latin-1 text: checked
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        import contextlib, io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.run_main("--all"), 1)
        self.assertIn("notes.txt", err.getvalue())
        self.assertNotIn("icon.png", err.getvalue())
        # The staged diff holds the non-UTF-8 bytes too: the pre-commit check must read it, not crash.
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_main("--staged"), 1)

    def run_main(self, *argv):
        saved = sys.argv
        sys.argv = ["check_public.py", *argv]
        try:
            return check_public.main()
        finally:
            sys.argv = saved

    def test_private_files_are_refused(self):
        bad = check_public.check_paths(["snapshots/x/manifest.json", "config/settings.json", "work/out/a.img",
                                        ".env", "tools/fleet.py"])
        self.assertEqual(len(bad), 4)
        self.assertFalse(any("tools/fleet.py" in b for b in bad))

    def test_diff_reports_file_and_line(self):
        diff = "+++ b/README.md\n@@ -1,0 +5,2 @@\n+plain line\n+on FPP-Gazebo\n"
        self.assertEqual(check_public.scan_diff(diff, self.rules), ["README.md:6: controller name: 'FPP-Gazebo'"])


if __name__ == "__main__":
    unittest.main()
