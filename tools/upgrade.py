#!/usr/bin/env python3
"""Upgrade controllers in place to a newer FPP release, one at a time, verified.

The in-place counterpart of Build card / Flash card (which stay the safety net
and the way to a fresh install). Two paths, the same ones FPP's About page uses:

  fpp      FPP's own update (upgradefpp.php -> scripts/upgrade_FPP): the
           controller fetches the release from GitHub and rebuilds FPP, then
           reboots. Needs the controller to reach GitHub.
  os       FPP's OS-level upgrade (upgradeOS.php) with the release's .fppos
           image for the board: the root filesystem is replaced, FPP reboots.
  os-push  The same, for a controller that cannot reach GitHub: this Mac
           downloads the image (checked against GitHub's SHA-256), uploads it
           through the player, applies it, and deletes it afterwards.

Each controller in turn: refuse while the show plays or is about to; record a
"before" picture; start the upgrade; follow FPP's progress (updateActivity),
failing on errors, a stall or a timeout; reboot and wait for it; compare with
the "before" picture. The first failure stops the run, so a bad release can
take down only one controller. The player goes last: the others are reached
through it.

Usage:
  tools/upgrade.py plan [--target 10.4] [--hosts a,b] [--json]   (read-only)
  tools/upgrade.py run --hosts a,b [--target 10.4] [--os] [--dry-run]
  tools/upgrade.py images [--json]                     (read-only)
  tools/upgrade.py cleanup --hosts a,b

--dry-run does the checks, the "before" picture and the comparison without
starting anything: read-only.

images lists the .fppos OS images left in each controller's Uploads folder
(FPP keeps them after an OS upgrade; each is 1-1.3 GB); cleanup deletes them,
skipping a controller while an update runs there.
"""
import argparse
import concurrent.futures
import hashlib
import json
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import colorlight  # noqa: E402
import fleet  # noqa: E402
import fpp_http  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOTS = ROOT / "snapshots"
FPPOS_DIR = ROOT / "work/images/fppos"
BACKUP_MAX_AGE = 24 * 3600

# Seconds. FPP's own estimates: an update 5-10 min (20-30 on a slow board), an
# OS upgrade "a LONG time"; the limits are generous so only a stuck run trips them.
LIMITS = {"start": 180, "run_fpp": 60 * 60, "run_os": 120 * 60, "stall": 20 * 60, "reboot_fpp": 20 * 60,
          "reboot_os": 45 * 60, "fppd": 5 * 60, "poll": 10, "show_margin": 15 * 60}
# Minutes a controller is out, by method and board family, for the show-time check and the UI.
ESTIMATE = {"fpp": {"Pi": 12, "Pi64": 12, "BB64": 20, "BBB": 35},
            "os": {"Pi": 35, "Pi64": 35, "BB64": 40, "BBB": 50}}
# Output configuration that must survive an upgrade unchanged.
OUTPUT_FILES = ["channelOutputsJSON", "co-pixelStrings", "co-bbbStrings", "co-other", "universeOutputs", "co-pwm"]


