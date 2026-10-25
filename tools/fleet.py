#!/usr/bin/env python3
"""Discover every controller through the player and audit or change settings.

audit is read-only. set writes one setting per unit through FPP's own
PUT /api/settings/<name>, which also applies it (e.g. ntpServer rewrites
chrony.conf and restarts chrony), and only on the hosts named.

Usage:
  tools/fleet.py audit [--master <player-ip>] [--json]   # player defaults to config/settings.json
  tools/fleet.py drift [--json]               # every unit vs config/fleet-policy.json
  tools/fleet.py set ntpServer master --hosts FPP-Side,FPP-Roof
  tools/fleet.py set Latitude <latitude> --hosts all
  tools/fleet.py restart --hosts all          # fppd only; skips units that are playing
  tools/fleet.py reboot --hosts FPP-Perm
"""
import argparse
import concurrent.futures
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Per-installation settings (gitignored): which player is the source of truth,
# and app behaviour. Edited from the UI's Settings page.
SETTINGS = ROOT / "config/settings.json"
SETTINGS_DEFAULTS = {"player": "", "monitor": True, "monitor_interval": 60,
                     "update_check": True, "backup_max_age_days": 7}


def load_settings(path=SETTINGS):
    try:
        saved = json.loads(Path(path).read_text())
    except (FileNotFoundError, ValueError):
        saved = {}
    return {**SETTINGS_DEFAULTS, **{k: v for k, v in saved.items() if k in SETTINGS_DEFAULTS}}


def validate_settings(d):
    """Check and normalise settings from the UI. Raises ValueError."""
    out = dict(SETTINGS_DEFAULTS)
    player = str(d.get("player", "")).strip()
    if player and not re.match(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,252}$", player):
        raise ValueError("player must be an IP address or hostname")
    out["player"] = player
    out["monitor"] = bool(d.get("monitor", True))
    out["update_check"] = bool(d.get("update_check", True))
    try:
        out["monitor_interval"] = int(d.get("monitor_interval", 60))
        out["backup_max_age_days"] = int(d.get("backup_max_age_days", 7))
    except (TypeError, ValueError):
        raise ValueError("intervals must be whole numbers")
    if not 30 <= out["monitor_interval"] <= 3600:
        raise ValueError("monitor interval must be 30 to 3600 seconds")
    if not 1 <= out["backup_max_age_days"] <= 365:
        raise ValueError("backup reminder must be 1 to 365 days")
    return out


def save_settings(d, path=SETTINGS):
    d = validate_settings(d)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(d, indent=2) + "\n")
    return d
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


INVENTORY = ROOT / "config/fleet-inventory.json"


def load_inventory(path=INVENTORY):
    """Controllers that should exist: {"controllers": [{"host", "ip", "role", "notes"?}]}.
    None when there is no inventory file yet."""
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return None


def save_inventory(inv, path=INVENTORY):
    rows = []
    for c in inv.get("controllers", []):
        if not NAME_RE.match(c.get("host", "")) or not re.match(r"^\d+\.\d+\.\d+\.\d+$", c.get("ip", "")):
            raise ValueError(f"bad inventory entry: {c}")
        rows.append({k: c[k] for k in ("host", "ip", "role", "notes") if c.get(k)})
    rows.sort(key=lambda c: (c.get("role") != "player", c["host"].lower()))
    Path(path).write_text(json.dumps({"_comment": "Controllers that should exist. Discovery also probes these "
                                      "addresses, so a unit missing from the player's list still shows as offline.",
                                      "controllers": rows}, indent=2) + "\n")


