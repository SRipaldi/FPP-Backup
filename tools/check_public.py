#!/usr/bin/env python3
"""Check text bound for the public repo for this installation's private details and for secrets.

This repository is public. Nothing about the show it runs (controller names, addresses, subnets, show and
sequence names) and no secret or personal information may go into a commit, a commit message, a pull
request, a release note or an issue. This tool finds them before they leave the machine.

What counts as private is read from this installation's own, git-ignored files, so the list itself is never
published:
  config/fleet-inventory.json   controller names and addresses (and their subnets)
  config/settings.json          the player's address
  snapshots/<host>/             names of controllers that were backed up
  config/private-terms.txt      anything else, one literal term per line (# comments): show, playlist and
                                song names, street, Wi-Fi network names, personal email...
Secrets and personal data (tokens, private keys, passwords, email addresses) are matched by pattern.

Usage:
  tools/check_public.py --staged            the staged changes and file names (the pre-commit hook)
  tools/check_public.py --text FILE...      PR body, release notes, issue text: every line, headings included
  tools/check_public.py --commit-msg FILE   a commit message, minus git's own # comment lines (the commit-msg hook)
  tools/check_public.py --log RANGE         commit messages and changes in a range, e.g. origin/main..HEAD
  tools/check_public.py --all               every tracked file
  tools/check_public.py --pre-push          what a push would publish (the pre-push hook; refs on stdin)
  tools/check_public.py --install-hooks     install the three hooks in .git/hooks
Exit status 1 when anything is found. A finding that is genuinely fine can be committed with --no-verify,
after a second look.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
# Folders that hold this installation's data, git-ignored; nothing in them may ever be committed.
PRIVATE_DIRS = ("snapshots/", "work/", "config/", "fpp-issues/")
PRIVATE_FILES = re.compile(r"(^|/)(\.env|.*\.img(\.xz)?|.*\.fppos|.*\.pem|.*\.key|id_(rsa|ed25519)(\.pub)?)$")
LEAK_PATTERNS = [
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})")),
    ("AWS key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("password or key in text", re.compile(
        r"(?i)\b(password|passwd|psk|passphrase|secret|api[_-]?key|auth[_-]?token|token)\b(\s*=\s*|[\"']?:\s*)[\"']"
        r"(?!<|\$|\{)[^\"'\s]{6,}[\"']")),
    ("Wi-Fi key", re.compile(r"(?i)\b(wpa_passphrase|psk)=(?!<)\S{8,}")),
    ("email address", re.compile(
        r"\b[A-Za-z0-9._%+-]+@(?!users\.noreply\.github\.com\b|anthropic\.com\b|example\.(com|org)\b)"
        r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
]


def private_terms():
    """[(label, regex)] for this installation's names and addresses."""
    names, addrs = set(), set()
    try:
        inv = json.loads((CONFIG / "fleet-inventory.json").read_text())
        for c in inv.get("controllers", []):
            names.add(str(c.get("host") or ""))
            addrs.add(str(c.get("ip") or ""))
    except (OSError, ValueError):
        pass
    try:
        addrs.add(str(json.loads((CONFIG / "settings.json").read_text()).get("player") or ""))
    except (OSError, ValueError):
        pass
    snaps = ROOT / "snapshots"
    if snaps.is_dir():
        names.update(p.name for p in snaps.iterdir() if p.is_dir())
    extra = []
    try:
        extra = [ln.strip() for ln in (CONFIG / "private-terms.txt").read_text().splitlines()
                 if ln.strip() and not ln.lstrip().startswith("#")]
    except OSError:
        pass
    out = []
    for n in sorted(x for x in names if len(x) >= 3):
        out.append(("controller name", re.compile(r"(?<![\w-])" + re.escape(n) + r"(?![\w-])", re.I)))
        short = re.sub(r"^FPP-", "", n)
        # The part after "FPP-" is how people say it ("the Gazebo unit"); capitalized only, so the plain words
        # (master, side, tree) in ordinary prose don't match.
        if short != n and len(short) >= 4 and short[0].isupper():
            out.append(("controller name", re.compile(r"(?<![\w-])" + re.escape(short) + r"(?![\w-])")))
    nets = set()
    for a in addrs:
        if re.match(r"^\d+\.\d+\.\d+\.\d+$", a):
            out.append(("address", re.compile(r"(?<![\d.])" + re.escape(a) + r"(?![\d])")))
            nets.add(a.rsplit(".", 1)[0])
        elif a:
            out.append(("address", re.compile(r"(?<![\w.-])" + re.escape(a) + r"(?![\w-])", re.I)))
    for n in sorted(nets):
        out.append(("subnet", re.compile(r"(?<![\d.])" + re.escape(n) + r"\.(\d{1,3}|x)\b")))
    for t in extra:
        out.append(("private term", re.compile(re.escape(t), re.I)))
    return out


HIDE_VALUE_KINDS = {label for label, _ in LEAK_PATTERNS}


