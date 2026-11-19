#!/bin/sh
# The hub on a Mac as a single launchd agent (owner decision 2026-09-30: run the hub on this Mac).
# State in ~/.hub-core, settings in ~/.config/hub-core/hub.json (created once, later edited in place),
# hub sessions run without the user's settings (setting_sources: project,local): the owner's personal settings do not touch them.
set -eu
REPO=$(cd "$(dirname "$0")/../.." && pwd)
STATE="$HOME/.hub-core"
CFG_DIR="$HOME/.config/hub-core"
LABEL=com.hub-core
# a second hub on the same Mac (owner decision 2026-10-03: a personal hub, not the work one): HUB_INSTANCE=personal — its own state folder,
# its own settings and bot, its own service and ports, the hub-personal command; the work hub is left alone. The folders,
# the ports (handed out once, past every hub of the machine) and the names come from core/instance.py, below
INSTANCE=${HUB_INSTANCE:-}
# The downloaded app (installer S5): HUB_APP=<the app> — its shell (deploy/mac/appshell.py) cloned this code into
# ~/.hub-core/code and keeps ~/.hub-core/app on the app's environment; Python is the app's, Claude Code comes from the
# hub's "Setup" page, the launchd agent runs the app's shell. One hub per Mac in this mode.
APP=${HUB_APP:-}
if [ -n "$APP" ]; then
  [ -z "$INSTANCE" ] || { echo "The app installs only the main hub, without HUB_INSTANCE" >&2; exit 1; }
  PY="$STATE/app/runtime/python/bin/python3"
  [ -x "$PY" ] || { echo "No $PY — open Agents Hub.app, it installs the hub itself" >&2; exit 1; }
fi
# Python 3.12 or newer; if missing — offer to install it (the first python3 found on a colleague's Mac was 3.9 from Xcode)
[ -n "$APP" ] || PY=$("$REPO/deploy/mac/find-python.sh") || {
  echo "The hub needs Python 3.12 or newer, and this Mac has none (/usr/bin/python3 of Xcode is 3.9)." >&2
  BREW=$(command -v brew || ls /opt/homebrew/bin/brew /usr/local/bin/brew 2>/dev/null | head -1 || true)
  if [ -n "$BREW" ] && [ -t 0 ]; then
    printf "Install Python 3.12 with Homebrew (brew install python@3.12)? [Y/n] "
    read -r answer
    case "$answer" in ""|y*|Y*) ;; *) exit 1 ;; esac
    "$BREW" install python@3.12
    PY=$("$REPO/deploy/mac/find-python.sh") || { echo "Python 3.12 is not found after the install" >&2; exit 1; }
  else
    echo "Install it: brew install python@3.12 (Homebrew: https://brew.sh) or with the installer from" >&2
    echo "https://www.python.org/downloads/macos/ — and run $0 again." >&2
    exit 1
  fi
}
echo "Python: $PY ($("$PY" -V 2>&1))"
if [ -n "$INSTANCE" ]; then
  ASSIGN=$(HUB_STATE_DIR= HUB_CONFIG_DIR= "$PY" "$REPO/core/instance.py" setup "$INSTANCE" --repo "$REPO" --os mac --browsers mac) || {
    echo "Hub $INSTANCE: not installed (the reason is above)" >&2; exit 1; }
  eval "$ASSIGN"
  STATE=$INST_STATE CFG_DIR=$INST_CONFIG LABEL=$INST_SERVICE
  export HUB_LABEL="$LABEL" HUB_CONFIG_DIR="$CFG_DIR" HUB_STATE_DIR="$STATE"
  export HUB_WEB_PORT="$INST_WEB" HUB_FILES_PORT="$INST_FILES"
