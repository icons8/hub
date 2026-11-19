#!/usr/bin/env python3
"""What tools/publish.sh leaves in the state folder of the publishing hub: published.json (what last went to main — the
"Updates" page compares against it, and tools/release.py puts the tag on this snapshot; tools/publish_pair.py adds
embed_url and embed_pub, the embed's snapshot of the same commit). It describes ONE address — the
hub's own. A publication to another one (a rehearsal, a second repo) does not rewrite it, otherwise the page and the
release tag would count from the wrong snapshot.

The hub's own address: `git config hub.release-url` of the repo, else the address from published.json, else nobody's yet
— the first publication takes it. To move to another address, set hub.release-url.

  publish_state.py own <url>                  empty if the state belongs to <url> (or to nobody yet), else its address
  publish_state.py record <sha> <pub> <url>   published.json: sha — the commit of this copy, pub — the snapshot's Sync commit"""
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def state_dir():
    """The state folder — the same as in tools/release.py (it reads the published.json that this file writes)."""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import release
    return release.state_dir()


def address(state, repo=ROOT):
    """The address the state belongs to, or "" if nobody has taken it yet (repo — the working repository whose
    hub.release-url is asked)."""
    got = subprocess.run(["git", "config", "hub.release-url"], cwd=repo, capture_output=True, text=True).stdout.strip()
    if got:
        return got
    try:
        with open(os.path.join(state, "published.json"), encoding="utf-8") as f:
            return str(json.load(f).get("url") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def main(argv):
    state = state_dir()
    if argv[:1] == ["own"] and len(argv) == 2:
        mine = address(state)
        print(mine if mine and mine != argv[1] else "")
    elif argv[:1] == ["record"] and len(argv) == 4:
        sha, pub, url = argv[1:]
        write(os.path.join(state, "published.json"),
              json.dumps({"sha": sha, "pub": pub, "at": int(time.time()), "url": url}) + "\n")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
