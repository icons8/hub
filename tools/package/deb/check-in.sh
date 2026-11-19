#!/bin/sh
# Inside Docker (started by check.sh): /tmp/agents-hub.deb — the package of deb/build.sh. The path of a newcomer on a
# machine without anything of ours: install, start as a regular user (the menu entry runs the same command), the page,
# the code as a clone of the seed, the tools, Chrome with its profile, removal.
set -eu
ok() { echo "OK   $*"; }
bad() { echo "FAIL $*"; FAILED=1; }
FAILED=0
export DEBIAN_FRONTEND=noninteractive
. /etc/os-release; echo "== $PRETTY_NAME $(uname -m)"
apt-get update -qq >/dev/null
# without recommends: this shows that the package itself does not pull in python3, ffmpeg, node, gh (a full Ubuntu has python3 anyway —
# systemd's recommends bring it, and the hub still runs on its own); no AppArmor profile of Chrome here: it has its own stage below
AGENTS_HUB_NO_CHROME=1 apt-get install -y -qq --no-install-recommends /tmp/agents-hub.deb >/tmp/apt.log 2>&1 || { tail -20 /tmp/apt.log; exit 1; }
ok "the package is installed: $(dpkg-query -W -f '${Version} ${Installed-Size}K' agents-hub)"
for p in python3 ffmpeg node gh; do
  if command -v $p >/dev/null 2>&1; then bad "the system has $p ($(command -v $p)) — the package must make do with its own"; fi
done
[ "$FAILED" = 0 ] && ok "the system has no python3, ffmpeg, node, gh — only in /opt/agents-hub"
A=/opt/agents-hub
grep -q '"format": "app"' $A/PACKAGED && grep -q '"shared": "/opt/agents-hub/chrome"' $A/PACKAGED &&
  ok "PACKAGED: the app, Chrome is the shared one in /opt/agents-hub/chrome" || bad "PACKAGED: $(cat $A/PACKAGED)"
[ -f $A/code-seed/MANIFEST.json ] && [ -f $A/launch.py ] && [ -f $A/appcode.py ] && [ -d $A/licenses ] &&
  [ -x $A/runtime/python/bin/python3 ] &&
  ok "the factory's shell is in place: runtime, code-seed (the layer), launch.py, appcode.py, licenses" || bad "the contents of the shell"
[ ! -e $A/app ] && [ ! -e $A/chrome ] && ok "no Chrome in the package, no former app folder" || bad "app/ or chrome/ in the package"
[ -f /usr/share/applications/agents-hub.desktop ] && [ -f /usr/share/icons/hicolor/512x512/apps/agents-hub.png ] &&
  ok "the menu entry and the icon" || bad "the menu entry or the icon"
find /opt/agents-hub -perm /022 ! -type l | head -3 | grep -q . && bad "/opt/agents-hub has something writable by all or the group: $(find /opt/agents-hub -perm /022 ! -type l | head -3)" ||
  ok "nobody but root writes in /opt/agents-hub"
