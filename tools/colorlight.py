#!/usr/bin/env python3
"""ColorLight receiver-card health, read from each controller's own logs.

A ColorLight 5A-75 needs a clean gigabit link from the controller. A damaged
pair, jack or coupler shows up in the controller's kernel log as the link
dropping and coming back, often at 100 Mbps; fppd then cannot keep up, and if
fppd starts while the link is down it finds no receiver and leaves the panel
dark until fppd is restarted (it only searches for receivers at start). This
module finds every controller with a ColorLight output, reads the newest part
of its syslog and fppd.log (the `tail` option of /api/file, so only new lines
travel), and keeps the events in work/cache/colorlight.json so history
survives log rotation. Read-only against the controllers.

Usage: python3 tools/colorlight.py [--master <player-ip>] [--host <name>]
"""
import argparse
import json
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "work/cache/colorlight.json"
CHECK_EVERY = 300          # seconds between log reads of a ColorLight controller
DISCOVER_EVERY = 1800      # seconds between output-config reads of the others
KEEP_DAYS = 14
MAX_EVENTS = 6000          # per controller; send errors can be thousands a day on a bad link

_lock = threading.Lock()

# Kernel link messages: bcmgenet (Pi), cpsw (BeagleBone) and most PHY drivers
# print "Link is Down" / "Link is Up - 1Gbps/Full"; USB adapters (r8152) print
# "carrier off" / "carrier on" without a speed.
LINK_RE = re.compile(r"\b(eth\d+|en\w+): (?:(Link is Down|carrier off)|(?:Link is Up - (\d+)([GM])bps|carrier on))")
FPPD_RULES = [  # (kind, pattern); the first match wins
    ("init", re.compile(r"Calling Init\(\) on ColorLight")),
    ("found", re.compile(r"Found (\d+) ColorLight receiver")),
    ("noreceiver", re.compile(r"No receiver cards were detected on interface (\S+)")),
    ("nolink", re.compile(r"ColorLight: Configured interface (\S+) does not have link")),
    ("notgig", re.compile(r"ColorLight: Configured interface (\S+) is not 1000Mbps")),
    ("initfail", re.compile(r"Could not initialize output type ColorLight")),
    ("framedrop", re.compile(r"ColorLight: Interface (\S+) is not keeping up, (\d+) frame")),
    ("senderr", re.compile(r"sendmmsg\(\) failed for ColorLight")),
    ("slowsend", re.compile(r"Repeated frames taking more than \d+ms to send to ColorLight")),
]
RECEIVER_RE = re.compile(r"- (Firmware Version|Cabinet Size \(WxH\)|Uptime)\s*: (\S+)")


# ---------------------------------------------------------------- store

def load_store():
    try:
        return json.loads(STORE.read_text())
    except (FileNotFoundError, ValueError):
        return {"units": {}}


def save_store(store):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(store))


# ---------------------------------------------------------------- reading

def base_of(u, master):
    if u.get("base"):
        return u["base"]
    if u.get("is_master"):
        return f"http://{master}"
    return f"http://{u['via']}/proxy/{u['ip']}" if u.get("via") else f"http://{u['ip']}"


def outputs(base):
    """The controller's enabled ColorLight outputs: [{interface, link_check, type}]."""
    try:
        cfg = fleet.fetch_json(f"{base}/api/channel/output/channelOutputsJSON", timeout=8)
    except Exception as e:
        if "404" in str(e):  # no LED panel config at all
            return []
        raise
    return [{"interface": o.get("interface") or "eth0", "link_check": bool(o.get("linkCheck")),
             "type": o.get("subType")}
            for o in cfg.get("channelOutputs", [])
            if o.get("type") == "LEDPanelMatrix" and str(o.get("subType", "")).startswith("ColorLight")
            and o.get("enabled") in (1, "1", True)]


def tail(base, path, lines):
    b = fleet.fetch(f"{base}/api/file/{path}?tail={lines}", timeout=40)
    return b.decode("utf-8", "replace") if isinstance(b, bytes) else b


def syslog_time(line, tz):
    """Epoch and UTC offset of a syslog line: RFC 3339 (Debian 12+) or the
    traditional "Oct 30 14:06:08" (taken in the given offset, this year)."""
    m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?[+-]\d\d:\d\d) ", line)
    if m:
        d = datetime.fromisoformat(m.group(1))
        return d.timestamp(), d.utcoffset().total_seconds()
    m = re.match(r"^(\w{3} [ \d]\d \d\d:\d\d:\d\d) ", line)
    if m:
        now = time.time()
        d = datetime.strptime(f"{datetime.now().year} {m.group(1)}", "%Y %b %d %H:%M:%S")
        t = d.replace(tzinfo=timezone.utc).timestamp() - tz
        return (t - 365 * 86400 if t > now + 86400 else t), tz
    return None, tz


