#!/usr/bin/env python3
"""The opening of the public repository: the open snapshot of the published core, its checks, and the steps that make
the repository public with its install site. Without --go it only checks and prints the plan; nothing leaves the machine.

  python3 tools/oss_open.py [--repo-slug ACCOUNT/REPO] [--ref SHA] [--pages DIR] [--assets DIR --tag vX.Y.Z]
        [--catalog DIR [--catalog-slug ACCOUNT/REPO]] [--no-suite] [--work DIR] [--remote URL] [--catalog-remote URL]
        [--private] [--anew] [--go]

- the snapshot: the commit published to the working repository (published.json "pub", fetched from its "url" when this
  clone lacks it; --ref names another), cut as the open flavor (tools/package/layer.py open_tree: OPEN_CUT and its tests,
  the allow lines of forbidden strings, CHANGES.md from appcode.OPEN_CHANGES_FROM), as one commit on top of the public
  repository's main (the first one has no parent: the public history starts here);
- the checks, all of them before any step: the open guard against .open-deny of the main copy, .publish-deny and the
  shared forms (tools/publish_allow.py, as publish.sh), gitleaks, no Cyrillic outside the language catalogs, LICENSE and
  NOTICE, the opening day set (appcode.OPEN_CHANGES_FROM), and the whole suite on the tree (--no-suite skips it, said);
  the install site (--pages: install.sh, install.ps1, v1/releases.json; every file under GitHub Pages' 100 MB, all under
  1 GB, no write-in mark left, the same guard); the release assets (--assets: the environments, uploaded by
  tools/package/upload.py to the tag --tag on the snapshot); the plugin catalog (--catalog: the open tree's official
  one, core/marketplaces.OPEN_SOURCE — its file valid by the open tree's own code, every entry pinned, the same guard,
  forbidden strings, key formats and no Cyrillic);
- the steps, in an order the public sees whole: the snapshot into main (still private), the catalog's snapshot into
  its main, the tag and the release assets, the install site into gh-pages (one commit, no history: old layers do not
  pile up), the catalog public (an open hub connects it at its first start), then the repository public, Pages from
  gh-pages, private vulnerability reporting, Discussions, the description and the site address; last, the site is read
  back (install.sh and releases.json answer 200) and the catalog is read without a sign-in. --private stops before
  anything is seen: the snapshot, the catalog and the tag with its assets go in, both repositories stay private (no
  install site — its Pages need the repository public); a run without --private later does the rest.
- --anew: the public history starts with this snapshot even when the still private repository already has snapshots (a
  --private run): main becomes this one commit (forced), and the tag --tag of another tree goes with its release first;
  refused once the repository is public. What was cut from the open tree since those snapshots never shows in its
  history.
Codes: 0 — checked (and done with --go), 2 — wrong input, 3 — a check failed (listed), 4 — a step failed (the steps
before it are done; they are printed). Through git and gh (the login of this machine: an admin of the repository)."""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools", "package"))
from core import appcode, marketplaces  # noqa: E402
import layer  # noqa: E402
import upload  # noqa: E402
from tools import contributors, public_language, publish_allow, release  # noqa: E402

SLUG = "icons8/hub"
# the repositories this tool opens by default; tools/publish.sh refuses them (tools/publish_guard.py): the full tree never goes there
OPEN_TARGETS = (SLUG, marketplaces.OPEN_SOURCE)
PAGES_LIMIT = 100 * 1000 * 1000      # GitHub Pages: a file over 100 MB is refused
SITE_LIMIT = 1000 * 1000 * 1000      # and a site over about 1 GB
PAGES_NEED = ("install.sh", "install.ps1", "v1/releases.json")
DESCRIPTION = "Run a team of Claude Code or Codex agents from your browser and your phone"
TOPICS = ("claude-code", "ai-agents", "agent-orchestration", "telegram-bot", "self-hosted", "developer-tools")
CATALOG_DESCRIPTION = "The plugin catalog of Hub by Icons8: pages, themes, skills and helper processes, each pinned to a commit"


# git of this tool and of what it runs (upload.py's ls-remote) signs in through gh: the repository is private until it opens
GH_GIT = {"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "",
          "GIT_CONFIG_KEY_1": "credential.helper", "GIT_CONFIG_VALUE_1": "!gh auth git-credential"}


class Failed(Exception):
    pass


def run(cmd, cwd=None, check=True, data=None, env=None, timeout=7200):
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, input=data, timeout=timeout,
                           stdin=None if data is not None else subprocess.DEVNULL,
                           env={**os.environ, **env} if env else None)
    except subprocess.SubprocessError as e:   # a timeout: said as a failed step, not a traceback
        raise Failed(f"{' '.join(cmd[:4])}: {e}") from None
    if check and r.returncode:
        raise Failed(f"{' '.join(cmd[:4])}: {(r.stderr or r.stdout).strip()[-600:]}")
    return r


