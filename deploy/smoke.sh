#!/bin/sh
# A clean install checked end to end: the installer of this tree (deploy/install.sh, on macOS deploy/mac/install.sh)
# into an empty home folder, the hub up, its page answering and `hub status` reading its journal, then
# `hub uninstall --purge --yes` and a check that nothing of the hub is left. One line per phase, "phase <name>
# ok|fail <s>s", then SMOKE OK or SMOKE FAIL; exit 0 only when every phase passed.
#   sh deploy/smoke.sh [--here] [--line <address of install.sh>]
# --line: the one-line install instead of this tree's installer — `curl -fsSL <address> | bash -s -- --yes --no-open`
# (wget when there is no curl) from a delivery server or its stand, the hub started by the line itself (its service, or
# in the background where there is no service manager); "clean" then also wants no environments folder, no marked
# PATH line in the shell's profiles and no lingering the install turned on. SMOKE_SHOW=1 (not HUB_*: those are cleared
# below) prints each phase's output when it passed too (a record of a live stand); SMOKE_PORTS="<page> <Files>" gives
# --here other ports than 8787 and 8788 (a machine where another account's hub has them).
# By default it works in a fresh temporary HOME with the service manager cut off — the installer's own path for a
# machine without systemd --user: the hub is started by hand, as the installer says — and on free ports, so a hub
# already running on this machine is not touched. --here: this user's real home and real service manager (systemd
# --user, launchd) — only on a throwaway machine (a container, a VM, a CI runner): it refuses when any trace of a hub
# is there. macOS needs --here: launchd cannot be cut off.
# Claude Code is a stand-in (it prints a version and otherwise waits on its input): no sign-in, no network, no model.
set -u
REPO=$(cd "$(dirname "$0")/.." && pwd)
HERE=
LINE=
while [ $# -gt 0 ]; do
  case "$1" in
    --here) HERE=1 ;;
    --line) [ $# -ge 2 ] && [ -n "$2" ] || { echo "usage: sh deploy/smoke.sh [--here] [--line <address>]" >&2; exit 2; }
            LINE=$2; shift ;;
    *) echo "usage: sh deploy/smoke.sh [--here] [--line <address>]" >&2; exit 2 ;;
  esac
  shift
done
OS=$(uname -s)
WORK=$(mktemp -d "${TMPDIR:-/tmp}/hub-smoke.XXXXXX") || exit 2
STUBS="$WORK/stubs"
FAILED=
UP_PID=
LINGERED=

# nothing of the hub this check may run inside of: its sessions carry HUB_* (the state folder, the session, the tree).
# The HUB_NO_* opt-outs (no model, no statistics, no catalog download) stay: they only take things away
for name in $(env | sed -n 's/^\(HUB_[A-Za-z0-9_]*\)=.*/\1/p'); do
  case "$name" in HUB_NO_*) ;; *) unset "$name" ;; esac
done
unset CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL
mkdir -p "$STUBS"
if [ -n "$HERE" ]; then
  PORT=8787
  if [ -n "${SMOKE_PORTS:-}" ]; then   # "<page> <Files>": where 8787 is another account's hub on this machine
    PORT=${SMOKE_PORTS%% *}
    export HUB_WEB_PORT="$PORT" HUB_FILES_PORT="${SMOKE_PORTS#* }"
  fi
