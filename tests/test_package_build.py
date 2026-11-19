"""The release factory (installer spec S4, tools/package): what the shells of S5–S7 are built from.

versions.json pins every part of every platform (URL, sha256, size); build.py lays one platform out — the environment
(runtime/), the seed (a git clone of the shared repo on a release tag with its bytecode), PACKAGED of the app,
licenses — and packs it with a sheet of sizes; upload.py attaches files to a release only when told to send.
Downloads here are file:// archives built by the test: no network."""
import tests  # noqa: F401 - keeps the machine's settings out
import calendar
import fnmatch
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from unittest import mock

import platforms
from core import appcode

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(REPO, "tools", "package")


def load(name):
    spec = importlib.util.spec_from_file_location(f"hub_package_{name}", os.path.join(PKG, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def g(cwd, *a):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class Versions(unittest.TestCase):
    """The real tools/package/versions.json."""

    def setUp(self):
        with open(os.path.join(PKG, "versions.json")) as f:
            self.v = json.load(f)
        self.build = load("build")

    def test_every_part_of_every_platform_is_pinned_by_address_sha256_and_size(self):
        for plat in self.build.PLATFORMS:
            for key, item in self.v.items():
                if not isinstance(item, dict):
                    continue
                for pin in [(item.get("files") or {}).get(plat)] + [((item.get("build") or {}).get(plat) or {})
                                                                     .get("source")]:
                    if pin is None:
                        continue
                    with self.subTest(part=key, platform=plat):
                        self.assertTrue(pin["url"].startswith("https://"), pin)
                        self.assertRegex(pin["sha256"], r"^[0-9a-f]{64}$")
                        self.assertIsInstance(pin["size"], int)
                        self.assertGreater(pin["size"], 0)

    def test_each_platform_gets_what_the_hub_needs(self):
        bus = {"cryptography", "cffi", "pycparser"}   # the hub bus: the library in the environment's Python
        need = {"linux-x64": {"python", "node", "gh", "ffmpeg", *bus}, "linux-arm64": {"python", "node", "gh", "ffmpeg", *bus},
                "mac-arm64": {"python", "node", "gh", "ffmpeg", "git", *bus},
                "mac-x64": {"python", "node", "gh", "ffmpeg", "git", *bus},
                "windows-x64": {"python", "node", "gh", "ffmpeg", "git", *bus},
                # no cryptography >= 47 for Windows on ARM: its wheels ended with 46.0.3 (the bus says no_lib there)
                "windows-arm64": {"python", "node", "gh", "ffmpeg", "git"}}
        # owner 2026-11-08 (q1880847): an Intel Mac and Windows on ARM get their own build, the install picks it
        self.assertEqual(set(self.build.PLATFORMS), set(need))
        fetch = load("fetch")
        for plat, want in need.items():
            with self.subTest(platform=plat):
                have = {p["key"] for p in fetch.parts(self.v, plat)} | set(self.build.made_here(self.v, plat))
                self.assertEqual(have, want)

    def test_chrome_is_not_in_the_package_but_pinned_for_the_first_start(self):
        # owner 2026-11-01 (q1404846): Chrome is downloaded at the first start, the downloader needs sha256 and size
        fetch = load("fetch")
        for key in ("chrome", "chrome-headless-shell"):
            self.assertIs(self.v[key]["bundle"], False)
            self.assertEqual(set(self.v[key]["files"]), set(self.build.PLATFORMS))
        for plat in self.build.PLATFORMS:
            self.assertFalse([p for p in fetch.parts(self.v, plat) if p["key"].startswith("chrome")])
        # Chrome for Testing has no ARM64 Windows build: Windows on ARM takes the win64 one (x64 emulation) — the same
        # pin, so that a new Chrome cannot leave it on the old build under the new version
        for key in ("chrome", "chrome-headless-shell"):
            self.assertEqual(self.v[key]["files"]["windows-arm64"], self.v[key]["files"]["windows-x64"])

    def test_no_compiler_of_the_code_any_more(self):
        # owner 2026-11-01: Nuitka is dropped, the code is open (.py + .pyc)
        self.assertNotIn("nuitka", self.v)
        self.assertFalse(os.path.exists(os.path.join(PKG, "compile.py")))

    def test_the_bus_library_is_pinned_new_enough_for_the_environment_s_python(self):
        from core import bus
        minor = self.build.python_minor(self.v)
        fetch = load("fetch")
        for plat in self.build.PLATFORMS:
            for p in fetch.parts(self.v, plat):
                item = self.v[p["key"]]
                if not item.get("wheel"):
                    continue
                with self.subTest(part=p["key"], platform=plat):
                    self.assertIsNone(self.build.wheels.fits(p["name"], plat, minor))
                    version = item["files"][plat].get("version") or item["version"]
                    self.assertTrue(p["name"].startswith(f"{p['key']}-{version}-"), p["name"])
                    self.assertTrue(item.get("license"))
                    if p["key"] == "cryptography":
                        self.assertGreaterEqual(self.build.wheels.version_of(version), bus.MIN_LIB)
        self.assertGreaterEqual(self.v["runtime"], 2)   # the environment that carries it is a new one

    def test_the_environment_number_covers_what_the_code_needs(self):
        from core import version
        self.assertIsInstance(self.v["runtime"], int)
        self.assertGreaterEqual(self.v["runtime"], version.RUNTIME_MIN)

    def test_ffmpeg_comes_from_builds_that_stay_and_without_gpl(self):
        # BtbN keeps 14 daily builds and the last build of every month for two years (2026-10-06 was gone by 11-01)
        for plat, pin in self.v["ffmpeg"]["files"].items():
            with self.subTest(platform=plat):
                m = re.search(r"/autobuild-(\d{4})-(\d\d)-(\d\d)-\d\d-\d\d/", pin["url"])
                self.assertTrue(m, pin["url"])
                y, mo, d = map(int, m.groups())
                self.assertEqual(d, calendar.monthrange(y, mo)[1], "not the last build of a month")
                self.assertIn("-lgpl", pin["url"])
        for plat in ("mac-arm64", "mac-x64"):
            configure = self.v["ffmpeg"]["build"][plat]["configure"]
            self.assertFalse({"--enable-gpl", "--enable-nonfree", "--enable-version3"} & set(configure))
            self.assertEqual(self.v["ffmpeg"]["build"][plat]["source"], self.v["ffmpeg"]["build"]["mac-arm64"]["source"])
        # an Intel Mac's ffmpeg is built on the arm Mac bridge: compiled and linked for x86_64, run under Rosetta
        x64 = self.v["ffmpeg"]["build"]["mac-x64"]["configure"]
        self.assertIn("--arch=x86_64", x64)
        self.assertIn("--enable-cross-compile", x64)
        self.assertTrue(any(c.startswith("--extra-cflags=") and "-arch x86_64" in c for c in x64), x64)
        self.assertTrue(any(c.startswith("--extra-ldflags=") and "-arch x86_64" in c for c in x64), x64)

    def test_git_on_windows_is_the_full_one_with_bash(self):
        # S0-2: MinGit and dugite have no bash — Claude Code finds no shell
        name = os.path.basename(self.v["git"]["files"]["windows-x64"]["url"])
        self.assertRegex(name, r"^Git-[\d.]+-64-bit\.tar\.bz2$")
        name = os.path.basename(self.v["git"]["files"]["windows-arm64"]["url"])
        self.assertRegex(name, r"^Git-[\d.]+-arm64\.tar\.bz2$")

    def test_the_trimmed_git_for_windows_keeps_what_sessions_and_the_hub_run(self):
        # S6: docs, vim's documentation and copies, Tcl/Tk with git-gui and gitk go; nothing a session's bash, git
        # over https, LFS, perl one-liners or the credential manager of a person's own pushes need. The ARM64 build is
        # the same tree with its native part in clangarm64/ instead of ucrt64/ (msys's usr/ stays x64, emulated)
        for plat, native in (("windows-x64", "ucrt64"), ("windows-arm64", "clangarm64")):
            drop = self.build.for_platform(self.v["git"]["drop"], plat)
            gone = lambda name: any(fnmatch.fnmatchcase(name, d) for d in drop)   # noqa: E731
            here = lambda name: name.replace("ucrt64/", native + "/")              # noqa: E731
            for keep in ("bin/bash.exe", "usr/bin/bash.exe", "usr/bin/sh.exe", "cmd/git.exe", "ucrt64/bin/git.exe",
                         "ucrt64/libexec/git-core/git-remote-https.exe", "ucrt64/libexec/git-core/git-upload-pack.exe",
                         "ucrt64/bin/git-lfs.exe", "ucrt64/bin/git-credential-manager.exe", "usr/bin/perl.exe",
                         "usr/lib/perl5/core_perl/CORE/msys-perl5_42.dll", "usr/bin/vim.exe", "usr/bin/vi",
                         "usr/share/vim/vim92/defaults.vim", "usr/bin/msys-2.0.dll", "usr/share/misc/magic.mgc",
                         "etc/gitconfig", "ucrt64/bin/git-askpass.exe", "ucrt64/libexec/git-core/git-submodule"):
                with self.subTest(platform=plat, keep=keep):
                    self.assertFalse(gone(here(keep)), here(keep))
            for away in ("ucrt64/share/doc/git-doc/git.html", "usr/share/vim/vim92/doc/help.txt", "usr/bin/view.exe",
                         "ucrt64/lib/tk8.6/tk.tcl", "ucrt64/bin/wish.exe", "ucrt64/libexec/git-core/git-gui",
                         "ucrt64/bin/gitk", "ucrt64/bin/git-update-git-for-windows"):
                with self.subTest(platform=plat, away=away):
                    self.assertTrue(gone(here(away)), here(away))
            self.assertEqual(sorted(drop), sorted(here(d) for d in self.build.for_platform(self.v["git"]["drop"],
                                                                                           "windows-x64")))
        for plat in ("mac-arm64", "mac-x64"):
            self.assertIsNone(self.build.for_platform(self.v["git"]["drop"], plat))   # dugite stays as it is
        self.assertEqual(self.v["git"]["msys"], ["dev/*", "etc/mtab"])

    def test_inno_setup_is_a_pinned_build_tool_not_a_part(self):
        fetch = load("fetch")
        self.assertTrue(self.v["inno-setup"]["tool"])
        self.assertFalse([p for p in fetch.parts(self.v, "windows-x64") if p["key"] == "inno-setup"])
        self.assertEqual([p["key"] for p in fetch.parts(self.v, "windows-x64", tools=True)], ["inno-setup"])


def tgz(path, files, mode="w:gz", links=None, hard=None, dirs=()):
    """files: {name: (bytes, mode)}; links — symbolic, hard — hard links {name: target}; dirs — folders"""
    with tarfile.open(path, mode) as t:
        for name in dirs:
            info = tarfile.TarInfo(name)
            info.type, info.mode = tarfile.DIRTYPE, 0o755
            t.addfile(info)
        for name, (data, m) in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), m
            t.addfile(info, io.BytesIO(data))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type, info.linkname = tarfile.SYMTYPE, target
            t.addfile(info)
        for name, target in (hard or {}).items():
            info = tarfile.TarInfo(name)
            info.type, info.linkname = tarfile.LNKTYPE, target
            t.addfile(info)


def zipped(path, files):
    with zipfile.ZipFile(path, "w") as z:
        for name, (data, m) in files.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFREG | m) << 16
            z.writestr(info, data)


