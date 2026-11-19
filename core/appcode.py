"""The code layer of the app (app spec §3.1): the core's tree as one folder without .git, as the factory built and checked
it, with MANIFEST.json — every file's sha256 and size. The stdlib only, and nothing of the hub: the same file is the
factory's (tools/package/layer.py), the shell's (the factory copies it next to launch.py — an update of the layer cannot
break the check) and hubd's (the daily check).

- The layer lives in <state>/layers/<version>-<flavor>/; <state>/code — the path every part of the shell already uses —
  points at the one the hub runs (`current`: a link; a junction on Windows). The files are read-only (0444, 0555 for programs) — a sign "this is not edited", not a protection.
- An edit of the layer is noticed, not refused: quick() compares the list, sizes and times (every file carries the one
  time the factory gave the layer), full() — the sha256 too. What differs is a list of {path, kind: changed|missing|extra}.
- restore() puts the released files back in place, file by file (a running process keeps what it already imported; the
  hub restarts after), from a copy of the same release: the shell's seed or the release's archive. What it takes away
  is kept first in <version>-<flavor>-modified-<stamp> next to the layer (the last copy only): an edit is never lost
  silently.
- The open flavor (§3.2) leaves OPEN_CUT (the embeds, core/embed.py, the team's signed Mac launcher) out and puts
  OPEN_SWAP in its place; open_guard() refuses a layer that still has any of it, a Developer ID signature in a bundle
  or a forbidden string (the distribution address, the DSN).

MANIFEST.json: {"format": 1, "version", "flavor": team|open, "channel", "commit", "runtime_min", "built" (the files' time
as a date: the same commit gives the same archive), "python",
"mtime": the files' time, "files": {path: {"sha256", "size", "x": true for a program}}, "sum": sha256 of the files,
"source"?: the repository the commit is from (an https address, core/ownsrc.py clones it), "issues"?: ACCOUNT/REPO where
a person files issues, "own"?: a hub's own build of the person's edits (`hub app apply`, version <release>+local.<sha>) —
{release, commit, layer: the release's layer it is over, edits: how many commits}, "profile"?: the company's build
profile it was built by (tools/package/buildprofile.py), "core"?: {repo, ref, commit} of the open core a composed tree
is of (tools/package/compose.py), "embeds"?: [{name, repo, ref?, commit, tree}] — each embed it carries (core/embed.py;
repo "." — in the core's own tree, tree — git's id of its folder)}."""
import contextlib
import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import time

try:   # Windows: `current` is a junction (a link there needs a right a user may not have)
    import _winapi
except ImportError:
    _winapi = None

FORMAT = 1
MANIFEST = "MANIFEST.json"
FLAVORS = ("team", "open")
# what the open flavor does not carry: the embeds (core/embed.py — a company's own code: the team's login of accounts
# with its gateway and conduit, the Codex account pool, the reply voice, the cloud row of speech recognition, the stats
# report, the pin of its modules, their tests and phrases); the core is everything else and works without them
OPEN_CUT = ("embed/*", "EMBED.json",   # EMBED.json: what a composed tree is made of (tools/package/compose.py)
            "modules/accounts/accounts.py", "modules/gateway/gateway.py", "modules/conduit/conduit.py",
            "modules/stats/stats.py", "modules/voice/speak.py", "bin/hub-gateway", "bin/hub-conduit",
            "bin/hub-stats", "core/codexpool.py",   # the shims of the move for one release (core/embed.SHIMS): they lead into an embed
            "deploy/mac/Agents Hub.app/*",   # the Mac launcher signed by the company's Developer ID: OPEN_SWAP's instead
            "deliver/*")   # the delivery server of the team build (keys, installer links): the open build is given out by pages
