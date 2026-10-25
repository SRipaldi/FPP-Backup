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

Usage: python3 ui/server.py [--master 172.1.2.10] [--port 8765] [--no-browser]
"""
import argparse
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

TOKEN = secrets.token_urlsafe(24)
CONFIG = {"master": "172.1.2.10"}


# ---------------------------------------------------------------- jobs

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
    found = fleet.discover(CONFIG["master"])
    snaps = snapshots()
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

_fleet = {"t": 0, "data": None, "lock": threading.Lock()}


def fleet_report(refresh=False):
    """tools/fleet.py's drift report, cached: a full scan takes ~20 s."""
    with _fleet["lock"]:
        if refresh or _fleet["data"] is None or time.time() - _fleet["t"] > 300:
            _fleet["data"] = fleet.drift_report(CONFIG["master"])
            _fleet["t"] = time.time()
        return _fleet["data"]


def load_policy():
    return json.loads(fleet.POLICY.read_text())


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
    if master_host in drifted:
        steps.append(base + [rule.get("master_value", rule["value"]), "--hosts", master_host])
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
    rep = _fleet["data"]
    if not rep:
        raise ValueError("the fleet has not been scanned yet")
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
    """Make a value the expected one (or promote a compared setting into the policy).
    Edits fleet-policy.json only; nothing is sent to a controller."""
    name, value = body["setting"], str(body["value"])
    if not re.match(r"^[\w\-]+$", name) or len(value) > 200 or "\n" in value:
        raise ValueError("bad setting or value")
    policy = load_policy()
    rep = _fleet["data"] or {}
    known = {r["name"] for k in ("match", "compare", "other") for r in rep.get(k, [])}
    if name not in policy["match"] and name not in known:
        raise ValueError(f"unknown setting {name}")
    rule = policy["match"].setdefault(name, {"why": "Added from the Fleet page"})
    rule["value"] = value
    if name in policy["compare"]:
        policy["compare"].remove(name)
    fleet.POLICY.write_text(json.dumps(policy, indent=2) + "\n")
    _fleet["t"] = 0
    return None


def act_fleet_restart(body):
    hosts = [h for h in body.get("hosts", []) if re.match(r"^[\w\-]+$", h)]
    if not hosts:
        raise ValueError("no hosts")
    return start_job("fleet", f"Restart players on {', '.join(hosts)}",
                     [[PY, TOOLS / "fleet.py", "--master", CONFIG["master"], "restart", "--hosts", ",".join(hosts)]],
                     exclusive="fleet", after=rescan_after)


ACTIONS = {"snapshot": act_snapshot, "download": act_download, "build": act_build, "flash": act_flash,
           "fleet/apply": act_fleet_apply, "fleet/expect": act_fleet_expect, "fleet/restart": act_fleet_restart}


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
            job = ACTIONS[m.group(1)](body)
            self.send(200, job.summary() if job else {"ok": True})
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
    ap.add_argument("--master", default=os.environ.get("FPP_MASTER", CONFIG["master"]))
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    CONFIG["master"] = args.master
    try:
        CONFIG["master_host"] = fetch_json(f"http://{args.master}/api/system/info").get("HostName")
    except Exception:
        print(f"warning: player {args.master} not reachable", file=sys.stderr)

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"FPP-Backup UI on {url}  (player {args.master}); Ctrl-C to stop")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
