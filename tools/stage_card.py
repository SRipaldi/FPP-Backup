#!/usr/bin/env python3
"""Turn a snapshot into a staged /home/fpp/media tree for a fresh FPP image.

The staged tree is what tools/build_image.sh copies into the image's root
partition: the settings file plus config, playlists, scripts and media.

No BootActions=settings, although FPP's own restore sets it: its handler runs
inside `fppinit start`, and on FPP-Side's first 10.3 boot fppinit stopped
there until the 60 s watchdog killed it, so setupNetwork and everything after
it never ran. The handler makes synchronous systemctl calls that can wait on
units ordered after fppinit (`systemctl restart chrony` for ntpServer, and
`service apache2 reload` from the password setting); see
fpp-issues/04-boot-actions-stall.md. (Service_* changes are backgrounded and
are not the cause.) What that action would do is written to <out>.rootfs.sh
instead, for build_image.sh to apply to the image: chrony's NTP server and
service enablement. Time zone needs nothing; fppinit applies it every boot.

10.x shows its setup page until LegalJurisdiction and the privacy settings are
in the settings file. Ones the source lacks are taken from --reference, a
snapshot of a unit already on the target version (the owner's own answers).

Settings start from the settings file inside the unit's FPP backup, because the
live API reports defaults for unset keys and pinning an old version's defaults
would defeat the upgrade. But that backup is only as new as the last time FPP
made one, so live values are layered on top: a live value that differs from
the file wins, and a key missing from the file is added when its live value
differs from the schema default (e.g. Latitude set after the last backup).
Values fppinit derives per box, and secrets the API will not return, are left
alone.

Config migrations: a fresh image's /etc/fpp/config_version is already current,
so FPP will not run its upgrade/<n> scripts on restored settings. The ones that
rewrite the settings file are applied here (numbers match /opt/fpp/upgrade/<n>).

The output contains secrets (osPassword, .htpasswd, Wi-Fi PSKs). It belongs
under work/, which is gitignored.

Usage:
  tools/stage_card.py snapshots/FPP-Side/<ts> work/stage/FPP-Side \\
      [--reference snapshots/FPP-Porch/<ts>]
"""
import argparse
import json
import os
import re
import shlex
import shutil
import sys
from pathlib import Path

# Per-box state FPP recomputes on boot; carrying it over would be wrong on a new card.
TRANSIENT_SETTINGS = {"LastBlock", "UnpartitionedSpace", "storageDevice",
                      "rebootFlag", "restartFlag", "BootActions"}
# Config files that are caches, or replaced by a different mechanism in 10.x
# (UI password moved from .htaccess to ui-password-config.conf).
SKIP_CONFIG = {".htaccess", "media_durations.cache", "sequence_fps.cache"}
# Never taken from live values: derived by fppinit on each box (audio device,
# storage, flags), or not readable through the API (passwords come back None).
NOT_FROM_LIVE = {"AudioOutput", "AudioMixerDevice", "ForceAudioId", "AudioCardType",
                 "AudioCard0Type", "HostDescription", "osPassword", "osPasswordVerify",
                 "password", "passwordVerify", "initialSetup", "initialSetup-01",
                 "initialSetup-02"}
CONTENT_DIRS = ["playlists", "scripts", "sequences", "music", "videos", "images", "effects"]
# What www/common.php MissingSetupSettings() requires before 10.x skips its setup
# page: LegalJurisdiction plus the initialSetup-privacy group minus emailAddress.
SETUP_SETTINGS = ["LegalJurisdiction", "ShareCrashData", "FetchVendorLogos",
                  "SendVendorSerial", "statsPublish"]
# Service_* setting -> systemd units it enables (scripts/apply_service_setting).
SERVICE_UNITS = {"Service_rsync": ["rsync"], "Service_smbd_nmbd": ["smbd", "nmbd"],
                 "Service_vsftpd": ["vsftpd"]}
# tzdata "backward" names -> canonical (Debian 13 ships them only in tzdata-legacy).
LEGACY_TZ = {"US/Pacific": "America/Los_Angeles", "US/Mountain": "America/Denver",
             "US/Arizona": "America/Phoenix", "US/Central": "America/Chicago",
             "US/Eastern": "America/New_York", "US/East-Indiana": "America/Indiana/Indianapolis",
             "US/Michigan": "America/Detroit", "US/Alaska": "America/Anchorage",
             "US/Hawaii": "Pacific/Honolulu", "US/Aleutian": "America/Adak",
             "Canada/Pacific": "America/Vancouver", "Canada/Mountain": "America/Edmonton",
             "Canada/Central": "America/Winnipeg", "Canada/Eastern": "America/Toronto",
             "Canada/Atlantic": "America/Halifax", "Canada/Newfoundland": "America/St_Johns"}