def parse_syslog(text, tz):
    """Link events from the kernel's lines, the newest line's time, and the
    controller's UTC offset (fppd.log times are local and carry none)."""
    events, first, last = [], None, None
    for line in text.splitlines():
        t, tz = syslog_time(line, tz)
        if t is None:
            continue
        first = first if first is not None else t
        last = t
        if "kernel" not in line:
            continue
        m = LINK_RE.search(line)
        if m:
            if m.group(2):
                events.append({"t": t, "k": "down", "if": m.group(1)})
            else:
                mbps = int(m.group(3)) * (1000 if m.group(4) == "G" else 1) if m.group(3) else None
                events.append({"t": t, "k": "up", "if": m.group(1), "v": mbps})
    return events, first, last, tz


def parse_fppd(text, tz):
    """ColorLight events from fppd.log (local times, shifted by tz to epoch)."""
    events, first, last, receiver = [], None, None, None
    for line in text.splitlines():
        m = re.match(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d+)?) ", line)
        if not m:
            continue
        t = datetime.fromisoformat(m.group(1)).replace(tzinfo=timezone.utc).timestamp() - tz
        first = first if first is not None else t
        last = t
        if "ColorLight" not in line and "Receiver" not in line and receiver is None:
            continue
        r = RECEIVER_RE.search(line)
        if r and receiver is not None:
            receiver["v"][{"Firmware Version": "fw", "Cabinet Size (WxH)": "size", "Uptime": "uptime_ms"}[r.group(1)]] = r.group(2)
            continue
        if receiver is not None:
            events.append(receiver)
            receiver = None
        if "Found Receiver #" in line:
            receiver = {"t": t, "k": "receiver", "v": {}}
            continue
        for kind, rx in FPPD_RULES:
            m = rx.search(line)
            if m:
                e = {"t": t, "k": kind}
                if kind == "found":
                    e["v"] = int(m.group(1))
                elif kind == "framedrop":
                    e.update({"if": m.group(1), "v": int(m.group(2))})
                elif m.groups():
                    e["if"] = m.group(1)
                events.append(e)
                break
    if receiver is not None:
        events.append(receiver)
    return events, first, last


def read_log(base, path, seen, parse, first_lines, more_lines, older=()):
    """Parse the newest lines of a log; read further back when the short tail
    does not reach the last line already seen (a busy log, or the first read).
    On the first read the rotated copies that are still plain text are read too.
    Returns (events, time of the oldest line read, time of the newest line, ...)."""
    text = tail(base, path, first_lines if seen else more_lines)
    out = parse(text)
    if seen and out[1] is not None and out[1] > seen:
        out = parse(tail(base, path, more_lines))
    for name in older if not seen else ():
        try:
            prev = parse(tail(base, name, more_lines))
        except Exception:  # gone, or compressed (.gz): history then starts later
            break
        out = (prev[0] + out[0], prev[1] if prev[1] is not None else out[1]) + out[2:]
    return out


def merge(old, new):
    seen = {(round(e["t"], 3), e["k"], e.get("if")) for e in old}
    out = old + [e for e in new if (round(e["t"], 3), e["k"], e.get("if")) not in seen]
    cutoff = time.time() - KEEP_DAYS * 86400
    out = sorted((e for e in out if e["t"] >= cutoff), key=lambda e: e["t"])
    return out[-MAX_EVENTS:]


def check_unit(u, master, rec):
    """Refresh one controller's record (outputs, events, live link and warnings)."""
    base = base_of(u, master)
    rec["checked"] = time.time()
    rec["error"] = None
    rec["outputs"] = outputs(base)
    if not rec["outputs"]:
        return rec
    tz = rec.get("tz", -time.timezone)
    ev, first, last, tz = read_log(base, "Logs/var/log/syslog", rec.get("syslog_seen"),
                               lambda s: parse_syslog(s, tz), 3000, 30000,
                               [f"Logs/var/log/syslog.{n}" for n in range(1, 8)])
    rec["tz"] = tz
    rec["syslog_seen"] = last or rec.get("syslog_seen")
    if first is not None:  # link history (the kernel's lines) starts here
        rec["link_from"] = min(first, rec.get("link_from") or first)
    fev, _, flast = read_log(base, "Logs/fppd.log", rec.get("fppd_seen"),
                             lambda s: parse_fppd(s, tz), 2000, 20000)
    rec["fppd_seen"] = flast or rec.get("fppd_seen")
    rec["events"] = merge(rec.get("events", []), ev + fev)
    ifaces = {o["interface"] for o in rec["outputs"]}
    try:
        rec["link"] = {i["ifname"]: {"up": i.get("operstate") == "UP" and "LOWER_UP" in i.get("flags", []),
                                     "state": i.get("operstate")}
                       for i in fleet.fetch_json(f"{base}/api/network/interface", timeout=8) if i.get("ifname") in ifaces}
    except Exception:
        rec["link"] = {}
    try:
        rec["warnings"] = [w for w in fleet.fetch_json(f"{base}/api/fppd/warnings", timeout=8)
                           if "colorlight" in str(w).lower()]
    except Exception:
        rec["warnings"] = []
    try:
        st = fleet.fetch_json(f"{base}/api/fppd/status", timeout=8)
        rec["boot"] = time.time() - int(st.get("systemUptimeTotalSeconds") or 0) if st.get("systemUptimeTotalSeconds") else None
        rec["status"] = st.get("status_name")
    except Exception:
        pass
    return rec


