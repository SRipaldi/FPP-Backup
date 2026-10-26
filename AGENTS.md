# AGENTS.md: notes for AI coding agents

Read this before changing or running anything. User-facing docs are in `README.md`.

## What this is

A local tool for an FPP (Falcon Player) light show: back up controllers, keep their settings consistent against a policy, check sequence coverage and live sync, and rebuild a controller onto a fresh SD card (optionally upgrading FPP). A Python standard-library web server (`ui/server.py`) serves a plain-JS UI and runs the command-line tools in `tools/` as background jobs.

## Set up

1. **Python 3.9+**, standard library only. There is no `requirements.txt` or virtualenv, and none should be added without a reason: the UI server and every tool run with the system `python3` (macOS ships 3.9). Keep code 3.9-compatible: no `match`, no `X | Y` type unions.
2. **Docker engine** for `tools/build_image.sh` only. The owner uses Rancher Desktop (dockerd/moby): CLI at `~/.rd/bin`, socket at `~/.rd/docker.sock` (not `/var/run/docker.sock`). The script sets `DOCKER_HOST` itself. Containers run `--privileged` (loop mounts) and pull `debian:trixie-slim`.
3. **bb-imager-cli** for SD card detection: `tools/setup_bb_imager.sh` (needs `cargo`) clones `beagleboard/bb-imager-rs` at a pinned tag and builds it into `work/bin/`. It is used only for `list-destinations sd`; writing does not use it.
4. **Config** lives in `config/` (gitignored): `settings.json` (player address, monitor, update check, backup age), `fleet-inventory.json`, `fleet-policy.json`. With no `settings.json`, the UI opens on Settings and the CLI asks for `--master`. The policy is created on first scan from `templates/fleet-policy.json` (`@player` values filled from the player). Never commit anything in `config/`, `snapshots/` or `work/`.
5. **Run**: `python3 ui/server.py [--no-browser] [--no-monitor] [--port N] [--master IP]`, then http://127.0.0.1:8765/. Flashing needs the server started from the user's GUI login session (see macOS below).

## Layout

```
ui/server.py          HTTP server, job runner (Job: steps = subprocess argv lists, SSE log stream),
                      notifications, background monitor, fleet/dashboard/settings endpoints
ui/static/            index.html (all pages as <section id="tab-*">), app.js, style.css
tools/fleet.py        discovery (player list + inventory, verified per unit), scans, policy
                      validation/evaluation, update check (GitHub), sequence coverage, settings,
                      inventory; CLI: audit / drift / set / restart / reboot
tools/snapshot.py     read-only backup of a unit (config files, playlists, scripts, optional media,
                      live settings, settings schema, network config, newest FPP backup JSON)
tools/stage_card.py   snapshot -> staged /home/fpp/media tree + <stage>.rootfs.sh (settings
                      migrations, setup answers from --reference, legacy TZ mapping)
tools/build_image.sh  writes a stage into a COPY of a stock image in a container
tools/flash_card.py   writes an image to an SD card via authopen, verifies by read-back
tools/verify_unit.py  read-only check of a booted unit against its snapshot
tools/get_image.py    FPP release images from GitHub
tools/fpp_http.py     every request to a controller: adds the UI password (HTTP Basic, user admin) from the macOS Keychain
tools/xlights.py      pushes sequences to controllers through xLights' automation API (FPP Connect)
```

Pure vs network: `fleet.scan_fleet()` talks to controllers (about 30 s); `fleet.evaluate(scan, policy)` is pure and is re-run on every request, so policy edits and previews need no rescan. Keep that split. `fleet.scan_one()` is one unit's part of a scan; the server's `fleet/rescan` action merges it into the cached scan. Reading settings dominates (~15 s per unit): FPP has no bulk read of values and answers one request at a time, so more concurrency does not help.

## How to check a change

There is no automated test suite. Verify the way this repo has been verified:

