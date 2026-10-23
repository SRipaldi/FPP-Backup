# FPP-Backup: Handoff Document

Purpose: carry everything decided so far into a new Claude Code session (VS Code, running on a machine on the owner's LAN) so work continues with no loss of context.

Date of handoff: 2026-10-23
Owner: Stephen (GitHub: `sripaldi`)

---

## 1. The problem

The owner runs a Christmas light show on **FPP (Falcon Player)**, with sequences and configuration authored in **xLights**. Players run on **BeagleBone** boards, using **KulpLights** controllers (BeagleBone-based capes).

Pain points:
- Flashing a new FPP image gives a blank system with none of the old unit's data.
- FPP's built-in Backup & Restore is unreliable and does **not** move media (sequences, music, video).
- When a controller dies or needs an upgrade, rebuilding it by hand is slow and error-prone.

## 2. The goal

A tool plus a Git repository that:
1. Regularly captures the full state of every controller into a repo (one folder per controller).
2. Lets the owner flash a **new SD card** with a fresh FPP image and have it come up **exactly as the old unit did** (same hostname, same network/IP) with no manual steps, then restore content and configuration onto it.
3. Is fully drivable and monitorable from a **UI**, not just a CLI.

Workflow target: unit fails or needs upgrade -> flash a new card using the tool -> swap the card in -> unit boots with its old identity -> tool restores config and media -> done.

## 3. Decisions made

| # | Topic | Decision |
|---|---|---|
| 1 | Repo | New private repo **`sripaldi/FPP-Backup`**. Does not exist yet (see section 6). |
| 2 | Storage model | Git repo with **one folder per controller** holding config. Updated on a regular schedule. |
| 3 | Config vs media | **Config** (small text/JSON) goes in Git. **Media** (sequences, music, video) is **not** in plain Git. Media comes in a later phase and may use Git LFS, a NAS/rsync target, or Releases. Sequences can often be regenerated from xLights; music is the hard-to-replace part. |
| 4 | Secrets | **Never stored in plain text and never in the repo.** Credentials are entered once in the tool and written to the OS vault: macOS Keychain, Windows Credential Manager, or Secret Service on Linux. Use the Python `keyring` library for a cross-platform API. The repo holds only references (e.g. `fpp-backup/<controller>/wifi`). A scrubber strips secret fields before commit, and a pre-commit check blocks anything that still looks like a secret. |
| 5 | Identity after swap | The new card must come up with the **old hostname and network config without owner action**. Preferred approach: **pre-seed the flashed card** by writing the saved hostname and interface files onto the card's partitions before it is inserted. Fallback: boot on DHCP, then push identity over the API (leaves a window where the unit is at the wrong address). Restoring identity should be an option so a bench test restore does not clash with a live unit. |
| 6 | Language | Owner has no preference. Recommended: **Python** (keyring, requests, easy tests, simple web UI). |
| 7 | UI | A **local web app** (Python backend). Shows controller list, last-backup time, diff view, buttons for backup / restore / flash, live progress. Every function should be doable and monitorable in the UI. **CLI first**, with the UI built on the same core so everything is also scriptable and cron-able. UI may come later than the CLI. |
| 8 | Where it runs | The tool runs **locally on a machine on the owner's LAN** (VS Code / terminal), not in the cloud container. It needs LAN access to the player and controllers, physical access to SD cards for flashing, and the local keychain. |
| 9 | Network reach | The FPP player at **172.1.2.10** is the owner's main player and **proxy to all the other controllers**. The tool should reach other units through it. |

## 4. Technical background (to verify, not assume)

These are working assumptions from general FPP knowledge. **Verify against a real unit before relying on them.**

- FPP state lives under `/home/fpp/media`: `config/`, `playlists/`, `scripts/`, `plugins/`, `sequences/`, `music/`, `videos/`, `images/`, `effects/`, `upload/`, `logs/`, plus a `settings` file.
- Default SSH user is `fpp`. Check the owner's actual credentials; store them only in the keychain.
- FPP has an HTTP API (e.g. `/api/fppd/status`, `/api/settings`, `/api/configfile`, file list/download endpoints, backup endpoints). Confirm exact endpoints on the owner's FPP version.
- KulpLights string/channel-output config is a file under `config/` and can be backed up and restored. The cape identity itself comes from hardware (EEPROM) and is not restored.
- Network identity is in `config/interface.*` files and the hostname setting.
- BeagleBone images: SD-card image vs eMMC flasher image. Owner flashes **SD cards**. Releases are published at github.com/FalconChristmas/fpp.
- Plugins: back up the installed-plugin list and reinstall from it. Do not copy plugin directories across FPP versions.
- Version jumps: restoring config across major FPP versions can break things. Record the FPP version in each backup and warn on mismatch, with an option to restore media only.

## 5. Planned architecture

```
FPP-Backup/
  controllers/
    <controller-name>/
      meta.json          # name, IP, FPP version, last backup time, hardware notes
      config/            # scrubbed copy of media/config + settings
      playlists/
      scripts/
      plugins.json
      network/           # hostname + interface files, used for pre-seeding
  src/fppbackup/
    core/                # fpp client (API+SSH), backup, restore, scrub, diff
    secrets/             # keyring wrapper, secret reference resolution
    flash/               # image download, SD write, pre-seed identity
    cli/
    ui/                  # local web app
  tests/                 # unit tests against recorded sample API responses
  README.md
```

CLI surface (v1): `backup`, `list`, `diff`, `restore`, `verify`, `secrets set/get`, then `flash`.

## 6. Open items and blockers

1. **Repo does not exist.** Creating it from the cloud session failed (GitHub integration returned 403). The owner must create `sripaldi/FPP-Backup` as **Private** with a README.
2. **No access to real hardware yet.** The cloud session could not reach 172.1.2.10 (blocked by the environment's network allowlist). One controller is currently **down**; its last backup does not exist yet, so capturing the remaining units soon matters.
3. **Unknowns to discover on the first run:** FPP version(s) in use; exact API endpoints available; SSH credentials; list of controllers and their IPs; hostnames; whether any use eMMC; which config files hold Wi-Fi or other secrets.
4. **Flashing is partly physical.** Inserting the SD card is manual. The tool can download the image, write it (dd/balenaEtcher CLI), and pre-seed identity, but needs OS-specific handling and care to avoid writing to the wrong disk.

## 7. Phased plan

**Phase 0: Setup (owner)**
- Create private repo `sripaldi/FPP-Backup`.
- Open the project in VS Code with Claude Code on a LAN machine; confirm `curl http://172.1.2.10/api/fppd/status` works.

**Phase 1: Discovery (read-only)**
- Query the player and each controller via the proxy: version, settings, file listings.
- Save sample responses as test fixtures (scrub secrets).
- Document the real config file layout and which fields are secret.

**Phase 2: Core CLI**
- Project scaffold, packaging, tests.
- FPP client (API + SSH/rsync).
- `backup` of config into `controllers/<name>/`, with the scrubber and keychain-backed secret references.
- `list`, `diff`, `verify`.
- Pre-commit secret check.

**Phase 3: Restore**
- `restore` of config, playlists, scripts, plugin list onto a running FPP.
- Version-mismatch warnings; optional identity restore.
- Test on a spare or bench unit first, never first on a live show unit.

**Phase 4: Flash and identity pre-seed**
- Download the matching FPP image; write to SD with safe disk selection.
- Pre-seed hostname and network files so the unit boots with its old identity.

**Phase 5: Scheduling**
- Cron/launchd/Task Scheduler job for regular backups with commit and push.

**Phase 6: UI**
- Local web app over the same core: controller dashboard, backup/restore/flash with live progress, diffs, secrets management.

**Phase 7: Media**
- Separate store for sequences, music, video (LFS, NAS/rsync, or Releases). Incremental sync with checksums.

## 8. Suggested first prompt in the new session

> Read FPP-Backup-handoff.md. Start Phase 1 (discovery, read-only) against the FPP player at 172.1.2.10 and the controllers it proxies. Do not write to any controller. Report what you find (versions, endpoints, config layout, likely secret fields) and then propose the Phase 2 scaffold.

## 9. Ground rules for the next session

- Never commit credentials. Secrets go to the OS keychain only.
- Read-only against live controllers until a restore is explicitly requested and tested on a bench unit.
- Treat the assumptions in section 4 as hypotheses to verify.
- Keep the CLI and UI on one shared core.
