#!/usr/bin/env python3
"""Push sequences to controllers through xLights, as FPP Connect does.

Uses xLights' automation API on this computer (port 49913: Preferences, xFade
port A). xLights finds the controller (through the player's proxy when it is
not directly reachable), cuts the sequence down to the channel ranges that
controller drives (v2 zstd sparse), and uploads it. This tool only decides
what goes where, refuses while a show is playing, and checks the result: the
copy on the controller must carry the same render ID and frame count as the
.fseq in the show folder.

status and versions are read-only. push writes one .fseq to each named
controller; the player also gets the audio (remotes play no audio in MultiSync).

versions (allowed during a show, at a gentler pace) reads the render ID of every scheduled sequence on the chosen
controllers (one small request per file, through the player's proxy, so it
takes a few minutes for a whole fleet) and keeps the result in
work/cache/sequence-versions.json; a copy whose ID differs from the show
folder's .fseq is from an older render.

Usage:
  tools/xlights.py status
  tools/xlights.py push rgb-24-Beetlejuice.fseq --hosts FPP-Tree[,FPP-Roof]
  tools/xlights.py push <name>.fseq --hosts FPP-Tree --render   # re-render first
  tools/xlights.py versions --hosts all|FPP-Tree,FPP-Roof
"""
import argparse
import concurrent.futures
import json
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet  # noqa: E402

XL = "http://127.0.0.1:49913"
# xLights can crash if the client hangs up before it answers, so waits are long.
UPLOAD_TIMEOUT = 3600
RENDER_TIMEOUT = 3600
SKIP_DIRS = {"Backup", "backup"}
VERSIONS = fleet.ROOT / "work/cache/sequence-versions.json"


class XLError(Exception):
    pass


