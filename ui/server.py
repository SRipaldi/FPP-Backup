#!/usr/bin/env python3
"""FPP-Backup local web UI.

A thin layer over the tools/ scripts: every action runs the same command you
would run by hand, as a job whose output streams to the browser. Read-only
against controllers, like the tools.

Listens on 127.0.0.1 only. Every POST must carry the per-run token embedded in
the page, and the Host header must be local, so other sites open in the same
browser cannot trigger actions (CSRF / DNS rebinding).

Start it from Terminal or VS Code (not a background service): flashing shows
the macOS admin dialog, which needs your login session.

Usage: python3 ui/server.py [--master <player-ip>] [--port 8765] [--no-browser]
The player defaults to the one saved on the Settings page (config/settings.json).
"""
import argparse
import concurrent.futures
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
STATIC = Path(__file__).resolve().parent / "static"
SNAPSHOTS = ROOT / "snapshots"
IMAGES = ROOT / "work/images"
OUT = ROOT / "work/out"
PY = sys.executable

sys.path.insert(0, str(TOOLS))
import fleet  # noqa: E402
import get_image  # noqa: E402
import xlights  # noqa: E402

TOKEN = secrets.token_urlsafe(24)
CONFIG = {"master": None, "from_cli": False}  # the player; from config/settings.json unless --master


def need_player():
    if not CONFIG["master"]:
        raise ValueError("No player configured yet: open Settings and enter your FPP player's address.")
    return CONFIG["master"]


# ---------------------------------------------------------------- jobs

# ---------------------------------------------------------------- notifications

NOTES_FILE = ROOT / "work/notifications.json"
_notes = {"items": [], "lock": threading.Lock()}


def _load_notes():
    try:
        _notes["items"] = json.loads(NOTES_FILE.read_text())
    except (FileNotFoundError, ValueError):
        _notes["items"] = []


def notify(level, text, link=None, key=None, quiet_s=3600):
    """Record a notification (level: bad / warn / ok / info). A key suppresses a
    repeat of the same unread notice within quiet_s seconds."""
    with _notes["lock"]:
        now = time.time()
        if key and any(n.get("key") == key and not n["read"] and now - n["t"] < quiet_s for n in _notes["items"]):
            return
        _notes["items"].append({"id": secrets.token_hex(4), "t": now, "level": level, "text": text,
                                "link": link, "key": key, "read": False})
        _notes["items"] = _notes["items"][-200:]
        NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
        NOTES_FILE.write_text(json.dumps(_notes["items"]))


def notes_mark(ids=None, clear=False):
    with _notes["lock"]:
        if clear:
            _notes["items"] = []
        for n in _notes["items"]:
            if ids is None or n["id"] in ids:
                n["read"] = True
        NOTES_FILE.write_text(json.dumps(_notes["items"]))


class Job:
    def __init__(self, kind, title, steps, keep_going=False, after=None):
        """keep_going: run every step even if one fails (independent actions).
        after: called with the job when the steps are done, before the final
        status is reported (e.g. a fleet rescan, so the UI redraws fresh data)."""
        self.id = secrets.token_hex(4)
        self.kind, self.title, self.steps = kind, title, steps
        self.keep_going, self.after = keep_going, after
        self.lines, self.progress = [], ""
        self.status, self.started, self.ended = "running", time.time(), None
        self.cond = threading.Condition()

    def emit(self, line=None, progress=None):
        with self.cond:
            if line is not None:
                self.lines.append(line)
                self.progress = ""
            if progress is not None:
                self.progress = progress
            self.cond.notify_all()

    def run(self):
        failed = 0
        try:
            for step in self.steps:
                self.emit("$ " + " ".join(str(a) for a in step))
                proc = subprocess.Popen([str(a) for a in step], cwd=ROOT, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                        env={**os.environ, "PYTHONUNBUFFERED": "1"})
                buf = b""
                while True:
                    b = proc.stdout.read1(4096)
                    if not b:
                        break
                    buf += b
                    # \r-terminated output is a progress line that replaces itself.
                    while True:
                        m = re.search(rb"[\r\n]", buf)
                        if not m:
                            break
                        text = buf[:m.start()].decode(errors="replace")
                        sep, buf = buf[m.start():m.end()], buf[m.end():]
                        if sep == b"\r":
                            if text.strip():
                                self.emit(progress=text)
                        else:
                            self.emit(line=text)
                if buf:
                    self.emit(line=buf.decode(errors="replace"))
                if proc.wait() != 0:
                    failed += 1
                    if not self.keep_going:
                        self.finish("failed", f"exit status {proc.returncode}")
                        return
                    self.emit(f"[step failed: exit status {proc.returncode}; continuing]")
            if self.after:
                self.after(self)
            if failed:
                self.finish("failed", f"{failed} of {len(self.steps)} steps failed")
            else:
                self.finish("done")
        except Exception as e:  # report, never kill the server
            self.finish("failed", repr(e))

    def finish(self, status, note=None):
        with self.cond:
            if note:
                self.lines.append(f"[{status}] {note}")
            self.status, self.ended = status, time.time()
            self.cond.notify_all()
        if self.kind == "test":
            return
        if status == "failed":
            notify("bad", f"Failed: {self.title}" + (f" ({note})" if note else ""), "jobs")
        elif self.kind in ("snapshot", "build", "flash", "fleet", "xlights"):
            notify("ok", f"Done: {self.title}", "jobs")

    def summary(self):
        return {"id": self.id, "kind": self.kind, "title": self.title, "status": self.status,
                "started": self.started, "ended": self.ended,
                "last": self.progress or (self.lines[-1] if self.lines else "")}