def settings_file(snap):
    """The unit's real settings file, from the FPP backup inside a snapshot."""
    m = json.loads((snap / "manifest.json").read_text())
    if not m.get("fpp_backup_file"):
        sys.exit(f"{snap} has no FPP backup; cannot recover its settings file")
    backup = json.loads((snap / "config" / m["fpp_backup_file"]).read_text())
    return m, backup["settings"]["system_settings"][0]


def rootfs_script(settings, warnings):
    """Shell run by build_image.sh with $ROOTFS set, doing what BootActions=settings would."""
    lines = ["# generated by stage_card.py; run by build_image.sh with ROOTFS set", "set -e"]
    tz = re.sub(r"[^A-Za-z0-9_+/\-]", "", settings.get("TimeZone", ""))
    if tz:
        lines.append(f'[ -e "$ROOTFS/usr/share/zoneinfo/{tz}" ] || '
                     f'{{ echo "time zone {tz} is not in this image; FPP would stay on UTC" >&2; exit 1; }}')
    ntp = re.sub(r"[^A-Za-z0-9.\-]", "", settings.get("ntpServer", ""))
    conf = "$ROOTFS/etc/chrony/chrony.conf"
    # Same edit as SetNTPServer() in www/common/settings.php
    lines += [f"sed -i -e '/^pool .*/d' -e '/^server .*/d' {conf}",
              f"echo {shlex.quote(f'server {ntp} iburst' if ntp else 'pool falconplayer.pool.ntp.org iburst minpoll 8 maxpoll 12')} >> {conf}"]
    for key, units in SERVICE_UNITS.items():
        if key not in settings:
            continue
        for u in units:
            link = f"$ROOTFS/etc/systemd/system/multi-user.target.wants/{u}.service"
            if settings[key] == "1":
                lines.append(f"[ -f $ROOTFS/lib/systemd/system/{u}.service ] && "
                             f"ln -sf /lib/systemd/system/{u}.service {link} || true")
            else:
                lines.append(f"rm -f {link}")
    for key in ("passwordEnable", "osPasswordEnable"):
        if settings.get(key) == "1":
            warnings.append(f"{key} is on: not applied offline; re-enter it in the FPP UI after boot")
    return "\n".join(lines) + "\n"


def migrate(settings, log):
    s = dict(settings)
    # 108: AudioBackend renamed to MediaBackend
    if "AudioBackend" in s:
        s["MediaBackend"] = s.pop("AudioBackend")
        log.append("108: renamed AudioBackend -> MediaBackend")
    # 109: legacy ALSA -> Simple PipeWire. Mapping AudioOutput to ForceAudioId
    # needs /proc/asound/cards on the real box, so it is left unset (default sink).
    if s.get("MediaBackend") not in ("pipewire", "pipewire-simple"):
        for k in ("ForceHDMI", "EnableBBBHDMI"):
            if k in s:
                s.pop(k)
                log.append(f"109: removed obsolete {k}")
        s["MediaBackend"] = "pipewire-simple"
        log.append("109: MediaBackend = pipewire-simple (ForceAudioId left unset)")
    # Not an FPP upgrade step: Debian 13 (FPP 10.x images) moved the backward-
    # compatible zone names (US/Pacific, ...) to tzdata-legacy, which the image
    # lacks, so fppinit's setupTimezone fails every boot and the box stays on UTC.
    tz = s.get("TimeZone", "")
    if tz in LEGACY_TZ:
        s["TimeZone"] = LEGACY_TZ[tz]
        log.append(f"tz: TimeZone {tz} -> {LEGACY_TZ[tz]} (legacy alias missing on Debian 13)")
    # 121: DPI_FPS removed
    if s.pop("DPI_FPS", None) is not None:
        log.append("121: removed DPI_FPS")
    # 122 + 132: MultiSync defaults to multicast; pin unicast off if never recorded
    if s.get("MultiSyncEnabled") == "1":
        if not any(s.get(k) == "1" for k in ("MultiSyncMulticast", "MultiSyncBroadcast", "MultiSyncUnicast")):
            s["MultiSyncMulticast"], s["MultiSyncUnicast"] = "1", "0"
            log.append("122: MultiSyncMulticast = 1, MultiSyncUnicast = 0")
        if not s.get("MultiSyncUnicast"):
            s["MultiSyncUnicast"] = "0"
            log.append("132: MultiSyncUnicast pinned to 0")
    # 127: plugin-source flags cleared
    for k in ("PluginOfficialEverInstalled", "PluginCommunityEverInstalled", "PluginUnknownEverInstalled"):
        if s.pop(k, None) is not None:
            log.append(f"127: removed {k}")
    return s


