#!/bin/sh
# The hub on Linux — without root, like deploy/mac/install.sh and deploy/windows/install.ps1: Python 3.12+ and Claude Code (if
# missing — say how to install), state in ~/.hub-core, settings in ~/.config/hub-core/hub.json with a hub session (once, later
# edited in place), the hub and hub-telegram commands in ~/.local/bin, the hub — one systemd --user service with bin/hub-up.
# Running it again is harmless: hub.json is left alone, the hub is restarted on the new code.
# HUB_INSTANCE=<name> — another hub on this machine (core/instance.py, `hub instance new` runs this with it): its own
# ~/.hub-<name>, ~/.config/hub-<name>, service agents-hub-<name>.service, command hub-<name> and ports, handed out once
# past the ports of every hub of the machine; the main hub is left alone.
#   sh deploy/install.sh [-y]
set -eu
REPO=$(cd "$(dirname "$0")/.." && pwd)
STATE=${HUB_STATE_DIR:-$HOME/.hub-core}
CFG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/hub-core"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT=agents-hub.service
INSTANCE=${HUB_INSTANCE:-}
WEB=8787
YES=; [ "${1:-}" = "-y" ] && YES=1

ask() {   # a yes/no question: -y or not a terminal (pasted into Claude Code) — no, only a hint
  [ -n "$YES" ] && return 0
  [ -t 0 ] || return 1
  printf "%s [Y/n] " "$1"; read -r answer
  case "$answer" in ""|y*|Y*) ;; *) return 1 ;; esac
}

# Python 3.12 or newer: the first suitable one from PATH (on Ubuntu 22.04 python3 is 3.10, python3.12 may be next to it)
PY=
for c in python3.14 python3.13 python3.12 python3; do
  p=$(command -v "$c" 2>/dev/null) || continue
  if "$p" -c "import sys; sys.exit(sys.version_info < (3, 12))" 2>/dev/null; then PY=$p; break; fi
done
if [ -z "$PY" ]; then
  echo "The hub needs Python 3.12 or newer, and there is none here. Install it and run $0 again:" >&2
  echo "  Ubuntu 24.04+, Debian 13: sudo apt install python3" >&2
  echo "  Ubuntu 22.04: sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt install python3.12" >&2
  echo "  Fedora: sudo dnf install python3.12" >&2
  exit 1
fi
echo "Python: $PY ($("$PY" -V 2>&1))"
if [ -n "$INSTANCE" ]; then   # the folders, ports and names of the instance — from one place, written once
  ASSIGN=$(HUB_STATE_DIR= HUB_CONFIG_DIR= "$PY" "$REPO/core/instance.py" setup "$INSTANCE" --repo "$REPO" --os linux) || {
    echo "Hub $INSTANCE: not installed (the reason is above)" >&2; exit 1; }
  eval "$ASSIGN"
  STATE=$INST_STATE CFG_DIR=$INST_CONFIG UNIT=$INST_SERVICE WEB=$INST_WEB
fi

# mine UNIT_FILE… — of these service files, the ones of this hub: their HUB_STATE_DIR (without one — the default folder)
# is this state folder. A service of another hub is not rewritten, and the clean-up below does not take it for this hub's
# earlier layout (tests research 03.11: the second hub's service was rewritten or removed)
mine() {
  "$PY" - "$STATE" "$@" <<'PYEOF'
import os, sys
state, *paths = sys.argv[1:]
for path in paths:
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except (OSError, UnicodeDecodeError):
        continue
    env = dict(x.split("=", 2)[1:] for x in lines if x.startswith("Environment=") and x.count("=") >= 2)
    held = os.path.realpath(env.get("HUB_STATE_DIR") or os.path.expanduser("~/.hub-core"))
    if held == os.path.realpath(state):
        print(path)
PYEOF
}
if [ -f "$UNIT_DIR/$UNIT" ] && ! { grep -qE "^ExecStart=.*hub-up" "$UNIT_DIR/$UNIT" && [ -n "$(mine "$UNIT_DIR/$UNIT")" ]; }; then
  echo "$UNIT_DIR/$UNIT is a service of another hub or another program (not with HUB_STATE_DIR=$STATE): leaving it" \
    "alone and installing nothing. If it is an earlier install of this hub, delete the file and run $0 again" >&2
  exit 1
