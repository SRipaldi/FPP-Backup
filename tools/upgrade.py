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
import collections
import concurrent.futures
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
PRE_DIR = ROOT / "work/upgrade/pre"     # the pre-upgrade state, kept so `recheck` can compare after a power cycle
BACKUP_MAX_AGE = 24 * 3600
# FPP's own warning after an OS-level upgrade: the root filesystem was replaced, so each plugin's installed
# system pieces are gone (its files on persistent media remain). Expected, and cleared by reinstalling in the
# Plugin Manager. Flagged as its own check, not a surprise warning, after an OS upgrade.
PLUGINS_WARNING = "Plugins must be reinstalled"


class NotReturned(RuntimeError):
    """The controller did not rejoin the network after its upgrade reboot, so the tool could not confirm it.

    The upgrade may still have succeeded — this only means we lost sight of the controller. It matters most for
    the master, which this Mac reaches over a single link (for example its Wi-Fi): if the first boot after an
    OS upgrade leaves that link down, the box is invisible here and the reboot-once-more self-heal can't even run.
    """

    def __init__(self, u, minutes, method):
        self.host, self.ip, self.minutes, self.method = u["host"], u.get("ip"), minutes, method
        self.direct = "/proxy/" not in u["base"]   # reached straight (the master); no alternative route from here
        super().__init__(self._describe())

    def _describe(self):
        base = f"{self.host} did not rejoin the network at {self.ip} within {self.minutes} min after the upgrade reboot"
        if self.method != "fpp" and self.direct:
            return (base + ". The OS upgrade was sent and FPP began rebooting, so it may well have completed; the tool "
                    "just can't see it. The first boot after an OS upgrade can leave the controller's own Wi-Fi down — "
                    f"the one link this Mac reaches it over — so it goes invisible here. Power-cycle {self.host}, check "
                    f"its version in the Fleet view, then confirm with `tools/upgrade.py recheck --hosts {self.host}`. "
                    "Treat it as a failed upgrade only if it stays on the old version.")
        if self.method != "fpp":
            return base + ". An OS upgrade sometimes needs a power cycle to finish its reboot (FPP's release notes)."
        return base + "."


class FppdDown(RuntimeError):
    """The controller rebooted and is back on the network, but fppd (FPP's daemon) is not running.

    Seen after an `fpp` software update: the update swaps the files and restarts FPP's services, but on a slow
    board fppd can need a clean reboot to come up on the new version. The box is reachable (its web server
    answers) and on the new version — it just isn't playing until fppd starts, which a power cycle fixes. This
    is distinct from NotReturned: there the whole controller is invisible; here only fppd is down.
    """

    def __init__(self, u, minutes, state):
        self.host, self.ip, self.minutes, self.state = u["host"], u.get("ip"), minutes, state
        super().__init__(self._describe())

    def _describe(self):
        return (f"{self.host} is back on the network at {self.ip} but fppd is {self.state or 'not running'} "
                f"{self.minutes} min after the reboot — the files updated but it is not playing yet. Power-cycle "
                f"{self.host}, then confirm with `tools/upgrade.py recheck --hosts {self.host}`.")

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
    # Only plain image names from GitHub's own download host: the name becomes a file name on this Mac and on the
    # controller, and the URL is fetched by both.
    assets = {a["name"]: {"url": a["browser_download_url"], "size": a["size"],
                          "sha256": (a.get("digest") or "").removeprefix("sha256:") or None}
              for a in r.get("assets", []) if asset_ok(a.get("name"), a.get("browser_download_url"))}
    body = r.get("body") or ""
    return {"tag": r["tag_name"], "published": r.get("published_at"), "url": r.get("html_url"),
            "assets": assets, "os": os_advice(body), "notes_head": notes_head(body)}


ASSET_RE = re.compile(r"^[A-Za-z0-9][\w.\-]*\.fppos$")


def asset_ok(name, url):
    return bool(ASSET_RE.match(name or "")) and str(url or "").startswith("https://github.com/")