fi
# Claude Code: if missing — the release's version into the hub's own folder, with consent (core/ownclaude.py: the signed
# manifest, sha256; the person's ~/.local/bin is not touched; the same path as deploy/install.sh and install.ps1)
if [ -z "$APP" ] && ! command -v claude >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/claude" ]; then
  echo "No Claude Code (claude) — without it the hub's sessions will not start."
  answer=n
  if [ -t 0 ]; then
    printf "Download it into the hub's folder from Anthropic (hub setup claude: the signed manifest, sha256)? [Y/n] "
    read -r answer
  fi
  case "$answer" in
    ""|y*|Y*) if HUB_STATE_DIR="$STATE" "$PY" "$REPO/bin/hub" setup claude; then
         echo "Sign in to Claude Code on the hub's page (Setup)."
       else
         echo "Claude Code is not installed — the cause is above; to try again: hub setup claude" >&2
       fi ;;
    *) echo "Download it later: hub setup claude — then sign in on the hub's page (Setup)." >&2 ;;
  esac
fi
mkdir -p "$STATE/hub-home" "$STATE/logs" "$STATE/bin" "$CFG_DIR"
ln -sf "$PY" "$STATE/bin/python3"   # hub commands (#!/usr/bin/env python3) in sessions — by the same Python, first in PATH
if [ -n "$INSTANCE" ]; then   # its own terminal command: hub-personal — the same hub, but with this hub's folder and settings
  "$PY" "$REPO/deploy/commands.py" --instance "$INSTANCE" "$REPO" "$PY" "$STATE" "$CFG_DIR" || true
else
  # hub and hub-telegram — in the terminal; from the app — with its git, node and gh first, as the hub runs them
  "$REPO/deploy/mac/link-commands.sh" "$REPO" "$PY" ${APP:+"$STATE/app/bin:$STATE/app/runtime/node/bin:$STATE/app/runtime/gh/bin"}
  echo "$STATE" > "$CFG_DIR/state-dir"
fi
ln -sf "$REPO/hub-home/CLAUDE.md" "$STATE/hub-home/CLAUDE.md"
ln -sf "$REPO/hub-home/ONBOARDING.md" "$STATE/hub-home/ONBOARDING.md"   # the introduction at the first message
if [ -n "$APP" ]; then   # settings of a new hub as the .deb writes them, with its sandbox project
  mkdir -p "$STATE/sandbox"
  "$PY" "$REPO/deploy/mac/app.py" config "$STATE" "$STATE/app" "$CFG_DIR/hub.json" >/dev/null
elif [ ! -f "$CFG_DIR/hub.json" ]; then
  # written next to its place and moved there whole (an instance's part first): see deploy/install.sh
  "$PY" - "$REPO" "$STATE" "$CFG_DIR/hub.json.part" "$INSTANCE" <<'PYEOF'
import json, os, sys
repo, state, path, instance = sys.argv[1:]
home = os.path.expanduser("~")
sets = os.path.join(os.path.dirname(repo), "hub-skills")
cfg = {
    "hub": {"enabled": True, "name": "hub-core", "cwd": os.path.join(state, "hub-home")},  # the name «hub» may be taken
    "sets": [{"name": "hub-skills", "path": sets}] if os.path.isdir(sets) else [],
    "base": ["core:hub-base"],
    "hub_tools": ["core:hub-coordinator"] + (["hub@hub-skills"] if os.path.isdir(sets) else []),
    "mcp": {}, "connectors": False, "timezone": "Europe/Berlin", "setting_sources": "project,local",
    "projects": {},   # a session without a project works in its own folder (core/workspace.py), not a shared one
    "browser": {"mcp": ["/opt/homebrew/bin/npx", "-y", "chrome-devtools-mcp@latest"],
                "env": {"PATH": "/opt/homebrew/bin:$PATH"}},
    "browsers": {"mac": {"kind": "local", "port": 9223, "headless": False, "risk": ["owner-data"],
                         "profile": os.path.join(home, "Library", "Application Support", "agent-chrome"),
                         "description": "The owner's Chrome with their sign-ins — Gmail and Google, GitHub, Microsoft "
                                        "365: their mail, dashboards, consoles. For tasks in their accounts; a window "
                                        "on the Mac"}},
}
if instance:  # a second hub: its ports, browser and update — core/instance.py settings, below; projects are repos
    # from HUB_PROJECTS_DIR (personal), without the working MCP and logins
    root = os.path.expanduser(os.environ.get("HUB_PROJECTS_DIR") or "")
    for name in sorted(os.listdir(root)) if root and os.path.isdir(root) else []:
        p = os.path.join(root, name)
        if os.path.isdir(os.path.join(p, ".git")):
            cfg["projects"][name] = {"path": p}