def discover(master, inventory=None):
    """Every unit the player knows plus every unit in the inventory, verified by
    asking each who it is.

    The player is reached directly; others through its proxy, falling back to
    direct (the proxy cannot reach every subnet). The multisync list can be
    stale, so a unit only counts at an address that answers with its name. The
    player drops a unit from its list once it has been gone a while, so
    inventory addresses are probed too: an expected unit then shows as offline
    instead of vanishing. Each unit gets "expected" (in the inventory or not;
    None when there is no inventory)."""
    systems = fetch_json(f"http://{master}/api/fppd/multiSyncSystems", timeout=8).get("systems", [])
    by_host = {}
    for s in systems:
        if re.match(r"^\d+\.\d+\.\d+\.\d+$", s.get("address", "")):
            by_host.setdefault(s.get("hostname"), []).append(s["address"])
    expected = {c["host"]: c for c in (inventory or {}).get("controllers", [])}
    for host, c in expected.items():
        addrs = by_host.setdefault(host, [])
        if c["ip"] not in addrs:
            addrs.insert(0, c["ip"])

    def probe(host, addrs):
        routes = [(a, None) for a in addrs if a == master]
        routes += [(a, master) for a in addrs if a != master]
        routes += [(a, None) for a in addrs if a != master]
        for a, via in routes:
            base = f"http://{via}/proxy/{a}" if via else f"http://{a}"
            try:
                # The player's proxy is sometimes slow; one missed reply must not
                # make an expected unit look offline.
                info = fetch_json(f"{base}/api/system/info", timeout=6 if via else 3, tries=3)
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
    for u in units:
        u["expected"] = (u["host"] in expected) if inventory is not None else None
        if not u["online"] and u["host"] in expected:
            u["role"] = expected[u["host"]].get("role", "remote")
            u["is_master"] = u["role"] == "player"
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


def scan_unit(u, workers=4):
    """Every user setting on a unit (schema entries with a "type"), plus its default."""
    if not u["online"]:
        return {}
    schema = fetch_json(f"{u['base']}/api/settings", timeout=20).get("settings", {})
    names = [k for k, v in schema.items() if "type" in v]

    def one(k):
        try:
            return k, fetch_json(f"{u['base']}/api/settings/{urllib.parse.quote(k)}", timeout=10).get("value")
        except Exception:
            return k, None

    with concurrent.futures.ThreadPoolExecutor(workers) as ex:
        values = dict(ex.map(one, names))
    def default_of(e):
        # A select with no "default" marks one option's label "(Default)".
        if e.get("default") is None and e.get("type") == "select" and isinstance(e.get("options"), dict):
            return next((v for label, v in e["options"].items() if "(Default)" in str(label)), None)
        return e.get("default")

    return {k: {"value": values.get(k), "default": default_of(schema[k]), "type": schema[k].get("type"),
                "description": schema[k].get("description"),
                "restart": schema[k].get("restart"), "reboot": schema[k].get("reboot")} for k in names}


def ntp_for(u, units, master):
    """The player's address on the same /24 as this unit, else its main address."""
    m = next((x for x in units if x["is_master"]), None)
    if not m:
        return master
    net = u["ip"].rsplit(".", 1)[0]
    return next((a for a in m["addresses"] if a.rsplit(".", 1)[0] == net), master)


POLICY = ROOT / "config/fleet-policy.json"   # per installation (gitignored)
POLICY_TEMPLATE = ROOT / "templates/fleet-policy.json"


def create_policy(master, path=POLICY):
    """Write a policy from the template, filling '@player' values from the
    player's own settings (and fpp_version from its version), so a new fleet
    starts out matching its player. Read-only against the player."""
    tpl = json.loads(POLICY_TEMPLATE.read_text())
    info = fetch_json(f"http://{master}/api/system/info", timeout=8)
    schema = fetch_json(f"http://{master}/api/settings", timeout=20).get("settings", {})
    if tpl.get("fpp_version") == "@player":
        tpl["fpp_version"] = info.get("Version", "")
    for name, rule in list(tpl["match"].items()):
        if rule.get("value") != "@player":
            continue
        try:
            entry = fetch_json(f"http://{master}/api/settings/{urllib.parse.quote(name)}", timeout=8)
        except Exception:
            entry = None
        if not entry or name not in schema:
            del tpl["match"][name]  # this FPP version doesn't have it
            continue
        meta = schema[name]
        default = meta.get("default")
        if default is None and meta.get("type") == "select" and isinstance(meta.get("options"), dict):
            default = next((v for label, v in meta["options"].items() if "(Default)" in str(label)), None)
        rule["value"] = effective({"value": entry.get("value"), "default": default, "type": meta.get("type")})
    tpl["_comment"] = (f"Created from templates/fleet-policy.json using {info.get('HostName', master)}'s settings. "
                       "Edit it on the Fleet page (Edit policy).")
    policy = validate_policy(tpl)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(policy, indent=2) + "\n")
    return policy


