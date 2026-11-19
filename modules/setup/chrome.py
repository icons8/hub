"""Chrome for Testing on first use (installer spec §4, §12, owner 01.11: not in the package — downloaded like Claude
Code): the build pinned in tools/package/versions.json (URL, sha256 and size per platform) through core/fetch, unpacked
into `<state>/runtime/chrome/<version>/` and named in `<state>/runtime/chrome.json` (core/runtime). A zip entry that
would land outside the folder (`..`, an absolute path, a link out) is refused before anything is written."""
import functools
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import zipfile

from core import fetch, i18n, runtime

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VERSIONS = os.path.join(REPO, "tools", "package", "versions.json")
# the program inside the archive's top folder, per platform key
PROGRAMS = ("chrome", "chrome.exe", "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing")
MAX_ENTRIES = 20000


class Refused(Exception):
    """Not downloaded or not unpacked, for a reason a person can act on; `why` is the job's reason word."""

    def __init__(self, text, why="unpack"):
        super().__init__(text)
        self.why = why


SHARED_NAME = "agents-hub-chrome"           # the AppArmor profile of the package's Chrome (/etc/apparmor.d/<name>)
SAFE_PATH = re.compile(r"[A-Za-z0-9_./+-]+")   # what may stand in a profile's path: no pattern characters at all


def sandbox_blocked():
    """Linux (Ubuntu 23.10+) gives the Chrome sandbox only to a program with an AppArmor profile (testchrome)."""
    from modules.testchrome import chrome as testchrome
    return testchrome.sandbox_blocked()


def sandbox_command():
    """The command that puts Chrome where the sandbox is allowed, when this hub came in a package that has one."""
    from core import packaged
    info = packaged.info()
    return "sudo agents-hub chrome" if isinstance(info, dict) and info.get("shared") else None


GOOGLE_DEB = "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb"
GOOGLE_RPM = "https://dl.google.com/linux/direct/google-chrome-stable_current_x86_64.rpm"


def system_command(which=shutil.which, plat=None):
    """The one root command that puts Google Chrome on this Linux for the whole machine, or None (another system, an
    arm machine — Google has no Linux arm build, a system without apt or dnf). Google's package brings what a Chrome
    in the user's folder misses: the AppArmor profile Ubuntu 23.10+ wants for its sandbox (the system's own, for
    /opt/google/chrome/chrome) and the libraries a server lacks; the hub then takes it (browsers.find_chrome). Root
    runs nothing a session can write: the package is fetched by root into its own new folder, from Google over https
    (a command that ran code from the hub's folders as root would hand root to anything that edits them)."""
    if (plat or key()) != "linux-x64":
        return None
    if which("apt-get"):
        get = "curl -fsSLO" if which("curl") else "wget -q" if which("wget") else None
        name = GOOGLE_DEB.rsplit("/", 1)[1]
        # a stale index (a cloud image) fails on 404s of the dependencies, a broken third-party repository fails the
        # update itself: the update is tried, the install goes on either way; the 110 MB package does not stay behind;
        # the folder is readable (not writable) by others: apt's own user _apt reads the package from it
        return get and (f"sudo sh -c 'd=$(mktemp -d) && chmod 755 \"$d\" && cd \"$d\" && {get} {GOOGLE_DEB} && (apt-get update -qq || :) && "
                        f"apt-get install -y ./{name}; r=$?; rm -rf \"$d\"; exit $r'")
    if which("dnf"):
        return f"sudo dnf install -y {GOOGLE_RPM}"
    return None


def sandbox_help():
    cmd = sandbox_command()
    if cmd:
        return i18n.t("cli.setup.chrome_sandbox_cmd", cmd=cmd)
    system = system_command()
    return i18n.t("cli.setup.chrome_sandbox_system", cmd=system) if system else i18n.t("cli.setup.chrome_sandbox_other")


@functools.cache
def _translated():
    """Under Rosetta (asked once: sysctl is a process)."""
    from platforms import current
    return current.claude_platform() == "darwin-arm64"