def os_is_64bit(os_version):
    """FPP writes the architecture into OSVersion in more than one spelling (64bit, 64-bit, 64 bit)."""
    return "64bit" in (os_version or "").replace("-", "").replace(" ", "").lower()


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
        return "Pi64" if os_is_64bit(info.get("OSVersion")) else "Pi"
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
             os_level=os_level(info.get("OSVersion")), arch="64-bit" if os_is_64bit(info.get("OSVersion")) else "32-bit",
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
    # Whether an OS upgrade could run here, worked out whatever the planned method: the user can still choose
    # the OS upgrade where the FPP update would do (run --os), and it needs the same image and space then.
    p["os_space"] = None
    if p["fppos"] and root.get("Free") is not None and root["Free"] < 2 * size:
        p["os_space"] = f"needs {2 * size / 1e9:.1f} GB free for the OS image, has {root['Free'] / 1e9:.1f} GB"
    if p["method"] != "fpp":
        if not p["fppos"]:
            p["blockers"].append(f"no {rel['tag']} OS image for this board ({fam or 'unknown'})")
        elif p["os_space"]:
            p["blockers"].append(p["os_space"])
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

def capture(u, tries=4):
    """What an upgrade must not change (or must change: the version). Read right after a reboot, a controller
    can answer 503 for a moment (its web server is up, fppd not yet), so a 503 means read it all again."""
    for i in range(tries):
        try:
            return _capture(u)
        except urllib.error.HTTPError as e:
            if e.code != 503 or i == tries - 1:
                raise
            time.sleep(2)


def _capture(u):
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
    try:
        d["plugins"] = sorted(str(x) for x in (fleet.fetch_json(f"{b}/api/plugin", timeout=10) or []))
    except Exception:
        d["plugins"] = []
    d["settings"] = {k: v["value"] for k, v in fleet.scan_unit({**u, "online": True}).items()}
    d["colorlight"] = bool(colorlight.outputs(b))
    return d


def save_pre(u, pre, target, method):
    """Keep the pre-upgrade state so `recheck` can compare after the controller is back (e.g. a manual power cycle)."""
    PRE_DIR.mkdir(parents=True, exist_ok=True)
    (PRE_DIR / f"{u['host']}.json").write_text(json.dumps({"pre": pre, "target": target, "method": method, "saved": time.time()}))


def load_pre(host):
    f = PRE_DIR / f"{host}.json"
    return json.loads(f.read_text()) if name_ok(host) and f.exists() else None


def colorlight_found(u):
    """Whether fppd found the ColorLight receiver at its latest start."""
    text = colorlight.tail(u["base"], "Logs/fppd.log", 4000)
    ev = colorlight.parse_fppd(text, 0)[0]
    starts = [i for i, e in enumerate(ev) if e["k"] == "init"]
    after = ev[starts[-1]:] if starts else []
    return any(e["k"] == "found" and e.get("v") for e in after), bool(starts)


def verify(u, pre, post, target, os_upgrade=False):
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
    # After an OS upgrade, "Plugins must be reinstalled" is expected (the rootfs was replaced), not a surprise:
    # report it as its own actionable check and keep it out of the generic warnings line.
    if os_upgrade and PLUGINS_WARNING in post["warnings"]:
        plugs = sorted(set(post.get("plugins") or []))
        out.append(("WARN", "plugins", "reinstall in FPP's Plugin Manager (expected after an OS upgrade)"
                    + (": " + ", ".join(plugs) if plugs else "")))
        new = [w for w in new if w != PLUGINS_WARNING]
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
# ...but not a copied file's path that happens to contain one of those words
# (usr/share/vim/.../htmlcomplete.vim, var/log/apache2/error.log), nor rsync's
# chown complaints about the boot partition: it is FAT, which has no owners.
NOISE = re.compile(r'^[^\s]+/[^\s]*$|^rsync: \[generator\] chown ".*" failed: Operation not permitted')