- Syntax: `python3 -c "import ast; ast.parse(open('ui/server.py').read())"` and `node --check ui/static/app.js`.
- Server: run it on a spare port (`--no-browser --no-monitor --port 8798`), call the endpoint with `curl`; every `/api/` request (GET too, except job streams) needs the per-run token from the page's `<meta name="token">` in an `X-Token` header, and a local `Host`.
- UI: render headless with Chrome (`/Applications/Google Chrome.app/Contents/MacOS/Google Chrome --headless=new --screenshot=...`), or drive it over the DevTools protocol to click things; pages can be opened directly with `#dashboard`, `#fleet`, `#settings` etc. Check dark mode (the owner uses it).
- To simulate a fresh install, move `config/` aside, run a second server, then put it back. Never leave the owner's `config/` modified.
- Card builds: build to a file and inspect it read-only in a container; never flash without the user.

## Rules for working against the controllers

The fleet is a real, running light show.

- **Read-only by default.** Only GET endpoints that are known to be pure. Ask the user before any write, restart, reboot, playlist start/stop, or anything that changes a controller.
- **Some FPP GET endpoints have side effects.** Never call these to "check" something: `/api/fppd/ports/pixelCount` (starts a pixel-count test pattern on every port), `/api/fppd/ports/stop`, `/api/network/wifi/scan/*` (starts a scan), `GET /api/system/reboot` and `/shutdown`, `/api/playlist/<name>/start`, `/api/playlists/stop`. Before calling an unfamiliar endpoint, read its description in the unit's `/api/openapi.json` (10.x) or `/api/endpoints.json` (8.x).
- **Everything goes through the player's `/proxy/`.** Its Apache proxy falls over under load: about 30 concurrent requests made healthy units time out and read as offline. Full scans are limited to 3 units x 4 requests; keep new concurrency at or below that. The proxy occasionally returns an empty body: retry (see `fetch_json`).
- Writes go through FPP's own API (`PUT /api/settings/<name>` applies the setting). FPP 8.x accepts only GET for `system/fppd/restart`; `fleet.py` falls back.
- Don't start or stop the show, and don't restart players while one is playing (`fleet.py restart` skips playing units).

## Security rules

- **Controllers are untrusted input.** Anything a unit reports (hostname, file names, settings, sequence names) can come from a rogue device on the show network. Hostnames must match `^[A-Za-z0-9][A-Za-z0-9-]{0,62}$` before they become paths; file names from a listing must stay inside the snapshot (`snapshot.py save()`); values that reach a shell script are filtered to safe characters (`stage_card.rootfs_script`); everything shown in the UI goes through `esc()`.
- **Secrets on disk**: snapshot, stage and build tools set `umask 077`.
- **Controller passwords**: only in the Keychain (`fpp_http`, service `FPP-Backup`, accounts `default` and `host:<HostName>`; `FPP_BACKUP_KEYCHAIN` points tests at another service). Never return a password from an endpoint or write one to `config/`. Every controller request must go through `fpp_http.request` (via `fleet.fetch`/`fetch_json`) so it carries the password. Through the player's proxy only one Authorization header is sent: the controller's password, else the default; different player and controller passwords therefore need direct access.
- **Incremental media** (`snapshot.py`): reused files are hard links into the previous snapshot. Never write into a snapshot after it is taken (that would change older ones too); the reuse rule is same size plus an identical recorded mtime; only for snapshots that recorded none, a listing mtime older than the previous snapshot's start minus 10 minutes (BeagleBones can stamp uploads with a stale date before NTP, so the older-than rule alone is not trusted). Staging copies the files, so builds never write through a link.
- **Supply chain**: FPP images are checked against the SHA-256 GitHub publishes (`get_image.py`); the build container is pinned by digest (`BUILDER_IMAGE` in `build_image.sh`); bb-imager-cli is built `--locked` from a pinned commit. Bump pins deliberately.
- **xLights automation port** (49913) has no auth, binds all interfaces and allows any origin; never leave it on in instructions or defaults.

## FPP facts this code depends on