class MacBuild(unittest.TestCase):
    """The Mac parts of the factory that run only on a Mac, checked through their calls."""

    def setUp(self):
        self.build = load("build")

    def test_a_program_that_cannot_start_is_a_refusal_not_a_traceback(self):
        # an x86_64 ffmpeg on an arm Mac without Rosetta: "Bad CPU type in executable" is an OSError of exec
        with self.assertRaisesRegex(self.build.Refused, "cannot start"):
            self.build.run([os.path.join(tempfile.gettempdir(), "no-such-program-o0")])

    def test_the_built_ffmpeg_is_for_the_platform_it_goes_into(self):
        # cross-compiled on the arm Mac bridge: a configure line without -arch would quietly give an arm64 binary; the
        # check reads the Mach-O header, so a build brought from a Mac is checked on the server too
        d = tempfile.mkdtemp(prefix="hub-arch-")
        self.addCleanup(platforms.current.rmtree, d)

        def made(name, data):
            path = os.path.join(d, name)
            with open(path, "wb") as f:
                f.write(data)
            return path
        arm, x64 = made("arm", ARM_MACHO), made("x64", X64_MACHO)
        fat, script = made("fat", macho(0x07, 0x01000007)), made("script", b"#!/bin/sh\n")
        with self.assertRaisesRegex(self.build.Refused, "arm is built for arm64, and the platform needs x86_64"):
            self.build.check_arch(arm, ["--arch=x86_64"])
        self.build.check_arch(arm, ["--arch=arm64"])
        self.build.check_arch(x64, ["--enable-cross-compile", "--arch=x86_64"])
        with self.assertRaisesRegex(self.build.Refused, "arm64"):
            self.build.check_arch(x64, ["--arch=arm64"])
        with self.assertRaisesRegex(self.build.Refused, "x86 x86_64"):           # a fat binary is not one platform's
            self.build.check_arch(fat, ["--arch=x86_64"])
        with self.assertRaisesRegex(self.build.Refused, "built for \\?"):           # not a program at all
            self.build.check_arch(script, ["--arch=arm64"])
        self.build.check_arch(script, ["--disable-doc"])                         # no --arch: nothing to check

    def test_an_intel_build_is_smoke_run_on_an_arm_mac_with_rosetta(self):
        cases = [("mac-x64", "mac-x64", None, True), ("mac-x64", "mac-arm64", True, True),
                 ("mac-x64", "mac-arm64", False, False), ("mac-arm64", "mac-x64", True, False),
                 ("windows-arm64", "linux-x64", True, False), ("linux-x64", "linux-x64", None, True)]
        for plat, here, rosetta, want in cases:
            with self.subTest(plat=plat, host=here, rosetta=rosetta), \
                    mock.patch.object(self.build, "host", lambda: here), \
                    mock.patch.object(self.build, "rosetta", lambda: rosetta):
                self.assertIs(self.build.runs_here(plat), want)


def elf(machine):
    return b"\x7fELF\x02\x01\x01" + bytes(9) + b"\x02\x00" + machine.to_bytes(2, "little") + bytes(40)


def macho(*cpus):
    if len(cpus) == 1:
        return b"\xcf\xfa\xed\xfe" + cpus[0].to_bytes(4, "little") + bytes(24)
    return (b"\xca\xfe\xba\xbe" + len(cpus).to_bytes(4, "big")
            + b"".join(c.to_bytes(4, "big") + bytes(16) for c in cpus) + bytes(64))


def pe(machine, managed=False):
    """A PE file; managed — a .NET assembly: a PE32+ optional header whose CLR runtime header (directory 14) is there."""
    coff = machine.to_bytes(2, "little") + bytes(18)
    opt = (0x20B).to_bytes(2, "little") + bytes(106) + (16).to_bytes(4, "little")
    dirs = b"".join((8 if managed and i == 14 else 0).to_bytes(4, "little") * 2 for i in range(16))
    return b"MZ" + bytes(58) + (64).to_bytes(4, "little") + b"PE\x00\x00" + coff + opt + dirs + bytes(64)


X64_ELF, ARM_ELF, ARM_MACHO, X64_MACHO, X64_PE, ARM_PE = (elf(0x3E), elf(0xB7), macho(0x0100000C), macho(0x01000007),
                                                          pe(0x8664), pe(0xAA64))


