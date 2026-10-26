#!/usr/bin/env python3
"""Read-only "snapshot now" of FPP controllers.

Pulls a unit's live config files, playlists, scripts, plugin list, network state
and its newest FPP-made all-backup JSON into snapshots/<host>/<timestamp>/.
Only HTTP GET is used; nothing on the controller is changed.

Snapshots contain secrets (Wi-Fi PSK, osPassword, .htpasswd) and must never be
committed. snapshots/ is in .gitignore.

Media is incremental: a media file is hard-linked from the unit's previous
snapshot instead of downloaded when that snapshot has it with the same size and
the controller's listing shows it was last modified before that snapshot
started (or with the same recorded modification time). Every snapshot is still
complete on its own; linked files take no extra space, and deleting an old
snapshot does not affect newer ones. Config files are always downloaded.
--full downloads everything.

Usage:
  tools/snapshot.py http://<unit-ip>                   # direct
  tools/snapshot.py --via <player-ip> <unit-ip>       # through the player's proxy
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TIMEOUT = 60

INFO_ENDPOINTS = {
    "system-info.json": "api/system/info",
    "fppd-version.json": "api/fppd/version",
    "network-interfaces.json": "api/network/interface",
    "plugins.json": "api/plugin",
    "playlists.json": "api/playlists",
}
FILE_DIRS = ["config", "playlists", "scripts"]
# FPP hostnames are letters, digits and hyphens. Anything else is not used as a
# folder name: a device answering on the show network chooses what it reports.
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-]{0,62}$")
MEDIA_DIRS = ["sequences", "music", "videos", "images", "effects"]


def get(base, path):
    req = urllib.request.Request(f"{base}/{path}", method="GET")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def list_dir(base, d):
    data = json.loads(get(base, f"api/files/{d}"))
    # Directories are listed with sizeBytes 0 and no extension; skip them by
    # checking against the names of other entries' parents.
    names = [f["name"] for f in data.get("files", [])]
    parents = {n.rsplit("/", 1)[0] for n in names if "/" in n}
    return [f for f in data.get("files", []) if f["name"] not in parents]


# A file modified this close to (or after) the previous snapshot's start is
# downloaded again: the listing has minute resolution and clocks may differ a little.
REUSE_MARGIN = timedelta(minutes=10)


def listing_mtime(text):
    """FPP's file-listing time ("10/19/26  07:44 PM", the controller's local time)."""
    try:
        return datetime.strptime(" ".join(str(text).split()), "%m/%d/%y %I:%M %p")
    except ValueError:
        return None


def previous_snapshot(host_dir, current):
    """The newest earlier snapshot of this unit: (path, manifest, start time), or None."""
    for d in sorted((p for p in host_dir.iterdir() if p.is_dir() and p != current), reverse=True):
        try:
            m = json.loads((d / "manifest.json").read_text())
            return d, m, datetime.strptime(m["taken"], "%Y%m%d-%H%M%S")
        except (OSError, ValueError, KeyError):
            continue
    return None


def newest_backup(files):
    # FPP names them <host>_all-backup_v<major>_<YYYYmmddHHMMSS>.json
    tops = [f["name"] for f in files
            if f["name"].startswith("backups/") and "_all-backup_" in f["name"]]
    return max(tops, key=lambda n: n.rsplit("_", 1)[-1]) if tops else None


def snapshot(base, out_root, media=False, full=False):
    info = json.loads(get(base, "api/system/info"))
    host = info.get("HostName", "unknown")
    if not HOST_RE.match(host):
        raise ValueError(f"refusing hostname {host[:40]!r}: not a valid FPP hostname")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = out_root / host / stamp
    out.mkdir(parents=True, mode=0o700)
    manifest = {"host": host, "source": base, "taken": stamp,
                "fpp_version": info.get("Version"), "platform": info.get("Platform"),
                "variant": info.get("SubPlatform") or info.get("Variant"),
                "uuid": info.get("uuid"), "files": {}, "errors": []}

    def inside(rel):
        # File names come from the controller's listing: never let one escape the snapshot.
        p = (out / rel).resolve()
        if not str(p).startswith(str(out.resolve()) + os.sep):
            raise ValueError(f"refusing file name {rel[:80]!r}: outside the snapshot")
        return p

    def save(rel, blob, mtime=None):
        p = inside(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(blob)
        manifest["files"][rel] = {"bytes": len(blob),
                                  "sha256": hashlib.sha256(blob).hexdigest()}
        if mtime:
            manifest["files"][rel]["mtime"] = mtime

    prev = None if full else previous_snapshot(out.parent, out)
    reused = {"files": 0, "bytes": 0}

    def reuse(rel, entry):
        """Hard-link rel from the previous snapshot when the listing shows it unchanged."""
        if not prev:
            return False
        pdir, pman, ptaken = prev
        old = pman.get("files", {}).get(rel)
        if not old or old.get("bytes") != entry.get("sizeBytes"):
            return False
        mt = listing_mtime(entry.get("mtime"))
        if not ((old.get("mtime") and old["mtime"] == entry.get("mtime"))
                or (mt and mt < ptaken - REUSE_MARGIN)):
            return False
        src, dst = pdir / rel, inside(rel)
        if not src.is_file() or src.stat().st_size != old["bytes"]:
            return False
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(src, dst)
        except OSError:
            return False
        manifest["files"][rel] = {"bytes": old["bytes"], "sha256": old["sha256"],
                                  "mtime": entry.get("mtime"), "reused_from": pman["taken"]}
        reused["files"] += 1
        reused["bytes"] += old["bytes"]
        return True

    for name, ep in INFO_ENDPOINTS.items():
        try:
            save(f"api/{name}", get(base, ep))
        except (urllib.error.URLError, TimeoutError) as e:
            manifest["errors"].append(f"{ep}: {e}")

    # Saved network config per interface, plus DNS and gateway. Wi-Fi PSKs are
    # not returned here (only PSKSet); they come from the FPP backup JSON.
    net = {}
    try:
        ifaces = [i["ifname"] for i in json.loads(get(base, "api/network/interface"))
                  if i.get("ifname") and i["ifname"] != "lo"]
        for name in ifaces:
            net[name] = json.loads(get(base, f"api/network/interface/{name}"))
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        manifest["errors"].append(f"network interfaces: {e}")
    for key, ep in (("dns", "api/network/dns"), ("gateway", "api/network/gateway")):
        try:
            net[key] = json.loads(get(base, ep))
        except urllib.error.HTTPError as e:
            if e.code != 404:  # 8.5 has no gateway endpoint; it lives on the interface
                manifest["errors"].append(f"{ep}: {e}")
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            manifest["errors"].append(f"{ep}: {e}")
    save("api/network-config.json", json.dumps(net, indent=2).encode())

    # The settings file itself is not downloadable; read each value live so the
    # snapshot does not depend on how old the unit's last FPP backup is.
    try:
        schema_blob = get(base, "api/settings")
        # Kept for its per-setting defaults: stage_card.py uses them to tell a
        # setting changed on the unit from one the API merely reports a default for.
        save("api/settings-schema.json", schema_blob)
        names = json.loads(schema_blob).get("settings", {}).keys()
        live, missing = {}, []
        for n in names:
            try:
                live[n] = json.loads(get(base, f"api/settings/{urllib.parse.quote(n)}")).get("value")
            except (urllib.error.URLError, TimeoutError, ValueError):
                missing.append(n)
        save("api/settings-live.json", json.dumps(live, indent=2, sort_keys=True).encode())
        if missing:
            manifest["errors"].append(f"settings not readable: {missing}")
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        manifest["errors"].append(f"api/settings: {e}")

    for d in FILE_DIRS + (MEDIA_DIRS if media else []):
        try:
            files = list_dir(base, d)
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            manifest["errors"].append(f"list {d}: {e}")
            continue
        listing = {f["name"]: f for f in files}
        wanted = [f["name"] for f in files if not f["name"].startswith("backups/")]
        if d == "config":
            nb = newest_backup(files)
            if nb:
                wanted.append(nb)
                manifest["fpp_backup_file"] = nb
        for i, name in enumerate(wanted, 1):
            # \r progress: the UI shows it as one updating line
            print(f"\r  {host} {d}: {i}/{len(wanted)} {name[:60]}", end="", file=sys.stderr, flush=True)
            try:
                entry = listing.get(name, {})
                if d in MEDIA_DIRS and reuse(f"{d}/{name}", entry):
                    continue
                save(f"{d}/{name}", get(base, f"api/file/{d}/{urllib.parse.quote(name)}"),
                     entry.get("mtime") if d in MEDIA_DIRS else None)
            except (urllib.error.URLError, TimeoutError, ValueError) as e:
                manifest["errors"].append(f"{d}/{name}: {e}")

    print("\r", end="", file=sys.stderr, flush=True)
    if reused["files"]:
        manifest["reused"] = {**reused, "from": prev[1]["taken"]}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return out, manifest


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("targets", nargs="+", help="base URL, or IP when using --via")
    ap.add_argument("--via", help="player IP to proxy through")
    ap.add_argument("--out", default=str(ROOT / "snapshots"))
    ap.add_argument("--media", action="store_true",
                    help="also pull sequences, music, videos, images, effects")
    ap.add_argument("--full", action="store_true",
                    help="download every media file, even ones unchanged since the last snapshot")
    args = ap.parse_args()
    os.umask(0o077)  # snapshots hold passwords and Wi-Fi keys: readable by this user only

    failed = 0
    for t in args.targets:
        base = f"http://{args.via}/proxy/{t}" if args.via else t.rstrip("/")
        try:
            out, m = snapshot(base, Path(args.out), media=args.media, full=args.full)
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            print(f"FAIL {t}: {e}", file=sys.stderr)
            failed += 1
            continue
        total = sum(f["bytes"] for f in m["files"].values())
        print(f"OK   {m['host']:<12} {m['platform']} FPP {m['fpp_version']}: "
              f"{len(m['files'])} files, {total / 1e6:.1f} MB, "
              f"{len(m['errors'])} errors -> {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}")
        if m.get("reused"):
            r = m["reused"]
            print(f"     {r['files']} unchanged media files ({r['bytes'] / 1e6:.0f} MB) linked from "
                  f"the {r['from']} snapshot, not downloaded")
        for e in m["errors"]:
            print(f"     ! {e}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
