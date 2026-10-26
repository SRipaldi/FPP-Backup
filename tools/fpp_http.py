"""HTTP to FPP controllers, with their UI passwords from the macOS Keychain.

FPP's UI password (Settings > UI password) is HTTP Basic authentication, user
"admin", on the whole web server: the API is protected along with the pages.
Every tool reaches controllers through request() here, so they all send it.

Passwords live in the login Keychain, service "FPP-Backup" (FPP_BACKUP_KEYCHAIN
overrides it, for tests): account "default" for every controller, and
"host:<HostName>" for one controller (its address is looked up in the
inventory). Nothing is written to config/ or anywhere else on disk.

A request through the player's /proxy/<ip>/ carries one Authorization header:
the player checks it when it has a password, and its proxy forwards the same
header to the controller. So the controller's password is sent if one is
stored, else the default. When the player and a controller have different
passwords, the proxied request is refused and discovery reaches the controller
directly instead.
"""
import base64
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVICE = os.environ.get("FPP_BACKUP_KEYCHAIN", "FPP-Backup")
USER = "admin"
INVENTORY = ROOT / "config/fleet-inventory.json"

_cache = {}  # account -> password or None; Keychain lookups cost a process each


def available():
    return shutil.which("security") is not None


def keychain_get(account):
    if account not in _cache:
        pw = None
        if available():
            r = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-a", account, "-w"],
                               capture_output=True, text=True)
            pw = r.stdout.rstrip("\n") if r.returncode == 0 else None
        _cache[account] = pw
    return _cache[account]


def keychain_set(account, password):
    if not available():
        raise RuntimeError("the macOS Keychain (security command) is not available on this computer")
    # -U updates an existing item. The password goes on the command line, which
    # other processes of this user could see for an instant; that is how the
    # security tool takes it non-interactively.
    r = subprocess.run(["security", "add-generic-password", "-U", "-s", SERVICE, "-a", account,
                        "-l", f"FPP-Backup ({account})", "-w", password], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"Keychain refused the password: {r.stderr.strip()[:120]}")
    _cache[account] = password


def keychain_delete(account):
    if available():
        subprocess.run(["security", "delete-generic-password", "-s", SERVICE, "-a", account],
                       capture_output=True)
    _cache.pop(account, None)


def clear_cache():
    _cache.clear()
    _hosts.clear()


_hosts = {}  # address -> HostName, from the inventory


def host_for(address):
    if not _hosts:
        try:
            for c in json.loads(INVENTORY.read_text()).get("controllers", []):
                _hosts[c["ip"]] = c["host"]
        except (OSError, ValueError, KeyError):
            pass
        _hosts.setdefault("", "")  # loaded, even if empty
    return _hosts.get(address)


def password_for(address, host=None):
    """The stored password for a controller (by name, else by its inventory
    address), else the default; None when neither is stored."""
    host = host or host_for(address)
    return (keychain_get(f"host:{host}") if host else None) or keychain_get("default")


def target_of(url):
    """(controller address, player address or None) for a controller URL."""
    u = urllib.parse.urlsplit(url)
    m = re.match(r"^/proxy/([^/]+)/", u.path)
    return (m.group(1), u.hostname) if m else (u.hostname, None)


class PasswordRequired(urllib.error.HTTPError):
    """A 401 from a controller, with a message that says what to do about it."""

    def __init__(self, err, address, sent):
        super().__init__(err.url, err.code, err.msg, err.hdrs, None)
        self.address, self.sent = address, sent

    def __str__(self):
        what = "rejected the stored password" if self.sent else "has a UI password"
        return (f"{self.address} {what}: enter it in Settings > Controller passwords "
                f"(FPP's UI password, user {USER})")


def request(url, timeout=6, method="GET", data=None, headers=None, password=None):
    """Raw response body. password overrides the Keychain (Test connection,
    adding a controller). Raises urllib.error.HTTPError, e.g. 401 when a
    password is needed or wrong."""
    h = dict(headers or {})
    if password is None:
        target, via = target_of(url)
        password = password_for(target) or (password_for(via) if via else None)
    if password:
        h["Authorization"] = "Basic " + base64.b64encode(f"{USER}:{password}".encode()).decode()
    req = urllib.request.Request(url, method=method, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 401:
            target, via = target_of(url)
            raise PasswordRequired(e, target, bool(password)) from None
        raise


def needs_password(err):
    return isinstance(err, urllib.error.HTTPError) and err.code == 401