#   (where an earlier tree has it; since Embed API 1.2 the embed carries it, core/embed.PLACED, under embed/*)
# what the open flavor carries in place of a team's file it cuts: (where it lies in the tree, where the open tree has it).
# The Mac launcher bundle "Hub by Icons8.app" (deploy/mac/app.py): built from the same deploy/mac/launcher.c, universal,
# signed ad hoc — no company's certificate in the open tree. A team layer leaves it out (LEFT_OUT)
OPEN_SWAP = (("deploy/mac/open/Hub by Icons8.app", "deploy/mac/Hub by Icons8.app"),)
OPEN_CUT_TESTS = ()   # the tests of an embed are in it (embed/<name>/tests): OPEN_CUT takes them
# what tools/publish.sh never publishes (its `rm -rf` line): the open tree is cut from what is published, not from the
# working tree
UNPUBLISHED = ("docs/*", "evals/locale/ru/*")
# the open snapshot's own history (spec d1973674 §2.2): its CHANGES.md keeps the entries from this day (YYYY-MM-DD) on;
# the earlier ones are the working repository's (they name its pull requests, servers and repositories). None until the
# open repository gets its first snapshot (private or public): the file goes as it is, and the guard holds its strings
OPEN_CHANGES_FROM = "2026-11-16"
# what a Mac bundle's signature carries when a Developer ID made it (its certificate's name): open_guard
SIGNED = b"Developer ID Application"
MACHO = (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce")   # one architecture
FAT = b"\xca\xfe\xba\xbe"   # universal (a Java class starts so too: its version follows, 45 and up, not a few architectures)


def macho(head):
    """head (the first 8 bytes) is a Mach-O's."""
    return head[:4] in MACHO or head[:4] == FAT and len(head) >= 8 and int.from_bytes(head[4:8], "big") < 20
# the open flavor's forbidden strings, one a line, in the root of the main copy (tools/package/layer.py deny_of): in
# .gitignore like .publish-deny — it names exactly what must not appear
DENY = ".open-deny"
# what the layer never carries (the publish tree without them, app spec §3.1)
LEFT_OUT = ("tests/*", "evals/*", "docs/*", ".git/*", ".github/*", "EMBED.json",   # EMBED.json: the MANIFEST says it
            *(src + "/*" for src, _ in OPEN_SWAP))   # the open flavor's own files where they lie (the cut moved them)
# ...nor the top of an embed's (core/embed.py) tests folder and its company's build profile — only at its top: a module's
# own tests/ or profile.json deeper in it is code
EMBED_LEFT_OUT = ("tests", "profile.json")
# a file the factory puts at the top of every layer: git stops at it ("not a git repository") instead of finding the
# repository the layer sits in (a state folder inside a home under git) — `git -C <layer>` of the update, the local
# edits and the private modules' address then acts on nothing; it is a file of the layer like any other (the manifest)
GIT_STOP = (".git", b"gitdir: .no-repository\n")
# what is in a layer folder and is not the layer: the shell's PACKAGED (written by the launcher), caches of another Python
IGNORED = ("PACKAGED", "PACKAGED.part", MANIFEST, ".DS_Store", "*/.DS_Store")
NAME = re.compile(r"[0-9][0-9A-Za-z.+_-]{0,63}\Z")
MODIFIED = "-modified-"
OWN = "+local."   # <release>+local.<sha>: the version of a hub's own build of the person's edits (core/ownsrc.py)
SIZE_MAX = 100 * 1024 * 1024   # a layer archive unpacks to at most this (installer S3's rule)


class Bad(RuntimeError):
    """The layer, its manifest or its source is not what it must be."""


def name_of(man):
    """<version>-<flavor> of a manifest — the layer's folder name."""
    version, flavor = man.get("version"), man.get("flavor")
    if not (isinstance(version, str) and NAME.match(version) and flavor in FLAVORS):
        raise Bad(f"a manifest without a version or flavor: {version!r} {flavor!r}")
    return f"{version}-{flavor}"


def version_key(version):
    """For ordering: the numbers of the version, then the rest (1.10.0 > 1.9.3; 1.9.3-nightly.20261107 > 1.9.3)."""
    m = re.match(r"(\d+)\.(\d+)\.(\d+)(.*)\Z", version or "")
    return (tuple(map(int, m.groups()[:3])), m.group(4)) if m else ((0, 0, 0), str(version or ""))


def _rel(root, path):
    return os.path.relpath(path, root).replace(os.sep, "/")


def _cache(rel):
    """A file in a __pycache__ folder: the bytecode Python keeps for a source next to it."""
    return "/__pycache__/" in "/" + rel


def _ignored(rel):
    return any(fnmatch.fnmatchcase(rel, p) for p in IGNORED)


def walk(root):
    """The files of a layer folder and the links in it (a linked folder is not entered: it is one entry), relative with
    '/', without what is not the layer."""
    out = []

    def failed(e):
        raise e   # a folder that cannot be read now: not "everything is gone"
    for d, dirs, names in os.walk(root, onerror=failed):
        dirs.sort()
        out += [_rel(root, os.path.join(d, n)) for n in dirs if is_pointer(os.path.join(d, n))]
        for n in sorted(names):
            rel = _rel(root, os.path.join(d, n))
            if not _ignored(rel):
                out.append(rel)
    return sorted(out)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def files_sum(files):
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def make(root, version, flavor, commit, channel="stable", runtime_min=0, python="", mtime=None, programs=None,
         source=None, issues=None, own=None, made=None):
    """The manifest of a built layer folder: every file of it (bytecode too). Sets the one time on every file and the
    read-only modes. programs — the paths that are programs (the commit's modes; Windows keeps none on a file), else
    the file's own mode says. "built" is the files' time: the same commit gives the same manifest whenever it is built.
    -> the manifest (also written as MANIFEST.json)."""
    if flavor not in FLAVORS:
        raise Bad(f"flavor {flavor!r}: one of {', '.join(FLAVORS)}")
    mtime = int(time.time() if mtime is None else mtime)
    files = {}
    for rel in walk(root):
        path = os.path.join(root, *rel.split("/"))
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            raise Bad(f"{rel}: not a regular file (a link in the layer)")
        entry = {"sha256": sha256(path), "size": st.st_size}
        program = rel in programs if programs is not None else bool(st.st_mode & 0o111)
        if program:
            entry["x"] = True
        files[rel] = entry
    man = {"format": FORMAT, "version": version, "flavor": flavor, "channel": channel, "commit": commit,
           "runtime_min": runtime_min, "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime)), "python": python,
           "mtime": mtime, "files": files, "sum": files_sum(files),
           **({"source": source} if source else {}), **({"issues": issues} if issues else {}),
           **({"own": own} if own else {}),
           **{k: v for k, v in (made or {}).items() if k in ("profile", "core", "embeds") and v}}
    name_of(man)
    seal(root, man)
    with open(os.path.join(root, MANIFEST), "w", encoding="utf-8", newline="\n") as f:
        json.dump(man, f, ensure_ascii=False, indent=1, sort_keys=True)
    _mode(os.path.join(root, MANIFEST), False)
    return man


def own_of(man):
    """The "own" of a hub's own build ({release, commit, layer, edits}), checked; None for a release."""
    own = man.get("own") if isinstance(man, dict) else None
    if not (isinstance(own, dict) and isinstance(own.get("release"), str) and NAME.match(own["release"])
            and isinstance(own.get("layer"), str) and NAME.match(own["layer"]) and isinstance(own.get("commit"), str)
            and isinstance(own.get("edits"), int) and not isinstance(own["edits"], bool)):
        return None
    return own


def _mode(path, x):
    os.chmod(path, 0o555 if x else 0o444)


def _writable(path):
    with contextlib.suppress(OSError):
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)