class Programs(unittest.TestCase):
    """Every program of a build is the platform's by its header (the O7 check of each build's sheet): on any machine,
    without lipo or file."""

    def setUp(self):
        self.build = load("build")
        self.top = tempfile.mkdtemp(prefix="hub-arch-")
        self.addCleanup(platforms.current.rmtree, self.top)

    def lay(self, files):
        for rel, data in files.items():
            path = os.path.join(self.top, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)

    def test_a_program_is_known_by_its_header(self):
        cases = {"a": (X64_ELF, ("elf", {"x86_64"})), "b": (ARM_ELF, ("elf", {"arm64"})),
                 "c": (ARM_MACHO, ("macho", {"arm64"})), "d": (macho(0x01000007, 0x0100000C), ("macho", {"x86_64", "arm64"})),
                 "e": (X64_PE, ("pe", {"x86_64"})), "f": (pe(0xA64E), ("pe", {"arm64"})), "g": (pe(0x14C), ("pe", {"x86"})),
                 "l": (pe(0x14C, managed=True), ("clr", {"any"})), "m": (pe(0xFD1D, managed=True), ("clr", {"any"})),
                 "h": (b"\xca\xfe\xba\xbe\x00\x00\x00\x3d" + bytes(64), None),   # a Java class file
                 "i": (b"#!/bin/sh\n", None), "j": (b"MZ" + bytes(10), None), "k": (b"", None)}
        self.lay({n: d for n, (d, _) in cases.items()})
        for name, (_, want) in cases.items():
            self.assertEqual(self.build.arch_of(os.path.join(self.top, name)), want, name)

    def test_each_platform_takes_only_its_own_programs(self):
        own = {"linux-x64": X64_ELF, "linux-arm64": ARM_ELF, "mac-arm64": ARM_MACHO, "mac-x64": X64_MACHO,
               "windows-x64": X64_PE, "windows-arm64": ARM_PE}
        for plat, good in own.items():
            for other, bad in own.items():
                with self.subTest(plat=plat, other=other):
                    platforms.current.rmtree(self.top)
                    self.lay({"runtime/python/bin/python3": good, "runtime/a/b": bad, "runtime/s.sh": b"#!/bin/sh\n"})
                    if other == plat:
                        got = self.build.check_programs(self.top, plat)
                        self.assertEqual(got["programs"], 2)
                        self.assertEqual(got["emulated"], {})
                    else:
                        with self.assertRaisesRegex(self.build.Refused, "runtime/a/b"):
                            self.build.check_programs(self.top, plat)

    def test_a_universal_mac_program_is_taken_when_it_carries_the_platform(self):
        self.lay({"runtime/node/bin/node": macho(0x01000007, 0x0100000C)})
        self.assertEqual(self.build.check_programs(self.top, "mac-arm64")["programs"], 1)
        self.assertEqual(self.build.check_programs(self.top, "mac-x64")["programs"], 1)
        platforms.current.rmtree(self.top)
        self.lay({"runtime/node/bin/node": macho(0x01000007, 0x07)})
        with self.assertRaisesRegex(self.build.Refused, "x86,x86_64"):
            self.build.check_programs(self.top, "mac-arm64")

    def test_windows_on_arm_counts_the_x64_msys_half_of_git_and_refuses_any_other_x64(self):
        self.lay({"runtime/git/usr/bin/bash.exe": X64_PE, "runtime/git/usr/bin/msys-2.0.dll": X64_PE,
                  "runtime/git/clangarm64/bin/git.exe": ARM_PE, "runtime/python/python.exe": ARM_PE})
        got = self.build.check_programs(self.top, "windows-arm64")
        self.assertEqual(got, {"format": "pe", "want": "arm64", "programs": 4, "emulated": {"runtime/git/usr/": 2},
                               "data": 0})
        with self.assertRaisesRegex(self.build.Refused, "runtime/git/usr/bin/bash.exe"):
            self.build.check_programs(self.top, "linux-arm64")
        self.lay({"runtime/node/node.exe": X64_PE})
        with self.assertRaisesRegex(self.build.Refused, "runtime/node/node.exe"):
            self.build.check_programs(self.top, "windows-arm64")
        platforms.current.rmtree(self.top)
        self.lay({"runtime/git/usr/libexec/getprocaddr32.exe": pe(0x14C), "runtime/python/vcruntime140_1.dll": X64_PE})
        self.assertEqual(self.build.check_programs(self.top, "windows-arm64")["emulated"],
                         {"runtime/git/usr/": 1, "runtime/python/vcruntime140_1.dll": 1})
        self.lay({"runtime/git/usr/bin/bash.exe": pe(0xAA64)})   # emulated is x64/x86 only — an ARM bash is its own
        self.assertEqual(self.build.check_programs(self.top, "windows-arm64")["programs"], 3)
        for plat, rel in (("windows-x64", "runtime/git/usr/libexec/getprocaddr32.exe"),):
            platforms.current.rmtree(self.top)
            self.lay({rel: pe(0x14C), "runtime/git/usr/bin/other32.exe": pe(0x14C)})   # x86 named, not the folder
            with self.assertRaisesRegex(self.build.Refused, "other32.exe") as cm:
                self.build.check_programs(self.top, plat)
            self.assertNotIn("getprocaddr32", str(cm.exception))

    def test_dotnet_assemblies_and_pip_s_windows_launchers_are_data_not_programs(self):
        self.lay({"runtime/git/libexec/git-core/GitHub.dll": pe(0x14C, managed=True),
                  "runtime/python/lib/python3.14/site-packages/pip/_vendor/distlib/t64-arm.exe": ARM_PE,
                  "runtime/python/bin/python3": X64_MACHO, "runtime/python/lib/other/t64-arm.exe": ARM_PE})
        with self.assertRaisesRegex(self.build.Refused, "lib/other/t64-arm.exe") as cm:
            self.build.check_programs(self.top, "mac-x64")
        self.assertNotIn("distlib", str(cm.exception))
        os.remove(os.path.join(self.top, "runtime/python/lib/other/t64-arm.exe"))
        got = self.build.check_programs(self.top, "mac-x64")
        self.assertEqual((got["programs"], got["data"]), (1, 2))


EXE = b"#!/bin/sh\necho ok\n"


# the factory and the upload run on the server and the Mac bridge (Setup.exe is made by tools/package/windows/setup_exe.py
# from their result); their tests stand on POSIX programs (shell-script gh, file:// paths) — a Windows hub's self-check
# would be red on them (winstand: 16 of 27 on the commit of S4)
POSIX_ONLY = unittest.skipIf(os.name == "nt", "the release factory runs on the server and the Mac, not on Windows")