def pages_url(slug):
    account, name = slug.split("/")
    return f"https://{account.lower()}.github.io/{name}/"


def published():
    """(sha, url) of the snapshot published to the working repository: published.json of the state folder publish.sh
    writes it in (tools/release.py state_dir)."""
    with open(os.path.join(release.state_dir(), "published.json"), encoding="utf-8") as f:
        got = json.load(f)
    return got["pub"], got["url"]


def have(repo, sha):
    return run(["git", "-C", repo, "cat-file", "-e", f"{sha}^{{commit}}"], check=False).returncode == 0


def check_tree(tree, forbid):
    """What the open snapshot must not carry, as lines (empty — clean)."""
    bad = list(appcode.open_guard(tree, forbid, tests=True, spare=publish_allow.credit_file))
    bad += denied(tree, os.path.join(tree, "tools", "publish-allow.txt"))
    bad += leaks(tree)
    # the ratchet's own reading (letters, escapes, its exempt catalogs), held at zero: the public tree starts clean
    bad += [f"{p}: {n} lines with Cyrillic outside the language catalogs"
            for p, n in sorted(public_language.count(tree).items())]
    for name, need, what in (("LICENSE", "Apache License\n", "not the Apache-2.0 text"),
                             ("NOTICE", "Hub by Icons8\nCopyright ", "no copyright line")):
        try:
            with open(os.path.join(tree, name), encoding="utf-8") as f:
                if not f.read().lstrip().startswith(need):
                    bad.append(f"{name}: {what}")
        except OSError:
            bad.append(f"{name}: missing")
    # the credits (owner decisions 2026-11-17, 2026-11-18): the author line heads README.md's Contributors section,
    # CONTRIBUTORS.md is built under it
    try:
        with open(os.path.join(tree, "README.md"), encoding="utf-8") as f:
            credited = contributors.author_of(f.read())
    except OSError:
        credited = None
    if not credited:
        bad.append('README.md: no author line "Created by <Name> ([@login](https://github.com/login))"')
    if not os.path.isfile(os.path.join(tree, contributors.FILE)):
        bad.append(f"{contributors.FILE}: missing")
    if not appcode.OPEN_CHANGES_FROM:
        bad.append("appcode.OPEN_CHANGES_FROM is not set: CHANGES.md would carry the working repository's history")
    else:   # the "Updates" page and the message after an update read its entries: the opening day's one at least
        try:
            with open(os.path.join(tree, "CHANGES.md"), encoding="utf-8") as f:
                dated = re.search(r"(?m)^## \d{4}-\d{2}-\d{2}", f.read())
        except OSError:
            dated = None
        if not dated:
            bad.append(f"CHANGES.md: no entry from {appcode.OPEN_CHANGES_FROM} on (the opening day's, at least)")
    return bad


def denied(folder, allow):
    """.publish-deny and the shared forms over folder, as publish.sh reads them (allow — its allow lines, a file)."""
    bad = []
    main = os.path.dirname(os.path.abspath(os.path.join(ROOT, run(["git", "-C", ROOT, "rev-parse",
                                                                    "--git-common-dir"]).stdout.strip())))
    lists = [(os.path.join(main, ".publish-deny"), "-i"), (os.path.join(main, ".publish-deny-case"), ""),
             (os.path.join(ROOT, "tools", "publish-deny.txt"), "")]
    if os.path.abspath(ROOT) != main:   # a worktree's own lists add to the main copy's (as publish.sh)
        lists += [(os.path.join(ROOT, ".publish-deny"), "-i"), (os.path.join(ROOT, ".publish-deny-case"), "")]
    for n, (patterns, flag) in enumerate(lists):
        if not os.path.isfile(patterns):
            if n in (0, 2):
                bad.append(f"no {patterns}: the snapshot is checked against nothing")
            continue
        # the folder's own allow lines (the open tree's are fewer than the working repository's: layer.cut_open)
        r = run([sys.executable, os.path.join(ROOT, "tools", "publish_allow.py"), patterns, flag, allow, folder],
                check=False)
        if r.returncode:
            bad.append(f"publish_allow.py {os.path.basename(patterns)}: {r.stderr.strip()[-300:]}")
        bad += [f"{os.path.basename(patterns)}: {x[:200]}" for x in r.stdout.splitlines() if x.strip()]
    return bad


