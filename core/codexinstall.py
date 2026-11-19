"""Codex CLI from OpenAI's official releases (app spec §4a.1: the app supports Codex at its first start, the owner
2026-11-07 — "the app should support codex at installation too"), the way core/claudeinstall brings Claude Code: never
inside the app, a program of the user's own.

1. The pinned release (PIN: the version the core's Codex host was checked on — tools/codex_compat.py, Windows and the
   hooks) and this machine's file of it: github.com/openai/codex/releases/download/rust-v<version>/<asset>;
2. the archive into `<state>/downloads/` (core/fetch: in chunks, a broken download continues, the size and sha256
   pinned here — GitHub's own digest of the release asset); a mismatch is deleted and never run;
3. the one file of the archive (the binary named as the asset) into ~/.local/bin/codex[.exe] (another hub of the
   machine: its own <state>/bin — own_place, for_instance) through a `.part` and a replace — where hosts/codex.codex_bin
   looks; a running codex.exe on Windows is renamed aside (put_over) — then the archive goes. Beside it goes the
   helper the codex starts for its tools, codex-code-mode-host[.exe] (HELPERS: an asset of the release of its own — the
   codex archive holds the one file), and on Linux the bubblewrap of its sandbox, codex-resources/bwrap (a hub's session
   in a sandbox mode and the bus liaison run commands in it; codex takes the system's first, the file is put always, so
   whether a copy is whole does not depend on what else the machine has): all
   archives are fetched and checked before any is placed, so a failed download leaves the codex that was there; a codex
   that lacks one of them is not complete (`complete`) and is put anew (`hub setup codex` adds just those). Windows and
   macOS need nothing more: Windows sandbox ("unelevated", which the core sets up) is codex.exe itself, macOS's is the
   system's;
4. `codex --version` of the placed file with the hub's CODEX_HOME (never the personal ~/.codex) is the pinned one.
A codex already there at least MIN_CODEX is left alone (`hub setup codex --force` puts the pinned one over a copy the
hub put itself). Steps go to `<state>/logs/setup.log`.

Tests point HUB_CODEX_DOWNLOADS at a fake server; the sums are not configurable from the environment."""
import os
import re
import subprocess
import sys
import tarfile
import time

import platforms
from core import fetch, i18n