def folders_writable(root):
    """The folders of a layer writable by its owner (the files stay as seal() leaves them): its PACKAGED and the
    launcher's records are written into it."""
    for d, _, _ in os.walk(root):
        with contextlib.suppress(OSError):
            mode = stat.S_IMODE(os.lstat(d).st_mode)
            if not mode & stat.S_IWUSR:
                os.chmod(d, mode | stat.S_IWUSR)


def seal(root, man):
    """The manifest's time and the read-only modes on every file of it (after an unpack or a copy)."""
    for rel, entry in man["files"].items():
        path = os.path.join(root, *rel.split("/"))
        os.utime(path, (man["mtime"], man["mtime"]))
        _mode(path, entry.get("x"))


def load(root):
    """MANIFEST.json of a layer folder (or of an archive member's bytes). Bad when it is not one."""
    try:
        with open(os.path.join(root, MANIFEST), encoding="utf-8") as f:
            man = json.load(f)
    except (OSError, ValueError) as e:
        raise Bad(f"{root}: no manifest ({e})") from None
    return checked(man)


def checked(man):
    if not (isinstance(man, dict) and man.get("format") == FORMAT and isinstance(man.get("files"), dict)
            and isinstance(man.get("mtime"), int)):
        raise Bad("not a layer manifest of this format")
    if files_sum(man["files"]) != man.get("sum"):
        raise Bad("the manifest's file list does not match its sum")
    for rel, entry in man["files"].items():
        if not (_safe(rel) and isinstance(entry, dict) and isinstance(entry.get("size"), int)
                and re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256")))):
            raise Bad(f"a bad manifest entry: {rel!r}")
    name_of(man)
    return man


def _safe(rel):
    parts = rel.split("/")
    return bool(rel) and "\\" not in rel and not rel.startswith("/") and ":" not in parts[0] \
        and not any(p in ("", ".", "..") for p in parts)


def diff(root, man, full=False):
    """What differs between the layer folder and its manifest: [{path, kind}] sorted by path. quick (full=False) —
    the list, sizes and the manifest's time (an edit gives a new time even with the same size); full — the sha256 too."""
    out = []
    have = set(walk(root))
    linked = {rel for rel in have if is_pointer(os.path.join(root, *rel.split("/")))}
    for rel, entry in man["files"].items():
        path = os.path.join(root, *rel.split("/"))
        parts = rel.split("/")
        if any("/".join(parts[:i]) in linked for i in range(1, len(parts))):   # under a folder swapped for a link
            out.append({"path": rel, "kind": "missing"})
            continue
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            out.append({"path": rel, "kind": "missing"})
            continue
        if not stat.S_ISREG(st.st_mode) or st.st_size != entry["size"] or int(st.st_mtime) != man["mtime"] \
                or (full and _sha_or_none(path) != entry["sha256"]):
            out.append({"path": rel, "kind": "changed"})
    for rel in have - set(man["files"]):
        # a cache Python wrote for a source the manifest has no bytecode of (another Python, an edited source) is not an
        # edit — the edit is the source; a .pyc anywhere else is a module Python would import
        if not (_cache(rel) and rel.endswith(".pyc")):
            out.append({"path": rel, "kind": "extra"})
    return sorted(out, key=lambda x: x["path"])


def _sha_or_none(path):
    """sha256 of a file, None when it cannot be read (an editor holds it, no rights): that counts as changed."""
    try:
        return sha256(path)
    except OSError:
        return None


class Source:
    """The released bytes of a layer: a folder of the release (the shell's seed) or the release archive. Its own manifest
    is the release's — the layer's may be the edited file."""

    def __init__(self, where):
        self.where, self.tar, self.top = where, None, None
        try:
            if os.path.isfile(where):
                self.tar = tarfile.open(where)
                names = self.tar.getnames()
                self.top = names[0].split("/")[0] if names else ""
            self.manifest = self._raw(MANIFEST)
            self.man = checked(json.loads(self.manifest))
        except (OSError, ValueError, tarfile.TarError, Bad) as e:
            self.close()
            raise Bad(f"{where}: not a release ({e})") from None

    def _raw(self, rel):
        if self.tar is None:
            with open(os.path.join(self.where, *rel.split("/")), "rb") as f:
                return f.read()
        try:
            member = self.tar.getmember(f"{self.top}/{rel}")
        except KeyError:
            raise Bad(f"{rel}: not in the release archive") from None
        if not member.isreg():
            raise Bad(f"{rel}: not a file in the release archive")
        return self.tar.extractfile(member).read()

    def read(self, rel):
        if rel == MANIFEST:
            return self.manifest
        try:
            data = self._raw(rel)
        except (OSError, tarfile.TarError) as e:
            raise Bad(f"{rel}: not readable in the release ({e})") from None
        if hashlib.sha256(data).hexdigest() != self.man["files"][rel]["sha256"]:
            raise Bad(f"{rel}: the source is not the release either")
        return data

    def close(self):
        if self.tar is not None:
            self.tar.close()


def _put(path, data, mtime, x):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.restore"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        os.utime(tmp, (mtime, mtime))
        _mode(tmp, x)
        if os.path.lexists(path):
            _writable(path)   # Windows: os.replace onto a read-only file is refused
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            _writable(tmp)
            os.unlink(tmp)
        raise


def aside(root, changes, stamp=None):
    """The edited and the extra files of the layer copied next to it, <name>-modified-<stamp>/ with changes.json (the
    previous copy goes once this one is whole: the last one is kept). -> its path, or None when there is nothing to keep.
    Bad when a file cannot be copied (unreadable, no room): this copy goes, the previous one and the layer stay."""
    keep = [c for c in changes if c["kind"] != "missing" and not _special(os.path.join(root, *c["path"].split("/")))]
    if not keep:   # a pipe, a socket in place of a file: no bytes to keep, the previous copy stays
        return None
    parent, name = os.path.split(os.path.abspath(root))
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    dest = base = os.path.join(parent, f"{name}{MODIFIED}{stamp}")
    n = 1
    while os.path.lexists(dest):   # a copy of the same second is not overwritten
        n += 1
        dest = f"{base}-{n}"
    path = os.path.basename(dest)
    try:
        os.makedirs(dest)
        for c in keep:
            path = c["path"]
            src = os.path.join(root, *path.split("/"))
            if is_pointer(src):   # a link: where it led goes into changes.json, not what it led to
                c["link"] = os.readlink(src)
                continue
            to = os.path.join(dest, *path.split("/"))
            os.makedirs(os.path.dirname(to), exist_ok=True)
            shutil.copyfile(src, to)
        path = "changes.json"
        with open(os.path.join(dest, "changes.json"), "w", encoding="utf-8") as f:
            json.dump(changes, f, ensure_ascii=False, indent=1)
    except OSError as e:   # the edit would be lost: nothing is restored
        with contextlib.suppress(OSError):
            remove_tree(dest)
        raise Bad(f"{path}: not copied aside, the layer is left as it is ({e})") from None
    for old in os.listdir(parent):
        if old.startswith(name + MODIFIED) and old != os.path.basename(dest):
            with contextlib.suppress(OSError):   # a copy too many is no reason to keep the release from coming back
                remove_tree(os.path.join(parent, old))
    return dest


def _special(path):
    """A pipe, a socket or a device (not a file, a folder or a link): nothing in it to keep."""
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        return False
    return stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode)