- **Settings**: `/api/settings` is the schema. Only entries with a `"type"` are user settings; the rest (Platform, *Directory, locale, ...) are computed and must never be written into a settings file. The live API reports schema defaults for unset keys. A checkbox with no value is off; a select's default is the option whose label contains `(Default)`. The real settings file is in the newest FPP backup JSON (`settings.system_settings[0]`); FPP makes a backup automatically when settings are saved.
- **Secrets**: FPP 8.5's API returns `osPassword` and `TetherPSK` in plain text; 10.x hides passwords. The drift report filters names matching `secret_pattern`; keep secrets out of anything the UI shows or Git tracks.
- **FPP 10.x first boot**: `fppinit start` runs `handleBootPartition` (copies `/boot/firmware/fpp/*` once), `checkHostName`, then later `handleBootActions` and `setupNetwork`. Do **not** set `BootActions = "settings"` on a built card: on FPP-Side it stalled `fppinit start` until the 60 s watchdog killed it, so network setup never ran (`fpp-issues/04-boot-actions-stall.md`). The build applies what that action would (NTP server in `chrony.conf`, service links) to the image instead.
- **10.x setup page** appears until `LegalJurisdiction` and the privacy settings exist; `stage_card.py` fills missing ones from a unit already on the target version.
- **Time zones**: 10.x images (Debian 13) lack backward names such as `US/Pacific`; FPP silently stays on UTC. `stage_card.py` maps them.
- **Gateway in the fleet scan**: `network_info()` reads `/api/network/gateway`, `/dns` and the interface list; `gateway_row()` adds a drift row named `gateway` (kind `network`, not an FPP setting) from `policy.network.gateway` rules, or rules inferred from the fleet. `POST /api/network/gateway` only saves the file; it applies when the network next comes up. An in-place 8.5→10.3 upgrade dropped the per-interface gateway on two units.
- **Network**: 10.x moves `GATEWAY` from `interface.*` into `config/gateway` and applies it to every static interface. 10.x hides `interface.*` and `.htpasswd` from `/api/files/config`; read network config from `/api/network/interface/<if>`.
- **Restart**: settings such as `TimeZone`, `Latitude`, `Locale` only take effect after an fppd restart (schema `restart: 2`).
- **Status**: `/api/fppd/status` positions are whole seconds; `/api/fppd/multiSyncStats` on a remote counts sync packets from the player.

## xLights automation

`tools/xlights.py` drives the xLights running on this computer through `POST http://127.0.0.1:49913/xlDoAutomation` (enabled by Preferences > xFade port A; commands are documented in xLights' `documentation/xlDo Commands.txt`). `uploadSequence` with `format: v2stdsparse` is FPP Connect: xLights discovers the controller (remotes through the player's `/api/proxies`) and cuts the file to the channel ranges that controller reports.

- **Never let a request time out early.** xLights can crash if the client disconnects before it replies; uploads and renders use a one-hour timeout.
- `getVersion`, `getShowFolder` and `getOpenSequence` are read-only. `uploadSequence` writes to a controller, `batchRender` rewrites the .fseq: both need the user's OK. Don't run `xLights --version` to read the version: it launches a second copy; read the app's Info.plist instead.
- Every copy of a render carries the render's ID (FSEQ v2 header bytes 24-31; `/api/sequence/<name>/meta` reports it as `ID`), so a controller's copy can be matched to the show folder's .fseq. FPP answers 404 on `meta` for a few seconds after an upload. `xlights.py versions` stores what it read in `work/cache/sequence-versions.json`; `version_report()` compares it with the show folder on every request. Show-folder names can carry a leading space that FPP drops, so names are matched with surrounding spaces ignored.

## macOS specifics

- Writing an SD card as root fails ("Operation not permitted", privacy protection), and bb-imager-cli's own authopen path fails with -60008. `flash_card.py` opens the device with `/usr/libexec/authopen -stdoutpipe -o O_RDWR` and receives the file descriptor over a socket, as the BeagleBoard GUI does. The admin dialog needs the user's GUI session: an agent's shell usually cannot show it, so the user runs the flash.
- Loop devices leak in the Rancher VM if a container exits with mounts held; `build_image.sh` detaches its own and verifies. Never detach `loop0` (the VM's modloop).
- In zsh, unquoted `$VAR` is not word-split; inline `python3 -c` with f-strings containing backslashes fails on 3.9. Prefer heredoc scripts.

## Conventions

- Match the surrounding code: module and function docstrings say why, comments are sparse, no new dependencies.
- New controller writes go through `fleet.py` (`set`, `restart`) so the CLI and UI share them; long actions run as server `Job`s (`keep_going=True` for independent steps, `after=` for a rescan).
- Commit messages explain the why; end with the co-author line used in this repo's history.