ROOT = "https://github.com/openai/codex/releases/download"
ROOT_ENV = "HUB_CODEX_DOWNLOADS"
MIN_CODEX = "0.162.1"   # the oldest the core's Codex host works with (the matrix of tools/codex_compat.py)
# the release the hub installs, with GitHub's sha256 digest and the size of each platform's archive (07.11: rust-v0.162.1)
PIN = {"version": "0.162.1", "assets": {
    "aarch64-apple-darwin": ("codex-aarch64-apple-darwin.tar.gz", 98104184,
                             "b293a0fcdd871900a44c0a4f40a1aa401b31e02854f790ebdb8bd5d1177fd1fe"),
    "x86_64-apple-darwin": ("codex-x86_64-apple-darwin.tar.gz", 107055460,
                            "d68e39d8f62639f33f7d00656554cb9c3ef7809e664a3827e62ab3ace3c21b21"),
    "x86_64-unknown-linux-musl": ("codex-x86_64-unknown-linux-musl.tar.gz", 111350218,
                                  "86f268d81b898f3e144c5ecff2ad2fda2c5802e042d6195b72538fe0fa125057"),
    "aarch64-unknown-linux-musl": ("codex-aarch64-unknown-linux-musl.tar.gz", 103595962,
                                   "bf3ca2719f1143b1440fbd85c149379e696c782c50ad7991f8bfcec60b2600c1"),
    "x86_64-pc-windows-msvc": ("codex-x86_64-pc-windows-msvc.exe.tar.gz", 111567537,
                               "a97c427a033b08d58902af3e52e8586ad2013267212112d306100223e2b12ec7"),
    "aarch64-pc-windows-msvc": ("codex-aarch64-pc-windows-msvc.exe.tar.gz", 102384529,
                                "af96f909063ecfbb12e9d4e88bbc6c7cec3be1397f0dbff0267013cf7f388a8c"),
},
       # the helper of each build (the same release's `codex-code-mode-host-<triple>` assets), the same kind of entries
       "helpers": {
           "aarch64-apple-darwin": ("codex-code-mode-host-aarch64-apple-darwin.tar.gz", 23826262,
                                    "1f464a2894adbc916e431b6bf19bef2cb485cf7e13e636d42578c581962b3727"),
           "x86_64-apple-darwin": ("codex-code-mode-host-x86_64-apple-darwin.tar.gz", 25706483,
                                   "cd52af8a6884b18239aaf36d0c99bc990e7dcf75b0917155474ffde88a111d37"),
           "x86_64-unknown-linux-musl": ("codex-code-mode-host-x86_64-unknown-linux-musl.tar.gz", 27500770,
                                         "458239ea45c1fc508d89695f180ab352f2834709a8a46929a4466f4f04399ada"),
           "aarch64-unknown-linux-musl": ("codex-code-mode-host-aarch64-unknown-linux-musl.tar.gz", 26003900,
                                          "1395ce9367f8acd280674dff083edf25bf221d908b8351d5bd4f3fdde9181e9a"),
           "x86_64-pc-windows-msvc": ("codex-code-mode-host-x86_64-pc-windows-msvc.exe.tar.gz", 28966536,
                                      "9fd8dbfd188fae23774081dc42297e85c87740697684ccae41c0e522b1d00382"),
           "aarch64-pc-windows-msvc": ("codex-code-mode-host-aarch64-pc-windows-msvc.exe.tar.gz", 27707106,
                                       "415038b2a58f9db858ed35cbca718005669d83b7ada2bb2a8503ec56d881f43b"),
       },
       # bubblewrap, the Linux sandbox of Codex (the same release's `bwrap-<triple>` assets): codex looks for the system
       # one in PATH, then for codex-resources/bwrap beside itself
       "bwrap": {
           "x86_64-unknown-linux-musl": ("bwrap-x86_64-unknown-linux-musl.tar.gz", 261612,
                                         "fa78980959466bd7a04643384c6293511f06a3140345c1237cfc75ccde7f3eea"),
           "aarch64-unknown-linux-musl": ("bwrap-aarch64-unknown-linux-musl.tar.gz", 254883,
                                          "83377376b697058a1bf6e73d754b027c66ef58b0a0e22527e12110865b33bc9c"),
       }}
VERSION = re.compile(r"\d+\.\d+\.\d+")


class Failed(Exception):
    """A step did not work: why — a short word for the page (network, broken, checksum, platform, install, old)."""

    def __init__(self, why, text):
        super().__init__(text)
        self.why = why


# the platform of Anthropic's downloads (platforms.current.claude_platform: the machine as the OS adapter sees it) ->
# the release triple of Codex CLI; Linux builds of Codex are musl ones, they run on glibc too
TRIPLES = {"darwin-arm64": "aarch64-apple-darwin", "darwin-x64": "x86_64-apple-darwin",
           "linux-x64": "x86_64-unknown-linux-musl", "linux-x64-musl": "x86_64-unknown-linux-musl",
           "linux-arm64": "aarch64-unknown-linux-musl", "linux-arm64-musl": "aarch64-unknown-linux-musl",
           "win32-x64": "x86_64-pc-windows-msvc", "win32-arm64": "aarch64-pc-windows-msvc"}


def target(plat=None):
    """This machine's release triple, or None (no build of Codex for it). plat — the platform instead of asking."""
    return TRIPLES.get(plat if plat is not None else platforms.current.claude_platform())


def newer_or_same(have, need):
    key = lambda v: tuple(int(x) for x in VERSION.search(v).group(0).split("."))   # noqa: E731
    return bool(have and VERSION.search(have)) and key(have) >= key(need)


def mine(path, home=None, state=None):
    """The codex at `path` is the one the hub puts (place, or this hub's own copy — own_place), not npm's, Homebrew's or
    Codex's own installer's."""
    same = lambda p: os.path.normcase(os.path.realpath(p))   # noqa: E731 — Windows: one file, two casings
    return same(path) == same(place(home)) or bool(state) and os.path.normcase(os.path.abspath(path)) == \
        os.path.normcase(os.path.abspath(own_place(state)))


def _suffix():
    return os.path.splitext(platforms.current.CLAUDE_EXE)[1]   # .exe on Windows, as the OS adapter names programs


