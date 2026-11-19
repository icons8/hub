#!/bin/sh
# The hub .deb (installer spec S7), packed in Docker of the needed architecture:
#   tools/package/deb/build.sh <amd64|arm64> --repo <url|path> --tag vX.Y.Z [--origin URL] [--issues <account>/<repo>]
#   → dist/agents-hub_<X.Y.Z>_<arch>.deb
# The package carries the shell of the release factory as it is (tools/package/build.py: runtime/, code-seed/ — the shared
# repo on the tag with its bytecode, PACKAGED, launch.py, licenses/); the code runs from a clone of the seed in the user's
# folder and updates through the hub's own channel (modules/update, the app mode). Chrome is not in the package: it is
# downloaded where it is needed (modules/setup), and by root into /opt/agents-hub/chrome where Ubuntu asks for a profile.
# Pinned parts: tools/package/versions.json.
set -eu
ARCH=${1:?amd64 or arm64}
shift
REPO=$(cd "$(dirname "$0")/../../.." && pwd)
CACHE=${HUB_PACKAGE_CACHE:-$HOME/.cache/hub-package}
OUT="$REPO/dist"
case "$ARCH" in amd64) PLATFORM=linux-x64 ;; arm64) PLATFORM=linux-arm64 ;; *) echo "amd64 or arm64" >&2; exit 2 ;; esac
TAG= prev=
for a in "$@"; do
  if [ "$prev" = --tag ]; then TAG=$a; fi
  prev=$a
done
case "$TAG" in v[0-9]*.[0-9]*.[0-9]*) ;; *) echo "needs --tag vX.Y.Z (and --repo: where to take the code on the tag)" >&2; exit 2 ;; esac
VERSION=${TAG#v}
mkdir -p "$CACHE" "$OUT"
SRC=$(mktemp -d)
trap 'rm -rf "$SRC"' EXIT
python3 "$REPO/tools/package/build.py" "$PLATFORM" --out "$SRC/env" --cache "$CACHE" "$@" >&2
docker run --rm --platform "linux/$ARCH" -v "$SRC/env/agents-hub-$TAG-$PLATFORM:/env:ro" \
  -v "$REPO/tools/package:/pkg:ro" -v "$OUT:/out" debian:12-slim sh /pkg/deb/build-in.sh "$ARCH" "$VERSION"
ls -la "$OUT/agents-hub_${VERSION}_$ARCH.deb"
