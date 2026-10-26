#!/usr/bin/env python3
"""List and download stock FPP SD images from FalconChristmas/fpp releases.

Downloads <asset>.img.zip into work/images/ and unzips it next to it. The zip
must match the size and SHA-256 that GitHub publishes for the release asset.

Usage:
  tools/get_image.py --list [--limit 5]
  tools/get_image.py 10.3 FPP-v10.3-BBB.img.zip
  tools/get_image.py 10.3 --platform "BeagleBone Black"
"""
import argparse
import hashlib
import json
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGES = ROOT / "work/images"
API = "https://api.github.com/repos/FalconChristmas/fpp/releases"
CHUNK = 1024 * 1024


def releases(limit=5):
    req = urllib.request.Request(f"{API}?per_page={limit}",
                                 headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    return [{"tag": r["tag_name"], "published": r["published_at"],
             "prerelease": r["prerelease"],
             "assets": [{"name": a["name"], "size": a["size"], "url": a["browser_download_url"],
                         "sha256": (a.get("digest") or "").removeprefix("sha256:") or None}
                        for a in r["assets"] if a["name"].endswith(".img.zip")]}
            for r in data if not r["draft"]]


def asset_for_platform(platform, variant=""):
    """Image family for a unit, from FPP's Platform/SubPlatform strings."""
    text = f"{platform} {variant}".lower()
    if "pocketbeagle2" in text.replace(" ", "") or "beaglebone 64" in text or "beagley" in text:
        return "BB64"
    if "beaglebone" in text:
        return "BBB"
    if "raspberry" in text:
        return "Pi64"
    return None


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for buf in iter(lambda: f.read(CHUNK), b""):
            h.update(buf)
    return h.hexdigest()


def download(url, dest, size, sha256):
    """Download to <dest>.part and rename only once size and SHA-256 match."""
    tmp = dest.with_suffix(dest.suffix + ".part")
    h = hashlib.sha256()
    started, done = time.time(), 0
    with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        while True:
            buf = resp.read(CHUNK)
            if not buf:
                break
            out.write(buf)
            h.update(buf)
            done += len(buf)
            if done % (32 * CHUNK) < CHUNK or done == total:
                rate = done / max(time.time() - started, 0.001) / 1e6
                print(f"\r  downloading: {done / 1e6:7.0f} / {total / 1e6:.0f} MB  {rate:5.1f} MB/s",
                      end="", flush=True)
    print()
    if done != size:
        tmp.unlink()
        sys.exit(f"download incomplete: {done} of {size} bytes")
    if sha256 and h.hexdigest() != sha256:
        tmp.unlink()
        sys.exit(f"download does not match the SHA-256 GitHub publishes ({sha256}); discarded")
    print("  verified: SHA-256 matches the release" if sha256 else "  size matches (the release publishes no SHA-256)")
    tmp.rename(dest)


def unzip(zpath):
    with zipfile.ZipFile(zpath) as z:
        imgs = [n for n in z.namelist() if n.endswith(".img")]
        if len(imgs) != 1:
            sys.exit(f"expected one .img in {zpath.name}, found {imgs}")
        info = z.getinfo(imgs[0])
        out = IMAGES / Path(imgs[0]).name
        done = 0
        with z.open(info) as src, open(out, "wb") as dst:
            while True:
                buf = src.read(CHUNK)
                if not buf:
                    break
                dst.write(buf)
                done += len(buf)
                if done % (256 * CHUNK) < CHUNK or done == info.file_size:
                    print(f"\r  unzipping: {done / 1e9:5.2f} / {info.file_size / 1e9:.2f} GB", end="", flush=True)
        print()
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tag", nargs="?")
    ap.add_argument("asset", nargs="?")
    ap.add_argument("--platform", help="pick the asset for this FPP Platform string")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args()

    if args.list:
        print(json.dumps(releases(args.limit), indent=2))
        return
    if not args.tag:
        ap.error("tag is required")
    rel = next((r for r in releases(20) if r["tag"] == args.tag), None)
    if not rel:
        sys.exit(f"no release {args.tag}")
    name = args.asset
    if not name and args.platform:
        fam = asset_for_platform(args.platform)
        name = fam and f"FPP-v{args.tag}-{fam}.img.zip"
    asset = next((a for a in rel["assets"] if a["name"] == name), None)
    if not asset:
        sys.exit(f"no asset {name!r} in {args.tag}; have {[a['name'] for a in rel['assets']]}")

    IMAGES.mkdir(parents=True, exist_ok=True)
    zpath = IMAGES / asset["name"]
    if not asset["sha256"]:
        print("warning: GitHub publishes no SHA-256 for this asset; only its size is checked")
    if zpath.exists() and zpath.stat().st_size == asset["size"] and (
            not asset["sha256"] or sha256_of(zpath) == asset["sha256"]):
        print(f"already downloaded and verified: {zpath.name}")
    else:
        print(f"Downloading {asset['name']} ({asset['size'] / 1e6:.0f} MB)")
        download(asset["url"], zpath, asset["size"], asset["sha256"])
    img = IMAGES / zpath.name.removesuffix(".zip")
    if img.exists():
        print(f"already unzipped: {img.name}")
    else:
        img = unzip(zpath)
    print(f"ready: {img.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