fi

# Claude Code: if missing — the release's version into the hub's own folder, with consent (core/ownclaude.py: the signed
# manifest, sha256; the person's ~/.local/bin is not touched; one path for every installer)
if ! command -v claude >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/claude" ]; then
  echo "No Claude Code (claude) — without it the hub's sessions will not start."
  if ask "Download it into the hub's folder from Anthropic (hub setup claude: the signed manifest, sha256)?"; then
    if HUB_STATE_DIR="$STATE" "$PY" "$REPO/bin/hub" setup claude; then   # into this hub's folder: the mark comes later
      echo "Sign in to Claude Code on the hub's page (Setup)."
    else
      echo "Claude Code is not installed — the cause is above; to try again: hub setup claude" >&2
    fi
  else
    echo "Download it later: hub setup claude — then sign in on the hub's page (Setup)." >&2
  fi
fi

# the claude directory goes into the service PATH: npm -g under nvm puts it (and node) outside ~/.local/bin and /usr/bin
CLAUDE_BIN=${HUB_CLAUDE:-$(command -v claude 2>/dev/null || echo "$HOME/.local/bin/claude")}
CLAUDE_DIR=$(dirname "${CLAUDE_BIN%% *}")

mkdir -p "$STATE/hub-home" "$STATE/logs" "$STATE/bin" "$CFG_DIR" "$HOME/.local/bin" "$UNIT_DIR"
ln -sf "$PY" "$STATE/bin/python3"   # hub commands (#!/usr/bin/env python3) in sessions — by the same Python, first in PATH
ln -sf "$REPO/hub-home/CLAUDE.md" "$STATE/hub-home/CLAUDE.md"
ln -sf "$REPO/hub-home/ONBOARDING.md" "$STATE/hub-home/ONBOARDING.md"   # the introduction at the first message
if [ -n "$INSTANCE" ]; then   # its own command hub-<name>; the mark of the main hub's folder and `hub` stay as they are
  "$PY" "$REPO/deploy/commands.py" --instance "$INSTANCE" "$REPO" "$PY" "$STATE" "$CFG_DIR" || true
else
  echo "$STATE" > "$CFG_DIR/state-dir"
  # in the terminal — by the same Python as the hub, not the first python3 from PATH; another program's hub stays
  "$PY" "$REPO/deploy/commands.py" "$REPO" "$PY"
fi
if [ ! -f "$CFG_DIR/hub.json" ]; then
  # written next to its place and moved there whole: an instance's part goes in first, a broken-off install leaves no
  # hub.json the rerun would keep
  "$PY" - "$STATE" "$CFG_DIR/hub.json.part" <<'PYEOF'
