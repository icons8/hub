# --- unpack.py (the same in install.sh and install.ps1) ---
"""unpack|verify <archive> <part folder> [<final folder>]: unpack the archive into the part folder (Windows: through
the long-path prefix, without which tar.exe and Python lose files) or take what tar unpacked there; check that
every member of the archive is there as itself (a folder, a file of its size, a link to its target), one top folder of
the archive dropped; write the sheet of the tree (SHEET: every file's size and sha256, every link's target) and the
mark (HUB_SHELL_MARK, JSON) with the count; rename the part to the final folder when given.
check <folder>: the folder against its sheet: every file of it there with its size and sum, every link to its target
(what the environment adds itself, such as the bytecode (its Python's too: own_bytecode) and Windows' links, is not in
the sheet and not looked at).
Prints "ok <files>" or the reason, exits 0 or 1."""
import hashlib
import json
import os
import stat
import sys
import tarfile

WIN = os.name == "nt"
SHEET = ".agents-hub-shell.files"


def long(path):
    path = os.path.abspath(path)
    return "\\\\?\\" + path if WIN and not path.startswith("\\\\?\\") else path


def rel_of(name):
    parts = name.replace("\\", "/").split("/")
    while len(parts) > 1 and parts[0] == ".":
        parts = parts[1:]
    if parts[0] in ("", ".", ".."):
        return None, parts[0]
    rest = [p for p in parts[1:] if p not in ("", ".")]
    if not rest:
        return "", parts[0]
    if any(p == ".." or ":" in p for p in rest):
        raise ValueError("a member reaches outside its folder: " + name)
    return "/".join(rest), parts[0]


def inside(rel, target):
    base = os.path.dirname(rel)
    joined = os.path.normpath(os.path.join(base, target)).replace("\\", "/")
    return not (target.startswith(("/", "\\")) or joined == ".." or joined.startswith("../"))


def own_bytecode(rel):
    """The bytecode of the environment's Python: the Python comes with .pyc older than their sources (a build the
    factory's machine cannot start keeps them) and the environment's installer compiles them again (install.py
    prepare). Not written into the sheet; one an earlier sheet holds is not looked at."""
    return rel.startswith("runtime/python/") and rel.endswith(".pyc")


def place(root, rel):
    return os.path.join(root, *rel.split("/")) if rel else root


def copy(src, path, umask, mode):
    """src's bytes into a new file at path -> their sha256."""
    h = hashlib.sha256()
    with open(path, "wb") as out:
        while True:
            block = src.read(1 << 20)
            if not block:
                break
            h.update(block)
            out.write(block)
    os.chmod(path, (mode & 0o777) & ~umask)
    return h.hexdigest()


def sum_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(1 << 20)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def main(argv):
    mode, archive, part = argv[1], argv[2], argv[3]
    final = argv[4] if len(argv) > 4 else None
    root = long(part)
    top = None
    files = 0
    sizes = {}
    sheet = {"files": {}, "links": {}}
    umask = os.umask(0o077)
    os.umask(umask)
    with tarfile.open(archive, "r:gz") as t:
        for m in t:
            rel, first = rel_of(m.name)
            if rel is None:
                return "a member outside the archive's top folder: " + m.name
            if top is None:
                top = first
            elif first != top:
                return "the archive has more than one top folder: " + first
            if rel == "":
                continue
            target = place(root, rel)
            got = None
            if mode == "unpack":
                if m.isdir():
                    os.makedirs(target, exist_ok=True)
                elif m.isreg():
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    got = copy(t.extractfile(m), target, umask, m.mode)
                elif m.issym() and not WIN:
                    if not inside(rel, m.linkname):
                        return "a link reaches outside its folder: " + m.name
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    os.symlink(m.linkname, target)
                elif m.islnk():
                    src_rel, _ = rel_of(m.linkname)
                    if not src_rel:
                        return "a hard link outside the archive's folder: " + m.name
                    with open(place(root, src_rel), "rb") as a:
                        got = copy(a, target, umask, m.mode)
                else:
                    return "a member of a kind the environment never has: " + m.name
            try:
                st = os.lstat(target)
            except OSError:
                return "missing after unpacking: " + rel
            if m.isdir():
                ok = stat.S_ISDIR(st.st_mode)
            elif m.isreg():
                ok, sizes[rel] = stat.S_ISREG(st.st_mode) and st.st_size == m.size, m.size
                files += 1
            elif m.issym():
                ok = stat.S_ISLNK(st.st_mode) and os.readlink(target) == m.linkname and inside(rel, m.linkname)
                sheet["links"][rel] = m.linkname
                files += 1
            elif m.islnk():
                src_rel, _ = rel_of(m.linkname)
                ok = stat.S_ISREG(st.st_mode) and st.st_size == sizes.get(src_rel, -1)
                files += 1
            else:
                ok = False
            if not ok:
                return "not as in the archive: " + rel
            if (m.isreg() or m.islnk()) and not own_bytecode(rel):
                sheet["files"][rel] = [st.st_size, got or sum_of(target)]
    if top is None:
        return "the archive is empty"
    with open(os.path.join(root, SHEET), "w", encoding="utf-8") as f:
        json.dump(sheet, f, separators=(",", ":"), sort_keys=True)
    mark = json.loads(os.environ.get("HUB_SHELL_MARK") or "{}")
    mark.update(files=files, top=top)
    with open(os.path.join(root, ".agents-hub-shell.json"), "w", encoding="utf-8") as f:
        json.dump(mark, f, indent=1, sort_keys=True)
    if final:
        os.replace(root, long(final))
    print("ok", files)
    return None


def check(folder):
    root = long(folder)
    try:
        with open(os.path.join(root, SHEET), encoding="utf-8") as f:
            sheet = json.load(f)
        items, links = sheet["files"], sheet["links"]
        if not (isinstance(items, dict) and isinstance(links, dict)):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        return "no sheet of the environment's files (" + SHEET + ")"
    for rel, target in links.items():
        path = place(root, rel)
        if not os.path.islink(path) or os.readlink(path) != target:
            return "not as unpacked: " + rel
    items = {rel: entry for rel, entry in items.items() if not own_bytecode(rel)}
    for rel, entry in items.items():
        path = place(root, rel)
        try:
            st = os.lstat(path)
        except OSError:
            return "missing: " + rel
        if not stat.S_ISREG(st.st_mode) or st.st_size != entry[0] or sum_of(path) != entry[1]:
            return "not as unpacked: " + rel
    print("ok", len(items) + len(links))
    return None


if __name__ == "__main__":
    try:
        why = check(sys.argv[2]) if sys.argv[1:2] == ["check"] else main(sys.argv)
    except (OSError, ValueError, tarfile.TarError, EOFError, IndexError) as e:
        why = f"{type(e).__name__}: {e}"
    if why:
        print(why)
        sys.exit(1)
# --- end of unpack.py ---