else
  [ "$OS" = Darwin ] && { echo "macOS: launchd cannot be cut off — run it on a throwaway account with --here" >&2; exit 2; }
  HOME="$WORK/home"
  mkdir -p "$HOME"
  unset XDG_CONFIG_HOME XDG_DATA_HOME XDG_STATE_HOME XDG_CACHE_HOME DBUS_SESSION_BUS_ADDRESS XDG_RUNTIME_DIR
  # no service manager: the installer writes its unit and says to start hub-up by hand; `hub uninstall` meets the same
  for cmd in systemctl loginctl; do
    printf '#!/bin/sh\necho "%s: no service manager in this check" >&2\nexit 1\n' "$cmd" > "$STUBS/$cmd"
    chmod +x "$STUBS/$cmd"
  done
  # free ports for the page and "Files": the machine's own hub keeps 8787 and 8788
  command -v python3 >/dev/null 2>&1 || { echo "no python3 to find free ports with: on a clean throwaway machine run it with --here" >&2; exit 2; }
  PORTS=$(python3 -c 'import socket
s = [socket.socket() for _ in range(2)]
for x in s: x.bind(("127.0.0.1", 0))
print(" ".join(str(x.getsockname()[1]) for x in s))') || exit 2
  PORT=${PORTS% *}
  export HUB_WEB_PORT="$PORT" HUB_FILES_PORT="${PORTS#* }"
fi
export HOME
STATE="$HOME/.hub-core"
CFG="${XDG_CONFIG_HOME:-$HOME/.config}/hub-core"
BIN="$HOME/.local/bin"
UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/agents-hub.service"
PLIST="$HOME/Library/LaunchAgents/com.hub-core.plist"
DATA="$HOME/.local/share/agents-hub"   # the one-line install's environments, downloads and log
[ "$OS" = Darwin ] || DATA="${XDG_DATA_HOME:-$HOME/.local/share}/agents-hub"
MARKED="hub-core: ~/.local/bin"         # the line it writes into the shell's profiles (core/uninstall.PATH_MARK)

if [ -n "$HERE" ]; then
  for path in "$STATE" "$CFG" "$BIN/hub" "$BIN/hub-telegram" "$UNIT" "$PLIST" "$DATA"; do
    if [ -e "$path" ] || [ -L "$path" ]; then
      echo "not a clean machine: $path exists — --here removes the hub at the end, run it only on a throwaway machine" >&2
      rm -rf "$WORK"
      exit 2
    fi
  done
fi

printf '#!/bin/sh\ncase "${1:-}" in\n  --version) echo "9.9.9 (Claude Code)" ;;\n  auth) echo '"'"'{"loggedIn": true}'"'"' ;;\n  *) exec cat > /dev/null ;;\nesac\n' \
  > "$STUBS/claude"
chmod +x "$STUBS/claude"
export PATH="$STUBS:$BIN:$PATH" HUB_INSTALL_NO_WAIT=1

now() { date +%s; }
py() {   # the hub's own Python once it is there: a clean machine of the line may have none (Debian 12)
  if [ -x "$STATE/bin/python3" ]; then "$STATE/bin/python3" "$@"; else python3 "$@"; fi
}
phase() {   # phase NAME COMMAND...: runs it, prints the verdict, and the tail of its output when it failed
  name=$1; shift
  start=$(now)
  if "$@" > "$WORK/$name.log" 2>&1; then
    echo "phase $name ok $(( $(now) - start ))s"
    if [ -n "${SMOKE_SHOW:-}" ]; then sed 's/^/  | /' "$WORK/$name.log" | tail -40; fi   # a live stand's record
  else
    echo "phase $name fail $(( $(now) - start ))s"
    sed 's/^/  | /' "$WORK/$name.log" | tail -40
    FAILED=1
    return 1
  fi
}

answers() {   # the page gives its HTML: 0 yes, 1 no, 2 nothing here to ask with (curl, wget, a Python — after the
  # uninstall the hub's own Python is gone, and a clean machine of the line has curl or wget, maybe no python3)
  if command -v curl >/dev/null 2>&1; then
    curl -fsS -m 3 --noproxy '*' "http://127.0.0.1:$PORT/" 2>/dev/null | head -c 65536 | grep -qi '<html'
    return
  fi
  if command -v wget >/dev/null 2>&1; then
    env -u http_proxy -u HTTP_PROXY wget -qO- -T 3 -t 1 "http://127.0.0.1:$PORT/" 2>/dev/null | head -c 65536 | grep -qi '<html'
    return
  fi
  [ -x "$STATE/bin/python3" ] || command -v python3 >/dev/null 2>&1 || return 2
  py - "$PORT" <<'EOF'
import sys, urllib.request
with urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/", timeout=3) as r:
    sys.exit(0 if r.status == 200 and b"<html" in r.read(65536).lower() else 1)
EOF
}

install() {
  if [ -n "$LINE" ]; then   # what a person pastes; --yes: nobody answers a question here
    # a download that fails gives bash an empty script — exit 0: it gets a line that fails instead
    failed='echo "the install script did not download" >&2; exit 97'
    if command -v curl >/dev/null 2>&1; then
      { curl -fsSL "$LINE" || echo "$failed"; } | bash -s -- --yes --no-open
    else
      { wget -qO- "$LINE" || echo "$failed"; } | bash -s -- --yes --no-open
    fi
    return
  fi
  case "$OS" in
    Darwin) sh "$REPO/deploy/mac/install.sh" < /dev/null ;;
    *) sh "$REPO/deploy/install.sh" < /dev/null ;;
  esac
}

