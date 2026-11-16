#!/bin/bash
# Check out bb-imager-rs at exactly the commit tools/setup_bb_imager.sh builds, so its Cargo.lock (the
# Rust dependencies that end up in bb-imager-cli) can be scanned. Read-only: nothing is built.
#
# Usage: .github/scripts/fetch-bb-imager.sh <dir>
set -euo pipefail

dir=$1
version=$(sed -n 's/^VERSION=\${1:-\(.*\)}$/\1/p' tools/setup_bb_imager.sh)
pinned=$(sed -n 's/^PINNED_COMMIT=\([0-9a-f]\{40\}\)$/\1/p' tools/setup_bb_imager.sh)
[ -n "$version" ] && [ -n "$pinned" ] || { echo "::error::could not read VERSION/PINNED_COMMIT from tools/setup_bb_imager.sh" >&2; exit 1; }

git clone -q --depth 1 --branch "$version" https://github.com/beagleboard/bb-imager-rs.git "$dir"
got=$(git -C "$dir" rev-parse HEAD)
[ "$got" = "$pinned" ] || { echo "::error::bb-imager tag $version is at $got, expected $pinned" >&2; exit 1; }
[ -f "$dir/Cargo.lock" ] || { echo "::error::no Cargo.lock in bb-imager $version" >&2; exit 1; }
echo "bb-imager $version at $pinned"
