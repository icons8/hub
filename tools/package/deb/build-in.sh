#!/bin/sh
# Inside Docker (debian:12-slim of the needed architecture): from /env (the shell of the release factory — runtime/,
# code-seed/, PACKAGED, launch.py, licenses/ — as tools/package/build.py made it) and /pkg (tools/package) build
# /out/agents-hub_<version>_<arch>.deb. Started by build.sh.
set -eu
ARCH=$1 VERSION=$2
R=/tmp/root
P=$R/opt/agents-hub
mkdir -p "$P/bin" "$P/lib" "$R/usr/bin" "$R/usr/lib/systemd/user" "$R/usr/share/applications" \
  "$R/usr/share/icons/hicolor/512x512/apps" "$R/DEBIAN"
cp -a /env/. "$P/"
# the shell's PACKAGED is the app's; the package adds where the Chrome for the whole machine will be (core/runtime shared)
"$P/runtime/python/bin/python3" -I - "$P/PACKAGED" <<'PY'
import json, sys
path = sys.argv[1]
with open(path) as f:
    mark = json.load(f)
mark["shared"] = "/opt/agents-hub/chrome"
with open(path, "w") as f:
    json.dump(mark, f, ensure_ascii=False)
PY
cp /pkg/first_run.py /pkg/deb/files/chrome_root.py /pkg/deb/files/open_page.py "$P/lib/"
cp /pkg/deb/files/agents-hub /pkg/deb/files/hub "$P/bin/"
ln -s ../runtime/python/bin/python3 "$P/bin/python3"
ln -s ../runtime/node/bin/node "$P/bin/node"
ln -s ../runtime/node/bin/npx "$P/bin/npx"
ln -s ../runtime/gh/bin/gh "$P/bin/gh"
ln -s ../runtime/ffmpeg/bin/ffmpeg "$P/bin/ffmpeg"
ln -s ../runtime/ffmpeg/bin/ffprobe "$P/bin/ffprobe"
ln -s ../runtime/mcp/node_modules/.bin/chrome-devtools-mcp "$P/bin/chrome-devtools-mcp"
ln -s /opt/agents-hub/bin/hub "$R/usr/bin/hub"
ln -s /opt/agents-hub/bin/agents-hub "$R/usr/bin/agents-hub"
cp /pkg/deb/files/agents-hub.service "$R/usr/lib/systemd/user/"
cp /pkg/deb/files/agents-hub.desktop "$R/usr/share/applications/"
cp "$P/code-seed/modules/web/static/icons/icon-512.png" "$R/usr/share/icons/hicolor/512x512/apps/agents-hub.png"
cp /pkg/deb/files/postinst /pkg/deb/files/postrm "$R/DEBIAN/"
chmod -R u+rwX,go+rX,go-w "$R"   # the shell comes from a temp folder with mode 700: any user must be able to read it
chmod 755 "$R/DEBIAN/postinst" "$R/DEBIAN/postrm"
"$P/runtime/python/bin/python3" -m compileall -q -j0 "$P/lib" "$P/launch.py"   # so that the user does not write __pycache__ into /opt
SIZE=$(du -sk "$R/opt" "$R/usr" | awk '{s += $1} END {print s}')
# Depends: besides git — the system libraries of Chrome for Testing, which is downloaded where it is needed
cat > "$R/DEBIAN/control" <<CTL
Package: agents-hub
Version: $VERSION
Architecture: $ARCH
Maintainer: Hub by Icons8 <noreply@agents-hub.invalid>
Installed-Size: $SIZE
Section: utils
Priority: optional
Depends: git, ca-certificates, fonts-liberation, libnss3, libnspr4, libgbm1, libdrm2, libxkbcommon0,
 libxcomposite1, libxdamage1, libxfixes3, libxrandr2, libx11-6, libxcb1, libxext6, libexpat1, libdbus-1-3,
 libpango-1.0-0, libcairo2, libglib2.0-0t64 | libglib2.0-0, libatk1.0-0t64 | libatk1.0-0,
 libatk-bridge2.0-0t64 | libatk-bridge2.0-0, libcups2t64 | libcups2, libasound2t64 | libasound2
Recommends: xdg-utils
Description: Hub by Icons8 — a fleet of Claude Code sessions through a web page and Telegram
 Everything it needs is inside: Python, Node, gh, ffmpeg (LGPL), chrome-devtools-mcp.
 The hub's code updates from its repository; the hub downloads Claude Code and Chrome itself.
CTL
dpkg-deb --build --root-owner-group -Zxz "$R" "/out/agents-hub_${VERSION}_$ARCH.deb" >/dev/null
