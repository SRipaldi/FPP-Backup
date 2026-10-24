#!/bin/bash
# Build a pinned bb-imager-cli (beagleboard/bb-imager-rs, MIT) into work/bin.
#
# Upstream publishes no macOS CLI, and its CLI leaves macOS authopen off, which
# would mean running the flash under sudo. Building with
# bb-flasher/sd_macos_authopen gives the standard macOS admin dialog instead.
#
# Usage: tools/setup_bb_imager.sh [version]
set -euo pipefail

VERSION=${1:-1.0.20}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
SRC="$ROOT/work/bb-imager-src/$VERSION"
BIN="$ROOT/work/bin"

command -v cargo >/dev/null || { echo "cargo not found; install Rust from https://rustup.rs" >&2; exit 1; }

if [ ! -d "$SRC" ]; then
    git clone -q --depth 1 --branch "$VERSION" https://github.com/beagleboard/bb-imager-rs.git "$SRC"
fi

FEATURES=""
[ "$(uname)" = Darwin ] && FEATURES="--features bb-flasher/sd_macos_authopen"
(cd "$SRC" && cargo build --release -p bb-imager-cli $FEATURES)

mkdir -p "$BIN"
cp "$SRC/target/release/bb-imager-cli" "$BIN/bb-imager-cli"
"$BIN/bb-imager-cli" --version
