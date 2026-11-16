# The container tools/build_image.sh runs (as root, --privileged) to write a card image.
# build_image.sh builds it from this file on every run (--no-cache), so the packages are
# always the current ones from Alpine's signed repository, including security fixes.
#
# Alpine rather than Debian: the same tools in about half the packages, and no backlog of
# known vulnerabilities without a fix. Debian's base system carried ~100, and its updates
# fixed 3; the scans in .github/workflows/security-scans.yml track this image weekly.
#
# The base is pinned by digest so a re-pushed tag cannot change what runs. Dependabot
# opens a PR when Alpine publishes a new 3.24 image; review it like any other change.
FROM alpine:3.24@sha256:f59abe656b692d901e184be3445e084786602108599d2a51d229c0f5a48c71bf

# Package versions are not pinned: Alpine replaces old versions in its repository, so pins
# would break the build, and the newest version is the one with the security fixes. The
# repository is signed and verified by apk; the base image above is pinned.
# hadolint ignore=DL3018
RUN apk upgrade --no-cache \
 && apk add --no-cache bash util-linux sfdisk losetup rsync e2fsprogs dosfstools

# build_image.sh passes the script to run.
ENTRYPOINT ["bash", "-euo", "pipefail", "-c"]
