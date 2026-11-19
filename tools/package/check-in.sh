#!/bin/sh
# Inside Docker (started by check.sh): /tmp/app.tar.gz — a Linux build of tools/package/build.py.
set -eu
ok() { echo "OK   $*"; }
bad() { echo "FAIL $*"; FAILED=1; }
FAILED=0
export DEBIAN_FRONTEND=noninteractive
. /etc/os-release; echo "== $PRETTY_NAME $(uname -m)"
apt-get update -qq >/dev/null
# git is the system one on Linux (the shells of S7 depend on it); curl and ps — for the check itself
apt-get install -y -qq --no-install-recommends git ca-certificates curl procps >/dev/null
for p in python3 node ffmpeg gh; do command -v $p >/dev/null 2>&1 && bad "the image has $p — the check would prove nothing"; done
mkdir -p /opt/app && tar -xzf /tmp/app.tar.gz -C /opt/app --no-same-owner
A=$(ls -d /opt/app/agents-hub-*-linux-*) && ok "unpacked: $(du -sm "$A" | cut -f1) MB, $(find "$A" -type f | wc -l) files"
chmod -R a+rX,go-w /opt/app
useradd -m u
cat > /tmp/env.sh <<ENV
export PATH="$A/runtime/python/bin:$A/runtime/node/bin:$A/runtime/gh/bin:$A/runtime/ffmpeg/bin:\$PATH"
export HUB_NO_SYSTEM_AGENTS=1 HUB_NO_INVITE=1 HUB_NO_MODEL=1 HUB_LANG=ru
ENV
chmod a+r /tmp/env.sh
run() { su u -c ". /tmp/env.sh; $*"; }
CODE=$(run "python3 $A/launch.py $A/code-seed $A/PACKAGED \$HOME/.hub-core") && ok "the first start: the code is in $CODE" ||
  { bad "launch.py"; exit 1; }
# the code is the seed's layer (core/appcode.py, the shell's copy next to launch.py): its release, nothing edited
LAYER="import sys; sys.path.insert(0, '$A'); import appcode; m = appcode.load(sys.argv[1])"
[ "$(run "python3 -I -c \"$LAYER; print(m['version'])\" $CODE")" = "$($A/runtime/python/bin/python3 -I -c "$LAYER; print(m['version'])" "$A/code-seed")" ] &&
  ok "the code is the seed's layer: $(run "python3 -I -c \"$LAYER; print(appcode.name_of(m))\" $CODE")" || bad "the code is not the seed's layer"
[ "$(run "python3 -I -c \"$LAYER; print(len(appcode.diff(sys.argv[1], m, full=True)))\" $CODE")" = 0 ] &&
  ok "the layer matches its manifest (PACKAGED is not an edit)" || bad "the layer differs from its manifest"
touch /tmp/mark; sleep 1
mkdir -p /home/u/.config/hub-core && echo '{"hub": {"enabled": false}}' > /home/u/.config/hub-core/hub.json && chown -R u /home/u/.config
su u -c ". /tmp/env.sh; cd ~ && nohup python3 $CODE/bin/hub-up > /tmp/hub.log 2>&1 & echo \$! > /tmp/hub.pid"
i=0; until curl -sf --noproxy 127.0.0.1 -o /dev/null http://127.0.0.1:8787/ || [ $i -ge 90 ]; do sleep 1; i=$((i+1)); done
curl -sf --noproxy 127.0.0.1 -o /dev/null http://127.0.0.1:8787/ && ok "the page answers (8787) in ${i} s" || bad "the page did not answer"
run "python3 $CODE/bin/hub version" > /tmp/ver.txt 2>&1 && ok "hub version: $(tr '\n' ' ' < /tmp/ver.txt)" || { bad "hub version"; cat /tmp/ver.txt; }
run "python3 $CODE/bin/hub status" > /tmp/status.txt 2>&1 && ok "hub status: $(head -1 /tmp/status.txt)" || { bad "hub status"; cat /tmp/status.txt; }
sleep 5
grep -q "hubd exited" /tmp/hub.log && bad "the core crashes: $(grep -m1 'hubd exited' /tmp/hub.log)" || ok "the core runs, not restarted"
# hub-up starts its parts with its own sys.executable: they show which Python it runs on (/proc/<pid>/exe of another
# user is closed in Docker)
# the parts run from the layer's own folder (the pointer resolved: a switch of the pointer does not move running code)
ps -eo args | grep -q "^$A/runtime/python/bin/python3 .*$(readlink -f "$CODE")/bin/hub-update" && ok "hub-up and its parts run on the build's Python" ||
  bad "parts of the hub not on the build's Python: $(ps -eo args | grep -m2 'bin/hub-')"
new=$(find "$CODE/" -name '*.pyc' -newer /tmp/mark | head -3)
[ -z "$new" ] && ok "the first start took the factory's bytecode ($(find "$CODE/" -name '*.pyc' | wc -l) .pyc), compiled nothing itself" ||
  bad "the first start compiled again: $new"
run "ffmpeg -nostdin -loglevel error -f lavfi -i sine=frequency=440:duration=1 -ar 16000 -ac 1 /tmp/t.wav" &&
  [ "$(run "ffprobe -v error -show_entries stream=sample_rate -of csv=p=0 /tmp/t.wav")" = 16000 ] &&
  ok "ffmpeg and ffprobe of the build: sound → WAV 16 kHz (as for voice)" || bad "ffmpeg/ffprobe of the build"
run "gh --version" > /tmp/gh.txt 2>&1 && ok "$(head -1 /tmp/gh.txt)" || bad "gh"
run "node -e 'console.log(process.version)'" > /tmp/node.txt && ok "node $(cat /tmp/node.txt)" || bad "node"
run "node $A/runtime/mcp/node_modules/chrome-devtools-mcp/build/src/bin/chrome-devtools-mcp.js --help" > /tmp/mcp.txt 2>&1 &&
  ok "chrome-devtools-mcp: $(grep -c . /tmp/mcp.txt) lines of help" || { bad "chrome-devtools-mcp"; tail -5 /tmp/mcp.txt; }
run "python3 -c 'import ssl, sqlite3, zoneinfo, venv; print(ssl.OPENSSL_VERSION)'" > /tmp/py.txt && ok "python: $(cat /tmp/py.txt)" || bad "python"
[ ! -e "$A/runtime/chrome" ] && ok "no Chrome in the build (downloaded at the first start)" || bad "Chrome in the build"
su u -c ". /tmp/env.sh; python3 $CODE/bin/hub stop" >/dev/null 2>&1 || true
[ "$FAILED" = 0 ] || { echo "--- the hub's log"; tail -30 /tmp/hub.log; exit 1; }
