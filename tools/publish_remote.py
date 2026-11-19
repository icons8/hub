#!/usr/bin/env python3
"""What appeared in the remote's main not from publish.sh. A snapshot is laid over main and the tag is put on the snapshot,
but the remote's main can also be moved by a person with write access (a release by snapshot once silently rolled back
two PRs merged straight into main). Before sending, publish.sh and tools/release.py compare the remote's main with the
last publication:

  publish_remote.py <copy> <url>   0 — only snapshots after the publication, 6 — foreign commits (list on stderr),
                                   3 — the comparison did not work

A snapshot is a "Sync …" (or tools/oss_open.py's "Hub by Icons8, snapshot of …") commit by PUBLISH_NAME / PUBLISH_EMAIL. The base is the last snapshot on main: published.json
of this address (if it is not in main's history — the history was rewritten, refuse) or a later snapshot from another
copy; with no record — the last snapshot on main. Everything after the base that is not a snapshot is foreign. To
override explicitly — --overwrite-remote."""
import json
import os
import subprocess
import sys

# the subjects of snapshots: publish.sh's and tools/publish_pair.py's, and tools/oss_open.py's (the open repository's first)
SNAPSHOTS = ("Sync ", "Hub by Icons8, snapshot of ")
HINT = ("go through them — move what is needed into our main — and override explicitly: --overwrite-remote "
        "(the remote main becomes a snapshot, their commits stay only in its history)")


class Failed(Exception):
    """git failed — that is neither "no foreign commits" nor "there are foreign commits"."""


def git(repo, *args):
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=300)   # author names are utf-8 on any OS
    if r.returncode:
        raise Failed(f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
    return r.stdout


def recorded(url):
    """The Sync commit of the last publish to url per published.json, "" — there is no record for this address."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import release   # the same state folder as for the publish and the tag
    try:
        with open(os.path.join(release.state_dir(), "published.json"), encoding="utf-8") as f:
            pub = json.load(f)
    except (OSError, ValueError):
        return ""
    return str(pub.get("pub") or "") if isinstance(pub, dict) and pub.get("url") == url else ""


def foreign(repo, ref, pub, name, mail):
    """[(sha, author, subject)] of the commits of ref after the last snapshot, newest first; pub — the publish record or "".
    None — the pub record is not in the history of ref (the history was rewritten)."""
    def log(*spec):
        for line in git(repo, "log", "--topo-order", "--format=%H%x1f%an <%ae>%x1f%s", *spec).splitlines():
            sha, who, subject = line.split("\x1f", 2)
            yield sha, who, subject, who == f"{name} <{mail}>" and subject.startswith(SNAPSHOTS)
    every = list(log(ref))
    if pub and not any(sha == pub for sha, *_ in every):
        return None
    # the base is a snapshot newer than the record (from another copy, it already went on top) or the record itself; descendants go first
    later = None
    if pub:
        later = {pub, *git(repo, "rev-list", "--ancestry-path", f"{pub}..{ref}").split()}
    base = next((sha for sha, _, _, snap in every if snap and (later is None or sha in later)), pub)
    return [(sha, who, subject) for sha, who, subject, snap in log(f"{base}..{ref}" if base else ref) if not snap]


def problem(repo, ref, url, pub, name, mail):
    """The refusal text if the snapshot would revert foreign work in ref; "" — only snapshots after the last publish."""
    got = foreign(repo, ref, pub, name, mail)
    if got is None:
        return f"the last publication {pub[:7]} is not in the history of main on {url} — it was rewritten; {HINT}"
    if not got:
        return ""
    lines = "\n".join(f"  {sha[:10]} {who}: {subject}" for sha, who, subject in got[:40])
    more = f"\n  … {len(got) - 40} more" if len(got) > 40 else ""
    return (f"main on {url} has commits after the last publication that are not from publish.sh ({len(got)}):\n{lines}{more}\n"
            f"the snapshot would revert them; {HINT}")


def main(argv):
    if len(argv) != 2:
        sys.exit(__doc__)
    try:
        text = problem(argv[0], "HEAD", argv[1], recorded(argv[1]), os.environ.get("PUBLISH_NAME", ""),
                       os.environ.get("PUBLISH_EMAIL", ""))
    except (Failed, OSError, subprocess.TimeoutExpired) as e:
        print(e, file=sys.stderr)
        return 3
    if text:
        print(text, file=sys.stderr)
        return 6
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
