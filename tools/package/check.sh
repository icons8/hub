#!/bin/sh
# Check a Linux build of the release factory in a clean image: tools/package/check.sh <agents-hub-…-linux-*.tar.gz> [image]
#   unpacks it as the shell would, runs launch.py (the seed becomes the code in the state folder), starts hub-up of a
#   regular user on the environment of the build (no Claude Code, no sessions), checks the page, hub version, the tools
#   and that the first start used the factory's bytecode. Docker of the build's architecture.
set -eu
ARCHIVE=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
IMAGE=${2:-debian:12-slim}
case "$ARCHIVE" in *-linux-x64.tar.gz) ARCH=amd64 ;; *-linux-arm64.tar.gz) ARCH=arm64 ;; *) echo "not a Linux build: $1" >&2; exit 2 ;; esac
docker run --rm --platform "linux/$ARCH" -v "$ARCHIVE:/tmp/app.tar.gz:ro" \
  -v "$(cd "$(dirname "$0")" && pwd)/check-in.sh:/check-in.sh:ro" "$IMAGE" sh /check-in.sh
