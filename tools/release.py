#!/usr/bin/env python3
"""A release: an annotated tag vX.Y.Z on the snapshot already published to main — the mark of a packaged release (hubs
from git follow main itself; owner 2026-11-18). Called by tools/publish.sh --tag vX.Y.Z | --release; on its own:

    tools/release.py <git-url> (--tag vX.Y.Z | --release) [--embed <git-url>] [--check] [--skip-compat] [--overwrite-remote]

Steps: the latest snapshot of main (published.json in the state folder) must have the same address and the HEAD of this
copy, otherwise refuse (publish.sh --nightly first); in the remote's main after the snapshot — only snapshots
(tools/publish_remote.py: foreign commits — refusal with code 6, the next snapshot would roll them back;
--overwrite-remote — release anyway); the tag is newer than all releases and this snapshot is not yet released; stands —
tools/compat.py (the snapshot's MIN_CLAUDE — the Claude Code publish.sh pinned into it, on this machine and Windows) and own checks (tools/publish_checks.py); a tag with the CHANGES.md entries since
the previous release — on the snapshot's Sync commit; the tag is pushed without force; published-stable.json.
--check — everything up to the stands, without sending.
A pair (tools/publish_pair.py: published.json names the embed's repository and snapshot too; --embed names the one
expected): one number newer than the last release of both repositories, a tag in each — the embed's first; the core's
snapshot released already and the embed's not — the tag goes to the embed alone (a fix of the embed, the open core
keeps its release); a release stopped between the two pushes (release-pending.json in the state folder) goes on with
the core's tag under the same number.

Number (--release): the first release is v1.0.0; the first of the day is a minor (v1.4.0 → v1.5.0); another one the same
day is a patch (v1.5.0 → v1.5.1): fixes go out during the day. A major number only explicitly (--tag v2.0.0), when
compatibility breaks. Without the core (stdlib): the release test copies only tools/."""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELEASE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")   # as core/version.RELEASE
DATE = re.compile(r"^## (\d{4}-\d{2}-\d{2})\s*$", re.M)
_SESSIONS = []   # the compiled heading of the "for sessions" subsection, once


def sessions_heading():
    """The "for sessions" headings the core reads (core/forms.SESSION_SECTIONS: the English one and the earlier language's);
    without the core alongside (release test) the English one."""
    if not _SESSIONS:
        try:
            if ROOT not in sys.path:
                sys.path.insert(0, ROOT)
            from core import forms
            names = tuple(forms.SESSION_SECTIONS)
        except ModuleNotFoundError as e:   # no core alongside; a broken core is an error, not a reason to print the sessions' items
            if e.name not in ("core", "core.forms"):
                raise
            names = ("### For sessions",)
        _SESSIONS.append(re.compile("^(?:" + "|".join(re.escape(n) for n in names) + r")\s*$", re.M))
    return _SESSIONS[0]


def __getattr__(name):   # release.SESSIONS: the heading regex, compiled on the first use
    if name == "SESSIONS":
        return sessions_heading()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class Refused(Exception):
    def __init__(self, text, code=2):
        super().__init__(text)
        self.code = code


def number(name):
    m = RELEASE.match(name or "")
    return tuple(map(int, m.groups())) if m else None


def git(repo, *args, check=True):
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=300)
    if check and r.returncode:
        raise Refused(f"git {' '.join(args)}: {r.stderr.strip()[:300]}", 3)
    return r.stdout.strip() if r.returncode == 0 else ""


def state_dir():
    """State folder — as in the core (core.daemon); without the core alongside (release test) — HUB_STATE_DIR or ~/.hub-core.
    publish.sh takes it from here too (it writes published.json, this file reads it)."""
    try:
        sys.path.insert(0, ROOT)
        from core.daemon import default_state_dir
        return default_state_dir()
    except Exception:   # noqa: BLE001 — the core cannot be imported (not alongside, old python): as before
        return os.environ.get("HUB_STATE_DIR") or os.path.expanduser("~/.hub-core")


