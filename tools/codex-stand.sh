#!/usr/bin/env bash
# EXPERIMENT (exp/codex-main, ported from exp/codex-slice slice C, C6): a test stand of "the whole hub on Codex" and its
# pair on Claude with the same layout.
#
#   tools/codex-stand.sh <codex|claude> up        the hub from this copy of the code as a systemd --user service (no
#                                                 installer: it would rewrite the live hub command); its own folders,
#                                                 ports and command; limited to 2 CPUs and 6 GB (the server is shared)
#   tools/codex-stand.sh <codex|claude> down      stop the stand's service
#   tools/codex-stand.sh <codex|claude> status    the service, the ports, `hub status` of the stand
#   tools/codex-stand.sh <codex|claude> clones [<commit>]
#                                                 core clones for the pilot tasks (one per task, all on one commit;
#                                                 without origin — they merge into their own main, nothing reaches the live repo)
#   tools/codex-stand.sh tasks                    the pilot task texts (the same for both stands)
#
# The codex stand: state ~/.hub-codex, settings ~/.config/hub-codex, page :8781, "Files" :8783, command
# ~/.local/bin/hub-codex; the sessions' PATH has no claude (only codex, hub and the system), HOME is the stand's own
# empty folder: the personal ~/.codex and ~/.claude do not take part. Sign-in: `hub-codex codex login` (a device code —
# the owner's step). The claude stand: ~/.hub-pilot-claude, ~/.config/hub-pilot-claude, :8782/:8784, command
# hub-pilot-claude, the machine's claude sign-in. Hub parts: hubd, page, schedule, watcher, "Files": no Telegram,
# statistics, update or browser.
#
# Overrides (checking the stand): STAND_ROOT (instead of ~/.hub-<name> and ~/.config/hub-<name> — <root>/state and
# <root>/config, the command — <root>/hub-<name>), STAND_UNIT, STAND_WEB_PORT, STAND_FILES_PORT, HUB_CODEX_BIN (codex or
# its fake), STAND_MODEL / STAND_EFFORT / STAND_WATCH_MODEL (by default the owner's decision q621320: sessions
# gpt-6.1-sol, the watcher and the core's model gpt-6-luna).
set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)   # the copy the code (its HEAD) and the task clones come from
PY=$(command -v python3)

tasks() {
  cat <<'EOF'
Pilot tasks: edges left "before release" in the core's decisions; each one in its own clone (`clones`), with the same
text for both stands. Done by the executor's rule: the branch is merged into the clone's main branch, the module's tests
are green.

1. pin_claude_dir is silent on an unreadable hub.json. `core/catalog.pin_claude_dir`: hub.json cannot be read (half
   written, not UTF-8) — now the launch environment is taken silently. Needed: one line with the reason in the hub-up
   log; the behaviour (the launch environment) stays as it is. Test first.
2. A relative claude_config_dir: every `hub` command from the terminal prints the warning. Needed: the settings error is
   visible, but not on every command — print it once (for example, once per change of hub.json). Test first.
3. owner.noticed: the strict check of new fields refuses the whole notice — `hub notify --session S`, when S is removed,
   does not arrive at all. Needed: drop a bad push field and deliver the notice; `session` as a list or a dict is a
   refusal of the event, not a core failure (TypeError). Test first.
4. Pushes: `modules/web/push.py` `_result` clears `meta.closed` only after a silent push, though the banner already
   carried `closed` — an extra silent push. Needed: clear `closed` after the push that carried it. Test first.
EOF
}

[ "${1:-}" = tasks ] && { tasks; exit 0; }
HOST=${1:-}; CMD=${2:-}
case "$HOST" in
  codex) NAME=hub-codex; WEB=${STAND_WEB_PORT:-8781}; FILES=${STAND_FILES_PORT:-8783} ;;
  claude) NAME=hub-pilot-claude; WEB=${STAND_WEB_PORT:-8782}; FILES=${STAND_FILES_PORT:-8784} ;;
  *) sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2 ;;
esac
UNIT=${STAND_UNIT:-$NAME}
if [ -n "${STAND_ROOT:-}" ]; then
  STATE=$STAND_ROOT/state; CFG=$STAND_ROOT/config; CMDFILE=$STAND_ROOT/$NAME
else
  STATE=$HOME/.$NAME; CFG=$HOME/.config/$NAME; CMDFILE=$HOME/.local/bin/$NAME
fi
STAND_HOME=$STATE/stand-home   # HOME of the codex stand's sessions: neither the owner's ~/.codex nor ~/.claude
# the stand's code is its own clone of this copy's HEAD without origin: the stand's system hub-dev works in the hub's code
# folder, and in a working copy of the live repo its commits would land there. New code — down, delete <state>/code, up
CODE=$STATE/code

busy() { ss -ltn "( sport = :$1 )" 2>/dev/null | grep -q LISTEN; }

wrapper() {   # $1 — the file; $2 — "owner": the owner's command (without HUB_SESSION — otherwise the core sees a session in it)
  local unset=""
  [ "$2" = owner ] && unset="-u HUB_SESSION -u HUB_TREE "
  mkdir -p "$(dirname "$1")"
  cat > "$1" <<EOF
#!/bin/sh
exec env ${unset}HUB_STATE_DIR="$STATE" HUB_CONFIG_DIR="$CFG" HUB_WEB_PORT=$WEB HUB_FILES_PORT=$FILES "$PY" "$CODE/bin/hub" "\$@"
EOF
  chmod +x "$1"
}

