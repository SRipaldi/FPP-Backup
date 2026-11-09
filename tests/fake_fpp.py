"""A pretend FPP controller for testing tools/upgrade.py without a real one.

Answers the endpoints an in-place upgrade uses: status and info, update
progress (updateActivity), upgradefpp.php and upgradeOS.php (which take a
couple of seconds instead of minutes), reboot (it stops answering for a
moment, then reports a fresh uptime), uploads, and the "before/after"
reads. `mode` makes it misbehave: "fail", "stall", "lose-sequence", "no-return".
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
        self.requests = []
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
        def go():
            time.sleep(after)
            if self.mode == "no-return":
                self.down_until = time.time() + 3600
                return
            self.down_until = time.time() + down
            self.boot = time.time() + down
            if self.mode == "lose-sequence":
                self.sequences = self.sequences[:1]
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
            if self.mode == "fail":
                self.act.update(inProgress=False, failed=True, stage="Upgrade Failed")
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
                up = int(time.time() - fake.boot)
                routes = {
                    "/api/system/info": {"HostName": "FakeUnit", "Version": fake.version, "Branch": "v" + fake.version,
                                         "Mode": "remote", "Platform": fake.platform, "SubPlatform": fake.sub,
                                         "OSVersion": fake.os_version,
                                         "Utilization": {"Disk": {"Root": {"Free": 30e9, "Total": 32e9}}}},
                    "/api/fppd/status": {"status_name": "playing" if fake.playing else "idle", "fppd": "running",
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
                if path == "/api/system/reboot":
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
