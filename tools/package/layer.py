"""The code layer factory (app spec §3.1, §3.2): the core's tree of one commit as the app runs it.

  python3 tools/package/layer.py --repo <path> --ref <vX.Y.Z|commit> --out <dir> --python <the environment's python>
        [--flavor team|open] [--version V] [--channel stable|nightly] [--forbid STRING …] [--test]
        [--source URL] [--issues ACCOUNT/REPO]

Result in <out>: code-<version>-<flavor>.tar.gz (one folder <version>-<flavor>/ inside: the tree without tests/, evals/,
docs/, its bytecode, MANIFEST.json — core/appcode.py) and code-<version>-<flavor>.json — the sheet: the archive's sha256
and size, the manifest's sum, the commit. The layer is the same on every OS; its bytecode is the environment's Python's
(checked-hash: an edited source is compiled again, not shadowed by the released .pyc).

- version: a release tag vX.Y.Z gives X.Y.Z (channel stable); any other commit — <the latest release before it>-nightly.
  <commit time>.<sha> (channel nightly), unless --version names it;
- flavor open: appcode.OPEN_CUT (the embeds, core/embed.py, the team's signed Mac launcher) is left out, OPEN_SWAP's
  files take their place, and the guard refuses the layer if any of it is still there, a bundle carries a Developer ID
  or a forbidden string is in any file or path, whatever the case: the --forbid ones and, for a release
  (not a hub's own build, --own), those of .open-deny in the main copy of this repository (deny_of; no such file — no
  open layer); --test first runs the guard and then the whole suite (tools/test.py) on the cut tree with its tests (an
  embed's tests go with it) — the core must work without the embeds; a red run builds no layer. The team flavor needs
  none of it: the open exit alone is held;
- source: the repository the hub's working copy of the core clones (core/ownsrc.py) — by default (team flavor) the repo's
  origin when it is an https address without a sign-in in it; issues: where a person files issues (`hub version`). The stdlib only."""
import argparse
import gzip
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
from core import appcode  # noqa: E402

RELEASE = re.compile(r"v(\d+\.\d+\.\d+)\Z")
ISSUES = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


class Refused(Exception):
    """The layer is not built: what is wrong, for a person."""


def git(repo, *args, data=False):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, timeout=600, stdin=subprocess.DEVNULL)
    if r.returncode:
        raise Refused(f"git {' '.join(args[:2])}: {r.stderr.decode(errors='replace').strip()[-400:]}")
    return r.stdout if data else r.stdout.decode().strip()


def version_of(repo, ref, commit):
    """(version, channel) of a ref: a release tag, or a nightly after the latest release before the commit."""
    m = RELEASE.match(ref)
    if m and git(repo, "rev-parse", f"refs/tags/{ref}^{{commit}}") == commit:
        return m.group(1), "stable"
    try:
        base = git(repo, "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*.[0-9]*.[0-9]*", commit)
    except Refused:
        base = "v0.0.0"
    base = RELEASE.match(base).group(1) if RELEASE.match(base) else "0.0.0"
    when = git(repo, "show", "-s", "--format=%cd", "--date=format-local:%Y%m%d%H%M", commit)
    return f"{base}-nightly.{when}.{commit[:9]}", "nightly"


def source_of(repo):
    """The repo's origin as the layer's "source": an https address with no user or token in it, else None."""
    try:
        url = git(repo, "remote", "get-url", "origin")
    except Refused:
        return None
    return url if public_url(url) else None


def public_url(url):
    import urllib.parse
    u = urllib.parse.urlsplit(url or "")
    return u.scheme == "https" and bool(u.hostname) and "@" not in u.netloc and not any(c.isspace() for c in url) \
        and len(url) <= 300


def runtime_min(tree):
    try:
        with open(os.path.join(tree, "core", "version.py"), encoding="utf-8") as f:
            m = re.search(r"(?m)^RUNTIME_MIN\s*=\s*(\d+)", f.read())
    except OSError:
        return 0
    return int(m.group(1)) if m else 0


