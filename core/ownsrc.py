"""The working copy of the core on a hub from the one-line install: <state>/own/core, a git repository whose branch
`local` carries the person's own edits over the commit of the release the hub runs. The hub itself always runs from a
built code layer (core/appcode.py) — an edit right in the layer is lost with the next release, so the Hub Developer edits
here and makes a PR from here.

`hub app src` makes it and keeps it on the release: a clone of the release's source repository (MANIFEST.json "source",
or the address the Hub Developer names) on the release's commit, when this machine has access to it; otherwise a
repository of the release's own files — one commit "release X" without tests (the layer carries none, appcode.LEFT_OUT).
The branch `release` is always the release the hub runs; `git log release..local` is the person's edits. A newer release
moves `release` and never touches `local`: merging it is the Hub Developer's step, on the person's request.

`hub app apply` (build(), request()) makes the hub's code of it: the running release's own factory
(tools/package/layer.py) builds `local` into a layer <release>+local.<sha> whose manifest names the release it is over
("own": {release, commit, layer, edits}), next to the layers in a folder no prune takes (layers/.own-<sha>); the update
process (modules/update/layer.py apply_own) leaves it for the person's restart under the release's own watchdog — a
build that does not come up goes back to the clean release.

The Hub Developer's own plugins and modules live next to it, <state>/own/<name> (formerly <state>/plugins-dev: settle()
moves that folder once and leaves the old name as a pointer — the Hub Developer's session folder and the hub.json lines
that name it keep working, and so does an older core that makes plugins-dev)."""
import contextlib
import json
import os
import re
import shutil
import sys
import tempfile
import time

from core import appcode, appwatch, codestore, lock

OWN = "own"
OLD = "plugins-dev"
POINTER_OWED = ".plugins-dev-pointer"   # in own: plugins-dev moved here, its pointer not made yet
CORE = "core"
RELEASE = "release"   # the branch at the commit of the release the hub runs
LOCAL = "local"       # the person's edits over it
META = "hub-source.json"   # in the copy's .git: {kind: clone|archive, source, version, commit}
WHO = ("-c", "user.name=hub", "-c", "user.email=hub@localhost.invalid")   # the core's own commits of a release's files
WORK = re.compile(r"\.core-[a-z0-9_]{8}")   # the work folders of make() (tempfile.mkdtemp)
STAGE = ".own-"   # layers/.own-<sha>: a build waiting for the update process (no layer's name: the prune never takes it)
FLAG = os.path.join("update", "own-apply.json")             # {stage, name, at}: the build for the update process
RESULT = os.path.join("update", "own-apply.result.json")   # {name, result, at, error?}: what it did with it


class Refused(Exception):
    """The working copy is not made: what is wrong, for the Hub Developer."""


def folder(state):
    """<state>/own; plugins-dev while that is still a real folder that settle() could not move yet."""
    own, old = os.path.join(state, OWN), os.path.join(state, OLD)
    return old if not os.path.lexists(own) and os.path.isdir(old) and not appcode.is_pointer(old) else own


def path(state):
    return os.path.join(folder(state), CORE)


def settle(state):
    """plugins-dev → own, once; the old name stays a pointer to own. A state folder that has both as real folders is left
    as it is; a move the system refuses now (Windows: a process works in the folder) is tried again at the next start,
    plugins-dev stays the folder meanwhile; so is a pointer it refuses after the move (own/.plugins-dev-pointer marks it).
    -> the folder (folder())."""
    own, old = os.path.join(state, OWN), os.path.join(state, OLD)
    owed = os.path.join(own, POINTER_OWED)
    if folder(state) == old:
        try:
            open(os.path.join(old, POINTER_OWED), "w").close()   # before the move: it goes along with the folder
            os.replace(old, own)
        except OSError:
            if os.path.isdir(old) and not appcode.is_pointer(old):   # still here: not moved by another settle meanwhile
                with contextlib.suppress(OSError):
                    os.unlink(os.path.join(old, POINTER_OWED))   # not left in the folder in use
                return old
    if os.path.exists(owed):
        import platforms
        try:
            if not os.path.lexists(old):
                platforms.current.link_dir(own, old)
            os.unlink(owed)
        except OSError:
            pass   # no pointer yet: a session revived meanwhile comes up in own; the next settle tries again
    os.makedirs(own, exist_ok=True)
    return own


def _run(repo, *args, timeout=120, remote=None):
    """git in repo -> the finished process; no answer in time, or no git at all — Refused."""
    try:
        return codestore.git("-C", repo, *args, timeout=timeout, remote=remote)
    except RuntimeError as e:
        raise Refused(str(e)) from None


