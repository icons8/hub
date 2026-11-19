"""The release factory (installer spec S4): everything one platform's app shell (S5–S7: .dmg, Setup.exe, .deb) is made of.

  python3 tools/package/build.py <platform> --out <dir> --repo <path> --tag vX.Y.Z [--flavor team|open]
        [--forbid STRING …] [--issues <account>/<repo>] [--download URL] [--cache DIR] [--runtime-only] [--skip ffmpeg,mcp]
        [--publisher NAME]

Platforms: linux-x64, linux-arm64, mac-arm64, mac-x64, windows-x64, windows-arm64. Any platform is assembled on any
machine (the parts are archives of ready builds), except ffmpeg for a Mac — it is built from the pinned source with
Apple's toolchain, so mac-arm64 and mac-x64 are built on a Mac (arm64; the x86_64 one cross-compiled there).
Result:

  <out>/agents-hub-<tag>-<platform>/
      runtime/     python, node, git (Mac, Windows; Linux takes the system git), gh, ffmpeg, mcp (chrome-devtools-mcp);
                   the "wheel" parts (the hub bus's cryptography, cffi, pycparser) in python's own site-packages, each
                   checked by its tags against the platform and the Python, by its METADATA against its pin
      code-seed/   the code layer of the tag (tools/package/layer.py: the tree without tests, its bytecode by the
                   environment's Python, MANIFEST.json): launch.py installs it into the state folder at the first start
                   and takes the released files from it to put an edited layer back
      PACKAGED     {"format": "app", "layout": "layer", "flavor", "version", "runtime", "platform", "download",
                   "issues", "publisher"?} — core/packaged.py; "publisher" (--publisher, Windows): the organization
                   (O=) of the signature this app's installers carry (tools/package/sign.py) — the app puts a newer
                   one in place only when it is validly signed by it (shellswap.py, app spec §4.4)
      deliver.json {"url", "key", "report"?} of the distribution server (team flavor, --deliver-url/--deliver-key): the
                   launcher hands it to the update (layers/deliver.json); the key reads the releases only; "report" —
                   the Sentry DSN reports of failed updates go to (--report-dsn, a file; modules/update/report.py)
      launch.py    what the shell runs before hub-up at every start (the layer, its check, a trial after a reboot)
      appcode.py   core/appcode.py, the shell's own copy: launch.py checks and restores the layer with it
      shellswap.py the replacement of the app by a newer one and its watchdog (app spec §4.4)
      install.py   the one-line install's hand-over (deploy/oneline/README.md, contract 1): the environment's own steps,
                   then deploy/oneline/setup.py of the newest code
      start.py     what the one-line install's service runs at every start (launch.py, then hub-up)
      paths.json   the programs of the environment relative to this folder (python, node, git, gh, ffmpeg, mcp, launch)
      licenses/    VERSIONS.json (what went in: version, license, address, sha256, sources) and the license files
  <out>/agents-hub-<tag>-<platform>.tar.gz   the same folder as one archive (the shells' input, a release asset;
                                             not with --runtime-only)
  <out>/agents-hub-<tag>-<platform>.json     the sheet: parts, sizes (download, on disk, files), the archive's sha256,
                                             paths to the programs for the shell
  <out>/code-<version>-<flavor>.tar.gz (.json)  the layer alone, the same for every platform (what an update downloads)

The open flavor (--flavor open) is named agents-hub-<tag>-open-<platform>: its layer leaves out the built-in four and
carries none of the --forbid strings nor those of .open-deny in the main copy (core/appcode.open_guard, layer.deny_of).

Every download is checked against versions.json (sha256 and size) before it is used; an archive that reaches outside
its folder, and a link in a Windows build, stop the build. Chrome for Testing is not in the package ("bundle": false,
owner 2026-11-01): its pins go into licenses/VERSIONS.json for the first start. --runtime-only — the environment
without the seed and PACKAGED (the older .deb takes its code from git archive). --skip — for trying the factory
without a part; the sheet names what is missing, a release is not built that way. The stdlib only."""
import argparse
import datetime
import fnmatch
import hashlib
import json
import os
import platform as _platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fetch  # noqa: E402
import layer  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))   # the repo: core/wheels.py, one reading of a wheel
from core import wheels  # noqa: E402

PLATFORMS = {"linux-x64": ("linux", "x86_64"), "linux-arm64": ("linux", "aarch64"),
             "mac-arm64": ("darwin", "arm64"), "mac-x64": ("darwin", "x86_64"),
             "windows-x64": ("win32", "amd64"), "windows-arm64": ("win32", "arm64")}
# what the shell and the hub run, relative to the platform folder (only what is there goes into the sheet)
PATHS = {
    "posix": {"python": "runtime/python/bin/python3", "node": "runtime/node/bin/node", "git": "runtime/git/bin/git",
              "gh": "runtime/gh/bin/gh", "ffmpeg": "runtime/ffmpeg/bin/ffmpeg", "ffprobe": "runtime/ffmpeg/bin/ffprobe"},
    "windows": {"python": "runtime/python/python.exe", "pythonw": "runtime/python/pythonw.exe",
                "node": "runtime/node/node.exe", "git": "runtime/git/cmd/git.exe",
                "bash": "runtime/git/bin/bash.exe", "gh": "runtime/gh/bin/gh.exe",
                "ffmpeg": "runtime/ffmpeg/bin/ffmpeg.exe", "ffprobe": "runtime/ffmpeg/bin/ffprobe.exe"},
}
REPO = os.path.dirname(os.path.dirname(HERE))
# the Windows shell (tools/package/windows): app.py — the install step, the Start menu, the uninstaller; the icon is the
# Mac one's drawing
WINDOWS = os.path.join(HERE, "windows")
WINDOWS_SHELL = ("app.py",)
# (the team's bundle or the open flavor's, where it lies or where the cut put it — the same drawing)
ICNS = next((p for p in (os.path.join(REPO, *b.split("/"), "Contents", "Resources", "logo.icns")
                         for b in ("deploy/mac/Agents Hub.app",
                                   *(x for pair in layer.appcode.OPEN_SWAP for x in reversed(pair))))
             if os.path.isfile(p)), os.path.join(REPO, "deploy", "mac", "Agents Hub.app", "Contents", "Resources", "logo.icns"))
