#!/usr/bin/env python3
"""Turn a snapshot into a staged /home/fpp/media tree for a fresh FPP image.

The staged tree is what tools/build_image.sh copies into the image's root
partition. It mirrors what FPP's own JSON restore does (www/backup.php): the
settings file plus config, playlists, scripts and media, then
BootActions=settings so FPP applies time zone, NTP, services and passwords on
first boot.

Settings come from the settings file inside the unit's FPP backup, not from the
live API: the API reports defaults for unset keys, and pinning an old version's
defaults would defeat the upgrade. snapshot.py's live values are used only to
confirm the backup is current.

Config migrations: a fresh image's /etc/fpp/config_version is already current,
so FPP will not run its upgrade/<n> scripts on restored settings. The ones that
rewrite the settings file are applied here (numbers match /opt/fpp/upgrade/<n>).

The output contains secrets (osPassword, .htpasswd, Wi-Fi PSKs). It belongs
under work/, which is gitignored.

Usage:
  tools/stage_card.py snapshots/FPP-Side/<ts> work/stage/FPP-Side \\
      [--schema snapshots/FPP-Porch/<ts>/api/settings-live.json]
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

# Per-box state FPP recomputes on boot; carrying it over would be wrong on a new card.
TRANSIENT_SETTINGS = {"LastBlock", "UnpartitionedSpace", "storageDevice",
                      "rebootFlag", "restartFlag", "BootActions"}
# Config files that are caches, or replaced by a different mechanism in 10.x
# (UI password moved from .htaccess to ui-password-config.conf).
SKIP_CONFIG = {".htaccess", "media_durations.cache", "sequence_fps.cache"}
CONTENT_DIRS = ["playlists", "scripts", "sequences", "music", "videos", "images", "effects"]


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
    ap.add_argument("--schema", type=Path,
                    help="settings-live.json from a unit on the target version, to flag unknown keys")
    args = ap.parse_args()

    snap, out = args.snapshot, args.out
    manifest = json.loads((snap / "manifest.json").read_text())
    if not manifest.get("fpp_backup_file"):
        sys.exit("snapshot has no FPP backup; cannot recover the real settings file")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    report = {"host": manifest["host"], "source_snapshot": str(snap),
              "source_version": manifest["fpp_version"], "migrations": [],
              "dropped_settings": [], "unknown_to_target": [], "stale_vs_live": [],
              "config_skipped": [], "files": 0, "bytes": 0}

    backup_rel = manifest["fpp_backup_file"]
    backup = json.loads((snap / "config" / backup_rel).read_text())
    settings = backup["settings"]["system_settings"][0]

    live = json.loads((snap / "api/settings-live.json").read_text())
    report["stale_vs_live"] = sorted(k for k, v in settings.items()
                                     if k in live and live[k] is not None and str(live[k]) != str(v))

    report["dropped_settings"] = sorted(k for k in settings if k in TRANSIENT_SETTINGS)
    settings = {k: v for k, v in settings.items() if k not in TRANSIENT_SETTINGS}
    settings = migrate(settings, report["migrations"])
    settings["BootActions"] = "settings"
    if args.schema:
        schema = json.loads(args.schema.read_text())
        report["unknown_to_target"] = sorted(k for k in settings if k not in schema)
    write_settings(out / "settings", settings)

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