JOBS = {}
JOBS_LOCK = threading.Lock()


def start_job(kind, title, steps, exclusive=None, **opts):
    with JOBS_LOCK:
        if exclusive and any(j.kind == exclusive and j.status == "running" for j in JOBS.values()):
            raise ValueError(f"a {exclusive} job is already running")
        job = Job(kind, title, steps, **opts)
        JOBS[job.id] = job
    threading.Thread(target=job.run, daemon=True).start()
    return job


# ---------------------------------------------------------------- data

def fetch_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.load(resp)


def snapshots():
    out = []
    for m in sorted(SNAPSHOTS.glob("*/*/manifest.json")):
        try:
            d = json.loads(m.read_text())
        except ValueError:
            continue
        files = d.get("files", {})
        out.append({"host": d["host"], "taken": d["taken"], "path": str(m.parent.relative_to(ROOT)),
                    "version": d.get("fpp_version"), "platform": d.get("platform"),
                    "variant": d.get("variant"), "files": len(files),
                    "bytes": sum(f["bytes"] for f in files.values()),
                    "media": any(k.startswith(("sequences/", "music/", "videos/")) for k in files),
                    "errors": len(d.get("errors", []))})
    return sorted(out, key=lambda s: (s["host"].lower(), s["taken"]), reverse=False)


def controllers():
    found = fleet.discover(need_player(), fleet.load_inventory())
    snaps = snapshots()

    def status(c):  # playing / idle, for the status dot
        try:
            return fleet.fetch_json(f"{c['base']}/api/fppd/status", timeout=4, tries=2).get("status_name")
        except Exception:
            return None

    online = [c for c in found if c["online"]]
    with concurrent.futures.ThreadPoolExecutor(6) as ex:
        for c, st in zip(online, ex.map(status, online)):
            c["status"] = st
    for c in found:
        c.pop("base", None)
        c["image_family"] = get_image.asset_for_platform(c.get("platform") or "", c.get("variant") or "")
        mine = [s for s in snaps if s["host"] == c["host"]]
        c["last_snapshot"] = mine[-1]["taken"] if mine else None
        c["snapshots"] = len(mine)
    return found


_releases = {"t": 0, "data": None}


def releases():
    if time.time() - _releases["t"] > 600 or _releases["data"] is None:
        _releases["data"] = get_image.releases(8)
        _releases["t"] = time.time()
    return _releases["data"]


def files_in(d, pattern):
    return [{"name": p.name, "path": str(p.relative_to(ROOT)), "bytes": p.stat().st_size,
             "mtime": p.stat().st_mtime}
            for p in sorted(d.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
            if not p.name.endswith(".part")] if d.exists() else []


def cards():
    out = subprocess.run([PY, TOOLS / "flash_card.py", "--list"], capture_output=True, text=True, cwd=ROOT)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "card listing failed")
    rows = []
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            rows.append({"disk": parts[0], "size": parts[1], "name": parts[2] if len(parts) > 2 else ""})
    return rows


def reference_for(snapshot_path, version):
    """A snapshot of another unit already on the target version, same platform first.

    stage_card.py takes the owner's 10.x setup answers (LegalJurisdiction,
    privacy) from it when the source predates them."""
    m = json.loads((ROOT / snapshot_path / "manifest.json").read_text())
    cands = [s for s in snapshots() if s["version"] == version and s["host"] != m["host"]]
    cands.sort(key=lambda s: (s["platform"] == m.get("platform"), s["taken"]), reverse=True)
    return cands[0]["path"] if cands else None


# ---------------------------------------------------------------- actions