@POSIX_ONLY
class Factory(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="hub-factory-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.build = load("build")
        self.src = os.path.join(self.root, "src")
        os.makedirs(self.src)
        self.cache = os.path.join(self.root, "cache")
        self.out = os.path.join(self.root, "out")
        self.repo = self.shared_repo()

    def shared_repo(self, runtime_min=1):
        up = os.path.join(self.root, f"up-{runtime_min}.git")
        g(self.root, "init", "-q", "--bare", "-b", "main", up)
        work = os.path.join(self.root, f"w-{runtime_min}")
        g(self.root, "clone", "-q", up, work)
        for rel, text in {"core/__init__.py": "", "core/version.py": f"RUNTIME_MIN = {runtime_min}\n",
                          "core/x.py": "X = 1\n", "tests/test_x.py": "T = 1\n", "bin/hub-up": "print(1)\n",
                          ".gitignore": "__pycache__/\n*.pyc\n"}.items():
            os.makedirs(os.path.dirname(os.path.join(work, rel)), exist_ok=True)
            with open(os.path.join(work, rel), "w") as f:
                f.write(text)
        g(work, "add", "-A")
        g(work, "commit", "-qm", "v1")
        g(work, "tag", "-a", "-m", "v1.2.3", "v1.2.3")
        g(work, "push", "-q", "origin", "main", "v1.2.3")
        return up

    def pin(self, name):
        path = os.path.join(self.src, name)
        return {"url": "file://" + path, "sha256": sha(path), "size": os.path.getsize(path)}

    def versions(self, plat):
        win = plat.startswith("windows")
        tgz(os.path.join(self.src, "py.tar.gz"),
            {f"python/{'python.exe' if win else 'bin/python3'}": (EXE, 0o755), "python/LICENSE.txt": (b"PSF", 0o644),
             ("python/Lib/site-packages/README.txt" if win else "python/lib/python%d.%d/site-packages/README.txt"
              % sys.version_info[:2]): (b"site", 0o644)})
        tgz(os.path.join(self.src, "node.tar.xz"), {f"node-v1-x/{'node.exe' if win else 'bin/node'}": (EXE, 0o755),
                                                    "node-v1-x/LICENSE": (b"MIT", 0o644)}, "w:xz")
        if win:
            zipped(os.path.join(self.src, "gh.zip"), {"bin/gh.exe": (EXE, 0o755), "LICENSE": (b"MIT", 0o644)})
            zipped(os.path.join(self.src, "ff.zip"), {
                "ffmpeg-x/bin/ffmpeg.exe": (EXE, 0o755), "ffmpeg-x/bin/ffprobe.exe": (EXE, 0o755),
                "ffmpeg-x/bin/ffplay.exe": (EXE, 0o755), "ffmpeg-x/bin/avcodec-62.dll": (b"d", 0o644),
                "ffmpeg-x/include/a.h": (b"h", 0o644), "ffmpeg-x/LICENSE.txt": (b"LGPL", 0o644)})
            # Git for Windows carries msys links to /proc (dev/fd, etc/mtab) and its device folders, hard links
            # (git.exe in libexec), documentation and the GUI that the hub does not run
            tgz(os.path.join(self.src, "git.tar.bz2"), {
                "./bin/bash.exe": (EXE, 0o755), "./usr/bin/bash.exe": (EXE, 0o755), "./cmd/git.exe": (EXE, 0o755),
                "./ucrt64/bin/git.exe": (EXE + b"git", 0o755), "./etc/gitconfig": (b"[core]\n\tautocrlf = true\n", 0o644),
                "./ucrt64/share/doc/git-doc/git.html": (b"<html>", 0o644), "./ucrt64/lib/tk8.6/tk.tcl": (b"tk", 0o644),
                "./usr/bin/vim.exe": (EXE, 0o755), "./usr/bin/view.exe": (EXE, 0o755)}, "w:bz2",
                links={"./dev/fd": "/proc/self/fd", "./etc/mtab": "/proc/mounts"},
                hard={"./ucrt64/libexec/git-core/git.exe": "./ucrt64/bin/git.exe"}, dirs=("./dev", "./dev/shm"))
        else:
            tgz(os.path.join(self.src, "gh.tar.gz"), {"gh_1_x/bin/gh": (EXE, 0o755), "gh_1_x/LICENSE": (b"MIT", 0o644)})
            tgz(os.path.join(self.src, "ff.tar.xz"), {
                "ffmpeg-x/bin/ffmpeg": (EXE, 0o755), "ffmpeg-x/bin/ffprobe": (EXE, 0o755),
                "ffmpeg-x/bin/ffplay": (EXE, 0o755), "ffmpeg-x/lib/libavcodec.so.62": (b"so", 0o644),
                "ffmpeg-x/lib/pkgconfig/x.pc": (b"pc", 0o644), "ffmpeg-x/include/a.h": (b"h", 0o644),
                "ffmpeg-x/doc/a.html": (b"d", 0o644), "ffmpeg-x/LICENSE.txt": (b"LGPL", 0o644)}, "w:xz")
        v = {"runtime": 1,
             "python": {"version": "%d.%d.8" % sys.version_info[:2], "license": "PSF",     # the build checks the minor
                        "files": {plat: self.pin("py.tar.gz")}},                           # of the python it is given
             "node": {"version": "24", "license": "MIT", "files": {plat: self.pin("node.tar.xz")}},
             "gh": {"version": "2", "license": "MIT", "files": {plat: self.pin("gh.zip" if win else "gh.tar.gz")}},
             "ffmpeg": {"version": "8.1", "license": "LGPL", "keep": self.build.fetch.load()["ffmpeg"]["keep"],
                        "files": {plat: self.pin("ff.zip" if win else "ff.tar.xz")}},
             "chrome": {"version": "155", "bundle": False, "files": {plat: {"url": "https://example.test/c.zip",
                                                                           "sha256": "0" * 64, "size": 1}}}}
        if win:
            real = self.build.fetch.load()["git"]
            v["git"] = {"version": "2.56", "license": "GPL-2.0", "msys": real["msys"], "drop": real["drop"],
                        "files": {plat: self.pin("git.tar.bz2")}}
        return v

    def run_build(self, plat, versions=None, **kw):
        kw.setdefault("skip", ("mcp",))
        return self.build.build(plat, self.out, repo=self.repo, tag="v1.2.3", origin="https://example.test/core.git",
                                issues="acme/hub-issues", cache=self.cache, versions=versions or self.versions(plat),
                                python=sys.executable, **kw)

    def test_a_linux_build_lays_out_the_environment_the_seed_and_the_mark(self):
        sheet = self.run_build("linux-x64")
        top = os.path.join(self.out, "agents-hub-v1.2.3-linux-x64")
        rt = os.path.join(top, "runtime")
        for rel in ("python/bin/python3", "node/bin/node", "gh/bin/gh", "ffmpeg/bin/ffmpeg", "ffmpeg/bin/ffprobe"):
            with self.subTest(rel):
                self.assertTrue(os.access(os.path.join(rt, rel), os.X_OK), rel)
        self.assertTrue(os.path.exists(os.path.join(rt, "ffmpeg", "lib", "libavcodec.so.62")))
        for rel in ("ffmpeg/bin/ffplay", "ffmpeg/include", "ffmpeg/doc", "ffmpeg/lib/pkgconfig"):
            self.assertFalse(os.path.exists(os.path.join(rt, rel)), rel)       # only what the hub calls
        self.assertFalse(os.path.exists(os.path.join(rt, "chrome")))           # downloaded at the first start
        self.assertFalse(os.path.exists(os.path.join(rt, "git")))               # Linux: the system git (Depends)
        seed = os.path.join(top, "code-seed")   # the code layer of the tag (app spec §3.1), not a git clone
        self.assertFalse(os.path.isdir(os.path.join(seed, ".git")))
        with open(os.path.join(seed, appcode.GIT_STOP[0]), "rb") as f:   # git stops at the layer's own file
            self.assertEqual(f.read(), appcode.GIT_STOP[1])
        man = appcode.load(seed)
        self.assertEqual((man["version"], man["flavor"], man["commit"]), ("1.2.3", "team", g(
            os.path.join(self.root, "w-1"), "rev-parse", "v1.2.3^{commit}")))
        self.assertEqual(appcode.diff(seed, man, full=True), [])
        pyc = [n for n in os.listdir(os.path.join(seed, "core", "__pycache__")) if n.startswith("x.")]
        self.assertEqual(len(pyc), 1)
        with open(os.path.join(seed, "core", "__pycache__", pyc[0]), "rb") as f:
            head = f.read(8)
        self.assertEqual(int.from_bytes(head[4:8], "little"), 0b11)          # checked-hash: an edit is compiled again
        self.assertFalse(os.path.exists(os.path.join(seed, "tests")))           # the layer has no tests
        with open(os.path.join(top, "PACKAGED")) as f:
            mark = json.load(f)
        self.assertEqual(mark, {"format": "app", "layout": "layer", "flavor": "team", "version": "1.2.3", "runtime": 1,
                                "platform": "linux-x64", "swaps": ["folder"], "issues": "acme/hub-issues"})
        with open(os.path.join(top, "appcode.py")) as f, open(appcode.__file__) as want:   # launch.py's own copy
            self.assertEqual(f.read(), want.read())
        self.assertEqual(sheet["layer"]["name"], "1.2.3-team")
        self.assertTrue(os.path.isfile(os.path.join(self.out, "code-1.2.3-team.tar.gz")))   # the layer alone, for updates
        with open(os.path.join(top, "licenses", "VERSIONS.json")) as f:
            told = json.load(f)
        self.assertEqual(told["parts"]["ffmpeg"]["sha256"], self.versions("linux-x64")["ffmpeg"]["files"]["linux-x64"]
                         ["sha256"])
        self.assertEqual(told["code"]["tag"], "v1.2.3")
        self.assertIn("chrome", told["first_start"])
        self.assertTrue(os.path.exists(os.path.join(top, "licenses", "node", "LICENSE")))
        archive = os.path.join(self.out, "agents-hub-v1.2.3-linux-x64.tar.gz")
        self.assertEqual(sheet["archive"], {"name": os.path.basename(archive), "sha256": sha(archive),
                                            "size": os.path.getsize(archive)})
        with tarfile.open(archive) as t:
            names = t.getnames()
        self.assertIn("agents-hub-v1.2.3-linux-x64/code-seed/MANIFEST.json", names)
        self.assertIn("agents-hub-v1.2.3-linux-x64/runtime/python/bin/python3", names)
        with open(os.path.join(self.out, "agents-hub-v1.2.3-linux-x64.json")) as f:
            self.assertEqual(json.load(f), sheet)
        self.assertEqual(sheet["parts"]["python"]["download"], os.path.getsize(os.path.join(self.src, "py.tar.gz")))
        self.assertGreater(sheet["parts"]["python"]["disk"], 0)
        self.assertEqual(sheet["parts"]["ffmpeg"]["files"], 4)                 # ffmpeg, ffprobe, libavcodec, LICENSE
        self.assertEqual(sheet["paths"]["ffmpeg"], "runtime/ffmpeg/bin/ffmpeg")
        self.assertEqual(sheet["paths"]["launch"], "launch.py")                  # the shell's copy, not the seed's
        with open(os.path.join(top, "paths.json")) as f:                         # the shell reads its programs here
            self.assertEqual(json.load(f), sheet["paths"])
        with open(os.path.join(top, "launch.py")) as f, open(os.path.join(PKG, "launch.py")) as want:
            self.assertEqual(f.read(), want.read())
        self.assertEqual(sheet["arch"], {"format": "elf", "want": "x86_64", "programs": 0, "emulated": {}, "data": 0})

    def with_wheel(self, plat, tag="cp311-abi3-manylinux2014_x86_64.manylinux_2_17_x86_64", version="50.0.2",
                   inside=None, extra=None, v=None):
        """versions of plat with a cryptography wheel (a "wheel" part: into the environment's Python)."""
        v = v or self.versions(plat)
        name = f"cryptography-{version}-{tag}.whl"
        info = f"cryptography-{inside or version}.dist-info"
        zipped(os.path.join(self.src, name), {
            "cryptography/__init__.py": (f'__version__ = "{version}"\n'.encode(), 0o644),
            f"{info}/METADATA": (f"Metadata-Version: 2.4\nName: cryptography\nVersion: {inside or version}\n".encode(),
                                 0o644),
            f"{info}/WHEEL": (b"Wheel-Version: 1.0\nRoot-Is-Purelib: false\n", 0o644),
            f"{info}/licenses/LICENSE.APACHE": (b"Apache", 0o644), **(extra or {})})
        v["cryptography"] = {"version": "50.0.2", "license": "Apache-2.0 OR BSD-3-Clause", "wheel": True,
                             "files": {plat: {**self.pin(name), **({} if version == "50.0.2" else {"version": version})}}}
        return v

    def test_the_bus_library_goes_into_the_environment_s_python(self):
        # 08.11: an environment without cryptography leaves the hub bus of every one-line hub broken (no_lib)
        sheet = self.run_build("linux-x64", versions=self.with_wheel("linux-x64"))
        top = os.path.join(self.out, "agents-hub-v1.2.3-linux-x64")
        site = os.path.join(top, "runtime", "python", "lib", "python%d.%d" % sys.version_info[:2], "site-packages")
        self.assertTrue(os.path.isfile(os.path.join(site, "cryptography", "__init__.py")))
        self.assertTrue(os.path.isfile(os.path.join(site, "cryptography-50.0.2.dist-info", "METADATA")))
        self.assertFalse(os.path.exists(os.path.join(top, "runtime", "cryptography")))   # not a part folder of its own
        with open(os.path.join(top, "licenses", "VERSIONS.json")) as f:
            told = json.load(f)["parts"]["cryptography"]
        self.assertEqual((told["version"], told["sha256"]), ("50.0.2", self.with_wheel("linux-x64")["cryptography"]
                                                              ["files"]["linux-x64"]["sha256"]))
        self.assertTrue(os.path.isfile(os.path.join(top, "licenses", "cryptography", "licenses", "LICENSE.APACHE")))
        self.assertEqual(sheet["parts"]["cryptography"]["version"], "50.0.2")
        self.assertEqual(sheet["parts"]["cryptography"]["files"], 4)

    def test_the_wheel_s_own_version_of_a_platform_is_the_one_told(self):
        # an Intel Mac: cryptography 49 dropped its wheels — 48.0.1 there, pinned by its own "version"
        sheet = self.run_build("linux-x64", versions=self.with_wheel("linux-x64", version="48.0.1"))
        self.assertEqual(sheet["parts"]["cryptography"]["version"], "48.0.1")

    def test_a_windows_build_puts_the_library_into_lib_site_packages(self):
        self.run_build("windows-x64", versions=self.with_wheel("windows-x64", tag="cp311-abi3-win_amd64"))
        site = os.path.join(self.out, "agents-hub-v1.2.3-windows-x64", "runtime", "python", "Lib", "site-packages")
        self.assertTrue(os.path.isfile(os.path.join(site, "cryptography", "__init__.py")))

    def test_a_wheel_not_for_the_platform_its_python_or_its_pin_stops_the_build(self):
        minor = "%d%d" % sys.version_info[:2]
        bad = {"another OS": dict(tag="cp311-abi3-win_amd64"),
               "another processor": dict(tag="cp311-abi3-manylinux2014_aarch64.manylinux_2_17_aarch64"),
               "musl": dict(tag="cp311-abi3-musllinux_1_2_x86_64"),
               "a newer Python": dict(tag=f"cp{int(minor) + 1}-abi3-manylinux2014_x86_64"),
               "another Python's ABI": dict(tag=f"cp{int(minor) + 1}-cp{int(minor) + 1}-manylinux2014_x86_64"),
               "free-threaded": dict(tag=f"cp{minor}-cp{minor}t-manylinux2014_x86_64"),
               "a newer glibc than the install admits": dict(tag="cp311-abi3-manylinux_2_34_x86_64"),
               "a pure wheel of a newer Python": dict(tag=f"py3{int(minor[1:]) + 1}-none-any"),
               "other bytes inside": dict(inside="49.0.0"),
               "a .data folder": dict(extra={"cryptography-50.0.2.data/scripts/x": (b"#!/bin/sh\n", 0o755)}),
               "a path outside": dict(extra={"../x.py": (b"x", 0o644)})}
        for why, kw in bad.items():
            with self.subTest(why):
                with self.assertRaises(self.build.Refused):
                    self.run_build("linux-x64", versions=self.with_wheel("linux-x64", **kw))
                self.assertFalse(os.path.exists(os.path.join(self.out, "agents-hub-v1.2.3-linux-x64")))

    def test_the_smoke_run_imports_the_bus_library_with_the_environment_s_python(self):
        seen = []
        real = self.build.run

        def run(cmd, *a, **kw):
            seen.append(cmd)
            return real(cmd, *a, **kw)
        with mock.patch.object(self.build, "runs_here", return_value=True), mock.patch.object(self.build, "run", run):
            self.run_build("linux-x64", versions=self.with_wheel("linux-x64"))
        probe = [c for c in seen if c[0].endswith("runtime/python/bin/python3") and "hpke" in " ".join(c)]
        self.assertEqual(len(probe), 1, seen)
        self.assertIn("-I", probe[0])

    def test_a_part_with_a_program_of_another_platform_stops_the_build(self):
        v = self.versions("linux-x64")
        tgz(os.path.join(self.src, "gh.tar.gz"), {"gh_1_x/bin/gh": (ARM_MACHO, 0o755), "gh_1_x/LICENSE": (b"MIT", 0o644)})
        v["gh"]["files"]["linux-x64"] = self.pin("gh.tar.gz")      # the Mac's gh pinned for Linux by mistake
        with self.assertRaisesRegex(self.build.Refused, r"runtime/gh/bin/gh \(macho arm64\)"):
            self.run_build("linux-x64", versions=v)
        self.assertFalse(os.path.exists(self.out) and os.listdir(self.out))

    def test_the_team_shell_names_where_reports_of_failed_updates_go(self):
        # app spec §7.1: the DSN goes into the shell's deliver.json beside the server and the build key — never a layer
        deliver = {"url": "https://deliver.example.test", "key": "a-build-key-0000000",
                   "report": "https://0123456789abcdef@sentry.example.test/42"}
        self.run_build("linux-x64", deliver=dict(deliver))
        top = os.path.join(self.out, "agents-hub-v1.2.3-linux-x64")
        with open(os.path.join(top, "deliver.json")) as f:
            self.assertEqual(json.load(f), deliver)
        for d, _, names in os.walk(os.path.join(top, "code-seed")):
            for n in names:
                with open(os.path.join(d, n), "rb") as f:
                    self.assertNotIn(b"sentry.example.test", f.read(), n)
        for bad in ("http://0123456789abcdef@sentry.example.test/42", "https://sentry.example.test/42", "x"):
            with self.assertRaises(self.build.Refused, msg=bad):
                self.run_build("linux-x64", deliver={**deliver, "report": bad})

    def test_a_tag_sharing_its_commit_with_another_tag_is_still_the_tag(self):
        work = os.path.join(self.root, "w-1")
        g(work, "tag", "-a", "-m", "x", "a-later-name")                        # the same commit, another name
        g(work, "push", "-q", "origin", "a-later-name")
        self.run_build("linux-x64")
        seed = os.path.join(self.out, "agents-hub-v1.2.3-linux-x64", "code-seed")
        self.assertEqual(appcode.load(seed)["commit"], g(work, "rev-parse", "v1.2.3^{commit}"))

    def test_the_environment_alone_has_no_seed_mark_or_archive(self):
        sheet = self.build.build("linux-x64", self.out, runtime_only=True, cache=self.cache, skip=("mcp",),
                                 versions=self.versions("linux-x64"))
        top = os.path.join(self.out, "agents-hub-runtime-linux-x64")
        self.assertTrue(os.path.isdir(os.path.join(top, "runtime", "python")))
        for name in ("code-seed", "PACKAGED"):
            self.assertFalse(os.path.exists(os.path.join(top, name)), name)
        self.assertIsNone(sheet["archive"])
        with open(os.path.join(self.out, "agents-hub-runtime-linux-x64.tar.gz"), "w") as f:
            f.write("an earlier build's archive")
        self.build.build("linux-x64", self.out, runtime_only=True, cache=self.cache, skip=("mcp",),
                         versions=self.versions("linux-x64"))
        self.assertFalse(os.path.exists(os.path.join(self.out, "agents-hub-runtime-linux-x64.tar.gz")))   # stale
        self.assertEqual(sorted(os.listdir(self.out)), ["agents-hub-runtime-linux-x64",
                                                        "agents-hub-runtime-linux-x64.json"])

    def test_a_branch_named_like_a_tag_is_not_a_release(self):
        work = os.path.join(self.root, "w-1")
        g(work, "push", "-q", "origin", "main:refs/heads/v2.0.0")
        with self.assertRaisesRegex(self.build.Refused, "is not a tag"):
            self.build.build("linux-x64", self.out, repo=self.repo, tag="v2.0.0", origin="https://example.test/c.git",
                             cache=self.cache, versions=self.versions("linux-x64"), python=sys.executable,
                             skip=("mcp",))

    def test_what_git_clones(self):
        for repo in ("https://example.test/core.git", "ssh://git@example.test/core.git", "git@example.test:acct/core.git"):
            self.assertEqual(self.build.clone_url(repo), repo)
        self.assertEqual(self.build.clone_url("/srv/core.git"), "file:///srv/core.git")

    def test_a_github_repo_gives_the_link_to_a_newer_app(self):
        self.assertEqual(self.build.download_of("https://github.com/acme/core.git"),
                         "https://github.com/acme/core/releases/latest")
        self.assertIsNone(self.build.download_of("/srv/core.git"))

    def test_a_windows_build_brings_git_with_bash_and_keeps_no_links(self):
        self.run_build("windows-x64")
        rt = os.path.join(self.out, "agents-hub-v1.2.3-windows-x64", "runtime")
        for rel in ("git/usr/bin/bash.exe", "git/cmd/git.exe", "gh/bin/gh.exe", "node/node.exe",
                    "python/python.exe", "ffmpeg/bin/ffmpeg.exe", "ffmpeg/bin/avcodec-62.dll"):
            self.assertTrue(os.path.exists(os.path.join(rt, rel)), rel)
        self.assertFalse(os.path.exists(os.path.join(rt, "ffmpeg", "bin", "ffplay.exe")))
        self.assertFalse(os.path.lexists(os.path.join(rt, "git", "dev", "fd")))
        top = os.path.dirname(rt)
        with open(os.path.join(top, "links.json")) as f:
            links = json.load(f)
        # the shell makes them again on Windows (tools/package/windows/app.py): msys's own link files and hard links
        self.assertEqual(links["msys"], {"runtime/git/dev/fd": "/proc/self/fd", "runtime/git/etc/mtab": "/proc/mounts",
                                         "runtime/git/dev/shm": None})
        self.assertEqual(links["hard"], {"runtime/git/ucrt64/libexec/git-core/git.exe": "runtime/git/ucrt64/bin/git.exe"})
        self.assertFalse(os.path.lexists(os.path.join(rt, "git", "ucrt64", "libexec", "git-core", "git.exe")))
        for rel in ("ucrt64/share/doc/git-doc/git.html", "ucrt64/lib/tk8.6", "usr/bin/view.exe"):
            self.assertFalse(os.path.lexists(os.path.join(rt, "git", rel)), rel)   # docs, GUI, copies of vim
        self.assertTrue(os.path.exists(os.path.join(rt, "git", "usr", "bin", "vim.exe")))
        with open(os.path.join(rt, "git", "etc", "gitconfig")) as f:
            got = f.read()
        self.assertTrue(got.startswith("[core]\n\tautocrlf = true\n"), got)      # Git for Windows' own settings stay
        cfg = os.path.join(self.root, "probe.gitconfig")
        with open(cfg, "w") as f:
            f.write(got)
        self.assertEqual(subprocess.run(["git", "config", "--file", cfg, "core.longpaths"], capture_output=True,
                                        text=True).stdout.strip(), "true")          # S0-8: paths over 260 characters
        for name in ("app.py", "launch.py", "app.ico"):
            self.assertTrue(os.path.exists(os.path.join(top, name)), name)
        with open(os.path.join(top, "app.ico"), "rb") as f:
            ico = f.read()
        self.assertEqual(ico[:4], b"\x00\x00\x01\x00")
        self.assertIn(256 % 256, [ico[6 + 16 * i] for i in range(int.from_bytes(ico[4:6], "little"))])
        with open(os.path.join(self.out, "agents-hub-v1.2.3-windows-x64.json")) as f:
            sheet = json.load(f)
        self.assertEqual(sheet["paths"]["bash"], "runtime/git/bin/bash.exe")      # what CLAUDE_CODE_GIT_BASH_PATH names
        self.assertEqual(sheet["paths"]["app"], "app.py")
        v = self.versions("windows-x64")
        tgz(os.path.join(self.src, "git.tar.bz2"), {"./bin/bash.exe": (EXE, 0o755)}, "w:bz2",
            links={"./bin/sh.exe": "bash.exe"})
        v["git"]["files"]["windows-x64"] = self.pin("git.tar.bz2")
        shutil.rmtree(self.out)
        with self.assertRaisesRegex(self.build.Refused, "sh.exe"):
            self.run_build("windows-x64", versions=v)       # Windows: a link would not unpack for a regular user

    def test_the_publisher_of_the_windows_signature_goes_into_the_mark(self):
        # the app puts a newer one in place only when it is validly signed by this publisher (shellswap.py, §4.4)
        self.run_build("windows-x64", publisher="Example Org")
        with open(os.path.join(self.out, "agents-hub-v1.2.3-windows-x64", "PACKAGED")) as f:
            self.assertEqual(json.load(f)["publisher"], "Example Org")
        for plat, name in (("linux-x64", "Example Org"), ("windows-x64", "Example\nOrg"), ("windows-x64", " Org")):
            with self.subTest(plat=plat, name=name), self.assertRaisesRegex(self.build.Refused, "--publisher"):
                self.build.build(plat, os.path.join(self.root, "other"), repo=self.repo, tag="v1.2.3", publisher=name)

    def test_a_download_that_does_not_match_its_pin_is_not_used(self):
        for field, value in (("sha256", "1" * 64), ("size", 7)):
            v = self.versions("linux-x64")
            v["node"]["files"]["linux-x64"][field] = value
            with self.subTest(field), self.assertRaises(self.build.Refused):
                self.run_build("linux-x64", versions=v)
            self.assertFalse([n for n in os.listdir(self.cache) if n.startswith("node")])   # nothing kept
            self.assertFalse(os.path.exists(self.out))

    def test_an_archive_that_reaches_outside_its_folder_is_refused(self):
        v = self.versions("linux-x64")
        tgz(os.path.join(self.src, "gh.tar.gz"), {"gh_1_x/bin/gh": (EXE, 0o755), "gh_1_x/../../evil": (b"x", 0o644)})
        v["gh"]["files"]["linux-x64"] = self.pin("gh.tar.gz")
        with self.assertRaises(self.build.Refused):
            self.run_build("linux-x64", versions=v)
        self.assertFalse(os.path.exists(os.path.join(self.root, "evil")))
        self.assertFalse(os.path.exists(os.path.join(self.out, "evil")))

    def test_the_code_of_the_tag_may_ask_for_a_newer_environment(self):
        self.repo = self.shared_repo(runtime_min=5)
        with self.assertRaisesRegex(self.build.Refused, "RUNTIME_MIN"):
            self.run_build("linux-x64")

    def test_bytecode_is_built_only_by_the_python_of_the_package(self):
        v = self.versions("linux-x64")
        v["python"]["version"] = "%d.%d.8" % (sys.version_info[0], sys.version_info[1] + 1)
        with self.assertRaisesRegex(self.build.Refused, "needs the same"):
            self.run_build("linux-x64", versions=v)

    def test_ffmpeg_for_a_mac_is_built_only_on_a_mac(self):
        v = self.versions("linux-x64")
        v = {k: ({**x, "files": {"mac-arm64": x["files"]["linux-x64"]}} if isinstance(x, dict) and "files" in x
                 else x) for k, x in v.items()}
        del v["ffmpeg"]["files"]
        v["ffmpeg"]["build"] = {"mac-arm64": {"source": self.pin("py.tar.gz"), "configure": ["--disable-doc"]}}
        if sys.platform != "darwin":
            with self.assertRaisesRegex(self.build.Refused, "Mac"):
                self.run_build("mac-arm64", versions=v)
        sheet = self.run_build("mac-arm64", versions=v, skip=("mcp", "ffmpeg"))
        self.assertEqual(sheet["skipped"], ["ffmpeg", "mcp"])                  # a test build says what it lacks

        def made(spec, cache, dest):                                           # what the Mac build leaves
            os.makedirs(os.path.join(dest, "bin"))
            for n in ("ffmpeg", "ffprobe"):                                    # on a Mac the build runs them
                with open(os.path.join(dest, "bin", n), "wb") as f:
                    f.write(EXE + f"# {n}\n".encode())
                os.chmod(os.path.join(dest, "bin", n), 0o755)
        shutil.rmtree(self.out)
        with mock.patch.object(self.build, "ffmpeg_mac", made):
            self.run_build("mac-arm64", versions=v)
        with open(os.path.join(self.out, "agents-hub-v1.2.3-mac-arm64", "licenses", "VERSIONS.json")) as f:
            told = json.load(f)["parts"]["ffmpeg"]
        self.assertEqual(told["source"], v["ffmpeg"]["build"]["mac-arm64"]["source"])
        self.assertEqual(told["built"], {f"bin/{n}": hashlib.sha256(EXE + f"# {n}\n".encode()).hexdigest()
                                         for n in ("ffmpeg", "ffprobe")})


HEADER = {"linux-x64": X64_ELF, "linux-arm64": ARM_ELF, "mac-arm64": ARM_MACHO, "mac-x64": X64_MACHO,
          "windows-x64": X64_PE, "windows-arm64": ARM_PE}


@POSIX_ONLY
class Release(unittest.TestCase):
    """tools/package/factory.py: one release of the one-line install — the six environments built here (the one part
    only a Mac makes, its ffmpeg, brought from the Mac's `mac-parts`), deliver.json of the team build sealed into each,
    one layer, the sheets checked, the plan."""
    setUp, shared_repo, pin, versions = Factory.setUp, Factory.shared_repo, Factory.pin, Factory.versions

    def all_versions(self, plats):
        """One versions.json for every platform: each one's parts under its own names, its ffmpeg library a program of
        its own processor (the parts that run in the smoke stay scripts)."""
        merged = {}
        for plat in plats:
            v = self.versions(plat)
            win = plat.startswith("windows")
            lib = "ffmpeg-x/bin/avcodec-62.dll" if win else "ffmpeg-x/lib/libavcodec.so.62"
            ff = os.path.join(self.src, "ff.zip" if win else "ff.tar.xz")
            files = {"ffmpeg-x/bin/ffmpeg" + (".exe" if win else ""): (EXE, 0o755),
                     "ffmpeg-x/bin/ffprobe" + (".exe" if win else ""): (EXE, 0o755), lib: (HEADER[plat], 0o644),
                     "ffmpeg-x/LICENSE.txt": (b"LGPL", 0o644)}
            zipped(ff, files) if win else tgz(ff, files, "w:xz")
            v["ffmpeg"]["files"][plat] = self.pin(os.path.basename(ff))
            for key, item in v.items():
                if not isinstance(item, dict) or not item.get("files"):
                    merged.setdefault(key, item)
                    continue
                part = item["files"][plat]
                if part["url"].startswith("file://"):
                    old = part["url"][len("file://"):]
                    new = f"{old}-{plat}{os.path.splitext(old)[1]}"
                    shutil.copyfile(old, new)
                    part = {"url": "file://" + new, "sha256": sha(new), "size": os.path.getsize(new)}   # portable: POSIX_ONLY class
                merged.setdefault(key, {**item, "files": {}})["files"][plat] = part
        return merged

    def setUp(self):  # noqa: F811 — Factory's, then the factory module
        Factory.setUp(self)
        spec = importlib.util.spec_from_file_location("hub_package_factory", os.path.join(PKG, "factory.py"))
        self.factory = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.factory)
        self.parts = os.path.join(self.root, "parts")
        self.v = self.all_versions(self.factory.PLATFORMS)

    def release(self, **kw):
        return self.factory.release("v1.2.3", self.out, self.repo, cache=self.cache, versions=self.v,
                                    python=sys.executable, **kw)

    def as_on_a_mac(self):
        """versions.json whose Mac ffmpeg is built from source, and what `mac-parts` leaves on a Mac for it: here from
        a Mac's cache laid out by hand (the build itself runs only on a Mac)."""
        mac_cache = os.path.join(self.root, "mac-cache")
        for plat, header in (("mac-arm64", ARM_MACHO), ("mac-x64", X64_MACHO)):
            del self.v["ffmpeg"]["files"][plat]
            arch = "arm64" if plat == "mac-arm64" else "x86_64"
            self.v["ffmpeg"].setdefault("build", {})[plat] = {"source": self.pin("py.tar.gz"),
                                                              "configure": ["--disable-doc", f"--arch={arch}"]}
            built = os.path.join(mac_cache, self.build.ffmpeg_key(self.v["ffmpeg"]["build"][plat]))
            os.makedirs(os.path.join(built, "bin"))
            for n in ("ffmpeg", "ffprobe"):
                with open(os.path.join(built, "bin", n), "wb") as f:
                    f.write(header)
                os.chmod(os.path.join(built, "bin", n), 0o755)
            with open(os.path.join(built, "COPYING.LGPLv2.1"), "w") as f:
                f.write("LGPL")
        return self.factory.mac_parts(self.parts, versions=self.v, cache=mac_cache)

    def test_six_environments_sealed_with_the_team_build_and_checked_with_the_plan_layer_last(self):
        deliver = {"url": "https://deliver.example.test/", "key": "b" * 32, "report": "https://0123456789abcdef@sentry.example.test/1"}
        got = self.release(deliver=deliver, server="https://deliver.example.test")
        self.assertEqual(sorted(got["platforms"]), sorted(self.factory.PLATFORMS))
        for plat, p in got["platforms"].items():
            with self.subTest(plat):
                arc = os.path.join(self.out, f"agents-hub-v1.2.3-{plat}.tar.gz")
                self.assertEqual(p["archive"], {"name": os.path.basename(arc), "sha256": sha(arc),
                                                "size": os.path.getsize(arc)})
                with tarfile.open(arc) as t:
                    m = t.getmember(f"agents-hub-v1.2.3-{plat}/deliver.json")
                    self.assertEqual(m.mode, 0o600)
                    self.assertEqual(json.load(t.extractfile(m)), {"url": "https://deliver.example.test", "key": "b" * 32,
                                                                   "report": "https://0123456789abcdef@sentry.example.test/1"})
                    self.assertEqual(len([n for n in t.getnames() if n.endswith("/deliver.json")]), 1)
                    self.assertIn(f"agents-hub-v1.2.3-{plat}/PACKAGED", t.getnames())
                self.assertEqual(p["arch"]["programs"], 1)
                self.assertFalse(os.path.exists(arc[:-len(".tar.gz")]))           # the unpacked folder went
                with open(arc[:-len(".tar.gz")] + ".json") as f:
                    self.assertEqual(json.load(f)["archive"], p["archive"])       # the sheet names the sealed one
        with open(os.path.join(self.out, "upload-plan.txt")) as f:
            rows = [r for r in f.read().splitlines() if not r.startswith("#")]
        self.assertEqual([r.split()[3] for r in rows], ["shell"] * 6 + ["script", "script", "layer", "show"])
        self.assertIn(f"{os.path.join(self.out, 'agents-hub-v1.2.3-windows-arm64.tar.gz')} --platform windows-arm64 "
                      "--runtime 1", rows[5])
        self.assertTrue(rows[8].endswith(f"--channel stable --version 1.2.3 --commit {got['layer']['commit']} "
                                         "--runtime-min 1"), rows[8])
        self.assertTrue(all(" https://deliver.example.test " in r for r in rows))
        self.assertNotIn("--send", "".join(rows))
        with open(os.path.join(self.out, "release.json")) as f:
            self.assertEqual(json.load(f), got)

    def test_the_open_build_carries_no_key_and_its_plan_writes_its_pages(self):
        got = self.release(platforms=("linux-x64", "windows-x64"), flavor="open", forbid=("forbidden-word",),
                           server="https://acme.example.test/agents-hub", github="acme/agents-hub")
        self.assertEqual((got["flavor"], got["layer"]["flavor"]), ("open", "open"))
        for plat, p in got["platforms"].items():
            arc = os.path.join(self.out, f"agents-hub-v1.2.3-open-{plat}.tar.gz")
            self.assertEqual(p["archive"]["name"], os.path.basename(arc))
            with tarfile.open(arc) as t:
                self.assertFalse([n for n in t.getnames() if n.endswith("/deliver.json")])
                mark = json.load(t.extractfile(f"agents-hub-v1.2.3-open-{plat}/PACKAGED"))
            self.assertEqual(mark["flavor"], "open")
        with open(os.path.join(self.out, "upload-plan.txt")) as f:
            rows = [r for r in f.read().splitlines() if not r.startswith("#")]
        self.assertEqual(len(rows), 2)
        self.assertIn("tools/package/pages.py", rows[0])
        self.assertIn("--base https://acme.example.test/agents-hub "
                      "--assets https://github.com/acme/agents-hub/releases/download/v1.2.3", rows[0])
        self.assertIn("upload.py acme/agents-hub v1.2.3 ", rows[1])
        with self.assertRaises(self.factory.Refused):   # and never a key with it
            self.release(platforms=("linux-x64",), flavor="open", forbid=("x",),
                         deliver={"url": "https://deliver.example.test", "key": "b" * 32})

    def test_a_build_key_or_a_dsn_not_of_their_form_stops_the_release_before_any_archive(self):
        for deliver in ({"url": "https://deliver.example.test", "key": "KEY=short"},
                        {"url": "http://deliver.example.test", "key": "b" * 32},
                        {"url": "https://deliver.example.test", "key": "b" * 32, "report": "https://sentry.example.test/1"}):
            with self.subTest(deliver), self.assertRaisesRegex(self.factory.Refused, "--deliver|--report-dsn"):
                self.release(platforms=("linux-x64",), deliver=deliver)
            self.assertFalse([n for n in os.listdir(self.out) if n.endswith(".tar.gz")])

    def test_the_mac_ffmpeg_comes_from_a_mac_s_parts_checked_and_the_mac_builds_are_made_here(self):
        told = self.as_on_a_mac()
        self.assertEqual(sorted(x["platform"] for x in told.values()), ["mac-arm64", "mac-x64"])
        if sys.platform != "darwin":   # no part in this cache and no Mac to build it: the refusal says what to do
            with self.assertRaisesRegex(self.factory.Refused, "mac-arm64: .*only on a Mac.*mac-parts"):
                self.release(platforms=("mac-arm64",))
        # the Mac parts here are a program's header, not a program: a Mac (the bridge) would start them in its smoke run
        # (the smoke run of an Intel build on an arm Mac has its own test)
        runs = self.factory.build.runs_here
        with mock.patch.object(self.factory.build, "runs_here", lambda plat: not plat.startswith("mac-") and runs(plat)):
            got = self.release(platforms=("linux-x64", "mac-arm64", "mac-x64"), parts=self.parts)
        for plat, header in (("mac-arm64", ARM_MACHO), ("mac-x64", X64_MACHO)):
            self.assertEqual(got["platforms"][plat]["arch"]["programs"], 2)        # ffmpeg, ffprobe (static)
            with tarfile.open(os.path.join(self.out, f"agents-hub-v1.2.3-{plat}.tar.gz")) as t:
                self.assertEqual(t.extractfile(f"agents-hub-v1.2.3-{plat}/runtime/ffmpeg/bin/ffprobe").read(), header)
        self.assertEqual(len({p["layer"]["sum"] for p in [self.factory.read_sheet(os.path.join(
            self.out, f"agents-hub-v1.2.3-{plat}.json")) for plat in got["platforms"]]}), 1)   # one layer for all

    def test_a_mac_part_of_other_bytes_or_beyond_its_folder_is_not_brought(self):
        told = self.as_on_a_mac()
        name = next(n for n, x in told.items() if x["platform"] == "mac-x64")
        path = os.path.join(self.parts, name + ".tar.gz")
        with open(path, "ab") as f:
            f.write(b"\0")
        with self.assertRaisesRegex(self.factory.Refused, "mac-x64: .* is not the part"):
            self.release(platforms=("mac-x64",), parts=self.parts)
        tgz(path, {f"{name}/bin/ffmpeg": (X64_MACHO, 0o755), "elsewhere/x": (b"x", 0o644)})
        told[name].update(sha256=sha(path), size=os.path.getsize(path))
        with open(os.path.join(self.parts, self.factory.PARTS), "w") as f:
            json.dump(told, f)
        with self.assertRaisesRegex(self.factory.Refused, "holds more than its folder"):
            self.release(platforms=("mac-x64",), parts=self.parts)
        self.assertFalse(os.path.exists(os.path.join(self.cache, "elsewhere")))

    def test_a_mac_part_of_the_wrong_processor_stops_the_mac_build(self):
        told = self.as_on_a_mac()
        name = next(n for n, x in told.items() if x["platform"] == "mac-x64")
        path = os.path.join(self.parts, name + ".tar.gz")   # a Mac whose cross build quietly gave arm64
        tgz(path, {f"{name}/bin/ffmpeg": (ARM_MACHO, 0o755), f"{name}/bin/ffprobe": (ARM_MACHO, 0o755)})
        told[name].update(sha256=sha(path), size=os.path.getsize(path))
        with open(os.path.join(self.parts, self.factory.PARTS), "w") as f:
            json.dump(told, f)
        with self.assertRaisesRegex(self.factory.Refused, "mac-x64: ffmpeg is built for arm64, and the platform needs x86_64"):
            self.release(platforms=("mac-x64",), parts=self.parts)

    def test_sheets_of_another_tag_another_layer_or_other_bytes_stop_the_release(self):
        self.release(platforms=("linux-x64", "mac-x64"), keep_folders=True)
        sheets = {p: self.factory.read_sheet(os.path.join(self.out, f"agents-hub-v1.2.3-{p}.json"))
                  for p in ("linux-x64", "mac-x64")}
        self.factory.check(sheets, self.out, "v1.2.3")
        for what, edit, said in (("tag", lambda s: s.update(tag="v1.2.2"), "not mac-x64 v1.2.3"),
                                 ("layer", lambda s: s["layer"].update(sum="0" * 64), "the builds differ"),
                                 ("programs", lambda s: s.pop("arch"), "no check of its programs"),
                                 ("trial", lambda s: s.update(skipped=["ffmpeg"]), "a trial"),
                                 ("bytes", lambda s: s["archive"].update(size=1), "is not the archive its sheet names")):
            with self.subTest(what):
                bad = json.loads(json.dumps(sheets))
                edit(bad["mac-x64"])
                with self.assertRaisesRegex(self.factory.Refused, said):
                    self.factory.check(bad, self.out, "v1.2.3")

    def test_a_nightly_is_its_layer_alone(self):
        work = os.path.join(self.root, "w-1")
        with open(os.path.join(work, "core", "x.py"), "w") as f:
            f.write("X = 2\n")
        g(work, "commit", "-qam", "after the tag")
        g(work, "push", "-q", "origin", "main")
        got = self.factory.nightly("main", self.out, self.repo, cache=self.cache, versions=self.v,
                                   python=sys.executable, server="https://deliver.example.test")
        self.assertEqual(got["layer"]["channel"], "nightly")
        self.assertTrue(got["layer"]["version"].startswith("1.2.3-nightly."), got["layer"]["version"])
        with open(os.path.join(self.out, "upload-plan.txt")) as f:
            rows = [r for r in f.read().splitlines() if not r.startswith("#")]
        self.assertEqual([r.split()[3] for r in rows], ["layer", "show"])
        self.assertIn("--channel nightly", rows[0])
        self.assertFalse([n for n in os.listdir(self.out) if n.startswith("agents-hub-")])