def restore(root, source, stamp=None):
    """The released files back in the layer folder: -> {"changes": [...], "copy": path or None}. Nothing differs —
    nothing is touched. Bad when the source does not give the release's bytes or an edit cannot be copied aside (nothing
    is half-done: every file is read and checked, every edit copied, before the first one is written)."""
    src = Source(source)
    try:
        man = src.man
        if name_of(man) != os.path.basename(os.path.abspath(root)):
            raise Bad(f"{source}: the release {name_of(man)}, not the layer's")
        changes = diff(root, man, full=True)
        try:
            with open(os.path.join(root, MANIFEST), "rb") as f:
                mine = f.read()
        except OSError:
            mine = None
        try:   # the layer's own manifest, while it still reads: the same build, not another one of the same version
            own = checked(json.loads(mine)) if mine else None
        except (ValueError, Bad):
            own = None
        if own and own.get("commit") != man.get("commit"):
            raise Bad(f"{source}: another build of {name_of(man)} (commit {man.get('commit')}, the layer's {own.get('commit')})")
        if mine != src.manifest:   # the layer's own manifest edited or gone: the release's goes back too
            changes.append({"path": MANIFEST, "kind": "missing" if mine is None else "changed"})
        if not changes:
            return {"changes": [], "copy": None}
        data = {c["path"]: src.read(c["path"]) for c in changes if c["kind"] != "extra"}
    finally:
        src.close()
    copy = aside(root, changes, stamp)
    for c in changes:
        path = os.path.join(root, *c["path"].split("/"))
        if c["kind"] == "extra":
            _unlink(path)
        else:
            if is_pointer(path):
                _unlink(path)
            _put(path, data[c["path"]], man["mtime"], (man["files"].get(c["path"]) or {}).get("x"))
    left = diff(root, man, full=True)
    if left:
        raise Bad(f"the layer still differs after the restore: {left[0]['path']}")
    return {"changes": changes, "copy": copy}