def key(system=None, machine=None):
    """The platform key of versions.json — the release factory's names (tools/package/build.py PLATFORMS):
    linux-x64 | linux-arm64 | mac-arm64 | mac-x64 | windows-x64 | windows-arm64; None — unknown."""
    system = system or sys.platform
    m = (machine or platform.machine() or "").lower()
    arch = "arm64" if m in ("arm64", "aarch64") else "x64" if m in ("x86_64", "amd64", "x64") else None
    if not arch:
        return None
    if system.startswith("linux"):
        return f"linux-{arch}"
    if system == "darwin":
        if arch == "x64" and machine is None:   # an x86_64 Python under Rosetta on an arm Mac: the arm build runs
            arch = "arm64" if _translated() else arch
        return f"mac-{arch}"
    if system in ("win32", "cygwin"):
        return f"windows-{arch}"
    return None


def pinned(path=VERSIONS, plat=None):
    """{version, url, sha256, size} of this platform's build, or None (no pin for it: Chrome is not downloadable here
    yet). An entry without a size is not usable: the downloader needs it."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    c = data.get("chrome") if isinstance(data, dict) else None
    files = c.get("files") if isinstance(c, dict) else None
    got = (files or {}).get(plat or key()) if isinstance(files, dict) else None
    if not isinstance(got, dict):
        return None
    url, sha, size = got.get("url"), got.get("sha256"), got.get("size")
    if not (isinstance(url, str) and url.startswith("https://") and isinstance(sha, str)
            and re.fullmatch(r"[0-9a-f]{64}", sha) and type(size) is int and size > 0):
        return None
    ver = re.match(r"[0-9.]+", str(c.get("version") or "")) if isinstance(c, dict) else None
    return {"version": ver.group(0) if ver else "0", "url": url, "sha256": sha, "size": size}


def _inside(root, rel):
    """The path of a zip entry under root, or Refused: absolute, a drive, `..`."""
    rel = rel.replace("\\", "/")
    if rel.startswith("/") or re.match(r"[A-Za-z]:", rel) or any(p == ".." for p in rel.split("/")):
        raise Refused(rel)
    dest = os.path.normpath(os.path.join(root, *[p for p in rel.split("/") if p]))
    if os.path.commonpath([os.path.abspath(root), os.path.abspath(dest)]) != os.path.abspath(root):
        raise Refused(rel)
    return dest


def unpack(archive, root):
    """The zip into root (empty): files with their x bit, links (macOS frameworks: `Versions/Current -> A`) only
    downward — a target with `..` or absolute is refused, and nothing is written through a link that leads out of root
    (checked on disk, not on paper: chained links)."""
    real_root = os.path.realpath(root)
    with zipfile.ZipFile(archive) as z:
        infos = z.infolist()
        if len(infos) > MAX_ENTRIES:
            raise Refused(f"{len(infos)} entries")
        for info in infos:
            dest = _inside(root, info.filename)
            if os.path.commonpath([real_root, os.path.realpath(os.path.dirname(dest))]) != real_root:
                raise Refused(info.filename)
            mode = info.external_attr >> 16
            if info.is_dir():
                os.makedirs(dest, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if stat.S_ISLNK(mode):
                target = z.read(info).decode("utf-8", "replace")
                if os.path.isabs(target) or re.match(r"[A-Za-z]:", target) or ".." in target.replace("\\", "/").split("/"):
                    raise Refused(f"{info.filename} -> {target}")
                os.symlink(target, dest)
                continue
            with z.open(info) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
            if mode & 0o111:
                os.chmod(dest, 0o755)


def sound(root):
    """A folder unpacked earlier holds to unpack()'s rules today: every link downward and inside root."""
    real_root = os.path.realpath(root)
    for top, dirs, files in os.walk(root):
        for name in dirs + files:
            p = os.path.join(top, name)
            if os.path.islink(p):
                target = os.readlink(p)
                if os.path.isabs(target) or ".." in target.replace("\\", "/").split("/") or \
                        os.path.commonpath([real_root, os.path.realpath(p)]) != real_root:
                    return False
    return True


def program(root):
    """The Chrome program in the unpacked build (path relative to root), or None."""
    for top in sorted(os.listdir(root)):
        for name in PROGRAMS:
            p = os.path.join(root, top, *name.split("/"))
            if os.path.isfile(p):
                return os.path.relpath(p, root)
    return None