def write_settings(path, settings):
    # Same format as WriteSettingToFile() in www/common.php
    path.write_text("".join(f'{k} = "{v}"\n' for k, v in settings.items()))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("snapshot", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--reference", type=Path,
                    help="snapshot of a unit already on the target version: supplies 10.x setup "
                         "answers the source lacks, and the settings schema for the report")
    args = ap.parse_args()

    snap, out = args.snapshot, args.out
    os.umask(0o077)  # the stage holds passwords and Wi-Fi keys
    manifest, settings = settings_file(snap)
    backup_rel = manifest["fpp_backup_file"]
    if not re.match(r"^backups/[^/]+\.json$", backup_rel):
        sys.exit(f"unexpected FPP backup name in the manifest: {backup_rel!r}")
    # Replaced wholesale on every run, so make sure it is a stage and not some other folder.
    if out.exists() and any(out.iterdir()) and not (out / "settings").is_file():
        sys.exit(f"{out} exists and is not a staged card (no settings file); not replacing it")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    report = {"host": manifest["host"], "source_snapshot": str(snap),
              "source_version": manifest["fpp_version"], "migrations": [],
              "dropped_settings": [], "setup_answers_from_reference": {}, "setup_still_missing": [],
              "unknown_to_target": [], "updated_from_live": {}, "added_from_live": {},
              "warnings": [], "config_skipped": [], "files": 0, "bytes": 0}

    live = json.loads((snap / "api/settings-live.json").read_text())
    schema_path = snap / "api/settings-schema.json"
    # Only entries with a "type" are user settings (a UI control); the rest
    # (Platform, *Directory, locale, ...) are values FPP computes and the API
    # merely reports. Those must never be written into the settings file.
    defaults = ({k: v.get("default") for k, v in json.loads(schema_path.read_text()).get("settings", {}).items()
                 if "type" in v}
                if schema_path.exists() else None)
    if defaults is None:
        report["warnings"].append("snapshot has no settings schema; settings changed since the unit's "
                                  "last FPP backup but absent from it cannot be detected. Re-snapshot.")
    for k, v in live.items():
        if v is None or k in NOT_FROM_LIVE or k in TRANSIENT_SETTINGS:
            continue
        if defaults is not None and k not in defaults:
            continue  # computed value, not a setting
        v = str(v)
        if k in settings:
            if v != str(settings[k]):
                report["updated_from_live"][k] = {"backup": settings[k], "live": v}
                settings[k] = v
        elif defaults is not None and k in defaults and v != str(defaults[k] if defaults[k] is not None else ""):
            report["added_from_live"][k] = v
            settings[k] = v

    report["dropped_settings"] = sorted(k for k in settings if k in TRANSIENT_SETTINGS)
    settings = {k: v for k, v in settings.items() if k not in TRANSIENT_SETTINGS}
    settings = migrate(settings, report["migrations"])

    ref = settings_file(args.reference)[1] if args.reference else {}
    for k in SETUP_SETTINGS:
        if k not in settings and k in ref:
            settings[k] = ref[k]
            report["setup_answers_from_reference"][k] = ref[k]
    report["setup_still_missing"] = [k for k in SETUP_SETTINGS if k not in settings]
    if report["setup_still_missing"]:
        report["warnings"].append("FPP will show its setup page on first boot (missing: "
                                  + ", ".join(report["setup_still_missing"]) + ")")
    if args.reference:
        schema = json.loads((args.reference / "api/settings-live.json").read_text())
        report["unknown_to_target"] = sorted(k for k in settings if k not in schema)
    write_settings(out / "settings", settings)
    script = out.parent / f"{out.name}.rootfs.sh"
    script.write_text(rootfs_script(settings, report["warnings"]))

    def copy(src, dst):
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        report["files"] += 1
        report["bytes"] += src.stat().st_size

    for f in sorted((snap / "config").rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(snap / "config")
        if rel.parts[0] == "backups" and str(rel) != backup_rel:
            continue
        if rel.name in SKIP_CONFIG:
            report["config_skipped"].append(str(rel))
            continue
        # The newest FPP backup rides along so FPP's own Restore page is a fallback.
        copy(f, out / "config" / rel)

    for d in CONTENT_DIRS:
        for f in sorted((snap / d).rglob("*")) if (snap / d).exists() else []:
            if f.is_file():
                copy(f, out / d / f.relative_to(snap / d))

    (out.parent / f"{manifest['host']}-stage-report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({k: v for k, v in report.items()}, indent=2))


if __name__ == "__main__":
    main()
