# FPP-Backup

Back up, monitor and rebuild the controllers of an [FPP (Falcon Player)](https://github.com/FalconChristmas/fpp) light show from one Mac.

- **Back up** every controller's configuration and, optionally, its media (sequences, music, video).
- **Keep the fleet consistent**: compare every controller against a policy (time zone, time server, location, privacy, services and anything else you choose) and fix drift in a click.
- **Check the show will play**: every sequence the schedule uses is on every controller, and during a show each remote is playing the same thing at the same time.
- **Rebuild or upgrade a controller onto a fresh SD card**: turn a backup into a ready-to-boot card image, on the same or a newer FPP version, flash it and verify it. The card comes up as the old controller (hostname, static address, settings, outputs, sequences) with no setup steps.
- **Get told when something changes**: controllers going offline, remotes dark during a show, failed jobs, drift, missing sequences, new FPP releases.

Everything runs locally: a small web UI in your browser and the command-line tools behind it.

Not affiliated with or endorsed by the Falcon Player (FPP) project. "FPP" and "Falcon Player" refer to that project's software.

## Status

Working and in use on an 11-controller show (Raspberry Pi 4, BeagleBone Black with KulpLights capes, PocketBeagle 2; FPP 8.5.1 and 10.3).

- Proven end to end: an FPP 8.5.1 BeagleBone rebuilt onto a 10.3 card with the old card kept as a rollback; it booted with its static address, settings, outputs and all sequences, and passed `tools/verify_unit.py`.
- Card images have been built and booted for BeagleBone Black (BBB images). Raspberry Pi and BB64 images go through the same code but haven't been built and booted yet. Flashing works for any FPP image.
- macOS only for flashing (uses `/usr/libexec/authopen`). Everything else is plain Python.

## Requirements

| What | Why | Notes |
|---|---|---|
| macOS | flashing SD cards | other OSes can run the UI and backups, not the flash step |
| Python 3.9+ | everything | no packages to install. macOS's own `python3` (3.9.6, from the Command Line Tools) works but is past end of life; a current Python (python.org or Homebrew) is better |
| Network access to your FPP player | everything | the player is the one controller the app talks to directly; others are reached through its `/proxy/` |
| [Rancher Desktop](https://rancherdesktop.io/) (dockerd / moby engine) | building card images | any Docker engine works; the tools look for Rancher's socket at `~/.rd/docker.sock` |
| Rust (`cargo`), once | SD card detection | `tools/setup_bb_imager.sh` builds the BeagleBoard Imaging Utility's CLI |
| An SD card reader | flashing | built-in or USB |
| [xLights](https://xlights.org/) on this computer, with its automation port on | pushing missing sequences | xLights **Preferences**: set **xFade port** to **A** (port 49913) |

## Install

```bash
git clone <this repo> FPP-Backup && cd FPP-Backup
tools/setup_bb_imager.sh          # once: builds bb-imager-cli into work/bin/ (needs cargo)
python3 ui/server.py              # opens http://127.0.0.1:8765/
```

Start the server from Terminal or VS Code (not as a background service): flashing shows the macOS administrator dialog, which needs your login session.

Server options: `--port 8765`, `--no-browser`, `--no-monitor` (no background watching), `--master <player-ip>` (use a different player for this run only).

## Configure (first run)

The app opens on **Settings** until a player is saved.

1. **Player**: enter the address of the FPP controller that runs your show (the MultiSync player). Press **Test connection**: it shows the player's name, version and mode, and the controllers it knows. Use an address this computer can reach directly. **Save settings.**
2. **Controller passwords** (only if FPP's UI password is on): enter one password for every controller, and/or a password for individual controllers. They're stored in this Mac's Keychain (service "FPP-Backup"), never in a file. **Test connection** has its own password field for trying one before saving it. A controller that answers but rejects the password shows as *needs its UI password* instead of offline.
3. **Controller inventory**: press **Build inventory from the player**. This records which controllers should exist, so one that stops answering shows as *offline* (instead of disappearing) and a new one shows as *unknown*. **Add controller** adds one by address (with its password, if it has one) after checking that FPP answers there, directly or through the player: for a controller the player's MultiSync doesn't list. The × on each controller removes it.
4. **Fleet policy**: created automatically on the first scan from `templates/fleet-policy.json`, with every value taken from your player's own settings, so the fleet starts out matching its player. Review it on the same page (see *Settings > Fleet policy* below).
5. **Monitoring and checks**: background watching on/off and how often, the GitHub update check, and after how many days a backup is "due".

All of this is saved in `config/` on this computer and is not in Git:

| File | Contents |
|---|---|
| `config/settings.json` | player address, monitoring, update check, backup reminder |
| `config/fleet-inventory.json` | the controllers that should exist (host, address, role) |
| `config/fleet-policy.json` | what should match across controllers |

## Using it

### Dashboard
The landing page. Tiles for controllers online vs expected, policy drift, controllers on the target FPP version, updates available, backups due, sequence coverage and health (clock, storage). **Needs attention** lists what to fix, each with a button to the page that fixes it; unknown controllers can be added to the inventory from here. Also shows the show (playing, or the next scheduled playlist), the latest FPP release, and a status table with a dot per controller (green online, blue playing, red offline).

The **↻** next to a controller's name, on the Dashboard, Fleet and Controllers pages, rescans just that controller: about 15 seconds instead of a minute for the whole fleet (the Controllers page only refreshes its status, in a moment).

### Backup > Controllers
Tick controllers, tick **Media** for the ones whose sequences, music and video you also want, then **Back up selected** (or **Back up all**). Each backup is a timestamped folder under `snapshots/<host>/`. Media is incremental: a file the controller hasn't changed since its previous backup is hard-linked from that backup instead of downloaded, so a repeat backup takes seconds and no extra disk space, yet every backup folder is still complete. (`tools/snapshot.py --full` downloads everything.) Backing up only reads from the controllers. Take a backup with media before rebuilding a controller's card.

### Fleet Management > Fleet
Every controller compared with the fleet policy. Red cells differ from the expected value.
- Tick drifting settings and press **Update checked**: each setting is written to just the controllers that differ, one at a time, then the players that need it are restarted (any that are playing are skipped) and the fleet is rescanned. **Apply** on a row does the same for one setting.
- **Gateway** is checked too: each controller's default gateway against a rule per network (`172.1.2.x → 172.1.2.1`), first match wins for a controller on two networks. Until you press **Keep these rules** (or add `network.gateway` to the policy), the rules are inferred from what most controllers use. **Apply** saves the gateway through FPP's network API; it takes effect at the controller's next reboot, so a running show isn't disturbed. The Dashboard also flags a gateway that changed since the controller's last backup, which is how one lost in an upgrade shows up.
- Click a red value to make it the expected one instead, or use **Remotes that disagree** to promote a setting into the policy. These edit the policy only.
- The Controllers table shows each unit's FPP version, available updates (a newer release, and newer patches on its own branch), clock offset, uptime and storage.

### Fleet Management > Sequences
Every sequence that a playlist in the player's schedule plays must be on every controller: a remote plays its own copy and stays dark if it is missing. The page shows what is missing where. Tick the missing ones and press **Push checked**: xLights (running on this computer) uploads each one as FPP Connect would, cut down to the channels that controller drives (v2 zstd sparse); the player also gets the audio. The copy on the controller is then checked against the show folder's render (same render ID and frame count) and the page rescans.
- **Check versions** finds copies from an older render: it reads the render ID of each scheduled sequence on the controllers you pick (the dropdown: all, or a few) and compares it with the .fseq in the xLights show folder (or the player's copy when xLights isn't running). The **Render** column shows the result per controller, and older copies are listed with a checkbox, ready for **Push checked**. It only reads; during a show it runs at half pace. The whole fleet takes about two minutes.
- A tag next to a missing sequence says when xLights can't push it as is: *not in show folder*, *not rendered*, or *render is old* (the .xsq was saved after its last render). Tick **Render first** to render it in xLights before uploading; controllers that already have the sequence keep the old render until they get the new one too.
- Pushing is refused while the show is playing, and **Render first** is refused while xLights has a sequence open. xLights is busy until the push finishes: don't quit it, or this app, meanwhile.
- You can still use FPP Connect or FPP's file manager instead; press **Rescan** afterwards.
- **Security:** xLights' automation port has no password and listens on every network interface, and it accepts commands (uploads, controller configuration, scripts) from any device on your network and from web pages open in your browser. Turn it on only while pushing, and off afterwards. Leave out controllers that aren't in the show by listing them under `coverage_exclude` in the policy.

### Show > Live
While a show plays, every 3 seconds: the player's playlist, sequence and position, and for each remote whether it is playing the same sequence at the same point and how many sync packets per second it receives from the player. Verdicts: *in sync*, *DARK*, *wrong sequence*, *ahead/behind*, *no sync packets*. Positions are whole seconds (FPP's resolution). The **Live updates** switch pauses the polling (remembered in this browser); **Refresh** then reads the show once.

### Image Management > Build card
Pick a backup and an FPP release. The matching stock image (BBB, BB64, Pi64) is chosen from the backup's hardware, and **Download image** fetches it if needed. **Build image** writes the backup into a copy of the stock image inside a container and produces `work/out/<host>-<version>.img`. Building onto a newer version is an offline upgrade: settings are migrated (including 10.x's new setup questions and old time zone names) and FPP's own first boot does the rest.

### Image Management > Flash card
Pick a built image and a detected SD card, type the disk id to confirm, then **Erase and flash**. macOS asks for an administrator password once. The card is written, read back and compared partition by partition, then ejected.

Then power the controller off, keep its old card as a rollback, insert the new card and boot. Never boot the new card while the controller it replaces is running (same hostname and address). Check the result with:

```bash
python3 tools/verify_unit.py snapshots/<host>/<ts> --via <player-ip> <unit-ip> --stage work/stage/<host>
```

### Settings > Fleet policy
What should match across the fleet. For each setting: the expected value (suggestions show what the fleet has), the player's own value or *not checked* when the player should differ, aliases to accept, and a note. Also the lists of settings compared only among remotes, and settings that differ by design (shown, never flagged). **Preview** shows which controllers would drift under your changes before you **Save**.

### Notifications
The bell in the header counts unread notifications. While the UI runs, a background monitor watches for controllers going offline or coming back (after two missed checks), the show starting and stopping, and remotes that are dark during the show. Scans add new drift, missing sequences and new FPP releases; jobs add their results.

## Command line

The UI runs these; they also work on their own.

```bash
tools/snapshot.py [--media] --via <player-ip> <unit-ip>       # back up units (read-only)
tools/fleet.py audit                                          # time zone, clock offset, NTP, location per unit
tools/fleet.py drift [--json]                                 # every unit vs the fleet policy
tools/fleet.py set <Setting> <value|master> --hosts a,b|all   # write one setting (master = the player's address)
tools/fleet.py gateway <ip|policy> --hosts a,b             # save a default gateway (applies at next reboot)
tools/fleet.py restart|reboot --hosts a,b|all                 # skips units that are playing
tools/get_image.py --list | <tag> --platform "BeagleBone Black"
tools/stage_card.py snapshots/<host>/<ts> work/stage/<host> [--reference snapshots/<unit-on-target-version>/<ts>]
tools/build_image.sh work/images/<stock>.img work/stage/<host> work/out/<host>-<ver>.img
tools/flash_card.py --list | work/out/<host>-<ver>.img <diskN>
tools/verify_unit.py snapshots/<host>/<ts> --via <player-ip> <unit-ip> [--stage work/stage/<host>]
tools/xlights.py status [<name>.fseq ...]                    # xLights version, show folder (read-only)
tools/xlights.py push <name>.fseq --hosts a,b [--render]      # upload through xLights, verify
tools/xlights.py versions --hosts all|a,b                    # render ID of each copy vs the show folder (read-only)
```

`fleet.py` uses the player saved in `config/settings.json` unless given `--master`.

## Safety

- **Reading** is the default. Backups, scans, coverage, the live view and the monitor only use HTTP GET requests that don't change anything.
- **Writing** to controllers happens only when you ask, after a confirmation: Fleet **Update checked** / **Apply** (through FPP's own settings API), player restarts, and sequence pushes (through xLights). Restarts skip controllers that are playing; pushes wait until the show is not playing.
- `snapshots/`, `work/` and `config/` are gitignored. Backups contain passwords and Wi-Fi keys: never commit them. Backups, staged cards and built images are created readable by your user only.
- Flashing only targets disks listed as SD cards, refuses anything over 256 GB, requires typing the disk id, and verifies the card afterwards. The stock image is never modified.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Flash: "Unable to obtain authorization" or no password dialog | Start `ui/server.py` (or `flash_card.py`) from your own Terminal or VS Code session. |
| Flash: "Operation not permitted" | Something tried to write the disk as root, which macOS privacy protection blocks. Use the app's flash step, which writes through `authopen`. |
| Build: Docker not found or cannot connect | Start Rancher Desktop (dockerd/moby). The tools use `~/.rd/docker.sock`; set `DOCKER_HOST` for another engine. |
| Build: "failed to set up loop device" | Stale loop devices in the Rancher VM: `docker run --rm --privileged debian:trixie-slim losetup -a`, then `losetup -d` the ones that aren't `modloop`. |
| A healthy controller shows offline | The player's proxy was overloaded or slow. Rescan; the monitor only reports offline after two missed checks. |
| A rebuilt card comes up on DHCP or at FPP's setup page | Build the card with this tool, which handles both. |

## Known limits and to-dos

- Plugins are not carried onto a rebuilt card (their settings are); reinstall them from FPP's plugin page.
- Backups run when you start them; scheduled backups need this computer running and are a future addition.
- The release list covers recent FPP releases (10.x); rebuilding onto an older version needs its image added to `work/images/` by hand.
- Only BeagleBone Black card images have been built and booted so far.

## Project layout

```
ui/server.py, ui/static/      web UI (Python standard library server, plain JS)
tools/                        command-line tools (the UI runs these)
templates/fleet-policy.json   default policy for new installations
config/                       per-computer settings, inventory and policy (gitignored)
snapshots/                    backups (gitignored; contain secrets)
work/                         images, staging, builds, caches, logs (gitignored)
```

See [AGENTS.md](AGENTS.md) for development and setup notes for AI coding agents, and [FPP-Backup-handoff.md](FPP-Backup-handoff.md) for the original plan.

## License

[MIT](LICENSE). This repository contains no FPP code. FPP itself is licensed separately (LGPL 2.1, GPL v2 and CC-BY-ND for different parts; see the [FPP repository](https://github.com/FalconChristmas/fpp/blob/master/LICENSE)), and card images this tool builds contain FPP, so don't publish built `.img` files (they also hold your passwords). `tools/setup_bb_imager.sh` downloads and builds the MIT-licensed [BeagleBoard Imaging Utility](https://github.com/beagleboard/bb-imager-rs); if you redistribute a built copy, include its license.