def _unlink(path):
    """A file or a link (to a folder too: a junction on Windows goes with rmdir) away, read-only or not."""
    if not is_pointer(path):
        _writable(path)
    try:
        os.unlink(path)
    except (IsADirectoryError, PermissionError):
        if not is_pointer(path):
            raise
        os.rmdir(path)


def remove_tree(path):
    """A layer folder away: its files are read-only (Windows refuses to delete those)."""
    def again(func, p, exc):
        if isinstance(exc, FileNotFoundError):
            return
        _writable(p)
        func(p)
    if os.path.lexists(path):
        shutil.rmtree(path, onexc=again)


def unpack(archive, parent, expected=None):
    """A layer archive (<name>/… inside) into parent/<name>, checked against its manifest: only inside, no links, at most
    SIZE_MAX. -> (name, manifest). The folder appears whole or not at all (<name>.part on the way). expected: the name
    the archive must hold — another one is refused before anything is put in place (a layer of that name stays)."""
    with tarfile.open(archive) as t:
        members = t.getmembers()
        top = {m.name.split("/")[0] for m in members}
        if len(top) != 1:
            raise Bad(f"{archive}: not one folder")
        top = top.pop()
        if expected is not None and top != expected:
            raise Bad(f"{archive}: the folder {top}, not {expected}")
        if sum(m.size for m in members if m.isreg()) > SIZE_MAX:
            raise Bad(f"{archive}: over {SIZE_MAX // 2 ** 20} MB")
        for m in members:
            if not (m.isreg() or m.isdir()) or not (m.name == top or _safe(m.name)):
                raise Bad(f"{archive}: {m.name!r} — not a plain file inside")
        part = os.path.join(parent, top + ".part")
        remove_tree(part)
        os.makedirs(part)
        try:
            t.extractall(part, filter="data")
            man = load(os.path.join(part, top))
            if name_of(man) != top:
                raise Bad(f"{archive}: the folder {top} holds {name_of(man)}")
            seal(os.path.join(part, top), man)
            left = diff(os.path.join(part, top), man, full=True)
            if left:
                raise Bad(f"{archive}: {left[0]['path']} is not as the manifest says ({left[0]['kind']})")
            dest = os.path.join(parent, top)
            remove_tree(dest)
            os.replace(os.path.join(part, top), dest)
        finally:
            remove_tree(part)
    return top, man


