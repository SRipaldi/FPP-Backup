#!/usr/bin/env python3
"""Flash a built FPP image to an SD card with bb-imager-cli, then verify it.

Safety: the target must be a card bb-imager-cli itself lists as an SD
destination, removable, no larger than 256 GB and big enough for the image.
Without --yes the disk id must be typed back before anything is written.

Verify: the card is read back through macOS authopen (admin dialog, no sudo).
The partition table and every non-FAT partition must match the image byte for
byte. The FAT boot partition is compared file by file against the
<image>.boot-sums written by build_image.sh, because the flasher appends its
(empty) customization to sysconf.txt and may touch FAT metadata.

Usage:
  tools/flash_card.py --list
  tools/flash_card.py work/out/FPP-Side-10.3.img disk4 [--yes] [--no-verify]
"""
import argparse
import hashlib
import os
import plistlib
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAX_CARD = 256_000_000_000
CHUNK = 4 * 1024 * 1024
FAT_TYPES = {0x01, 0x04, 0x06, 0x0B, 0x0C, 0x0E}


def imager():
    p = os.environ.get("BB_IMAGER_CLI") or str(ROOT / "work/bin/bb-imager-cli")
    if not (Path(p).exists() or shutil.which(p)):
        sys.exit(f"bb-imager-cli not found at {p}; run tools/setup_bb_imager.sh")
    return p


def sd_destinations():
    out = subprocess.run([imager(), "list-destinations", "sd", "--no-frills"],
                         capture_output=True, text=True, check=True).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def disk_info(disk):
    out = subprocess.run(["diskutil", "info", "-plist", disk], capture_output=True, check=True).stdout
    return plistlib.loads(out)


def mbr_partitions(mbr):
    if mbr[510:512] != b"\x55\xaa":
        sys.exit("image has no MBR partition table")
    parts = []
    for i in range(4):
        _, _, ptype, _, start, count = struct.unpack_from("<B3sB3sII", mbr, 446 + 16 * i)
        if ptype and count:
            parts.append({"n": i + 1, "type": ptype, "start": start * 512, "size": count * 512})
    return parts


def check_target(image, disk):
    img_bytes = image.stat().st_size
    raw = f"/dev/r{disk}"
    listed = sd_destinations()
    if raw not in listed:
        sys.exit(f"{raw} is not an SD destination per bb-imager-cli (sees: {listed or 'none'})")
    info = disk_info(disk)
    size = info.get("TotalSize") or info.get("Size")
    if not info.get("RemovableMedia") and info.get("BusProtocol") not in ("Secure Digital", "USB"):
        sys.exit(f"refusing {disk}: not removable media")
    if size > MAX_CARD:
        sys.exit(f"refusing {disk}: {size / 1e9:.0f} GB does not look like an SD card")
    if size < img_bytes:
        sys.exit(f"card {size / 1e9:.1f} GB is smaller than image {img_bytes / 1e9:.1f} GB")
    return info, size


def flash(image, disk):
    print(f"Flashing {image.name} to /dev/r{disk} (macOS will ask for an admin password)...")
    subprocess.run([imager(), "flash", "sd", str(image), f"/dev/r{disk}"], check=True)


def verify(image, disk, parts):
    end = max(p["start"] + p["size"] for p in parts)
    print(f"Verifying: reading back {end / 1e9:.2f} GB (macOS will ask for an admin password)...")
    proc = subprocess.Popen(["/usr/libexec/authopen", "-r", f"/dev/r{disk}"],
                            stdout=subprocess.PIPE, bufsize=0)
    card, pos, failures = proc.stdout, 0, []

    def read_exact(n):
        nonlocal pos
        buf = bytearray()
        while len(buf) < n:
            b = card.read(min(CHUNK, n - len(buf)))
            if not b:
                raise IOError(f"card read ended at byte {pos + len(buf)}")
            buf += b
        pos += n
        return bytes(buf)

    def hash_card(n):
        nonlocal pos
        h, left = hashlib.sha256(), n
        while left:
            b = card.read(min(CHUNK, left))
            if not b:
                raise IOError(f"card read ended at byte {pos}")
            h.update(b)
            left -= len(b)
            pos += len(b)
        return h.hexdigest()

    try:
        with open(image, "rb") as img:
            img_mbr = img.read(512)
            if read_exact(512)[446:510] != img_mbr[446:510]:
                failures.append("partition table differs")
            for p in sorted(parts, key=lambda p: p["start"]):
                skip = p["start"] - pos
                while skip:
                    skip -= len(card.read(min(CHUNK, skip)))
                pos = p["start"]
                if p["type"] in FAT_TYPES:
                    hash_card(p["size"])  # read past; checked by files below
                    continue
                img.seek(p["start"])
                want = hashlib.sha256()
                left = p["size"]
                while left:
                    b = img.read(min(CHUNK, left))
                    want.update(b)
                    left -= len(b)
                got = hash_card(p["size"])
                ok = got == want.hexdigest()
                print(f"  partition {p['n']} (type {p['type']:#04x}, {p['size'] / 1e6:.0f} MB): {'match' if ok else 'MISMATCH'}")
                if not ok:
                    failures.append(f"partition {p['n']} differs")
    finally:
        proc.kill()
        proc.wait()

    failures += verify_boot_files(image, disk, parts)
    return failures