def check(units, master, force=False):
    """Check every online controller that is due. Returns the store."""
    with _lock:
        store = load_store()
        now = time.time()
        for u in units:
            if not u.get("online"):
                continue
            rec = store["units"].setdefault(u["host"], {})
            rec.update(ip=u.get("ip"), version=u.get("version"))
            due = CHECK_EVERY if rec.get("outputs") else DISCOVER_EVERY
            if not force and now - rec.get("checked", 0) < due:
                continue
            try:
                check_unit(u, master, rec)
            except Exception as e:
                rec["checked"] = now
                rec["error"] = str(e)[:160]
        for h, rec in store["units"].items():
            rec["online"] = any(u["host"] == h and u.get("online") for u in units) if units else rec.get("online")
        save_store(store)
        return store


# ---------------------------------------------------------------- report

def flaps(events, iface):
    """Link drops on an interface. A down followed by an up within a second is
    the boot-time reset, not a drop; a real drop takes 2-5 s to renegotiate."""
    link = [e for e in events if e["k"] in ("down", "up") and e.get("if") == iface]
    out = []
    for i, e in enumerate(link):
        if e["k"] != "down":
            continue
        nxt = link[i + 1] if i + 1 < len(link) else None
        if nxt and nxt["k"] == "up" and nxt["t"] - e["t"] < 1:
            continue
        out.append({"t": e["t"], "back": nxt["t"] if nxt and nxt["k"] == "up" else None,
                    "mbps": nxt.get("v") if nxt and nxt["k"] == "up" else None})
    return out


