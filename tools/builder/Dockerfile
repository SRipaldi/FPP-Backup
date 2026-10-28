# The container tools/build_image.sh runs (as root, --privileged) to write a card image.
# build_image.sh reads the image from the FROM line below; nothing is built from this file.
# Pinned by digest so a re-pushed tag cannot change what runs. Dependabot opens a PR
# when Debian publishes a new trixie-slim; review it like any other change.
FROM debian:trixie-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad
