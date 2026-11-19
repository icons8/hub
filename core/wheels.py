"""Wheels the hub pins (tools/package/versions.json, its "wheel" parts — the hub bus's cryptography and what it loads):
whether one runs on an environment's Python and platform, and putting it into a folder of sys.path as `pip --target`
would. The release factory (tools/package/build.py) puts them into the environment's site-packages; the core
(core/busd.heal_lib) puts the same pinned bytes into <state>/pylib of a hub whose environment came without them. The
stdlib only and no imports of the hub: the factory runs it on any machine."""
import json
import os
import re
import shutil
import stat
import zipfile

# the platform tags of a wheel that runs in the environment of each build (python-build-standalone): manylinux —
# glibc, macosx — the macOS of its build; the floors are those the one-line install admits (deploy/oneline/install.sh:
# glibc 2.28, macOS 13.5)
PLATFORM = {"linux-x64": r"manylinux\w*_x86_64", "linux-arm64": r"manylinux\w*_aarch64",
            "mac-arm64": r"macosx_\d+_\d+_(arm64|universal2)", "mac-x64": r"macosx_\d+_\d+_(x86_64|universal2)",
            "windows-x64": r"win_amd64", "windows-arm64": r"win_arm64"}
GLIBC = (2, 28)
MACOS = (13, 5)
LEGACY = {"manylinux1": (2, 5), "manylinux2010": (2, 12), "manylinux2014": (2, 17)}


class Bad(Exception):
    """A wheel that is not put in: what is wrong, for a person."""


def _floor(tag):
    """(the OS floor a platform tag needs, the floor the install admits) — or None (a tag without one)."""
    m = re.match(r"manylinux_(\d+)_(\d+)_", tag)
    if m or tag.split("_")[0] in LEGACY:
        return (int(m.group(1)), int(m.group(2))) if m else LEGACY[tag.split("_")[0]], GLIBC
    m = re.match(r"macosx_(\d+)_(\d+)_", tag)
    return ((int(m.group(1)), int(m.group(2))), MACOS) if m else None


def fits(name, plat, minor):
    """None — the wheel file runs on the platform's CPython <minor> ("3.14"; not free-threaded) on any machine the
    install admits; else why not. By its name's tags (PEP 427/425): python — py3/py3N with N <= the minor, or cpXY with
    abi3 and XY <= the minor, or cp3N itself; platform — PLATFORM's, not above the OS floors, or any for a pure one."""
    m = re.fullmatch(r"([A-Za-z0-9_.]+)-([^-]+)(?:-\d[^-]*)?-([^-]+)-([^-]+)-([^-]+)\.whl", name)
    if not m:
        return f"{name}: not a wheel's name"
    py, abi, tags = m.group(3).split("."), m.group(4).split("."), m.group(5).split(".")
    here = int(minor.split(".")[1])
    cp = [int(t[3:]) for t in py if re.fullmatch(r"cp3\d+", t)]
    pure = "none" in abi and any(t == "py3" or (re.fullmatch(r"py3\d+", t) and int(t[3:]) <= here) for t in py)
    stable = "abi3" in abi and any(n <= here for n in cp)
    own = f"cp3{here}" in abi and here in cp
    if not (pure or stable or own):
        return f"{name}: not for CPython {minor} (python {m.group(3)}, ABI {m.group(4)})"
    ok = [t for t in tags if t == "any" or re.fullmatch(PLATFORM[plat], t)]
    if not ok:
        return f"{name}: not for {plat} (platform {m.group(5)})"
    if all(_floor(t) and _floor(t)[0] > _floor(t)[1] for t in ok):
        return f"{name}: needs a newer OS than the install admits ({m.group(5)})"
    return None


def version_of(text):
    return tuple(int(p) for p in re.findall(r"\d+", str(text))[:2])


def install(path, dest, version):
    """The wheel at path into the folder dest, as pip --target puts it, its METADATA naming version: -> (bytes, files,
    its .dist-info folder). Data folders (scripts, headers), links and paths outside are refused before anything is
    written."""
    name = os.path.basename(path)
    try:
        z = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as e:
        raise Bad(f"{name}: not a wheel ({e})") from None
    with z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        for info in infos:
            n = info.filename
            parts = n.split("/")
            if n.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", n) or ".." in parts or "\\" in n \
                    or stat.S_ISLNK(info.external_attr >> 16):
                raise Bad(f"{name}: {n!r} — a link or a path outside the wheel")
            if parts[0].endswith(".data"):
                raise Bad(f"{name}: {n!r} — a wheel with data folders is not put in")
        dist = {i.filename.split("/")[0] for i in infos if i.filename.split("/")[0].endswith(".dist-info")}
        if len(dist) != 1:
            raise Bad(f"{name}: {len(dist)} .dist-info folders")
        dist = dist.pop()
        try:
            meta = z.read(f"{dist}/METADATA").decode("utf-8", "replace")
        except KeyError:
            raise Bad(f"{name}: no METADATA") from None
        got = re.search(r"(?m)^Version:\s*(\S+)", meta)
        if not got or got.group(1) != version:
            raise Bad(f"{name}: its METADATA says {got.group(1) if got else 'no version'}, the pin {version}")
        size = 0
        for info in infos:
            z.extract(info, dest)
            size += info.file_size
    return size, len(infos), dist


def pinned(versions_path, plat, minor):
    """[{key, url, sha256, size, name, version}] — the wheel parts versions.json pins for the platform, each fitting
    CPython <minor>; [] — none (Windows on ARM: no cryptography wheel), another Python, no readable file, or a pin that
    is malformed or does not fit: the set goes whole or not at all (cryptography without cffi does not load)."""
    try:
        with open(versions_path, encoding="utf-8") as f:
            versions = json.load(f)
    except (OSError, ValueError):
        return []
    py = versions.get("python") if isinstance(versions, dict) else None
    if not isinstance(py, dict) or ".".join(str(py.get("version", "")).split(".")[:2]) != minor:
        return []
    out = []
    for key, item in versions.items():
        if not isinstance(item, dict) or not item.get("wheel"):
            continue
        pin = (item.get("files") or {}).get(plat)
        if not isinstance(pin, dict):
            continue
        name = os.path.basename(str(pin.get("url", "")).split("?")[0])
        if not (str(pin.get("url", "")).startswith("https://") and re.fullmatch(r"[0-9a-f]{64}", str(pin.get("sha256")))
                and type(pin.get("size")) is int and pin["size"] > 0) or fits(name, plat, minor):
            return []
        out.append({"key": key, "url": pin["url"], "sha256": pin["sha256"], "size": pin["size"], "name": name,
                    "version": pin.get("version") or item.get("version")})
    return out


def replace_tree(new, dest, rmtree=shutil.rmtree):
    """The folder new becomes dest: the old one aside first, then the new in its place, then the old goes."""
    old = dest + ".old"
    if os.path.lexists(old):
        rmtree(old)
    if os.path.lexists(dest):
        os.replace(dest, old)
    os.replace(new, dest)
    if os.path.lexists(old):
        rmtree(old)