def unit_report(host, rec, now):
    """Health of one controller's ColorLight output(s): a level, the reasons,
    and the counts and history the page shows. Pure."""
    ev = rec.get("events", [])
    tz = rec.get("tz", 0)
    out = {"host": host, "ip": rec.get("ip"), "online": rec.get("online"), "checked": rec.get("checked"),
           "error": rec.get("error"), "status": rec.get("status"), "boot": rec.get("boot"),
           "warnings": rec.get("warnings", []), "outputs": []}
    issues = []
    for o in rec.get("outputs", []):
        i = o["interface"]
        drops = flaps(ev, i)
        ups = [e for e in ev if e["k"] == "up" and e.get("if") == i]
        live = (rec.get("link") or {}).get(i)
        speed = ups[-1].get("v") if ups else None
        since = lambda s: [d for d in drops if d["t"] >= now - s]  # noqa: E731
        slow_relinks = [d for d in drops if d["mbps"] and d["mbps"] < 1000]
        # the receiver search after fppd's latest start of this output
        starts = [e for e in ev if e["k"] == "init"]
        last_start = starts[-1]["t"] if starts else None
        after = [e for e in ev if last_start is not None and e["t"] >= last_start]
        found = next((e["v"] for e in reversed(after) if e["k"] == "found"), None)
        no_recv = any(e["k"] in ("noreceiver", "initfail") for e in after) and not found
        recv = [e for e in ev if e["k"] == "receiver"]
        # a receiver's uptime dates its power-on; a later power-on means the card restarted
        power = []
        for e in recv:
            try:
                p = e["t"] - int(e["v"].get("uptime_ms", "0").rstrip("ms")) / 1000
            except ValueError:
                continue
            if not power or abs(p - power[-1]) > 120:
                power.append(p)
        counts = {k: sum(1 for e in ev if e["k"] == k and e["t"] >= now - 86400 and e.get("if", i) == i)
                  for k in ("senderr", "slowsend")}
        counts["framedrop"] = sum(e.get("v", 1) for e in ev if e["k"] == "framedrop" and e["t"] >= now - 86400)
        link_from = max(rec.get("link_from") or now, now - KEEP_DAYS * 86400)
        today = int((now + tz) // 86400)
        # None: a day before the kernel log the monitor has seen, not a day without drops
        days = [0 if today - n >= int((link_from + tz) // 86400) else None for n in range(KEEP_DAYS - 1, -1, -1)]
        hours = [0] * 24
        for d in drops:
            day = today - int((d["t"] + tz) // 86400)
            if 0 <= day < KEEP_DAYS:
                days[KEEP_DAYS - 1 - day] += 1
            if d["t"] >= now - 7 * 86400:
                hours[int((d["t"] + tz) % 86400 // 3600)] += 1
        row = {"interface": i, "type": o.get("type"), "link_check": o["link_check"],
               "link_up": live["up"] if live else None, "speed": speed,
               "drops_1h": len(since(3600)), "drops_24h": len(since(86400)), "drops_7d": len(since(7 * 86400)),
               "slow_24h": len([d for d in slow_relinks if d["t"] >= now - 86400]),
               "last_drop": drops[-1]["t"] if drops else None, "history_from": rec.get("link_from"),
               "receivers": found, "no_receiver": no_recv, "last_start": last_start,
               "receiver": recv[-1]["v"] if recv else None,
               "card_power_on": power[-1] if power else None, "card_restarts": [p for p in power[1:] if p >= now - 7 * 86400],
               "counts": counts, "days": days, "hours": hours,
               "recent": [{"t": d["t"], "back": d["back"], "mbps": d["mbps"]} for d in drops[-8:]][::-1]}
        out["outputs"].append(row)
        name = f"{host} {i}"
        if live and not live["up"]:
            issues.append(("bad", "down", f"{name}: no link to the ColorLight card right now"))
        elif speed is not None and speed < 1000:
            issues.append(("bad", "slow", f"{name}: linked at {speed} Mbps; the card needs gigabit (a damaged pair, jack or cable)"))
        if no_recv:
            issues.append(("bad", "noreceiver", f"{name}: fppd found no receiver card when it last started; restart fppd once the link is steady"))
        if row["drops_1h"]:
            issues.append(("bad", "flapping", f"{name}: link dropped {row['drops_1h']} time(s) in the last hour"))
        elif row["drops_24h"]:
            issues.append(("warn", "drops", f"{name}: link dropped {row['drops_24h']} time(s) in the last 24 hours"))
        if row["card_restarts"] and row["card_restarts"][-1] >= now - 86400:
            issues.append(("warn", "restart", f"{name}: the card restarted (lost power or reset) in the last 24 hours"))
        if counts["framedrop"]:
            issues.append(("warn", "framedrop", f"{name}: {counts['framedrop']} frame(s) dropped in the last 24 hours: the link is not keeping up"))
        elif counts["senderr"] or counts["slowsend"]:
            issues.append(("warn", "send", f"{name}: {counts['senderr'] + counts['slowsend']} slow or failed sends in the last 24 hours"))
    for w in out["warnings"]:
        issues.append(("bad", "warning", f"{host}: FPP warning: {w}"))
    if out["online"] is False:
        issues = [("warn", "offline", f"{host} is offline: its ColorLight card can't be checked")]
    elif out["error"]:
        issues.append(("warn", "error", f"{host}: couldn't read its logs ({out['error']})"))
    out["issues"] = [{"level": lv, "code": c, "text": t} for lv, c, t in issues]
    out["level"] = "bad" if any(x[0] == "bad" for x in issues) else "warn" if issues else "ok"
    return out


def report(store=None, now=None):
    store = store if store is not None else load_store()
    now = now or time.time()
    units = [unit_report(h, r, now) for h, r in sorted(store["units"].items(), key=lambda kv: kv[0].lower())
             if r.get("outputs")]
    checked = [r.get("checked") for r in store["units"].values() if r.get("checked")]
    return {"units": units, "checked": max(checked) if checked else None,
            "scanned_units": len(store["units"]), "now": now}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--master", default=fleet.load_settings()["player"])
    ap.add_argument("--host", help="only this controller")
    args = ap.parse_args()
    if not args.master:
        sys.exit("no player: pass --master or set it on the Settings page")
    units = fleet.discover(args.master, fleet.load_inventory(), hosts={args.host} if args.host else None)
    rep = report(check(units, args.master, force=True))
    if not rep["units"]:
        print("No controller has an enabled ColorLight output.")
    for u in rep["units"]:
        print(f"{u['host']} ({u['ip']}): {u['level'].upper()}")
        for o in u["outputs"]:
            print(f"  {o['interface']}: link {'up' if o['link_up'] else 'down' if o['link_up'] is False else '?'}"
                  f" at {o['speed'] or '?'} Mbps, receivers {o['receivers']}, drops 1h/24h/7d "
                  f"{o['drops_1h']}/{o['drops_24h']}/{o['drops_7d']}, 100 Mbps relinks 24h {o['slow_24h']}")
        for i in u["issues"]:
            print(f"  - {i['level']}: {i['text']}")


if __name__ == "__main__":
    main()