import json, os, sys
state, path = sys.argv[1:]
link = os.path.realpath("/etc/localtime")
zone = link.split("/zoneinfo/", 1)[1] if "/zoneinfo/" in link else "local"
cfg = {
    "hub": {"enabled": True, "name": "hub-core", "cwd": os.path.join(state, "hub-home")},  # the name «hub» may be taken
    "sets": [], "base": ["core:hub-base"], "hub_tools": ["core:hub-coordinator"],
    "mcp": {}, "connectors": False, "timezone": zone, "setting_sources": "project,local",
    "projects": {},   # a session without a project works in its own folder (core/workspace.py), not a shared one
}
with open(path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
PYEOF
  [ -z "$INSTANCE" ] || "$PY" "$REPO/core/instance.py" settings "$STATE" "$CFG_DIR/hub.json.part"
  mv "$CFG_DIR/hub.json.part" "$CFG_DIR/hub.json"
  echo "Settings: $CFG_DIR/hub.json (the hub session is on)"
fi
INST_ENV=
[ -z "$INSTANCE" ] || INST_ENV="Environment=HUB_CONFIG_DIR=$CFG_DIR
Environment=HUB_WEB_PORT=$INST_WEB
Environment=HUB_FILES_PORT=$INST_FILES
"

# one systemd --user service holds hub-up (the core and modules are its children). No After=default.target: together with
# WantedBy=default.target it is a cycle, and systemd drops the service at boot (seen on a VM). TimeoutStopSec — longer than
# hub-up waits for the core snapshot (platforms/posix.py, stop_grace). PATH: the code's bin before ~/.local/bin, as on the
# Mac — sessions reach the hub's own `hub` even where ~/.local/bin/hub is another program's (commands.py left it). OOMPolicy=continue: when the OS kernel kills a process for lack of memory —
# only that process dies (hub-up restarts a failed part, the core a session); by default systemd would stop the whole hub
cat > "$UNIT_DIR/$UNIT" <<EOF
[Unit]
Description=Hub by Icons8${INSTANCE:+ ($INSTANCE)}
After=network-online.target

[Service]
ExecStart=$PY $REPO/bin/hub-up
WorkingDirectory=$REPO
Environment=PATH=$STATE/bin:$REPO/bin:$HOME/.local/bin:$CLAUDE_DIR:/usr/local/bin:/usr/bin:/bin
Environment=HUB_STATE_DIR=$STATE
${INST_ENV}Restart=on-failure
RestartSec=5
OOMPolicy=continue
TimeoutStopSec=75
StandardOutput=append:$STATE/logs/hub-up.log
StandardError=append:$STATE/logs/hub-up.log

[Install]
WantedBy=default.target
EOF
if systemctl --user daemon-reload 2>/dev/null; then
  # the earlier layout (a service per part: hubd, hub-web…) and this repo's own part services (a statistics-only service) —
  # remove them so there are not two hubs and a second send. The core of the earlier layout calls this script itself
  # (core/daemon). Only this hub's: a second hub on the same code (its own HUB_STATE_DIR) keeps its services
  for path in "$UNIT_DIR"/*.service; do
    old=$(basename "$path")
    [ -f "$path" ] && [ "$old" != "$UNIT" ] || continue
    if grep -qE "^ExecStart=(.*[ =])?$REPO/(bin|modules)/" "$path" && [ -n "$(mine "$path")" ]; then
      systemctl --user disable --now "$old" 2>/dev/null || true
      rm -f "$path"
    fi
  done
  systemctl --user enable "$UNIT" >/dev/null
  systemctl --user restart "$UNIT"
  echo "The hub runs as a service: systemctl --user status $UNIT, the log is $STATE/logs/hub-up.log"
  if [ "$(loginctl show-user "${USER:-$(id -un)}" -p Linger 2>/dev/null)" != "Linger=yes" ]; then
    echo "To keep the hub running without a login session (a server, ssh): sudo loginctl enable-linger ${USER:-$(id -un)}"
  fi
else
  echo "No systemd --user here (a container, WSL without systemd) — run the hub in a terminal: $PY $REPO/bin/hub-up"
  exit 0
fi

if [ -z "${HUB_INSTALL_NO_WAIT:-}" ]; then
  i=0
  until "$PY" -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:$WEB/', timeout=2)" 2>/dev/null; do
    i=$((i+1)); [ $i -ge 60 ] && { echo "The page did not answer in a minute — see $STATE/logs/hub-up.log" >&2; exit 1; }
    sleep 1
  done
fi
echo "Page: http://127.0.0.1:$WEB/ · the ${INST_COMMAND:-hub} command is in ~/.local/bin (if it is not in PATH, add that folder)"