def leaks(folder):
    """gitleaks over folder, run from it (its allowlist names paths from the root)."""
    if not shutil.which("gitleaks"):
        return ["gitleaks is not installed: no check of key formats"]
    conf = os.path.join(ROOT, ".gitleaks.toml")
    r = run(["gitleaks", "dir", *(["--config", conf] if os.path.isfile(conf) else []), "--no-banner", "--redact",
             "--log-level", "warn", "--exit-code", "1", "."], cwd=folder, check=False)
    return ["gitleaks: " + (r.stdout + r.stderr).strip()[-600:]] if r.returncode else []


def pins_missing(entries):
    """Lines for the entries whose pinned commit GitHub does not show (a deleted or private repository, a commit that is
    gone): the strict reader checks the file's form, not that the code is there, and an open hub would meet a 404 at its
    first install. An entry of another kind of source, or a revoked one, is not asked."""
    pins = [(e.get("name"), e["source"]["github"], e["source"]["sha"]) for e in entries
            if isinstance(e, dict) and isinstance(e.get("source"), dict) and not e.get("revoked")
            and isinstance(e["source"].get("github"), str) and isinstance(e["source"].get("sha"), str)]
    if pins and not shutil.which("gh"):
        return ["gh is not installed: no check that the pinned commits are on GitHub"]
    bad = []
    for name, repo, sha in pins:
        if not (re.fullmatch(r"\w[\w.-]*/\w[\w.-]*", repo) and re.fullmatch(r"[0-9a-f]{40}", sha)):
            bad.append(f"{name}: the pin {repo}@{sha[:12]} is not owner/repo at a full commit")
            continue
        try:   # a stalled gh is a line for that entry, not the end of the other checks
            r = run(["gh", "api", f"repos/{repo}/commits/{sha}", "--jq", ".sha"], check=False, timeout=60)
        except (Failed, OSError) as e:
            bad.append(f"{name}: GitHub was not asked: {e}"[:200])
            continue
        if r.returncode:
            why = (r.stderr or r.stdout).strip().splitlines()
            last = why[-1][:120] if why else "no answer"
            # only a 404 (no repository) or a 422 (no such commit) says the pin is gone; a login, a limit or the network is this machine's, and is said so
            bad.append(f"{name}: {repo} at {sha[:12]} is not on GitHub ({last})" if "HTTP 404" in last or "HTTP 422" in last
                       else f"{name}: GitHub did not answer for {repo} ({last})")
    return bad


def check_catalog(folder, tree, forbid):
    """The plugin catalog that goes public with the core, as lines (empty — clean): its file valid by the open tree's
    own reader (the code an open hub reads it with), every entry pinned to a commit, plain files only, the guard, the
    forbidden strings, key formats, no Cyrillic (an author's manifest may be in another language: the catalog is not).
    The strict reader requires every entry pinned (source.sha)."""
    bad, files, listed = [], [], []
    for d, dirs, names in os.walk(folder):
        dirs[:] = [n for n in dirs if n != ".git"]
        for n in names + dirs:
            path = os.path.join(d, n)
            rel = os.path.relpath(path, folder).replace(os.sep, "/")
            if os.path.islink(path):
                bad.append(f"catalog: {rel} is a link")
            elif n in names:
                files.append(rel)
    if os.path.isdir(os.path.join(folder, ".git")):   # the snapshot is the files: a history would go out with them
        bad.append("catalog: a .git folder (give the files, not a clone)")
    try:   # the strict reader takes an empty list: an empty official catalog would go out
        with open(os.path.join(folder, "hub-marketplace.json"), encoding="utf-8") as f:
            listed = json.load(f).get("plugins")
        if not isinstance(listed, list) or not listed:
            bad.append("catalog: hub-marketplace.json lists no plugins")
    except (OSError, ValueError, AttributeError) as e:
        bad.append(f"catalog: hub-marketplace.json: {e}")
    r = run([sys.executable, "-m", "core.marketplace", "validate", os.path.abspath(folder), "--strict"], cwd=tree,
            check=False)
    if r.returncode:
        bad.append("catalog: " + (r.stdout + r.stderr).strip()[-400:])
    bad += [f"catalog: {x}" for x in pins_missing(listed if isinstance(listed, list) else [])]
    bad += [f"catalog: {x}" for x in appcode.open_guard(folder, forbid)]
    bad += [f"catalog: {x}" for x in denied(folder, os.path.join(tree, "tools", "publish-allow.txt"))
            if not x.startswith("no ")]   # a missing list is said once, by the tree's check
    bad += [f"catalog: {x}" for x in leaks(folder)]
    bad += [f"catalog: {p}: {n} lines with Cyrillic" for p, n in sorted(public_language.count(folder, files).items())]
    return bad