@POSIX_ONLY
class Upload(unittest.TestCase):
    """upload.py: files to a release of the repo — a look by default, sending only with --send."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="hub-upload-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.remote = os.path.join(self.root, "up.git")
        g(self.root, "init", "-q", "--bare", "-b", "main", self.remote)
        work = os.path.join(self.root, "w")
        g(self.root, "clone", "-q", self.remote, work)
        g(work, "commit", "-q", "--allow-empty", "-m", "x")
        g(work, "tag", "v1.2.3")
        g(work, "push", "-q", "origin", "main", "v1.2.3")
        self.file = os.path.join(self.root, "agents-hub-v1.2.3-linux-x64.tar.gz")
        with open(self.file, "wb") as f:
            f.write(b"archive")
        self.calls = os.path.join(self.root, "calls.jsonl")
        self.view = os.path.join(self.root, "view.json")
        self.list = os.path.join(self.root, "list.json")
        bindir = os.path.join(self.root, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "gh"), "w") as f:
            f.write(f"#!{sys.executable}\nimport json, sys\n"
                    f"open({self.calls!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
                    "if sys.argv[1:3] == ['release', 'view']:\n"
                    "    try:\n"
                    f"        print(open({self.view!r}).read())\n"
                    "    except FileNotFoundError:\n"
                    "        sys.stderr.write('release not found\\n'); sys.exit(1)\n"
                    "if sys.argv[1:3] == ['release', 'list']:\n"
                    "    try:\n"
                    f"        print(open({self.list!r}).read())\n"
                    "    except FileNotFoundError:\n"
                    "        print('[]')\n")
        os.chmod(os.path.join(bindir, "gh"), 0o755)
        self.env = {**os.environ, "PATH": bindir + os.pathsep + os.environ["PATH"]}

    def up(self, *args):
        return subprocess.run([sys.executable, os.path.join(PKG, "upload.py"), "acme/core", *args,
                               "--remote", self.remote], capture_output=True, text=True, env=self.env, timeout=60)

    def gh_calls(self):
        try:
            with open(self.calls) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    def test_without_send_it_only_looks(self):
        r = self.up("v1.2.3", self.file)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(sha(self.file), r.stdout)
        self.assertFalse([c for c in self.gh_calls() if c[:2] in (["release", "create"], ["release", "upload"])])

    def test_send_creates_the_release_on_the_tag_and_attaches_the_files_with_their_sums(self):
        r = self.up("v1.2.3", self.file, "--send")
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.gh_calls()
        create, = [c for c in calls if c[:2] == ["release", "create"]]
        self.assertIn("--verify-tag", create)
        self.assertIn(self.file, create)                     # files with the create: gh publishes after attaching them
        self.assertTrue([a for a in create if a.endswith(os.path.basename(self.file) + ".sha256")])
        self.assertFalse([c for c in calls if c[:2] == ["release", "upload"]])

    def test_a_tag_that_is_not_on_the_repo_is_refused(self):
        r = self.up("v9.9.9", self.file.replace("v1.2.3", "v9.9.9"), "--send")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse([c for c in self.gh_calls() if c[:2] != ["release", "view"]])

    def test_a_file_already_there_with_other_bytes_is_not_replaced(self):
        with open(self.view, "w") as f:
            json.dump({"assets": [{"name": os.path.basename(self.file), "digest": "sha256:" + "0" * 64}]}, f)
        r = self.up("v1.2.3", self.file, "--send")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertFalse([c for c in self.gh_calls() if c[:2] == ["release", "upload"]])

    def test_a_file_already_there_with_the_same_bytes_is_left_as_is(self):
        name = os.path.basename(self.file)
        line = f"{sha(self.file)}  {name}\n".encode()
        with open(self.view, "w") as f:
            json.dump({"assets": [{"name": name, "digest": "sha256:" + sha(self.file)},
                                  {"name": name + ".sha256", "digest": "sha256:" + hashlib.sha256(line).hexdigest()}]}, f)
        r = self.up("v1.2.3", self.file, "--send")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse([c for c in self.gh_calls() if c[:2] in (["release", "upload"], ["release", "create"])])

    def test_a_file_not_named_for_the_tag_is_refused(self):
        for name in ("agents-hub-v1.0.0-linux-x64.tar.gz", "agents-hub-v1.2.30-linux-x64.tar.gz",
                     "agents-hub-v1.2.3-rc.1-linux-x64.tar.gz", "agents-hub-v1.2.3-RC1-linux-x64.tar.gz",
                     "agents-hub_1.2.3~rc1_amd64.deb"):
            other = os.path.join(self.root, name)
            shutil.copy(self.file, other)
            r = self.up("v1.2.3", other)
            self.assertEqual(r.returncode, 2, name)
        deb = os.path.join(self.root, "agents-hub_1.2.3_amd64.deb")              # a .deb names the version without v
        shutil.copy(self.file, deb)
        self.assertEqual(self.up("v1.2.3", deb).returncode, 0)

    def test_a_send_cut_after_the_file_attaches_its_sum_on_the_rerun(self):
        with open(self.view, "w") as f:
            json.dump({"assets": [{"name": os.path.basename(self.file), "digest": "sha256:" + sha(self.file)}]}, f)
        r = self.up("v1.2.3", self.file, "--send")
        self.assertEqual(r.returncode, 0, r.stderr)
        upload, = [c for c in self.gh_calls() if c[:2] == ["release", "upload"]]
        self.assertNotIn(self.file, upload)
        self.assertTrue([a for a in upload if a.endswith(".tar.gz.sha256")])

    def test_a_draft_left_by_a_broken_send_is_not_made_twice(self):
        with open(self.list, "w") as f:
            json.dump([{"tagName": "v1.2.3", "isDraft": True}], f)
        r = self.up("v1.2.3", self.file, "--send")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertFalse([c for c in self.gh_calls() if c[:2] in (["release", "upload"], ["release", "create"])])

    def test_an_existing_release_gets_the_new_files_attached(self):
        with open(self.view, "w") as f:
            json.dump({"assets": []}, f)
        r = self.up("v1.2.3", self.file, "--send")
        self.assertEqual(r.returncode, 0, r.stderr)
        upload, = [c for c in self.gh_calls() if c[:2] == ["release", "upload"]]
        self.assertIn(self.file, upload)
        self.assertNotIn("--clobber", upload)
        self.assertFalse([c for c in self.gh_calls() if c[:2] == ["release", "create"]])


if __name__ == "__main__":
    unittest.main()
