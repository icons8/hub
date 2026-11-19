#!/usr/bin/env python3
"""The publication of a pair (spec d2597953 §2.2): one commit of this working repository as two snapshots — the open
core (everything but embed/) into its repository, the embed (embed/<name>, its folder as the repository's root) into its
own closed one. One command, all or nothing up to the first push.

  python3 tools/publish_pair.py <core-url> --embed <embed-url> [--embed-name NAME] [--check] [--no-suite]
        [--overwrite-remote] [--work DIR]
  python3 tools/publish_pair.py <core-url> --embed <embed-url> (--tag vX.Y.Z | --release) [--check] [--skip-compat]
        [--overwrite-remote]

- the open tree: HEAD cut as the open flavor (tools/package/layer.py open_tree), with the Claude Code of the stable
  channel as its CLAUDE_TESTED (tools/compat.py --pin, as publish.sh); checked as tools/oss_open.py checks it (the open
  guard, the forbidden strings, gitleaks, no Cyrillic, LICENSE, the credits), the translations of the working tree
  (tools/i18n.py check --release) and the whole suite on the open tree (--no-suite skips it, said);
- the embed: embed/<name> of HEAD (the only folder under embed/, or --embed-name), checked for the forbidden strings and
  key formats at its place in the tree; its address is never an open repository (tools/publish_guard.py), nor the
  core's; the embed's main holds no core/ and the core's no embed/ (a mix-up of addresses, the team's full tree among
  them, lays no tree over another repository); the private modules' test of publish.sh has nothing
  to read here (modules/private.json is the embed's and pins no module), tools/release.py runs it at the tag;
- before any push: the main of both repositories against the last publication (tools/publish_remote.py: a foreign
  commit — code 6 for both, --overwrite-remote goes over it), the new CHANGES.md entries under today (code 4);
- the pushes: the embed first (nobody but the team's build reads it), then the core; a snapshot is one "Sync <day>"
  commit on top of main by PUBLISH_NAME / PUBLISH_EMAIL; a repository whose tree did not change gets no commit, nor the
  core when only CLAUDE_TESTED moved (the hubs on main would restart onto an untested claude for nothing);
- published.json (tools/publish_state.py, the hub's own address only): sha — HEAD, url and pub — the core's snapshot,
  embed_url and embed_pub — the embed's.
--tag / --release — the release of the published pair, tools/release.py: one number newer than the last release of
both repositories, the embed's tag first; an embed's fix alone tags the embed alone; a release stopped after the
embed's tag goes on with the core's under the same number.
Codes: 0 — published (checked with --check), 2 — wrong input, 3 — a check or a step failed, 4 — CHANGES.md, 6 — foreign
commits, 11 — no Claude Code version for the pin, 12 — the embed's address is an open repository."""
import argparse
import datetime
import io
import json
import os
import re
import sys
import tarfile
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tools", "package")]
from core import appcode, embed as embeds  # noqa: E402
import layer  # noqa: E402
from tools import contributors, oss_open, publish_guard, publish_remote, publish_state, release  # noqa: E402

run = oss_open.run


class Refused(Exception):
    def __init__(self, text, code=3):
        super().__init__(text)
        self.code = code


class Side:
    """One repository of the pair: its address, the snapshot's files, the work repository its commit is made in, the
    remote main (None: none yet) and the tree of the files."""

    def __init__(self, what, url, files, work):
        self.what, self.url, self.files, self.work = what, url, files, work
        self.main = self.tree = self.pushed = None
        self.same = False   # main already holds this tree


def prepare(side):
    """The remote main of side fetched, the tree of its files written (the files' .git aside)."""
    os.makedirs(side.work, exist_ok=True)
    run(["git", "init", "-q", "-b", "main", side.work])
    if oss_open.remote_heads(side.url, side.work, "refs/heads/main"):
        run(["git", "fetch", "-q", side.url, "refs/heads/main"], cwd=side.work, env=oss_open.GH_GIT)
        side.main = run(["git", "rev-parse", "FETCH_HEAD"], cwd=side.work).stdout.strip()
    stage(side)


def stage(side):
    run(["git", "--work-tree", side.files, "add", "-A", "--", ".", ":!.git"], cwd=side.work)
    side.tree = run(["git", "write-tree"], cwd=side.work).stdout.strip()
    side.same = bool(side.main) and run(["git", "rev-parse", "FETCH_HEAD^{tree}"], cwd=side.work).stdout.strip() == side.tree


def holds(side, path):
    """side's remote main has path (a file or a folder)."""
    return run(["git", "cat-file", "-e", f"FETCH_HEAD:{path}"], cwd=side.work, check=False).returncode == 0


