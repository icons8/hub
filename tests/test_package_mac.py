"""The Mac shell of the release factory (installer spec S5): tools/package/macapp.py makes "Agents Hub.app" and its .dmg
from the mac-arm64 platform folder of build.py.

What must hold in every build: the shell (deploy/mac/appshell.py) and a git wrapper go in next to the environment; Python's
own modules carry bytecode that is never rewritten (a .pyc written at the first run would break the app's seal); every
Mach-O without a Developer ID is signed ad-hoc and the app last, then verified strictly; a link leaving the app stops
the build; the .dmg window is the size of its picture. On Linux the Mac tools (clang, codesign, sips, iconutil) are
recorders; the real build runs on the Mac bridge."""
import tests  # noqa: F401 - keeps the machine's settings out
import importlib.util
import json
import os
import plistlib
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests import winsafe

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(REPO, "tools", "package")


def load():
    spec = importlib.util.spec_from_file_location("hub_package_macapp", os.path.join(PKG, "macapp.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def stub(path, body):
    with open(path, "w") as f:
        f.write("#!/bin/sh\n" + body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


THIN = b"\xcf\xfa\xed\xfe" + b"\0" * 60
FAT = b"\xca\xfe\xba\xbe" + (2).to_bytes(4, "big") + b"\0" * 56
JAVA = b"\xca\xfe\xba\xbe" + (65).to_bytes(4, "big") + b"\0" * 56


class Detect(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="macapp-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.m = load()

    def put(self, rel, data):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def rel(self, paths):
        return [os.path.relpath(p, self.root).replace(os.sep, "/") for p in paths]

    def test_mach_o_thin_and_fat_but_not_a_java_class_or_a_script(self):
        self.put("bin/node", THIN)
        self.put("lib/x.so", FAT)
        self.put("lib/A.class", JAVA)
        self.put("bin/npm", b"#!/usr/bin/env node\n")
        winsafe.try_symlink("node", os.path.join(self.root, "bin", "nodejs"))   # a link to a Mach-O is not one more
        self.assertEqual(self.rel(self.m.machos(self.root)), ["bin/node", "lib/x.so"])

    def test_links_that_leave_the_app(self):
        self.put("runtime/python/bin/python3.14", THIN)
        # portable: a file name in the app's layout, not the Python the test runs on
        winsafe.symlink(self, "python3.14", os.path.join(self.root, "runtime/python/bin/python3"))     # inside: fine
        self.assertEqual(self.m.outside(self.root), [])
        winsafe.symlink(self, "/usr/bin/env", os.path.join(self.root, "runtime/python/bin/env"))
        winsafe.symlink(self, "../../../../x", os.path.join(self.root, "runtime/python/bin/up"))
        self.assertEqual(sorted(p.replace(os.sep, "/") for p in self.m.outside(self.root)),
                         ["runtime/python/bin/env", "runtime/python/bin/up"])

    def test_dmg_window_is_the_size_of_its_picture(self):
        def size(name):
            with open(os.path.join(PKG, "mac", name), "rb") as f:
                head = f.read(24)
            return struct.unpack(">II", head[16:24])
        w, h = self.m.WINDOW
        self.assertEqual(size("background.png"), (w, h))
        self.assertEqual(size("background@2x.png"), (2 * w, 2 * h))     # a Retina screen gets the sharp one
        s = self.m.settings("/x/Agents Hub.app", "/x/logo.icns")
        self.assertEqual(s["window_rect"][1], (w, h))
        self.assertEqual(set(s["icon_locations"]), {"Agents Hub.app", "Applications"})
        self.assertEqual(s["symlinks"], {"Applications": "/Applications"})
        for x, y in s["icon_locations"].values():
            self.assertTrue(0 < x < w and 0 < y < h)

    def test_dmgbuild_is_pinned(self):
        for w in self.m.DMGBUILD:
            self.assertEqual(os.path.basename(w["url"]), w["name"])
            self.assertRegex(w["sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(w["url"].startswith("https://files.pythonhosted.org/"))

    def test_only_on_a_mac(self):
        with mock.patch.object(self.m.sys, "platform", "linux"):
            with self.assertRaises(self.m.Refused):
                self.m.build(self.root, os.path.join(self.root, "out"))


@unittest.skipIf(os.name == "nt", "sh stubs")
class Bundle(unittest.TestCase):
    """app() on a platform folder laid out like build.py's, with the Mac tools recorded."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="macapp-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.m = load()
        self.top = os.path.join(self.root, "agents-hub-v1.2.3-mac-arm64")
        self.lib = os.path.join(self.root, "pylib")       # "Python's own modules": the stdlib of the fake python
        os.makedirs(os.path.join(self.lib, "json"))
        with open(os.path.join(self.lib, "json", "__init__.py"), "w") as f:
            f.write("X = 1\n")
        py = os.path.join(self.top, "runtime", "python", "bin")
        os.makedirs(py)
        stub(os.path.join(py, "python3"), f'case "$*" in *sysconfig*) echo "{self.lib}"; exit 0 ;; esac\n'
             f'exec "{sys.executable}" "$@"\n')
        for rel, data in (("runtime/node/bin/node", THIN), ("runtime/git/bin/git", THIN),
                          ("runtime/git/libexec/git-core/git-remote-https", THIN), ("runtime/gh/bin/gh", FAT),
                          ("runtime/python/lib/libpython.dylib", THIN), ("runtime/node/README.md", b"x")):
            path = os.path.join(self.top, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
        winsafe.symlink(self, "../../bin/git", os.path.join(self.top, "runtime/git/libexec/git-core/git-log"))
        os.makedirs(os.path.join(self.top, "code-seed"))
        shutil.copy2(os.path.join(PKG, "launch.py"), self.top)
        shutil.copy2(os.path.join(os.path.dirname(PKG), "..", "core", "appcode.py"), self.top)   # build.py puts it there
        with open(os.path.join(self.top, "paths.json"), "w") as f:
            json.dump({"python": "runtime/python/bin/python3"}, f)
        with open(os.path.join(self.top, "PACKAGED"), "w") as f:
            json.dump({"format": "app", "version": "1.2.3", "runtime": 1, "platform": "mac-arm64"}, f)
        self.stubs = os.path.join(self.root, "stubs")
        os.makedirs(self.stubs)
        self.calls = os.path.join(self.root, "calls.log")
        # codesign -dv: node and gh carry a Developer ID (as downloaded), the rest — the linker's signature
        stub(os.path.join(self.stubs, "codesign"), f'echo "codesign $*" >> "{self.calls}"\n'
             'if [ "$1" = -dv ]; then case "$3" in */node|*/gh) echo "Authority=Developer ID Application: X" >&2 ;; '
             'esac; exit 0; fi\nexit 0\n')
        stub(os.path.join(self.stubs, "clang"), f'echo "clang $*" >> "{self.calls}"\n'
             'while [ $# -gt 0 ]; do [ "$1" = -o ] && : > "$2"; shift; done\n')
        stub(os.path.join(self.stubs, "sips"), 'while [ $# -gt 0 ]; do [ "$1" = --out ] && : > "$2"; shift; done\n')
        stub(os.path.join(self.stubs, "iconutil"), 'while [ $# -gt 0 ]; do [ "$1" = -o ] && : > "$2"; shift; done\n')
        self.path = mock.patch.dict(os.environ, {"PATH": self.stubs + ":" + os.environ.get("PATH", "")})
        self.path.start()
        self.addCleanup(self.path.stop)

    def recorded(self):
        with open(self.calls) as f:
            return f.read().splitlines()

    def test_the_app_carries_the_shell_signs_what_has_no_developer_id_and_seals_last(self):
        out = os.path.join(self.root, "out")
        os.makedirs(out)
        path, signed = self.m.app(self.top, out)
        hub = os.path.join(path, "Contents", "Resources", "hub")
        with open(os.path.join(hub, "appshell.py")) as f, open(os.path.join(REPO, "deploy", "mac", "appshell.py")) as w:
            self.assertEqual(f.read(), w.read())                          # the shell, frozen
        self.assertTrue(os.access(os.path.join(hub, "bin", "git"), os.X_OK))
        self.assertTrue(os.path.isdir(os.path.join(hub, "code-seed")))
        self.assertEqual(os.readlink(os.path.join(hub, "runtime/git/libexec/git-core/git-log")), "../../bin/git")
        with open(os.path.join(path, "Contents", "Info.plist"), "rb") as f:
            info = plistlib.load(f)
        self.assertEqual((info["CFBundleShortVersionString"], info["LSMinimumSystemVersion"]), ("1.2.3", "13.0"))
        self.assertEqual(info["CFBundleIdentifier"], self.m._deploy_app().APP_ID)   # what the agent associates
        self.assertNotEqual(info["CFBundleIdentifier"], "com.hub-core")             # a git hub's bundle is that
        self.assertEqual(sorted(signed["developer-id"]), ["runtime/gh/bin/gh", "runtime/node/bin/node"])
        self.assertEqual(sorted(signed["ad-hoc"]), ["runtime/git/bin/git", "runtime/git/libexec/git-core/git-remote-https",
                                                    "runtime/python/lib/libpython.dylib"])
        calls = [c for c in self.recorded() if c.startswith("codesign") and "-dv" not in c]
        self.assertEqual(calls[-2:], [f"codesign --force --sign - {path}",
                                      f"codesign --verify --deep --strict {path}"])   # the app after what is inside
        self.assertEqual(len(calls), 3 + 2)
        clang = next(c for c in self.recorded() if c.startswith("clang"))
        self.assertIn("-mmacosx-version-min=13.0", clang)
        self.assertIn(os.path.join("deploy", "mac", "launcher.c"), clang)
        # Python's own modules: hash-based bytecode that is never checked against the source, so never rewritten
        pyc = os.path.join(self.lib, "json", "__pycache__")
        name = next(n for n in os.listdir(pyc) if n.startswith("__init__."))
        with open(os.path.join(pyc, name), "rb") as f:
            self.assertEqual(int.from_bytes(f.read(8)[4:8], "little"), 0b01)

    def test_bytecode_whatever_the_build_folder_is_called(self):
        # the exclusion of Python's own tests is anchored at its library, not at any "tests" in the path
        out = os.path.join(self.root, "tests", "out")
        os.makedirs(out)
        os.makedirs(os.path.join(self.lib, "test"))
        with open(os.path.join(self.lib, "test", "badsyntax.py"), "w") as f:
            f.write("def (:\n")                                           # broken on purpose, as CPython's are
        self.lib_moved = shutil.move(self.lib, os.path.join(self.root, "tests", "pylib"))
        stub(os.path.join(self.top, "runtime", "python", "bin", "python3"),
             f'case "$*" in *sysconfig*) echo "{self.lib_moved}"; exit 0 ;; esac\nexec "{sys.executable}" "$@"\n')
        self.m.app(self.top, out)
        self.assertTrue(os.listdir(os.path.join(self.lib_moved, "json", "__pycache__")))
        self.assertFalse(os.path.exists(os.path.join(self.lib_moved, "test", "__pycache__")))

    def test_the_git_wrapper_points_git_at_its_helpers(self):
        out = os.path.join(self.root, "out")
        os.makedirs(out)
        path, _ = self.m.app(self.top, out)
        git = os.path.join(path, "Contents", "Resources", "hub", "runtime", "git", "bin", "git")
        stub(git, 'echo "$GIT_EXEC_PATH|$GIT_TEMPLATE_DIR|$*"\n')
        r = subprocess.run([os.path.join(path, "Contents", "Resources", "hub", "bin", "git"), "--version"],
                           capture_output=True, text=True, check=True)
        g = os.path.join(path, "Contents", "Resources", "hub", "runtime", "git")
        self.assertEqual(r.stdout.strip(), f"{g}/libexec/git-core|{g}/share/git-core/templates|--version")

    def test_a_link_leaving_the_app_stops_the_build(self):
        winsafe.symlink(self, "/usr/lib/libz.dylib", os.path.join(self.top, "runtime", "python", "lib", "libz.dylib"))
        with self.assertRaises(self.m.Refused) as e:
            self.m.app(self.top, self.root)
        self.assertIn("runtime/python/lib/libz.dylib", str(e.exception))

    def test_not_a_mac_app_folder_stops_the_build(self):
        with open(os.path.join(self.top, "PACKAGED"), "w") as f:
            json.dump({"format": "app", "version": "1.2.3", "platform": "linux-x64"}, f)
        with self.assertRaises(self.m.Refused):
            self.m.app(self.top, self.root)


if __name__ == "__main__":
    unittest.main()