def safe_rel(path, under):
    p = (ROOT / path).resolve()
    if not str(p).startswith(str((ROOT / under).resolve()) + os.sep) or not p.exists():
        raise ValueError(f"not a valid path under {under}/: {path}")
    return p


def act_snapshot(body):
    ip, media = body["ip"], bool(body.get("media"))
    if not re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
        raise ValueError("bad ip")
    via = body.get("via")
    step = [PY, TOOLS / "snapshot.py"] + (["--media"] if media else [])
    step += ["--via", via, ip] if via else [f"http://{ip}"]
    return start_job("snapshot", f"Snapshot {body.get('host', ip)}" + (" + media" if media else ""), [step])


def act_backup(body):
    """Snapshot several controllers in one job: one snapshot.py run per unit, in
    turn, each with its own media choice; a failed unit does not stop the rest.
    Read-only against the controllers."""
    units = body.get("units") or []
    if not units:
        raise ValueError("no controllers selected")
    steps, names = [], []
    for u in units:
        ip, via, host = u.get("ip", ""), u.get("via") or None, u.get("host", "")
        if not re.match(r"^\d+\.\d+\.\d+\.\d+$", ip) or not re.match(r"^[\w\-]*$", host):
            raise ValueError(f"bad controller: {host or ip}")
        if via and via != CONFIG["master"]:
            raise ValueError(f"{host}: unexpected proxy {via}")
        step = [PY, TOOLS / "snapshot.py"] + (["--media"] if u.get("media") else [])
        step += ["--via", via, ip] if via else [f"http://{ip}"]
        steps.append(step)
        names.append(host + (" + media" if u.get("media") else ""))
    title = f"Back up {len(units)} controller(s): " + ", ".join(names)
    return start_job("snapshot", title, steps, keep_going=True)


def act_download(body):
    tag, asset = body["tag"], body["asset"]
    if not re.match(r"^[\w.\-]+$", tag) or not re.match(r"^[\w.\-]+\.img\.zip$", asset):
        raise ValueError("bad release or asset name")
    return start_job("download", f"Download {asset}", [[PY, TOOLS / "get_image.py", tag, asset]],
                     exclusive="download")


def act_build(body):
    snap = safe_rel(body["snapshot"], "snapshots")
    stock = safe_rel(body["image"], "work/images")
    m = json.loads((snap / "manifest.json").read_text())
    host = m["host"]
    ver = re.search(r"v([\d.]+|nightly)", stock.name)
    ver = ver.group(1) if ver else "custom"
    stage = ROOT / "work/stage" / host
    out = OUT / f"{host}-{ver}.img"
    step1 = [PY, TOOLS / "stage_card.py", snap, stage]
    ref = reference_for(str(snap.relative_to(ROOT)), ver)
    if ref:
        step1 += ["--reference", ROOT / ref]
    return start_job("build", f"Build {out.name} from {host} {m['taken']}",
                     [step1, ["bash", TOOLS / "build_image.sh", stock, stage, out]], exclusive="build")


def act_flash(body):
    image = safe_rel(body["image"], "work/out")
    disk = body["disk"]
    if not re.match(r"^disk\d+$", disk) or body.get("confirm") != disk:
        raise ValueError("type the disk id to confirm")
    if disk not in [c["disk"] for c in cards()]:
        raise ValueError(f"{disk} is not a detected SD card")
    return start_job("flash", f"Flash {image.name} to {disk}",
                     [[PY, TOOLS / "flash_card.py", image, disk, "--yes"]], exclusive="flash")


# ---------------------------------------------------------------- fleet

_fleet = {"t": 0, "scan": None, "data": None, "lock": threading.Lock()}


def fleet_report(refresh=False):
    """tools/fleet.py's drift report. The scan (~20 s, read-only) is cached for
    five minutes; the policy is re-evaluated on every call, which is instant, so
    policy edits show up without rescanning."""
    with _fleet["lock"]:
        rescanned = refresh or _fleet["scan"] is None or time.time() - _fleet["t"] > 300
        if rescanned:
            _fleet["scan"] = fleet.scan_fleet(need_player(), check_updates=fleet.load_settings()["update_check"])
            _fleet["t"] = time.time()
        prev = _fleet["data"]
        _fleet["data"] = fleet.evaluate(_fleet["scan"], load_policy())
        _fleet["data"]["policy"]["backup_max_age_days"] = fleet.load_settings()["backup_max_age_days"]
        if rescanned:
            scan_notices(prev, _fleet["data"])
        return _fleet["data"]