def effective(entry):
    """A setting's value, or its schema default when unset, as a string."""
    if entry is None:
        return None
    v = entry["value"] if entry["value"] is not None else entry.get("default")
    if v is None and entry.get("type") == "checkbox":
        return "0"  # never saved: the checkbox is off
    return "" if v is None else str(v)


NAME_RE = re.compile(r"^[\w\-]+$")


def load_policy(path=POLICY):
    return validate_policy(json.loads(Path(path).read_text()))


def validate_policy(policy):
    """Check a policy's shape and return it normalised. Raises ValueError.

    A match rule is {"value", "player"?, "equivalent"?, "min_version"?, "why"?}.
    "player" is the player's own expected value when it differs from the
    remotes, or "*" to leave the player unchecked; the older "master_value"
    is read as "player"."""
    if not isinstance(policy, dict):
        raise ValueError("policy must be an object")
    secret = re.compile(policy.get("secret_pattern") or "pass|psk|secret|token|key", re.I)

    def text(v, what):
        if not isinstance(v, (str, int, float)) or isinstance(v, bool):
            raise ValueError(f"{what} must be text")
        v = str(v)
        if len(v) > 200 or "\n" in v:
            raise ValueError(f"{what} is too long or has a line break")
        return v

    def names(lst, what):
        if not isinstance(lst, list) or not all(isinstance(n, str) and NAME_RE.match(n) for n in lst):
            raise ValueError(f"{what} must be a list of setting names")
        return list(dict.fromkeys(lst))

    match = {}
    for name, rule in (policy.get("match") or {}).items():
        if not NAME_RE.match(name):
            raise ValueError(f"bad setting name {name!r}")
        if secret.search(name) and not name.endswith("Enable"):
            raise ValueError(f"{name} looks like a secret; it cannot be in the policy")
        if not isinstance(rule, dict) or "value" not in rule:
            raise ValueError(f"{name}: needs an expected value")
        r = {"value": text(rule["value"], f"{name} value")}
        player = rule.get("player", rule.get("master_value"))
        if player is not None:
            r["player"] = text(player, f"{name} player value")
        if rule.get("equivalent"):
            r["equivalent"] = [text(v, f"{name} equivalent") for v in rule["equivalent"]]
        if rule.get("min_version") not in (None, ""):
            r["min_version"] = int(rule["min_version"])
        if rule.get("why"):
            r["why"] = text(rule["why"], f"{name} note")
        match[name] = r
    out = {"_comment": policy.get("_comment", ""),
           "fpp_version": text(policy.get("fpp_version", ""), "fpp_version"), "match": match,
           "backup_max_age_days": int(policy.get("backup_max_age_days", 7)),
           "coverage_exclude": names(policy.get("coverage_exclude", []), "coverage_exclude"),
           "compare": [n for n in names(policy.get("compare", []), "compare") if n not in match],
           "per_unit": names(policy.get("per_unit", []), "per_unit"),
           "secret_pattern": secret.pattern}
    return out