def install(seed, parent):
    """A layer folder (the shell's seed) copied into parent/<name>, checked. -> (name, manifest)."""
    man = load(seed)
    name = name_of(man)
    part = os.path.join(parent, name + ".part")
    remove_tree(part)
    os.makedirs(parent, exist_ok=True)
    try:
        shutil.copytree(seed, part, ignore=lambda d, names: [n for n in names if n in ("PACKAGED", "PACKAGED.part")])
        folders_writable(part)   # a seed in a read-only environment gives its folders' modes to the copy
        seal(part, man)
        left = diff(part, man, full=True)
        if left:
            raise Bad(f"{seed}: {left[0]['path']} is not as the manifest says ({left[0]['kind']})")
        dest = os.path.join(parent, name)
        remove_tree(dest)
        os.replace(part, dest)
    finally:
        remove_tree(part)
    return name, man


def is_pointer(link):
    """The path is a link or a junction (not a real folder that happens to be there)."""
    return os.path.islink(link) or (hasattr(os.path, "isjunction") and os.path.isjunction(link))


def current(link, parent):
    """The name of the layer in parent the pointer link leads to, or None."""
    if not is_pointer(link):
        return None
    try:
        target = os.readlink(link)
    except OSError:
        return None
    name = os.path.basename(target.rstrip("/\\"))
    return name if os.path.isfile(os.path.join(parent, name, MANIFEST)) else None