def check_pages(folder, forbid):
    bad, total = [], 0
    for need in PAGES_NEED:
        if not os.path.isfile(os.path.join(folder, *need.split("/"))):
            bad.append(f"pages: no {need}")
    for d, dirs, names in os.walk(folder):
        for n in names + dirs:
            path = os.path.join(d, n)
            rel = os.path.relpath(path, folder).replace(os.sep, "/")
            if os.path.islink(path):   # the site is plain files: a link would publish what it points to, unchecked
                bad.append(f"pages: {rel} is a link")
                continue
            if n in dirs:
                continue
            size = os.path.getsize(path)
            total += size
            if size >= PAGES_LIMIT:
                bad.append(f"pages: {rel} is {size / 1e6:.0f} MB (Pages takes files under 100 MB)")
            if n in ("install.sh", "install.ps1"):
                with open(path, "rb") as f:
                    text = f.read()
                if b"@@HUB_DELIVER_URL@@" in text:
                    bad.append(f"pages: {rel} has no address written in")
    if total >= SITE_LIMIT:
        bad.append(f"pages: {total / 1e6:.0f} MB in all (a Pages site is about 1 GB at most)")
    bad += [f"pages: {x}" for x in appcode.open_guard(folder, forbid)]
    return bad


def suite(tree):
    """The whole suite on the tree; it must leave the tree as it was (the tree is what goes out, checked before)."""
    try:
        r = run([sys.executable, os.path.join("tools", "test.py")], cwd=tree, check=False)
    except Failed as e:
        return [f"the whole suite: {e}"]
    tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
    bad = [] if r.returncode == 0 and tail and tail[-1].startswith("OK") else ["the whole suite: " + " | ".join(tail)]
    left = run(["git", "status", "--porcelain"], cwd=tree).stdout.strip()
    return bad + ([f"the suite changed the tree: {left[:300]}"] if left else [])


def check_assets(folder, tag):
    files = [n for n in os.listdir(folder) if n.endswith(".tar.gz")]
    bad = [] if files else [f"assets: no .tar.gz in {folder}"]
    for n in files:
        if not upload.of_release(n, tag):
            bad.append(f"assets: {n} is not named for {tag}")
        if os.path.getsize(os.path.join(folder, n)) > upload.LIMIT:
            bad.append(f"assets: {n} is over 2 GiB (a release asset is under)")
    return bad


def remote_of(a):
    return getattr(a, "remote", None) or f"https://github.com/{a.repo_slug}.git"


def remote_heads(url, cwd, *refs):
    """{ref: sha} of the remote, for exactly these refs (a tag also as its commit, <tag>^{})."""
    out = run(["git", "ls-remote", url, *refs], cwd=cwd, env=GH_GIT).stdout.split("\n")
    return {r: sha for sha, _, r in (x.partition("\t") for x in out if x) if r in refs}


def tag_free(a, tree):
    """Before any step: the tag of the assets is not on the public repository, or it is on its main that already holds
    this tree (a run again) — else the release would need the tag moved, which never happens here."""
    d = tempfile.mkdtemp(prefix="tag-", dir=a.work)
    run(["git", "init", "-q", d])
    got = remote_heads(remote_of(a), d, f"refs/tags/{a.tag}", f"refs/tags/{a.tag}^{{}}", "refs/heads/main")
    tag = got.get(f"refs/tags/{a.tag}^{{}}") or got.get(f"refs/tags/{a.tag}")
    if not tag or getattr(a, "anew", False):   # --anew: a tag of another tree goes with its release first (stale_tag)
        return []
    if tag != got.get("refs/heads/main"):
        return [f"{a.tag} is already on {a.repo_slug} at {tag[:12]}, not at its main"]
    run(["git", "fetch", "-q", remote_of(a), "refs/heads/main"], cwd=d, env=GH_GIT)
    run(["git", "--work-tree", tree, "add", "-A", "--", ".", ":!.git"], cwd=d)
    if run(["git", "write-tree"], cwd=d).stdout.strip() != run(["git", "rev-parse", "FETCH_HEAD^{tree}"], cwd=d).stdout.strip():
        return [f"{a.tag} is already on {a.repo_slug} at a snapshot of another tree"]
    return []


def still_private(a):
    """--anew only while the repository is private (its history is not seen yet): [] or the reason."""
    if getattr(a, "remote", None):   # a stand-in: nothing on GitHub to ask
        return other_tags(a)
    r = run(["gh", "api", f"repos/{a.repo_slug}", "--jq", ".private"], check=False, timeout=60)
    if not (r.returncode == 0 and r.stdout.strip() == "true"):
        return [f"--anew: {a.repo_slug} is not private (or not readable) — its history is seen already, it is never rewritten"]
    return other_tags(a)