def scan_fleet(master, check_updates=True):
    """Everything drift evaluation needs, from the controllers. Read-only, ~20 s."""
    inventory = load_inventory()
    units = discover(master, inventory)
    online = [u for u in units if u["online"]]
    # 3 units x 4 requests at a time: everything goes through the player's
    # proxy, and more than that made healthy units time out (and read as offline).
    with concurrent.futures.ThreadPoolExecutor(3) as ex:
        scans = dict(zip([u["host"] for u in online], ex.map(scan_unit, online)))
        audits = dict(zip([u["host"] for u in online], ex.map(audit_unit, online)))
    for u in online:
        try:
            info = fetch_json(f"{u['base']}/api/system/info")
            util = info.get("Utilization", {})
            u.update(os_version=info.get("OSVersion"), uptime=util.get("Uptime"),
                     branch=info.get("Branch"), git_local=info.get("LocalGitVersion"),
                     disk_free=(util.get("Disk", {}).get("Media", {}) or {}).get("Free"),
                     disk_total=(util.get("Disk", {}).get("Media", {}) or {}).get("Total"))
        except Exception:
            pass
        a = audits[u["host"]]
        u.update(clock=a.get("clock"), offset_s=a.get("offset_s"), status=a.get("status"))
        u.pop("base", None)
    for u in units:
        u["role"] = "player" if u.get("is_master") else "remote"
    try:
        updates = update_status(units) if check_updates else {}
    except Exception as e:  # GitHub unreachable: report, don't fail the scan
        updates = {"error": str(e)}
    for u in units:
        u["update"] = updates.get(u["host"]) if isinstance(updates, dict) else None
    player = next((u for u in units if u.get("is_master") and u["online"]), None)
    show = None
    if player:
        try:
            st = fetch_json(f"http://{master}/api/fppd/status")
            show = {"status": st.get("status_name"), "playlist": (st.get("current_playlist") or {}).get("playlist"),
                    "sequence": st.get("current_sequence"), "next": st.get("next_playlist")}
        except Exception:
            pass
    try:
        policy = load_policy()
        coverage = sequence_coverage(master, units, exclude=policy.get("coverage_exclude", []))
    except Exception as e:
        coverage = {"error": str(e)[:120]}
    return {"master": master, "scanned": time.time(), "units": units, "scans": scans, "coverage": coverage,
            "releases": updates.get("_releases") if isinstance(updates, dict) else None,
            "update_error": updates.get("error") if isinstance(updates, dict) else None,
            "show": show, "inventory": inventory is not None}


# ---------------------------------------------------------------- sequences

def playlist_sequences(base, name, seen=None):
    """Sequence files a playlist plays, following sub-playlists."""
    seen = seen if seen is not None else set()
    if name in seen:
        return set()
    seen.add(name)
    d = fetch_json(f"{base}/api/playlist/{urllib.parse.quote(name)}", timeout=10)
    out = set()
    for sec in ("leadIn", "mainPlaylist", "leadOut"):
        for e in d.get(sec) or []:
            if e.get("enabled") in (0, "0", False):
                continue
            if e.get("type") in ("both", "sequence") and e.get("sequenceName"):
                out.add(e["sequenceName"])
            elif e.get("type") == "playlist" and e.get("name"):
                out |= playlist_sequences(base, e["name"], seen)
    return out


def sequence_coverage(master, units, exclude=(), playlists=None):
    """Which remotes lack a sequence that a scheduled playlist plays.

    In MultiSync a remote plays its own copy of each .fseq by name; if the file
    is missing it simply stays dark. Checks the playlists that are enabled in
    the player's schedule (or the ones given). Sizes are not compared: xLights
    can upload per-controller copies that legitimately differ. Read-only."""
    base = f"http://{master}"
    if playlists is None:
        sched = fetch_json(f"{base}/api/fppd/schedule", timeout=10).get("schedule", {}).get("entries", [])
        playlists = sorted({e["playlist"] for e in sched if e.get("enabled") and e.get("playlist")})
    needed = {}
    for pl in playlists:
        for seq in playlist_sequences(base, pl):
            needed.setdefault(seq, []).append(pl)

    def have(u):
        b = f"http://{master}" if u["is_master"] else (
            f"http://{u['via']}/proxy/{u['ip']}" if u.get("via") else f"http://{u['ip']}")
        try:
            return u["host"], {f["name"] for f in fetch_json(f"{b}/api/files/sequences", timeout=15).get("files", [])}
        except Exception as e:
            return u["host"], e

    targets = [u for u in units if u.get("online") and u["host"] not in exclude]
    with concurrent.futures.ThreadPoolExecutor(6) as ex:
        files = dict(ex.map(have, targets))
    rows = []
    for u in targets:
        f = files[u["host"]]
        if isinstance(f, Exception):
            rows.append({"host": u["host"], "error": str(f)[:80]})
            continue
        missing = sorted(s for s in needed if s not in f)
        rows.append({"host": u["host"], "role": "player" if u["is_master"] else "remote",
                     "have": len(needed) - len(missing), "missing": missing})
    return {"playlists": playlists, "needed": len(needed), "units": rows, "used_by": needed,
            "excluded": sorted(set(exclude)), "checked": time.time()}