def scan_notices(prev, rep):
    """Notify what a full scan found that the previous one did not."""
    old_drift = {r["name"] for r in (prev or {}).get("match", []) if r["drift"]}
    for r in rep["match"]:
        if r["drift"] and r["name"] not in old_drift:
            hosts = [h for h, c in r["cells"].items() if c["status"] == "drift"]
            notify("warn", f"Policy drift: {r['name']} differs on {', '.join(hosts)}", "fleet", key=f"drift:{r['name']}")
    cov = rep.get("coverage") or {}
    for u in cov.get("units", []):
        for seq in u.get("missing", []):
            used = (cov.get("used_by") or {}).get(seq) or cov.get("playlists", [])
            notify("bad", f"{u['host']} is missing {seq}, used by {', '.join(used)}. It will be dark for that sequence.",
                   "sequences", key=f"cov:{u['host']}:{seq}", quiet_s=86400)
    latest = (rep.get("releases") or {}).get("latest")
    if latest and fleet._vkey(latest) > fleet._vkey(rep["policy"].get("fpp_version")):
        notify("info", f"FPP {latest} has been released (the policy targets {rep['policy']['fpp_version']})",
               "dashboard", key=f"release:{latest}", quiet_s=7 * 86400)


# ---------------------------------------------------------------- monitor

_mon = {"online": None, "misses": {}, "show": None, "last": None}


def live_status():
    """Every online unit's playback state and how many sync packets it has had
    from the player, for the live show view and the monitor. Read-only."""
    scan = _fleet["scan"]
    units = [u for u in (scan["units"] if scan else fleet.discover(CONFIG["master"], fleet.load_inventory())) if u.get("online")]
    master = need_player()
    player = next((u for u in units if u.get("is_master")), None)
    player_addrs = set((player or {}).get("addresses", [])) | {master}

    def one(u):
        base = f"http://{master}" if u.get("is_master") else (
            f"http://{u['via']}/proxy/{u['ip']}" if u.get("via") else f"http://{u['ip']}")
        out = {"host": u["host"], "role": "player" if u.get("is_master") else "remote", "ip": u["ip"]}
        try:
            st = fleet.fetch_json(f"{base}/api/fppd/status", timeout=4, tries=2)
            out.update(status=st.get("status_name"), sequence=st.get("current_sequence") or "",
                       seconds=int(st.get("seconds_played") or 0), remaining=int(st.get("seconds_remaining") or 0),
                       playlist=(st.get("current_playlist") or {}).get("playlist"), mode=st.get("mode_name"))
        except Exception as e:
            out["error"] = str(e)[:60]
            return out
        if not u.get("is_master"):
            try:
                ms = fleet.fetch_json(f"{base}/api/fppd/multiSyncStats", timeout=4, tries=1).get("systems", [])
                from_player = [x for x in ms if x.get("sourceIP") in player_addrs]
                out["sync_pkts"] = sum(x.get("pktSyncSeqSync", 0) + x.get("pktSyncMedSync", 0) for x in from_player)
                out["sync_errors"] = sum(x.get("pktError", 0) for x in from_player)
                out["last_sync"] = max((x.get("lastReceiveTime") or "" for x in from_player), default="")
            except Exception:
                pass
        return out

    with concurrent.futures.ThreadPoolExecutor(8) as ex:
        rows = list(ex.map(one, units))
    now = time.time()
    prev = _mon["last"]
    if prev:
        dt = now - prev["t"]
        for r in rows:
            p = prev["pkts"].get(r["host"])
            if p is not None and r.get("sync_pkts") is not None and dt > 0:
                r["sync_rate"] = round(max(0, r["sync_pkts"] - p) / dt, 1)
    _mon["last"] = {"t": now, "pkts": {r["host"]: r.get("sync_pkts") for r in rows}}
    return {"t": now, "units": rows, "player": next((r for r in rows if r["role"] == "player"), None)}