# S0-8: without core.longpaths git fails on paths over 260 characters (worktree add, add) — in the system config of the
# app's own git, so the person's ~/.gitconfig and another git on the machine stay as they are
LONGPATHS = "\n[core]\n\tlongpaths = true\n"
LICENSES = ("LICENSE*", "COPYING*", "NOTICE*", "ThirdPartyNotices*")
# every program of a build is checked by its header against the platform (the sheet's "arch"), on any machine: the
# format of its OS and its processor — a part pinned for the wrong platform starts nowhere on the person's machine
ARCH = {"linux-x64": ("elf", "x86_64"), "linux-arm64": ("elf", "arm64"), "mac-arm64": ("macho", "arm64"),
        "mac-x64": ("macho", "x86_64"), "windows-x64": ("pe", "x86_64"), "windows-arm64": ("pe", "arm64")}
ELF_MACHINE = {0x3E: "x86_64", 0xB7: "arm64", 0x03: "x86", 0x28: "arm"}
MACHO_CPU = {0x01000007: "x86_64", 0x0100000C: "arm64", 0x07: "x86", 0x0C: "arm"}
PE_MACHINE = {0x8664: "x86_64", 0xAA64: "arm64", 0xA641: "arm64", 0xA64E: "arm64", 0x14C: "x86", 0x1C4: "arm"}
# programs of another processor a build carries knowingly — counted in the sheet, not refused:
# - Git for Windows on ARM64: its msys half (bash, coreutils, perl — runtime/git/usr) exists only for x64 and runs under
#   Windows 11's emulation (Windows 10 on ARM has none — the install script refuses it); its getprocaddr32.exe is x86
#   on both builds (it looks into 32-bit processes);
# - python-build-standalone for Windows on ARM64 ships the x64 vcruntime140_1.dll, which nothing of ARM64 loads
EMULATED = {"windows-x64": (("runtime/git/usr/libexec/getprocaddr32.exe", ("x86",)),),
            "windows-arm64": (("runtime/git/usr/", ("x86_64", "x86")),
                              ("runtime/python/vcruntime140_1.dll", ("x86_64",)))}
# files with a program's header that are data where they lie: pip's script launchers for Windows (templates it copies
# into a venv's Scripts on Windows, one per processor); .NET assemblies (Git Credential Manager) run on its own runtime
TEMPLATES = ("runtime/python/*/site-packages/pip/_vendor/distlib/*.exe",
             "runtime/python/Lib/site-packages/pip/_vendor/distlib/*.exe")


class Refused(Exception):
    """The build stops: what is wrong, for a person."""


def kind(plat):
    return "windows" if plat.startswith("windows") else "posix"


def host():
    machine = _platform.machine().lower()
    for name, (osname, arch) in PLATFORMS.items():
        if sys.platform == osname and machine in (arch, {"x86_64": "amd64", "aarch64": "arm64"}.get(arch, arch)):
            return name
    return None


def rosetta():
    """An arm Mac runs x86_64 programs (Rosetta 2 is installed)."""
    try:
        return subprocess.run(["/usr/bin/arch", "-x86_64", "/usr/bin/true"], capture_output=True,
                              stdin=subprocess.DEVNULL, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def runs_here(plat):
    """This machine starts the platform's programs: its own platform, and an Intel Mac's build on an arm Mac with
    Rosetta (an arm Mac builds mac-x64) — so the smoke run sees what goes to Intel Macs."""
    here = host()
    return here == plat or (plat == "mac-x64" and here == "mac-arm64" and rosetta())


def check_arch(path, configure):
    """The built program is for the configure line's --arch, one slice only (its Mach-O header — on any machine: a
    build brought from a Mac is checked where it is used): a cross build without -arch in its flags gives the build
    machine's architecture quietly."""
    want = next((c.split("=", 1)[1] for c in configure if c.startswith("--arch=")), None)
    if not want:
        return
    got = arch_of(path)
    if got != ("macho", {want}):
        raise Refused(f"{os.path.basename(path)} is built for {' '.join(sorted(got[1])) if got else '?'}, and the "
                      f"platform needs {want}")


def arch_of(path):
    """(format, the processors it is for) of a program file by its header — every slice of a universal Mach-O — or
    None: not a program (a script, data, a library of another kind than the three)."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
            if head[:4] == b"\x7fELF" and len(head) >= 20:
                order = "<" if head[5] == 1 else ">"
                return "elf", {ELF_MACHINE.get(struct.unpack(order + "H", head[18:20])[0], "other")}
            if head[:4] in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe") and len(head) >= 8:
                return "macho", {MACHO_CPU.get(struct.unpack("<I", head[4:8])[0], "other")}
            if head[:4] in (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf") and len(head) >= 8:
                count = struct.unpack(">I", head[4:8])[0]
                if not 0 < count <= 30:   # a Java class file shares the magic: its version is no count of slices
                    return None
                size = 32 if head[3] == 0xBF else 20
                cpus = [struct.unpack(">I", head[8 + i * size:12 + i * size])[0] for i in range(count)
                        if 12 + i * size <= len(head)]
                return "macho", {MACHO_CPU.get(c, "other") for c in cpus}
            if head[:2] == b"MZ" and len(head) >= 64:
                at = struct.unpack("<I", head[60:64])[0]
                f.seek(at)
                pe = f.read(512)
                if pe[:4] == b"PE\0\0" and len(pe) >= 26:
                    if clr(pe):
                        return "clr", {"any"}
                    return "pe", {PE_MACHINE.get(struct.unpack("<H", pe[4:6])[0], "other")}
    except OSError:
        return None
    return None


def clr(pe):
    """A PE file of .NET (its optional header names a CLR runtime header, data directory 14): code of the runtime that
    loads it, whatever its Machine says. pe — the bytes from its "PE\\0\\0" on."""
    opt = 24                                           # the signature (4) and the COFF header (20)
    if len(pe) < opt + 2:
        return False
    count_at = {0x10B: 92, 0x20B: 108}.get(struct.unpack("<H", pe[opt:opt + 2])[0])   # PE32, PE32+
    if count_at is None or len(pe) < opt + count_at + 4:
        return False
    at = opt + count_at + 4 + 14 * 8                   # the directories follow NumberOfRvaAndSizes, 8 bytes each
    return (struct.unpack("<I", pe[opt + count_at:opt + count_at + 4])[0] > 14 and len(pe) >= at + 8
            and struct.unpack("<I", pe[at + 4:at + 8])[0] > 0)


def check_programs(top, plat):
    """Every program file under top is the platform's (its OS's format, its processor — a universal Mach-O carries
    it); the known emulated ones are counted. The sheet's "arch"; a foreign program stops the build."""
    form, want = ARCH[plat]
    count, emulated, foreign, data = 0, {}, [], 0
    for root, dirs, files in os.walk(top):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(root, name)
            if os.path.islink(path):
                continue
            got = arch_of(path)
            if got is None:
                continue
            rel = os.path.relpath(path, top).replace(os.sep, "/")
            if got[0] == "clr" or any(fnmatch.fnmatchcase(rel, p) for p in TEMPLATES):
                data += 1
                continue
            count += 1
            if got[0] == form and want in got[1] and (form == "macho" or len(got[1]) == 1):
                continue
            where = next((p for p, archs in EMULATED.get(plat, ()) if rel.startswith(p)
                          and got[0] == form and len(got[1]) == 1 and next(iter(got[1])) in archs), None)
            if where:
                emulated[where] = emulated.get(where, 0) + 1
                continue
            foreign.append(f"{rel} ({got[0]} {','.join(sorted(got[1]))})")
    if foreign:
        raise Refused(f"{len(foreign)} program(s) of another platform than {plat} ({form} {want}): "
                      f"{'; '.join(foreign[:8])}")
    return {"format": form, "want": want, "programs": count, "emulated": emulated, "data": data}


def made_here(versions, plat):
    """Parts built from the source for this platform (ffmpeg on a Mac)."""
    return [k for k, item in versions.items() if isinstance(item, dict)
            and ((item.get("build") or {}).get(plat)) and not (item.get("files") or {}).get(plat)]


def download_of(repo):
    """Where a newer app is: the releases page of a GitHub repo (core/packaged takes only https)."""
    m = re.match(r"^https://github\.com/([\w.-]+/[\w.-]+?)(?:\.git)?/?$", repo or "")
    return f"https://github.com/{m.group(1)}/releases/latest" if m else None


def run(cmd, cwd=None, env=None, timeout=1800):
    try:
        r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except OSError as e:   # a missing program, or one this machine cannot run (x86_64 without Rosetta: "Bad CPU type")
        raise Refused(f"{os.path.basename(cmd[0])} cannot start here: {e.strerror or e}") from None
    if r.returncode:
        raise Refused(f"{os.path.basename(cmd[0])} {' '.join(cmd[1:3])}: {(r.stderr or r.stdout).strip()[-600:]}")
    return r.stdout.strip()


def tree(path):
    """(bytes, files) of the regular files below path."""
    size = files = 0
    for root, _, names in os.walk(path):
        for n in names:
            st = os.lstat(os.path.join(root, n))
            if stat.S_ISREG(st.st_mode):
                size += st.st_size
                files += 1
    return size, files


def _safe(name):
    parts = name.replace("\\", "/").split("/")
    return not (name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name) or ".." in parts)


def for_platform(value, plat):
    """keep/drop/msys of versions.json: a list for every platform, or {platform: list}."""
    return value.get(plat) if isinstance(value, dict) else value


