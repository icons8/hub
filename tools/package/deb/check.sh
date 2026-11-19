#!/bin/sh
# Check a .deb in a clean image: tools/package/deb/check.sh <file.deb> <image> [arch]
#   installs the package (apt fetches only system libraries), starts the hub as a regular user the way the menu entry
#   does, checks that: the page responds, the code is a clone of the seed on the release tag, the packaged tools run,
#   Chrome goes under /opt with its AppArmor profile for exactly that program and the hub finds it; removes the package.
#   CHECK_NO_CHROME=1 — without the Chrome stage (a 200 MB download).
#   The sandbox itself (a kernel with AppArmor): deb/check-sandbox.sh.
set -eu
DEB=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
IMAGE=$2
ARCH=${3:-arm64}
docker run --rm --platform "linux/$ARCH" -e CHECK_NO_CHROME="${CHECK_NO_CHROME:-}" -v "$DEB:/tmp/agents-hub.deb:ro" \
  -v "$(cd "$(dirname "$0")" && pwd)/check-in.sh:/check-in.sh:ro" "$IMAGE" sh /check-in.sh
