#!/usr/bin/env python3
"""Discover every controller through the player and audit or change settings.

audit is read-only. set writes one setting per unit through FPP's own
PUT /api/settings/<name>, which also applies it (e.g. ntpServer rewrites
chrony.conf and restarts chrony), and only on the hosts named.

Usage:
  tools/fleet.py audit [--master 172.1.2.10] [--json]
  tools/fleet.py set ntpServer master --hosts FPP-Side,FPP-Roof
  tools/fleet.py set Latitude <latitude> --hosts all
"""
import argparse
import concurrent.futures
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

DEFAULT_MASTER = "172.1.2.10"
# fppd status prints local time with a zone abbreviation; enough of them to
# compute a clock offset for the zones a show is likely to use.
TZ_ABBR = {"UTC": 0, "GMT": 0, "PST": -8, "PDT": -7, "MST": -7, "MDT": -6,
           "CST": -6, "CDT": -5, "EST": -5, "EDT": -4, "AKST": -9, "AKDT": -8,
           "HST": -10, "BST": 1, "CET": 1, "CEST": 2}
AUDIT_SETTINGS = ["TimeZone", "ntpServer", "Latitude", "Longitude"]


def fetch(url, timeout=6, method="GET", data=None):
    req = urllib.request.Request(url, method=method, data=data,
                                 headers={"Content-Type": "text/plain"} if data is not None else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_json(url, timeout=6, tries=3):
    # The player's proxy occasionally returns an empty body; retry.
    for i in range(tries):
        try:
            return json.loads(fetch(url, timeout))
        except ValueError:
            if i == tries - 1:
                raise
            time.sleep(1)


def discover(master):
    """Every unit the player knows, verified by asking each who it is.

    The player is reached directly; others through its proxy, falling back to
    direct (the proxy cannot reach every subnet). The multisync list can be
    stale, so a unit only counts at an address that answers with its name."""
    systems = fetch_json(f"http://{master}/api/fppd/multiSyncSystems", timeout=8).get("systems", [])
    by_host = {}
    for s in systems:
        if re.match(r"^\d+\.\d+\.\d+\.\d+$", s.get("address", "")):
            by_host.setdefault(s.get("hostname"), []).append(s["address"])

    def probe(host, addrs):
        routes = [(a, None) for a in addrs if a == master]
        routes += [(a, master) for a in addrs if a != master]
        routes += [(a, None) for a in addrs if a != master]
        for a, via in routes:
            base = f"http://{via}/proxy/{a}" if via else f"http://{a}"
            try:
                info = fetch_json(f"{base}/api/system/info", timeout=4 if via else 2, tries=2)
            except Exception:
                continue
            if info.get("HostName") and info["HostName"] != host:
                continue
            return {"host": info.get("HostName", host), "ip": a, "via": via, "base": base,
                    "online": True, "addresses": addrs, "version": info.get("Version"),
                    "platform": info.get("Platform"), "variant": info.get("SubPlatform") or info.get("Variant"),
                    "mode": info.get("Mode"), "cape": (info.get("capeInfo") or {}).get("name"),
                    "is_master": a == master}
        return {"host": host, "ip": addrs[0], "online": False, "addresses": addrs, "is_master": False}

    with concurrent.futures.ThreadPoolExecutor(8) as ex:
        units = list(ex.map(lambda kv: probe(*kv), by_host.items()))
    return sorted(units, key=lambda u: (not u["is_master"], u["host"].lower()))


def clock_offset(status_time):
    """Seconds the unit's clock is ahead of this machine's, from fppd's time string."""
    m = re.match(r"^\w+ (\w+ +\d+ \d+:\d+:\d+) (\w+) (\d{4})$", status_time or "")
    if not m or m.group(2) not in TZ_ABBR:
        return None
    local = datetime.strptime(f"{m.group(1)} {m.group(3)}", "%b %d %H:%M:%S %Y")
    utc = (local - timedelta(hours=TZ_ABBR[m.group(2)])).replace(tzinfo=timezone.utc)
    return round((utc - datetime.now(timezone.utc)).total_seconds())


def audit_unit(u):
    row = {k: u.get(k) for k in ("host", "ip", "version", "platform", "is_master", "online")}
    if not u["online"]:
        return row
    base = u["base"]
    for k in AUDIT_SETTINGS:
        try:
            d = fetch_json(f"{base}/api/settings/{k}")
            row[k] = d.get("value")
            row[k + "_default"] = d.get("default")
        except Exception as e:
            row[k] = f"<{e}>"
    try:
        st = fetch_json(f"{base}/api/fppd/status")
        row["clock"] = st.get("time")
        row["offset_s"] = clock_offset(st.get("time"))
        row["status"] = st.get("status_name")
    except Exception as e:
        row["clock"] = f"<{e}>"
    return row


def ntp_for(u, units, master):
    """The player's address on the same /24 as this unit, else its main address."""
    m = next((x for x in units if x["is_master"]), None)
    if not m:
        return master
    net = u["ip"].rsplit(".", 1)[0]
    return next((a for a in m["addresses"] if a.rsplit(".", 1)[0] == net), master)


def cmd_audit(args):
    units = discover(args.master)
    with concurrent.futures.ThreadPoolExecutor(8) as ex:
        rows = list(ex.map(audit_unit, units))
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    print(f"{'unit':12} {'FPP':6} {'clock offset':>12}  {'TimeZone':20} {'ntpServer':16} location")
    for r in rows:
        if not r["online"]:
            print(f"{r['host']:12} offline ({r['ip']})")
            continue
        off = r.get("offset_s")
        loc = f"{r.get('Latitude')}, {r.get('Longitude')}"
        if r.get("Latitude") == r.get("Latitude_default") and r.get("Longitude") == r.get("Longitude_default"):
            loc += "  (FPP default)"
        print(f"{r['host'] + ('*' if r['is_master'] else ''):12} {str(r['version']):6} "
              f"{(f'{off:+d}s' if off is not None else '?'):>12}  {str(r.get('TimeZone')):20} "
              f"{str(r.get('ntpServer') or '(internet pool)'):16} {loc}")
    print("* = player. Offset is the unit's clock minus this Mac's.")


def cmd_set(args):
    units = [u for u in discover(args.master) if u["online"]]
    wanted = None if args.hosts == "all" else set(args.hosts.split(","))
    targets = [u for u in units if wanted is None or u["host"] in wanted]
    if wanted:
        missing = wanted - {u["host"] for u in targets}
        if missing:
            sys.exit(f"not found or offline: {', '.join(sorted(missing))}")
    if args.skip_master:
        targets = [u for u in targets if not u["is_master"]]
    failed = 0
    for u in targets:
        value = ntp_for(u, units, args.master) if args.value == "master" else args.value
        try:
            fetch(f"{u['base']}/api/settings/{urllib.parse.quote(args.name)}", timeout=30,
                  method="PUT", data=value.encode())
            got = fetch_json(f"{u['base']}/api/settings/{urllib.parse.quote(args.name)}").get("value")
            ok = str(got) == value
            print(f"{'OK  ' if ok else 'FAIL'} {u['host']:12} {args.name} = {got!r}")
            failed += not ok
        except Exception as e:
            print(f"FAIL {u['host']:12} {e}")
            failed += 1
    sys.exit(1 if failed else 0)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--master", default=DEFAULT_MASTER)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("audit")
    a.add_argument("--json", action="store_true")
    s = sub.add_parser("set")
    s.add_argument("name")
    s.add_argument("value", help='value, or "master" for the player address on each unit\'s subnet')
    s.add_argument("--hosts", required=True, help='comma-separated hostnames, or "all"')
    s.add_argument("--skip-master", action="store_true")
    args = ap.parse_args()
    {"audit": cmd_audit, "set": cmd_set}[args.cmd](args)


if __name__ == "__main__":
    main()