def place(home=None):
    """Where the hub puts the command: ~/.local/bin/codex (codex.exe on Windows) — the last place codex_bin looks."""
    return os.path.join(home or os.path.expanduser("~"), ".local", "bin", "codex" + _suffix())


def helper_of(path):
    """The helper beside a codex file: codex looks for codex-code-mode-host[.exe] in its own folder."""
    return os.path.join(os.path.dirname(path), "codex-code-mode-host" + _suffix())


def bwrap_of(path):
    """The bubblewrap beside a codex file (Linux): codex-resources/bwrap, where codex looks when the system has none."""
    return os.path.join(os.path.dirname(path), "codex-resources", "bwrap")


def needs(path, plat=None):
    """The files beside the codex at `path` it starts, or needs for its sandbox, on this machine."""
    plat = plat if plat is not None else platforms.current.claude_platform()
    return [helper_of(path)] + ([bwrap_of(path)] if (target(plat) or "").endswith("linux-musl") else [])


def complete(path, home=None, state=None):
    """The codex at `path` has what it starts for its tools and its sandbox needs (`needs`). Only the hub's own copy is
    asked (`mine`): npm's, Homebrew's and Codex's own installer bring their own, a link points at one of those."""
    return os.path.islink(path) or not mine(path, home, state) or all(os.path.isfile(p) for p in needs(path))


def own_place(state):
    """A hub's own copy: <state>/bin/codex[.exe] — what another hub of the machine on Codex puts when the machine has no
    codex (the spec of several hubs, 2.12): not in the PATH of the main hub, whose critic would otherwise start taking
    the personal ~/.codex; hosts/codex.codex_bin looks there second, after HUB_CODEX_BIN."""
    return os.path.join(state, "bin", "codex" + _suffix())


def shared(home=None, which=None):
    """The machine's codex another hub may take: in PATH or ~/.local/bin, or None. which — shutil.which in tests."""
    import shutil
    which = which or shutil.which
    found = which("codex")
    at = place(home)
    return found or (at if os.path.isfile(at) else None)


def for_instance(state, say=None, home=None, which=None, installer=None, dry=False):
    """The codex of a new hub on Codex (`hub instance new <name> --engine codex`; decided 9.5: "if there is no codex we
    install it"): the machine's one at least MIN_CODEX is taken as it is — a POSIX hub's service does not have the
    person's PATH, so one outside ~/.local/bin gets a link at own_place (to the command PATH found, not to its real file:
    Homebrew's moves with every upgrade); one that is a script (npm's codex.js wants node, which the service has not)
    or none, or an older one of someone else's — the pinned release into own_place, never into PATH. A copy at
    own_place that is not fit goes first: the host looks there before PATH. -> (kind, path): kind "shared" | "linked" |
    "own" | "install" (dry: what would be done — nothing is run or written, a found codex is not asked its version).
    Failed when it cannot be had."""
    inst = installer or Installer(state, home=home, say=say or (lambda text: None), dest=own_place(state))
    own = own_place(state)
    fit = lambda exe: dry or newer_or_same(inst.version_of(exe), MIN_CODEX) and complete(exe, home, state)   # noqa: E731
    if os.path.isfile(own) and fit(own):
        return "own", own
    found = taken(home, which)
    if found and fit(found):
        if dry:
            return ("linked" if linked(found, home) else "shared"), found
        if os.path.lexists(own):
            os.remove(own)   # an older copy of its own, or a link to a codex that went: it would be found before PATH
        if not linked(found, home):
            return "shared", found
        os.makedirs(os.path.dirname(own), exist_ok=True)
        os.symlink(os.path.abspath(found), own)
        return "linked", found
    if inst.triple not in inst.pin["assets"]:
        raise Failed("platform", i18n.t("cli.codex_setup.platform", system=inst.plat or "?", machine="-"))
    if dry:
        return "install", own
    return "own", inst.run_install()[1]


def linked(found, home=None):
    """The machine's codex reaches a new hub through a link at own_place: POSIX, outside ~/.local/bin (the scheduler's
    task on Windows runs with the person's PATH)."""
    if platforms.current.APP_OS == "windows":
        return False
    return os.path.normcase(os.path.realpath(found)) != os.path.normcase(os.path.realpath(place(home)))


