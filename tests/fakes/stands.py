"""A cache of built test stands between the processes of one tools/test.py run.

Tests of the app build a release in every setUp (git init, commits, a tag, tools/package/layer.py): about 28 processes, a
second on Linux and 17-25 s on Windows, and the suites have hundreds of such tests. `cached()` builds a stand once - under
a file lock, the key is a hash of what goes into the build - and gives every other test a copy in its own folder.

The folder of the cache is HUB_TEST_STANDS: tools/test.py makes it for the run and removes it with the run; without it
(a test started by hand) `cached()` just builds, as before. An entry is written beside, renamed into place whole and
never changed after; a copy is checked against the digest taken at the build, a damaged entry is built again.

What the copy does not carry by itself is the folder's own path: git writes it into the `config` of a clone (a remote's
address), so the files called `config` are rewritten to the new folder. A test that changes what goes into the build
passes it in the key; one that needs its own build does not call `cached()`."""
import contextlib
import hashlib
import json
import os
import tempfile
import time

import platforms
from core import appcode, lock

ENV = "HUB_TEST_STANDS"
META = "meta.json"
TREE = "tree"
REWRITE = ("config",)      # the files that carry the folder's path (git: the address of a remote)


def enabled():
    base = os.environ.get(ENV)
    return base if base and os.path.isdir(base) else None


def key_of(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=repr).encode()).hexdigest()[:20]


