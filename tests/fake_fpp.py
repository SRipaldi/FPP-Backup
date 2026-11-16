"""A pretend FPP controller for testing tools/upgrade.py without a real one.

Answers the endpoints an in-place upgrade uses: status and info, update
progress (updateActivity), upgradefpp.php and upgradeOS.php (which take a
couple of seconds instead of minutes), reboot (it stops answering for a
moment, then reports a fresh uptime), uploads, and the "before/after"
reads. `mode` makes it misbehave: "fail", "stall", "lose-sequence", "no-return",
"startup-fails-once" (FPP's startup times out on the first boot after the
upgrade, as seen on a PocketBeagle2), "startup-fails" (on every boot),
"fppd-down", "fpp-restart", "os-blip" (two progress reads fail mid-upgrade),
"reboot-refused" (the reboot request returns 500 and nothing reboots).
"""
import json
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeFPP:
    def __init__(self, platform="Raspberry Pi", sub="Raspberry Pi 4 Model B", os_version="v2026-10b (64bit)",
                 internet=True, mode=None):
        self.version, self.platform, self.sub, self.os_version = "10.3", platform, sub, os_version
        self.internet, self.mode = internet, mode
        self.boot, self.down_until, self.playing, self.next_show = time.time() - 3600, 0, False, None
        self.sequences = ["a.fseq", "b.fseq"]
        self.uploads = {}
        self.act = {"inProgress": False, "op": "", "runId": "old", "stage": "", "logSize": 100, "failed": False}
        self.fppd_state = "running"   # fppd/status reports this; a reboot can leave it "stopped" (see mode fppd-down)
        self.activity_503 = False     # when set, updateActivity 503s, as fppd does while an fpp update restarts it
        self.activity_misses = 0      # the next N updateActivity reads 503 (a busy board, a slow proxy)
        self.root_free = 30e9
        self.systems = []             # what /api/fppd/multiSyncSystems lists
        self.requests = []
        self.syslog = ["systemd[1]: Starting fppinit.service - Inialization routines for FPP...",
                       "systemd[1]: Started fppinit.service - Inialization routines for FPP."]
        self.reboots = 0
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.addr = f"127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()

    def unit(self, host="FakeUnit"):
        return {"host": host, "ip": "127.0.0.1", "base": f"http://{self.addr}", "online": True, "is_master": False}

    # ---- behaviour

    def reboot(self, after=0.0, down=1.0):
        # Decide the boot's outcome synchronously, in call order, so the "fails once" mode can't race itself when
        # two reboots happen close together (seen under the loaded full-suite run). Only the down-then-back timing
        # is deferred to the thread; the syslog is only readable once the box is back up, so appending it now is safe.
        if self.mode == "no-return":
            def go_nr():
                time.sleep(after)
                self.down_until = time.time() + 3600
            threading.Thread(target=go_nr, daemon=True).start()
            return
        self.reboots += 1
        fails = self.mode == "startup-fails" or (self.mode == "startup-fails-once" and self.reboots == 1)
        self.syslog += ["systemd[1]: Starting fppinit.service - Inialization routines for FPP...",
                        "systemd[1]: fppinit.service: start operation timed out. Terminating." if fails else
                        "systemd[1]: Started fppinit.service - Inialization routines for FPP."]

        def go():
            time.sleep(after)
            self.down_until = time.time() + down
            self.boot = time.time() + down
            if self.mode == "lose-sequence":
                self.sequences = self.sequences[:1]
        if self.mode == "fppd-down":
            self.fppd_state = "stopped"   # it rebooted and is back on the network, but fppd never comes up
        threading.Thread(target=go, daemon=True).start()

    def run_upgrade(self, op, target, stages, reboots):
        self.act.update(inProgress=True, op=op, runId=f"run{time.time()}", stage="", failed=False)
        lines = []

        def go():
            for st in stages:
                time.sleep(0.4)
                if self.mode == "stall":
                    self.act["stage"] = "Building FPP"
                    return
                self.act["stage"] = st
                self.act["logSize"] += 50
                lines.append(st)
                if self.mode == "os-blip" and st == stages[0]:
                    self.activity_misses = 2
            if self.mode == "fail":
                self.act.update(inProgress=False, failed=True, stage="Upgrade Failed")
                return
            if self.mode == "fpp-restart":
                # the fpp update applied, then took fppd (and its progress feed) down to restart its services
                # and did NOT reboot on its own — the feed just goes quiet; the tool must reboot to finish.
                self.version = target
                self.activity_503 = True
                return
            self.version = target
            if reboots:
                self.act.update(stage="Rebooting", inProgress=False)
                self.reboot(0.2)
            else:
                self.act.update(stage="Upgrade Complete", inProgress=False)
        t = threading.Thread(target=go, daemon=True)
        t.start()
        return t, lines

    # ---- HTTP

    def _handler(fake):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send_json(self, obj, code=200):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def down(self):
                if time.time() < fake.down_until:
                    self.send_response(503)
                    self.end_headers()
                    return True
                return False

            def do_GET(self):
                fake.requests.append(("GET", self.path))
                if self.down():
                    return
                path, _, q = self.path.partition("?")
                if path == "/api/system/updateActivity" and fake.activity_misses > 0:
                    fake.activity_misses -= 1
                    self.send_response(503)
                    self.end_headers()
                    return
                if path == "/api/system/updateActivity" and fake.activity_503:
                    self.send_response(503)
                    self.end_headers()
                    return
                up = int(time.time() - fake.boot)
                routes = {
                    "/api/system/info": {"HostName": "FakeUnit", "Version": fake.version, "Branch": "v" + fake.version,
                                         "Mode": "remote", "Platform": fake.platform, "SubPlatform": fake.sub,
                                         "OSVersion": fake.os_version,
                                         "Utilization": {"Disk": {"Root": {"Free": fake.root_free, "Total": 32e9}}}},
                    "/api/fppd/multiSyncSystems": {"systems": fake.systems},
                    "/api/fppd/status": {"status_name": "playing" if fake.playing else "idle", "fppd": fake.fppd_state,
                                         "systemUptimeTotalSeconds": up,
                                         "scheduler": {"nextPlaylist": {"playlistName": "Show", "scheduledStartTime": fake.next_show}
                                                       if fake.next_show else {}}},
                    "/api/system/updateActivity": fake.act,
                    "/api/system/updateStatus": {"checked": fake.internet, "versionUnknown": False, "isMajorVersionUpgrade": False},
                    "/api/files/sequences": {"files": [{"name": n} for n in fake.sequences]},
                    "/api/files/Uploads": {"files": [{"name": n, "sizeBytes": s} for n, s in fake.uploads.items()]},
                    "/api/playlists": ["Show"],
                    "/api/network/interface": [{"ifname": "eth0", "addr_info": [{"family": "inet", "local": "192.168.1.9"}]}],
                    "/api/network/gateway": {"GATEWAY": "192.168.1.1"},
                    "/api/fppd/warnings": [],
                    "/api/settings": {"settings": {"TimeZone": {"type": "text"}, "Volume": {"type": "number"}}},
                    "/api/settings/TimeZone": {"value": "America/Los_Angeles"},
                    "/api/settings/Volume": {"value": 70},
                    "/api/channel/output/channelOutputsJSON": {"channelOutputs": []},
                    "/api/channel/output/co-other": {"channels": [{"type": "x"}]},
                }
                if path in routes:
                    return self.send_json(routes[path])
                if path == "/api/file/Logs/var/log/syslog":
                    b = "".join(f"2026-11-09T15:01:46-08:00 FakeUnit {ln}\n" for ln in fake.syslog).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(b)))
                    self.end_headers()
                    return self.wfile.write(b)
                if path == "/api/file/Logs/fppd.log":
                    b = b"2026-11-09 10:00:00.000 fppd(1) [General] fppd.cpp:1: Starting\n"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(b)))
                    self.end_headers()
                    return self.wfile.write(b)
                self.send_json({"status": "not found"}, 404)

            def do_POST(self):
                fake.requests.append(("POST", self.path))
                if self.down():
                    return
                path, _, q = self.path.partition("?")
                args = dict(urllib.parse.parse_qsl(q))
                n = int(self.headers.get("Content-Length") or 0)
                if path == "/jqupload.php":
                    body = self.rfile.read(n)
                    m = re.search(rb'filename="([^"]+)"\r\n[^\r]*\r\n\r\n', body)
                    data = body[m.end():body.rindex(b"\r\n--")]
                    fake.uploads[m.group(1).decode()] = len(data)
                    return self.send_json(["ok"])
                self.rfile.read(n)
                if path == "/api/system/reboot" and fake.mode == "reboot-refused":
                    return self.send_json({"status": "error"}, 500)
                if path == "/api/system/reboot":
                    fake.activity_503 = False   # the reboot brings fppd (and its feed) back
                    fake.reboot(0.2)
                    return self.send_json({"status": "OK"})
                if path in ("/upgradefpp.php", "/upgradeOS.php"):
                    if path == "/upgradeOS.php" and not args["os"].startswith("http") and args["os"] not in fake.uploads:
                        return self.send_json({"error": "no such image"}, 404)
                    target = args.get("version") or re.search(r"-(\d+\.\d+)", args["os"]).group(1)
                    t, lines = fake.run_upgrade("fpp-upgrade" if "fpp" in path else "os-upgrade", target,
                                                ["Fetching", "Building FPP", "Done"], reboots=path == "/upgradeOS.php")
                    self.send_response(200)
                    self.end_headers()
                    t.join(5)
                    self.wfile.write(("\n".join(lines) + "\n").encode())
                    return
                self.send_json({"status": "not found"}, 404)

            def do_DELETE(self):
                fake.requests.append(("DELETE", self.path))
                m = re.match(r"^/api/file/Uploads/(.+)$", self.path)
                if m:
                    fake.uploads.pop(urllib.parse.unquote(m.group(1)), None)
                    return self.send_json({"status": "OK"})
                self.send_json({"status": "not found"}, 404)
        return H
