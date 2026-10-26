#!/bin/bash
# Build a pinned bb-imager-cli (beagleboard/bb-imager-rs, MIT) into work/bin.
#
# Upstream publishes no macOS CLI. flash_card.py uses it to list SD cards;
# writing goes through /usr/libexec/authopen directly, because the CLI's own
# macos_authopen path fails with errAuthorizationInternal (-60008) and a root
# process is blocked from raw disks by macOS privacy protection.
#
# Usage: tools/setup_bb_imager.sh [version]
set -euo pipefail

VERSION=${1:-1.0.20}
# The commit that tag pointed to when it was reviewed; a moved tag is refused.
# Only checked for the default version.
PINNED_COMMIT=441317635f441bf663a0d042f4531b70e8b3110d
ROOT=$(cd "$(dirname "$0")/.." && pwd)
SRC="$ROOT/work/bb-imager-src/$VERSION"
BIN="$ROOT/work/bin"

command -v cargo >/dev/null || { echo "cargo not found; install Rust from https://rustup.rs" >&2; exit 1; }

if [ ! -d "$SRC" ]; then
    git clone -q --depth 1 --branch "$VERSION" https://github.com/beagleboard/bb-imager-rs.git "$SRC"
fi

if [ "$VERSION" = 1.0.20 ]; then
    got=$(git -C "$SRC" rev-parse HEAD)
    [ "$got" = "$PINNED_COMMIT" ] || { echo "tag $VERSION is at $got, expected $PINNED_COMMIT; refusing" >&2; exit 1; }
fi

# --locked: exactly the dependency versions in upstream's Cargo.lock.
(cd "$SRC" && cargo build --release --locked -p bb-imager-cli)

mkdir -p "$BIN"
cp "$SRC/target/release/bb-imager-cli" "$BIN/bb-imager-cli"
"$BIN/bb-imager-cli" --version
