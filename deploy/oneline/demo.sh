#!/bin/bash
# What the one-line install looks like, without installing anything: the look of install.sh and look.py with made-up steps
# and pauses (the same functions the install calls; nothing is downloaded, nothing is written but a temporary file).
#   bash demo.sh [fail]             fail - the screen of a failure
# Run it in a terminal (60 columns or more, a UTF-8 locale). HUB_INSTALL_LOOK, NO_COLOR, TERM, COLORTERM, LANG work as in the install.
HERE=$(cd "$(dirname "$0")" && pwd)
case ${1:-} in ''|fail) ;; *) echo "usage: bash demo.sh [fail]" >&2; exit 64 ;; esac
SCRIPT=$HERE/install.sh
[ -f "$SCRIPT" ] || SCRIPT=$HERE/../../deploy/oneline/install.sh
PY=$(command -v python3)
[ -n "$PY" ] || { echo "demo.sh: python3 is needed for the last two steps" >&2; exit 1; }
# the look of install.sh: from the line "--- the look" to "--- end of the look", as it is in the file
eval "$(sed -n '/^# --- the look:/,/^# --- end of the look/p' "$SCRIPT")"
ui_init
if [ -z "$UI_ON" ]; then
  echo "demo.sh: nothing to show here: the install draws nothing (the lines as they were) when stderr is not a terminal, TERM is dumb," >&2
  echo "NO_COLOR is set, the locale is not UTF-8 or the window is under 60 columns wide" >&2
  exit 1
fi
PLATFORM=linux-x64
SHELL_SIZE=208654218
WORK=$(mktemp -d "${TMPDIR:-/tmp}/hub-look-demo.XXXXXX") || exit 1
trap 'rm -rf "$WORK"; [ -n "$UI_CPID" ] && kill "$UI_CPID" 2>/dev/null' EXIT
nap() { if [ -n "${HUB_DEMO_FAST:-}" ]; then sleep 0.01; else sleep "$1" 2>/dev/null || sleep 1; fi; }   # HUB_DEMO_FAST: the tests
say() { ui_say "$*"; }
refuse() { ui_fail "$*"; return 1; }

ui_header
ui_begin 1 Check "this computer"
nap 1.2
ui_done "$PLATFORM $UI_DOT 41 GB free"

ui_begin 2 Download "$((SHELL_SIZE / 1000000)) MB"
( n=0; while [ "$n" -lt 40 ]; do head -c 5000000 /dev/zero >> "$WORK/part"; n=$((n + 1)); nap 0.2; if [ "$n" -eq 22 ] && [ "${1:-}" = fail ]; then exit 1; fi; done; exit 0 ) &
UI_CPID=$!
ui_download_bar "$UI_CPID" "$WORK/part" 0
wait "$UI_CPID"
code=$?
UI_CPID=
if [ "$code" -ne 0 ]; then
  refuse "the download of the environment did not complete (110 of 208 MB): no answer from the delivery server (https://icons8.github.io/hub): check the connection (and HTTPS_PROXY) - the line continues where it stopped"
  exit 1
fi
ui_done "$((SHELL_SIZE / 1000000)) MB $UI_DOT verified"

ui_begin 3 Unpack "every file is checked"
nap 2.4
ui_done "every file checked"

# the last two steps are setup.py's: look.py, told by the same words the install hands over
ui_tail_clear; UI_IDLE=1
HUB_INSTALL_UI=$(ui_env 4) "$PY" -I -B "$HERE/look.py"