def unpack(archive, dest, keep=None, drop=None, msys=None):
    """An archive into dest: one top folder is dropped; keep — globs of what stays (relative, '/'); drop — globs of
    members not unpacked at all (as named in the archive, without a leading './'); msys — the same, but the links and
    folders among them are recorded: {relative name: link target, or None for a folder}, returned (Git for Windows: msys
    links to /proc that the Windows shell makes again as msys's own link files, without rights)."""
    clean = lambda name: name.replace("\\", "/").removeprefix("./").rstrip("/")   # noqa: E731
    dropped = lambda name, globs: any(fnmatch.fnmatchcase(clean(name), d) for d in globs or ())   # noqa: E731
    recorded = {}

    def record(m):
        if m.issym():
            recorded[clean(m.name)] = m.linkname
        elif m.isdir():
            recorded[clean(m.name)] = None
        return None

    tmp = dest + ".part"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    try:
        if archive.endswith(".zip"):
            with zipfile.ZipFile(archive) as z:
                for info in z.infolist():
                    if dropped(info.filename, drop) or dropped(info.filename, msys):
                        continue
                    mode = info.external_attr >> 16
                    if not _safe(info.filename):
                        raise Refused(f"{os.path.basename(archive)}: {info.filename!r} — a path outside the archive's folder")
                    if stat.S_ISLNK(mode):
                        raise Refused(f"{os.path.basename(archive)}: {info.filename!r} — a link in a zip")
                    path = z.extract(info, tmp)
                    if not info.is_dir() and mode & 0o111:
                        os.chmod(path, 0o755)
        else:
            with tarfile.open(archive) as t:
                try:
                    t.extractall(tmp, filter=lambda m, path: None if dropped(m.name, drop) else record(m) if dropped(
                        m.name, msys) else tarfile.data_filter(m, path))
                except tarfile.FilterError as e:
                    raise Refused(f"{os.path.basename(archive)}: {e}") from None
        top = os.listdir(tmp)
        root = os.path.join(tmp, top[0]) if len(top) == 1 and os.path.isdir(os.path.join(tmp, top[0])) else tmp
        if root != tmp:
            recorded = {n.removeprefix(top[0] + "/"): v for n, v in recorded.items() if n != top[0]}
        if keep:
            for d, dirs, names in os.walk(root, topdown=False):
                for n in names + [x for x in dirs if os.path.islink(os.path.join(d, x))]:
                    rel = os.path.relpath(os.path.join(d, n), root).replace(os.sep, "/")
                    if not any(fnmatch.fnmatchcase(rel, k) for k in keep):
                        os.unlink(os.path.join(d, n))
                if d != root and not os.listdir(d):
                    os.rmdir(d)
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(root, dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return recorded


# a "wheel" part of versions.json (the hub bus's cryptography and what it loads) goes into the environment's Python, not
# into runtime/<part> (core/wheels.py: its tags against the platform and the Python, its METADATA against the pin)
SITE = {"posix": "runtime/python/lib/python{minor}/site-packages", "windows": "runtime/python/Lib/site-packages"}


def install_wheel(path, site, version):
    try:
        return wheels.install(path, site, version)
    except wheels.Bad as e:
        raise Refused(str(e)) from None


def get(part, cache):
    try:
        return fetch.fetch(part, cache)
    except fetch.Mismatch as e:
        raise Refused(str(e)) from None
    except OSError as e:
        raise Refused(f"{part['name']}: not downloaded ({e})") from None


def tool(versions, key, cache):
    """A program of this machine's platform from versions.json (Python to compile, node for npm): unpacked once."""
    plat = host()
    part = next((p for p in fetch.parts(versions, plat or "") if p["key"] == key), None)
    if part is None:
        raise Refused(f"{key} is not pinned for this machine ({sys.platform} {_platform.machine()}) — pass your own")
    place = os.path.join(cache, "tools", f"{key}-{plat}-{part['sha256'][:12]}")
    if not os.path.isdir(place):
        unpack(get(part, cache), place)
    rel = PATHS[kind(plat)][key].split("/", 2)[2]
    return os.path.join(place, rel)


def python_minor(versions):
    return ".".join(versions["python"]["version"].split(".")[:2])


def wheel_version(versions, key, plat):
    """The version of a part on a platform: its pin's own (an older wheel where a newer has none), else the part's."""
    item = versions[key]
    return ((item.get("files") or {}).get(plat) or {}).get("version") or item.get("version")


def ffmpeg_key(spec):
    """The name of a Mac ffmpeg build in the cache: its source and configure line."""
    return "ffmpeg-mac-" + hashlib.sha256((spec["source"]["sha256"] + json.dumps(spec["configure"])).encode()).hexdigest()[:12]


def ffmpeg_mac(spec, cache, dest):
    """Our LGPL ffmpeg for a Mac from the pinned source (S0-4: ≈3 min on an M2 Mac); built once per source and
    configure line, kept in the cache. Built only on a Mac (Apple's toolchain); any machine takes a build already in its
    cache — one a Mac made and the factory brought (tools/package/factory.py mac-parts there, --mac-parts here), checked
    by its headers like any other."""
    src = spec["source"]
    built = os.path.join(cache, ffmpeg_key(spec))
    if not os.path.exists(os.path.join(built, "bin", "ffprobe")):
        if sys.platform != "darwin":
            raise Refused("ffmpeg for a Mac is built from source only on a Mac: there `factory.py mac-parts --out "
                          "DIR`, its DIR copied here, then `factory.py … --mac-parts DIR`")
        part = {"key": "ffmpeg-src", "url": src["url"], "sha256": src["sha256"], "size": src["size"],
                "name": os.path.basename(src["url"])}
        work = tempfile.mkdtemp(prefix="hub-ffmpeg-")
        try:
            unpack(get(part, cache), os.path.join(work, "src"))
            s = os.path.join(work, "src")
            run(["./configure", *spec["configure"]], cwd=s)
            run(["make", f"-j{os.cpu_count() or 4}"], cwd=s, timeout=3600)
            check_arch(os.path.join(s, "ffmpeg"), spec["configure"])   # before running it: a wrong arch may not start
            conf = run(["./ffmpeg", "-hide_banner", "-buildconf"], cwd=s)
            if "--enable-gpl" in conf or "--enable-nonfree" in conf:
                raise Refused("ffmpeg was built with GPL/nonfree — not putting it into the package")
            os.makedirs(os.path.join(work, "out", "bin"))
            for name in ("ffmpeg", "ffprobe"):
                shutil.copy2(os.path.join(s, name), os.path.join(work, "out", "bin", name))
                run(["strip", os.path.join(work, "out", "bin", name)])
            for name in os.listdir(s):
                if name.startswith(("COPYING.LGPL", "LICENSE")):
                    shutil.copy2(os.path.join(s, name), os.path.join(work, "out", name))
            shutil.rmtree(built, ignore_errors=True)
            os.replace(os.path.join(work, "out"), built)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    for name in ("ffmpeg", "ffprobe"):   # a cached build too: it may predate the check
        check_arch(os.path.join(built, "bin", name), spec["configure"])
    shutil.copytree(built, dest, symlinks=True)


def mcp(dest, node, cache, windows):
    """chrome-devtools-mcp from package-lock.json (npm ci checks integrity): plain JavaScript, the same on every OS,
    so any machine's node installs it."""
    os.makedirs(dest)
    for name in ("package.json", "package-lock.json"):
        shutil.copy2(os.path.join(HERE, "mcp", name), dest)
    root = os.path.dirname(node) if node.endswith(".exe") else os.path.dirname(os.path.dirname(node))
    cli = os.path.join(root, "node_modules" if node.endswith(".exe") else "lib/node_modules", "npm", "bin", "npm-cli.js")
    env = {**os.environ, "PATH": os.path.dirname(node) + os.pathsep + os.environ.get("PATH", ""),
           "npm_config_cache": os.path.join(cache, "npm"), "npm_config_update_notifier": "false"}
    run([node, cli, "ci", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund", "--loglevel=error"],
        cwd=dest, env=env)
    if windows:   # links of .bin are this machine's; Windows runs node with the script (the sheet's path)
        shutil.rmtree(os.path.join(dest, "node_modules", ".bin"), ignore_errors=True)
    with open(os.path.join(HERE, "mcp", "package-lock.json")) as f:
        lock = json.load(f)
    script = lock["packages"]["node_modules/chrome-devtools-mcp"]["bin"]["chrome-devtools-mcp"]
    return f"runtime/mcp/node_modules/chrome-devtools-mcp/{script}"


def runtime_min(seed):
    try:
        with open(os.path.join(seed, "core", "version.py")) as f:
            m = re.search(r"(?m)^RUNTIME_MIN\s*=\s*(\d+)", f.read())
    except OSError:
        m = None
    return int(m.group(1)) if m else 0


def clone_url(repo):
    """What git clones: a URL or user@host:path as is, a folder as file:// (--depth needs it)."""
    if re.match(r"^[a-z][a-z0-9+.-]*://", repo) or re.match(r"^[\w.-]+@[\w.-]+:", repo):
        return repo
    return "file://" + os.path.abspath(repo)


def source(repo, tag, work):
    """A local git repository with the tag: the folder itself, or the tag fetched from a URL into work (shallow, bare).
    The tag must be a tag (a branch of the same name is not a release)."""
    if os.path.isdir(repo):
        local = repo
    else:
        local = os.path.join(work, "source.git")
        run(["git", "init", "-q", "--bare", local])
        run(["git", "-C", local, "fetch", "-q", "--depth", "1", clone_url(repo), f"+refs/tags/{tag}:refs/tags/{tag}"],
            timeout=1800)
    tagged = subprocess.run(["git", "-C", local, "rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}"],
                            capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout.strip()
    if not tagged:
        raise Refused(f"{tag} is not a tag in {repo}")
    return local


def licenses(top, versions, plat, used, skipped, code, dist=None):
    """licenses/: the license files of every part and VERSIONS.json — what went in and where its sources are.
    dist — {part: its .dist-info folder}: a wheel's license files are there (licenses/<part>/…), its version may be the
    pin's."""
    dist = dist or {}
    lic = os.path.join(top, "licenses")
    os.makedirs(lic)
    parts = {}
    for key in used:
        item = versions.get(key) or {}
        pin = (item.get("files") or {}).get(plat) or {}
        build = (item.get("build") or {}).get(plat) or {}
        about = {k: item[k] for k in ("version", "note", "license") if item.get(k)}
        if pin.get("version"):   # a platform's own (an older wheel where a newer has none): wheel_version
            about["version"] = wheel_version(versions, key, plat)
        if pin:
            about.update(url=pin["url"], sha256=pin["sha256"], size=pin["size"])
        if build:   # built here: what came out, so that a later build can be compared with this one
            about.update(source=build["source"], configure=build["configure"], built={
                rel: fetch.sha256(os.path.join(top, "runtime", key, rel)) for rel in sorted(
                    os.path.relpath(os.path.join(d, n), os.path.join(top, "runtime", key)).replace(os.sep, "/")
                    for d, _, names in os.walk(os.path.join(top, "runtime", key)) for n in names)})
        src = (item.get("source") or {}).get(plat)
        if src:
            about["source"] = src
        if key == "mcp":
            about = {k: versions["chrome-devtools-mcp"].get(k) for k in ("version", "license", "npm_integrity")}
        parts[key] = about
        where = dist.get(key) or os.path.join(top, "runtime", key)
        for d, dirs, names in os.walk(where):
            if os.path.relpath(d, where).count(os.sep) >= 3:
                dirs[:] = []
            for n in names:
                if any(fnmatch.fnmatch(n, p) for p in LICENSES) and os.path.isfile(os.path.join(d, n)):
                    rel = os.path.relpath(os.path.join(d, n), where)
                    os.makedirs(os.path.join(lic, key, os.path.dirname(rel)), exist_ok=True)
                    shutil.copy2(os.path.join(d, n), os.path.join(lic, key, rel))
    later = {p["key"]: {k: p[k] for k in ("url", "sha256", "size")}
             for p in fetch.parts(versions, plat, bundled=False) if versions[p["key"]].get("bundle") is False}
    with open(os.path.join(lic, "VERSIONS.json"), "w") as f:
        json.dump({"runtime": versions["runtime"], "platform": plat, "code": code, "parts": parts,
                   "first_start": later, "skipped": sorted(skipped),
                   "built": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
                  f, ensure_ascii=False, indent=1)


def links(path):
    return [os.path.relpath(os.path.join(d, n), path) for d, dirs, names in os.walk(path) for n in names + dirs
            if os.path.islink(os.path.join(d, n))]


def hardlinks(top):
    """Windows: one file under several names (Git for Windows: 92 hard links, 70 MB) would be installed as copies — one
    name stays, the others go: {name: the name that stays} (relative to top, '/'), the shell links them again."""
    seen, out = {}, {}
    for d, dirs, names in os.walk(top):
        dirs.sort()
        for n in sorted(names):
            path = os.path.join(d, n)
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink < 2:
                continue
            rel = os.path.relpath(path, top).replace(os.sep, "/")
            if (st.st_dev, st.st_ino) in seen:
                out[rel] = seen[(st.st_dev, st.st_ino)]
                os.unlink(path)
            else:
                seen[(st.st_dev, st.st_ino)] = rel
    return out


def icon(icns, out):
    """The Windows icon from the Mac one (the same drawing): an .ico of its PNG images up to 256 px (Windows Vista+)."""
    with open(icns, "rb") as f:
        data = f.read()
    pngs, i = {}, 8
    while i + 8 <= len(data):
        n = int.from_bytes(data[i + 4:i + 8], "big")
        body = data[i + 8:i + n]
        if body[:8] == b"\x89PNG\r\n\x1a\n" and int.from_bytes(body[16:20], "big") <= 256:
            pngs[int.from_bytes(body[16:20], "big")] = body
        if n < 8:
            break
        i += n
    if not pngs:
        raise Refused(f"{icns}: the icon has no PNG")
    sizes = sorted(pngs)
    head = (0).to_bytes(2, "little") + (1).to_bytes(2, "little") + len(sizes).to_bytes(2, "little")
    entries, blobs, offset = b"", b"", 6 + 16 * len(sizes)
    for s in sizes:
        entries += bytes([s % 256, s % 256, 0, 0]) + (1).to_bytes(2, "little") + (32).to_bytes(2, "little") + \
            len(pngs[s]).to_bytes(4, "little") + (offset + len(blobs)).to_bytes(4, "little")
        blobs += pngs[s]
    with open(out, "wb") as f:
        f.write(head + entries + blobs)


def windows_shell(top, msys):
    """What only the Windows shell has: the app's git takes long paths, the links the build took out (links.json:
    "hard" — hard links, "msys" — msys's links and folders), app.py and the icon."""
    gitconfig = os.path.join(top, "runtime", "git", "etc", "gitconfig")
    if os.path.isdir(os.path.join(top, "runtime", "git")):
        os.makedirs(os.path.dirname(gitconfig), exist_ok=True)
        with open(gitconfig, "a", newline="\n") as f:
            f.write(LONGPATHS)
    with open(os.path.join(top, "links.json"), "w") as f:
        json.dump({"hard": hardlinks(top), "msys": msys}, f, ensure_ascii=False, indent=1, sort_keys=True)
    for name in WINDOWS_SHELL:
        shutil.copy2(os.path.join(WINDOWS, name), os.path.join(top, name))
    icon(ICNS, os.path.join(top, "app.ico"))


def _norm(info):
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def pack(top, archive):
    tmp = archive + ".part"
    with tarfile.open(tmp, "w:gz", compresslevel=6) as t:
        t.add(top, arcname=os.path.basename(top), filter=_norm)
    os.replace(tmp, archive)
    return {"name": os.path.basename(archive), "sha256": fetch.sha256(archive), "size": os.path.getsize(archive)}


# the bus's library loads in the environment's own Python, isolated from this machine's (core/bus.py: HPKE, keys)
BUS_PROBE = ("from cryptography.hazmat.primitives import hpke; "
             "from cryptography.hazmat.primitives.asymmetric import ed25519, x25519; "
             "hpke.Suite(hpke.KEM.X25519, hpke.KDF.HKDF_SHA256, hpke.AEAD.CHACHA20_POLY1305); "
             "x25519.X25519PrivateKey.generate(); ed25519.Ed25519PrivateKey.generate()")


def smoke(top, paths, bus=False):
    """This machine's platform: every program of the environment starts; bus — the library of the hub bus imports."""
    for key, args in (("python", ["-c", "import ssl, sqlite3, zoneinfo, venv"]), ("node", ["--version"]),
                      ("git", ["--version"]), ("gh", ["--version"]), ("ffmpeg", ["-hide_banner", "-version"]),
                      ("ffprobe", ["-hide_banner", "-version"])):
        if key in paths:
            run([os.path.join(top, paths[key]), *args], timeout=120)
    if bus:
        run([os.path.join(top, paths["python"]), "-I", "-c", BUS_PROBE], timeout=120)


def build(plat, out, repo=None, tag=None, origin=None, issues=None, download=None, cache=None, versions=None,
          runtime_only=False, skip=(), python=None, node=None, flavor="team", forbid=(), deliver=None, publisher=None,
          profile=None, embeds=None, check=True):
    """The platform folder, its archive and its sheet in out; the sheet (dict). Refused — nothing is left in out.
    deliver — {"url", "key"} of the distribution server, the team flavor's only; profile, embeds — the name of the
    company's build profile and its embeds (the layer is checked against them unless check is off — a release checks
    its commit once, not on each platform; its MANIFEST names them)."""
    if deliver and (flavor != "team" or not str(deliver.get("url") or "").startswith("https://")
                    or not re.fullmatch(r"[A-Za-z0-9_-]{16,200}", str(deliver.get("key") or ""))):
        raise Refused("--deliver-url (https://…) and --deliver-key (a key file) go with the team flavor only")
    if deliver and deliver.get("report") is not None and not re.fullmatch(
            r"https://[0-9A-Za-z]{8,64}@[A-Za-z0-9.-]+(?::\d+)?(?:/[A-Za-z0-9._-]+)*/\d+", str(deliver["report"])):
        raise Refused("--report-dsn: a Sentry DSN https://<key>@<host>/<project>")
    if sys.version_info < (3, 12):
        raise Refused(f"needs Python 3.12+ (the extraction filters of tarfile), and this is {sys.version.split()[0]}")
    if plat not in PLATFORMS:
        raise Refused(f"platform {plat!r}: one of {', '.join(PLATFORMS)}")
    if publisher is not None and (kind(plat) != "windows" or not re.fullmatch(r"[^\x00-\x1f\x7f]{1,200}", publisher)
                                  or publisher != publisher.strip()):
        raise Refused("--publisher: the organization of the Windows signature (an app for Windows only)")
    versions = versions or fetch.load()
    cache = cache or os.path.join(os.path.expanduser("~"), ".cache", "hub-package")
    skip = set(skip)
    if not runtime_only:
        if not (repo and re.match(r"^v\d+\.\d+\.\d+$", tag or "")):
            raise Refused("needs --repo and --tag vX.Y.Z (or --runtime-only)")
        if flavor not in layer.appcode.FLAVORS:
            raise Refused(f"--flavor {flavor!r}: one of {', '.join(layer.appcode.FLAVORS)}")
    name = (f"agents-hub-{tag}{'-open' if flavor == 'open' else ''}-{plat}" if tag else f"agents-hub-runtime-{plat}")
    windows = kind(plat) == "windows"
    native = host() == plat
    wanted = [p for p in fetch.parts(versions, plat) if p["key"] not in skip]
    files = {p["key"]: get(p, cache) for p in wanted}            # every download verified before any work
    wheel_parts = [p for p in wanted if versions[p["key"]].get("wheel")]
    for p in wheel_parts:
        why = wheels.fits(p["name"], plat, python_minor(versions))
        if why:
            raise Refused(why)
    if python is None and not runtime_only:
        python = tool(versions, "python", cache) if not native else None
    if node is None and "mcp" not in skip and "chrome-devtools-mcp" in versions:
        node = tool(versions, "node", cache) if not native else None
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    work = tempfile.mkdtemp(prefix="hub-build-", dir=os.path.dirname(os.path.abspath(out)))
    try:
        top = os.path.join(work, name)
        rt = os.path.join(top, "runtime")
        os.makedirs(rt)
        used, msys, installed = [], {}, {}
        for p in wanted:
            item = versions[p["key"]]
            if item.get("wheel"):
                continue
            got = unpack(files[p["key"]], os.path.join(rt, p["key"]), for_platform(item.get("keep"), plat),
                         for_platform(item.get("drop"), plat), for_platform(item.get("msys"), plat))
            msys.update({f"runtime/{p['key']}/{name}": target for name, target in got.items()})
            used.append(p["key"])
        for key in made_here(versions, plat):
            if key in skip:
                continue
            ffmpeg_mac(versions[key]["build"][plat], cache, os.path.join(rt, key))
            used.append(key)
        site = os.path.join(top, *SITE[kind(plat)].format(minor=python_minor(versions)).split("/"))
        for p in wheel_parts:   # after the Python they go into; the hub imports them as its own (core/bus.py)
            if not os.path.isdir(site):
                raise Refused(f"{p['name']}: the environment's Python has no {os.path.relpath(site, top)}")
            installed[p["key"]] = install_wheel(files[p["key"]], site, wheel_version(versions, p["key"], plat))
            used.append(p["key"])
        paths = {k: v for k, v in PATHS[kind(plat)].items() if os.path.exists(os.path.join(top, v))}
        if "mcp" not in skip and "chrome-devtools-mcp" in versions:
            paths["mcp"] = mcp(os.path.join(rt, "mcp"), node or os.path.join(top, paths["node"]), cache, windows)
            used.append("mcp")
        if windows and links(top):
            raise Refused(f"links in a build for Windows (they cannot be unpacked there without rights): {', '.join(links(top)[:5])}")
        if windows:
            windows_shell(top, msys)
            paths.update(app="app.py", icon="app.ico")
        arch = check_programs(top, plat)   # before running any: a foreign program says what it is, not "cannot start"
        if runs_here(plat):
            smoke(top, paths, bus="cryptography" in installed)
        code = None
        if not runtime_only:
            if python is None:
                python = os.path.join(top, paths["python"])
            got = run([python, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"])
            if got != python_minor(versions):
                raise Refused(f"the bytecode is compiled by Python {got}, and the package has {python_minor(versions)} — it needs the same")
            try:
                # the layer names where its source is (a hub's working copy clones it) and where issues go
                got = layer.build(source(repo, tag, work), tag, work, python, flavor=flavor, forbid=forbid,
                                  keep=os.path.join(top, "code-seed"), issues=issues,
                                  source=None if os.path.isdir(repo) or not layer.public_url(repo) else repo,
                                  profile=profile, wanted=embeds, check=check)
            except layer.appcode.Bad as e:
                raise Refused(str(e)) from None
            except layer.Refused as e:
                raise Refused(str(e)) from None
            if got["runtime_min"] > versions["runtime"]:
                raise Refused(f"the code of {tag} needs runtime #{got['runtime_min']} (RUNTIME_MIN), and versions.json "
                              f"gives #{versions['runtime']}")
            code = {"tag": tag, "commit": got["commit"], "layer": got["name"]}
            # swaps: how this environment's shellswap.py replaces a shell — "folder": the one-line install's pointer
            mark = {"format": "app", "layout": "layer", "flavor": flavor, "version": tag[1:],
                    "runtime": versions["runtime"], "platform": plat, "swaps": ["folder"]}
            if download or download_of(origin):
                mark["download"] = download or download_of(origin)
            if issues:
                mark["issues"] = issues
            if publisher:
                mark["publisher"] = publisher
            with open(os.path.join(top, "PACKAGED"), "w") as f:
                json.dump(mark, f, ensure_ascii=False)
            if deliver:
                fd = os.open(os.path.join(top, "deliver.json"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as f:
                    json.dump({"url": deliver["url"].rstrip("/"), "key": deliver["key"],
                               **({"report": deliver["report"]} if deliver.get("report") else {})}, f)
            shutil.copy2(os.path.join(HERE, "launch.py"), os.path.join(top, "launch.py"))   # the shell's own copy
            shutil.copy2(layer.appcode.__file__, os.path.join(top, "appcode.py"))   # launch.py's check, frozen with it
            # the replacement of the app by a newer one (app spec §4.4): its watchdog is always the old shell's own copy
            shutil.copy2(os.path.join(HERE, "shellswap.py"), os.path.join(top, "shellswap.py"))
            # the one-line install (spec §4.5): its scripts hand over to install.py, its service runs start.py
            for script in ("install.py", "start.py"):
                shutil.copy2(os.path.join(HERE, script), os.path.join(top, script))
            paths.update(launch="launch.py", install="install.py", start="start.py")
        skipped = sorted(skip)
        with open(os.path.join(top, "paths.json"), "w") as f:   # the shell's own map of its programs (S5–S7)
            json.dump(paths, f, indent=1)
        licenses(top, versions, plat, used, skipped, code, {k: os.path.join(site, d) for k, (_, _, d) in installed.items()})
        sheet = {"platform": plat, "tag": tag, "code": code, "runtime": versions["runtime"], "parts": {}, "arch": arch}
        for key in used:
            disk, count = installed[key][:2] if key in installed else tree(os.path.join(rt, key))
            item = versions["chrome-devtools-mcp"] if key == "mcp" else versions[key]
            sheet["parts"][key] = {"version": wheel_version(versions, key, plat) if key in installed else item.get("version"),
                                   "disk": disk, "files": count,
                                   "download": os.path.getsize(files[key]) if key in files else None}
        if not runtime_only:
            disk, count = tree(os.path.join(top, "code-seed"))
            sheet["seed"] = {"disk": disk, "files": count}
            sheet["layer"] = got
        disk, count = tree(top)
        sheet.update(total={"disk": disk, "files": count}, paths=paths, skipped=skipped)
        # the environment alone goes into another package (the .deb takes the folder): no archive of it
        sheet["archive"] = None if runtime_only else pack(top, os.path.join(work, name + ".tar.gz"))
        with open(os.path.join(work, name + ".json"), "w") as f:
            json.dump(sheet, f, ensure_ascii=False, indent=1)
        os.makedirs(out, exist_ok=True)
        layers = [] if runtime_only else [got["archive"]["name"], got["archive"]["name"][:-len(".tar.gz")] + ".json"]
        for n in (name, name + ".tar.gz", name + ".json", *layers):   # an earlier build's file of the name goes, even unreplaced
            target = os.path.join(out, n)
            if os.path.isdir(target) and not os.path.islink(target):
                shutil.rmtree(target)
            elif os.path.lexists(target):
                os.unlink(target)
            if os.path.lexists(os.path.join(work, n)):
                os.replace(os.path.join(work, n), target)
        return sheet
    finally:
        shutil.rmtree(work, ignore_errors=True)


def table(sheet):
    mb = lambda n: "—" if n is None else f"{n / 1e6:.1f}"   # noqa: E731
    rows = [("part", "version", "download, MB", "on disk, MB", "files")]
    for key, p in sheet["parts"].items():
        rows.append((key, str(p["version"] or "")[:28], mb(p["download"]), mb(p["disk"]), str(p["files"])))
    if sheet.get("seed"):
        rows.append(("code-seed", sheet["tag"], "—", mb(sheet["seed"]["disk"]), str(sheet["seed"]["files"])))
    rows.append(("total", "", "", mb(sheet["total"]["disk"]), str(sheet["total"]["files"])))
    if sheet.get("arch"):
        a = sheet["arch"]
        emu = "".join(f", {n} of another processor (known) in {w}" for w, n in a["emulated"].items())
        rows.append(("programs", f"{a['programs']} {a['format']} {a['want']}{emu}", "", "", ""))
    if sheet["archive"]:
        rows.append(("archive", sheet["archive"]["name"], mb(sheet["archive"]["size"]), "", ""))
    width = [max(len(r[i]) for r in rows) for i in range(5)]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, width)).rstrip() for r in rows)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="build.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("platform", choices=sorted(PLATFORMS))
    ap.add_argument("--out", required=True)
    ap.add_argument("--repo")
    ap.add_argument("--tag")
    ap.add_argument("--origin")
    ap.add_argument("--issues")
    ap.add_argument("--download")
    ap.add_argument("--cache")
    ap.add_argument("--runtime-only", action="store_true")
    ap.add_argument("--skip", default="")
    ap.add_argument("--flavor", default="team", choices=("team", "open"))
    ap.add_argument("--forbid", action="append", default=[])
    ap.add_argument("--deliver-url")
    ap.add_argument("--deliver-key", help="a file with the build key")
    ap.add_argument("--report-dsn", help="a file with the Sentry DSN of failed updates' reports (with --deliver-url)")
    ap.add_argument("--publisher", help="Windows: the organization (O=) of the signature the installers carry")
    a = ap.parse_args(argv)
    deliver = None
    if a.deliver_url or a.deliver_key:
        try:
            with open(a.deliver_key or "", encoding="utf-8") as f:
                deliver = {"url": a.deliver_url or "", "key": f.read().strip()}
        except OSError as e:
            sys.exit(f"build.py: --deliver-key: {e}")
    if a.report_dsn:
        if deliver is None:
            sys.exit("build.py: --report-dsn goes with --deliver-url and --deliver-key")
        try:
            with open(a.report_dsn, encoding="utf-8") as f:
                deliver["report"] = f.read().strip()
        except OSError as e:
            sys.exit(f"build.py: --report-dsn: {e}")
    try:
        # the open flavor is held against the forbidden strings of this repository's main copy (layer.deny_of)
        forbid = a.forbid + (layer.deny_of() if a.flavor == "open" else [])
        sheet = build(a.platform, a.out, a.repo, a.tag, a.origin, a.issues, a.download, a.cache,
                      runtime_only=a.runtime_only, skip=[s for s in a.skip.split(",") if s], flavor=a.flavor,
                      forbid=forbid, deliver=deliver, publisher=a.publisher)
    except (Refused, layer.Refused) as e:
        sys.exit(f"build.py: {e}")
    print(table(sheet))
    if sheet["archive"]:
        print(f"sha256 {sheet['archive']['sha256']}")
    if sheet["skipped"]:
        print(f"without: {', '.join(sheet['skipped'])} — a trial, not a release")


if __name__ == "__main__":
    main()