def ancestor(repo, a, b):
    """a is an ancestor of b; a git failure (not a plain "no") — refusal with the reason."""
    r = subprocess.run(["git", "merge-base", "--is-ancestor", a, b], cwd=repo, capture_output=True, text=True,
                       timeout=300)
    if r.returncode not in (0, 1):
        raise Refused(f"git merge-base {a} {b}: {r.stderr.strip()[:300]}", 3)
    return r.returncode == 0


def releases(pub):
    """[(number, name, tag date in local time)] of the releases in the pub copy, oldest first. A release is a vX.Y.Z tag on
    the main history (as in modules/update): a tag on a foreign commit is not a release and does not hold the number."""
    out = []
    for line in git(pub, "for-each-ref", "--merged", "refs/heads/main", "--format=%(refname:lstrip=2) %(creatordate:unix)",
                    "refs/tags/").splitlines():
        name, _, ts = line.partition(" ")
        if number(name):
            day = datetime.date.fromtimestamp(int(ts or 0)).isoformat()
            out.append((number(name), name, day))
    return sorted(out)


def next_number(found, today):
    if not found:
        return "v1.0.0"
    (x, y, z), _, day = found[-1]
    return f"v{x}.{y}.{z + 1}" if day == today else f"v{x}.{y + 1}.0"


def _sections(text):
    """[(date, items)] of CHANGES.md, newest first, without the "For sessions" sections (sessions see those, not people)."""
    marks = list(DATE.finditer(text or ""))
    out, heading = [], sessions_heading()
    for i, m in enumerate(marks):
        body = text[m.end():marks[i + 1].start() if i + 1 < len(marks) else len(text)]
        out.append((m.group(1), heading.split(body)[0].strip()))
    return out


def _items(body):
    """The items of a day's text: an item is its "- " line with the indented lines that continue it (a nested one too);
    any other non-blank line is an item of its own."""
    out, open_item = [], False
    for line in body.splitlines():
        if open_item and line.strip() and line[:1] in " \t":
            out[-1] += "\n" + line
            continue
        if line.strip() and line[:1] not in " \t":   # an indented line after a blank one belongs to no item
            out.append(line)
        open_item = line.startswith("- ")   # a blank line ends an item (core/news.py parse)
    return out


def _flat(item):
    """An item as one line: a re-wrapped item is the same item."""
    return " ".join(x.strip() for x in item.splitlines())


def notes(current, previous):
    """CHANGES.md entries since the previous release: days newer than its last day and new items of the same day (items are written
    under the date of the day — a second release in a day takes what was not in the first). No previous one — the last day."""
    now, last = _sections(current), _sections(previous)
    if not last:
        picked = now[:1]
    else:
        since, old = last[0][0], {_flat(x) for x in _items(last[0][1])}
        picked = [(d, b) for d, b in now if d > since]
        picked += [(d, "\n".join(x for x in _items(b) if _flat(x) not in old)) for d, b in now if d == since]
        picked = [(d, b) for d, b in picked if b]
    return "\n\n".join(f"## {d}\n\n{b}" for d, b in picked) or "No CHANGES.md entries since the previous release."


def published(state, url, head):
    """The Sync commit of the last main snapshot: the address and HEAD are the same as in the last publish, otherwise refused."""
    try:
        with open(os.path.join(state, "published.json")) as f:
            pub = json.load(f)
    except (OSError, ValueError):
        pub = {}
    if not pub.get("sha"):
        raise Refused(f"no {state}/published.json — publish main first: tools/publish.sh {url} --nightly")
    if pub.get("url") != url:
        raise Refused(f"the last publication of main went to {pub.get('url')}, and the tag goes to {url}; not releasing")
    if pub["sha"] != head:
        raise Refused(f"HEAD ({head[:7]}) is not what is published in main ({pub['sha'][:7]}): first "
                      f"tools/publish.sh {url} --nightly, then the tag")
    if not pub.get("pub"):
        raise Refused("published.json has no snapshot commit — publish main with this version of publish.sh (--nightly)")
    return pub["pub"]


