#!/bin/bash
# Build a ready-to-flash FPP image: a copy of a stock FPP image with a staged
# /home/fpp/media tree (from tools/stage_card.py) written into its root
# partition. Runs the filesystem work in a Rancher Desktop container; the stock
# image is never modified and no physical disk is touched.
#
# Usage: tools/build_image.sh <stock.img> <stage-dir> <out.img>
set -euo pipefail

STOCK=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
STAGE=$(cd "$2" && pwd)
OUT=$(cd "$(dirname "$3")" && pwd)/$(basename "$3")

# The image holds the controller's passwords and Wi-Fi keys: readable by this user only.
umask 077
# Pinned by digest in tools/builder/Dockerfile's FROM line (Dependabot proposes bumps).
BUILDER_IMAGE=${BUILDER_IMAGE:-$(sed -n 's/^FROM //p' "$(dirname "$0")/builder/Dockerfile")}
[[ "$BUILDER_IMAGE" == *@sha256:* ]] || { echo "builder image is not pinned by digest: '$BUILDER_IMAGE'" >&2; exit 1; }

export PATH="$HOME/.rd/bin:$PATH"
export DOCKER_HOST="${DOCKER_HOST:-unix://$HOME/.rd/docker.sock}"

[ -f "$STOCK" ] || { echo "no stock image: $STOCK" >&2; exit 1; }
[ -f "$STAGE/settings" ] || { echo "no settings in stage dir: $STAGE" >&2; exit 1; }
# OS-level settings staged by stage_card.py (NTP server, services).
ROOTFS_SH="$STAGE.rootfs.sh"
[ -f "$ROOTFS_SH" ] || { echo "no $ROOTFS_SH; re-run stage_card.py" >&2; exit 1; }
[ "$STOCK" != "$OUT" ] || { echo "refusing to write over the stock image" >&2; exit 1; }

# APFS clone: instant, and the stock file stays untouched.
rm -f "$OUT"
cp -c "$STOCK" "$OUT"

docker run --rm --privileged \
    -v "$OUT:/img/out.img" -v "$STAGE:/stage:ro" -v "$ROOTFS_SH:/rootfs.sh:ro" \
    "$BUILDER_IMAGE" bash -euo pipefail -c '
apt-get -qq update >/dev/null && apt-get -qq install -y fdisk rsync e2fsprogs dosfstools >/dev/null 2>&1

IMG=/img/out.img
# Partition 1 = FAT boot, last Linux (83) partition = root.
read BOOT_START BOOT_SIZE < <(sfdisk -d $IMG | awk -F"[=,]" "/type=c/ {print \$2+0, \$4+0; exit}")
read ROOT_START ROOT_SIZE < <(sfdisk -d $IMG | awk -F"[=,]" "/type=83/ {s=\$2+0; z=\$4+0} END {print s, z}")
BOOT=$(losetup -f --show -o $((BOOT_START*512)) --sizelimit $((BOOT_SIZE*512)) $IMG)
ROOT=$(losetup -f --show -o $((ROOT_START*512)) --sizelimit $((ROOT_SIZE*512)) $IMG)
# set -e applies inside the trap too, so each cleanup step must not fail.
trap "umount -q /mnt/boot /mnt/root 2>/dev/null || true; losetup -d $BOOT $ROOT 2>/dev/null || true" EXIT

mkdir -p /mnt/boot /mnt/root
mount $ROOT /mnt/root
mount $BOOT /mnt/boot
MEDIA=/mnt/root/home/fpp/media
[ -d $MEDIA/config ] || { echo "not an FPP image: no $MEDIA/config" >&2; exit 1; }

rsync -a --no-owner --no-group /stage/ $MEDIA/
chown -R 1000:1000 $MEDIA
find $MEDIA -type d -exec chmod 775 {} +
find $MEDIA -type f -exec chmod 664 {} +
chmod 755 $MEDIA/scripts/*.sh 2>/dev/null || true

# The restore marker FPP s own restore sets (www/backup.php): settings
# migrations and user packages on first boot. Not BootActions; see stage_card.py.
touch /mnt/root/fpp_restored
ROOTFS=/mnt/root bash /rootfs.sh
echo "applied OS settings: $(grep -c . /rootfs.sh) lines"
# Grow the root partition to fill the card on first boot (FPPINIT checkUnpartitionedSpace).
touch /mnt/boot/fpp_expand_rootfs

sync
df -h /mnt/root | tail -1
umount /mnt/boot /mnt/root
fsck.vfat -n $BOOT >/dev/null
echo "boot fs: clean"
e2fsck -fn $ROOT >/dev/null
echo "root fs: clean"

# Loop devices live in the Rancher VM, not the container; leaked ones pile up
# across runs until losetup runs out.
sync
for i in 1 2 3 4 5; do
    losetup -d $BOOT $ROOT 2>/dev/null || true
    [ -z "$(losetup -j $IMG)" ] && break
    sleep 1
done
[ -z "$(losetup -j $IMG)" ] || { echo "loop devices still attached: $(losetup -j $IMG)" >&2; exit 1; }
'
echo "built $OUT"
