# FPP-Backup

Backup and restore for an FPP (Falcon Player) light show: snapshot each controller, then build an SD card that boots as that controller (same hostname, network, settings, outputs and sequences), optionally on a newer FPP version.

See [FPP-Backup-handoff.md](FPP-Backup-handoff.md) for the original decisions and phased plan.

## Requirements (macOS)

- Python 3.9+ (the system `python3` is fine; no packages needed)
- Rancher Desktop (dockerd/moby engine) for building images
- Rust (`cargo`) once, to build `bb-imager-cli` for SD card detection: `tools/setup_bb_imager.sh`

## UI

```
python3 ui/server.py
```

Opens `http://127.0.0.1:8765/`. Start it from Terminal or VS Code so the macOS admin dialog can appear when flashing. Options: `--port`, `--no-browser`, `--no-monitor`, and `--master <player-ip>` to override the saved player for one run.

**First run:** the app opens on **Settings**. Enter the address of your FPP *player* (the controller that runs the show; every other controller is reached through its `/proxy/` and found through MultiSync), press **Test connection**, save, then **Build inventory from the player**. Settings are saved in `config/settings.json`, which is per computer and not in Git.

- **Dashboard**: controllers online vs expected, policy drift, versions and available updates, backups due, sequence coverage, the show, and what needs attention.
- **Backup > Controllers**: back up the selected controllers (configuration, optionally media).
- **Fleet Management > Fleet**: compare every controller with `fleet-policy.json`, edit the policy, update drifting settings. **Sequences**: every sequence the schedule plays must be on every controller.
- **Show > Live**: during a show, whether each remote plays the same sequence at the same point and receives the player's sync.
- **Image Management**: build a card image from a backup, flash and verify an SD card.
- **Notifications** (bell): controllers going offline, dark remotes during a show, failed jobs, drift, missing sequences, new releases.

## Command line

The UI runs exactly these:

```
tools/snapshot.py [--media] --via <player-ip> <unit-ip>      # or: tools/snapshot.py http://<unit-ip>
tools/fleet.py audit | drift | set | restart                  # player from config/settings.json, or --master
tools/get_image.py 10.3 --platform "BeagleBone Black"         # stock image into work/images/
tools/stage_card.py snapshots/<host>/<ts> work/stage/<host>
tools/build_image.sh work/images/<stock>.img work/stage/<host> work/out/<host>-<ver>.img
tools/flash_card.py work/out/<host>-<ver>.img <diskN>
```

## Safety

- Everything that talks to controllers only reads (HTTP GET). Nothing is written to a running controller.
- `snapshots/` and `work/` hold passwords and Wi-Fi keys and are gitignored. Never commit them.
- Flashing only targets disks that `bb-imager-cli` lists as SD cards, refuses anything over 256 GB, requires the disk id to be typed, and verifies the card byte for byte afterwards.
- A new card must not boot while the unit it replaces is running (same hostname and IP).