def other_tags(a):
    """--anew: tags of the repository other than --tag keep the earlier snapshots reachable — named, not removed here."""
    d = tempfile.mkdtemp(prefix="tags-", dir=a.work)
    run(["git", "init", "-q", d])
    out = run(["git", "ls-remote", "--tags", remote_of(a)], cwd=d, env=GH_GIT).stdout.split("\n")
    names = sorted({x.split("\t")[1][len("refs/tags/"):].removesuffix("^{}") for x in out if "\t" in x})
    extra = [n for n in names if n != getattr(a, "tag", None)]
    return [f"--anew: tags other than --tag on {a.repo_slug}: {', '.join(extra)} — they keep the earlier snapshots "
            "reachable; remove them (and their releases) first"] if extra else []


def stale_tag(a, url, tree, work):
    """--anew, before the snapshot: the tag --tag on the repository goes, its release with it, unless it is at a root
    commit of this very tree (a run again of --anew); a tag on an earlier snapshot of the same tree goes too — it keeps
    the earlier history reachable and is not at the new main."""
    os.makedirs(work, exist_ok=True)
    run(["git", "init", "-q", work])
    got = remote_heads(url, work, f"refs/tags/{a.tag}", f"refs/tags/{a.tag}^{{}}")
    if not got:
        return
    run(["git", "fetch", "-q", url, f"refs/tags/{a.tag}"], cwd=work, env=GH_GIT)
    run(["git", "--work-tree", tree, "add", "-A", "--", ".", ":!.git"], cwd=work)
    root = not run(["git", "rev-parse", "-q", "--verify", "FETCH_HEAD^{commit}^1"], cwd=work, check=False).stdout.strip()
    if root and run(["git", "write-tree"], cwd=work).stdout.strip() == \
            run(["git", "rev-parse", "FETCH_HEAD^{tree}"], cwd=work).stdout.strip():
        return
    if not getattr(a, "remote", None) and run(["gh", "release", "view", a.tag, "-R", a.repo_slug], check=False).returncode == 0:
        run(["gh", "release", "delete", a.tag, "-R", a.repo_slug, "--yes"])
    run(["git", "push", "-q", url, f":refs/tags/{a.tag}"], cwd=work, env=GH_GIT)
    print(f"  {a.tag} of another tree removed from {a.repo_slug}" + ("" if getattr(a, "remote", None) else ", with its release"))


def put_main(url, slug, files, work, who, message, anew=False):
    """The files as one commit on top of main of url (the first one has no parent); a run again with the same files adds
    nothing; message — the text, or a function of main's CHANGES.md before it (None: no main yet) and of the text of
    main's commits (None: no main yet) giving it; anew — the
    commit has no parent and replaces main (--anew). -> the sha main holds."""
    os.makedirs(work, exist_ok=True)
    run(["git", "init", "-q", "-b", "main", work])
    heads = remote_heads(url, work, "refs/heads/main")
    run(["git", "--work-tree", files, "add", "-A", "--", ".", ":!.git"], cwd=work)
    mine = run(["git", "write-tree"], cwd=work).stdout.strip()
    if heads:
        run(["git", "fetch", "-q", url, "refs/heads/main"], cwd=work, env=GH_GIT)
        if run(["git", "rev-parse", "FETCH_HEAD^{tree}"], cwd=work).stdout.strip() == mine and \
                (not anew or not run(["git", "rev-parse", "-q", "--verify", "FETCH_HEAD^1"], cwd=work, check=False).stdout.strip()):
            # a run again: main already holds this tree, no second commit
            sha = run(["git", "rev-parse", "FETCH_HEAD"], cwd=work).stdout.strip()
            print(f"  main of {slug} already holds it: {sha}")
            return sha
    if callable(message):
        old = run(["git", "show", "FETCH_HEAD:CHANGES.md"], cwd=work, check=False) if heads else None
        log = run(["git", "log", "--format=%B", "FETCH_HEAD"], cwd=work, check=False) if heads else None
        message = message(None if old is None else old.stdout if old.returncode == 0 else "",
                          None if log is None else log.stdout if log.returncode == 0 else None)
    sha = run(["git", *who, "commit-tree", mine, *(["-p", "FETCH_HEAD"] if heads and not anew else []), "-m", message],
              cwd=work).stdout.strip()
    run(["git", "push", "-q", *(["-f"] if anew else []), url, f"{sha}:refs/heads/main"], cwd=work, env=GH_GIT)
    print(f"  main of {slug} = {sha}")
    return sha


