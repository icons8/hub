#!/bin/sh
# Python 3.12 or newer for the hub: prints the path; none found — exit code 1. The first python3 found will not do: on
# a Mac it is often /usr/bin/python3 from Xcode, 3.9 (the hub did not start for a colleague on 2026-10-02). PATH first,
# then the usual install places (Homebrew, python.org); newer versions first. HUB_PY_DIRS — own places instead of
# the usual ones (tests).
ok() { [ -x "$1" ] && "$1" -c 'import sys; sys.exit(sys.version_info < (3, 12))' >/dev/null 2>&1; }
NAMES=""
v=20
while [ $v -ge 12 ]; do NAMES="$NAMES python3.$v"; v=$((v - 1)); done
NAMES="$NAMES python3"
for name in $NAMES; do
  p=$(command -v "$name" 2>/dev/null) && ok "$p" && { echo "$p"; exit 0; }
done
DIRS=${HUB_PY_DIRS:-"/opt/homebrew/bin /usr/local/bin $(ls -d /Library/Frameworks/Python.framework/Versions/3.*/bin 2>/dev/null | sort -rV | tr '\n' ' ')"}
for dir in $DIRS; do
  for name in $NAMES; do
    ok "$dir/$name" && { echo "$dir/$name"; exit 0; }
  done
done
exit 1