def install(state, pin, say=None, progress=None, log=None, run=subprocess.run):
    """Download, check, unpack, name it in runtime/chrome.json; -> the program's path. fetch.Failed or Refused.
    Where Linux forbids the sandbox to a program without an AppArmor profile (Ubuntu 23.10+), a Chrome in the user's own
    folder could never start and no profile may name that folder (anyone who can write it could put any program
    there): nothing is downloaded, the person is told what to do (a package has a root helper, install_shared)."""
    say = say or (lambda text: None)
    if sandbox_blocked():
        raise Refused(sandbox_help(), "sandbox")
    base = runtime.folder(state)
    downloads = os.path.join(state, "downloads")
    os.makedirs(downloads, exist_ok=True)
    zip_path = os.path.join(downloads, f"chrome-{pin['version']}.zip")
    final = os.path.join(base, "chrome", pin["version"])
    rel = program(final) if os.path.isdir(final) and sound(final) else None
    if rel:   # unpacked before, but it did not start then (libraries): look at it again, no second download
        return _start(state, pin, final, rel, run)
    say(i18n.t("cli.setup.chrome_downloading", mb=f"{pin['size'] / 1e6:.0f}"))
    fetch.download(pin["url"], zip_path, pin["sha256"], pin["size"], f"Chrome {pin['version']}",
                   f"{i18n.t('web.settings.tab.about')} → {i18n.t('web.setup.chrome')}",   # the card of "About the hub"
                   progress=progress, log=log, say=say)
    tmp = final + ".new"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    try:
        say(i18n.t("cli.setup.chrome_unpacking"))
        unpack(zip_path, tmp)
        rel = program(tmp)
        if not rel:
            raise Refused("no Chrome program in the archive")
        shutil.rmtree(final, ignore_errors=True)
        os.replace(tmp, final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    finally:
        try:
            os.remove(zip_path)
        except OSError:
            pass
    return _start(state, pin, final, rel, run)


def _wont_start(exe, run):
    """Why the program does not answer `--version` (text), or None when it does."""
    try:
        r = run([exe, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                stdin=subprocess.DEVNULL, timeout=60)
        return None if r.returncode == 0 else ((r.stderr or "") + (r.stdout or "")).strip()[-400:] or r.returncode
    except (OSError, subprocess.SubprocessError) as e:
        return f"{type(e).__name__}: {e}"[:400]


def _start(state, pin, final, rel, run):
    """Whether the unpacked build starts (`--version`); then it is named in runtime/chrome.json. It does not start:
    fetch.Failed(start), the files stay (they are fine — Linux without Chrome's libraries) for the next try."""
    base = runtime.folder(state)
    exe = os.path.join(final, rel)
    bad = _wont_start(exe, run)
    if bad is not None:
        raise fetch.Failed("start", i18n.t("cli.setup.chrome_no_start", output=bad))
    from core import catalog
    catalog.write_private(os.path.join(base, runtime.CHROME_FILE),
                          {"version": pin["version"], "path": os.path.relpath(exe, base)})
    for old in os.listdir(os.path.join(base, "chrome")):   # an earlier build: gone once the new one starts
        if old != pin["version"]:
            shutil.rmtree(os.path.join(base, "chrome", old), ignore_errors=True)
    return exe


# --- Chrome for the whole machine (a package; installer spec S7) -----------------------------------------------------
PROFILE = """# Agents Hub: Chrome for Testing, put here by `agents-hub chrome` (root). The one program that may make the user
# namespaces its sandbox needs. The path is root's: nobody else can put a program under it.
{abi}include <tunables/global>

profile {name} {exe} flags=(unconfined) {{
  userns,
  {exe} mr,

  # Site-specific additions and overrides. See local/README for details.
  include if exists <local/{name}>
}}
"""


def profile_text(exe, abi):
    """The AppArmor profile that gives `exe` — exactly this path — the user namespaces (Ubuntu 23.10+ gives them only to
    programs with a profile; the one Ubuntu ships for Google Chrome looks the same, but names the program by a variable
    that apparmor_parser -Q cannot resolve without the kernel — here the path stands as it is)."""
    if not SAFE_PATH.fullmatch(exe) or not os.path.isabs(exe):
        raise Refused(f"path {exe!r} cannot stand in a profile", "path")
    return PROFILE.format(abi=f"abi <abi/{abi}>,\n", name=SHARED_NAME, exe=exe)


def abi_of(apparmor_dir="/etc/apparmor.d"):
    """The newest abi the system's AppArmor has that knows the `userns` rule (4.0+: Ubuntu 23.10+ — the systems that restrict
    the namespaces); None — an older AppArmor, whose parser would refuse the file (and so would the boot of the service)."""
    return next((v for v in ("5.0", "4.0") if os.path.exists(os.path.join(apparmor_dir, "abi", v))), None)


def load_profile(path):
    """Loads (or replaces) the profile in the kernel; False — no AppArmor here or the parser said no (the file is read
    again at the next boot)."""
    parser = shutil.which("apparmor_parser")
    if not parser or not os.path.isdir("/sys/kernel/security/apparmor"):
        return False
    try:
        return subprocess.run([parser, "-r", path], capture_output=True, timeout=120,
                              stdin=subprocess.DEVNULL).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _read_only_for_others(root):
    """Nothing under root is writable by group or others (a zip entry may say 0777); links are left as they are."""
    for top, dirs, files in os.walk(root):
        for name in dirs + files:
            p = os.path.join(top, name)
            if not os.path.islink(p):
                os.chmod(p, os.stat(p).st_mode & ~(stat.S_IWGRP | stat.S_IWOTH))
    os.chmod(root, os.stat(root).st_mode & ~(stat.S_IWGRP | stat.S_IWOTH))


def _publish(path, text, mode=0o644, suffix=".new"):
    """A file everybody can read, replaced whole through a file next to it; `suffix` names that file (in /etc/apparmor.d
    it is one AppArmor skips, `.dpkg-new`: a run cut off must not leave a second copy of the profile there). The file is
    made with its mode at once and never through a link."""
    tmp = path + suffix
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), mode)
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def install_shared(root, pin, say=None, progress=None, log=None, run=subprocess.run, apparmor_dir="/etc/apparmor.d",
                   load=load_profile, root_only=True):
    """Chrome for every user of the machine: `<root>/chrome/<version>/…` (root's, nobody else can write there), a profile
    for exactly that program, `<root>/chrome/chrome.json` naming it (core/runtime.shared reads it through PACKAGED
    "shared"). The pin is the caller's — the package's own seed, never code a user can write: this runs as root.
    -> the program's path. Refused (not root, an odd path, a bad archive) or fetch.Failed."""
    say = say or (lambda text: None)
    if root_only and os.geteuid() != 0:
        raise Refused(i18n.t("cli.setup.chrome_root"), "root")
    base = os.path.join(root, "chrome")
    if not SAFE_PATH.fullmatch(os.path.abspath(base)):
        raise Refused(f"path {base!r} cannot stand in a profile", "path")
    os.makedirs(base, mode=0o755, exist_ok=True)
    final = os.path.join(base, pin["version"])
    rel = program(final) if os.path.isdir(final) and sound(final) else None
    if rel is None:
        downloads = os.path.join(base, ".download")
        os.makedirs(downloads, mode=0o755, exist_ok=True)
        zip_path = os.path.join(downloads, f"chrome-{pin['version']}.zip")
        say(i18n.t("cli.setup.chrome_downloading", mb=f"{pin['size'] / 1e6:.0f}"))
        fetch.download(pin["url"], zip_path, pin["sha256"], pin["size"], f"Chrome {pin['version']}",
                       "sudo agents-hub chrome", progress=progress, log=log, say=say)
        tmp = final + ".new"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        try:
            say(i18n.t("cli.setup.chrome_unpacking"))
            unpack(zip_path, tmp)
            rel = program(tmp)
            if not rel:
                raise Refused("no Chrome program in the archive")
            _read_only_for_others(tmp)
            bad = _wont_start(os.path.join(tmp, rel), run)
            if bad is not None:
                raise fetch.Failed("start", i18n.t("cli.setup.chrome_no_start", output=bad))
            shutil.rmtree(final, ignore_errors=True)
            os.replace(tmp, final)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        finally:
            shutil.rmtree(downloads, ignore_errors=True)
    exe = os.path.join(final, rel)
    path = os.path.join(apparmor_dir, SHARED_NAME)
    abi = abi_of(apparmor_dir)
    if abi:      # no such AppArmor (none, or older): nobody asks for a profile, and its parser would refuse this one
        _publish(path, profile_text(exe, abi), suffix=".dpkg-new")
        say(i18n.t("cli.setup.chrome_profile_loaded" if load(path) else "cli.setup.chrome_profile_later", path=path))
    _publish(os.path.join(base, runtime.CHROME_FILE),
             json.dumps({"version": pin["version"], "path": os.path.relpath(exe, base)}, ensure_ascii=False) + "\n")
    for old in os.listdir(base):                      # an earlier build: gone once the new one is in place
        if old != pin["version"] and not old.startswith(".") and os.path.isdir(os.path.join(base, old)):
            shutil.rmtree(os.path.join(base, old), ignore_errors=True)
    return exe
