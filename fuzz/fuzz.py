#!/usr/bin/env python3
"""Fuzz targets for the code that parses input this tool doesn't control.

  syslog         controller syslog / fppd.log text   (colorlight.parse_syslog, parse_fppd)
  release-notes  GitHub release notes                (upgrade.os_advice, notes_head)
  rootfs         controller settings -> shell script (stage_card.rootfs_script; must stay injection-free)
  fseq           sequence file headers               (xlights.fseq_header)
  policy         the fleet policy JSON from the UI   (fleet.validate_policy)
  leak-check     any text checked before publishing  (check_public.scan_text; catastrophic regex time)

A parser of untrusted input must not raise anything but the error it documents (ValueError where noted),
must not hang, and rootfs must never let a setting's value escape into the shell.

In CI (.github/workflows/fuzz.yml) this runs under Atheris (libFuzzer), pinned by version and hash:
  python3 fuzz/fuzz.py <target> -max_total_time=60 fuzz/corpus/<target>
Without Atheris (no install needed) it runs the same checks on random inputs, as a quick local check:
  python3 fuzz/fuzz.py <target> --selftest [iterations]
"""
import json
import os
import random
import re
import shlex
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

try:
    import atheris
except ImportError:  # --selftest only
    atheris = None


def _imports():
    global check_public, colorlight, fleet, stage_card, upgrade, xlights
    import check_public  # noqa: F401
    import colorlight  # noqa: F401
    import fleet  # noqa: F401
    import stage_card  # noqa: F401
    import upgrade  # noqa: F401
    import xlights  # noqa: F401


if atheris:
    with atheris.instrument_imports():
        _imports()
else:
    _imports()


def text(data):
    return data.decode("utf-8", "replace")


def t_syslog(data):
    s = text(data)
    colorlight.parse_syslog(s, 0)
    colorlight.parse_fppd(s, 0)


def t_release_notes(data):
    s = text(data)
    upgrade.os_advice(s)
    upgrade.notes_head(s)


TZ_LINE = re.compile(r'^\[ -e "\$ROOTFS/usr/share/zoneinfo/(.*)" \] \|\| ')
SAFE_TZ = re.compile(r"^[A-Za-z0-9_+/\-]*$")
SAFE_NTP = re.compile(r"^[A-Za-z0-9.\-]*$")


def t_rootfs(data):
    parts = text(data).split("\x00")
    keys = ["TimeZone", "ntpServer"] + list(stage_card.SERVICE_UNITS)
    settings = {k: parts[i] for i, k in enumerate(keys) if i < len(parts)}
    script = stage_card.rootfs_script(settings, [])
    for line in script.splitlines():
        m = TZ_LINE.match(line)
        if m:
            assert SAFE_TZ.match(m.group(1)), f"time zone escaped its filter: {line!r}"
        if line.startswith("echo "):
            words = shlex.split(line.split(" >> ")[0])  # raises on broken quoting
            assert len(words) == 2, f"echo line split into extra words: {line!r}"
            server = words[1].split()
            assert server[0] in ("server", "pool"), line
            if server[0] == "server":
                assert SAFE_NTP.match(server[1]), f"ntp server escaped its filter: {line!r}"
    # Every line must be one of the shapes the generator writes, nothing a value could add.
    for line in script.splitlines():
        assert line.startswith(("#", "set -e", "[ -e ", "sed -i ", "echo ", "[ -f ", "rm -f ")), f"unexpected line: {line!r}"


def t_fseq(data):
    fd, path = tempfile.mkstemp(suffix=".fseq")
    try:
        os.write(fd, data)
        os.close(fd)
        try:
            xlights.fseq_header(path)
        except ValueError:
            pass  # documented: not an FSEQ file
    finally:
        os.unlink(path)


def t_policy(data):
    try:
        obj = json.loads(text(data))
    except ValueError:
        return
    try:
        fleet.validate_policy(obj)
    except ValueError:
        pass  # documented: a bad policy is refused with ValueError


LEAK_RULES = None


def t_leak_check(data):
    global LEAK_RULES
    if LEAK_RULES is None:  # made-up installation names, never real ones
        LEAK_RULES = [("controller name", re.compile(r"(?<![\w-])FPP-Gazebo(?![\w-])", re.I)),
                      ("subnet", re.compile(r"(?<![\d.])10\.44\.7\.(\d{1,3}|x)\b"))] + check_public.LEAK_PATTERNS
    check_public.scan_text(text(data), "fuzz", LEAK_RULES)


TARGETS = {"syslog": t_syslog, "release-notes": t_release_notes, "rootfs": t_rootfs, "fseq": t_fseq,
           "policy": t_policy, "leak-check": t_leak_check}


def selftest(fn, n):
    """Random and mutated inputs without Atheris: a smoke test, not a substitute for coverage-guided fuzzing."""
    rnd = random.Random(1234)
    corpus = [p.read_bytes() for p in (Path(__file__).parent / "corpus" / sys.argv[1]).glob("*")] or [b""]
    alphabet = b"0123456789-:T+ .\n\x00{}[]\",:aAzZ$`;|&()<>'\\/\xff"
    for i in range(n):
        base = bytearray(rnd.choice(corpus))
        for _ in range(rnd.randint(1, 8)):
            op = rnd.random()
            pos = rnd.randint(0, len(base))
            if op < 0.4:
                base[pos:pos] = bytes(rnd.choice(alphabet) for _ in range(rnd.randint(1, 6)))
            elif op < 0.7 and base:
                del base[pos:pos + rnd.randint(1, 4)]
            elif base:
                base[min(pos, len(base) - 1)] = rnd.randrange(256)
        try:
            fn(bytes(base))
        except Exception:
            print(f"selftest: input {i} failed: {bytes(base)!r}", file=sys.stderr)
            raise
    print(f"selftest {sys.argv[1]}: {n} inputs OK")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in TARGETS:
        sys.exit(f"usage: fuzz.py <{'|'.join(TARGETS)}> [--selftest [n] | libFuzzer options]")
    fn = TARGETS[sys.argv[1]]
    if "--selftest" in sys.argv:
        i = sys.argv.index("--selftest")
        return selftest(fn, int(sys.argv[i + 1]) if len(sys.argv) > i + 1 else 20000)
    if not atheris:
        sys.exit("atheris is not installed (CI installs a pinned copy); use --selftest locally")
    atheris.Setup([sys.argv[0]] + sys.argv[2:], fn)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