def steps(a, tree, commit, pages, catalog=None):
    """[(what, function)] in the order they go."""
    url = remote_of(a)
    name, mail = os.environ.get("PUBLISH_NAME", ""), os.environ.get("PUBLISH_EMAIL", "")
    who = ["-c", f"user.name={name}", "-c", f"user.email={mail}"]
    work = os.path.join(a.work, "push")
    out = []
    # a rehearsal pushes to a stand-in (--remote for the core, --catalog-remote for the catalog): the settings of that
    # repository stay untouched on GitHub; the other one, if real, is set as on the day
    core_stand_in = bool(getattr(a, "remote", None))
    cat_stand_in = bool(getattr(a, "catalog_remote", None))

    def gh(args, stand_in):
        if stand_in:
            print("  a rehearsal: no change on GitHub" + (f" (gh {' '.join(args[:3])})" if args else ""))
            return
        run(["gh", *args])

    def message(old, history):
        """the snapshot's commit message, with its co-authors (tools/contributors.py credit): the first commit —
        everyone of the tree's CONTRIBUTORS.md (the whole history), a later one — whom its CHANGES.md thanks anew and
        whom the commits of main do not credit yet (the list's logins too); a login without a GitHub id is said, not a
        stop"""
        lines, missing = contributors.credit(tree, old, whole_list=True, history=history)
        for login in missing:
            print(f"  WARNING: no GitHub id for @{login} (gh api users/{login}): not a co-author of this commit")
        return f"Hub by Icons8, snapshot of {commit[:12]}" + ("\n\n" + "\n".join(lines) if lines else "")

    anew = getattr(a, "anew", False)

    def snapshot():
        if anew and a.tag:
            stale_tag(a, url, tree, os.path.join(a.work, "stale-tag"))
        # --anew: the commit is the history's first — its co-authors are every contributor, as for a first snapshot
        msg = (lambda old, history: message(None, None)) if anew else message
        a.pushed = put_main(url, a.repo_slug, tree, work, who, msg, **({"anew": True} if anew else {}))
    out.append((f"the open snapshot of {commit[:12]} into main of {a.repo_slug} ("
                + ("its history starts anew: main is this one commit, forced; a tag of another tree goes with its "
                   "release; " if anew else "one commit, ") + "the repository still private)", snapshot))
    private = getattr(a, "private", False)
    if catalog:
        cat_url = getattr(a, "catalog_remote", None) or f"https://github.com/{a.catalog_slug}.git"
        flows = os.path.isfile(os.path.join(catalog, ".github", "workflows", "check.yml"))

        def catalog_snapshot():
            if flows:   # its checks read the core, still private: they wait for it
                gh(["api", "-X", "PUT", f"repos/{a.catalog_slug}/actions/permissions", "-F", "enabled=false"],
                   cat_stand_in)
            put_main(cat_url, a.catalog_slug, catalog, os.path.join(a.work, "catalog-push"), who,
                     f"The plugin catalog of Hub by Icons8 ({commit[:12]})")
        out.append((f"the plugin catalog of {catalog} into main of {a.catalog_slug} (one commit, still private"
                    + ("; its Actions off until the core is public)" if flows else ")"), catalog_snapshot))
    if a.assets:
        def assets():
            got = remote_heads(url, work, f"refs/tags/{a.tag}^{{}}", f"refs/tags/{a.tag}")
            if not got:
                run(["git", "push", "-q", url, f"{a.pushed}:refs/tags/{a.tag}"], cwd=work, env=GH_GIT)
            elif a.pushed not in got.values():   # a tag of another snapshot (tag_free saw it first): never moved here
                raise Failed(f"{a.tag} is already on {a.repo_slug} at another commit")
            if core_stand_in:   # the release is the real repository's: upload.py does not take a stand-in
                return gh([], True)
            files = sorted(os.path.join(a.assets, n) for n in os.listdir(a.assets) if n.endswith(".tar.gz"))
            # upload.py leaves a file already attached with the same bytes as it is: a run again goes on
            run([sys.executable, os.path.join(ROOT, "tools", "package", "upload.py"), a.repo_slug, a.tag, *files, "--send"],
                env=GH_GIT)
        out.append((f"the tag {a.tag} on that snapshot and the environments of {a.assets} as its release assets", assets))
    if private:   # nothing public: the rest waits for a run without --private
        return out
    if pages:
        def site():
            d = os.path.join(tempfile.mkdtemp(prefix="site-", dir=a.work), "site")
            shutil.copytree(pages, d, symlinks=True)   # the caller's folder is not written into (links: refused above)
            open(os.path.join(d, ".nojekyll"), "a").close()   # the files as they are, no Jekyll build
            run(["git", "init", "-q", "-b", "gh-pages", d])
            run(["git", "add", "-A"], cwd=d)
            sha = run(["git", *who, "commit-tree", run(["git", "write-tree"], cwd=d).stdout.strip(), "-m",
                       f"The install site of {commit[:12]}"], cwd=d).stdout.strip()
            run(["git", "push", "-q", "-f", url, f"{sha}:refs/heads/gh-pages"], cwd=d, env=GH_GIT)
        out.append((f"the install site of {pages} into gh-pages (one commit, forced: no history)", site))

    if catalog:
        def catalog_public():
            gh(["api", "-X", "PATCH", f"repos/{a.catalog_slug}", "-f", "visibility=public", "-F", "has_wiki=false",
                "-f", f"description={CATALOG_DESCRIPTION}"], cat_stand_in)
        out.append((f"{a.catalog_slug} public, before the core: an open hub connects it at its first start",
                    catalog_public))

    def public():
        site = ["-f", f"homepage={pages_url(a.repo_slug)}"] if pages else []   # no site, no address that answers 404
        gh(["api", "-X", "PATCH", f"repos/{a.repo_slug}", "-f", "visibility=public", "-F", "has_discussions=true",
            "-F", "has_wiki=false", "-f", f"description={DESCRIPTION}", *site], core_stand_in)
        gh(["api", "-X", "PUT", f"repos/{a.repo_slug}/topics", *[x for t in TOPICS for x in ("-f", f"names[]={t}")]],
           core_stand_in)
        if catalog:   # the catalog points at the core only once it answers
            gh(["api", "-X", "PATCH", f"repos/{a.catalog_slug}", "-f", f"homepage=https://github.com/{a.repo_slug}"],
               cat_stand_in)
    out.append((f"{a.repo_slug} public, with Discussions, its description{', the site address' if pages else ''} and topics",
                public))

    def reporting():
        gh(["api", "-X", "PUT", f"repos/{a.repo_slug}/private-vulnerability-reporting"], core_stand_in)
    out.append(("private vulnerability reporting on (SECURITY.md and the issue chooser link it)", reporting))
    if catalog and flows:
        def catalog_checks():
            # its workflows run the core's code, now public: the variable names it; then the check of what went in
            if cat_stand_in:
                return gh([], True)
            there = run(["gh", "api", f"repos/{a.catalog_slug}/actions/variables/HUB_CORE_REPO"], check=False).returncode == 0
            run(["gh", "api", "-X", "PATCH" if there else "POST", f"repos/{a.catalog_slug}/actions/variables"
                 + ("/HUB_CORE_REPO" if there else ""), "-f", "name=HUB_CORE_REPO", "-f", f"value={a.repo_slug}"])
            # only on again: whatever policy of actions the repository has stays
            run(["gh", "api", "-X", "PUT", f"repos/{a.catalog_slug}/actions/permissions", "-F", "enabled=true"])
            run(["gh", "workflow", "run", "check.yml", "-R", a.catalog_slug, "--ref", "main"])
        out.append((f"the checks of {a.catalog_slug} on (HUB_CORE_REPO={a.repo_slug}) and run once", catalog_checks))
    if pages:
        def turn_on():
            if core_stand_in:
                return gh([], True)
            there = run(["gh", "api", f"repos/{a.repo_slug}/pages"], check=False).returncode == 0
            run(["gh", "api", "-X", "PUT" if there else "POST", f"repos/{a.repo_slug}/pages", "-f", "build_type=legacy",
                 "-f", "source[branch]=gh-pages", "-f", "source[path]=/"])
        out.append((f"GitHub Pages from gh-pages: {pages_url(a.repo_slug)}", turn_on))

        def read_back():
            if core_stand_in:
                return gh([], True)
            base = pages_url(a.repo_slug)
            for path in ("install.sh", "v1/releases.json"):
                for _ in range(40):   # a Pages build takes a minute or so
                    try:
                        req = urllib.request.Request(base + path, headers={"User-Agent": "agents-hub-oss-open"})
                        with urllib.request.urlopen(req, timeout=30) as r:
                            if r.status == 200:
                                break
                    except OSError:
                        pass
                    time.sleep(15)
                else:
                    raise Failed(f"{base}{path} does not answer 200 after 10 minutes")
            print(f"  {base}install.sh and releases.json answer")
        out.append(("the site read back", read_back))
    if catalog:
        def catalog_read():
            # read as a new hub reads it: git, no sign-in and none of this machine's settings (a global insteadOf to
            # ssh would read a private repository with this machine's key)
            d = tempfile.mkdtemp(prefix="read-", dir=a.work)
            r = run(["git", "-c", "credential.helper=", "ls-remote", cat_url, "refs/heads/main"], cwd=d, check=False,
                    env={"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                         "GIT_SSH_COMMAND": "false", "GIT_CONFIG_COUNT": "0", "GIT_CONFIG_PARAMETERS": ""})
            if r.returncode or not r.stdout.strip():
                raise Failed(f"{cat_url} is not read without a sign-in: {(r.stderr or r.stdout).strip()[-300:]}")
            print(f"  {a.catalog_slug} answers without a sign-in")
        out.append(("the catalog read without a sign-in", catalog_read))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="oss_open.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo-slug", default=SLUG)
    ap.add_argument("--ref", help="the commit to open (default: published.json pub)")
    ap.add_argument("--pages", help="the install site, built (install scripts, v1/releases.json, layers)")
    ap.add_argument("--assets", help="a folder of the environment archives for the release assets")
    ap.add_argument("--tag", help="the release tag the assets go to (vX.Y.Z)")
    ap.add_argument("--catalog", help="the plugin catalog's files (hub-marketplace.json, icons, …), not a clone")
    ap.add_argument("--catalog-slug", default=marketplaces.OPEN_SOURCE,
                    help="its repository (default: the open tree's official catalog)")
    ap.add_argument("--catalog-remote", help="push the catalog here instead of its GitHub address (a rehearsal)")
    ap.add_argument("--no-suite", action="store_true", help="do not run the whole suite on the open tree")
    ap.add_argument("--work", help="where to build (a new folder by default)")
    ap.add_argument("--remote", help="push here instead of https://github.com/<repo-slug>.git (a rehearsal)")
    ap.add_argument("--private", action="store_true",
                    help="only the code, the catalog and the release into the repositories, both left private")
    ap.add_argument("--anew", action="store_true",
                    help="the public history starts with this snapshot: the private repository's earlier snapshots and "
                         "the --tag release of another tree go (refused once it is public)")
    ap.add_argument("--go", action="store_true", help="do the steps (without it: checks and the plan only)")
    a = ap.parse_args(argv)
    for slug in (a.repo_slug, a.catalog_slug):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug):
            ap.error(f"{slug}: ACCOUNT/REPO")
    if a.catalog_slug.lower() == a.repo_slug.lower():
        ap.error("--catalog-slug: a repository of its own, not the core's")
    if a.private and a.pages:
        ap.error("--private: no install site (GitHub Pages of a private repository need a paid plan; it comes with the opening)")
    if bool(a.assets) != bool(a.tag) or (a.tag and not re.fullmatch(r"v\d+\.\d+\.\d+", a.tag)):
        ap.error("--assets and --tag vX.Y.Z go together")
    if a.go and not (os.environ.get("PUBLISH_NAME") and os.environ.get("PUBLISH_EMAIL")):
        ap.error("--go: PUBLISH_NAME and PUBLISH_EMAIL — the author of the public commits")
    a.work = a.work or tempfile.mkdtemp(prefix="hub-oss-open-")
    a.pushed = None
    try:
        if a.ref:
            ref = a.ref
        else:
            ref, source = published()
            if not have(ROOT, ref):
                run(["git", "-C", ROOT, "fetch", "-q", source, "main"])
        forbid = layer.deny_of()
        tree = os.path.join(a.work, "open")
        commit = layer.open_tree(ROOT, ref, tree, forbid)
        print(f"open tree of {commit[:12]}: {tree}")
        bad = check_tree(tree, forbid)
        if a.pages:
            bad += check_pages(os.path.abspath(a.pages), forbid)
        if a.catalog:
            bad += check_catalog(os.path.abspath(a.catalog), tree, forbid)
        if a.assets:
            bad += check_assets(a.assets, a.tag) or tag_free(a, tree)
        if a.anew:
            bad += still_private(a)
        if a.no_suite:
            print("the whole suite on the open tree: NOT RUN (--no-suite)")
        elif not bad:
            bad += suite(tree)
        if bad:
            print("not ready:\n  " + "\n  ".join(bad))
            return 3
    except (Failed, layer.Refused, OSError, KeyError, ValueError) as e:
        print(f"oss_open.py: {e}", file=sys.stderr)
        return 3
    plan = steps(a, tree, commit, os.path.abspath(a.pages) if a.pages else None,
                 os.path.abspath(a.catalog) if a.catalog else None)
    if a.private:
        print(f"--private: {a.repo_slug}" + (f" and {a.catalog_slug}" if a.catalog else "") + " stay private, nothing is opened")
    elif not a.pages:
        print("no --pages: the install site and Pages are not in the plan")
    if not a.catalog:
        print(f"no --catalog: the open hub's official catalog ({a.catalog_slug}) stays as it is")
    for n, (what, _) in enumerate(plan, 1):
        print(f"{n}. {what}")
    if not a.go:
        print("checked; nothing done (--go does the steps)")
        return 0
    for n, (what, do) in enumerate(plan, 1):
        print(f"step {n}: {what}")
        try:
            do()
        except (Failed, OSError) as e:
            print(f"step {n} failed: {e}\nthe steps before it are done", file=sys.stderr)
            return 4
    print(("in, still private: " if a.private else "open: ") + f"https://github.com/{a.repo_slug}"
          + (f" and {pages_url(a.repo_slug)}" if a.pages else "")
          + (f", the catalog https://github.com/{a.catalog_slug}" if a.catalog else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
