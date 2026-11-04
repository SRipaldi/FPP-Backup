# The container tools/build_image.sh runs (as root, --privileged) to write a card image.
# build_image.sh reads the image from the FROM line below; nothing is built from this file.
# Pinned by digest so a re-pushed tag cannot change what runs. Dependabot opens a PR
# when Debian publishes a new trixie-slim; review it like any other change.
FROM debian:trixie-slim@sha256:f6b08b09a05725f9431502d3b5714f5c4576e82c1a84fa1debe0c24a7e0c3ec4