def stream(u, path, label):
    """Start one of FPP's upgrade pages and echo its notable output as it
    arrives. It runs on in its own thread: FPP finishes the upgrade even if the
    connection drops (the player's proxy may cut a long request), and progress
    is followed through updateActivity regardless."""
    state = {"error": None, "lines": collections.deque(maxlen=40), "hidden": 0}

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
                        state["lines"].append(text)
                        if SHOW.search(text) and not NOISE.search(text):
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
    started, progressed, last_stage, last_size, last_move, misses = False, False, None, None, t0, 0
    while True:
        time.sleep(LIMITS["poll"])
        now = time.time()
        try:
            a = activity(u)
            misses = 0
        except Exception:
            misses += 1
            if started and method != "fpp" and misses >= 6:
                # an OS upgrade reboots on its own, so a minute or more of silence means it has begun. One missed
                # reply is not enough: mid-copy the board can be too busy to answer through the player's proxy.
                return "rebooting"
            if started and progressed and method == "fpp" and misses >= 9:
                # an fpp update stops fppd and its progress feed to restart its services at the end; losing the
                # feed for ~90 s after the update has already made progress means it is finishing, not failing.
                return "restarting"
            if misses >= 6 and not started:
                raise RuntimeError("the controller stopped answering before the upgrade began"
                                   + (f": {out['error']}" if out["error"] else ""))
            if misses >= 18:                     # started but never showed progress and now silent: genuinely stuck
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
            last_move, progressed = now, True
        if a.get("logSize") != last_size:
            last_size, last_move, progressed = a.get("logSize"), now, True
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