def point(link, parent, name):
    """The pointer link → parent/name: a relative link swapped in one step (Windows — a junction: removed and made
    again). A real folder at link is not touched: Bad."""
    if os.path.lexists(link) and not is_pointer(link):
        raise Bad(f"{link} is a folder, not the layer's pointer")
    if _winapi is not None:
        if os.path.lexists(link):
            os.rmdir(link)   # a junction goes without its target
        _winapi.CreateJunction(os.path.join(parent, name), link)
        return
    tmp = f"{link}.{os.getpid()}.part"
    with contextlib.suppress(FileNotFoundError):
        os.unlink(tmp)
    # both ends by their real paths: a relative link is followed from the link's real folder (macOS: /var is
    # /private/var, and a link made from the two spellings led to /private/private/var — the Mac bridge)
    os.symlink(os.path.relpath(os.path.realpath(os.path.join(parent, name)),
                               os.path.realpath(os.path.dirname(os.path.abspath(link)))), tmp)
    os.replace(tmp, link)


NEXT = "next.json"   # <state>/layers/next.json {name, ...}: the layer the next full restart runs (modules/update/layer.py)


def pending(state):
    """The note left for the next full restart (layers/next.json {name, from, why: update|return, at}), or None."""
    try:
        with open(os.path.join(state, "layers", NEXT), encoding="utf-8") as f:
            note = json.load(f)
    except (OSError, ValueError):
        return None
    return note if isinstance(note, dict) else None


def version_ok(version, layered):
    """A version `hub update rollback` and the page's "Return" may name: a release vX.Y.Z; on a code layer its version
    (1.9.3 or v1.9.3, 1.9.3-nightly.…, NAME)."""
    if layered:
        return bool(NAME.match(version.removeprefix("v")))
    return bool(re.fullmatch(r"v\d+\.\d+\.\d+", version))


def on_layer(state, repo):
    """repo is a layer of this state folder (<state>/layers/<name> with its MANIFEST.json), not a clone."""
    real = os.path.realpath(repo)
    return os.path.isfile(os.path.join(real, MANIFEST)) and os.path.normcase(os.path.dirname(real)) == \
        os.path.normcase(os.path.realpath(os.path.join(state, "layers")))


