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

Opens `http://127.0.0.1:8765/`. Start it from Terminal or VS Code so the macOS admin dialog can appear when flashing. Options: `--master <player ip>` (default `172.1.2.10`), `--port`, `--no-browser`.

- **Controllers**: every unit the player knows about, live; snapshot one (config, or config + media).
- **Build card**: pick a snapshot and an FPP release; download the stock image if needed; build.
- **Flash card**: pick a built image and a detected SD card, type the disk id, flash and verify.
- **Jobs**: live output of every action.

## Command line

The UI runs exactly these:

```
tools/snapshot.py [--media] --via <player-ip> <unit-ip>      # or: tools/snapshot.py http://<unit-ip>
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