def _git(repo, *args, timeout=120, remote=None):
    r = _run(repo, *args, timeout=timeout, remote=remote)
    if r.returncode:
        cmd = next((a for a in args if not a.startswith("-") and "=" not in a), "")
        raise Refused(f"git {cmd}: {codestore.said(r)}")
    return r.stdout.strip()


def _has(repo, commit):
    try:
        return not _run(repo, "cat-file", "-e", f"{commit}^{{commit}}").returncode
    except Refused:
        return False


def _not_on_release(copy):
    """git does not move a branch a working tree has out (this one or another of the copy): `release` is ours to move,
    the person works on `local`."""
    if f"branch refs/heads/{RELEASE}\n" in _git(copy, "worktree", "list", "--porcelain") + "\n":
        raise Refused(f"{copy}: a working tree has `{RELEASE}` out — `git switch {LOCAL}` there first")


def _meta(copy):
    try:
        with open(os.path.join(copy, ".git", META), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_meta(copy, data):
    with open(os.path.join(copy, ".git", META), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def _open_core(man):
    """The release is composed of an open core and embeds (tools/package/compose.py; MANIFEST "core"): the copy is the
    open core's — a PR from it goes to a public repository, so no commit of it may carry the embeds (the own build takes
    them from the release, build())."""
    core = man.get("core")
    return isinstance(core, dict) and isinstance(core.get("commit"), str) and bool(core["commit"])


def _at(man):
    """The commit of the source repository a clone stands on: a composed tree is a commit of its own that no repository
    has — the clone is of the open core, at MANIFEST "core"."""
    return man["core"]["commit"] if _open_core(man) else man.get("commit") or ""


def _source(man):
    """The repository the copy clones by default: of a composed release, its open core (MANIFEST "core" "repo", an
    https address with no sign-in in it), else the release's "source"."""
    repo = man["core"].get("repo") if _open_core(man) else None
    if isinstance(repo, str) and re.fullmatch(r"https://[^@\s/]+/\S+", repo):
        return repo
    return man.get("source")


def _carries_embeds(copy):
    """A commit on a branch of the copy has a path under embed/ or EMBED.json (what a push could carry; the open core's
    remote branches never have them). git cannot tell — Refused: a copy is never moved on a guess."""
    r = _run(copy, "log", "--branches", "-1", "--format=%H", "--", "embed", "EMBED.json")
    if r.returncode:
        raise Refused(f"git log in {copy}: {codestore.said(r)}")
    return bool(r.stdout.strip())


def _stale(copy, man):
    """The copy cannot follow this release: the release is of an open core, and a branch of the copy carries the embeds
    (a clone of a closed tree with them, a copy of a release's files with them) — a PR of it would publish them. A copy
    made for an open core whose own commits put embed/ in is the person's, not stale: Refused, nothing moves."""
    if not (_open_core(man) and _carries_embeds(copy)):
        return False
    if _meta(copy).get("open"):
        raise Refused(f"a commit on a branch of {copy} has embed/ (a push carries every ancestor): the copy is of the "
                      f"open core and goes to a public repository — put the branch back on `{RELEASE}` and commit the "
                      "edits again without embed/ (the embeds are not edited here)")
    return True


def _set_aside(own, copy, meta, say):
    """The stale copy to own/core.before-<its version>[.N], its remotes removed (nothing in it is pushed by mistake);
    the person's edits stay there, the Hub Developer moves them as a patch."""
    linked = [line[9:] for line in _git(copy, "worktree", "list", "--porcelain").splitlines()
              if line.startswith("worktree ")][1:]
    if linked:   # moved, their .git would point nowhere
        raise Refused(f"{copy} is of a tree with the embeds and has working trees of its own ({', '.join(linked)}): "
                      "`git worktree remove` them, then `hub app src`")
    base = os.path.join(own, f"{CORE}.before-{re.sub(r'[^0-9A-Za-z.+_-]', '_', str(meta.get('version') or 'x'))}")
    aside, n = base, 1
    while os.path.lexists(aside):
        n += 1
        aside = f"{base}.{n}"
    try:
        os.replace(copy, aside)
    except OSError as e:   # Windows: a program holds a file of the copy
        raise Refused(f"{copy} is of a tree with the embeds and cannot be set aside now ({e}): close what holds it, "
                      "then `hub app src`") from e
    for remote in _run(aside, "remote").stdout.split():
        with contextlib.suppress(Refused):
            _git(aside, "remote", "remove", remote)
    left = _run(aside, "remote").stdout.split()
    say(f"the working copy was of {meta.get('source') or 'the release files'}, its history has code the open core has "
        f"not: set aside as {aside} ({'remotes left: ' + ', '.join(left) + ' — remove them' if left else 'no remotes'}), "
        f"a new copy is made; the committed edits move as a patch — `git -C {aside} diff --binary {RELEASE}...{LOCAL} -- . "
        f"':!embed' ':!EMBED.json' | git apply` in the new copy; changes not committed stay in {aside}")
    return aside


def release_of(code):
    """(manifest, layer folder) of the release the hub runs — on a build of the person's edits, the release it is over;
    Refused on a hub that does not run from a code layer."""
    root = appwatch.layer(code)
    if root is None:
        raise Refused(f"the hub's code ({code}) is not a code layer of the one-line install")
    try:
        man = appcode.load(root)
        own = appcode.own_of(man)
        if own:
            root = os.path.join(os.path.dirname(root), own["layer"])
            man = appcode.load(root)
        return man, root
    except appcode.Bad as e:
        raise Refused(str(e)) from None


def placed(man):
    """The paths of a layer's manifest a team build laid in place of an embed's file (embed/<name>/<path> under
    core/embed.PLACED, the same path outside it)."""
    from core import embed
    out = set()
    for rel in man["files"]:
        parts = rel.split("/", 2)
        if len(parts) == 3 and parts[0] == embed.FOLDER and parts[2].startswith(embed.PLACED) and parts[2] in man["files"]:
            out.add(parts[2])
    return out


def _release_files(root, man, into):
    """The release's own files (sources, not bytecode; not the layer's git stop) from its layer folder into a folder,
    each checked against the manifest: an edited layer gives no copy of an edit as the release. A release of an open
    core (_open_core) gives no embed: the copy stays the open core's. What a team build laid in place of an embed's
    (core/embed.PLACED — the company's signed launcher) is not the copy's either: the embed has it, a build of the copy
    lays it again (and the company's signed files stay out of an open core's copy)."""
    bad, apart = [], _open_core(man)
    laid = placed(man)
    for rel, entry in sorted(man["files"].items()):
        if appcode._cache(rel) or rel == appcode.GIT_STOP[0] or apart and rel.startswith("embed/") or rel in laid:
            continue
        src, dest = os.path.join(root, *rel.split("/")), os.path.join(into, *rel.split("/"))
        if appcode._sha_or_none(src) != entry["sha256"]:
            bad.append(rel)
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(src, dest)
        os.chmod(dest, 0o755 if entry.get("x") else 0o644)
    if bad:
        raise Refused(f"the code folder differs from its release in {len(bad)} files ({', '.join(bad[:5])}): "
                      "`hub doctor` puts the release back")


def _clean(tree):
    """Everything of a working tree but its .git: a new release's files replace the old ones whole."""
    for name in os.listdir(tree):
        if name != ".git":
            p = os.path.join(tree, name)
            appcode.remove_tree(p) if os.path.isdir(p) and not os.path.islink(p) else os.unlink(p)


def _commit_release(copy, root, man, parent=None):
    """A commit of the release's files moved onto `release` -> its sha. The first one is born on the unborn branch of a
    new repository; a later one (after parent, the previous release) is made in a working tree of its own beside the
    copy: the person's tree, index and HEAD are not touched — not their ignored files, not a rebase in progress. The
    release `release` already is (a run cut off before it wrote the copy's note) gets no second commit."""
    msg = f"release {man['version']} ({man.get('commit') or '?'})"
    if parent is not None and _git(copy, "log", "-1", "--format=%s", parent) == msg:
        return _git(copy, "rev-parse", parent)
    side = copy if parent is None else tempfile.mkdtemp(prefix=".release-", dir=os.path.dirname(copy))
    try:
        if side != copy:
            os.rmdir(side)
            _git(copy, "worktree", "add", "-q", "--detach", side, parent)
            _clean(side)
        _release_files(root, man, side)
        _git(side, "add", "-A", "-f")   # only the release's files are there: one the .gitignore names goes in too
        _git(side, *WHO, "commit", "-q", "--no-verify", "--allow-empty", "-m", msg)
        sha = _git(side, "rev-parse", "HEAD")
    finally:
        if side != copy:
            _drop_side(copy, side)
    _git(copy, "branch", "-f", RELEASE, sha)
    return sha


def _drop_side(copy, side):
    """The side working tree away, with its entry in the copy's .git/worktrees (git's own remove, else by hand)."""
    try:
        if os.path.isdir(side):
            _git(copy, "worktree", "remove", "-f", "-f", side)
    except Refused:
        pass
    if os.path.lexists(side):
        appcode.remove_tree(side)
    entry = os.path.join(copy, ".git", "worktrees", os.path.basename(side))
    if os.path.isdir(entry):
        appcode.remove_tree(entry)


def make(state, code, url=None, say=print):
    """The working copy, made or brought onto the release the hub runs -> status(). url — the source repository to clone
    (default: the release's own "source"); none, or no access — the release's files. One at a time (a lock in own)."""
    own = settle(state)
    with lock.exclusive(os.path.join(own, ".core.lock")):
        return _made(state, code, url, say)


def _made(state, code, url, say):
    """make() under its lock."""
    man, root = release_of(code)
    own = folder(state)
    copy = path(state)
    _sweep(own)
    if os.path.isdir(os.path.join(copy, ".git")) and _stale(copy, man):
        _not_on_release(copy)
        _set_aside(own, copy, _meta(copy), say)
    if os.path.isdir(os.path.join(copy, ".git")):
        return follow(state, code, say=say)
    if os.path.isdir(copy) and not appcode.is_pointer(copy) and not os.listdir(copy):
        os.rmdir(copy)   # an empty folder is no one's
    if os.path.lexists(copy):
        raise Refused(f"{copy} is a folder of its own, not the working copy: move it away, then `hub app src`")
    work = None
    try:
        try:
            work = _make(own, man, root, url or _source(man), say, named=bool(url))
        except OSError as e:   # the disk, a folder another program holds (Windows: an antivirus, a git that has not let go)
            raise Refused(f"{copy} is not made: {e}" + ("" if url else "; or from a clone of the source on this machine: "
                                                        "`hub app src --repo <its folder>`")) from e
        try:
            os.replace(work, copy)
        except OSError as e:
            raise Refused(f"{copy} is not made: {e}; again in a moment") from e
    finally:
        _discard(work)
    return status(state, code)


def _discard(work):
    """A work folder away, as far as the system lets: a leftover is no error here, the next make() sweeps it."""
    if work is not None:
        with contextlib.suppress(OSError):
            appcode.remove_tree(work)


def _sweep(own):
    """The work folders of runs that were cut off (make() holds the lock: none is in use)."""
    try:
        names = os.listdir(own)
    except OSError:
        return
    for name in names:
        if WORK.fullmatch(name) and os.path.isdir(os.path.join(own, name)):   # tempfile's name: .core- and 8 characters
            _discard(os.path.join(own, name))


def _make(own, man, root, url, say, named=False):
    """The finished copy in a new work folder of own -> its path (the caller moves it into place or discards it). A clone
    that fails — the network, a proxy that garbles the pack, no such commit — gives the copy of the release's files, said
    in a line; the failed clone's folder is never reused: what git left in it may be held by the system."""
    commit, why = _at(man), None
    meta = {"kind": "archive", "version": man["version"], "commit": man.get("commit") or "",
            **({"open": True} if _open_core(man) else {})}
    work = tempfile.mkdtemp(prefix=".core-", dir=own)
    try:
        if url:
            try:
                r = codestore.git("clone", "-q", "--no-checkout", "--", url, work, timeout=900, remote=url)
                why = codestore.said(r) if r.returncode else None
            except RuntimeError as e:   # no answer in time, or no git: the copy of files does not need the network
                why = str(e)
            if not why and not _has(work, commit):
                why = f"the release's commit {commit[:12]} is not in {url}"
            if not why:
                _git(work, "config", "core.autocrlf", "false")
                _git(work, "checkout", "-q", "-B", LOCAL, commit)
                _git(work, "branch", "-f", RELEASE, commit)
                if _open_core(man):   # the clone's own default branch: of a tree with embeds it would carry them
                    for b in _git(work, "for-each-ref", "--format=%(refname:short)", "refs/heads").split():
                        if b not in (LOCAL, RELEASE):
                            _git(work, "branch", "-D", b)
                meta.update(kind="clone", source=url)
            else:
                say(f"no clone of {url}: {why}; the copy is made of the release's files, without tests"
                    + ("" if named else " (a clone of the source on this machine instead: `hub app src --repo <its folder>`)"))
                _discard(work)
                work = tempfile.mkdtemp(prefix=".core-", dir=own)
        if meta["kind"] == "archive":
            _git(work, "init", "-q", "-b", LOCAL)
            _git(work, "config", "core.autocrlf", "false")
            _commit_release(work, root, man)
        _write_meta(work, meta)
    except BaseException:
        _discard(work)
        raise
    return work


def follow(state, code, say=print):
    """The branch `release` of an existing copy onto the release the hub runs now (a clone fetches it, a copy of files
    gets one more commit of the release's files); `local`, the working tree and HEAD are never touched. -> status()."""
    man, root = release_of(code)
    copy, meta = path(state), _meta(path(state))
    commit = man.get("commit") or ""
    if meta.get("version") == man["version"] and meta.get("commit") == commit:
        return status(state, code)
    _not_on_release(copy)
    if meta.get("kind") == "clone":
        at = _at(man)
        for more in ((), (at,)):   # the branches first; a commit no branch names yet — by itself
            if not _has(copy, at):
                _git(copy, "fetch", "-q", "origin", *more, timeout=900, remote=meta.get("source"))
        _git(copy, "branch", "-f", RELEASE, at)
    else:
        _commit_release(copy, root, man, parent=RELEASE)
    say(f"`{RELEASE}` is now {man['version']}; the edits on `{LOCAL}` are not merged: `git merge {RELEASE}` on request")
    _write_meta(copy, {**meta, "version": man["version"], "commit": commit, **({"open": True} if _open_core(man) else {})})
    return status(state, code)


def status(state, code=None):
    """{path, kind, source, version, commit, edits: commits on local not on release (merges not counted), dirty,
    behind: the hub runs a newer release than the copy follows} — {} when there is no copy."""
    copy = path(state)
    if not os.path.isdir(os.path.join(copy, ".git")):
        return {}
    meta = _meta(copy)
    out = {"path": copy, "kind": meta.get("kind") or "?", "source": meta.get("source"), "version": meta.get("version"),
           "commit": meta.get("commit")}
    try:
        out["edits"] = int(_git(copy, "rev-list", "--count", "--no-merges", f"{RELEASE}..{LOCAL}"))
        out["dirty"] = bool(_git(copy, "status", "--porcelain"))
    except (Refused, ValueError):
        out.update(edits=None, dirty=None)
    if code is not None:
        try:
            man, _ = release_of(code)
        except Refused:
            man = {}
        out["behind"] = bool(man) and (man.get("version"), man.get("commit")) != (out["version"], out["commit"])
    return out


def _python(state):
    """The environment's Python the layers' bytecode is of: the shell's launcher names it (layers/shell.json)."""
    try:
        with open(os.path.join(state, "layers", "shell.json"), encoding="utf-8") as f:
            got = json.load(f).get("python")
    except (OSError, ValueError, AttributeError):
        got = None
    return got if isinstance(got, str) and os.path.isfile(got) else sys.executable


FACTORY_TIMEOUT = 1800   # s: the factory compiles the whole tree


def _factory(root, args, timeout=FACTORY_TIMEOUT):
    """tools/package/layer.py of the clean release in a process of its own: its own core (appcode), not the running
    one's — a build of edits running now may carry an edit of either. -> what it printed; Refused with its words."""
    import subprocess
    path = os.path.join(root, "tools", "package", "layer.py")
    if not os.path.isfile(path):
        raise Refused(f"the release has no factory ({path})")
    try:
        r = subprocess.run([sys.executable, "-I", path, *args], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise Refused(f"the build did not end in {timeout} s") from None
    except OSError as e:
        raise Refused(f"the build did not start: {e}") from None
    said = (r.stdout + r.stderr).strip()
    if r.returncode:
        if "unrecognized" in said and "--embeds-from" in said.split("unrecognized", 1)[1]:
            raise Refused("the release's factory takes no embeds from the release: an older release")
        if "--own" in said and "unrecognized" in said:
            raise Refused("the release's factory builds no edits: an older release")
        raise Refused(f"the build: {said[-1500:] or r.returncode}")
    return said


def _embeds_apart(copy, sha, man):
    """A release of an open core with embeds, and the copy's commit has no embed/ (it never has: _stale): the build
    takes them from the release. A team's tree with its embeds builds the copy's own (a person's edit of them stays)."""
    return _open_core(man) and any(rel.startswith("embed/") for rel in man["files"]) and \
        bool(_run(copy, "cat-file", "-e", f"{sha}:embed").returncode)


def build(state, code, python=None, say=print):
    """`hub app apply`, its first half: `local` of the working copy built into layers/.own-<sha>/<name> by the release's
    own factory -> {name, stage, version, release, edits}. Refused: no copy, uncommitted changes, nothing over the
    release, `local` not over the release the hub runs (merge it first), the factory's refusal (a syntax error)."""
    man, root = release_of(code)
    own_dir = settle(state)
    with lock.exclusive(os.path.join(own_dir, ".core.lock")):   # one lock with the copy's follow: `release` stays put
        copy = _made(state, code, None, say)["path"]   # the copy's `release` onto the release the hub runs
        if _git(copy, "status", "--porcelain"):
            raise Refused(f"{copy} has changes not committed: commit them on `{LOCAL}` (the hub runs commits only; a new file too)")
        sha = _git(copy, "rev-parse", "--verify", "-q", f"{LOCAL}^{{commit}}")
        if _run(copy, "merge-base", "--is-ancestor", RELEASE, LOCAL).returncode:
            raise Refused(f"`{LOCAL}` is not over the release the hub runs ({man['version']}): `git merge {RELEASE}` "
                          "in the copy first, then the touched tests")
        edits = int(_git(copy, "rev-list", "--count", "--no-merges", f"{RELEASE}..{LOCAL}"))
        if not edits:
            raise Refused(f"no edits on `{LOCAL}` over the release {man['version']}: nothing to build")
        version = f"{man['version']}{appcode.OWN}{sha[:9]}"
        name = f"{version}-{man['flavor']}"
        if not appcode.NAME.match(name):
            raise Refused(f"{name}: too long for a layer's name")
        layers = os.path.dirname(root)
        stage = os.path.join(layers, STAGE + sha[:12])
        if os.path.lexists(stage):
            appcode.remove_tree(stage)
        os.makedirs(stage)
        own = {"release": man["version"], "commit": man.get("commit") or "", "layer": os.path.basename(root),
               "edits": edits}
        args = ["--repo", copy, "--ref", sha, "--out", stage, "--python", python or _python(state), "--flavor",
                man["flavor"], "--version", version, "--channel", man.get("channel") or "stable", "--own",
                json.dumps(own), "--keep", os.path.join(stage, name),
                *(["--source", man["source"]] if man.get("source") else []),
                *(["--issues", man["issues"]] if man.get("issues") else []),
                # the company's build profile the release is of (core/embed.py): a release that names one has a factory
                # that knows the flag; the embeds come with the copy's own tree, or — a copy of an open core, which has
                # none — from the release's own folder
                *(["--profile-name", man["profile"]] if isinstance(man.get("profile"), str) else []),
                *(["--embeds-from", root] if _embeds_apart(copy, sha, man) else [])]
        try:
            _factory(root, args)   # the clean release's own factory, in its own process
        except BaseException:
            appcode.remove_tree(stage)
            raise
    return {"name": name, "stage": stage, "version": version, "release": man["version"], "edits": edits}


def request(state, built):
    """`hub app apply`, its second half: the build for the update process, which leaves it for the restart."""
    from core import catalog
    os.makedirs(os.path.join(state, "update"), exist_ok=True)
    with contextlib.suppress(FileNotFoundError):
        os.unlink(os.path.join(state, RESULT))
    catalog.write_private(os.path.join(state, FLAG), {"stage": os.path.basename(built["stage"]), "name": built["name"],
                                                      "at": round(time.time())})


def result(state, name):
    """What the update process did with the build name: {result, error?}, or None — not yet."""
    try:
        with open(os.path.join(state, RESULT), encoding="utf-8") as f:
            got = json.load(f)
    except (OSError, ValueError):
        return None
    return got if isinstance(got, dict) and got.get("name") == name else None


def withdraw(state):
    """The request back, its build gone, while no update process has taken it -> True; False: it was taken already."""
    flag = os.path.join(state, FLAG)
    try:
        os.replace(flag, flag + ".withdrawn")
    except FileNotFoundError:
        return False
    try:
        with open(flag + ".withdrawn", encoding="utf-8") as f:
            stage = json.load(f).get("stage")
    except (OSError, ValueError, AttributeError):
        stage = None
    with contextlib.suppress(FileNotFoundError):
        os.unlink(flag + ".withdrawn")
    if isinstance(stage, str) and stage.startswith(STAGE) and "/" not in stage and "\\" not in stage:
        with contextlib.suppress(OSError):
            appcode.remove_tree(os.path.join(state, "layers", stage))
    return True