def monitor_loop():
    """Background watch while the UI server runs: offline/online changes, show
    start/stop, and remotes that are dark while the show plays. Follows the
    Settings page (on/off, interval) and does nothing until a player is set."""
    while True:
        st = fleet.load_settings()
        interval = st["monitor_interval"]
        if not st["monitor"] or not CONFIG["master"]:
            time.sleep(30)
            continue
        try:
            if _fleet["lock"].locked():  # a full scan is loading the proxy; check next time
                time.sleep(interval)
                continue
            inv = fleet.load_inventory()
            units = fleet.discover(CONFIG["master"], inv)
            seen = {u["host"]: u["online"] for u in units if u.get("expected") is not False}
            # A unit counts as offline only after two missed checks in a row: one
            # slow reply through the player's proxy is not an outage.
            online = {}
            for h, up in seen.items():
                _mon["misses"][h] = 0 if up else _mon["misses"].get(h, 0) + 1
                online[h] = _mon["misses"][h] < 2
            if _mon["online"] is not None:
                for h, up in online.items():
                    was = _mon["online"].get(h)
                    if was and not up:
                        notify("bad", f"{h} went offline", "dashboard", key=f"off:{h}")
                    elif was is False and up:
                        notify("ok", f"{h} is back online", "dashboard", key=f"on:{h}", quiet_s=60)
            for u in units:
                if u["online"] and u.get("expected") is False:
                    notify("warn", f"{u['host']} ({u['ip']}) answers but isn't in the inventory", "dashboard", key=f"unknown:{u['host']}", quiet_s=86400)
            _mon["online"] = online
            live = live_status()
            pl = live["player"] or {}
            playing = pl.get("status") == "playing"
            if playing and _mon["show"] != pl.get("playlist"):
                notify("info", f"Show started: {pl.get('playlist') or pl.get('sequence')}", "show", key=f"show:{pl.get('playlist')}", quiet_s=600)
            if not playing and _mon["show"]:
                notify("info", f"Show stopped: {_mon['show']}", "show", key=f"stop:{_mon['show']}", quiet_s=600)
            _mon["show"] = pl.get("playlist") if playing else None
            if playing:
                for r in live["units"]:
                    if r["role"] == "remote" and r.get("mode") == "remote" and "error" not in r and \
                            (r.get("status") != "playing" or r.get("sequence") != pl.get("sequence")):
                        notify("bad", f"{r['host']} is dark during {pl.get('sequence')} (it is {r.get('status')}"
                               + (f", on {r.get('sequence')}" if r.get('sequence') else "") + ")",
                               "show", key=f"dark:{r['host']}:{pl.get('sequence')}", quiet_s=1800)
        except Exception as e:
            sys.stderr.write(f"monitor: {e}\n")
        time.sleep(interval)


def load_policy():
    """The installation's policy; created from the template, using the player's
    own settings, the first time it is needed."""
    if not fleet.POLICY.exists():
        fleet.create_policy(need_player())
        notify("info", "Created the fleet policy from the template using your player's settings. Review it under Settings > Fleet policy.",
               "settings", key="policy-created")
    return fleet.load_policy()


def save_policy(policy):
    policy = fleet.validate_policy(policy)
    fleet.POLICY.write_text(json.dumps(policy, indent=2) + "\n")
    return policy


def fleet_set_steps(name, rule, rep):
    """fleet.py set commands that bring one policy setting to its expected value
    on the units that drift; returns (steps, drifted hosts)."""
    row = next((r for r in rep["match"] if r["name"] == name), None)
    drifted = [h for h, c in row["cells"].items() if c["status"] == "drift"] if row else []
    master_host = next((u["host"] for u in rep["units"] if u.get("is_master")), None)
    base = [PY, TOOLS / "fleet.py", "--master", CONFIG["master"], "set", name]
    steps = []
    others = [h for h in drifted if h != master_host]
    if others:
        steps.append(base + ["master" if rule["value"] == "@master" else rule["value"], "--hosts", ",".join(others)])
    if master_host in drifted and rule.get("player") != "*":
        value = rule.get("player", rule["value"])
        steps.append(base + ["" if value == "@master" else value, "--hosts", master_host])
    return steps, drifted


def rescan_after(job):
    job.emit("Rescanning the fleet (about 20 seconds)...")
    rep = fleet_report(refresh=True)
    left = [r["name"] for r in rep["match"] if r["drift"]]
    job.emit("Rescan done: " + (f"still drifting: {', '.join(left)}" if left else "no policy drift"))


def act_fleet_apply(body):
    """Bring the chosen policy settings to their expected values on the units that
    drift. One step per setting, run in turn; a failed step does not stop the rest.
    Optionally restart the players on units that changed a setting needing it
    (fleet.py restart skips any that are playing). Ends with a rescan.
    Values always come from fleet-policy.json, never from the request."""
    names = body.get("settings") or ([body["setting"]] if body.get("setting") else [])
    policy = load_policy()
    if _fleet["scan"] is None:
        raise ValueError("the fleet has not been scanned yet")
    rep = fleet.evaluate(_fleet["scan"], policy)  # the current policy, not the last report
    unknown = [n for n in names if n not in policy["match"]]
    if not names or unknown:
        raise ValueError(f"not policy settings: {', '.join(unknown) or '(none chosen)'}")
    steps, touched, restart_hosts = [], set(), set()
    for name in names:
        s, drifted = fleet_set_steps(name, policy["match"][name], rep)
        steps += s
        touched.update(drifted)
        row = next(r for r in rep["match"] if r["name"] == name)
        if row.get("needs"):
            restart_hosts.update(drifted)
    if not steps:
        raise ValueError("none of the chosen settings drift any more; rescan")
    if body.get("restart") and restart_hosts:
        steps.append([PY, TOOLS / "fleet.py", "--master", CONFIG["master"], "restart",
                      "--hosts", ",".join(sorted(restart_hosts))])
    title = f"Fleet: set {', '.join(names)} on {len(touched)} unit(s)"
    return start_job("fleet", title, steps, exclusive="fleet", keep_going=True, after=rescan_after)