def call(cmd, timeout=30, **params):
    """One automation command. Returns the decoded JSON reply; raises XLError
    with xLights' own message on failure."""
    req = urllib.request.Request(f"{XL}/xlDoAutomation", method="POST",
                                 data=json.dumps({"cmd": cmd, **params}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("msg")
        except ValueError:
            msg = None
        raise XLError(msg or f"HTTP {e.code}")
    except (urllib.error.URLError, ConnectionError) as e:
        raise XLError(f"xLights is not answering on {XL} ({getattr(e, 'reason', e)}): start xLights "
                      "and set Preferences > xFade port to A")


def status():
    """xLights version, show folder and open sequence. Read-only."""
    out = {"version": call("getVersion", timeout=5).get("version")}
    with urllib.request.urlopen(f"{XL}/getShowFolder", timeout=5) as r:
        out["show"] = r.read().decode().strip()
    try:
        out["open"] = call("getOpenSequence", timeout=5).get("seq")
    except XLError:
        out["open"] = None
    return out


def fseq_header(path):
    """Render ID and frame count from a .fseq header (v2: ID at byte 24)."""
    with open(path, "rb") as f:
        h = f.read(32)
    if h[:4] != b"PSEQ" or len(h) < 32:
        raise ValueError(f"{path} is not an FSEQ file")
    return {"id": str(struct.unpack("<Q", h[24:32])[0]) if h[7] >= 2 else None,
            "frames": struct.unpack("<I", h[14:18])[0], "version": f"{h[7]}.{h[6]}"}


def local_sequence(show, name):
    """The .xsq for an FPP sequence name in the show folder, its rendered .fseq,
    and whether the .xsq was saved after the render. None if not found."""
    # Spaces around the name are ignored: FPP drops a leading space that a
    # show-folder file name can have.
    stem = Path(name).stem.strip().lower()
    root = Path(show)
    hits = [p for p in root.rglob("*.xsq")
            if p.stem.strip().lower() == stem and not SKIP_DIRS & set(p.relative_to(root).parts[:-1])]
    if not hits:
        return None
    xsq = min(hits, key=lambda p: len(p.parts))  # the top-level one if there are several
    fseq = xsq.with_suffix(".fseq")
    out = {"xsq": str(xsq), "fseq": str(fseq) if fseq.exists() else None}
    if fseq.exists():
        out.update(fseq_header(fseq))
        # xLights writes the .xsq just before the .fseq when it saves a render.
        out["stale"] = xsq.stat().st_mtime > fseq.stat().st_mtime + 60
    return out


def remote_meta(u, name, wait=60):
    # FPP answers 404 for a few seconds while it moves a fresh upload into place.
    end = time.time() + wait
    while True:
        try:
            return fleet.fetch_json(f"{u['base']}/api/sequence/{urllib.parse.quote(name)}/meta", timeout=20)
        except urllib.error.HTTPError as e:
            if e.code != 404 or time.time() > end:
                raise
            time.sleep(3)


def load_versions():
    try:
        return json.loads(VERSIONS.read_text())
    except (OSError, ValueError):
        return {"units": {}}


def version_report(versions=None, show=None):
    """Each checked controller's copies against the reference render: the show
    folder's .fseq when xLights is available, else the player's copy."""
    versions = versions or load_versions()
    units = versions.get("units", {})
    if show is None:
        try:
            show = status()["show"]
        except Exception:
            show = None
    names = {n for u in units.values() for n in u.get("seqs", {})}
    ref, source = {}, None
    if show:
        source = "show folder"
        for n in names:
            loc = local_sequence(show, n)
            if loc and loc.get("id"):
                ref[n] = loc["id"]
    else:
        player = units.get(versions.get("player") or "", {})
        if player:
            source = f"player ({versions['player']})"
            ref = {n: v["id"] for n, v in player.get("seqs", {}).items() if v.get("id")}
    out = {}
    for host, u in units.items():
        r = {"checked": u.get("checked"), "current": 0, "outdated": [], "errors": [], "unknown": []}
        for n, v in sorted(u.get("seqs", {}).items()):
            if v.get("error"):
                r["errors"].append(f"{n}: {v['error']}")
            elif n not in ref:
                r["unknown"].append(n)
            elif str(v.get("id")) == ref[n]:
                r["current"] += 1
            else:
                r["outdated"].append({"seq": n, "id": v.get("id"), "want": ref[n]})
        out[host] = r
    return {"source": source, "units": out}


def cmd_versions(args):
    _, needed = fleet.scheduled_sequences(args.master)
    wanted = None if args.hosts == "all" else set(args.hosts.split(","))
    units = [u for u in fleet.discover(args.master, fleet.load_inventory())
             if u["online"] and (wanted is None or u["host"] in wanted)]
    missing = sorted((wanted or set()) - {u["host"] for u in units})
    for h in missing:
        print(f"SKIP {h:12} offline or not found")
    if not units:
        sys.exit("no controllers to check")
    try:
        state = fleet.fetch_json(f"http://{args.master}/api/fppd/status").get("status_name")
    except Exception as e:
        sys.exit(f"cannot read the player's status: {e}")
    # Read-only, so it may run during a show, but at half the pace: the player's
    # proxy is also carrying the show's status traffic.
    workers = 4 if state == "idle" else 2
    if state != "idle":
        print(f"The player is {state}: reading at a gentler pace.")

    work = []
    for u in units:
        try:
            have = {f["name"] for f in fleet.fetch_json(f"{u['base']}/api/files/sequences", timeout=15).get("files", [])}
        except Exception as e:
            print(f"FAIL {u['host']:12} cannot list sequences: {e}")
            continue
        work += [(u, n) for n in sorted(needed) if n in have]

    def one(item):
        u, n = item
        try:
            m = fleet.fetch_json(f"{u['base']}/api/sequence/{urllib.parse.quote(n)}/meta", timeout=20)
            return u["host"], n, {"id": str(m.get("ID")), "frames": m.get("NumFrames")}
        except Exception as e:
            return u["host"], n, {"error": str(e)[:80]}

    print(f"Reading {len(work)} sequence headers from {len(units)} controller(s)...", flush=True)
    found = {}
    # 4 at a time: every request goes through the player's proxy (see fleet.scan_fleet).
    with concurrent.futures.ThreadPoolExecutor(workers) as ex:
        for i, (host, n, v) in enumerate(ex.map(one, work), 1):
            found.setdefault(host, {})[n] = v
            print(f"\r  {i}/{len(work)} {host} {n[:50]}", end="", file=sys.stderr, flush=True)
    print("\r", end="", file=sys.stderr, flush=True)

    versions = load_versions()
    player = next((u["host"] for u in units if u.get("is_master")), None)
    if player:
        versions["player"] = player
    now = time.time()
    for u in units:
        if u["host"] in found:
            versions["units"][u["host"]] = {"checked": now, "seqs": found[u["host"]]}
    VERSIONS.parent.mkdir(parents=True, exist_ok=True)
    VERSIONS.write_text(json.dumps(versions, indent=1))

    rep = version_report(versions)
    print(f"Compared with the {rep['source'] or 'nothing (no show folder or player copy to compare with)'}:")
    failed = 0
    for u in units:
        r = rep["units"].get(u["host"])
        if not r:
            continue
        bad = r["outdated"] or r["errors"]
        failed += bool(bad)
        print(f"{'OUTD' if r['outdated'] else 'FAIL' if r['errors'] else 'OK  '} {u['host']:12} {r['current']} current"
              + (f", {len(r['outdated'])} older render: {', '.join(o['seq'] for o in r['outdated'][:5])}" if r["outdated"] else "")
              + (f", {len(r['errors'])} unreadable" if r["errors"] else "")
              + (f", {len(r['unknown'])} not in the reference" if r["unknown"] else ""))
    sys.exit(1 if failed else 0)


def cmd_status(args):
    st = status()
    print(f"xLights {st['version']}, show folder {st['show']}, open sequence: {st['open'] or 'none'}")
    for n in args.names:
        print(f"  {n}: {json.dumps(local_sequence(st['show'], n))}")


def cmd_push(args):
    st = status()
    print(f"xLights {st['version']}, show folder {st['show']}")
    if args.render and st["open"]:
        sys.exit(f"xLights has {st['open']} open; close it first (rendering would replace it)")
    loc = local_sequence(st["show"], args.name)
    if not loc:
        sys.exit(f"no .xsq for {args.name} in {st['show']}")
    if args.render:
        print(f"Rendering {Path(loc['xsq']).name} (this can take a few minutes)...", flush=True)
        t = time.time()
        call("batchRender", timeout=RENDER_TIMEOUT, seqs=[loc["xsq"]], promptIssues="false")
        loc = local_sequence(st["show"], args.name)
        print(f"  rendered in {time.time() - t:.0f} s, ID {loc.get('id')}")
    if not loc.get("fseq"):
        sys.exit(f"{Path(loc['xsq']).name} has no rendered .fseq next to it; render it in xLights (or use --render)")
    if loc["stale"]:
        sys.exit(f"{Path(loc['xsq']).name} was saved after its .fseq was rendered; render it first (--render)")
    print(f"Local {Path(loc['fseq']).name}: FSEQ v{loc['version']}, {loc['frames']} frames, ID {loc['id']}")

    units = {u["host"]: u for u in fleet.discover(args.master, fleet.load_inventory())}
    player = next((u for u in units.values() if u.get("is_master") and u["online"]), None)
    try:
        state = fleet.fetch_json(f"http://{args.master}/api/fppd/status").get("status_name")
    except Exception as e:
        sys.exit(f"cannot read the player's status: {e}")
    if state != "idle":
        sys.exit(f"the player is {state}; not uploading during a show")

    failed = 0
    for host in args.hosts.split(","):
        u = units.get(host)
        if not u or not u["online"]:
            print(f"FAIL {host:12} {'not found' if not u else 'offline'}")
            failed += 1
            continue
        is_player = u is player
        print(f"     {host:12} uploading to {u['ip']} (v2 zstd sparse{', with audio' if is_player else ''})...", flush=True)
        t = time.time()
        try:
            call("uploadSequence", timeout=UPLOAD_TIMEOUT, ip=u["ip"], format="v2stdsparse",
                 media="true" if is_player else "false", seq=loc["xsq"])
        except XLError as e:
            hint = (" (xLights could not find it: open FPP Connect once so it discovers the player and its remotes)"
                    if "not found" in str(e).lower() else "")
            print(f"FAIL {host:12} {e}{hint}")
            failed += 1
            continue
        try:
            m = remote_meta(u, args.name)
        except Exception as e:
            print(f"FAIL {host:12} uploaded in {time.time() - t:.0f} s, but cannot read it back: {e}")
            failed += 1
            continue
        ok = str(m.get("ID")) == loc["id"] and m.get("NumFrames") == loc["frames"]
        ranges = ", ".join(f"{r['Start'] + 1}-{r['Start'] + r['Length']}" for r in m.get("Ranges") or [])
        print(f"{'OK  ' if ok else 'FAIL'} {host:12} {time.time() - t:.0f} s; on the controller: ID {m.get('ID')}, "
              f"{m.get('NumFrames')} frames, {m.get('ChannelCount')} channels ({ranges}), {m.get('CompressionTypeString')}")
        failed += not ok
    sys.exit(1 if failed else 0)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--master", default=fleet.load_settings()["player"] or None,
                    help="the player's address (default: config/settings.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status", help="xLights version, show folder, and where named sequences are (read-only)")
    s.add_argument("names", nargs="*")
    p = sub.add_parser("push", help="upload one sequence to the named controllers")
    p.add_argument("name", help="sequence file name as FPP knows it, e.g. song.fseq")
    p.add_argument("--hosts", required=True, help="comma-separated hostnames")
    p.add_argument("--render", action="store_true", help="render it in xLights first")
    v = sub.add_parser("versions", help="read the render ID of every scheduled sequence on controllers (read-only)")
    v.add_argument("--hosts", required=True, help='comma-separated hostnames, or "all"')
    args = ap.parse_args()
    if args.cmd in ("push", "versions") and not args.master:
        sys.exit("no player configured: pass --master <ip> or set it on the UI's Settings page")
    try:
        {"status": cmd_status, "push": cmd_push, "versions": cmd_versions}[args.cmd](args)
    except XLError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