def taken(home=None, which=None):
    """The machine's codex a new hub would take (its version aside), or None: in PATH or ~/.local/bin, and not a script
    a service cannot run (npm's codex.js wants node) where it would be linked."""
    found = shared(home, which)
    return found if found and not (linked(found, home) and script(found)) else None


def script(path):
    """The program is a script (its first bytes "#!": npm's codex.js), which needs an interpreter in PATH."""
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"#!"
    except OSError:
        return True


class Installer:
    def __init__(self, state_dir, root_url=None, home=None, run=subprocess.run, say=None, progress=None, pin=None,
                 plat=None, dest=None):
        self.state_dir, self.home, self.run, self.dest = state_dir, home, run, dest
        self.root = (root_url or os.environ.get(ROOT_ENV) or ROOT).rstrip("/")
        self.say, self.progress = say or (lambda text: None), progress
        self.pin, self.plat = pin or PIN, plat if plat is not None else platforms.current.claude_platform()
        self.triple = target(self.plat)

    def log(self, text):
        try:
            os.makedirs(os.path.join(self.state_dir, "logs"), exist_ok=True)
            with open(os.path.join(self.state_dir, "logs", "setup.log"), "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} codex: {text}\n")
        except OSError:
            pass

    def version_of(self, exe):
        """`codex --version` with the hub's own CODEX_HOME: even --version writes a helper folder into its home."""
        from core import codexcli, codexmodel
        try:
            codexcli._prepare(self.state_dir)
            r = self.run([exe, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                         stdin=subprocess.DEVNULL, timeout=60, env=codexmodel.env(self.state_dir))
        except (OSError, subprocess.SubprocessError):
            return None
        m = VERSION.search(r.stdout or "") if r.returncode == 0 else None
        return m.group(0) if m else None

    def extras(self, dest, missing_only=False):
        """The files put beside the codex at `dest`, as (asset, where): the helper (every build) and bubblewrap (Linux).
        missing_only: those not there yet."""
        out = []
        for kind, at in (("helpers", helper_of(dest)), ("bwrap", bwrap_of(dest))):
            asset = (self.pin.get(kind) or {}).get(self.triple)
            if asset and not (missing_only and os.path.isfile(at)):
                out.append((asset, at))
        return out

    def run_install(self, extras_only=False):
        """-> (version, path) of the placed codex; Failed otherwise. extras_only: the codex at the place is the pinned
        one already, only what goes beside it is missing (a hub of an earlier release): just that is fetched."""
        version = self.pin["version"]
        if self.triple not in self.pin["assets"]:
            raise Failed("platform", i18n.t("cli.codex_setup.platform", system=self.plat or "?", machine="-"))
        dest = self.dest or place(self.home)
        # the files beside it are placed first: a stop between leaves the old codex with new ones, never a new codex
        # without them
        wanted = self.extras(dest, missing_only=extras_only)
        if not extras_only:
            wanted.append((self.pin["assets"][self.triple], dest))
        downloads = os.path.join(self.state_dir, "downloads")
        os.makedirs(downloads, exist_ok=True)
        archives = []
        try:
            for (name, size, sha), _ in wanted:   # all fetched and checked before anything is placed
                archive = os.path.join(downloads, name)
                archives.append(archive)
                url = f"{self.root}/rust-v{version}/{name}"
                self.say(i18n.t("cli.codex_setup.downloading", version=version, mb=max(1, round(size / 1e6))))
                self.log(f"downloading {url}")
                try:
                    what = " (helper)" if "code-mode-host" in name else \
                        " (bubblewrap)" if name.startswith("bwrap") else ""
                    fetch.download(url, archive, sha, size, f"Codex CLI {version}{what}", "hub setup codex",
                                   progress=self.progress, log=self.log)
                except fetch.Failed as e:
                    raise Failed(e.why, str(e)) from None
            for ((name, _, _), into), archive in zip(wanted, archives):
                self.unpack(archive, name, into)
        finally:
            for archive in archives:
                try:
                    os.remove(archive)
                except OSError:
                    pass
        got = self.version_of(dest)
        if not got:
            raise Failed("install", i18n.t("cli.codex_setup.not_found", path=dest))
        if not newer_or_same(got, MIN_CODEX):
            raise Failed("old", i18n.t("cli.codex_setup.old", have=got, need=MIN_CODEX))
        self.log(f"installed {got} at {dest}")
        return got, dest

    def ensure_whole(self):
        """run_install, except that a pinned codex of the hub's own that only lacks what goes beside it is not fetched
        again."""
        dest = self.dest or place(self.home)
        if os.path.isfile(dest) and not complete(dest, self.home, self.state_dir) and \
                self.version_of(dest) == self.pin["version"]:
            return self.run_install(extras_only=True)
        return self.run_install()

    def unpack(self, archive, name, dest):
        """The one member named as the asset without .tar.gz, a regular file — written whole, then put in place."""
        member = name[: -len(".tar.gz")]
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        part = dest + ".part"
        with tarfile.open(archive, "r:gz") as tar:
            info = next((m for m in tar.getmembers() if m.name.lstrip("./") == member), None)
            if info is None or not info.isfile():
                raise Failed("install", i18n.t("cli.codex_setup.no_member", member=member))
            src = tar.extractfile(info)
            with open(part, "wb") as out:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
        os.chmod(part, 0o755)
        put_over(part, dest)


def put_over(part, dest, replace=None, rename=os.replace):
    """part in dest's place. Windows does not replace a codex.exe some hub's session runs (platforms' replace gives up
    after 5 s), but it lets the running file be renamed: then dest becomes dest.old (dest.old1… while an earlier one
    still runs) and part goes in its place; the .old files of earlier installs go first, those nothing runs any more
    (the spec of several hubs, 2.12). replace, rename — tests."""
    replace = replace or platforms.current.replace
    folder, base = os.path.split(dest)
    for name in sorted(os.listdir(folder or ".")):
        if name == base + ".old" or name.startswith(base + ".old") and name[len(base) + 4:].isdigit():
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                pass   # still running: a later install takes it away
    try:
        replace(part, dest)
    except PermissionError:
        if not os.path.exists(dest):
            raise
        old = next(p for p in (dest + ".old" + (str(i) if i else "") for i in range(1000)) if not os.path.lexists(p))
        rename(dest, old)
        try:
            rename(part, dest)
        except OSError:
            rename(old, dest)   # the old one back: the hubs keep a codex
            raise


def main(argv, state_dir):
    """hub setup codex [--force]: the exit code. A codex already there at least MIN_CODEX is left alone."""
    from hosts.codex import codex_bin
    force = "--force" in argv
    if [a for a in argv if a != "--force"]:
        print(i18n.t("cli.codex_setup.usage"), file=sys.stderr)
        return 2
    from core import instance
    exe = codex_bin(state_dir)
    found = exe if os.path.isfile(exe) else None
    # another hub of the machine (an instance) puts its own copy, not one into the PATH of the main hub (2.12): the host
    # looks there before PATH, so it is put over a foreign or a missing one too; its own copy, or a link there to the
    # machine's one, is put over in its place
    own = bool(instance.load(state_dir)) and not (found and mine(found))
    inst = Installer(state_dir, say=print, dest=own_place(state_dir) if own else None)
    have = inst.version_of(found) if found else None
    if have and newer_or_same(have, MIN_CODEX) and not force:
        if complete(found, state=state_dir):
            print(i18n.t("cli.codex_setup.have", version=have, path=found))
            return 0
        if have == inst.pin["version"]:   # the hub's own copy of an earlier release: what goes beside it was never put
            inst.dest = found   # beside the codex that runs, wherever that is
            try:
                got, path = inst.run_install(extras_only=True)
            except Failed as e:
                print(str(e), file=sys.stderr)
                return 1
            print(i18n.t("cli.codex_setup.done", version=got, path=path))
            return 0
    if found and not own and not mine(found):   # PATH finds it before ~/.local/bin: a copy put there would never run
        print(i18n.t("cli.codex_setup.foreign", path=found), file=sys.stderr)
        return 1
    try:
        got, path = inst.run_install()
    except Failed as e:
        print(str(e), file=sys.stderr)
        return 1
    print(i18n.t("cli.codex_setup.done", version=got, path=path))
    return 0