def act_fleet_expect(body):
    """Make values the expected ones (or promote compared settings into the policy).
    Takes {"items": [{"setting", "value"}, ...]} or a single {"setting", "value"}.
    Every item is validated before the file is written, so it is all or nothing.
    Edits fleet-policy.json only; nothing is sent to a controller."""
    items = body.get("items") or [{"setting": body.get("setting"), "value": body.get("value")}]
    policy = load_policy()
    known = set(fleet.setting_catalog(_fleet["scan"], policy)) if _fleet["scan"] else set()
    for it in items:
        name, value = it.get("setting"), it.get("value")
        if not isinstance(name, str) or not re.match(r"^[\w\-]+$", name) or value is None:
            raise ValueError(f"bad setting: {name!r}")
        value = str(value)
        if len(value) > 200 or "\n" in value:
            raise ValueError(f"bad value for {name}")
        if name not in policy["match"] and name not in known:
            raise ValueError(f"unknown setting {name}")
    for it in items:
        name = it["setting"]
        rule = policy["match"].setdefault(name, {"why": "Added from the Fleet page"})
        rule["value"] = str(it["value"])
        if name in policy["compare"]:
            policy["compare"].remove(name)
    save_policy(policy)
    return fleet_report()


def act_fleet_preview(body):
    """Evaluate a proposed policy against the last scan. Nothing is saved or sent."""
    if _fleet["scan"] is None:
        raise ValueError("the fleet has not been scanned yet")
    return fleet.evaluate(_fleet["scan"], fleet.validate_policy(body.get("policy")))


def act_fleet_policy_save(body):
    """Validate and write fleet-policy.json; returns the report under the new policy."""
    save_policy(body.get("policy"))
    return fleet_report()


def backups_by_host():
    """Last backup per controller, and the last one that included media."""
    out = {}
    for sn in snapshots():
        b = out.setdefault(sn["host"], {"last": None, "last_media": None, "count": 0})
        b["count"] += 1
        b["last"] = max(b["last"] or "", sn["taken"])
        if sn["media"]:
            b["last_media"] = max(b["last_media"] or "", sn["taken"])
    return out


def act_inventory(body):
    """Add a controller to fleet-inventory.json (from the last scan) or remove one.
    Edits the inventory file only."""
    host, op = body.get("host", ""), body.get("op")
    if not re.match(r"^[\w\-]+$", host) or op not in ("add", "remove"):
        raise ValueError("bad request")
    inv = fleet.load_inventory() or {"controllers": []}
    rows = [c for c in inv["controllers"] if c["host"] != host]
    if op == "add":
        u = next((u for u in (_fleet["scan"] or {}).get("units", []) if u["host"] == host and u["online"]), None)
        if not u:
            raise ValueError(f"{host} is not online in the last scan")
        rows.append({"host": host, "ip": u["ip"], "role": "player" if u.get("is_master") else "remote"})
    inv["controllers"] = rows
    fleet.save_inventory(inv)
    if _fleet["scan"]:  # reflect it without a rescan
        for u in _fleet["scan"]["units"]:
            if u["host"] == host:
                u["expected"] = op == "add"
        _fleet["scan"]["inventory"] = True
        if op == "remove":
            _fleet["scan"]["units"] = [u for u in _fleet["scan"]["units"] if u["online"] or u["host"] != host]
    return fleet_report()


def act_settings_save(body):
    """Save the Settings page. A new player resets every cache and the monitor."""
    if CONFIG["from_cli"] and body.get("player") and body["player"] != CONFIG["master"]:
        raise ValueError("This server was started with --master; restart it without that to change the player here.")
    old = CONFIG["master"]
    st = fleet.save_settings(body)
    if not CONFIG["from_cli"]:
        CONFIG["master"] = st["player"] or None
    if CONFIG["master"] != old:
        with _fleet["lock"]:
            _fleet.update(scan=None, data=None, t=0)
        _mon.update(online=None, misses={}, show=None, last=None)
    return settings_view()


