# The container tools/build_image.sh runs (as root, --privileged) to write a card image.
# build_image.sh reads the image from the FROM line below; nothing is built from this file.
# Pinned by digest so a re-pushed tag cannot change what runs. Dependabot opens a PR
# when Debian publishes a new trixie-slim; review it like any other change.
FROM debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2
