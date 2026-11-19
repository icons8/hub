#!/bin/sh
# The Chrome sandbox of the package on a machine that restricts it (Ubuntu 23.10+ with AppArmor): as root, on the machine
# itself or in a VM — not in Docker (a container sees no AppArmor of its own).
#   sudo tools/package/deb/check-sandbox.sh <agents-hub_X_amd64.deb>
# The package puts Chrome into /opt/agents-hub/chrome with a profile for exactly that program (postinst; here the same
# command by hand). Checks that Chrome of the package starts WITH its sandbox for an ordinary user; that it does not
# without the profile (so the restriction really is on and the profile is what helps); and that the same program copied
# to a place the user writes gets nothing — the profile names one root's path and no pattern.
set -eu
[ "$(id -u)" = 0 ] || { echo "needs root" >&2; exit 2; }
DEB=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
ok() { echo "OK   $*"; }
bad() { echo "FAIL $*"; FAILED=1; }
FAILED=0
[ "$(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns 2>/dev/null || echo 0)" = 1 ] ||
  { echo "this system has no userns restriction — nothing to check" >&2; exit 2; }
. /etc/os-release; echo "== $PRETTY_NAME $(uname -m)"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$DEB" >/tmp/sbx-apt.log 2>&1 || { tail -20 /tmp/sbx-apt.log; exit 1; }
CHROME=$(ls -d /opt/agents-hub/chrome/*/*/chrome 2>/dev/null | head -1)
[ -x "$CHROME" ] && ok "the package's Chrome: $CHROME" || { agents-hub chrome && CHROME=$(ls -d /opt/agents-hub/chrome/*/*/chrome | head -1); }
id sbxcheck >/dev/null 2>&1 || useradd -m sbxcheck
trap 'userdel -r sbxcheck >/dev/null 2>&1 || true; rm -rf /tmp/sbx-copy' EXIT
run() { su sbxcheck -c "cd ~ && timeout 60 $1 --headless=new --disable-gpu --user-data-dir=\$(mktemp -d) --dump-dom about:blank" 2>&1; }
profile() { aa-status 2>/dev/null | grep -q agents-hub-chrome; }
profile && ok "the agents-hub-chrome profile is loaded" || bad "the profile is not loaded (aa-status)"
run "$CHROME" | grep -q '<html' && ok "the package's Chrome starts with its sandbox" || bad "the package's Chrome did not start: $(run "$CHROME" | grep -m1 FATAL)"
apparmor_parser -R /etc/apparmor.d/agents-hub-chrome
run "$CHROME" | grep -q 'No usable sandbox' && ok "without the profile: No usable sandbox (the restriction holds, the profile is what helps)" ||
  bad "without the profile Chrome started anyway — no restriction?"
apparmor_parser -r /etc/apparmor.d/agents-hub-chrome
mkdir -p /tmp/sbx-copy && cp -a "$(dirname "$CHROME")" /tmp/sbx-copy/ && chmod -R a+rX /tmp/sbx-copy
run "/tmp/sbx-copy/$(basename "$(dirname "$CHROME")")/chrome" | grep -q 'No usable sandbox' &&
  ok "the same program elsewhere gets no sandbox: the profile names one path" || bad "a copy outside /opt got the sandbox"
[ "$FAILED" = 0 ] || exit 1