def verify_boot_files(image, disk, parts):
    sums_file = Path(str(image) + ".boot-sums")
    fat = [p for p in parts if p["type"] in FAT_TYPES]
    if not fat:
        return []
    if not sums_file.exists():
        return [f"no {sums_file.name}; rebuild the image to check the boot partition"]
    dev = f"{disk}s{fat[0]['n']}"
    subprocess.run(["diskutil", "mount", "readOnly", dev], capture_output=True)
    mnt = disk_info(dev).get("MountPoint")
    if not mnt:
        return [f"could not mount {dev} to check boot files"]
    failures = []
    for line in sums_file.read_text().splitlines():
        want, size, rel = line.split(None, 2)
        f = Path(mnt) / rel.removeprefix("./")
        if not f.exists():
            failures.append(f"boot file missing: {rel}")
            continue
        data = f.read_bytes()
        if rel.endswith("sysconf.txt"):
            # bb-imager-cli appends its customization; the original must be intact.
            data = data[:int(size)]
        if hashlib.sha256(data).hexdigest() != want:
            failures.append(f"boot file differs: {rel}")
    print(f"  boot partition: {len(sums_file.read_text().splitlines())} files checked")
    return failures


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image", nargs="?", type=Path)
    ap.add_argument("disk", nargs="?", help="whole disk id, e.g. disk4")
    ap.add_argument("--list", action="store_true", help="list SD cards and exit")
    ap.add_argument("--yes", action="store_true", help="skip the typed confirmation (for the UI)")
    ap.add_argument("--no-verify", action="store_true")
    args = ap.parse_args()

    if args.list:
        for d in sd_destinations():
            disk = d.removeprefix("/dev/r").removeprefix("/dev/")
            info = disk_info(disk)
            print(f"{disk}\t{(info.get('TotalSize') or 0) / 1e9:.1f} GB\t{info.get('MediaName', '')}")
        return
    if not args.image or not args.disk:
        ap.error("image and disk are required")
    disk = args.disk.removeprefix("/dev/").removeprefix("r") if re.match(r"^(/dev/)?r?disk\d+$", args.disk) else None
    if not disk:
        ap.error("disk must look like disk4")
    if not args.image.is_file():
        sys.exit(f"no image: {args.image}")

    with open(args.image, "rb") as f:
        parts = mbr_partitions(f.read(512))
    info, size = check_target(args.image, disk)

    print(f"Target: /dev/{disk}  {info.get('MediaName', '')}, {info.get('BusProtocol', '')}, {size / 1e9:.1f} GB")
    subprocess.run(["diskutil", "list", disk])
    print(f"Image:  {args.image} ({args.image.stat().st_size / 1e9:.2f} GB)")
    print(f"EVERYTHING on /dev/{disk} will be erased.")
    if not args.yes and input(f"Type the disk id ({disk}) to continue: ").strip() != disk:
        sys.exit("aborted")

    flash(args.image, disk)
    if args.no_verify:
        print("Flashed (not verified).")
        return
    failures = verify(args.image, disk, parts)
    if failures:
        print("VERIFY FAILED:\n  " + "\n  ".join(failures), file=sys.stderr)
        sys.exit(1)
    print("Verified: card matches the image.")
    subprocess.run(["diskutil", "eject", disk], capture_output=True)
    print("Card ejected; safe to remove.")


if __name__ == "__main__":
    main()