with open(path, "w") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
PYEOF
  [ -z "$INSTANCE" ] || "$PY" "$REPO/core/instance.py" settings "$STATE" "$CFG_DIR/hub.json.part"
  mv "$CFG_DIR/hub.json.part" "$CFG_DIR/hub.json"
fi
# The "Agents Hub.app" bundle (built and signed in advance, deploy/mac/app.py; the open flavor's "Hub by Icons8.app",
# signed ad hoc): "Login Items" show the hub's name, not "python3", and folder access goes to the bundle. If the signature
# does not match, launchd runs Python directly, as before.
# A checkout without the signed bundle (deploy/mac/Agents Hub.app, or embed/<name>/deploy/mac/ in a tree from git) is fine: the installer builds its own from launcher.c with an ad-hoc signature (clang from the Command Line Tools). No clang or a failed
# build: no bundle, launchd runs Python directly.
NAME=$("$PY" "$REPO/deploy/mac/app.py" name)   # this tree's: "Agents Hub.app", the open flavor's "Hub by Icons8.app"
BUNDLE="$STATE/$NAME"
rm -rf "$BUNDLE"
if [ -n "$APP" ]; then   # the downloaded app is the bundle itself
  PLIST=$("$PY" "$REPO/deploy/mac/app.py" app-plist "$REPO" "$STATE" "$APP")
else
  SHIPPED=$("$PY" "$REPO/deploy/mac/app.py" shipped)   # deploy/mac/, or the embed's in a tree from git
  if [ -n "$SHIPPED" ] && [ -z "$INSTANCE" ]; then
    # the signed bundle is com.hub-core: an instance builds its own com.hub-<name> (Login Items and folder access are
    # kept by the bundle's identifier — the main hub's would be shared)
    ditto "$SHIPPED" "$BUNDLE" 2>/dev/null || true
  elif ! command -v clang >/dev/null 2>&1; then
    echo "No clang (Command Line Tools: xcode-select --install): installing the hub without \"$NAME\", Login Items will show python3." >&2
  elif ! "$PY" "$REPO/deploy/mac/app.py" build - "$BUNDLE" ${INSTANCE:+--id "$LABEL"} >/dev/null; then
    echo "\"$NAME\" did not build: installing the hub without it, Login Items will show python3." >&2
    rm -rf "$BUNDLE"
  fi
  PLIST=$("$PY" "$REPO/deploy/mac/app.py" plist "$REPO" "$STATE" "$PY" "$BUNDLE")
fi
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
i=0   # bootout does not wait for the hub to finish writing its snapshot; bootstrap too early gives "Bootstrap failed: 5"
while launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 && [ $i -lt 90 ]; do sleep 1; i=$((i+1)); done
launchctl bootstrap "gui/$(id -u)" "$PLIST"
# the other flavor's copy of an earlier install, once the agent runs through this one (app.py drop-others)
"$PY" "$REPO/deploy/mac/app.py" drop-others "$STATE" "$PLIST" >/dev/null || true
sleep 3
launchctl print "gui/$(id -u)/$LABEL" | grep -E "state =|pid =" | head -2
# the last command is not a `[ ] && …` condition: for a regular hub a false condition made the installer exit 1
if [ -n "$INSTANCE" ]; then
  echo "Hub $INSTANCE: page http://127.0.0.1:$HUB_WEB_PORT/, command hub-$INSTANCE"
fi