# ---------------------------------------------------------------- updates

GITHUB = "https://api.github.com/repos/FalconChristmas/fpp"
UPDATE_CACHE = ROOT / "work/cache/fpp-github.json"
UPDATE_TTL = 3600


def _github(path, cache):
    """GET from the FPP repo's API, cached for an hour (unauthenticated: 60/hour)."""
    hit = cache.get(path)
    if hit and time.time() - hit["t"] < UPDATE_TTL:
        return hit["data"]
    req = urllib.request.Request(f"{GITHUB}/{path}", headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)
    cache[path] = {"t": time.time(), "data": data}
    return data


def _vkey(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v or "0"))[:3])


def update_status(units):
    """Per unit: a newer FPP release than it runs, and newer commits on its own
    branch (patches). Checked from this machine, because most units can't reach
    GitHub themselves (their RemoteGitVersion is 'Unknown')."""
    try:
        cache = json.loads(UPDATE_CACHE.read_text())
    except (FileNotFoundError, ValueError):
        cache = {}
    releases = [r for r in _github("releases?per_page=15", cache) if not r["draft"] and not r["prerelease"]]
    latest = max(releases, key=lambda r: _vkey(r["tag_name"]))["tag_name"] if releases else None
    out = {"_releases": {"latest": latest, "published": next((r["published_at"] for r in releases
                                                              if r["tag_name"] == latest), None)}}
    for u in units:
        if not u.get("online"):
            continue
        info = {"latest_release": latest,
                "release_newer": bool(latest and _vkey(latest) > _vkey(u.get("version")))}
        branch, local = u.get("branch"), (u.get("git_local") or "").strip()
        if branch and re.match(r"^[0-9a-f]{7,40}$", local):
            try:
                head = _github(f"branches/{branch}", cache)["commit"]["sha"]
                if head.startswith(local):
                    info["patch_commits"] = 0
                else:
                    cmp = _github(f"compare/{local}...{head[:12]}", cache)
                    info["patch_commits"] = cmp.get("ahead_by", 0) if cmp.get("status") in ("ahead", "diverged") else 0
                info["branch_head"] = head[:9]
            except Exception as e:
                info["patch_error"] = str(e)[:80]
        out[u["host"]] = info
    UPDATE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    UPDATE_CACHE.write_text(json.dumps(cache))
    return out