def pinned(pub, snap):
    """The Claude Code version publish.sh pinned into the snapshot (core/version.py CLAUDE_TESTED; MIN_CLAUDE in older
    snapshots) or None."""
    m = re.search(r'^(?:CLAUDE_TESTED|MIN_CLAUDE)\s*=\s*"([\d.]+)"', git(pub, "show", f"{snap}:core/version.py", check=False),
                  re.M)
    return m.group(1) if m else None


def stands(head, skip_compat, claude=None):
    """Benches before a release, as in publish.sh: Claude Code compatibility (claude — the snapshot's version, the one
    hubs will install) and the hub's own checks."""
    if skip_compat:
        print("WARNING: the Claude Code compatibility check is skipped (--skip-compat)", file=sys.stderr)
    elif subprocess.call([sys.executable, os.path.join(ROOT, "tools", "compat.py")] + (["--versions", claude] if claude else [])):
        raise Refused("compatibility with Claude Code is not confirmed (table above) — not releasing; in an emergency use --skip-compat", 5)
    if subprocess.call([sys.executable, os.path.join(ROOT, "tools", "publish_checks.py"), head]):
        raise Refused("the hub's own pre-release checks did not pass (see above) — not releasing", 7)


def private(pub, snap):
    """Tests of the private modules against the snapshot the tag will sit on (tools/private_check.py): before the benches and in --check."""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import private_check
    shot = tempfile.mkdtemp(prefix="hub-release-snap-")
    try:
        try:
            tar = subprocess.run(["git", "archive", "--format=tar", snap], cwd=pub, capture_output=True, timeout=300)
            if tar.returncode:
                raise Refused(f"git archive {snap[:7]}: {tar.stderr.decode(errors='replace').strip()[-300:]}", 3)
            if subprocess.run(["tar", "-x", "-C", shot], input=tar.stdout, timeout=300).returncode:
                raise Refused(f"could not unpack snapshot {snap[:7]} for the private modules test", 3)
        except (subprocess.TimeoutExpired, OSError) as e:
            raise Refused(f"snapshot {snap[:7]} could not be assembled for the private modules test: {e}", 3)
        text, code = private_check.check(shot)
    finally:
        shutil.rmtree(shot, ignore_errors=True)
    if code:
        raise Refused(f"{text}\nnot releasing", code)
    print(text)


def paired(state, url, embed=None):
    """(the embed's address, its snapshot) when the last publication of url was a pair (tools/publish_pair.py), else
    None; embed — the address the caller names: another one recorded, or none, is refused."""
    try:
        with open(os.path.join(state, "published.json"), encoding="utf-8") as f:
            pub = json.load(f)
    except (OSError, ValueError):
        pub = {}
    got = None
    if isinstance(pub, dict) and pub.get("url") == url and pub.get("embed_url") and pub.get("embed_pub"):
        got = (pub["embed_url"], pub["embed_pub"])
    if embed and (not got or got[0] != embed):
        raise Refused(f"the last publication of {url} carries " + (f"the embed of {got[0]}" if got else "no embed")
                      + f", not {embed}: publish the pair first (tools/publish_pair.py)")
    return got


PENDING = "release-pending.json"   # a pair's release between its two pushes (the state folder)


def pending(state, url, snap, pair):
    """The tag of a pair's release recorded before its pushes, for these same snapshots, or None."""
    try:
        with open(os.path.join(state, PENDING), encoding="utf-8") as f:
            got = json.load(f)
    except (OSError, ValueError):
        return None
    if isinstance(got, dict) and pair and number(got.get("tag")) and \
            (got.get("url"), got.get("pub"), got.get("embed_url"), got.get("embed_pub")) == (url, snap, *pair):
        return got["tag"]
    return None


