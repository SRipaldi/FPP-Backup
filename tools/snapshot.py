#!/usr/bin/env python3
"""Read-only "snapshot now" of FPP controllers.

Pulls a unit's live config files, playlists, scripts, plugin list, network state
and its newest FPP-made all-backup JSON into snapshots/<host>/<timestamp>/.
Only HTTP GET is used; nothing on the controller is changed.

Snapshots contain secrets (Wi-Fi PSK, osPassword, .htpasswd) and must never be
committed. snapshots/ is in .gitignore.

Usage:
  tools/snapshot.py http://172.1.2.16                  # direct
  tools/snapshot.py --via 172.1.2.10 192.168.1.55      # through the player's proxy
"""
import argparse
import hashlib
import json
import sys
import time
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


def newest_backup(files):
    # FPP names them <host>_all-backup_v<major>_<YYYYmmddHHMMSS>.json
    tops = [f["name"] for f in files
            if f["name"].startswith("backups/") and "_all-backup_" in f["name"]]
    return max(tops, key=lambda n: n.rsplit("_", 1)[-1]) if tops else None


def snapshot(base, out_root, media=False):
    info = json.loads(get(base, "api/system/info"))
    host = info.get("HostName", "unknown")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = out_root / host / stamp
    out.mkdir(parents=True)
    manifest = {"host": host, "source": base, "taken": stamp,
                "fpp_version": info.get("Version"), "platform": info.get("Platform"),
                "variant": info.get("SubPlatform") or info.get("Variant"),
                "uuid": info.get("uuid"), "files": {}, "errors": []}

    def save(rel, blob):
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(blob)
        manifest["files"][rel] = {"bytes": len(blob),
                                  "sha256": hashlib.sha256(blob).hexdigest()}

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
                save(f"{d}/{name}", get(base, f"api/file/{d}/{urllib.parse.quote(name)}"))
            except (urllib.error.URLError, TimeoutError) as e:
                manifest["errors"].append(f"{d}/{name}: {e}")

    print("\r", end="", file=sys.stderr, flush=True)
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
    args = ap.parse_args()

    failed = 0
    for t in args.targets:
        base = f"http://{args.via}/proxy/{t}" if args.via else t.rstrip("/")
        try:
            out, m = snapshot(base, Path(args.out), media=args.media)
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            print(f"FAIL {t}: {e}", file=sys.stderr)
            failed += 1
            continue
        total = sum(f["bytes"] for f in m["files"].values())
        print(f"OK   {m['host']:<12} {m['platform']} FPP {m['fpp_version']}: "
              f"{len(m['files'])} files, {total / 1e6:.1f} MB, "
              f"{len(m['errors'])} errors -> {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}")
        for e in m["errors"]:
            print(f"     ! {e}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