def settings_view():
    inv = fleet.load_inventory()
    return {"settings": fleet.load_settings(), "active_player": CONFIG["master"], "from_cli": CONFIG["from_cli"],
            "inventory_count": len((inv or {}).get("controllers", [])), "inventory": inv is not None,
            "policy": fleet.POLICY.exists(),
            "files": {"settings": str(fleet.SETTINGS.relative_to(ROOT)), "inventory": str(fleet.INVENTORY.relative_to(ROOT)),
                      "policy": str(fleet.POLICY.relative_to(ROOT))}}


def act_settings_test(body):
    """Check that an address is an FPP player and how many controllers it knows. Read-only."""
    addr = str(body.get("player", "")).strip()
    fleet.validate_settings({"player": addr})
    if not addr:
        raise ValueError("enter an address first")
    try:
        info = fleet.fetch_json(f"http://{addr}/api/system/info", timeout=6, tries=1)
    except Exception as e:
        raise ValueError(f"No FPP answered at {addr} ({str(e)[:60]})")
    try:
        systems = fleet.fetch_json(f"http://{addr}/api/fppd/multiSyncSystems", timeout=8, tries=1).get("systems", [])
        known = sorted({x.get("hostname") for x in systems if re.match(r"^\d+\.\d+\.\d+\.\d+$", x.get("address", ""))} - {info.get("HostName")})
    except Exception:
        known = []
    return {"host": info.get("HostName"), "version": info.get("Version"), "platform": info.get("Platform"),
            "mode": info.get("Mode"), "controllers": known}


def act_inventory_rebuild(body):
    """Replace fleet-inventory.json with every controller the player can reach now."""
    units = [u for u in fleet.discover(need_player()) if u["online"]]
    if not units:
        raise ValueError("no controllers answered")
    fleet.save_inventory({"controllers": [{"host": u["host"], "ip": u["ip"],
                                           "role": "player" if u["is_master"] else "remote"} for u in units]})
    with _fleet["lock"]:
        _fleet.update(scan=None, data=None, t=0)
    return settings_view()


def act_fleet_restart(body):
    hosts = [h for h in body.get("hosts", []) if re.match(r"^[\w\-]+$", h)]
    if not hosts:
        raise ValueError("no hosts")
    return start_job("fleet", f"Restart players on {', '.join(hosts)}",
                     [[PY, TOOLS / "fleet.py", "--master", CONFIG["master"], "restart", "--hosts", ",".join(hosts)]],
                     exclusive="fleet", after=rescan_after)


def xlights_view():
    """xLights on this computer, and where each sequence the schedule needs is in
    its show folder. Read-only."""
    try:
        st = xlights.status()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    names = ((_fleet["scan"] or {}).get("coverage") or {}).get("used_by") or {}
    seqs = {}
    for n in names:
        loc = xlights.local_sequence(st["show"], n)
        seqs[n] = None if not loc else {k: loc.get(k) for k in ("id", "frames", "stale")} | {
            "rendered": bool(loc.get("fseq")), "xsq": Path(loc["xsq"]).name}
    return {"ok": True, **st, "sequences": seqs}


def act_xlights_push(body):
    """Upload sequences to controllers through xLights: one step per sequence
    (to all of its controllers), run in turn; a failed one does not stop the
    rest. Ends with a rescan so the Sequences page shows the result."""
    want = {}
    for it in body.get("items", []):
        seq, host = str(it.get("seq", "")), str(it.get("host", ""))
        if not re.match(r"^[\w\-]+$", host) or not seq.endswith(".fseq") or "/" in seq or seq.startswith("."):
            raise ValueError(f"bad item: {it}")
        want.setdefault(seq, []).append(host)
    if not want:
        raise ValueError("nothing to push")
    master = need_player()
    steps = [[PY, TOOLS / "xlights.py", "--master", master, "push", seq, "--hosts", ",".join(hosts)]
             + (["--render"] if body.get("render") else []) for seq, hosts in want.items()]
    title = "; ".join(f"{seq} to {', '.join(hosts)}" for seq, hosts in want.items())
    return start_job("xlights", f"Push via xLights: {title}"[:160], steps,
                     exclusive="xlights", keep_going=True, after=rescan_after)