def say(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- release

def release(target=None):
    """The release to upgrade to (the newest by default): tag, date, notes,
    .fppos assets, and what its notes say about the OS level."""
    cache = {}
    try:
        cache = json.loads(fleet.UPDATE_CACHE.read_text())
    except (FileNotFoundError, ValueError):
        pass
    if target:
        r = fleet._github(f"releases/tags/{urllib.parse.quote(target)}", cache)
    else:
        rels = [r for r in fleet._github("releases?per_page=15", cache) if not r["draft"] and not r["prerelease"]]
        r = max(rels, key=lambda r: fleet._vkey(r["tag_name"]))
    fleet.UPDATE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    fleet.UPDATE_CACHE.write_text(json.dumps(cache))
    assets = {a["name"]: {"url": a["browser_download_url"], "size": a["size"],
                          "sha256": (a.get("digest") or "").removeprefix("sha256:") or None}
              for a in r.get("assets", []) if a["name"].endswith(".fppos")}
    body = r.get("body") or ""
    return {"tag": r["tag_name"], "published": r.get("published_at"), "url": r.get("html_url"),
            "assets": assets, "os": os_advice(body), "notes_head": notes_head(body)}


def os_advice(body):
    """What the release notes say about the OS level: the new level, and
    whether an OS upgrade is required to run the release (None: not said)."""
    to = re.search(r"OS level moves from \**([\w.-]+)\** to \**([\w.-]+)\**", body, re.I)
    flat = re.sub(r"[*_`\"]", "", body)
    not_required = re.search(r"OS Level upgrade is not required", flat, re.I)
    required = re.search(r"OS Level upgrade is required", flat, re.I)
    note = next((p for p in re.split(r"\n\s*\n", flat) if re.search(r"OS Level", p, re.I)), "")
    return {"from": to.group(1) if to else None, "level": to.group(2) if to else None,
            # "required" in these notes is about older majors (9.x); the sentence about
            # this release is the "not required" one when both appear
            "required": False if not_required else True if required else None,
            "note": re.sub(r"^\s*NOTE:\s*", "", note.strip())[:700]}


def notes_head(body):
    """The release notes' opening paragraph, for the UI."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip() and not p.lstrip().startswith("#")]
    return re.sub(r"\*\*", "", paras[0])[:900] if paras else ""


def board_family(info):
    """The .fppos family for a controller. A Pi keeps its architecture: an
    in-place OS upgrade cannot move a 32-bit system to 64-bit."""
    text = f"{info.get('Platform')} {info.get('SubPlatform') or info.get('Variant')}".lower()
    if "pocketbeagle2" in text.replace(" ", "") or "beaglebone 64" in text or "beagley" in text:
        return "BB64"
    if "beaglebone" in text or "pocketbeagle" in text:
        return "BBB"
    if "raspberry" in text:
        return "Pi64" if "64bit" in (info.get("OSVersion") or "").replace("-", "").replace(" ", "") else "Pi"
    return None


def fppos_for(family, assets):
    return next((n for n in sorted(assets) if family and n.startswith(family + "-")), None)


def os_level(os_version):
    m = re.search(r"(\d{4}-\d\d[a-z]?)", os_version or "")
    return m.group(1) if m else None


# ---------------------------------------------------------------- plan

def last_backup(host):
    """Epoch of the newest snapshot of a controller, or None."""
    best = None
    for m in (SNAPSHOTS / host).glob("*/manifest.json") if name_ok(host) else []:
        try:
            t = datetime.strptime(json.loads(m.read_text())["taken"], "%Y%m%d-%H%M%S").timestamp()
        except (ValueError, KeyError):
            continue
        best = max(best or t, t)
    return best


def name_ok(host):
    return bool(re.match(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$", host or ""))


def show_window(master):
    """(playing, seconds until the next scheduled playlist or None, its name)."""
    st = fleet.fetch_json(f"http://{master}/api/fppd/status", timeout=8)
    nxt = (st.get("scheduler") or {}).get("nextPlaylist") or {}
    start = nxt.get("scheduledStartTime")
    return (st.get("status_name") == "playing", start - time.time() if start else None, nxt.get("playlistName"))


def unit_plan(u, rel, now):
    """Can this controller be upgraded in place, and how. Read-only."""
    p = {"host": u["host"], "ip": u["ip"], "via": u.get("via"), "online": u["online"], "role": "player" if u.get("is_master") else "remote",
         "blockers": [], "notes": []}
    if not u["online"]:
        p["blockers"].append("needs its UI password" if u.get("needs_password") else "offline")
        return p
    base = u["base"]
    info = fleet.fetch_json(f"{base}/api/system/info", timeout=10)
    fam = board_family(info)
    root = ((info.get("Utilization") or {}).get("Disk") or {}).get("Root") or {}
    p.update(version=info.get("Version"), platform=info.get("SubPlatform") or info.get("Platform"), family=fam,
             os_level=os_level(info.get("OSVersion")), arch="64-bit" if "64bit" in (info.get("OSVersion") or "") else "32-bit",
             root_free=root.get("Free"))
    if fleet._vkey(p["version"]) >= fleet._vkey(rel["tag"]):
        p["blockers"].append(f"already on {p['version']}")
        p["current"] = True
    try:
        st = fleet.fetch_json(f"{base}/api/fppd/status", timeout=8)
        if st.get("status_name") == "playing":
            p["blockers"].append("playing")
    except Exception:
        p["notes"].append("status unreadable")
    try:
        act = fleet.fetch_json(f"{base}/api/system/updateActivity", timeout=8)
        if act.get("inProgress"):
            p["blockers"].append(f"an update is already running ({act.get('kind')}: {act.get('stage')})")
    except Exception:
        pass
    # GitHub access, as the controller itself last saw it. FPP checks from the
    # controller; checked=false means its last fetch failed.
    try:
        upd = fleet.fetch_json(f"{base}/api/system/updateStatus", timeout=12, tries=1)
        p["internet"] = bool(upd.get("checked")) and not upd.get("versionUnknown")
        p["major"] = bool(upd.get("isMajorVersionUpgrade"))
    except Exception:
        p["internet"] = False
        p["notes"].append("couldn't confirm GitHub access (its update check timed out)")
    p["fppos"] = fppos_for(fam, rel["assets"])
    size = rel["assets"].get(p["fppos"], {}).get("size") if p["fppos"] else None
    p["method"] = "fpp" if p["internet"] and not p.get("major") else "os" if p["internet"] else "os-push"
    if p["method"] != "fpp":
        if not p["fppos"]:
            p["blockers"].append(f"no {rel['tag']} OS image for this board ({fam or 'unknown'})")
        elif root.get("Free") is not None and root["Free"] < 2 * size:
            p["blockers"].append(f"needs {2 * size / 1e9:.1f} GB free for the OS image, has {root['Free'] / 1e9:.1f} GB")
    p["minutes"] = ESTIMATE["fpp" if p["method"] == "fpp" else "os"].get(fam, 40)
    try:
        ups = fleet.fetch_json(f"{base}/api/files/Uploads", timeout=10).get("files", [])
        old = [f for f in ups if f["name"].endswith(".fppos")]
        if old:
            p["notes"].append(f"{len(old)} old OS image(s) in Uploads ({sum(f.get('sizeBytes', 0) for f in old) / 1e9:.1f} GB): "
                              + ", ".join(f["name"] for f in old))
    except Exception:
        pass
    try:
        p["colorlight"] = bool(colorlight.outputs(base))
    except Exception:
        p["colorlight"] = False
    b = last_backup(u["host"])
    p["backup_age_h"] = round((now - b) / 3600, 1) if b else None
    p["backup_due"] = b is None or now - b > BACKUP_MAX_AGE
    return p


def plan(master, target=None, hosts=None):
    """Everything the UI needs to offer an upgrade. Read-only."""
    rel = release(target)
    units = fleet.discover(master, fleet.load_inventory(), hosts=hosts)
    now = time.time()
    with concurrent.futures.ThreadPoolExecutor(3) as ex:  # the player's proxy limit
        rows = list(ex.map(lambda u: _safe_plan(u, rel, now), units))
    try:
        playing, until, nxt = show_window(master)
    except Exception:
        playing, until, nxt = None, None, None
    return {"release": {k: rel[k] for k in ("tag", "published", "url", "os", "notes_head")},
            "units": rows, "show": {"playing": playing, "next_in_s": until, "next": nxt}, "checked": now}


def _safe_plan(u, rel, now):
    try:
        return unit_plan(u, rel, now)
    except Exception as e:
        return {"host": u["host"], "ip": u["ip"], "online": u["online"], "blockers": [f"unreadable: {str(e)[:80]}"], "notes": []}


# ---------------------------------------------------------------- old OS images

def uploads_images(u):
    return [{"name": f["name"], "size": f.get("sizeBytes") or 0}
            for f in fleet.fetch_json(f"{u['base']}/api/files/Uploads", timeout=15).get("files", [])
            if f["name"].endswith(".fppos")]


def busy(u):
    try:
        return bool(activity(u).get("inProgress"))
    except Exception:
        return True  # unknown: leave its images alone


def images(master, hosts=None):
    """Every controller's leftover OS images, and whether an update is running
    there (an image may then be in use). Read-only."""
    units = fleet.discover(master, fleet.load_inventory(), hosts=hosts)

    def one(u):
        row = {"host": u["host"], "ip": u["ip"], "via": u.get("via"), "online": u["online"], "images": [], "busy": False}
        if u["online"]:
            try:
                row["images"], row["busy"] = uploads_images(u), busy(u)
            except Exception as e:
                row["error"] = str(e)[:80]
        return row

    with concurrent.futures.ThreadPoolExecutor(3) as ex:
        rows = list(ex.map(one, units))
    return {"units": rows, "total": sum(i["size"] for r in rows for i in r["images"]), "checked": time.time()}


def cleanup(master, hosts):
    """Delete the OS images from these controllers' Uploads folders."""
    units = {u["host"]: u for u in fleet.discover(master, fleet.load_inventory(), hosts=set(hosts))}
    freed, failed = 0, 0
    for h in hosts:
        u = units.get(h)
        if not u or not u["online"]:
            say(f"{h}: offline, skipped")
            continue
        if busy(u):
            say(f"{h}: an update is running (or its state is unknown), skipped")
            continue
        imgs = uploads_images(u)
        if not imgs:
            say(f"{h}: no OS images")
            continue
        for i in imgs:
            try:
                fpp_http.request(f"{u['base']}/api/file/Uploads/{urllib.parse.quote(i['name'])}", timeout=30, method="DELETE")
            except Exception as e:
                say(f"{h}: could not delete {i['name']} ({str(e)[:80]})")
                failed += 1
        left = {x["name"] for x in uploads_images(u)}
        gone = [i for i in imgs if i["name"] not in left]
        freed += sum(i["size"] for i in gone)
        say(f"{h}: deleted {len(gone)} of {len(imgs)} ({sum(i['size'] for i in gone) / 1e9:.1f} GB)"
            + (f"; still there: {', '.join(sorted(left & {i['name'] for i in imgs}))}" if len(gone) < len(imgs) else ""))
    say(f"Freed {freed / 1e9:.1f} GB.")
    return 1 if failed else 0


# ---------------------------------------------------------------- before / after

def capture(u):
    """What an upgrade must not change (or must change: the version)."""
    b = u["base"]
    info = fleet.fetch_json(f"{b}/api/system/info", timeout=15)
    st = fleet.fetch_json(f"{b}/api/fppd/status", timeout=15)
    d = {"host": info.get("HostName"), "version": info.get("Version"), "branch": info.get("Branch"),
         "mode": info.get("Mode"), "os_level": os_level(info.get("OSVersion")), "fppd": st.get("fppd"),
         "uptime": st.get("systemUptimeTotalSeconds"), "outputs": {}}
    for f in OUTPUT_FILES:
        try:
            d["outputs"][f] = fleet.fetch_json(f"{b}/api/channel/output/{f}", timeout=15)
        except urllib.error.HTTPError as e:
            d["outputs"][f] = None if e.code == 404 else f"error {e.code}"
    d["sequences"] = sorted(f["name"] for f in fleet.fetch_json(f"{b}/api/files/sequences", timeout=20).get("files", []))
    d["playlists"] = sorted(fleet.fetch_json(f"{b}/api/playlists", timeout=15) or [])
    d["ipv4"] = sorted(a["local"] for i in fleet.fetch_json(f"{b}/api/network/interface", timeout=15)
                       for a in i.get("addr_info", []) if a.get("family") == "inet" and i.get("ifname") != "lo")
    try:
        d["gateway"] = fleet.fetch_json(f"{b}/api/network/gateway", timeout=10).get("GATEWAY") or ""
    except Exception:
        d["gateway"] = None
    try:
        d["warnings"] = sorted(str(w) for w in fleet.fetch_json(f"{b}/api/fppd/warnings", timeout=10))
    except Exception:
        d["warnings"] = []
    d["settings"] = {k: v["value"] for k, v in fleet.scan_unit({**u, "online": True}).items()}
    d["colorlight"] = bool(colorlight.outputs(b))
    return d


def colorlight_found(u):
    """Whether fppd found the ColorLight receiver at its latest start."""
    text = colorlight.tail(u["base"], "Logs/fppd.log", 4000)
    ev = colorlight.parse_fppd(text, 0)[0]
    starts = [i for i, e in enumerate(ev) if e["k"] == "init"]
    after = ev[starts[-1]:] if starts else []
    return any(e["k"] == "found" and e.get("v") for e in after), bool(starts)


def verify(u, pre, post, target):
    """[(PASS|WARN|FAIL, check, detail)] comparing after with before."""
    out = []

    def check(level_if_bad, name, ok, detail=""):
        out.append(("PASS" if ok else level_if_bad, name, detail))

    check("FAIL", "version", str(post["version"]).startswith(target), f"{pre['version']} -> {post['version']}")
    check("FAIL", "hostname", post["host"] == pre["host"], post["host"])
    check("FAIL", "fppd running", post["fppd"] == "running", post["fppd"])
    check("FAIL", "mode", post["mode"] == pre["mode"], f"{pre['mode']} -> {post['mode']}")
    check("FAIL", "addresses", set(pre["ipv4"]) <= set(post["ipv4"]),
          ", ".join(sorted(set(pre["ipv4"]) - set(post["ipv4"]))) and "lost " + ", ".join(sorted(set(pre["ipv4"]) - set(post["ipv4"]))))
    check("FAIL", "gateway", pre["gateway"] is None or post["gateway"] == pre["gateway"], f"{pre['gateway']} -> {post['gateway']}")
    changed = [f for f in OUTPUT_FILES if pre["outputs"].get(f) != post["outputs"].get(f)]
    check("FAIL", "output configuration", not changed, "changed: " + ", ".join(changed) if changed else "")
    lost = sorted(set(pre["sequences"]) - set(post["sequences"]))
    check("FAIL", "sequences", not lost, f"{len(post['sequences'])} files" + (f"; missing {', '.join(lost[:5])}" if lost else ""))
    lost = sorted(set(pre["playlists"]) - set(post["playlists"]))
    check("FAIL", "playlists", not lost, f"missing {', '.join(lost)}" if lost else f"{len(post['playlists'])}")
    diff = sorted(k for k in pre["settings"] if k in post["settings"] and post["settings"][k] != pre["settings"][k])
    check("WARN", "settings", not diff, "changed: " + ", ".join(diff[:12]) if diff else f"{len(post['settings'])} unchanged")
    new = sorted(set(post["warnings"]) - set(pre["warnings"]))
    check("WARN", "FPP warnings", not new, "; ".join(new)[:300] if new else "no new warnings")
    if pre["colorlight"]:
        found, started = colorlight_found(u)
        check("FAIL" if started else "WARN", "ColorLight receiver", found, "found at fppd's latest start" if found else
              "not found at fppd start: check the link, then restart fppd" if started else "fppd's start is no longer in the log")
    return out


# ---------------------------------------------------------------- running

def activity(u):
    return fleet.fetch_json(f"{u['base']}/api/system/updateActivity", timeout=10, tries=1)


# Lines of FPP's upgrade output worth showing: its "===== stage =====" markers and
# anything that reads like a problem. The rest (an OS upgrade lists every file
# it copies: ~50,000 lines) is counted, and the last lines are kept for a failure.
SHOW = re.compile(r"=====|error|fail|warn|abort|not enough|cannot|could not|complete|reboot|upgrade to|image:", re.I)


def stream(u, path, label):
    """Start one of FPP's upgrade pages and echo its notable output as it
    arrives. It runs on in its own thread: FPP finishes the upgrade even if the
    connection drops (the player's proxy may cut a long request), and progress
    is followed through updateActivity regardless."""
    state = {"error": None, "lines": [], "hidden": 0}

    def run():
        try:
            with fpp_http.open_url(f"{u['base']}/{path}", timeout=LIMITS["run_os"], method="POST", data=b"",
                                   headers={"Content-Type": "application/x-www-form-urlencoded"}) as r:
                buf = b""
                while True:
                    chunk = r.read1(4096) if hasattr(r, "read1") else r.read(4096)
                    if not chunk:
                        break
                    buf += chunk
                    *lines, buf = re.split(rb"\r?\n|\r", buf)
                    for ln in lines:
                        text = ln.decode("utf-8", "replace").rstrip()
                        if not text:
                            continue
                        state["lines"] = (state["lines"] + [text])[-40:]
                        if SHOW.search(text):
                            say(f"    | {text}")
                        else:
                            state["hidden"] += 1
        except urllib.error.HTTPError as e:
            state["error"] = f"{label}: HTTP {e.code}" + (" (another update is already running)" if e.code == 409 else "")
        except Exception as e:  # a dropped connection: the upgrade carries on
            state["error"] = f"{label}: connection ended ({str(e)[:80]})"

    threading.Thread(target=run, daemon=True).start()
    return state


def tail_lines(out):
    if out["lines"]:
        say(f"  last lines of FPP's output ({out['hidden']} routine lines were not shown):")
        for ln in out["lines"]:
            say(f"    | {ln}")


def follow(u, before, method, out):
    """Follow FPP's progress until the run ends. Returns 'done', 'rebooting'
    (an OS upgrade restarts by itself) or raises with the reason."""
    t0 = time.time()
    limit = LIMITS["run_fpp" if method == "fpp" else "run_os"]
    started, last_stage, last_size, last_move, misses = False, None, None, t0, 0
    while True:
        time.sleep(LIMITS["poll"])
        now = time.time()
        try:
            a = activity(u)
            misses = 0
        except Exception:
            misses += 1
            if method != "fpp" and started:
                return "rebooting"
            if misses >= 6:
                raise RuntimeError("the controller stopped answering during the upgrade")
            continue
        if not started:
            if a.get("runId") and a.get("runId") != before.get("runId") or a.get("inProgress"):
                started = True
                last_move = now
            elif now - t0 > LIMITS["start"]:
                raise RuntimeError("the upgrade did not start" + (f": {out['error']}" if out["error"] else ""))
            continue
        if a.get("stage") != last_stage:
            last_stage = a.get("stage")
            say(f"  stage: {last_stage or '(starting)'}  [{int((now - t0) / 60)} min]")
            last_move = now
        if a.get("logSize") != last_size:
            last_size, last_move = a.get("logSize"), now
        if method != "fpp" and re.search(r"reboot", a.get("stage") or "", re.I):
            return "rebooting"
        if not a.get("inProgress"):
            if a.get("failed") or re.search(r"fail", a.get("stage") or "", re.I):
                tail_lines(out)
                raise RuntimeError(f"FPP reports the upgrade failed ({a.get('stage')}); see the output above")
            return "done"
        if now - last_move > LIMITS["stall"]:
            tail_lines(out)
            raise RuntimeError(f"no progress for {LIMITS['stall'] // 60} minutes (stage: {last_stage})")
        if now - t0 > limit:
            raise RuntimeError(f"still running after {limit // 60} minutes (stage: {last_stage})")


def wait_restart(u, since, limit, method):
    """Wait until the controller has rebooted (its uptime restarted) and fppd runs."""
    t0 = time.time()
    say(f"  waiting for {u['host']} to restart (up to {limit // 60} min)")
    back = None
    while time.time() - t0 < limit:
        time.sleep(LIMITS["poll"])
        try:
            st = fleet.fetch_json(f"{u['base']}/api/fppd/status", timeout=8, tries=1)
        except Exception:
            print(f"\r  restarting… {int(time.time() - t0)} s", end="", flush=True)
            continue
        up = st.get("systemUptimeTotalSeconds")
        if up is not None and up < time.time() - since:
            if back is None:
                back = time.time()
                say(f"\n  back after {int(back - t0)} s (uptime {up} s)")
            if st.get("fppd") == "running":
                return
            if time.time() - back > LIMITS["fppd"]:
                raise RuntimeError(f"it restarted but fppd is {st.get('fppd')} after {LIMITS['fppd'] // 60} min")
    hint = " An OS upgrade sometimes needs a power cycle to finish its reboot (FPP's release notes)." if method != "fpp" else ""
    raise RuntimeError(f"it did not come back within {limit // 60} minutes.{hint}")


def fetch_fppos(name, asset):
    """The .fppos image on this Mac, downloaded once and checked against GitHub's SHA-256."""
    FPPOS_DIR.mkdir(parents=True, exist_ok=True)
    path = FPPOS_DIR / name
    if path.exists() and path.stat().st_size == asset["size"] and (not asset["sha256"] or sha256(path) == asset["sha256"]):
        say(f"  {name}: already downloaded, checksum OK")
        return path
    import get_image
    say(f"  downloading {name} ({asset['size'] / 1e9:.2f} GB)")
    get_image.download(asset["url"], path, asset["size"], asset["sha256"])
    return path


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


class Multipart:
    """A multipart/form-data body read from disk as it is sent, so a 1 GB
    image is never held in memory."""

    def __init__(self, path, field="myfile"):
        self.boundary = secrets.token_hex(16)
        self.head = (f'--{self.boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{path.name}"\r\n'
                     "Content-Type: application/octet-stream\r\n\r\n").encode()
        self.tail = f"\r\n--{self.boundary}--\r\n".encode()
        self.size = path.stat().st_size
        self.length = len(self.head) + self.size + len(self.tail)
        self.f = open(path, "rb")
        self.parts, self.sent, self.t0, self.shown = [self.head], 0, time.time(), 0

    def read(self, n=-1):
        n = 1 << 20 if n is None or n < 0 else n
        if self.parts:
            return self.parts.pop()
        chunk = self.f.read(n) if self.f else b""
        if chunk:
            self.sent += len(chunk)
            if time.time() - self.shown > 0.5 or self.sent == self.size:
                self.shown = time.time()
                rate = self.sent / max(1, self.shown - self.t0) / 1e6
                print(f"\r  uploading: {self.sent / 1e6:6.0f} / {self.size / 1e6:.0f} MB  {rate:4.1f} MB/s", end="", flush=True)
            return chunk
        if self.f:
            self.f.close()
            self.f = None
            return self.tail
        return b""


def upload(u, path):
    body = Multipart(path)
    with fpp_http.open_url(f"{u['base']}/jqupload.php", timeout=3600, method="POST", data=body,
                           headers={"Content-Type": f"multipart/form-data; boundary={body.boundary}",
                                    "Content-Length": str(body.length)}) as r:
        r.read()
    say("")
    files = {f["name"]: f for f in fleet.fetch_json(f"{u['base']}/api/files/Uploads", timeout=15).get("files", [])}
    got = files.get(path.name, {}).get("sizeBytes")
    if got != body.size:
        raise RuntimeError(f"the upload arrived as {got} bytes, expected {body.size}")
    say(f"  uploaded {path.name}: size matches")


def remove_upload(u, name):
    try:
        fpp_http.request(f"{u['base']}/api/file/Uploads/{urllib.parse.quote(name)}", timeout=30, method="DELETE")
        left = [f["name"] for f in fleet.fetch_json(f"{u['base']}/api/files/Uploads", timeout=15).get("files", [])]
        say(f"  removed {name} from Uploads" if name not in left else f"  WARN: {name} is still in Uploads")
    except Exception as e:
        say(f"  WARN: could not remove {name} from Uploads ({str(e)[:80]}): delete it in File Manager > Uploads")


def guard_show(master, minutes):
    playing, until, nxt = show_window(master)
    if playing:
        raise RuntimeError("the show is playing: upgrades wait until it stops")
    if until is not None and until < minutes * 60 + LIMITS["show_margin"]:
        raise RuntimeError(f"{nxt} starts in {int(until // 60)} min; this upgrade takes about {minutes} min")


def run_unit(u, p, rel, master, use_os, dry):
    method = p["method"] if not (use_os and p["method"] == "fpp") else "os"
    target = rel["tag"]
    if method != "fpp" and not p.get("fppos"):
        raise RuntimeError(f"{target} has no OS image for this board ({p.get('family') or 'unknown'}): use the FPP update")
    say(f"\n=== {u['host']} ({p.get('platform')}, {p['version']} -> {target}, {method}) ===")
    guard_show(master, p["minutes"])
    say("  recording the current state (outputs, content, network, settings)…")
    pre = capture(u)
    if dry:
        say("  dry run: nothing started; comparing the state with itself")
        post = pre
    else:
        image = None
        if method == "os-push":
            image = fetch_fppos(p["fppos"], rel["assets"][p["fppos"]])
            upload(u, image)
            guard_show(master, p["minutes"])
        before = activity(u)
        since = time.time()
        if method == "fpp":
            out = stream(u, f"upgradefpp.php?wrapped=1&version={urllib.parse.quote(target)}", "upgrade")
        else:
            src = p["fppos"] if method == "os-push" else rel["assets"][p["fppos"]]["url"]
            out = stream(u, f"upgradeOS.php?wrapped=1&os={urllib.parse.quote(src, safe='')}", "OS upgrade")
        result = follow(u, before, method, out)
        if result == "done" and method == "fpp":
            say("  upgrade complete; rebooting")
            fpp_http.request(f"{u['base']}/api/system/reboot", timeout=15, method="POST", data=b"")
        wait_restart(u, since, LIMITS["reboot_fpp" if method == "fpp" else "reboot_os"], method)
        post = capture(u)
        if method != "fpp":  # FPP leaves the image in Uploads whether it downloaded it or we sent it
            remove_upload(u, p["fppos"])
    results = verify(u, pre, post, target if not dry else str(pre["version"]))
    for level, name, detail in results:
        say(f"  {level}  {name}{': ' + detail if detail else ''}")
    failed = [r for r in results if r[0] == "FAIL"]
    if failed:
        raise RuntimeError("verification failed: " + ", ".join(r[1] for r in failed))
    say(f"  {u['host']}: OK" + (f" ({sum(r[0] == 'WARN' for r in results)} warning(s))" if any(r[0] == "WARN" for r in results) else ""))


def run(master, hosts, target=None, use_os=False, dry=False):
    rel = release(target)
    units = {u["host"]: u for u in fleet.discover(master, fleet.load_inventory(), hosts=set(hosts))}
    missing = [h for h in hosts if h not in units]
    if missing:
        raise SystemExit(f"unknown controller(s): {', '.join(missing)}")
    order = sorted(hosts, key=lambda h: (units[h].get("is_master", False), hosts.index(h)))  # the player last
    adv = rel["os"]
    say(f"FPP {rel['tag']}: {len(order)} controller(s), one at a time: {', '.join(order)}")
    if adv["level"]:
        say(f"OS level {adv['level']}: " + ("required" if adv["required"] else "optional per the release notes" if adv["required"] is False else "not stated"))
    done = []
    for h in order:
        u = units[h]
        p = unit_plan(u, rel, time.time())
        blockers = [b for b in p["blockers"] if not (dry and b.startswith("already on"))]
        try:
            if blockers:
                raise RuntimeError("; ".join(blockers))
            run_unit(u, p, rel, master, use_os, dry)
            done.append(h)
        except (Exception, SystemExit) as e:  # get_image.download exits on a bad checksum
            say(f"\nSTOPPED at {h}: {e}")
            rest = [x for x in order if x not in done and x != h]
            if rest:
                say(f"Not started: {', '.join(rest)}")
            say(f"Upgraded: {', '.join(done) or 'none'}")
            return 1
    say(f"\nAll {len(done)} controller(s) {'checked (dry run)' if dry else 'upgraded to ' + rel['tag'] + ' and verified'}.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "run", "images", "cleanup"):
        s = sub.add_parser(name)
        s.add_argument("--master", default=fleet.load_settings()["player"])
        s.add_argument("--target", help="release tag (default: the newest)")
        s.add_argument("--hosts", required=name in ("run", "cleanup"), help="comma-separated controller names")
    sub.choices["plan"].add_argument("--json", action="store_true")
    sub.choices["images"].add_argument("--json", action="store_true")
    sub.choices["run"].add_argument("--os", action="store_true", help="OS-level upgrade even where the FPP update would do")
    sub.choices["run"].add_argument("--dry-run", action="store_true", help="checks and comparison only; starts nothing")
    args = ap.parse_args()
    if not args.master:
        sys.exit("no player: pass --master or set it on the Settings page")
    hosts = [h for h in (args.hosts or "").split(",") if h] or None
    if hosts and not all(name_ok(h) for h in hosts):
        sys.exit("bad controller name")
    if args.cmd == "images":
        r = images(args.master, set(hosts) if hosts else None)
        if args.json:
            print(json.dumps(r, indent=1))
            return
        for u in r["units"]:
            if u["images"]:
                print(f"  {u['host']:<12} {sum(i['size'] for i in u['images']) / 1e9:4.1f} GB  "
                      + ", ".join(i["name"] for i in u["images"]) + ("  (update running)" if u["busy"] else ""))
        print(f"Total {r['total'] / 1e9:.1f} GB")
        return
    if args.cmd == "cleanup":
        sys.exit(cleanup(args.master, hosts))
    if args.cmd == "plan":
        p = plan(args.master, args.target, set(hosts) if hosts else None)
        if args.json:
            print(json.dumps(p, indent=1))
            return
        r = p["release"]
        print(f"FPP {r['tag']} ({(r['published'] or '')[:10]}). OS level {r['os']['level']}: "
              + {True: "required", False: "optional", None: "not stated"}[r["os"]["required"]])
        for u in p["units"]:
            state = "BLOCKED: " + "; ".join(u["blockers"]) if u["blockers"] else f"{u.get('method')} (~{u.get('minutes')} min)"
            bk = "never" if u.get("backup_age_h") is None else f"{u['backup_age_h']} h ago"
            print(f"  {u['host']:<12} {u.get('version', ''):<6} {state}  backup {bk}" + "".join(f"\n      {n}" for n in u.get("notes", [])))
        return
    sys.exit(run(args.master, hosts, args.target, args.os, args.dry_run))


if __name__ == "__main__":
    main()