managed() {   # the installer's service runs the hub: launchd, or a systemd --user unit it enabled
  [ "$OS" = Darwin ] && return 0
  [ -n "$LINE" ] && return 0   # the line starts the hub itself: its service, or in the background without one
  [ -n "$HERE" ] && [ -f "$UNIT" ] && systemctl --user is-enabled agents-hub.service >/dev/null 2>&1
}

start() {   # without a service manager the installer says "run hub-up in a terminal": that is what a user does
  managed && return 0
  nohup "$STATE/bin/python3" "$REPO/bin/hub-up" > "$WORK/hub-up.out" 2>&1 &
  UP_PID=$!
  echo "hub-up $UP_PID"
}

page() {   # within a minute, or hub-up died and its output says why
  i=0
  until answers 2>/dev/null; do
    [ $? -eq 2 ] && { echo "nothing here to ask the page with (curl, wget or python3)"; return 1; }
    if [ -n "$UP_PID" ] && ! kill -0 "$UP_PID" 2>/dev/null; then tail -20 "$WORK/hub-up.out"; return 1; fi
    i=$((i+1)); [ $i -ge 60 ] && { [ -n "$UP_PID" ] && tail -20 "$WORK/hub-up.out"; return 1; }
    sleep 1
  done
}

status() {   # the core has read hub.json: `hub status` shows the hub session from the journal
  i=0
  until "$BIN/hub" status --json > "$WORK/status.json" 2>&1 && py - "$WORK/status.json" <<'EOF'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    sys.exit(0 if "hub-core" in json.dumps(json.load(f)) else 1)
EOF
  do
    i=$((i+1)); [ $i -ge 60 ] && { cat "$WORK/status.json"; return 1; }
    sleep 1
  done
}

uninstall() {
  [ -f "$DATA/linger.json" ] && LINGERED=1   # the install turned linger on: uninstall turns it off
  "$BIN/hub" uninstall --purge --yes
}

clean() {
  for path in "$STATE" "$CFG" "$UNIT" "$PLIST" "$BIN/hub" "$BIN/hub-telegram" "$DATA"; do
    if [ -e "$path" ] || [ -L "$path" ]; then echo "left: $path"; return 1; fi
  done
  for profile in .zshrc .bash_profile .bash_login .profile .bashrc; do   # not "name": phase() keeps its own there
    if grep -qF "$MARKED" "$HOME/$profile" 2>/dev/null; then echo "left: the PATH line in ~/$profile"; return 1; fi
  done
  if [ -n "$HERE" ] && [ -n "$LINE" ] && [ "$OS" != Darwin ] && [ -n "$LINGERED" ] && \
      [ -e "/var/lib/systemd/linger/$(id -un)" ]; then
    echo "left: linger of $(id -un), which the install turned on"; return 1
  fi
  if [ -n "$UP_PID" ] && kill -0 "$UP_PID" 2>/dev/null; then echo "hub-up still runs: $UP_PID"; return 1; fi
  answers 2>/dev/null
  case $? in
    0) echo "the page still answers on $PORT"; return 1 ;;
    2) echo "nothing here to ask the page with (curl, wget or python3)"; return 1 ;;
  esac
}

finish() {
  trap - EXIT INT TERM
  if [ -x "$BIN/hub" ]; then phase uninstall uninstall; fi
  phase clean clean
  if [ -n "$UP_PID" ] && kill -0 "$UP_PID" 2>/dev/null; then   # only our own hub-up, by its pid
    kill "$UP_PID" 2>/dev/null; sleep 3; kill -9 "$UP_PID" 2>/dev/null
  fi
  rm -rf "$WORK"
  if [ -n "$FAILED" ]; then echo "SMOKE FAIL"; exit 1; fi
  echo "SMOKE OK"; exit 0
}
trap finish EXIT INT TERM

WHERE="a temporary home"; [ -n "$HERE" ] && WHERE="this home"
FROM="tree $REPO"; [ -n "$LINE" ] && FROM="the line ${LINE%%/i/*}/i/…"   # the build key is not printed
echo "smoke: $OS, $WHERE $HOME, port $PORT, $FROM"
phase install install || exit
phase start start || exit
phase page page || exit
if [ -z "$UP_PID" ] && [ -f "$STATE/hub-up.json" ]; then   # the line's hub-up in the background: checked gone at the end
  UP_PID=$(py -c 'import json,sys; print(json.load(open(sys.argv[1]))["pid"])' "$STATE/hub-up.json" 2>/dev/null)
fi
phase status status