apt-get install -y -qq --no-install-recommends curl procps >/dev/null 2>&1   # for the check: curl and ps
# Ubuntu has AppArmor (its tools and the abi files of the profiles); a minimal Debian does not, and nobody asks for a profile there
case "$ID" in ubuntu) apt-get install -y -qq --no-install-recommends apparmor >/dev/null 2>&1 || bad "apt install apparmor" ;; esac
useradd -m u
# the newcomer's command: no systemd --user here, so the hub starts apart from it
T0=$(date +%s)
su u -c 'cd ~ && agents-hub' > /tmp/start.txt 2>&1 && ok "agents-hub in $(( $(date +%s) - T0 )) s: $(tail -2 /tmp/start.txt | tr '\n' ' ')" || { bad "agents-hub"; cat /tmp/start.txt; }
curl -sf --noproxy 127.0.0.1 -o /dev/null http://127.0.0.1:8787/ && ok "the page answers (8787)" || bad "the page did not answer"
CODE=/home/u/.hub-core/code
# the code is the seed's layer behind the pointer (core/appcode.py, the shell's copy): its release, nothing edited
LAYER="import sys; sys.path.insert(0, '$A'); import appcode; m = appcode.load(sys.argv[1])"
[ "$(su u -c "$A/runtime/python/bin/python3 -I -c \"$LAYER; print(m['sum'])\" $CODE")" = "$($A/runtime/python/bin/python3 -I -c "$LAYER; print(m['sum'])" $A/code-seed)" ] &&
  ok "the code is the seed's layer $(cat $A/code-seed/MANIFEST.json | grep -o '"version": "[^"]*"')" || bad "the code is not the seed's layer"
[ "$(su u -c "$A/runtime/python/bin/python3 -I -c \"$LAYER; print(len(appcode.diff(sys.argv[1], m, full=True)))\" $CODE")" = 0 ] &&
  ok "the layer matches its manifest (PACKAGED is not an edit)" || bad "the layer differs from its manifest"
su u -c 'hub status' > /tmp/status.txt 2>&1 && ok "hub status: $(head -1 /tmp/status.txt)" || { bad "hub status"; cat /tmp/status.txt; }
sleep 5
grep -q "hubd exited" /home/u/.hub-core/logs/agents-hub.log && bad "the core crashes" || ok "the core runs, not restarted"
grep -q '"core:hub-app-developer"' /home/u/.hub-core/journal.jsonl && ok "the app's Hub Developer is set up (plugins and issues)" ||
  bad "no Hub Developer of the app in the journal"
# a Claude Code hook from the code: what every session calls — the event reaches the journal
echo '{"hook_event_name": "SessionStart", "session_id": "s-check", "source": "startup"}' |
  su u -c "HUB_SESSION=hub-dev HUB_STATE_DIR=\$HOME/.hub-core $A/runtime/python/bin/python3 $CODE/hooks/hub_hook.py"
i=0; until grep -q '"s-check"' /home/u/.hub-core/journal.jsonl || [ $i -ge 15 ]; do sleep 1; i=$((i+1)); done
grep -q '"s-check"' /home/u/.hub-core/journal.jsonl && ok "the session hook of the code writes to the journal" || bad "the session hook did not reach the journal"
su u -c 'hub version' > /tmp/ver.txt 2>&1 && grep -q "hub app: code v.*app .* (environment" /tmp/ver.txt &&
  ok "hub version: $(tr '\n' ' ' < /tmp/ver.txt)" || { bad "hub version"; cat /tmp/ver.txt; }
ps -eo args | grep -q "$A/runtime/python/bin/python3 .*$(readlink -f "$CODE")/bin/hub-update" && ok "hub-up and the update run on the package's Python" ||
  bad "parts of the hub not on the package's Python: $(ps -eo args | grep -m2 'bin/hub-')"
su u -c "$A/bin/ffmpeg -nostdin -loglevel error -f lavfi -i sine=frequency=440:duration=1 -ar 16000 -ac 1 /tmp/t.wav" &&
  [ -s /tmp/t.wav ] && ok "ffmpeg of the package: sound → WAV 16 kHz (as for voice)" || bad "ffmpeg of the package"
for t in gh node; do su u -c "$A/bin/$t --version" >/dev/null 2>&1 && ok "$t of the package starts" || bad "$t of the package"; done
su u -c "$A/bin/node $A/runtime/mcp/node_modules/chrome-devtools-mcp/build/src/bin/chrome-devtools-mcp.js --help" >/dev/null 2>&1 &&
  ok "chrome-devtools-mcp starts" || bad "chrome-devtools-mcp"
[ -d $A/code-seed/__pycache__ ] || find $A -newer /tmp/apt.log -name '*.pyc' | grep -q . && bad "the hub writes into /opt: $(find $A -newer /tmp/apt.log -name '*.pyc' | head -2)" || ok "the hub writes nothing into /opt"
# Chrome for the whole machine: the root helper, the profile for exactly its program (the sandbox itself is checked on a
# machine that has AppArmor: check-sandbox.sh)
su u -c "$A/bin/agents-hub chrome" >/tmp/chrome-user.txt 2>&1 && bad "agents-hub chrome worked without root" || ok "agents-hub chrome without root: $(head -1 /tmp/chrome-user.txt)"
if [ -z "${CHECK_NO_CHROME:-}" ]; then
  agents-hub chrome > /tmp/chrome.txt 2>&1 && ok "sudo agents-hub chrome: $(tail -1 /tmp/chrome.txt)" || { bad "agents-hub chrome"; tail -5 /tmp/chrome.txt; }
  CHROME=$(ls -d $A/chrome/*/*/chrome 2>/dev/null | head -1)
  [ -x "$CHROME" ] && ok "Chrome is in place: $CHROME" || bad "Chrome is not installed"
  [ -z "$(find $A/chrome -perm /022 ! -type l)" ] && ok "nobody but root writes under /opt/agents-hub/chrome" || bad "Chrome is open for writing"
  if [ -e /etc/apparmor.d/abi/4.0 ] || [ -e /etc/apparmor.d/abi/5.0 ]; then   # Ubuntu 23.10+: the profile is parsed by the system's own parser (not loaded: no kernel of ours here)
    apparmor_parser -QKT /etc/apparmor.d/agents-hub-chrome >/tmp/aa.txt 2>&1 && ok "apparmor_parser parses the profile" ||
      { bad "apparmor_parser did not take the profile"; cat /tmp/aa.txt; }
    grep -q "^profile agents-hub-chrome $CHROME flags=(unconfined)" /etc/apparmor.d/agents-hub-chrome && grep -q '^  userns,' /etc/apparmor.d/agents-hub-chrome &&
      ok "the AppArmor profile names the exact path of Chrome" || bad "the AppArmor profile: $(cat /etc/apparmor.d/agents-hub-chrome 2>&1 | head -8)"
  else   # no AppArmor, or one whose parser does not know `userns` (22.04): no restriction there, and no profile that would break the boot of its service
    [ ! -e /etc/apparmor.d/agents-hub-chrome ] && ok "no profile is written: AppArmor without userns (or no AppArmor)" || bad "a profile on a system without userns"
  fi
  su u -c "$CHROME --version" >/dev/null 2>&1 && ok "Chrome starts for the user" || bad "Chrome does not start (system libraries?)"
  su u -c "HUB_STATE_DIR=\$HOME/.hub-core $A/runtime/python/bin/python3 -I -c \"
import sys; sys.path.insert(0, '$CODE')
from core import runtime
print(runtime.chrome())\"" > /tmp/found.txt 2>&1
  [ "$(cat /tmp/found.txt)" = "$CHROME" ] && ok "the hub finds this Chrome itself" || bad "the hub did not find the shared Chrome: $(cat /tmp/found.txt)"
fi
su u -c 'hub stop' >/dev/null 2>&1 || true
sleep 3
apt-get remove -y -qq agents-hub >/dev/null 2>&1
[ ! -e /opt/agents-hub ] && [ ! -e /usr/bin/hub ] && [ ! -e /usr/lib/systemd/user/agents-hub.service ] &&
  [ ! -e /usr/share/applications/agents-hub.desktop ] && [ ! -e /etc/apparmor.d/agents-hub-chrome ] && ok "removed cleanly (Chrome and its profile too)" ||
  { bad "files are left after the removal"; find /opt/agents-hub /etc/apparmor.d/agents-hub-chrome 2>/dev/null | head; }
[ -d /home/u/.hub-core ] && ok "the user's data (~/.hub-core) stayed in place"
[ "$FAILED" = 0 ] || { echo "--- the hub's log"; tail -30 /home/u/.hub-core/logs/agents-hub.log; exit 1; }