ACTIONS = {"snapshot": act_snapshot, "backup": act_backup, "download": act_download, "build": act_build, "flash": act_flash,
           "fleet/apply": act_fleet_apply, "fleet/expect": act_fleet_expect, "fleet/restart": act_fleet_restart,
           "fleet/preview": act_fleet_preview, "xlights/push": act_xlights_push, "fleet/policy": act_fleet_policy_save,
           "inventory": act_inventory, "inventory/rebuild": act_inventory_rebuild,
           "settings": act_settings_save, "settings/test": act_settings_test,
           "notifications/read": lambda b: notes_mark(b.get("ids")) or {"ok": True},
           "notifications/clear": lambda b: notes_mark(clear=True) or {"ok": True}}


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "FPPBackup"

    def log_message(self, fmt, *args):
        if not self.path.startswith("/api/jobs"):
            sys.stderr.write("%s %s\n" % (self.command, self.path))

    def local_host(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost")

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self.local_host():
            return self.send(403, {"error": "local access only"})
        path = self.path.split("?")[0]
        try:
            if path == "/":
                html = (STATIC / "index.html").read_text().replace("__TOKEN__", TOKEN)
                return self.send(200, html.encode(), "text/html; charset=utf-8")
            if path in ("/app.js", "/style.css"):
                ctype = "text/javascript" if path.endswith(".js") else "text/css"
                return self.send(200, (STATIC / path[1:]).read_bytes(), ctype + "; charset=utf-8")
            if path == "/api/controllers":
                return self.send(200, {"master": CONFIG["master"], "controllers": controllers()})
            if path == "/api/snapshots":
                return self.send(200, snapshots())
            if path == "/api/releases":
                return self.send(200, releases())
            if path == "/api/images":
                return self.send(200, {"stock": files_in(IMAGES, "*.img"), "built": files_in(OUT, "*.img")})
            if path == "/api/cards":
                return self.send(200, cards())
            if path == "/api/fleet":
                return self.send(200, fleet_report(refresh="refresh=1" in self.path))
            if path == "/api/settings":
                return self.send(200, settings_view())
            if path == "/api/xlights":
                return self.send(200, xlights_view())
            if path == "/api/notifications":
                items = sorted(_notes["items"], key=lambda n: -n["t"])
                return self.send(200, {"unread": sum(not n["read"] for n in items), "items": items[:60]})
            if path == "/api/show/live":
                return self.send(200, live_status())
            if path == "/api/dashboard":
                return self.send(200, {"report": fleet_report(refresh="refresh=1" in self.path),
                                       "backups": backups_by_host(), "now": time.time()})
            if path == "/api/fleet/policy":
                policy = load_policy()
                return self.send(200, {"policy": policy, "catalog": fleet.setting_catalog(_fleet["scan"], policy)
                                       if _fleet["scan"] else {}})
            if path == "/api/jobs":
                return self.send(200, [j.summary() for j in sorted(JOBS.values(), key=lambda j: -j.started)])
            m = re.match(r"^/api/jobs/(\w+)/stream$", path)
            if m and m.group(1) in JOBS:
                return self.stream(JOBS[m.group(1)])
            self.send(404, {"error": "not found"})
        except Exception as e:
            self.send(502, {"error": str(e)})

    def do_POST(self):
        if not self.local_host() or self.headers.get("X-Token") != TOKEN:
            return self.send(403, {"error": "forbidden"})
        m = re.match(r"^/api/([\w/]+)$", self.path)
        if not m or m.group(1) not in ACTIONS:
            return self.send(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            result = ACTIONS[m.group(1)](body)
            self.send(200, result.summary() if isinstance(result, Job) else result if result is not None else {"ok": True})
        except (KeyError, ValueError, RuntimeError) as e:
            self.send(400, {"error": str(e)})

    def stream(self, job):
        """Server-sent events: every line, then progress updates, then the final status."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        sent, last_progress = 0, None

        def event(kind, data):
            self.wfile.write(f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode())

        try:
            while True:
                with job.cond:
                    while (sent == len(job.lines) and job.progress == last_progress
                           and job.status == "running"):
                        if not job.cond.wait(15):
                            break
                    lines, progress, status = job.lines[sent:], job.progress, job.status
                for line in lines:
                    event("line", line)
                sent += len(lines)
                if progress != last_progress:
                    event("progress", progress)
                    last_progress = progress
                if status != "running" and sent == len(job.lines):
                    event("status", status)
                    self.wfile.flush()
                    return
                if not lines and progress == last_progress:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--master", default=os.environ.get("FPP_MASTER"),
                    help="the player's address for this run (default: the one saved on the Settings page)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-monitor", action="store_true", help="don't watch controllers in the background")
    args = ap.parse_args()
    CONFIG["from_cli"] = bool(args.master)
    CONFIG["master"] = args.master or fleet.load_settings()["player"] or None
    if not CONFIG["master"]:
        print("No player configured yet: open the UI and set it on the Settings page.", file=sys.stderr)

    _load_notes()
    if not args.no_monitor:
        threading.Thread(target=monitor_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"FPP-Backup UI on {url}  (player {CONFIG['master'] or 'not set'}); Ctrl-C to stop")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
