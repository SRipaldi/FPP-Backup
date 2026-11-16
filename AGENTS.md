# AGENTS.md: notes for AI coding agents

Read this before changing or running anything. User-facing docs are in `README.md`.

## What this is

A local tool for an FPP (Falcon Player) light show: back up controllers, keep their settings consistent against a policy, check sequence coverage and live sync, and rebuild a controller onto a fresh SD card (optionally upgrading FPP). A Python standard-library web server (`ui/server.py`) serves a plain-JS UI and runs the command-line tools in `tools/` as background jobs.

## Set up

1. **Python 3.9+**, standard library only. There is no `requirements.txt` or virtualenv, and none should be added without a reason: the UI server and every tool run with the system `python3` (macOS ships 3.9). Keep code 3.9-compatible: no `match`, no `X | Y` type unions.
2. **Docker engine** for `tools/build_image.sh` only. The owner uses Rancher Desktop (dockerd/moby): CLI at `~/.rd/bin`, socket at `~/.rd/docker.sock` (not `/var/run/docker.sock`). The script sets `DOCKER_HOST` itself. Containers run `--privileged` (loop mounts); the image is built from `tools/builder/Dockerfile` (Alpine) on each run.
3. **SD cards** are found with macOS's own `diskutil` (`flash_card.sd_destinations`): a physical whole disk reported as removable or ejectable, the BeagleBoard Imaging Utility's rule. Nothing to install.
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
tools/upgrade.py      in-place upgrades: plan (read-only), run (one unit at a time, follow, reboot, verify);
                      images / cleanup (leftover .fppos in Uploads); the Fleet page's dialogs run it as jobs.
                      Upgrade output is filtered: an OS upgrade prints ~50,000 file lines (froze the Jobs tab)
tools/check_public.py finds this installation's names/addresses, secrets and personal data in what would be
                      published (staged changes, commit messages, PR/release text, pushes); installs git hooks
tests/                unittest suite; fake_fpp.py is a pretend controller for the upgrade's write paths
fuzz/                 fuzz.py: Atheris targets for untrusted-input parsers (--selftest runs without Atheris); corpus/ seeds
tools/colorlight.py   ColorLight receiver health from each controller's syslog/fppd.log tails; history in
                      work/cache/colorlight.json; the monitor checks it every 5 min (Show > ColorLight)
