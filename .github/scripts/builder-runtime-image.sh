#!/bin/bash
# Build and save the card-builder container exactly as tools/build_image.sh builds it (from
# tools/builder/Dockerfile, fresh, no cache), so the scanners see the packages a real build gets.
#
# Usage: .github/scripts/builder-runtime-image.sh <out.tar>
set -euo pipefail

out=$1
grep -Eq '^FROM [^ ]+@sha256:[0-9a-f]{64}$' tools/builder/Dockerfile \
    || { echo "::error::builder base image is not pinned by digest in tools/builder/Dockerfile" >&2; exit 1; }
docker build --quiet --no-cache --tag fpp-backup/builder:scan tools/builder >/dev/null
docker save fpp-backup/builder:scan -o "$out"
echo "builder image: $(sed -n 's/^FROM //p' tools/builder/Dockerfile) + $(sed -n 's/.*apk add --no-cache //p' tools/builder/Dockerfile) -> $out"
