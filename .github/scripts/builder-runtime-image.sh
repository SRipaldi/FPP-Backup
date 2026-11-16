#!/bin/bash
# Save the card-builder container as tools/build_image.sh actually runs it: the digest-pinned base image
# from tools/builder/Dockerfile plus the packages build_image.sh installs at run time. Scanning only the
# base image would miss those packages.
#
# Usage: .github/scripts/builder-runtime-image.sh <out.tar>
set -euo pipefail

out=$1
base=$(sed -n 's/^FROM //p' tools/builder/Dockerfile)
[[ "$base" == *@sha256:* ]] || { echo "::error::builder image is not pinned by digest: '$base'" >&2; exit 1; }
# Keep this list the same as build_image.sh's apt-get install line.
pkgs=$(sed -n 's/.*apt-get -qq install -y \([a-z0-9 .+-]*\) >.*/\1/p' tools/build_image.sh)
[ -n "$pkgs" ] || { echo "::error::could not read the package list from tools/build_image.sh" >&2; exit 1; }

docker pull --quiet "$base"
docker rm -f builder-runtime >/dev/null 2>&1 || true
# shellcheck disable=SC2086  # the package list is meant to split into words
docker run --name builder-runtime "$base" bash -euo pipefail -c "apt-get -qq update && apt-get -qq install -y $pkgs >/dev/null"
docker commit builder-runtime fpp-backup/builder-runtime:scan >/dev/null
docker rm builder-runtime >/dev/null
docker save fpp-backup/builder-runtime:scan -o "$out"
echo "builder runtime image: $base + $pkgs -> $out"