def evaluate(scan, policy):
    """Compare a scan with a policy. Pure: no network, so a proposed policy can be
    previewed instantly against the last scan.

    Returns units, 'match' rows (expected value and status ok/equivalent/drift/
    exempt/n-a per unit), 'compare' rows, 'other' settings that differ, and
    'per_unit' rows. Secret settings are never included."""
    secret = re.compile(policy["secret_pattern"], re.I)
    master = scan["master"]
    units = [dict(u) for u in scan["units"]]
    scans = scan["scans"]
    online = [u for u in units if u["online"]]
    for u in units:
        u["version_ok"] = u.get("version") == policy.get("fpp_version") if u["online"] else None

    def major(u):
        try:
            return int(str(u.get("version", "0")).split(".")[0])
        except ValueError:
            return 0

    match_rows = []
    for name, rule in policy["match"].items():
        cells = {}
        for u in online:
            entry = scans[u["host"]].get(name)
            if rule.get("min_version") and major(u) < rule["min_version"]:
                cells[u["host"]] = {"value": None, "expected": None, "status": "n/a"}
                continue
            got = effective(entry)
            if u["is_master"] and rule.get("player") == "*":
                cells[u["host"]] = {"value": got, "expected": None, "status": "exempt"}
                continue
            expected = rule.get("player", rule["value"]) if u["is_master"] else rule["value"]
            if expected == "@master":
                expected = "" if u["is_master"] else ntp_for(u, units, master)
            if entry is None:
                status = "n/a"
            elif got == expected:
                status = "ok"
            elif got in rule.get("equivalent", []):
                status = "equivalent"
            else:
                status = "drift"
            cells[u["host"]] = {"value": got, "expected": expected, "status": status}
        meta = next((scans[h][name] for h in scans if name in scans[h]), {})
        match_rows.append({"name": name, "why": rule.get("why", ""), "expected": rule["value"],
                           "player": rule.get("player"), "description": meta.get("description", ""),
                           "needs": "reboot" if meta.get("reboot") else "restart" if meta.get("restart") else None,
                           "cells": cells, "drift": sum(c["status"] == "drift" for c in cells.values())})

    def role_rows(names, kind):
        rows = []
        for name in names:
            if secret.search(name):
                continue
            cells = {u["host"]: effective(scans[u["host"]].get(name)) for u in online}
            remotes = {v for u in online if not u["is_master"] for v in [cells[u["host"]]] if v is not None}
            if kind == "other" and len(set(v for v in cells.values() if v is not None)) < 2:
                continue
            desc = next((scans[h][name].get("description") for h in scans if name in scans[h]), "")
            rows.append({"name": name, "description": desc, "cells": cells,
                         "remotes_differ": len(remotes) > 1})
        return rows

    known = set(policy["match"]) | set(policy["compare"]) | set(policy["per_unit"])
    all_names = sorted(set().union(*[s.keys() for s in scans.values()])) if scans else []
    for u in units:
        u["drift_count"] = 0
    by_host = {u["host"]: u for u in units}
    for r in match_rows:
        for h, c in r["cells"].items():
            if c["status"] == "drift":
                by_host[h]["drift_count"] += 1
    return {
        "master": master, "policy": {"fpp_version": policy.get("fpp_version"),
                                     "backup_max_age_days": policy.get("backup_max_age_days", 7)},
        "scanned": scan["scanned"], "units": units,
        "releases": scan.get("releases"), "update_error": scan.get("update_error"),
        "show": scan.get("show"), "inventory": scan.get("inventory"), "coverage": scan.get("coverage"),
        "match": match_rows,
        "compare": role_rows(policy["compare"], "compare"),
        "other": role_rows([n for n in all_names if n not in known], "other"),
        "per_unit": role_rows([n for n in policy["per_unit"] if any(n in s for s in scans.values())], "per_unit"),
    }


def setting_catalog(scan, policy):
    """Every non-secret setting seen in a scan, with its description and the
    values units have, for the policy editor's pickers."""
    secret = re.compile(policy["secret_pattern"], re.I)
    out = {}
    for host, s in scan["scans"].items():
        for name, e in s.items():
            if secret.search(name) and not name.endswith("Enable"):
                continue
            c = out.setdefault(name, {"description": e.get("description") or "", "values": {}})
            v = effective(e)
            c["values"][v] = c["values"].get(v, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: kv[0].lower()))


def drift_report(master, policy_path=POLICY):
    """Scan every unit and compare it with the policy file. Read-only."""
    return evaluate(scan_fleet(master), load_policy(policy_path))