def put(side, message):
    """side's tree as one commit on top of its main, pushed (never forced) -> the commit."""
    name, mail = os.environ["PUBLISH_NAME"], os.environ["PUBLISH_EMAIL"]
    sha = run(["git", "-c", f"user.name={name}", "-c", f"user.email={mail}", "commit-tree", side.tree,
               *(["-p", side.main] if side.main else []), "-F", "-"], cwd=side.work, data=message).stdout.strip()
    run(["git", "push", "-q", side.url, f"{sha}:refs/heads/main"], cwd=side.work, env=oss_open.GH_GIT)
    return sha


def pin(tree):
    """The stable channel's Claude Code into tree's CLAUDE_TESTED (tools/compat.py --pin, as publish.sh)."""
    r = run([sys.executable, os.path.join(ROOT, "tools", "compat.py"), "--pin", tree], check=False)
    if r.returncode:
        raise Refused(f"the Claude Code version for CLAUDE_TESTED is unknown: {(r.stderr or r.stdout).strip()[-300:]}", 11)


def conflicts(tree):
    """Unresolved merge conflicts (their markers once reached CHANGES.md of colleagues' hubs), as lines."""
    bad = []
    for d, dirs, names in os.walk(tree):
        dirs[:] = [n for n in dirs if n not in (".git", "vendor", "node_modules")]
        for n in names:
            path = os.path.join(d, n)
            try:
                with open(path, "rb") as f:
                    if re.search(rb"(?m)^(<<<<<<< |>>>>>>> )", f.read()):
                        bad.append(f"{os.path.relpath(path, tree)}: an unresolved merge conflict")
            except OSError as e:
                bad.append(f"{os.path.relpath(path, tree)}: {e}")
    return bad


def check_open(tree, forbid):
    return oss_open.check_tree(tree, forbid) + conflicts(tree)


def check_embed(tree):
    """tree holds the embed at its place in the working tree (embed/<name>): the allow lines and gitleaks' paths read so."""
    return (oss_open.denied(tree, os.path.join(ROOT, "tools", "publish-allow.txt")) + oss_open.leaks(tree)
            + conflicts(tree))


def translations(repo):
    """Every language the hub offers whole and fresh (tools/i18n.py check --release), as lines."""
    r = run([sys.executable, os.path.join(repo, "tools", "i18n.py"), "check", "--release"], cwd=repo, check=False)
    return [] if r.returncode == 0 else ["translations: " + (r.stdout + r.stderr).strip()[-600:]]


def suite(tree):
    return oss_open.suite(tree)


def only_pin(side):
    """The open tree differs from main only by CLAUDE_TESTED: main's core/version.py back into it, nothing new."""
    names = run(["git", "diff", "--name-only", "FETCH_HEAD", side.tree], cwd=side.work).stdout.split()
    if names != ["core/version.py"]:
        return False
    lines = run(["git", "diff", "-U0", "FETCH_HEAD", side.tree, "--", "core/version.py"], cwd=side.work).stdout
    changed = [x for x in lines.splitlines() if x[:1] in "+-" and not x.startswith(("+++ ", "--- "))]
    if not all(re.match(r'^[-+]CLAUDE_TESTED = "', x) for x in changed):
        return False
    old = run(["git", "show", "FETCH_HEAD:core/version.py"], cwd=side.work).stdout
    with open(os.path.join(side.files, "core", "version.py"), "w", encoding="utf-8", newline="\n") as f:
        f.write(old)
    stage(side)
    return True


def embed_name(repo, given):
    names = run(["git", "ls-tree", "-d", "--name-only", "HEAD", f"{embeds.FOLDER}/"], cwd=repo).stdout.split()
    names = [n.split("/", 1)[1] for n in names]
    if given:
        if given not in names:
            raise Refused(f"no {embeds.FOLDER}/{given} in HEAD (there are: {', '.join(names) or 'none'})", 2)
        return given
    if len(names) != 1:
        raise Refused(f"{len(names)} embeds in HEAD ({', '.join(names) or 'none'}): name the one with --embed-name", 2)
    return names[0]


def embed_tree(repo, commit, name, out):
    """embed/<name> of commit into out/embed/<name> -> that folder (the embed repository's root)."""
    tar = layer.git(repo, "archive", "--format=tar", f"{commit}:{embeds.FOLDER}/{name}", data=True)
    files = os.path.join(out, embeds.FOLDER, name)
    os.makedirs(files)
    with tarfile.open(fileobj=io.BytesIO(tar)) as t:
        t.extractall(files, filter="data")
    return files