def release(url, tag, check=False, skip_compat=False, name="", mail="", overwrite=False, embed=None):
    """embed — the embed's repository the pair went to (default: whatever published.json records for url). A pair is
    released under one number newer than the last release of both repositories, the embed's tag first; when the core's
    snapshot is released already and the embed's is not, the embed alone gets the tag (a fix of the embed). A pair's
    release that stopped after the embed's tag goes on with the core's (the same number, the embed's tag stays)."""
    if not (name and mail):   # before the benches: without an author the tag cannot be set and snapshots in main cannot be recognised, and the benches take minutes
        raise Refused("PUBLISH_NAME and PUBLISH_EMAIL — the author of the tag and snapshots", 2)
    head = git(ROOT, "rev-parse", "HEAD")
    state = state_dir()
    snap = published(state, url, head)
    pair = paired(state, url, embed)
    tmp = tempfile.mkdtemp(prefix="hub-release-")
    try:
        pub = os.path.join(tmp, "pub.git")
        git(tmp, "clone", "-q", "--bare", url, pub)
        repos = [(pub, url, snap)]
        if pair:   # the embed's repository first: its tag goes first
            epub = os.path.join(tmp, "embed.git")
            git(tmp, "clone", "-q", "--bare", pair[0], epub)
            repos.insert(0, (epub, pair[0], pair[1]))
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import publish_remote
        later = []
        for repo, where, commit in repos:
            if not ancestor(repo, commit, "refs/heads/main"):
                raise Refused(f"snapshot {commit[:7]} is not in main at {where} — not releasing")
            try:   # foreign commits in main after the snapshot: the check runs to the end and does not pass
                later.append(publish_remote.problem(repo, "refs/heads/main", where, commit, name, mail))
            except (publish_remote.Failed, OSError, subprocess.TimeoutExpired) as e:
                raise Refused(f"the comparison with the remote main failed: {e}", 3)
        later = "\n".join(x for x in later if x)
        if later and not (overwrite or check):
            raise Refused(later, 6)
        if later:
            print(later, file=sys.stderr)
        if later and overwrite:
            print("WARNING: releasing anyway (--overwrite-remote)", file=sys.stderr)
            later = ""
        found = {repo: releases(repo) for repo, _, _ in repos}
        every = sorted(x for got in found.values() for x in got)   # one number over both repositories
        half = pending(state, url, snap, pair)
        if pair and not half and os.path.exists(os.path.join(state, PENDING)):
            print(f"WARNING: {os.path.join(state, PENDING)} names an earlier release that may have stopped after the embed's "
                  "tag (another snapshot): that tag stays on the embed alone", file=sys.stderr)
        if half and not (git(repos[0][0], "rev-parse", "-q", "--verify", f"refs/tags/{half}^{{commit}}", check=False) == pair[1]
                         and not git(pub, "rev-parse", "-q", "--verify", f"refs/tags/{half}", check=False)):
            half = None   # not stopped between the two: nothing pushed, or both
        if half:
            if tag not in (None, half):
                raise Refused(f"the release {half} stopped after the embed's tag: finish it first (--tag {half} or --release)")
            tag = half
            every = [x for x in every if x[1] != half]
            print(f"{half}: the embed has its tag, the core not yet — the release goes on with the core's")
        if tag is None:
            tag = next_number(every, datetime.date.today().isoformat())
        if not number(tag):
            raise Refused(f"{tag}: not a release number vX.Y.Z")
        if any(n == tag for _, n, _ in every):
            raise Refused(f"{tag} is already released")
        if every and number(tag) <= every[-1][0]:
            raise Refused(f"{tag} is not newer than the last release {every[-1][1]}")
        on = {}
        for repo, where, commit in repos:
            if not (half and repo != pub) and git(repo, "rev-parse", "-q", "--verify", f"refs/tags/{tag}", check=False):
                raise Refused(f"{tag}: the name is taken by a tag not on main of {where} — pick a number (--tag vX.Y.Z)")
            last = found[repo][-1][1] if found[repo] else None
            if last and not ancestor(repo, f"refs/tags/{last}^{{commit}}", commit):
                raise Refused(f"snapshot {commit[:7]} does not continue {last} of {where} (is published.json stale?) — not releasing")
            on[repo] = [n for _, n, _ in found[repo] if git(repo, "rev-parse", f"refs/tags/{n}^{{commit}}") == commit]
        alone = bool(pair and on[pub] and not on[repos[0][0]])   # the embed's fix: the open core keeps its release
        if on[pub] and not alone:
            raise Refused(f"this snapshot is already released as {on[pub][-1]}; new things go to main first (--nightly)")
        if alone:
            text = f"The embed alone, on the open core's {on[pub][-1]}."
            print(f"release {tag} of the embed alone — snapshot {pair[1][:7]}, the core's {on[pub][-1]}\n\n{text}\n")
        else:
            prev = found[pub][-1][1] if found[pub] else None
            text = notes(git(pub, "show", f"{snap}:CHANGES.md", check=False),
                         git(pub, "show", f"refs/tags/{prev}:CHANGES.md", check=False) if prev else "")
            print(f"release {tag} — snapshot {snap[:7]}" + (f" and the embed's {pair[1][:7]}" if pair else "")
                  + (f", after {prev}" if prev else "") + f"\n\n{text}\n")
        private(pub, snap)
        if check:
            if later:
                raise Refused("check: the remote main has foreign commits (above) — will not release without --overwrite-remote", 6)
            print("the check passed, not releasing")
            return tag
        stands(head, skip_compat, pinned(pub, snap))
        # the message goes as a file: release notes can exceed the limit of a single argument (Linux — 128 KiB, once ~140 KB)
        message = os.path.join(tmp, "tag-message")
        with open(message, "w", encoding="utf-8", newline="") as f:   # no \r\n on Windows: --cleanup=verbatim would keep them
            f.write(f"{tag}\n\n{text}")
        record = os.path.join(state, PENDING)
        if pair and not alone:   # a stop between the two pushes goes on from here (pending)
            with open(record + ".tmp", "w", encoding="utf-8") as f:
                json.dump({"tag": tag, "url": url, "pub": snap, "embed_url": pair[0], "embed_pub": pair[1]}, f)
            os.replace(record + ".tmp", record)
        went = bool(half)
        try:
            for repo, where, commit in repos[:1] if alone else repos:
                if half and repo != pub:
                    continue
                git(repo, "-c", f"user.name={name}", "-c", f"user.email={mail}", "tag", "-a", "--cleanup=verbatim", tag,
                    "-F", message, commit)
                git(repo, "push", "-q", "origin", f"refs/tags/{tag}")   # no force: such a tag already exists — the remote refuses
                went = True
        except BaseException:
            if not went and os.path.exists(record):   # nothing went: nothing to go on with
                os.unlink(record)
            raise
        if pair and not alone:
            os.unlink(record)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"released: {tag} ({pair[0] if alone else url})")
    got = {"tag": tag, "sha": head, "pub": snap, "at": int(time.time()), "url": url}
    if pair:   # the team's build of this release: the open core at core_tag, the embed at tag (tools/package/team.py --core-ref)
        got.update(embed_url=pair[0], embed_pub=pair[1], core_tag=on[pub][-1] if alone else tag)
    with open(os.path.join(state, "published-stable.json"), "w") as f:
        json.dump(got, f)
        f.write("\n")
    return tag


def main(argv):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("url")
    what = p.add_mutually_exclusive_group(required=True)
    what.add_argument("--tag")
    what.add_argument("--release", action="store_true")
    p.add_argument("--embed", help="the embed's repository of the pair (default: the one published.json records)")
    p.add_argument("--check", action="store_true")
    p.add_argument("--skip-compat", action="store_true")
    p.add_argument("--overwrite-remote", action="store_true")
    a = p.parse_args(argv)
    try:
        release(a.url, a.tag, a.check, a.skip_compat, os.environ.get("PUBLISH_NAME", ""),
                os.environ.get("PUBLISH_EMAIL", ""), a.overwrite_remote, a.embed)
    except Refused as e:
        print(str(e), file=sys.stderr)
        return e.code
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