def take_next(state):
    """The pointer <state>/code onto the layer an update (or a return) left for the next full restart. Called only while
    no part of the hub runs — hub-up between stopping its parts and starting again, the shell's launcher before hub-up —
    so no process mixes two releases. -> the name it points at now, or None: nothing was left, or that layer is not
    there (the note goes, the pointer stays). Errors of the pointer itself (OSError, Bad) go up."""
    parent = os.path.join(state, "layers")
    path = os.path.join(parent, NEXT)
    try:
        with open(path, encoding="utf-8") as f:
            note = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        note = None
    name = note.get("name") if isinstance(note, dict) else None
    link = os.path.join(state, "code")
    ok = isinstance(name, str) and bool(NAME.match(name)) and os.path.isfile(os.path.join(parent, name, MANIFEST)) \
        and is_pointer(link)
    now = current(link, parent) if ok else None
    if ok and now != name:
        mark, there = os.path.join(parent, now or "-", "PACKAGED"), os.path.join(parent, name, "PACKAGED")
        if now and os.path.isfile(mark) and not os.path.isfile(there):   # the shell's PACKAGED goes along (not an edit)
            shutil.copyfile(mark, there + ".part")
            os.replace(there + ".part", there)
        point(link, parent, name)
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)
    return name if ok else None


def restart_script(state, repo, script):
    """The path hub-up starts itself again by: through the pointer <state>/code when it runs from a layer of this state
    folder — its real path is that layer's own folder, which the pointer may have left for a newer one; else script."""
    link = os.path.join(state, "code")
    if is_pointer(link) and on_layer(state, repo):
        return os.path.join(link, os.path.relpath(os.path.realpath(script), os.path.realpath(repo)))
    return script


def open_guard(root, forbid=(), tests=False, spare=None):
    """The open flavor's guard: none of OPEN_CUT (with tests — of OPEN_CUT_TESTS either) nor of OPEN_SWAP where it lies,
    no file of a Mac bundle (*.app/) signed with a Developer ID (the open flavor's are signed ad hoc), and no forbidden
    string (a server or repository of the team, the distribution address, the DSN) in any file, path or link of the layer
    or open tree, whatever the case of its ASCII letters; a .git folder is not looked into, a link is read as its
    target's text, as it is published. spare(rel, data) -> the bytes to read instead, or None: the credits the publish
    check spares too (tools/publish_allow.py credit_file). -> [what is wrong]."""
    bad = []
    cut = OPEN_CUT + (OPEN_CUT_TESTS if tests else ()) + tuple(src + "/*" for src, _ in OPEN_SWAP)
    needles = [s.encode().lower() for s in forbid if s]   # ASCII letters fold, as bytes.lower() of the data does

    def hit(data):
        return any(s in data for s in needles)

    for d, dirs, names in os.walk(root):
        dirs[:] = [x for x in dirs if x != ".git"]
        for n in dirs:   # a folder's name, an empty one or a link to one too
            path = os.path.join(d, n)
            rel = _rel(root, path)
            if hit(os.fsencode(rel).lower()):
                bad.append(f"{rel}/: its path carries a forbidden string")
            if os.path.islink(path) and hit(os.fsencode(os.readlink(path)).lower()):
                bad.append(f"{rel}/: its link carries a forbidden string")
        for n in names:
            path = os.path.join(d, n)
            rel = _rel(root, path)
            if any(fnmatch.fnmatchcase(rel, p) for p in cut):
                bad.append(f"{rel}: cut from the open flavor")
                continue
            if os.path.islink(path):
                if not needles:
                    continue
                data = os.fsencode(os.readlink(path))
            else:
                mac = ".app/" in rel or rel.endswith((".dmg", ".pkg"))
                with open(path, "rb") as f:
                    if not needles and not mac and not macho(f.read(8)):
                        continue
                    f.seek(0)
                    data = f.read()
                # a certificate of a Mac signature (an ad-hoc one carries none): a bundle's file, a Mach-O, an image
                if SIGNED in data and (mac or macho(data[:8])):
                    bad.append(f"{rel}: signed with a Developer ID (the open flavor's are signed ad hoc)")
                if not needles:
                    continue
                if spare is not None:
                    got = spare(rel, data)
                    data = data if got is None else got
            if hit(data.lower()):
                bad.append(f"{rel}: carries a forbidden string")
            if hit(os.fsencode(rel).lower()):
                bad.append(f"{rel}: its path carries a forbidden string")
    return sorted(bad)