def scan_text(text, where, rules):
    found = []
    for i, line in enumerate(text.splitlines(), 1):
        for label, rx in rules:
            m = rx.search(line)
            if not m:
                continue
            if label in HIDE_VALUE_KINDS:
                # Never echo a secret: this output can end up in a terminal log, CI or a pasted transcript.
                found.append(f"{where}:{i}: {label} at column {m.start() + 1} (value not shown)")
            else:
                found.append(f"{where}:{i}: {label}: {m.group(0)[:60]!r}")
    return found


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def check_paths(paths):
    return [f"{p}: private file or folder; never commit it" for p in paths
            if p.startswith(PRIVATE_DIRS) or PRIVATE_FILES.search(p)]


def scan_diff(diff, rules):
    """Added lines of a unified diff, reported by file and new line number."""
    found, path, line = [], None, 0
    for d in diff.splitlines():
        if d.startswith("+++ "):
            path = d[6:] if d.startswith("+++ b/") else d[4:]
        elif d.startswith("@@"):
            m = re.search(r"\+(\d+)", d)
            line = int(m.group(1)) if m else 0
        elif d.startswith("+") and path:
            found += [f.replace("<diff>:1:", f"{path}:{line}:", 1) for f in scan_text(d[1:], "<diff>", rules)]
            line += 1
        elif not d.startswith("-"):
            line += 1
    return found


def scan_commits(shas, rules):
    """Each commit's message, the files it adds and the lines it adds (a root commit included)."""
    found = []
    for sha in shas:
        found += [f"commit {sha[:7]}: {f}" for f in
                  check_paths(git("show", "--name-only", "--diff-filter=AR", "--format=", sha).splitlines())]
        found += scan_text(git("log", "-1", "--format=%B", sha), f"commit {sha[:7]} message", rules)
        found += [f"commit {sha[:7]}: {f}" for f in scan_diff(git("show", "-U0", "--format=", sha), rules)]
    return found


def install_hooks():
    hooks = Path(git("rev-parse", "--git-path", "hooks").strip())
    if not hooks.is_absolute():
        hooks = ROOT / hooks
    hooks.mkdir(parents=True, exist_ok=True)
    tool = "tools/check_public.py"
    for name, cmd in (("pre-commit", f'python3 {tool} --staged'),
                      ("commit-msg", f'python3 {tool} --commit-msg "$1"'),
                      ("pre-push", f'python3 {tool} --pre-push')):
        p = hooks / name
        p.write_text(f"#!/bin/sh\n# Installed by {tool}: keep private details and secrets out of the public repo.\n"
                     f'cd "$(git rev-parse --show-toplevel)" && exec {cmd}\n')
        p.chmod(0o755)
        print(f"installed {p}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--staged", action="store_true")
    g.add_argument("--text", nargs="+", metavar="FILE")
    g.add_argument("--commit-msg", metavar="FILE")
    g.add_argument("--log", metavar="RANGE")
    g.add_argument("--all", action="store_true")
    g.add_argument("--pre-push", action="store_true")
    g.add_argument("--install-hooks", action="store_true")
    args = ap.parse_args()
    if args.install_hooks:
        return install_hooks()
    rules = private_terms() + LEAK_PATTERNS
    if args.staged:
        found = check_paths(git("diff", "--cached", "--name-only", "--diff-filter=AR").splitlines())
        found += scan_diff(git("diff", "--cached", "-U0"), rules)
    elif args.text:
        found = []
        for f in args.text:  # every line: a Markdown heading starts with # too
            found += scan_text(Path(f).read_text(), f, rules)
    elif args.commit_msg:
        # git's own comment lines in the message file are stripped before the commit, so never published.
        text = "\n".join("" if ln.startswith("#") else ln for ln in Path(args.commit_msg).read_text().splitlines())
        found = scan_text(text, args.commit_msg, rules)
    elif args.log:
        found = scan_commits(git("rev-list", args.log).split(), rules)
    elif args.pre_push:
        found = []
        for ln in sys.stdin.read().splitlines():
            parts = ln.split()
            if len(parts) < 4 or set(parts[1]) == {"0"}:
                continue  # deleting a remote branch publishes nothing
            # Only the commits no remote has yet: what this push publishes. A tag is checked through its commit.
            found += scan_commits(git("rev-list", parts[1], "--not", "--remotes").split(), rules)
    else:
        found = check_paths(git("ls-files").splitlines())
        for p in git("ls-files").splitlines():
            try:
                found += scan_text((ROOT / p).read_text(), p, rules)
            except (UnicodeDecodeError, OSError):
                continue
    if found:
        print("Private details or secrets found in what would be published (this repo is public):", file=sys.stderr)
        for f in found:
            print("  " + f, file=sys.stderr)
        print("Replace them with generic stand-ins (the player, a remote, 10.0.0.x, song.fseq) and try again. "
              "If one is genuinely fine, look twice, then use --no-verify.", file=sys.stderr)
        return 1
    if not (args.text or args.commit_msg):
        print("check_public: nothing private found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