def cmd_drift(args):
    rep = drift_report(args.master)
    if args.json:
        print(json.dumps(rep, indent=2))
        return
    hosts = [u["host"] for u in rep["units"] if u["online"]]
    print("Settings that should match (policy):")
    for r in rep["match"]:
        bad = [f"{h}={c['value']!r}" for h, c in r["cells"].items() if c["status"] == "drift"]
        print(f"  {'DRIFT' if bad else 'ok   '} {r['name']:18} expected {r['expected']!r}"
              + (f": {', '.join(bad)}" if bad else ""))
    vers = [f"{u['host']}={u.get('version')}" for u in rep["units"] if u["online"] and not u["version_ok"]]
    print(f"  {'DRIFT' if vers else 'ok   '} {'FPP version':18} expected {rep['policy']['fpp_version']!r}"
          + (f": {', '.join(vers)}" if vers else ""))
    print("\nRemotes that disagree:")
    for r in rep["compare"] + rep["other"]:
        if r["remotes_differ"]:
            vals = {}
            for h in hosts:
                if h != next((u["host"] for u in rep["units"] if u.get("is_master")), None):
                    vals.setdefault(r["cells"].get(h), []).append(h)
            print(f"  {r['name']:26} " + "; ".join(f"{v!r}: {', '.join(hs)}" for v, hs in vals.items()))
    off = [u["host"] for u in rep["units"] if not u["online"]]
    if off:
        print(f"\nOffline: {', '.join(off)}")


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


def cmd_restart(args):
    """Restart fppd (or reboot) on named units, skipping any that are playing."""
    units = [u for u in discover(args.master) if u["online"]]
    wanted = None if args.hosts == "all" else set(args.hosts.split(","))
    targets = [u for u in units if wanted is None or u["host"] in wanted]
    path = "api/system/reboot" if args.cmd == "reboot" else "api/system/fppd/restart"
    failed = 0
    for u in targets:
        try:
            state = fetch_json(f"{u['base']}/api/fppd/status").get("status_name")
        except Exception as e:
            print(f"FAIL {u['host']:12} status: {e}")
            failed += 1
            continue
        if state != "idle" and not args.force:
            print(f"SKIP {u['host']:12} is {state}; not interrupting it (use --force)")
            continue
        try:
            try:
                fetch(f"{u['base']}/{path}", timeout=30, method="POST", data=b"")
            except urllib.error.HTTPError as e:
                if e.code not in (404, 405):
                    raise
                fetch(f"{u['base']}/{path}", timeout=30)  # FPP 8.x: GET only
            print(f"OK   {u['host']:12} {args.cmd} sent")
        except Exception as e:
            print(f"FAIL {u['host']:12} {e}")
            failed += 1
    sys.exit(1 if failed else 0)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--master", default=load_settings()["player"] or None,
                    help="the player's address (default: config/settings.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("audit")
    a.add_argument("--json", action="store_true")
    dr = sub.add_parser("drift", help="compare every unit with config/fleet-policy.json (read-only)")
    dr.add_argument("--json", action="store_true")
    s = sub.add_parser("set")
    s.add_argument("name")
    s.add_argument("value", help='value, or "master" for the player address on each unit\'s subnet')
    s.add_argument("--hosts", required=True, help='comma-separated hostnames, or "all"')
    s.add_argument("--skip-master", action="store_true")
    for name in ("restart", "reboot"):
        r = sub.add_parser(name, help=f"{'restart fppd' if name == 'restart' else 'reboot the OS'} on idle units")
        r.add_argument("--hosts", required=True, help='comma-separated hostnames, or "all"')
        r.add_argument("--force", action="store_true", help="also units that are playing")
    args = ap.parse_args()
    if not args.master:
        sys.exit("no player configured: pass --master <ip> or set it on the UI's Settings page")
    if args.cmd == "drift" and not POLICY.exists():
        create_policy(args.master)
        print(f"Created {POLICY.relative_to(ROOT)} from the template using the player's settings.", file=sys.stderr)
    {"audit": cmd_audit, "drift": cmd_drift, "set": cmd_set, "restart": cmd_restart,
     "reboot": cmd_restart}[args.cmd](args)


if __name__ == "__main__":
    main()