def recorded(state):
    try:
        with open(os.path.join(state, "published.json"), encoding="utf-8") as f:
            got = json.load(f)
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def foreign(side, pub):
    """The refusal text when side's main has commits after the last publication that are not snapshots, "" — none."""
    if not side.main:
        return ""
    try:
        return publish_remote.problem(side.work, "FETCH_HEAD", side.url, pub or "", os.environ["PUBLISH_NAME"],
                                      os.environ["PUBLISH_EMAIL"])
    except publish_remote.Failed as e:
        raise Refused(f"the comparison with the main of {side.url} failed: {e}") from None


def top_day(path):
    try:
        with open(path, encoding="utf-8") as f:
            m = re.search(r"(?m)^## (.*)$", f.read())
        return m.group(1).strip() if m else None
    except OSError:
        return None


def core_message(side, today):
    """"Sync <day>", the newest CHANGES.md entry of the open tree, and its co-authors (tools/contributors.py credit: whom
    its CHANGES.md thanks anew and the list's logins no commit of main credits yet)."""
    old = history = None
    if side.main:
        r = run(["git", "show", "FETCH_HEAD:CHANGES.md"], cwd=side.work, check=False)
        old = r.stdout if r.returncode == 0 else ""
        history = run(["git", "log", "--format=%B", "FETCH_HEAD"], cwd=side.work).stdout
    try:
        with open(os.path.join(side.files, "CHANGES.md"), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        text = ""
    days = re.split(r"(?m)^(?=## )", text)
    entry = next((d.strip() for d in days if d.startswith("## ")), "")
    lines, missing = contributors.credit(side.files, old, whole_list=True, history=history)
    for login in missing:
        print(f"WARNING: no GitHub id for @{login}: not a co-author of this snapshot", file=sys.stderr)
    return f"Sync {today}" + (f"\n\n{entry}" if entry else "") + ("\n\n" + "\n".join(lines) if lines else "") + "\n"


def publish(core_url, embed_url, repo=ROOT, name=None, check=False, run_suite=True, overwrite=False, work=None):
    if not (os.environ.get("PUBLISH_NAME") and os.environ.get("PUBLISH_EMAIL")):
        raise Refused("PUBLISH_NAME and PUBLISH_EMAIL — the author of the snapshots", 2)
    if publish_guard.slug_of(embed_url) in publish_guard.open_targets():
        raise Refused(f"{embed_url} is an open repository: the embed is the company's and goes only to its own closed one", 12)
    if embed_url.strip() == core_url.strip() or (publish_guard.slug_of(embed_url)
                                                 and publish_guard.slug_of(embed_url) == publish_guard.slug_of(core_url)):
        raise Refused("the core and the embed go to two repositories: one for both would carry the embed with the core", 2)
    if run(["git", "status", "--porcelain", "--untracked-files=no", "--", ".", ":!docs", ":!evals/locale/ru"],
           cwd=repo).stdout.strip():
        raise Refused("there are uncommitted changes — commit first", 2)
    commit = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    name = embed_name(repo, name)
    state = release.state_dir()
    work = work or tempfile.mkdtemp(prefix="hub-publish-pair-")
    forbid = layer.deny_of(repo)
    tree = os.path.join(work, "open")
    layer.open_tree(repo, commit, tree, forbid)
    pin(tree)
    core = Side("core", core_url, tree, os.path.join(work, "core"))
    embed = Side("embed", embed_url, embed_tree(repo, commit, name, os.path.join(work, "embed-at")),
                 os.path.join(work, "embed"))
    for side in (embed, core):
        prepare(side)
    # a mix-up of addresses would lay one tree over the other's repository (the team's full tree among them): the
    # embed's main holds an embed at its root, the core's main holds no embeds
    if embed.main and holds(embed, "core"):
        raise Refused(f"main of {embed_url} holds a core (core/): not an embed's repository", 2)
    if core.main and holds(core, embeds.FOLDER):
        raise Refused(f"main of {core_url} holds {embeds.FOLDER}/: a full tree's repository, not the open core's", 2)
    if core.main and not core.same and only_pin(core):
        print("the core: only the Claude Code of the stable channel moved — no snapshot for that alone")
    # the open tree as checked is what goes: its own commit follows the pin (the suite says when a run changes it)
    run(["git", "add", "-A"], cwd=tree)
    run(["git", "-c", "user.name=factory", "-c", "user.email=factory@example.test", "commit", "-q", "--no-verify",
         "--allow-empty", "--amend", "-m", f"open flavor of {commit[:12]}"], cwd=tree)
    last = recorded(state)
    later = [x for x in (foreign(embed, last.get("embed_pub") if last.get("embed_url") == embed_url else ""),
                         foreign(core, last.get("pub") if last.get("url") == core_url else "")) if x]
    if later and not (overwrite or check):
        raise Refused("\n".join(later), 6)
    for x in later:   # --check goes on to the end and refuses last, as publish.sh
        print(x + ("\nWARNING: the snapshot goes over them (--overwrite-remote)" if overwrite else ""), file=sys.stderr)
    today = datetime.date.today().isoformat()
    if core.main and not core.same and \
            run(["git", "diff", "--quiet", "FETCH_HEAD", core.tree, "--", "CHANGES.md"], cwd=core.work, check=False).returncode \
            and top_day(os.path.join(tree, "CHANGES.md")) != today:
        raise Refused(f'CHANGES.md: new entries are not under "## {today}" — add the heading of the day', 4)
    for side in (embed, core):
        print(f"the {side.what}: " + ("nothing new" if side.same else
                                      f"a snapshot of {commit[:12]} into main of {side.url}"
                                      + ("" if side.main else " (its first)")))
    bad = check_embed(os.path.join(work, "embed-at")) if not embed.same else []
    if not core.same:
        bad += check_open(tree, forbid) + translations(repo)
        if not run_suite:
            print("the whole suite on the open tree: NOT RUN (--no-suite)")
        elif not bad:
            bad += suite(tree)
    if bad:
        raise Refused("not published:\n  " + "\n  ".join(bad))
    # both messages before the first push: a failure of the co-authors leaves both repositories as they were
    try:
        messages = {embed: f"Sync {today}\n", core: core_message(core, today) if not core.same else ""}
    except Exception as e:   # noqa: BLE001 — any failure of them: refused before anything went
        raise Refused(f"the co-authors are not known ({type(e).__name__}: {e}) — not published") from None
    if check:
        if later and not overwrite:
            raise Refused("check: a main has foreign commits (above) — it will not go without --overwrite-remote", 6)
        for line in messages[core].splitlines():   # what the core's snapshot credits besides its author
            if line.startswith("Co-authored-by:"):
                print(line)
        print("the check passed, nothing sent")
        return
    for side in (embed, core):
        if side.same:
            side.pushed = side.main
            continue
        side.pushed = put(side, messages[side])
        print(f"published: the {side.what} {side.pushed[:12]} into {side.url}")
    mine = publish_state.address(state, repo)
    if mine and mine != core_url:
        print(f"the publishing state of this hub stays with {mine}; to move it here: git config hub.release-url {core_url}",
              file=sys.stderr)
        return
    publish_state.write(os.path.join(state, "published.json"), json.dumps(
        {"sha": commit, "pub": core.pushed, "at": int(time.time()), "url": core_url, "embed_url": embed_url,
         "embed_pub": embed.pushed}) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="publish_pair.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("url", help="the open core's repository")
    ap.add_argument("--embed", required=True, help="the embed's closed repository")
    ap.add_argument("--embed-name", help="the folder under embed/ (default: the only one)")
    ap.add_argument("--repo", default=ROOT, help=argparse.SUPPRESS)   # the working repository (tests)
    ap.add_argument("--check", action="store_true", help="build and check, push nothing")
    ap.add_argument("--no-suite", action="store_true", help="do not run the whole suite on the open tree")
    ap.add_argument("--overwrite-remote", action="store_true", help="go over foreign commits of either main")
    ap.add_argument("--work", help="where to build (a new folder by default, removed after)")
    what = ap.add_mutually_exclusive_group()
    what.add_argument("--tag")
    what.add_argument("--release", action="store_true")
    ap.add_argument("--skip-compat", action="store_true", help="a release without the Claude Code stand (emergencies)")
    a = ap.parse_args(argv)
    try:
        if a.tag or a.release:
            if a.no_suite or a.work or a.embed_name:
                raise Refused("a release tags what is published: --no-suite, --work and --embed-name are the publication's", 2)
            release.release(a.url, a.tag, a.check, a.skip_compat, os.environ.get("PUBLISH_NAME", ""),
                            os.environ.get("PUBLISH_EMAIL", ""), a.overwrite_remote, embed=a.embed)
            return 0
        if a.skip_compat:
            raise Refused("--skip-compat is a release's: a publication runs no stands", 2)
        work = a.work and os.path.abspath(a.work)
        own = tempfile.mkdtemp(prefix="hub-publish-pair-") if not work else None
        try:
            publish(a.url, a.embed, a.repo, a.embed_name, a.check, not a.no_suite, a.overwrite_remote,
                    work or os.path.join(own, "w"))
        finally:
            if own:
                appcode.remove_tree(own)
    except (Refused, release.Refused) as e:
        print(str(e), file=sys.stderr)
        return e.code
    except (oss_open.Failed, layer.Refused, OSError, KeyError, ValueError) as e:
        print(f"publish_pair.py: {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