```

Pure vs network: `fleet.scan_fleet()` talks to controllers (about 30 s); `fleet.evaluate(scan, policy)` is pure and is re-run on every request, so policy edits and previews need no rescan. Keep that split. `fleet.scan_one()` is one unit's part of a scan; the server's `fleet/rescan` action merges it into the cached scan. Reading settings dominates (~15 s per unit): FPP has no bulk read of values and answers one request at a time, so more concurrency does not help.

## This repository is public: no environment details, secrets or personal information

Everything that leaves this machine is public: commits, commit messages, pull requests, release notes, issues, code comments, tests and docs. None of it may describe the installation this tool runs. That covers controller names (also the short forms people say, like "the Gazebo unit"), IP addresses and subnets, show, playlist and sequence names, Wi-Fi network names, location, time zone and incident stories tied to a named unit. It also covers secrets (passwords, Wi-Fi keys, tokens, private keys) and personal information (email addresses, anything identifying the owner beyond LICENSE and CODEOWNERS).

- **Write generically**: "the player", "a remote", "a BeagleBone Black remote", `remote1`, `10.0.0.x`, `song.fseq`, `fpp.local`. Board models and FPP versions are fine. So is "seen on a PocketBeagle2, 10.3 -> 10.4". Naming which unit it was is not.
- **Check before every publish**: `tools/check_public.py` finds this installation's names and addresses (read from its own git-ignored `config/` and `snapshots/`, so the list is never published), anything in `config/private-terms.txt`, and secrets and email addresses by pattern. It also refuses any file under `snapshots/`, `work/`, `config/` or `fpp-issues/`.
  - `tools/check_public.py --install-hooks` once per clone. The hooks then check every commit, every commit message and every push.
  - Before opening a PR or a release, check the text itself: `tools/check_public.py --text pr-body.md` (or `release-notes.md`), and `--log origin/main..HEAD` for what a push would add.
  - `--all` checks every tracked file.
- **A finding blocks the publish.** Replace the detail; don't bypass the hook. `--no-verify` is only for a finding that has been looked at twice and is genuinely not private.
- **Already published is not "too late"**: if something slips out, edit the PR or release text, scrub the files in a new commit, and tell the owner, who decides whether history needs rewriting.

## How to check a change

`python3 -m unittest discover -s tests` runs the tests (stdlib only, about 80 s): the in-place upgrade against a fake controller, including failure, stall, no-return and lost-content cases, and `tools/check_public.py` against a made-up installation. Run `tools/check_public.py --all` too (see the section above). There is no other suite; otherwise verify the way this repo has been verified:

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
- **Supply chain**: FPP images are checked against the SHA-256 GitHub publishes (`get_image.py`); the build container is built from `tools/builder/Dockerfile` on every run (`--no-cache`, so its Alpine packages are the current, patched ones) on a base pinned by digest (Dependabot proposes bumps). Bump pins deliberately. Fewer dependencies beat scanned dependencies: bb-imager-cli (~790 Rust crates) was dropped for `diskutil`.
- **xLights automation port** (49913) has no auth, binds all interfaces and allows any origin; never leave it on in instructions or defaults.

## CI and scanning

| What | Covers | Where |
|---|---|---|
| CodeQL (`security-extended` queries) | SAST: Python and JavaScript | GitHub default setup; every PR and weekly. A high or critical alert blocks merging (ruleset). |
| ShellCheck, hadolint | The shell scripts and the builder Dockerfile, which CodeQL can't read | `.github/workflows/lint.yml`, every PR |
| OSV-Scanner | SCA: the builder container exactly as `build_image.sh` builds it (`tools/builder/Dockerfile`) | `.github/workflows/security-scans.yml`, weekly + PRs touching them |
| OpenSSF Scorecard | Repository posture: branch protection, pinning, signing, token permissions | same workflow, weekly; a score table on the run's summary page |
| Unit tests | `tests/` on Python 3.9, 3.13 and Apple's `/usr/bin/python3` (the one the owner runs), plus `check_public.py --all` | `.github/workflows/tests.yml`, every PR (required) |
| Atheris (libFuzzer) | Fuzzing the parsers of untrusted input: controller logs, release notes, settings that become shell, FSEQ headers, the policy, the leak check's regexes (`fuzz/fuzz.py`) | `.github/workflows/fuzz.yml`, 1 min per target on PRs touching `tools/`, 10 min weekly |
| Syft | Signed SPDX SBOMs plus a signed source archive attached to each release | `.github/workflows/sbom.yml`, run against the draft release |
| Dependabot | Version bumps for the base image and the pinned actions; security alerts | `.github/dependabot.yml` |
| Secret scanning + push protection | Known token formats, server-side | repository settings |
| `tools/check_public.py` | This installation's names and addresses, secrets, personal data | git hooks, before anything is published |

**Fuzzing finds real bugs**: its first run found a crash on a controller log timestamp Python 3.9 can't parse and a policy regex error that escaped the server's handler (both fixed, with tests in `tests/test_parsers.py`). Run a target locally without installing anything: `python3 fuzz/fuzz.py <target> --selftest`.

**No open alert at any severity.** Severity is not risk, so a low or medium finding is fixed like a high one: update, replace or remove the component. The only exception is a finding with no fix anywhere (no patched version, no alternative): dismiss it in code scanning as "won't fix" with the reason and the date to look again, never silently. The weekly scan is the check that this holds.

CodeQL and OSV-Scanner results land in Security > Code scanning, one category per tool. Scorecard's program has no SARIF output (only its third-party action does), so its table is on the run's summary page. The tool itself has no package manifests, which is why the builder image is scanned directly. The one manifest is `fuzz/requirements.txt` (Atheris, CI-only): Dependabot alerts and updates cover it, and an alert there falls under the same no-open-alert rule.

**Trust model for CI**, after third-party action compromises (tj-actions/changed-files, trivy-action) in which a moved tag shipped malicious code:
- **No third-party GitHub Actions.** The repository allows only GitHub-owned actions, and workflows pin even those by full commit SHA, with the version in a comment. Dependabot proposes updates after a 14-day cooldown.
- **Scanners are release binaries, pinned by version and SHA-256** in `.github/scripts/fetch-tool.sh`; the one Python package, Atheris for fuzzing, is pinned by version and hash in `fuzz/requirements.txt` (`pip --require-hashes`). Those are the only places third-party code enters CI. A pin is chosen only from a release at least 14 days old, and only when GitHub's recorded asset digest and the publisher's checksum file agree. To update one, change the version and the hash together, from both sources, and review it like code.
- **Least privilege.** Every workflow starts from `permissions: {}` or `contents: read`, and checkout never keeps credentials. A job that runs a third-party scanner holds only a read-only token and no secrets. Writing results (`security-events: write`), signing (`id-token`, `attestations`) and attaching to a release (`contents: write`) happen in separate jobs that run only GitHub's own actions and the gh CLI.
- **No secrets in CI.** Nothing in this repository's Actions needs one; keep it that way.
- **Release process with SBOMs**: `gh release create vX.Y.Z --draft --verify-tag ...`, then `gh workflow run sbom.yml -f tag=vX.Y.Z`, wait for it, check the assets (three `sbom-*.spdx.json`, `FPP-Backup-vX.Y.Z.tar.gz`, and its `.sigstore.json` bundle and `.intoto.jsonl` DSSE envelope of the signed provenance), then publish (`gh release edit vX.Y.Z --draft=false --latest`). Published releases are immutable.

## FPP facts this code depends on

- **Settings**: `/api/settings` is the schema. Only entries with a `"type"` are user settings; the rest (Platform, *Directory, locale, ...) are computed and must never be written into a settings file. The live API reports schema defaults for unset keys. A checkbox with no value is off; a select's default is the option whose label contains `(Default)`. The real settings file is in the newest FPP backup JSON (`settings.system_settings[0]`); FPP makes a backup automatically when settings are saved.
- **Secrets**: FPP 8.5's API returns `osPassword` and `TetherPSK` in plain text; 10.x hides passwords. The drift report filters names matching `secret_pattern`; keep secrets out of anything the UI shows or Git tracks.
- **FPP 10.x first boot**: `fppinit start` runs `handleBootPartition` (copies `/boot/firmware/fpp/*` once), `checkHostName`, then later `handleBootActions` and `setupNetwork`. Do **not** set `BootActions = "settings"` on a built card: on one remote it stalled `fppinit start` until the 60 s watchdog killed it, so network setup never ran (`fpp-issues/04-boot-actions-stall.md`). The build applies what that action would (NTP server in `chrony.conf`, service links) to the image instead.
- **10.x setup page** appears until `LegalJurisdiction` and the privacy settings exist; `stage_card.py` fills missing ones from a unit already on the target version.
- **Time zones**: 10.x images (Debian 13) lack backward names such as `US/Pacific`; FPP silently stays on UTC. `stage_card.py` maps them.
- **Gateway in the fleet scan**: `network_info()` reads `/api/network/gateway`, `/dns` and the interface list; `gateway_row()` adds a drift row named `gateway` (kind `network`, not an FPP setting) from `policy.network.gateway` rules, or rules inferred from the fleet. `POST /api/network/gateway` only saves the file; it applies when the network next comes up. An in-place 8.5→10.3 upgrade dropped the per-interface gateway on two units.
- **Network**: 10.x moves `GATEWAY` from `interface.*` into `config/gateway` and applies it to every static interface. 10.x hides `interface.*` and `.htpasswd` from `/api/files/config`; read network config from `/api/network/interface/<if>`.
- **Restart**: settings such as `TimeZone`, `Latitude`, `Locale` only take effect after an fppd restart (schema `restart: 2`).
- **Logs**: `GET /api/file/Logs/var/log/syslog?tail=N` (capital `L`; also `Logs/fppd.log`) returns the last N lines, so a check reads only what is new. Rotated `syslog.1` is plain text on 10.x, older ones are compressed.
- **ColorLight**: outputs are `LEDPanelMatrix` entries with `subType` `ColorLight5a75` in `/api/channel/output/channelOutputsJSON`. fppd searches for receivers only when it starts. The API has no link speed (`/api/network/interface` has carrier only); the speed comes from the kernel's "Link is Up - 1Gbps/Full" lines. A down/up pair under a second apart is the boot-time reset, not a drop.
- **In-place upgrade** (what FPP's About page does): `POST /upgradefpp.php?wrapped=1&version=10.4` runs `scripts/upgrade_FPP` (git fetch + checkout + rebuild on the controller; 5-30 min) and ends with "Upgrade complete", no reboot; then `POST /api/system/reboot`. `POST /upgradeOS.php?wrapped=1&os=<.fppos URL or a file in Uploads>` replaces the root filesystem and reboots by itself; it needs 2x the image size free. Both hold a server-side lock (409 when another update runs) and keep going if the client disconnects. `GET /api/system/updateActivity` reports the latest run (`runId`, `inProgress`, `stage`, `failed`); `GET /api/system/updateStatus` says whether the unit itself reached GitHub (`checked`). Uploads go to `POST /jqupload.php` (multipart field `myfile`, up to 1999 MB, staged on the media disk) and are removed with `DELETE /api/file/Uploads/<name>`. An in-place OS upgrade keeps the architecture: a 32-bit Pi needs the `Pi-` image, not `Pi64-`. The first boot after an OS upgrade can run `fppinit.service` past systemd's time limit (seen on a PocketBeagle2, 10.3 -> 10.4): what it never reached, such as writing `wpa_supplicant-wlan0.conf`, stays undone (WiFi down) until one more reboot. upgrade.py reads the syslog for that and reboots once more. An FPP update can also leave fppd stopped without rebooting (seen on a BeagleBone Black, 10.3 -> 10.4: web up on the new version, `/api/fppd/*` 503); upgrade.py treats its progress going quiet after real progress as "restarting", always reboots at the end, and retries a transient 503 right after a reboot.
- **Status**: `/api/fppd/status` positions are whole seconds; `/api/fppd/multiSyncStats` on a remote counts sync packets from the player.

## xLights automation

`tools/xlights.py` drives the xLights running on this computer through `POST http://127.0.0.1:49913/xlDoAutomation` (enabled by Preferences > xFade port A; commands are documented in xLights' `documentation/xlDo Commands.txt`). `uploadSequence` with `format: v2stdsparse` is FPP Connect: xLights discovers the controller (remotes through the player's `/api/proxies`) and cuts the file to the channel ranges that controller reports.

- **Never let a request time out early.** xLights can crash if the client disconnects before it replies; uploads and renders use a one-hour timeout.
- `getVersion`, `getShowFolder` and `getOpenSequence` are read-only. `uploadSequence` writes to a controller, `batchRender` rewrites the .fseq: both need the user's OK. Don't run `xLights --version` to read the version: it launches a second copy; read the app's Info.plist instead.
- Every copy of a render carries the render's ID (FSEQ v2 header bytes 24-31; `/api/sequence/<name>/meta` reports it as `ID`), so a controller's copy can be matched to the show folder's .fseq. FPP answers 404 on `meta` for a few seconds after an upload. `xlights.py versions` stores what it read in `work/cache/sequence-versions.json`; `version_report()` compares it with the show folder on every request. Show-folder names can carry a leading space that FPP drops, so names are matched with surrounding spaces ignored.

## macOS specifics

- Writing an SD card as root fails ("Operation not permitted", privacy protection), and the BeagleBoard Imaging Utility's CLI authopen path failed with -60008. `flash_card.py` opens the device with `/usr/libexec/authopen -stdoutpipe -o O_RDWR` and receives the file descriptor over a socket, as the BeagleBoard GUI does. The admin dialog needs the user's GUI session: an agent's shell usually cannot show it, so the user runs the flash.
- Loop devices leak in the Rancher VM if a container exits with mounts held; `build_image.sh` detaches its own and verifies. Never detach `loop0` (the VM's modloop).
- In zsh, unquoted `$VAR` is not word-split; inline `python3 -c` with f-strings containing backslashes fails on 3.9. Prefer heredoc scripts.

## Conventions

- Match the surrounding code: module and function docstrings say why, comments are sparse, no new dependencies.
- New controller writes go through `fleet.py` (`set`, `restart`) so the CLI and UI share them; long actions run as server `Job`s (`keep_going=True` for independent steps, `after=` for a rescan).
- Commit messages explain the why; end with the co-author line used in this repo's history.