def source_of(*paths):
    """A hash of the files' text - the sources that go into a build (the factory, the launcher) belong to its key."""
    h = hashlib.sha256()
    for p in paths:
        with open(p, "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:16]


def _walk(top):
    """(relative path with '/', is a folder, absolute path), folders before what they hold, in a fixed order."""
    for d, dirs, names in os.walk(top):
        dirs.sort()
        rel = os.path.relpath(d, top)
        for n in sorted(dirs) + sorted(names):
            p = os.path.join(d, n)
            yield (n if rel == "." else rel.replace(os.sep, "/") + "/" + n), os.path.isdir(p) and not os.path.islink(p), p


def _copy(src, dst, names, modes=None):
    """Copy `names` from src to dst, as plain files; -> (a digest of names, paths and bytes, the modes and times of what
    was copied: {path: [mode, mtime_ns]}). Into the cache the files go with plain modes (the entry is removable like any
    folder) and the real ones are kept aside; out of it `modes` give them back. A link is not copied: nothing in the
    stands has one, and one that appears should be seen."""
    h = hashlib.sha256()
    seen = {}
    for name in names:
        top = os.path.join(src, name)
        if not os.path.isdir(top):
            raise FileNotFoundError(f"the stand has no {name} folder")
        out = os.path.join(dst, name)
        os.makedirs(out, exist_ok=True)
        seen[name] = [os.stat(top).st_mode & 0o7777, os.stat(top).st_mtime_ns]   # portable: a time kept as it is, not compared
        h.update(f"d {name}\n".encode())
        for rel, is_dir, p in _walk(top):
            full = f"{name}/{rel}"
            target = os.path.join(out, *rel.split("/"))
            if os.path.islink(p):
                raise OSError(f"{p}: a link in a stand")
            st = os.stat(p)
            seen[full] = [st.st_mode & 0o7777, st.st_mtime_ns]   # portable: a time kept as it is, not compared
            if is_dir:
                os.makedirs(target, exist_ok=True)
                h.update(f"d {full}\n".encode())
                continue
            with open(p, "rb") as f:
                data = f.read()
            h.update(f"f {full} {len(data)} {hashlib.sha256(data).hexdigest()}\n".encode())
            with open(target, "wb") as f:
                f.write(data)
            if modes is None:
                os.chmod(target, 0o700 if st.st_mode & 0o100 else 0o600)
    if modes is not None:   # out of the cache: the modes and times, files first and folders after, the innermost first
        for full in sorted(modes, key=lambda n: (os.path.isdir(os.path.join(dst, *n.split("/"))), -n.count("/"))):
            path = os.path.join(dst, *full.split("/"))
            if os.path.lexists(path):
                os.utime(path, ns=(modes[full][1], modes[full][1]))
                os.chmod(path, modes[full][0])
    return h.hexdigest(), seen


def _forms(path):
    """The ways a folder's path is written into a git config: as it is, with doubled backslashes, with slashes."""
    return [path, path.replace("\\", "\\\\"), path.replace("\\", "/")]


def _relocate(root, names, old, new):
    """Rewrite the folder's path in the `config` files of root/<names>. -> False if some still carry the old one (a path git
    wrote in another spelling than the temp folder gave: 8.3 names, the case of a drive letter) - such a copy would
    push into the folder of the test that built the stand."""
    if old == new:
        return True
    pairs = list(zip(_forms(old), _forms(new)))
    left = [form.lower() for form in _forms(old)]
    for name in names:
        for rel, is_dir, p in _walk(os.path.join(root, name)):
            if is_dir or os.path.basename(p) not in REWRITE:
                continue
            with open(p, "rb") as f:
                data = f.read()
            text = data.decode("utf-8", "surrogateescape")
            for a, b in pairs:
                text = text.replace(a, b)
            if any(form in text.lower() for form in left):
                return False
            changed = text.encode("utf-8", "surrogateescape")
            if changed != data:
                mode = os.stat(p).st_mode
                os.chmod(p, 0o600)
                with open(p, "wb") as f:
                    f.write(changed)
                os.chmod(p, mode & 0o7777)
    return True


def _entry(base, key):
    return os.path.join(base, key)


def _meta(entry, key):
    try:
        with open(os.path.join(entry, META), encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) and meta.get("key") == key else None


def _remove(path):
    """A folder away, as far as it goes: read-only files and folders, long paths; an error is not the test's."""
    for remover in (appcode.remove_tree, platforms.current.rmtree):
        if not os.path.lexists(path):
            return
        try:
            remover(path)
        except OSError:
            pass


HIT, MISS, DAMAGED, FAILED = "hit", "miss", "damaged", "failed"


def _restore(base, key, root, names):
    """(HIT, data) - the stand is copied into root; MISS - no entry; DAMAGED - the entry does not match what it says (or
    its digest); FAILED - the copy could not be made here (the entry may be fine). The copy is made beside and put in
    place whole, so a failure leaves root as it was: a step builds on the folders of the steps before it."""
    entry = _entry(base, key)
    meta = _meta(entry, key)
    if meta is None:
        return (DAMAGED if os.path.lexists(entry) else MISS), None
    if meta.get("names") != list(names):
        return DAMAGED, None
    staging = None
    try:
        staging = tempfile.mkdtemp(prefix=".stand-", dir=root)
        try:
            digest, _ = _copy(os.path.join(entry, TREE), staging, names, meta["modes"])
        except FileNotFoundError:
            return DAMAGED, None   # the tree is cut short
        if digest != meta.get("digest"):
            return DAMAGED, None
        if not _relocate(staging, names, meta["root"], root):
            return FAILED, None
        for name in names:
            _remove(os.path.join(root, name))
            os.replace(os.path.join(staging, name), os.path.join(root, name))
        return HIT, meta["data"]
    except (OSError, KeyError, TypeError):
        return FAILED, None
    finally:
        if staging:
            _remove(staging)


def _store(base, key, root, names, data):
    """The entry of the stand in the cache; best effort - a cache that cannot be written (full, read-only, a stand with a
    link in it) is a cache that is not there, not a failed test. The entry appears whole or not at all. A key that could
    not be kept is marked, so that the others do not try it again."""
    entry = _entry(base, key)
    part = f"{entry}.part-{os.getpid()}"
    try:
        _remove(part)
        os.makedirs(os.path.join(part, TREE))
        digest, modes = _copy(root, os.path.join(part, TREE), names)
        with open(os.path.join(part, META), "w", encoding="utf-8") as f:
            json.dump({"key": key, "names": names, "root": root, "digest": digest, "modes": modes, "data": data}, f)
        if _publish(part, entry):
            return
    except (OSError, TypeError, ValueError):
        pass
    _remove(part)
    try:
        open(entry + ".nocache", "w").close()
    except OSError:
        pass


def _publish(part, entry):
    """On Windows a scanner may hold a file of the new folder for a moment and the rename is refused: a few tries."""
    for attempt in range(5):
        try:
            os.replace(part, entry)
            return True
        except OSError:
            time.sleep(0.2)
    return False


def cached(parts, root, names, build):
    """Fill the folders `names` of root with the stand that `parts` name: build() does it (and returns the stand's data - a
    dict of strings, numbers, lists: the sheet of a layer, the tags - for the others to get back, the same whether this
    test built the stand or took it), or a copy of an earlier build does. -> that data. `parts` is everything the build
    depends on (arguments, the text of the sources it runs). Whatever goes wrong with the cache, the stand is built."""
    names = list(names)
    base = enabled()
    key = key_of(parts, names) if base else None
    if base is not None and os.path.lexists(_entry(base, key) + ".nocache"):
        base = None
    if base is None:
        return _plain(build())
    status, data = _restore(base, key, root, names)
    if status == HIT:
        return data
    with contextlib.ExitStack() as held:
        try:
            held.enter_context(lock.exclusive(os.path.join(base, key + ".lock")))
        except OSError:
            return _plain(build())   # no lock - no cache
        status, data = _restore(base, key, root, names)   # another process built it while this one waited
        if status == HIT:
            return data
        data = _plain(build())
        if status == FAILED:
            return data   # the entry may be fine; this test just could not take it
        missing = [n for n in names if not os.path.isdir(os.path.join(root, n))]
        if missing:
            raise FileNotFoundError(f"the stand has no {', '.join(missing)}")
        if status == DAMAGED:
            _remove(_entry(base, key))
        _store(base, key, root, names, data)
        return data


def _plain(data):
    """What a stand's data is after the cache: JSON, as read back."""
    return json.loads(json.dumps(data or {}))


class Chain:
    """The steps of one test's stand: each is keyed by the one before it, so a stand that took the same steps comes from
    the same entry. What the test does to the folders between the steps, outside them, `off()` says: the steps after
    it are built, none is taken from the cache or given to it."""

    def __init__(self, root, *seed):
        self.root = root
        self.key = key_of(seed)

    def step(self, parts, names, build):
        if self.key is None:
            return _plain(build())
        data = cached([self.key, parts], self.root, names, build)
        self.key = key_of(self.key, parts, list(names))
        return data

    def off(self):
        self.key = None