up() {
  if systemctl --user is-active --quiet "$UNIT"; then
    echo "$UNIT is already running (status — $0 $HOST status)"; return 0
  fi
  for p in "$WEB" "$FILES"; do
    busy "$p" && { echo "port $p is busy (ss -ltnp) — set STAND_WEB_PORT/STAND_FILES_PORT" >&2; exit 1; }
  done
  mkdir -p "$STATE/bin" "$STATE/logs" "$STATE/hub-home" "$CFG"
  if [ ! -d "$CODE" ]; then
    git clone -q --no-hardlinks "$REPO" "$CODE"
    git -C "$CODE" checkout -q -B stand "$(git -C "$REPO" rev-parse HEAD)"
    git -C "$CODE" remote remove origin
  fi
  echo "stand code: $CODE at $(git -C "$CODE" rev-parse --short HEAD)"
  ln -sf "$PY" "$STATE/bin/python3"
  wrapper "$STATE/bin/hub" session
  wrapper "$CMDFILE" owner
  echo "$STATE" > "$CFG/state-dir"
  local path
  if [ "$HOST" = codex ]; then
    local codex=${HUB_CODEX_BIN:-$(command -v codex || echo "$HOME/.codex/packages/standalone/current/bin/codex")}
    [ -x "$codex" ] || { echo "no codex ($codex) — set HUB_CODEX_BIN" >&2; exit 1; }
    ln -sf "$codex" "$STATE/bin/codex"
    mkdir -p "$STAND_HOME"
    path=$STATE/bin:/usr/local/bin:/usr/bin:/bin
  else
    path=$STATE/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
  fi
  if [ ! -f "$CFG/hub.json" ]; then
    "$PY" - "$STATE" "$CFG/hub.json" "$HOST" "${STAND_MODEL:-gpt-6.1-sol}" "${STAND_EFFORT:-medium}" \
      "${STAND_WATCH_MODEL:-gpt-6-luna}" <<'PYEOF'
import json, os, sys
state, path, host, model, effort, watch = sys.argv[1:]
link = os.path.realpath("/etc/localtime")
cfg = {
    "hub": {"enabled": True, "name": "hub", "cwd": os.path.join(state, "hub-home")},
    "sets": [], "base": ["core:hub-base"], "hub_tools": ["core:hub-coordinator"], "mcp": {}, "connectors": False,
    "setting_sources": "project,local", "language": "ru", "projects": {},
    "timezone": link.split("/zoneinfo/", 1)[1] if "/zoneinfo/" in link else "UTC",
}
if host == "codex":
    cfg.update(host="codex", codex={"model": model, "effort": effort, "watch_model": watch})
with open(path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=1)
PYEOF
  fi
  local envs=(--setenv=PATH="$path" --setenv=HUB_STATE_DIR="$STATE" --setenv=HUB_CONFIG_DIR="$CFG"
              --setenv=HUB_WEB_PORT="$WEB" --setenv=HUB_FILES_PORT="$FILES" --setenv=LANG=C.UTF-8)
  if [ "$HOST" = codex ]; then   # without its own HOME git has no name — the stand signs the executors' commits
    envs+=(--setenv=HOME="$STAND_HOME" --setenv=GIT_AUTHOR_NAME=hub-codex --setenv=GIT_AUTHOR_EMAIL=hub-codex@stand
           --setenv=GIT_COMMITTER_NAME=hub-codex --setenv=GIT_COMMITTER_EMAIL=hub-codex@stand)
  fi
  systemd-run --user --quiet --unit "$UNIT" --collect --working-directory="$CODE" "${envs[@]}" \
    -p StandardOutput=append:"$STATE/logs/hub-up.log" -p StandardError=append:"$STATE/logs/hub-up.log" \
    -p TimeoutStopSec=75 -p CPUQuota=200% -p MemoryMax=6G -p MemorySwapMax=0 -p Nice=19 "$PY" "$CODE/bin/hub-up" --only hubd,web,scheduler,watch,files
  for _ in $(seq 1 40); do busy "$WEB" && break; sleep 0.5; done
  busy "$WEB" || { echo "the page :$WEB did not come up — log $STATE/logs/hub-up.log" >&2; exit 1; }
  echo "$UNIT: page http://127.0.0.1:$WEB, command $CMDFILE, log $STATE/logs/hub-up.log"
  [ "$HOST" = codex ] && echo "Codex login: $CMDFILE codex login"
  return 0
}

clones() {
  local commit=${1:-HEAD} root=$STATE/clones n
  commit=$(git -C "$REPO" rev-parse --verify "$commit^{commit}")
  for n in 1 2 3 4; do
    local dir=$root/t$n
    if [ -d "$dir" ]; then echo "$dir already exists"; continue; fi
    git clone -q --no-hardlinks "$REPO" "$dir"
    git -C "$dir" checkout -q -B main "$commit"
    git -C "$dir" remote remove origin   # they merge into their own main; a push to the live repo is impossible
    git -C "$dir" for-each-ref --format='%(refname:short)' refs/heads | { grep -vx main || true; } | xargs -r git -C "$dir" branch -q -D
    echo "$dir: main at ${commit:0:8}"
  done
}

case "$CMD" in
  up) up ;;
  down) systemctl --user stop "$UNIT" 2>/dev/null && echo "$UNIT stopped" || echo "$UNIT was not running" ;;
  status)
    systemctl --user --no-pager status "$UNIT" 2>/dev/null | head -5 || true
    for p in "$WEB" "$FILES"; do busy "$p" && echo "port $p — listening" || echo "port $p — not"; done
    [ -x "$CMDFILE" ] && "$CMDFILE" status 2>&1 | head -20 || true ;;
  clones) clones "${3:-HEAD}" ;;
  *) sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2 ;;
esac
