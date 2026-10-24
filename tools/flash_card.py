#!/usr/bin/env python3
"""Flash a built FPP image to an SD card and verify it, without root.

Raw access goes through macOS /usr/libexec/authopen, the same way the
BeagleBoard Imaging Utility does it: authopen shows the admin dialog, opens the
card read-write and passes the open descriptor back over a socket. This process
never runs as root and needs no Full Disk Access (a root shell from sudo or
osascript is blocked from raw disks by macOS privacy protection).

Safety: the target must be a card bb-imager-cli lists as an SD destination,
removable, no larger than 256 GB and big enough for the image. Without --yes
the disk id must be typed back before anything is written.

Verify: the card is read back through the same descriptor and every partition
plus the partition table must match the image byte for byte.

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
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAX_CARD = 256_000_000_000
CHUNK = 4 * 1024 * 1024  # a multiple of 512, as raw devices require


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
    if img_bytes % 512:
        sys.exit("image size is not a multiple of 512 bytes")
    raw = f"/dev/r{disk}"
    listed = sd_destinations()
    if raw not in listed:
        sys.exit(f"{raw} is not an SD destination per bb-imager-cli (sees: {listed or 'none'})")
    info = disk_info(disk)
    size = info.get("TotalSize") or info.get("Size")
    if not info.get("RemovableMedia") and info.get("BusProtocol") not in ("Secure Digital", "USB"):
        sys.exit(f"refusing {disk}: not removable media")
    if info.get("WritableMedia") is False:
        sys.exit(f"refusing {disk}: card is write-protected (check the lock switch)")
    if size > MAX_CARD:
        sys.exit(f"refusing {disk}: {size / 1e9:.0f} GB does not look like an SD card")
    if size < img_bytes:
        sys.exit(f"card {size / 1e9:.1f} GB is smaller than image {img_bytes / 1e9:.1f} GB")
    return info, size


def authopen_rw(path):
    """Open path read-write via authopen (admin dialog); return the descriptor."""
    ours, theirs = socket.socketpair()
    proc = subprocess.Popen(["/usr/libexec/authopen", "-stdoutpipe", "-o", str(os.O_RDWR), path],
                            stdout=theirs, stderr=subprocess.PIPE)
    theirs.close()
    try:
        _, fds, _, _ = socket.recv_fds(ours, 16, 1)
    finally:
        ours.close()
    err = proc.communicate()[1].decode().strip()
    if not fds:
        sys.exit(f"authopen did not open {path}: {err or 'cancelled or denied'}; nothing was written")
    return fds[0]


def progress(label, done, total, started):
    rate = done / max(time.time() - started, 0.001) / 1e6
    print(f"\r  {label}: {done / 1e9:5.2f} / {total / 1e9:.2f} GB  {rate:5.1f} MB/s", end="", flush=True)


def flash(fd, image):
    total = image.stat().st_size
    started, done = time.time(), 0
    os.lseek(fd, 0, os.SEEK_SET)
    with open(image, "rb", buffering=0) as img:
        while True:
            buf = img.read(CHUNK)
            if not buf:
                break
            view = memoryview(buf)
            while view:
                view = view[os.write(fd, view):]
            done += len(buf)
            if done % (64 * CHUNK) == 0 or done == total:
                progress("writing", done, total, started)
    os.fsync(fd)
    print()


def sha_range(read, start, size):
    h, left = hashlib.sha256(), size
    while left:
        b = read(start + size - left, min(CHUNK, left))
        if not b:
            raise IOError(f"read ended {left} bytes short at offset {start + size - left}")
        h.update(b)
        left -= len(b)
    return h.hexdigest()


def verify(fd, image, parts):
    print("Verifying: reading the card back...", flush=True)
    failures = []
    card = lambda off, n: os.pread(fd, n, off)
    with open(image, "rb") as f:
        img = lambda off, n: (f.seek(off), f.read(n))[1]
        if card(0, 512)[446:510] != img(0, 512)[446:510]:
            failures.append("partition table differs")
        for p in parts:
            ok = sha_range(card, p["start"], p["size"]) == sha_range(img, p["start"], p["size"])
            print(f"  partition {p['n']} (type {p['type']:#04x}, {p['size'] / 1e6:.0f} MB): "
                  f"{'match' if ok else 'MISMATCH'}", flush=True)
            if not ok:
                failures.append(f"partition {p['n']} differs")
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
    m = re.match(r"^(?:/dev/)?r?(disk\d+)$", args.disk)
    if not m:
        ap.error("disk must look like disk4")
    disk = m.group(1)
    if not args.image.is_file():
        sys.exit(f"no image: {args.image}")

    with open(args.image, "rb") as f:
        parts = mbr_partitions(f.read(512))
    info, size = check_target(args.image, disk)

    if not args.yes:
        print(f"Target: /dev/{disk}  {info.get('MediaName', '')}, {info.get('BusProtocol', '')}, {size / 1e9:.1f} GB")
        subprocess.run(["diskutil", "list", disk])
        print(f"Image:  {args.image} ({args.image.stat().st_size / 1e9:.2f} GB)")
        print(f"EVERYTHING on /dev/{disk} will be erased.")
        if input(f"Type the disk id ({disk}) to continue: ").strip() != disk:
            sys.exit("aborted")

    subprocess.run(["diskutil", "unmountDisk", f"/dev/{disk}"], capture_output=True, check=True)
    print("macOS will ask for an admin password...", flush=True)
    fd = authopen_rw(f"/dev/r{disk}")
    try:
        print(f"Flashing {args.image.name} to /dev/r{disk}...", flush=True)
        flash(fd, args.image)
        failures = [] if args.no_verify else verify(fd, args.image, parts)
    finally:
        os.close(fd)

    if failures:
        print("VERIFY FAILED:\n  " + "\n  ".join(failures), file=sys.stderr)
        sys.exit(1)
    print("Flashed (not verified)." if args.no_verify else "Verified: card matches the image.")
    subprocess.run(["diskutil", "eject", disk], capture_output=True)
    print("Card ejected; safe to remove.")


if __name__ == "__main__":
    main()