def _norm(man):
    def norm(info):
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mtime = man["mtime"]
        if info.isdir():
            info.mode = 0o755
        else:   # the manifest's modes, not the factory's file system (Windows reports no program bit)
            entry = man["files"].get(info.name.split("/", 1)[-1]) or {}
            info.mode = 0o555 if entry.get("x") else 0o444
        return info
    return norm


def pack(tree, archive, man):
    """The folder as one deterministic .tar.gz (sorted names, no owners, the manifest's time and modes, gzip without a
    time)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as t:
        top = os.path.basename(tree)
        t.add(tree, arcname=top, recursive=False, filter=_norm(man))
        for d, dirs, names in os.walk(tree):
            dirs.sort()
            for n in sorted(dirs) + sorted(names):
                path = os.path.join(d, n)
                t.add(path, arcname=top + "/" + os.path.relpath(path, tree).replace(os.sep, "/"), recursive=False,
                      filter=_norm(man))
    tmp = archive + ".part"
    with open(tmp, "wb") as f, gzip.GzipFile(fileobj=f, mode="wb", mtime=0, compresslevel=9) as z:
        z.write(buf.getvalue())
    os.replace(tmp, archive)
    return {"name": os.path.basename(archive), "sha256": appcode.sha256(archive), "size": os.path.getsize(archive)}


def cut_open(tree, tests=False, forbid=()):
    """The open flavor's cut of a tree: OPEN_CUT — the embeds (core/embed.py), with their code, tests and phrases, and
    the team's signed Mac launcher, OPEN_SWAP's files moved to its place (a tree cut before has them there already); the
    lines of tools/publish-allow.txt that name a forbidden string (they allow what only an embed carries) and the
    entries of CHANGES.md before appcode.OPEN_CHANGES_FROM (the open snapshot's own history) and the numbers of pull
    requests in its thanks (they are the working repository's, see thanks_open). The contributors of README.md and
    CONTRIBUTORS.md are built first, from the whole history (tools/contributors.py)."""
    leave_out(tree, appcode.OPEN_CUT + (appcode.OPEN_CUT_TESTS if tests else ()))
    swap_open(tree)
    drop_lines(os.path.join(tree, "tools", "publish-allow.txt"), forbid)
    from tools import contributors   # the open flavor's only: a team build needs no more than core/appcode.py
    contributors.write(tree)
    own_history(os.path.join(tree, "CHANGES.md"), appcode.OPEN_CHANGES_FROM)
    if appcode.OPEN_CHANGES_FROM:   # not opened yet: the file as it is
        thanks_open(os.path.join(tree, "CHANGES.md"))


def drop_lines(path, forbid):
    """The lines of a text file that carry any of forbid, whatever the case, out of it (no file — nothing)."""
    low = [f.lower() for f in forbid if f]
    if not low or not os.path.isfile(path):
        return
    with open(path, encoding="utf-8", newline="") as f:
        lines = f.read().splitlines(keepends=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.writelines(x for x in lines if not any(s in x.lower() for s in low))


def own_history(path, since):
    """CHANGES.md without the entries (## YYYY-MM-DD and what follows) dated before since; the header stays. since None
    or no file — as it is."""
    if not since or not os.path.isfile(path):
        return
    with open(path, encoding="utf-8", newline="") as f:
        lines = f.read().splitlines(keepends=True)
    out, keep = [], True
    for x in lines:
        m = re.match(r"## (\d{4}-\d{2}-\d{2})(?:\s|$)", x)   # a day's heading, with a word after it or not
        if m:
            keep = m.group(1) >= since
        if keep:
            out.append(x)
    text = "".join(out).rstrip("\n") + "\n"
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text if out else "")


# "@login (PR #N)" of a thanks line, wrapped or not: the number of a pull request of the working repository (and the
# header's "#N", which says how such a line is written)
THANKS_PR = re.compile(r"(@[A-Za-z0-9-]+)\s+\(PR\s+#(?:\d+|N)((?:,\s*#\d+)*)([^()]*)\)")


def thanks_open(path):
    """CHANGES.md's thanks without the numbers of the working repository's pull requests (owner 11.11): the open
    repository has no such pull request. "thanks: @a (PR #7)" -> "thanks: @a"; words after the number stay,
    "@a (PR #7, partly)" -> "@a (partly)", unless they name another number or the modules — then they go with it. No
    file — nothing."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()

    def one(m):
        rest = m.group(3).strip()
        if rest.startswith(","):
            rest = rest[1:].strip()
        keep = rest and not re.search(r"#\d|\bmodules?\b", rest, re.I)
        return m.group(1) + (f" ({rest})" if keep else "")
    got = THANKS_PR.sub(one, text)
    if got != text:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(got)


def swapped(rel):
    """rel (with '/') at its place in the open tree: under a source of appcode.OPEN_SWAP — under its place."""
    for src, dst in appcode.OPEN_SWAP:
        if rel == src or rel.startswith(src + "/"):
            return dst + rel[len(src):]
    return rel


def swap_open(tree):
    """appcode.OPEN_SWAP: each of the open flavor's own files from where it lies to its place in the tree (what was
    there goes); a tree cut before has them in place already."""
    tree = os.path.abspath(tree)
    for src, dst in appcode.OPEN_SWAP:
        here, there = os.path.join(tree, *src.split("/")), os.path.join(tree, *dst.split("/"))
        if not os.path.isdir(here):
            continue
        if os.path.isdir(there) and not os.path.islink(there):
            appcode.remove_tree(there)
        elif os.path.lexists(there):
            os.unlink(there)
        os.replace(here, there)
        parent = os.path.dirname(here)
        while parent != tree and not os.listdir(parent):   # deploy/mac/open/ left empty
            os.rmdir(parent)
            parent = os.path.dirname(parent)


def leave_out(tree, globs):
    """Remove what matches globs (relative with '/'); folders left empty go too."""
    import fnmatch
    for d, dirs, names in os.walk(tree, topdown=False):
        for n in names:
            rel = os.path.relpath(os.path.join(d, n), tree).replace(os.sep, "/")
            if any(fnmatch.fnmatchcase(rel, g) for g in globs):
                os.unlink(os.path.join(d, n))
        if d != tree and not os.listdir(d):
            os.rmdir(d)


def deny_of(repo=None, missing_ok=False):
    """The open flavor's forbidden strings (appcode.DENY): .open-deny in the root of the main copy of repo (this one by
    default), then a worktree's own — one string a line, "#" a comment. Like .publish-deny it is in .gitignore: it names
    the team's servers and repositories, so the tree never carries them. No string in the main copy — Refused: an open
    release checked against nothing is no check. missing_ok: no such file in the main copy at all — [] (a company's
    checkout of the open core: its build profile's own list is its whole list); an empty one still refuses."""
    repo = repo or REPO
    common = os.path.abspath(os.path.join(repo, git(repo, "rev-parse", "--git-common-dir")))
    got = []
    for root in dict.fromkeys((os.path.dirname(common), os.path.abspath(repo))):
        try:
            with open(os.path.join(root, appcode.DENY), encoding="utf-8") as f:
                got += [s.strip() for s in f if s.strip() and not s.strip().startswith("#")]
        except FileNotFoundError:
            if missing_ok and root == os.path.dirname(common):
                return []
        if not got:
            raise Refused(f"no forbidden strings in {os.path.join(root, appcode.DENY)} — no open release without them")
    return got


def open_tree(repo, ref, out, forbid=()):
    """The open flavor's tree WITH its tests, for the factory's run of the whole suite (§3.2) and the open repository:
    the commit as published (without appcode.UNPUBLISHED), without OPEN_CUT (the embeds with their tests), as a git
    repository of one commit (tests that read git see the cut tree as a clone would); forbid — the forbidden strings
    (cut_open). out must not exist. -> the commit."""
    commit = git(repo, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}")
    os.makedirs(out)
    with tarfile.open(fileobj=io.BytesIO(git(repo, "archive", "--format=tar", commit, data=True))) as t:
        t.extractall(out, filter="data")
    leave_out(out, appcode.UNPUBLISHED)
    cut_open(out, tests=True, forbid=forbid)
    for args in (("init", "-q", "-b", "main"), ("add", "-A"),
                 ("-c", "user.name=factory", "-c", "user.email=factory@example.test", "commit", "-q", "--no-verify",
                  "-m", f"open flavor of {commit[:12]}")):
        git(out, *args)
    return commit


def check_open(repo, ref, work, python, args=(), timeout=7200, forbid=()):
    """The guard (forbid — the forbidden strings) and then the whole suite (tools/test.py) on the open tree of ref in work
    (must not exist; a refusal leaves it there for a look): green — the suite's last lines; a guard's finding, red,
    silent or out of time — Refused with them."""
    open_tree(repo, ref, work, forbid)
    from tools import publish_allow
    bad = appcode.open_guard(work, forbid, tests=True, spare=publish_allow.credit_file)
    if bad:
        raise Refused(f"the open tree still carries ({work}): " + "; ".join(bad[:10]))
    import platforms   # its own group or job: a timeout stops the run's workers too, not only tools/test.py
    try:
        r = platforms.current.run([python, os.path.join("tools", "test.py"), *args], timeout, cwd=work,
                                  capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  stdin=subprocess.DEVNULL, **platforms.current.apart())
    except subprocess.TimeoutExpired:
        raise Refused(f"the whole suite on the open tree did not end in {timeout} s ({work})") from None
    tail = (r.stdout + r.stderr).strip()[-3000:]
    if r.returncode or not tail:
        raise Refused(f"the whole suite on the open tree is red ({work}):\n{tail or '(nothing printed)'}")
    return tail


def build(repo, ref, out, python, flavor="team", version=None, channel=None, forbid=(), keep=None, tested=None,
          source=None, issues=None, own=None, profile=None, wanted=None, check=False, embeds_from=None):
    """The layer archive and its sheet in out; own — the manifest's "own" of a hub's own build (core/ownsrc.py); keep — a path where the built folder is left as well (the shell's seed);
    tested — the commit --test ran on: the layer is of it even if ref has moved since (a tag that no longer names it
    gives a nightly version); profile — the name of the company's build profile, wanted — its embeds; check — the
    rules of embedding and the contract of the slots (embed_check) hold the tree first, as for every profile;
    embeds_from — a hub's own build of a copy of an open core, which has no embed/: the embeds come from that release
    folder (each file checked against its manifest), and so do "core" and "embeds" of the MANIFEST.
    -> the sheet (dict). Refused — nothing is left in out."""
    if flavor not in appcode.FLAVORS:
        raise Refused(f"flavor {flavor!r}: one of {', '.join(appcode.FLAVORS)}")
    commit = tested or git(repo, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}")
    found, kind = version_of(repo, ref, commit)
    version, channel = version or found, channel or kind
    if not appcode.NAME.match(version):
        raise Refused(f"version {version!r}: digits first, then letters, digits, . + _ -")
    # the team flavor names its origin by default; the open one only what it is told (a team checkout's origin is not its)
    source = source or (source_of(repo) if flavor == "team" else None)
    if source and not public_url(source):
        raise Refused(f"--source {source!r}: an https address without a sign-in in it")
    if issues and not ISSUES.match(issues):
        raise Refused(f"--issues {issues!r}: ACCOUNT/REPO")
    name = f"{version}-{flavor}"
    minor = subprocess.run([python, "-I", "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                           capture_output=True, text=True, timeout=60).stdout.strip()
    if not minor:
        raise Refused(f"{python}: does not run")
    os.makedirs(out, exist_ok=True)
    work = tempfile.mkdtemp(prefix="hub-layer-", dir=out)
    try:
        tree = os.path.join(work, name)
        os.makedirs(tree)
        with tarfile.open(fileobj=io.BytesIO(git(repo, "archive", "--format=tar", commit, data=True))) as t:
            # the programs of the commit (0755 in git): an unpack on Windows keeps no mode, the layer is the same anyway
            programs = {m.name for m in t.getmembers() if m.isreg() and m.mode & 0o111}
            t.extractall(tree, filter="data")
        for d, _, names in os.walk(tree):   # git archive keeps links: the layer is plain files (Windows has no rights for links)
            for n in names:
                if os.path.islink(os.path.join(d, n)):
                    raise Refused(f"{os.path.relpath(os.path.join(d, n), tree)}: a link in the tree")
        release = None
        if embeds_from:
            more, release = embeds_of(embeds_from, tree)
            programs |= more
        if check:   # by this machine's Python: the result is the tree's, not the platform's being built
            embed_check(tree, sys.executable, None if flavor == "open" else wanted)
        made = provenance(repo, commit, tree, profile, cut=flavor == "open") if release is None else \
            {"profile": profile or release.get("profile"), "core": release.get("core"),
             "embeds": release.get("embeds") or []}   # EMBED.json goes with the open cut
        if flavor == "open":
            cut_open(tree, forbid=forbid)
            programs = {swapped(p) for p in programs}   # a program the swap moved is one at its place too
        leave_out(tree, appcode.LEFT_OUT)
        tops = os.path.join(tree, "embed")   # the embeds (core/embed.py: a folder with embed.json)
        for top in [os.path.join(tops, n) for n in sorted(os.listdir(tops))] if os.path.isdir(tops) else []:
            if not (re.fullmatch(r"[a-z0-9_-]{1,32}", os.path.basename(top))   # core/embed.NAME
                    and os.path.isfile(os.path.join(top, "embed.json"))):
                continue
            for rel in appcode.EMBED_LEFT_OUT:
                path = os.path.join(top, rel)
                if os.path.isdir(path):
                    appcode.remove_tree(path)
                elif os.path.isfile(path):
                    os.unlink(path)
        if flavor != "open":   # the open flavor has no embed/ left (cut_open)
            programs = place(tree, programs)
        with open(os.path.join(tree, appcode.GIT_STOP[0]), "wb") as f:
            f.write(appcode.GIT_STOP[1])
        r = subprocess.run([python, "-I", "-m", "compileall", "-q", "-j", "0", "--invalidation-mode", "checked-hash", "."],
                           cwd=tree, capture_output=True, text=True, timeout=1800, stdin=subprocess.DEVNULL)
        if r.returncode:
            raise Refused(f"the bytecode: {(r.stdout + r.stderr).strip()[-600:]}")
        man = appcode.make(tree, version, flavor, commit, channel, runtime_min(tree), minor,
                           mtime=int(git(repo, "show", "-s", "--format=%ct", commit)), programs=programs,
                           source=source, issues=issues, own=own, made=made)
        if flavor == "open":
            from tools import publish_allow
            bad = appcode.open_guard(tree, forbid, spare=publish_allow.credit_file)
            if bad:
                raise Refused("the open flavor still carries: " + "; ".join(bad[:10]))
        archive = pack(tree, os.path.join(work, f"code-{name}.tar.gz"), man)
        sheet = {"name": name, "version": version, "flavor": flavor, "channel": channel, "commit": commit,
                 "runtime_min": man["runtime_min"], "python": minor, "files": len(man["files"]), "sum": man["sum"],
                 "archive": archive}
        with open(os.path.join(work, f"code-{name}.json"), "w", encoding="utf-8") as f:
            json.dump(sheet, f, ensure_ascii=False, indent=1)
        for n in (f"code-{name}.tar.gz", f"code-{name}.json"):
            os.replace(os.path.join(work, n), os.path.join(out, n))
        if keep:
            appcode.remove_tree(keep)
            shutil.move(tree, keep)
        return sheet
    finally:
        appcode.remove_tree(work)


def place(tree, programs):
    """A team layer: the files the embeds place (core/embed.PLACED — the company's signed Mac launcher) laid at their path
    of the tree, byte for byte, a program among them still one; each embed keeps its own copy (a build of a person's edits
    takes the embeds from the release's files, embeds_of). The open flavor's launcher (appcode.OPEN_SWAP's place, where a
    composed tree of the open core has it) goes from a folder an embed lays a launcher of its own into. -> the programs.
    Refused: the tree has that path already. A factory whose core is older than Embed API 1.2 lays nothing."""
    try:
        from core import embed
    except ImportError:   # a core before the Embed API (the factory of an earlier release builds a person's edits)
        return programs
    if not (hasattr(embed, "placed") and hasattr(embed, "bundle")):
        return programs
    laid, seen = embed.placed(tree), set()
    for name, rel in laid:
        dest, src = os.path.join(tree, *rel.split("/")), f"{embed.FOLDER}/{name}/{rel}"
        whole = embed.bundle(rel)
        if os.path.lexists(dest) or whole and (name, whole) not in seen and os.path.lexists(os.path.join(tree, *whole.split("/"))):
            raise Refused(f"{src}: the tree has {whole or rel} already")
        if whole:
            seen.add((name, whole))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(os.path.join(tree, *src.split("/")), dest)
        if src in programs:
            programs = programs | {rel}
    apps = {os.path.dirname(embed.bundle(rel)) for _, rel in laid if embed.bundle(rel)}
    for _, dst in appcode.OPEN_SWAP:
        there = os.path.join(tree, *dst.split("/"))
        if dst.endswith(".app") and os.path.dirname(dst) in apps and os.path.isdir(there):
            appcode.remove_tree(there)
    return programs


def embed_check(tree, python, wanted=None):
    """The checks every build profile passes (Embed API, spec d2144426 §6), on the commit's tree with its tests: the
    rules of embedding (core/embed.py problems; wanted — exactly the profile's embeds) and the contract of each slot an
    embed fills (embed.check_tree) — both by the tree's own core, in a Python process of the tree. Refused."""
    # the tree's own core: its rules and its API number, not the factory's; no bytecode is written into the tree (the
    # layer compiles it checked-hash afterwards)
    code = ("import json, sys; sys.path.insert(0, sys.argv[1])\n"
            "import os\nif not os.path.isfile(os.path.join(sys.argv[1], 'core', 'embed.py')):\n"
            "    print('[]'); sys.exit()\n"   # a core before the API (a broken one is no such core: its import fails)
            "from core import embed\n"            "bad = embed.problems(sys.argv[1], json.loads(sys.argv[2]))\n"
            "print(json.dumps(bad or (embed.check_tree() if embed.roots() else [])))\n")
    state = tempfile.mkdtemp(prefix="hub-embed-check-")
    try:
        r = subprocess.run([python, "-I", "-B", "-c", code, tree, json.dumps(wanted)], capture_output=True, text=True,
                           timeout=300, stdin=subprocess.DEVNULL, cwd=state,
                           env={**os.environ, "HUB_STATE_DIR": state, "HUB_CONFIG_DIR": state, "HUB_NO_MODEL": "1"})
    except subprocess.TimeoutExpired:
        raise Refused("the check of the embeds did not end in 300 s (a slot's import hangs)") from None
    finally:
        appcode.remove_tree(state)
    try:
        bad = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else None
    except (ValueError, IndexError):
        bad = None
    if bad is None:
        raise Refused(f"the check of the embeds did not run: {(r.stdout + r.stderr).strip()[-600:]}")
    if bad:
        raise Refused("the embeds do not keep the Embed API (core/embed.py):\n" + "\n".join(bad[:20]))


def embeds_of(root, tree):
    """The embeds of the release in the layer folder root into the build tree (which must have no embed/ of its own):
    its sources as its MANIFEST lists them, each with its sum (an edited release gives none) -> (the programs among
    them, the release's manifest: the build's MANIFEST names its "core" and "embeds")."""
    if os.path.lexists(os.path.join(tree, "embed")):
        raise Refused("--embeds-from: the tree has an embed/ of its own")
    try:
        man = appcode.load(root)
    except appcode.Bad as e:
        raise Refused(f"--embeds-from: {e}") from None
    programs = set()
    for rel, entry in sorted(man["files"].items()):
        if not rel.startswith("embed/") or appcode._cache(rel):
            continue
        src, dest = os.path.join(root, *rel.split("/")), os.path.join(tree, *rel.split("/"))
        if appcode._sha_or_none(src) != entry["sha256"]:
            raise Refused(f"--embeds-from: {rel} differs from its release")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(src, dest)
        if entry.get("x"):
            programs.add(rel)
    return programs, man


def provenance(repo, commit, tree, profile=None, cut=False):
    """What the layer is made of, for its MANIFEST: {"profile"?, "core"?, "embeds"?} — EMBED.json of a composed tree
    (tools/package/compose.py), and for every embed the tree carries its commit and git's id of its folder (an embed in
    the core's own tree: repo "." and the build's commit); cut — the open flavor's layer, which carries none of them."""
    if not os.path.isfile(os.path.join(tree, "core", "embed.py")):   # a core before the Embed API
        return {}
    from core import embed
    try:
        with open(os.path.join(tree, "EMBED.json"), encoding="utf-8") as f:
            made = json.load(f)
    except (OSError, ValueError):
        made = {}
    made = made if isinstance(made, dict) else {}
    listed = {e.get("name"): e for e in made.get("embeds") or [] if isinstance(e, dict)}
    embeds = []
    for name, _ in [] if cut else embed.roots(tree):
        e = {k: v for k, v in (listed.get(name) or {}).items() if k in ("repo", "ref", "commit") and isinstance(v, str)}
        if not e.get("commit"):
            e = {"repo": ".", "commit": commit}
        embeds.append({"name": name, **e, "tree": git(repo, "rev-parse", f"{commit}:{embed.FOLDER}/{name}")})
    core = made.get("core") if isinstance(made.get("core"), dict) else None
    return {"profile": profile or (made.get("profile") if isinstance(made.get("profile"), str) else None),
            "core": {k: v for k, v in core.items() if k in ("repo", "ref", "commit")} if core else None,
            "embeds": embeds}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="layer.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--ref", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--python", required=True, help="the Python of the environment the layer runs on")
    ap.add_argument("--flavor", default="team", choices=appcode.FLAVORS)
    ap.add_argument("--version")
    ap.add_argument("--channel", choices=("stable", "nightly"))
    ap.add_argument("--forbid", action="append", default=[])
    ap.add_argument("--source", help="the repository a hub's working copy of the core clones (default: the origin)")
    ap.add_argument("--issues", help="ACCOUNT/REPO where a person files issues")
    ap.add_argument("--own", help="a hub's own build of the person's edits: the manifest's \"own\" as JSON (core/ownsrc.py)")
    ap.add_argument("--profile-name", help="the company's build profile the layer is of (its MANIFEST names it)")
    ap.add_argument("--keep", help="also leave the built folder here")
    ap.add_argument("--embeds-from", help="a hub's own build: the embeds of this release folder (its tree has none)")
    ap.add_argument("--test", action="store_true",
                    help="open flavor: first the whole suite (tools/test.py) on the cut tree with its tests; red — no layer")
    a = ap.parse_args(argv)
    try:
        # an open release is held against .open-deny; a hub's own build of the person's edits (--own) is no release
        forbid = a.forbid + (deny_of() if a.flavor == "open" and not a.own else [])
        tested = None
        if a.test:
            if a.flavor != "open":
                raise Refused("--test is the open flavor's run on its cut tree")
            work = tempfile.mkdtemp(prefix="hub-open-tree-")   # not in out: only the layer and its sheet go there
            os.rmdir(work)
            # one commit for both steps: a ref like HEAD may move while the suite runs
            tested = git(a.repo, "rev-parse", "--verify", "-q", f"{a.ref}^{{commit}}")
            print(check_open(a.repo, tested, work, a.python, forbid=forbid).splitlines()[-1])
            appcode.remove_tree(work)   # a red one stays where the refusal says, for a look
        try:
            own = json.loads(a.own) if a.own else None
        except ValueError:
            raise Refused("--own: not JSON") from None
        sheet = build(a.repo, a.ref, a.out, a.python, a.flavor, a.version, a.channel, forbid, keep=a.keep,
                      tested=tested, source=a.source, issues=a.issues, own=own, profile=a.profile_name, check=True,
                      embeds_from=a.embeds_from)
    except (Refused, appcode.Bad) as e:
        sys.exit(f"layer.py: {e}")
    print(f"{sheet['archive']['name']}  {sheet['files']} files  {sheet['archive']['size'] / 1e6:.1f} MB  "
          f"sha256 {sheet['archive']['sha256']}")


if __name__ == "__main__":
    main()
