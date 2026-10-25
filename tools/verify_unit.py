#!/usr/bin/env python3
"""Check a running unit against a snapshot, read-only (HTTP GET only).

Run after booting a rebuilt card: identity, network, setup state, settings,
config files, content and media must match what the snapshot (and the staged
settings) say, and FPP's own boot must have completed.

Usage:
  tools/verify_unit.py snapshots/FPP-Side/<ts> --via 172.1.2.10 192.168.1.59 \\
      [--stage work/stage/FPP-Side]
"""
import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Files FPP itself rewrites on a new version or box; compared by presence only.
REWRITTEN = {"interface.eth0", "interface.wlan0", "interface.wlan1", "gateway", "dns",
             "cape-eeprom.bin", "settings"}
CONTENT_DIRS = ["playlists", "scripts", "sequences", "music", "videos", "images", "effects"]
# 10.x hides these from /api/files/config on every unit; network is checked via its own API.
HIDDEN = re.compile(r"^(interface\..*|\.htpasswd|\.htaccess)$")
# 10.x never returns these through /api/settings.
UNREADABLE = {"osPassword", "osPasswordVerify", "password", "passwordVerify"}
# Rewritten by fppinit on 10.x first boot (PipeWire picks and persists the audio device).
FPP_REWRITES = {"AudioOutput", "AudioMixerDevice", "ForceAudioId"}

results = []


def check(name, ok, detail=""):
    results.append((ok, name, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{': ' + detail if detail else ''}", flush=True)


def get(base, path, timeout=30):
    with urllib.request.urlopen(f"{base}/{path}", timeout=timeout) as r:
        return r.read()


def getj(base, path):
    return json.loads(get(base, path))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("snapshot", type=Path)
    ap.add_argument("target", help="unit IP (or base URL without --via)")
    ap.add_argument("--via", help="player IP to proxy through")
    ap.add_argument("--stage", type=Path, help="staged tree the card was built from (expected settings)")
    args = ap.parse_args()

    base = f"http://{args.via}/proxy/{args.target}" if args.via else args.target.rstrip("/")
    if not base.startswith("http"):
        base = "http://" + base
    snap = args.snapshot
    m = json.loads((snap / "manifest.json").read_text())
    print(f"Verifying {base} against {snap}")

    print("Identity")
    info = getj(base, "api/system/info")
    check("hostname", info.get("HostName") == m["host"], info.get("HostName"))
    check("platform", info.get("Platform") == m["platform"], info.get("Platform"))
    print(f"  info  FPP {info.get('Version')} (snapshot was {m['fpp_version']}), cape "
          f"{(info.get('capeInfo') or {}).get('name')}")
    snap_info = json.loads((snap / "api/system-info.json").read_text())
    want_cape = (snap_info.get("capeInfo") or {}).get("name")
    check("cape detected", (info.get("capeInfo") or {}).get("name") == want_cape, want_cape or "none")

    print("Network")
    want_net = json.loads((snap / "api/network-config.json").read_text())
    for iface, cfg in want_net.items():
        if iface in ("dns", "gateway") or not isinstance(cfg, dict) or "PROTO" not in cfg:
            continue
        got = getj(base, f"api/network/interface/{iface}")
        check(f"{iface} saved config", (got.get("PROTO"), got.get("ADDRESS")) == (cfg.get("PROTO"), cfg.get("ADDRESS")),
              f"{got.get('PROTO')} {got.get('ADDRESS')}")
        if cfg.get("PROTO") == "static":
            check(f"{iface} live address", got.get("CurrentAddress") == cfg.get("ADDRESS"),
                  str(got.get("CurrentAddress")))
    want_gw = want_net.get("gateway", {}).get("GATEWAY") or next(
        (c.get("GATEWAY") for c in want_net.values() if isinstance(c, dict) and c.get("GATEWAY")), None)
    if want_gw:
        try:
            got_gw = getj(base, "api/network/gateway").get("GATEWAY")
        except urllib.error.HTTPError:
            got_gw = None
        check("gateway", got_gw == want_gw, str(got_gw))
    want_dns = want_net.get("dns", {})
    got_dns = getj(base, "api/network/dns")
    check("dns", all(got_dns.get(k) == want_dns.get(k) for k in ("DNS1", "DNS2")),
          f"{got_dns.get('DNS1')} {got_dns.get('DNS2')}")

    print("Setup and boot")
    setting = lambda k: getj(base, f"api/settings/{urllib.parse.quote(k)}").get("value")
    check("setup completed (initialSetup-02)", setting("initialSetup-02") in ("1", 1))
    status = getj(base, "api/fppd/status")
    check("fppd running", status.get("fppd") == "running", f"{status.get('mode_name')}, {status.get('status_name')}")
    tz = setting("TimeZone")
    check("clock not stuck on UTC", " UTC " not in status.get("time", "") or tz in ("UTC", "Etc/UTC"),
          f"{status.get('time')} (TimeZone {tz})")
    log = get(base, "api/file/logs/fppd.log", timeout=60).decode(errors="replace")
    boots = log.split("Running FPPINIT start")
    last = boots[-1] if len(boots) > 1 else ""
    check("last fppinit start reached network setup", "Setting max IGMP memberships" in last
          or "Need to restart/reload" in last,
          "" if last else "no fppinit start in log")

    print("Settings")
    if args.stage and (args.stage / "settings").exists():
        want = dict(re.findall(r'^(\S+) = "?(.*?)"?$', (args.stage / "settings").read_text(), re.M))
        bad, rewritten = [], []
        for k, v in want.items():
            if k in UNREADABLE:
                continue
            got = setting(k)
            if str(got if got is not None else "") != v:
                (rewritten if k in FPP_REWRITES else bad).append(f"{k}={got!r} (staged {v!r})")
        checked = len([k for k in want if k not in UNREADABLE])
        check(f"{checked} staged settings", not bad, "; ".join(bad))
        if rewritten:
            print(f"  info  changed by FPP on boot (expected): {'; '.join(rewritten)}")
        print(f"  info  not readable via the API: {', '.join(sorted(k for k in want if k in UNREADABLE))}")
    else:
        print("  skip  (no --stage given)")

    print("Files")
    snap_files = m["files"]
    for d in ["config"] + CONTENT_DIRS:
        want = {k[len(d) + 1:]: f for k, f in snap_files.items()
                if k.startswith(d + "/") and not k.startswith("config/backups/")}
        if not want:
            continue
        got = {f["name"]: f for f in getj(base, f"api/files/{d}").get("files", [])}
        missing = sorted(n for n in want if n not in got and not HIDDEN.match(Path(n).name))
        check(f"{d}: {len(want)} files present", not missing, ", ".join(missing[:5]))
        changed = []
        for n, f in want.items():
            if n not in got or Path(n).name in REWRITTEN:
                continue
            if d == "config" or got[n].get("sizeBytes") != f["bytes"]:
                blob = get(base, f"api/file/{d}/{urllib.parse.quote(n)}", timeout=120)
                if hashlib.sha256(blob).hexdigest() != f["sha256"]:
                    changed.append(n)
        check(f"{d}: contents match", not changed, ", ".join(changed[:5]))

    failed = [r for r in results if not r[0]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
