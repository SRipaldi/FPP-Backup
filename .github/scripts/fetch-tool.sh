#!/bin/bash
# Install one pinned scanner into $RUNNER_TEMP/bin, refusing it unless its SHA-256 matches the value
# below. This is the only way third-party code enters CI: no third-party GitHub Actions are used
# (an action's tag can be moved to malicious code, as happened with tj-actions and trivy-action).
#
# Each pin is an exact release, at least 14 days old when chosen, whose hash matched both GitHub's
# recorded asset digest and the publisher's own checksum file. To update one: pick a release at
# least 14 days old, take its linux amd64 asset's hash from BOTH sources (they must agree), change
# the version and hash together here, and review it like code.
#
# Usage: .github/scripts/fetch-tool.sh hadolint|osv-scanner|syft|scorecard
set -euo pipefail

tool=$1
bin="${RUNNER_TEMP:?}/bin"
case "$tool" in
    hadolint)     # hadolint/hadolint v2.15.1 (2026-07-31)
        url=https://github.com/hadolint/hadolint/releases/download/v2.15.1/hadolint-linux-x86_64
        sha=c7187db94eeeeca956519a6af171adc31453941a1e777961f6e680f697c8c507 ;;
    osv-scanner)  # google/osv-scanner v2.7.0 (2026-10-12)
        url=https://github.com/google/osv-scanner/releases/download/v2.7.0/osv-scanner_linux_amd64
        sha=160c6253e2922e9e0a6a9771c5c4c40a4b3c6090b34a3cdd5e80c96cc2e4218b ;;
    syft)         # anchore/syft v1.55.0 (2026-10-19)
        url=https://github.com/anchore/syft/releases/download/v1.55.0/syft_1.55.0_linux_amd64.tar.gz
        sha=bb65249542db5fedeeb4d3f9b018b75e7264dbfbbb10486f044581a92f90490d ;;
    scorecard)    # ossf/scorecard v5.6.0 (2026-10-12)
        url=https://github.com/ossf/scorecard/releases/download/v5.6.0/scorecard_5.6.0_linux_amd64.tar.gz
        sha=19bde07129450cb65a95c6e3b9f34e034341579e6f0e241546ec3ae6efb2dd9a ;;
    *) echo "unknown tool: $tool" >&2; exit 2 ;;
esac

mkdir -p "$bin"
dl="$RUNNER_TEMP/$tool.download"
curl -fsSL --proto '=https' --tlsv1.2 --retry 3 -o "$dl" "$url"
echo "$sha  $dl" | sha256sum --check --strict --quiet || { echo "::error::$tool: SHA-256 mismatch for $url; refusing it" >&2; exit 1; }
if [[ "$url" == *.tar.gz ]]; then
    tar -xzf "$dl" -C "$bin" "$tool"   # only the one binary, nothing else from the archive
else
    install -m 0755 "$dl" "$bin/$tool"
fi
rm -f "$dl"
echo "$bin" >> "$GITHUB_PATH"
echo "$tool: installed, SHA-256 verified ($sha)"