def wait_restart(u, since, limit, method, reboot_sent=True):
    """Wait until the controller has rebooted (its uptime restarted) and fppd runs."""
    t0 = time.time()
    say(f"  waiting for {u['host']} to restart (up to {limit // 60} min)")
    back, answering = None, False
    while time.time() - t0 < limit:
        time.sleep(LIMITS["poll"])
        try:
            st = fleet.fetch_json(f"{u['base']}/api/fppd/status", timeout=8, tries=1)
            answering = True
        except Exception:
            answering = False
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
                raise FppdDown(u, LIMITS["fppd"] // 60, st.get("fppd"))
    if answering and back is None:
        # It answered all along; it just never rebooted. Not "did not rejoin the network".
        raise RuntimeError(f"{u['host']} is answering but did not reboot within {limit // 60} min (its uptime never "
                           "restarted)" + ("" if reboot_sent else "; the reboot request failed")
                           + f". Reboot it from FPP's UI, then confirm with `tools/upgrade.py recheck --hosts {u['host']}`.")
    raise NotReturned(u, limit // 60, method)


def startup_failed(u):
    """Whether FPP's startup (fppinit.service) failed on the controller's
    current boot; None when the system log can't be read. The first boot after
    an OS upgrade can run it past systemd's time limit, and the part it never
    reaches (writing the WiFi connection file, for one) stays undone until
    the next boot. Seen on a PocketBeagle2, 10.3 -> 10.4."""
    text = None
    for i in range(3):   # right after a reboot the log read can 503 for a moment; a transient miss is not "fine"
        try:
            text = colorlight.tail(u["base"], "Logs/var/log/syslog", 3000)
            break
        except Exception:
            if i < 2:
                time.sleep(1)
    if text is None:
        return None
    lines = text.splitlines()
    starts = [i for i, ln in enumerate(lines) if "Starting fppinit.service" in ln]
    if not starts:
        return None
    return any("fppinit.service" in ln and ("Failed" in ln or "timed out" in ln) for ln in lines[starts[-1]:])


def send_reboot(u):
    """POST a reboot, riding over a transient 503 (the proxy or the controller's web server can be momentarily
    unavailable while services cycle). wait_restart follows either way; a persistent failure is only a warning.
    Returns whether the request was accepted."""
    for i in range(5):
        try:
            fpp_http.request(f"{u['base']}/api/system/reboot", timeout=15, method="POST", data=b"")
            return True
        except urllib.error.HTTPError as e:
            if e.code == 503 and i < 4:
                time.sleep(1)
                continue
            say(f"  WARN: the reboot request returned {e.code}; waiting in case it reboots anyway")
            return False
        except Exception as e:
            # A dropped connection here usually means the reboot began before FPP answered.
            say(f"  WARN: couldn't send the reboot cleanly ({str(e)[:80]}); waiting in case it reboots on its own")
            return True


def check_startup(u, method):
    """After the upgrade's reboot: when FPP's startup failed, reboot once more
    (what fixed it by hand) and check again."""
    if startup_failed(u):
        say("  FPP's startup (fppinit) did not finish on this boot; rebooting once more to let it complete")
        since = time.time()
        sent = send_reboot(u)
        wait_restart(u, since, LIMITS["reboot_fpp"], method, reboot_sent=sent)
        if startup_failed(u):
            raise RuntimeError("FPP's startup (fppinit) failed again after a second reboot: check the controller's system log")
        say("  FPP's startup finished on the second boot")


def fetch_fppos(name, asset):
    """The .fppos image on this Mac, downloaded once and checked against GitHub's SHA-256."""
    import get_image
    if not ASSET_RE.match(name or ""):
        raise RuntimeError(f"unexpected OS image name {str(name)[:60]!r}")
    FPPOS_DIR.mkdir(parents=True, exist_ok=True)
    path = FPPOS_DIR / name
    if path.exists() and path.stat().st_size == asset["size"] and (not asset["sha256"] or get_image.sha256_of(path) == asset["sha256"]):
        say(f"  {name}: already downloaded, checksum OK")
        return path
    if not str(asset.get("url") or "").startswith("https://github.com/"):
        raise RuntimeError(f"{name}: not a GitHub download URL ({str(asset.get('url'))[:60]})")
    say(f"  downloading {name} ({asset['size'] / 1e9:.2f} GB)")
    get_image.download(asset["url"], path, asset["size"], asset["sha256"])
    return path


def remove_after_failure(u, name):
    """After a failed OS upgrade, delete its 1-1.3 GB image from Uploads, unless FPP is still using it."""
    later = f"delete it later with `tools/upgrade.py cleanup --hosts {u['host']}`"
    try:
        if name not in {i["name"] for i in uploads_images(u)}:
            return
        if activity(u).get("inProgress"):
            say(f"  {name} left in Uploads: an update is still running there; {later}")
            return
    except Exception:
        say(f"  {name} may still be in Uploads ({u['host']} isn't answering); {later}")
        return
    remove_upload(u, name)


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
    if method != "fpp" and p.get("os_space"):
        raise RuntimeError(p["os_space"] + (": use the FPP update" if p["method"] == "fpp" else ""))
    say(f"\n=== {u['host']} ({p.get('platform')}, {p['version']} -> {target}, {method}) ===")
    guard_show(master, p["minutes"])
    say("  recording the current state (outputs, content, network, settings)…")
    pre = capture(u)
    if not dry:
        save_pre(u, pre, target, method)  # so `recheck` can confirm later if the controller goes dark mid-reboot
    if dry:
        say("  dry run: nothing started; comparing the state with itself")
        post = pre
    else:
        image_there = False  # the .fppos is (or may be) in the controller's Uploads: ours, or FPP's own download
        try:
            if method == "os-push":
                image = fetch_fppos(p["fppos"], rel["assets"][p["fppos"]])
                image_there = True  # set before the upload: a failed upload can leave a partial file
                upload(u, image)
                guard_show(master, p["minutes"])
            before = activity(u)
            since = time.time()
            if method == "fpp":
                out = stream(u, f"upgradefpp.php?wrapped=1&version={urllib.parse.quote(target)}", "upgrade")
            else:
                image_there = True
                src = p["fppos"] if method == "os-push" else rel["assets"][p["fppos"]]["url"]
                out = stream(u, f"upgradeOS.php?wrapped=1&os={urllib.parse.quote(src, safe='')}", "OS upgrade")
            result = follow(u, before, method, out)
            sent = True
            if method == "fpp":
                # An fpp update does not reboot on its own, and on a slow board fppd may not come back until it does;
                # always reboot to finish, whether the update reported done or went quiet while restarting its services.
                say("  update applied; rebooting to finish" if result == "restarting" else "  upgrade complete; rebooting")
                sent = send_reboot(u)
            wait_restart(u, since, LIMITS["reboot_fpp" if method == "fpp" else "reboot_os"], method, reboot_sent=sent)
            check_startup(u, method)
            post = capture(u)
        except (Exception, SystemExit):  # SystemExit: get_image.download on a bad checksum
            if image_there:
                remove_after_failure(u, p["fppos"])
            raise
        if image_there:  # FPP leaves the image in Uploads whether it downloaded it or we sent it
            remove_upload(u, p["fppos"])
    results = verify(u, pre, post, target if not dry else str(pre["version"]), os_upgrade=(method != "fpp" and not dry))
    for level, name, detail in results:
        say(f"  {level}  {name}{': ' + detail if detail else ''}")
    failed = [r for r in results if r[0] == "FAIL"]
    if failed:
        raise RuntimeError("verification failed: " + ", ".join(r[1] for r in failed))
    say(f"  {u['host']}: OK" + (f" ({sum(r[0] == 'WARN' for r in results)} warning(s))" if any(r[0] == "WARN" for r in results) else ""))


def recheck(master, hosts):
    """Re-run the verification against the saved pre-upgrade state, without touching the controller.

    For the case where a controller went dark during its upgrade reboot (so `run` couldn't confirm it) and has
    since come back — typically after a manual power cycle. Confirms the upgrade took and nothing was lost.
    """
    units = {u["host"]: u for u in fleet.discover(master, fleet.load_inventory(), hosts=set(hosts))}
    bad = False
    for h in hosts:
        rec, u = load_pre(h), units.get(h)
        if not rec:
            say(f"{h}: no saved pre-upgrade state to compare against (run an upgrade first)"); bad = True; continue
        if u is None or not u.get("online"):
            say(f"{h}: not reachable from here — power-cycle it and try again"); bad = True; continue
        post = capture(u)
        results = verify(u, rec["pre"], post, rec["target"], os_upgrade=(rec["method"] != "fpp"))
        say(f"\n=== {h}: re-check against the saved pre-upgrade state ({rec['pre']['version']} -> target {rec['target']}) ===")
        for level, name, detail in results:
            say(f"  {level}  {name}{': ' + detail if detail else ''}")
        failed = [r for r in results if r[0] == "FAIL"]
        if failed:
            say(f"  {h}: still not verified: " + ", ".join(r[1] for r in failed)); bad = True
        else:
            say(f"  {h}: OK" + (f" ({sum(r[0] == 'WARN' for r in results)} warning(s))" if any(r[0] == "WARN" for r in results) else ""))
    return 1 if bad else 0


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
    for name in ("plan", "run", "images", "cleanup", "recheck"):
        s = sub.add_parser(name)
        s.add_argument("--master", default=fleet.load_settings()["player"])
        s.add_argument("--target", help="release tag (default: the newest)")
        s.add_argument("--hosts", required=name in ("run", "cleanup", "recheck"), help="comma-separated controller names")
    sub.choices["plan"].add_argument("--json", action="store_true")
    sub.choices["images"].add_argument("--json", action="store_true")
    sub.choices["run"].add_argument("--os", action="store_true", help="OS-level upgrade even where the FPP update would do")
    sub.choices["run"].add_argument("--dry-run", action="store_true", help="checks and comparison only; starts nothing")
    args = ap.parse_args()
    if not args.master:
        sys.exit("no player: pass --master or set it on the Settings page")
    hosts = list(dict.fromkeys(h for h in (args.hosts or "").split(",") if h)) or None  # each once, in order
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
    if args.cmd == "recheck":
        sys.exit(recheck(args.master, hosts))
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
