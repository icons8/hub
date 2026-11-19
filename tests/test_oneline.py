"""The one-line install scripts (deploy/oneline/install.sh and install.ps1, one-line spec §4–5): the build picked by the
machine, the refusals before anything is downloaded, releases.json read without jq, the environment downloaded with
resume and checked by size and sha256, unpacked and every file checked against the archive, one install at a time, and
the hand-over to the environment's install.py (here a stand-in that writes down what it got). install.sh runs for real
against deliver/server.py on a free port of 127.0.0.1 with a temporary home; its machine probes (uname, sysctl, sw_vers,
getconf) are stand-ins on PATH. install.ps1 is checked as text here (its live runs are on Windows)."""
import tests  # noqa: F401 - keeps the machine's settings out
import hashlib
import http.server
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import platforms
from tests import winsafe
from tests.fakes import cut

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SH = os.path.join(ROOT, "deploy", "oneline", "install.sh")
PS1 = os.path.join(ROOT, "deploy", "oneline", "install.ps1")
SERVER = os.path.join(ROOT, "deliver", "server.py")
SEND = os.path.join(ROOT, "tools", "package", "deliver_send.py")
BASH = shutil.which("bash")
READ, WRITE = "read-key-of-the-test", "write-key-of-the-test"
HELPER = re.compile(r"# --- unpack\.py .*?# --- end of unpack\.py ---\n", re.S)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def text(path):
    """The file as git keeps it: git with core.autocrlf (Windows) checks it out with CRLF (that the scripts themselves
    have none — Text.test_the_scripts_have_no_cr_of_their_own)."""
    with open(path, "rb") as f:
        return f.read().replace(b"\r\n", b"\n")


INSTALL_PY = b"""import json, os, sys
argv = sys.argv[1:]
key = open(argv[argv.index("--key-file") + 1]).read().strip() if "--key-file" in argv else None
with open(os.environ["STUB_OUT"], "w") as f:
    json.dump({"argv": argv, "key": key, "lang": os.environ.get("HUB_LANG"), "log": os.environ.get("HUB_INSTALL_LOG"),
               "tty": os.isatty(0), "cwd_env": os.path.isdir(argv[argv.index("--env") + 1]),
               "ui": os.environ.get("HUB_INSTALL_UI")}, f)
pyc = os.path.join(argv[argv.index("--env") + 1], "runtime", "python", "lib", "stale.pyc")
if os.path.isfile(pyc):   # as install.py prepare: its compileall writes the Python's stale bytecode again
    with open(pyc, "ab") as f:
        f.write(b"x")
sys.exit(int(os.environ.get("STUB_EXIT", "0")))
"""


# the helper's own switch (unpack.py WIN): an environment of Windows has no links, and there it refuses one
LINKS = os.name != "nt"


def archive(plat="linux-x64", top=None, extra=(), drop=(), installer=True, link=True):
    """A stand-in environment archive as the factory lays it out: one top folder, runtime/python/bin/python3 (a link to a
    wrapper of this Python; link=False — none, as on Windows), install.py; extra — (name, data) more files."""
    top = top or f"agents-hub-v9.9.9-{plat}"
    buf = io.BytesIO()

    def add(t, name, data=b"", mode=0o644, kind=None, link=None):
        ti = tarfile.TarInfo(f"{top}/{name}" if name else top)
        ti.mtime, ti.mode = 1, mode
        if kind == "dir":
            ti.type = tarfile.DIRTYPE
            t.addfile(ti)
        elif kind == "sym":
            ti.type, ti.linkname = tarfile.SYMTYPE, link
            t.addfile(ti)
        else:
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))

    # like the real one, this Python writes into its own tree (a stale .pyc rewritten) unless it is told -B (the Mac
    # bridge, 2026-11-08); the Python's bytecode is out of the sheet since (unpack.py own_bytecode)
    py = (f'#!/bin/sh\ncase " $* " in *" -B "*) ;; *) printf x >> "$(dirname "$0")/../lib/stale.pyc" ;; esac\n'
          f'exec "{sys.executable}" "$@"\n').encode()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        add(t, "", kind="dir", mode=0o755)
        for d in ("runtime", "runtime/python", "runtime/python/bin", "runtime/python/lib"):
            add(t, d, kind="dir", mode=0o755)
        add(t, "runtime/python/lib/stale.pyc", b"pyc")
        add(t, "runtime/python/lib/stale.py", b"")
        add(t, "runtime/python/bin/python-wrapper", py, 0o755)
        if link:
            add(t, "runtime/python/bin/python3", kind="sym", link="python-wrapper")
        if installer:
            add(t, "install.py", INSTALL_PY)
        add(t, "paths.json", b"{}")
        for name, data in extra:
            if name not in drop:
                add(t, name, data)
    return buf.getvalue()


class Busy(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(503)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


def body(script):
    """The script without its last line (main "$@"): its functions, to call one by one."""
    lines = text(script).decode().rstrip("\n").split("\n")
    assert lines[-1] == 'main "$@"', lines[-1]
    return "\n".join(lines[:-1]) + "\n"


class Text(unittest.TestCase):
    """What the delivery server and PowerShell require of the files, and what keeps the two scripts one."""

    def test_the_scripts_have_no_cr_of_their_own(self):
        # bash fails on a CR ($'\r': command not found) and the release sends the files as they lie: a CR in the
        # checkout is only what git with core.autocrlf writes — every line ended so, over a text git keeps without one
        for path in (SH, PS1, os.path.join(ROOT, "deploy", "oneline", "unpack.py")):
            with open(path, "rb") as f:
                raw = f.read()
            if b"\r" not in raw:
                continue
            rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
            self.assertEqual(raw, raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"), f"{rel}: a CR of its own")
            eol = subprocess.run(["git", "-C", ROOT, "ls-files", "--eol", "--", rel], capture_output=True, text=True)
            self.assertTrue(eol.stdout.startswith("i/lf "), f"{rel}: git keeps it with CR ({eol.stdout.strip()})")

    @cut.needs_deliver()
    def test_both_scripts_are_ascii_small_and_mark_the_place_of_the_key_once(self):
        srv = load("deliver_server_t", SERVER)
        send = load("deliver_send_t", SEND)
        for path in (SH, PS1):
            data = text(path)
            self.assertTrue(srv.SCRIPT_TEXT.fullmatch(data), path)        # the server takes printable ASCII only
            self.assertLess(len(data), srv.MAX_SCRIPT)
            self.assertEqual(data.count(srv.KEY_MARK), 1, path)          # every mark is replaced: one, where it is read
            self.assertEqual(data.count(send.URL_MARK), 1, path)         # the server the release puts it on

    def test_the_core_names_no_server_of_its_own(self):
        # the release writes in the server it puts the script on (tools/package/upload.py script)
        self.assertIn(b"\nHUB_INSTALL_SERVER='@@HUB_DELIVER_URL@@'", text(SH))
        self.assertIn(b"\n  $Server0 = '@@HUB_DELIVER_URL@@'", text(PS1))

    @cut.needs_deliver()
    def test_the_server_writes_the_key_where_it_is_read_and_param_stays_first(self):
        srv = load("deliver_server_t", SERVER)
        sh_out = srv.with_key("install.sh", text(SH), READ).decode()
        self.assertTrue(sh_out.startswith("#!/bin/bash\n"))
        self.assertIn(f"HUB_INSTALL_KEY_IN='{READ}'", sh_out)
        ps_out = srv.with_key("install.ps1", text(PS1), READ).decode()
        self.assertTrue(ps_out.startswith("param("))        # & ([scriptblock]::Create(...)) -Channel main needs it first
        self.assertIn(f"$HubInstallKeyIn = '{READ}'", ps_out)

    def test_the_unpack_helper_is_the_same_in_both(self):
        a, b = HELPER.findall(text(SH).decode()), HELPER.findall(text(PS1).decode())
        self.assertEqual(len(a), 1)
        self.assertEqual(a, b)
        # and the code's own copy, which the update unpacks a new environment with (modules/update/layer.py)
        self.assertEqual(a[0], text(os.path.join(ROOT, "deploy", "oneline", "unpack.py")).decode())

    def test_the_whole_body_runs_from_the_last_line(self):
        self.assertEqual(text(SH).decode().rstrip("\n").split("\n")[-1], 'main "$@"')
        last = text(PS1).decode().rstrip("\n").split("\n")[-1]
        self.assertTrue(last.startswith("Install-AgentsHub "), last)

    def test_powershell_never_exits(self):
        # inside `irm | iex` exit closes the person's window together with the reason
        code = HELPER.sub("", text(PS1).decode())
        code = re.sub(r"(?m)#.*$", "", code)
        code = re.sub(r"'[^'\n]*'|\"[^\"\n]*\"", "''", code)          # the words of strings are not statements
        self.assertIsNone(re.search(r"(?i)\bexit\b", code))
        self.assertNotIn("$script:", code)           # the person's session scope: nothing of the run is left in it
        self.assertNotIn("$global:", code.replace("$global:LASTEXITCODE", ""))

    def test_the_helper_compiles(self):
        compile(HELPER.search(text(SH).decode()).group(0), "unpack.py", "exec")


class Helper(unittest.TestCase):
    """The unpack/verify program the environment's Python runs (the same text in both scripts)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="oneline-helper-")
        self.addCleanup(platforms.current.rmtree, self.dir)
        self.helper = os.path.join(self.dir, "unpack.py")
        with open(self.helper, "w") as f:
            f.write(HELPER.search(text(SH).decode()).group(0))

    def run_helper(self, *args, mark=None):
        env = dict(os.environ, HUB_SHELL_MARK=json.dumps(mark or {"platform": "linux-x64"}))
        return subprocess.run([sys.executable, "-I", self.helper, *args], capture_output=True, text=True, env=env,
                              timeout=60)

    def arc(self, data):
        p = os.path.join(self.dir, "a.tar.gz")
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_unpack_puts_every_member_in_place_writes_the_mark_and_renames(self):
        a = self.arc(archive(extra=[("deep/er/file.txt", b"x" * 1000)], link=LINKS))
        part, final = os.path.join(self.dir, "1-team.part"), os.path.join(self.dir, "1-team")
        r = self.run_helper("unpack", a, part, final)
        files = 7 if LINKS else 6
        self.assertEqual((r.returncode, r.stdout.strip()), (0, f"ok {files}"), r.stdout + r.stderr)
        self.assertFalse(os.path.exists(part))
        if LINKS:
            self.assertEqual(os.readlink(os.path.join(final, "runtime/python/bin/python3")), "python-wrapper")
        self.assertEqual(os.path.getsize(os.path.join(final, "deep/er/file.txt")), 1000)
        with open(os.path.join(final, ".agents-hub-shell.json")) as f:
            self.assertEqual(json.load(f), {"platform": "linux-x64", "files": files, "top": "agents-hub-v9.9.9-linux-x64"})

    def test_verify_finds_a_lost_file_or_one_of_another_size(self):
        # tar.exe drops a path over 260 characters with code 0: the check by the archive is what tells it
        a = self.arc(archive(extra=[("x/lost.txt", b"abc"), ("x/short.txt", b"abcdef")], link=LINKS))
        part = os.path.join(self.dir, "p")
        self.assertEqual(self.run_helper("unpack", a, part).returncode, 0)
        os.unlink(os.path.join(part, ".agents-hub-shell.json"))
        os.unlink(os.path.join(part, "x", "lost.txt"))
        r = self.run_helper("verify", a, part)
        self.assertEqual((r.returncode, r.stdout.strip()), (1, "missing after unpacking: x/lost.txt"))
        with open(os.path.join(part, "x", "lost.txt"), "wb") as f:
            f.write(b"abc")
        with open(os.path.join(part, "x", "short.txt"), "wb") as f:
            f.write(b"abc")
        r = self.run_helper("verify", a, part)
        self.assertEqual((r.returncode, r.stdout.strip()), (1, "not as in the archive: x/short.txt"))
        self.assertFalse(os.path.exists(os.path.join(part, ".agents-hub-shell.json")))

    def test_unpack_writes_the_sheet_and_check_tells_a_tree_unlike_it(self):
        a = self.arc(archive(extra=[("deep/er/file.txt", b"x" * 1000)], link=LINKS))
        env = os.path.join(self.dir, "1-team")
        self.assertEqual(self.run_helper("unpack", a, env + ".part", env).returncode, 0)
        with open(os.path.join(env, ".agents-hub-shell.files")) as f:
            sheet = json.load(f)
        self.assertEqual(sheet["links"], {"runtime/python/bin/python3": "python-wrapper"} if LINKS else {})
        self.assertEqual(sheet["files"]["deep/er/file.txt"], [1000, sha(b"x" * 1000)])
        r = self.run_helper("check", env)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "ok 6" if LINKS else "ok 5"), r.stdout)
        with open(os.path.join(env, "added.pyc"), "w") as f:   # what the environment adds itself is not looked at
            f.write("x")
        self.assertEqual(self.run_helper("check", env).returncode, 0)
        path = os.path.join(env, "deep", "er", "file.txt")
        with open(path, "wb") as f:   # the same size, another byte
            f.write(b"x" * 999 + b"y")
        self.assertEqual(self.run_helper("check", env).stdout.strip(), "not as unpacked: deep/er/file.txt")
        os.unlink(path)
        self.assertEqual(self.run_helper("check", env).stdout.strip(), "missing: deep/er/file.txt")
        with open(path, "wb") as f:
            f.write(b"x" * 1000)
        if LINKS:
            link = os.path.join(env, "runtime", "python", "bin", "python3")
            os.unlink(link)
            winsafe.symlink(self, "elsewhere", link)
            self.assertEqual(self.run_helper("check", env).stdout.strip(), "not as unpacked: runtime/python/bin/python3")
        os.unlink(os.path.join(env, ".agents-hub-shell.files"))
        r = self.run_helper("check", env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no sheet", r.stdout)

    def test_the_pythons_bytecode_compiled_again_is_no_broken_environment(self):
        # python-build-standalone comes with .pyc older than their sources; install.py prepare compiles them again
        # (linux-arm64 and Windows: a run again downloaded the whole environment)
        stdlib = "runtime/python/lib/python3.14/encodings/__pycache__/__init__.cpython-314.pyc"
        seed = "code-seed/core/__pycache__/__init__.cpython-314.pyc"
        a = self.arc(archive(extra=[(stdlib, b"old"), (seed, b"seed")], link=LINKS))
        env = os.path.join(self.dir, "1-team")
        self.assertEqual(self.run_helper("unpack", a, env + ".part", env).returncode, 0)
        sheet_path = os.path.join(env, ".agents-hub-shell.files")
        with open(sheet_path) as f:
            sheet = json.load(f)
        self.assertNotIn(stdlib, sheet["files"])
        self.assertIn(seed, sheet["files"])                    # the code's bytecode is the release's: still checked
        with open(os.path.join(env, *stdlib.split("/")), "wb") as f:
            f.write(b"compiled again")
        r = self.run_helper("check", env)
        self.assertEqual((r.returncode, r.stdout.split()[0]), (0, "ok"), r.stdout)
        sheet["files"][stdlib] = [3, sha(b"old")]               # a sheet an earlier install wrote
        with open(sheet_path, "w") as f:
            json.dump(sheet, f)
        r = self.run_helper("check", env)
        self.assertEqual((r.returncode, r.stdout.split()[0]), (0, "ok"), r.stdout)
        with open(os.path.join(env, *seed.split("/")), "wb") as f:
            f.write(b"edit")
        self.assertEqual(self.run_helper("check", env).stdout.strip(), "not as unpacked: " + seed)

    def test_verify_after_tar_writes_the_same_sheet_as_unpack(self):
        a = self.arc(archive(extra=[("deep/er/file.txt", b"x" * 1000)], link=LINKS))
        one, two = os.path.join(self.dir, "one"), os.path.join(self.dir, "two")
        self.assertEqual(self.run_helper("unpack", a, one).returncode, 0)
        os.makedirs(two)
        with tarfile.open(a) as t:
            for m in t.getmembers():
                m.name = m.name.split("/", 1)[1] if "/" in m.name else "."
            t.extractall(two, filter="tar")
        self.assertEqual(self.run_helper("verify", a, two).returncode, 0)
        sheets = []
        for d in (one, two):
            with open(os.path.join(d, ".agents-hub-shell.files")) as f:
                sheets.append(json.load(f))
        self.assertEqual(sheets[0], sheets[1])

    def test_members_that_reach_outside_are_refused(self):
        for name, kind, link in (("top/../../evil", None, None), ("top/a/../../../evil", None, None),
                                 ("top/l", "sym", "../../etc/passwd"), ("top/l", "sym", "/etc/passwd"),
                                 ("other/file", None, None)):
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as t:
                ti = tarfile.TarInfo("top/ok")
                ti.size = 1
                t.addfile(ti, io.BytesIO(b"1"))
                ti = tarfile.TarInfo(name)
                if kind == "sym":
                    ti.type, ti.linkname = tarfile.SYMTYPE, link
                    t.addfile(ti)
                else:
                    ti.size = 1
                    t.addfile(ti, io.BytesIO(b"1"))
            part = os.path.join(self.dir, "p-" + sha(name.encode() + str(link).encode())[:8])
            r = self.run_helper("unpack", self.arc(buf.getvalue()), part)
            self.assertEqual(r.returncode, 1, (name, link, r.stdout))
            self.assertFalse(os.path.exists(os.path.join(self.dir, "evil")))


class Stand(unittest.TestCase):
    """deliver/server.py on a free port with one environment for linux-x64 (and the others the test asks for)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="oneline-")
        self.addCleanup(platforms.current.rmtree, self.dir)
        self.url = "http://127.0.0.1:1"   # no delivery server where deliver/ is cut out of the tree (the open flavor): nothing listens
        if cut.DELIVER:
            self.srv_mod = load("deliver_server_o", SERVER)
            data = os.path.join(self.dir, "data")
            self.srv = self.srv_mod.make(data, "127.0.0.1", 0, read=[sha(READ.encode())], write=[sha(WRITE.encode())],
                                         say=lambda *a: None)
            threading.Thread(target=self.srv.serve_forever, daemon=True).start()
            self.addCleanup(self.srv.server_close)
            self.addCleanup(self.srv.shutdown)
            self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.home = os.path.join(self.dir, "home")
        os.makedirs(self.home)
        self.out = os.path.join(self.dir, "stub.json")
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)
        self.arc = archive()
        if cut.DELIVER:
            self.publish("linux-x64", self.arc)

    def call(self, method, path, data=None, headers=None):
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Authorization": f"Bearer {WRITE}", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, dict(e.headers), e.read()

    def publish(self, plat, data, entry=None, tag="v9.9.9"):
        name = f"agents-hub-{tag}-{plat}.tar.gz"
        st = self.call("PUT", f"/v1/app/{name}?offset=0&size={len(data)}&sha256={sha(data)}", data)[0]
        self.assertIn(st, (200, 201))
        st, h, got = self.call("GET", "/v1/releases.json")
        doc = json.loads(got) if st == 200 else {"channels": {}, "runtime": {"current": 1, "shells": {}}}
        doc["runtime"]["shells"][plat] = entry or {"url": f"/v1/app/{name}", "sha256": sha(data), "size": len(data),
                                                   "runtime": 1}
        head = {"If-Match": h["ETag"]} if st == 200 else {"If-None-Match": "*"}
        self.assertEqual(self.call("PUT", "/v1/releases.json", json.dumps(doc).encode(), head)[0], 200)

    def env(self, **extra):
        e = {"HOME": self.home, "PATH": self.bin + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
             "LANG": "ru_RU.UTF-8", "HUB_DELIVER_URL": self.url, "HUB_DELIVER_KEY": READ, "STUB_OUT": self.out,
             "TMPDIR": self.dir}
        e.update(extra)
        return {k: v for k, v in e.items() if v is not None}

    def install(self, *args, **extra):
        r = subprocess.run([BASH, SH, *args], env=self.env(**extra), capture_output=True, text=True,  # portable: bash
                           stdin=subprocess.DEVNULL, timeout=120, start_new_session=True)
        return r

    def got(self):
        with open(self.out) as f:
            return json.load(f)

    @property
    def root(self):
        return os.path.join(self.home, ".local", "share", "agents-hub")

    def fake(self, name, script):
        p = os.path.join(self.bin, name)
        with open(p, "w") as f:
            f.write("#!/bin/sh\n" + script)
        os.chmod(p, 0o755)


@unittest.skipIf(BASH is None or os.name == "nt", "install.sh is for macOS and Linux")
@cut.needs_deliver()
class Pick(Stand):
    """The build of the machine, and the machines refused before anything is downloaded."""

    def pick(self, uname_s, uname_m, sysctl=None, sw_vers="15.7.7", glibc="glibc 2.39", musl=False):
        self.fake("uname", f'case "$1" in -s) echo {uname_s} ;; -m) echo {uname_m} ;; esac\n')
        self.fake("sw_vers", f"echo {sw_vers}\n")
        if sysctl is None:   # the real Intel Mac: no such key, a non-zero code
            self.fake("sysctl", 'echo "sysctl: unknown oid \'hw.optional.arm64\'" >&2; exit 1\n')
        else:
            self.fake("sysctl", f"echo {sysctl}\n")
        if glibc:
            self.fake("getconf", f'[ "$1" = GNU_LIBC_VERSION ] && echo "{glibc}"\n')
        else:
            self.fake("getconf", "exit 1\n")
        self.fake("ldd", "echo 'musl libc (x86_64)' >&2\n" if musl else "echo 'ldd (GNU libc) 2.39'\n")
        script = os.path.join(self.dir, "pick.sh")
        with open(script, "w") as f:
            f.write(body(SH) + 'pick_platform && echo "PLATFORM=$PLATFORM"\n')
        r = subprocess.run([BASH, script], env=self.env(), capture_output=True, text=True, timeout=30)  # portable: bash
        m = re.search(r"PLATFORM=(\S+)", r.stdout)
        return (m.group(1) if m else None), r.stderr

    def test_six_builds(self):
        self.assertEqual(self.pick("Darwin", "arm64")[0], "mac-arm64")
        self.assertEqual(self.pick("Darwin", "x86_64", sysctl="1")[0], "mac-arm64")     # a terminal under Rosetta
        self.assertEqual(self.pick("Darwin", "x86_64", sysctl=None)[0], "mac-x64")      # "unknown oid" is an answer
        self.assertEqual(self.pick("Darwin", "x86_64", sysctl="0")[0], "mac-x64")
        self.assertEqual(self.pick("Linux", "x86_64")[0], "linux-x64")
        self.assertEqual(self.pick("Linux", "aarch64")[0], "linux-arm64")

    def test_old_macos_old_glibc_musl_and_other_machines_are_refused(self):
        self.assertEqual(self.pick("Darwin", "arm64", sw_vers="13.5")[0], "mac-arm64")
        got, err = self.pick("Darwin", "arm64", sw_vers="13.4.1")
        self.assertIsNone(got)
        self.assertIn("macOS 13.4.1 is older than 13.5", err)
        self.assertEqual(self.pick("Linux", "x86_64", glibc="glibc 2.28")[0], "linux-x64")
        got, err = self.pick("Linux", "x86_64", glibc="glibc 2.27")          # Ubuntu 18.04
        self.assertIsNone(got)
        self.assertIn("glibc 2.27", err)
        got, err = self.pick("Linux", "x86_64", glibc=None, musl=True)       # Alpine
        self.assertIsNone(got)
        self.assertIn("musl", err)
        for s, m in (("Linux", "i686"), ("Linux", "armv7l"), ("FreeBSD", "amd64"), ("Darwin", "ppc")):
            self.assertIsNone(self.pick(s, m)[0], (s, m))

    def test_the_language_is_the_first_the_system_speaks(self):
        self.fake("defaults", 'printf \'(\\n    "ru-RU",\\n    en,\\n    "de-DE"\\n)\\n\'\n')
        script = os.path.join(self.dir, "lang.sh")
        with open(script, "w") as f:
            f.write(body(SH) + 'PLATFORM=mac-arm64; echo "mac=$(system_language)"; '
                    'PLATFORM=linux-x64; echo "linux=$(system_language)"; echo "c=$(LANG=C system_language)"\n')
        env = self.env(LANG="pt_BR.UTF-8")
        for k in ("LC_ALL", "LC_MESSAGES"):
            env.pop(k, None)
        out = subprocess.run([BASH, script], env=env, capture_output=True, text=True,  # portable: bash
                             timeout=30).stdout.split("\n")
        self.assertEqual(out[:3], ["mac=ru-RU", "linux=pt-BR", "c="])

    def test_versions_compare_as_numbers(self):
        script = os.path.join(self.dir, "v.sh")
        with open(script, "w") as f:
            f.write(body(SH) + "for p in '13.5 13.5' '13.10 13.5' '14 13.5' '13.4.9 13.5' '2.39 2.28' '2.3 2.28' "
                    "'10.15.7 13.5'; do set -- $p; version_at_least $1 $2 && echo \"$1>=$2 yes\" || echo \"$1>=$2 no\"; done\n")
        out = subprocess.run([BASH, script], capture_output=True, text=True, timeout=30).stdout.split("\n")  # portable: bash
        self.assertEqual(out[:7], ["13.5>=13.5 yes", "13.10>=13.5 yes", "14>=13.5 yes", "13.4.9>=13.5 no",
                                   "2.39>=2.28 yes", "2.3>=2.28 no", "10.15.7>=13.5 no"])

    def test_releases_json_is_read_without_jq_whatever_its_layout(self):
        entry = {"url": "/v1/app/agents-hub-v1.9.0-linux-x64.tar.gz", "sha256": "a" * 64, "size": 205900000,
                 "runtime": 4}
        doc = {"channels": {"stable": {"version": "1.9.0", "notes": 'braces { } and "shells": {"linux-x64": {}} in text'}},
               "runtime": {"current": 4, "apps": {"linux-x64": {"url": "/v1/app/WRONG.tar.gz", "sha256": "b" * 64,
                                                                "size": 1, "runtime": 9}},
                           "shells": {"mac-arm64": {"url": "/v1/app/m.tar.gz", "sha256": "c" * 64, "size": 2,
                                                    "runtime": 4}, "linux-x64": entry}},
               "scripts": {"install.sh": {"url": "/v1/script/install-1.9.0.sh", "sha256": "d" * 64, "size": 3,
                                          "version": "1.9.0"}}}
        for i, dump in enumerate((json.dumps(doc), json.dumps(doc, indent=1), json.dumps(doc, separators=(",", ":")))):
            p = os.path.join(self.dir, f"r{i}.json")
            with open(p, "w") as f:
                f.write(dump)
            script = os.path.join(self.dir, f"r{i}.sh")
            with open(script, "w") as f:
                f.write(body(SH) + f'e=$(shell_entry "{p}" linux-x64); for k in url sha256 size runtime; do '
                        'echo "$k=$(field "$e" $k)"; done; echo "none=$(shell_entry "' + p + '" windows-arm64)"\n')
            out = subprocess.run([BASH, script], capture_output=True, text=True, timeout=30).stdout  # portable: bash
            self.assertEqual(out.split("\n")[:5], [f"url={entry['url']}", f"sha256={'a' * 64}", "size=205900000",
                                                   "runtime=4", "none="], dump)


@unittest.skipIf(BASH is None or os.name == "nt", "install.sh is for macOS and Linux")
@cut.needs_deliver()
class Install(Stand):
    """The whole script against the stand, into a temporary home (this machine's platform; the stand serves linux-x64,
    so the build is pinned by stand-ins of uname on a Mac)."""

    def setUp(self):
        super().setUp()
        self.fake("uname", 'case "$1" in -s) echo Linux ;; -m) echo x86_64 ;; esac\n')
        self.fake("getconf", 'echo "glibc 2.39"\n')
        self.fake("df", 'echo "Filesystem 1024-blocks Used Available Capacity Mounted"; echo "x 1 1 99999999 1% /"\n')
        if not shutil.which("git"):
            self.fake("git", "exit 0\n")

    def test_install_hands_over_to_install_py_with_the_contract(self):
        r = self.install("--no-open", HUB_INSTALL="--channel main")
        self.assertEqual(r.returncode, 0, r.stderr)
        got = self.got()
        env = os.path.join(self.root, "shells", "1-team")
        argv = got["argv"]
        key_file = argv[argv.index("--key-file") + 1]
        self.assertEqual(argv, ["--contract", "1", "--env", env, "--server", self.url, "--key-file", key_file,
                                "--platform", "linux-x64", "--flavor", "team", "--runtime", "1", "--channel", "nightly",
                                "--no-open", "--no-input"])
        self.assertEqual(got["key"], READ)
        self.assertEqual((got["lang"], got["tty"]), ("ru-RU", False))
        self.assertEqual(got["log"], os.path.join(self.root, "install.log"))
        self.assertFalse(os.path.exists(key_file))                 # the key file goes with the run
        with open(os.path.join(env, ".agents-hub-shell.json")) as f:
            mark = json.load(f)
        self.assertEqual((mark["platform"], mark["sha256"], mark["runtime"]), ("linux-x64", sha(self.arc), 1))
        self.assertEqual(os.listdir(os.path.join(self.root, "downloads")), [])   # the archive is gone once in place
        with open(os.path.join(self.root, "install.log")) as f:
            log = f.read()
        self.assertNotIn(READ, log)
        self.assertNotIn(READ, r.stderr)
        self.assertEqual(os.stat(self.root).st_mode & 0o077, 0)

    def test_a_second_run_takes_the_environment_in_place(self):
        self.assertEqual(self.install().returncode, 0)
        os.unlink(self.out)
        r = self.install("--yes", "--move")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Downloading", r.stderr)
        self.assertEqual(self.got()["argv"][-3:], ["--move", "--yes", "--no-input"])

    def test_install_py_s_exit_code_is_the_scripts(self):
        r = self.install(STUB_EXIT="7")
        self.assertEqual(r.returncode, 7)

    def test_a_download_cut_short_goes_on_from_where_it_stopped(self):
        downloads = os.path.join(self.root, "downloads")
        os.makedirs(downloads)
        part = os.path.join(downloads, "agents-hub-v9.9.9-linux-x64.tar.gz.part")
        with open(part, "wb") as f:
            f.write(self.arc[:len(self.arc) // 2])
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(self.root, "install.log")) as f:
            self.assertIn(f"continuing agents-hub-v9.9.9-linux-x64.tar.gz from {len(self.arc) // 2}", f.read())
        # a part longer than the file is not a beginning of it: it starts over
        platforms.current.rmtree(os.path.join(self.root, "shells"))
        with open(part, "wb") as f:
            f.write(self.arc + b"more")
        self.assertEqual(self.install().returncode, 0)

    def test_a_wrong_sum_removes_the_download_and_installs_nothing(self):
        # the server names only what it stores, so the damage is on the way: a curl that flips a byte of an archive
        real = shutil.which("curl")
        self.fake("curl", f'''"{real}" "$@"; rc=$?; out=; prev=
for a in "$@"; do [ "$prev" = -o ] && out=$a; prev=$a; done
case $out in *.tar.gz.part) [ -f "$out" ] && printf X | dd of="$out" bs=1 seek=100 conv=notrunc 2>/dev/null ;; esac
exit $rc
''')
        r = self.install()
        self.assertEqual(r.returncode, 1)
        self.assertIn("does not match its sha256", r.stderr)
        self.assertEqual(os.listdir(os.path.join(self.root, "downloads")), [])
        self.assertFalse(os.path.exists(os.path.join(self.root, "shells", "1-team")))
        self.assertFalse(os.path.exists(self.out))

    def test_an_environment_that_unpacks_short_is_not_put_in_place(self):
        # what tar.exe does to a path over 260 characters, done here by a tar that skips a file and says nothing
        real = shutil.which("tar")
        self.fake("tar", f'"{real}" "$@" && rm -f "$4/paths.json"\n')
        r = self.install()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("missing after unpacking: paths.json", r.stderr)
        self.assertEqual(os.listdir(os.path.join(self.root, "shells")), [])

    def test_the_key_and_the_server_are_checked(self):
        r = self.install(HUB_DELIVER_KEY=None)
        self.assertIn("no build key", r.stderr)
        r = self.install(HUB_DELIVER_URL="http://203.0.113.5:8080")
        self.assertIn("must be an https address", r.stderr)
        self.assertFalse(os.path.exists(self.root))     # refused before anything was made
        r = self.install(HUB_DELIVER_KEY="not-the-key")
        self.assertEqual(r.returncode, 1)
        self.assertIn("does not take this build key", r.stderr)

    def test_no_environment_for_this_platform_and_no_installer_are_refusals(self):
        self.fake("uname", 'case "$1" in -s) echo Linux ;; -m) echo aarch64 ;; esac\n')
        r = self.install()
        self.assertIn("has no environment for linux-arm64", r.stderr)
        self.publish("linux-arm64", archive("linux-arm64", installer=False))
        r = self.install()
        self.assertEqual(r.returncode, 1)
        self.assertIn("has no installer (install.py)", r.stderr)

    def test_a_hub_folder_and_nobody_to_ask_is_the_installers_to_judge_not_the_script_s(self):
        # a folder of a hub with no service (its hub moved away) is no hub from git: the script said "most likely from
        # git, run with --move" and never the second hub's line; the installer reads the folder and says what it is
        state = os.path.join(self.home, ".hub-core")
        os.makedirs(state)
        with open(os.path.join(state, "journal.jsonl"), "w") as f:
            f.write('{"seq": 1}\n')
        r = self.install(STUB_EXIT="3")
        self.assertEqual(r.returncode, 3, r.stderr)     # the installer's code, the script adds no words of its own
        self.assertNotIn("most likely from git", r.stderr)
        self.assertNotIn("--move", r.stderr)
        argv = self.got()["argv"]
        self.assertIn("--no-input", argv)               # handed over: nobody to ask goes with it
        self.assertNotIn("--move", argv)
        self.assertNotIn("--stay", argv)
        self.assertTrue(os.path.isdir(os.path.join(self.root, "shells")))
        self.assertEqual(os.listdir(state), ["journal.jsonl"])
        r = self.install("--stay")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--stay", self.got()["argv"])

    def test_a_copy_that_names_no_server_says_so(self):
        r = self.install(HUB_DELIVER_URL=None)
        self.assertEqual(r.returncode, 1)
        self.assertIn("names no delivery server", r.stderr)

    def test_the_command_line_answer_wins_over_the_one_in_hub_install(self):
        r = self.install("--stay", HUB_INSTALL="--move --yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = self.got()["argv"]
        self.assertIn("--stay", argv)
        self.assertNotIn("--move", argv)
        self.assertIn("--yes", argv)
        self.assertEqual(self.install("--move", "--stay").returncode, 64)       # two answers on one line stay a mistake
        self.assertEqual(self.install(HUB_INSTALL="--move --stay").returncode, 64)

    def test_an_environment_left_without_its_installer_gets_a_whole_one_next_to_it(self):
        self.fake("uname", 'case "$1" in -s) echo Linux ;; -m) echo aarch64 ;; esac\n')
        self.publish("linux-arm64", archive("linux-arm64", installer=False))
        self.assertIn("has no installer", self.install().stderr)
        self.publish("linux-arm64", archive("linux-arm64", top="agents-hub-v9.9.10-linux-arm64"), tag="v9.9.10")
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        shells = sorted(os.listdir(os.path.join(self.root, "shells")))
        self.assertEqual(len(shells), 2, shells)
        self.assertEqual(shells[0], "1-team")   # left as it is: a running hub may hold its files
        self.assertRegex(shells[1], r"^1-team-\d+$")
        env = self.got()["argv"][self.got()["argv"].index("--env") + 1]
        self.assertEqual(env, os.path.join(self.root, "shells", shells[1]))
        self.assertTrue(os.path.isfile(os.path.join(env, "install.py")))

    def broken_run(self, damage):
        """A first install, the damage done to its environment, a second run: -> (the first one, the second's --env)."""
        self.assertEqual(self.install().returncode, 0)
        first = os.path.join(self.root, "shells", "1-team")
        damage(first)
        os.unlink(self.out)
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = self.got()["argv"]
        return first, argv[argv.index("--env") + 1]

    def test_a_file_of_the_environment_changed_or_gone_is_a_new_one_next_to_it(self):
        def changed(env):   # the same size, other bytes: only its sum tells
            path = os.path.join(env, "paths.json")
            with open(path, "w") as f:
                f.write("[]")
        first, env = self.broken_run(changed)
        self.assertRegex(os.path.basename(env), r"^1-team-\d+$")
        self.assertTrue(os.path.isfile(os.path.join(first, "paths.json")))   # the broken one is not touched
        with open(os.path.join(self.root, "install.log")) as f:
            self.assertIn("is broken: not as unpacked: paths.json", f.read())
        with open(os.path.join(env, "paths.json")) as f:
            self.assertEqual(f.read(), "{}")
        os.unlink(self.out)   # the broken one again, in the same second: one more name, not into the one just made
        self.assertEqual(self.install().returncode, 0)
        argv = self.got()["argv"]
        third = argv[argv.index("--env") + 1]
        self.assertNotIn(third, (first, env))
        self.assertEqual(len(os.listdir(os.path.join(self.root, "shells"))), 3)

    def test_a_file_of_the_environment_gone_is_a_new_one_next_to_it(self):
        first, env = self.broken_run(lambda e: os.unlink(os.path.join(e, "runtime", "python", "lib", "stale.py")))
        self.assertNotEqual(env, first)
        with open(os.path.join(self.root, "install.log")) as f:
            self.assertIn("is broken: missing: runtime/python/lib/stale.py", f.read())

    def test_an_environment_without_its_sheet_is_not_taken_as_whole(self):
        # an install before the sheet: nothing says what it should hold
        first, env = self.broken_run(lambda e: os.unlink(os.path.join(e, ".agents-hub-shell.files")))
        self.assertNotEqual(env, first)
        with open(os.path.join(self.root, "install.log")) as f:
            self.assertIn("no sheet of the environment's files", f.read())

    def test_the_whole_environment_the_hub_runs_on_is_taken_as_it_is(self):
        self.assertEqual(self.install().returncode, 0)
        shells = os.path.join(self.root, "shells")
        os.rename(os.path.join(shells, "1-team"), os.path.join(shells, "1-team-17"))   # a repair of before
        state = os.path.join(self.home, ".hub-core")
        os.makedirs(state)
        os.symlink(os.path.join(shells, "1-team-17"), os.path.join(state, "app"))
        os.unlink(self.out)
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Downloading", r.stderr)
        argv = self.got()["argv"]
        self.assertEqual(argv[argv.index("--env") + 1], os.path.join(shells, "1-team-17"))

    def test_with_wget_alone_the_servers_answer_is_still_told_apart(self):
        if not shutil.which("wget"):
            self.skipTest("no wget here")
        bare = self.bare_path(drop=("curl",))
        r = subprocess.run([BASH, SH], env=self.env(PATH=bare, HUB_DELIVER_KEY="not-the-key"), capture_output=True,  # portable: bash
                           text=True, stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertIn("does not take this build key", r.stderr)
        r = subprocess.run([BASH, SH], env=self.env(PATH=bare), capture_output=True, text=True,  # portable: bash
                           stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.got()["key"], READ)
        # wget's own code is only "a server error": a 503 is not "nothing is published"
        busy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Busy)
        threading.Thread(target=busy.serve_forever, daemon=True).start()
        self.addCleanup(busy.server_close)
        self.addCleanup(busy.shutdown)
        r = subprocess.run([BASH, SH], env=self.env(PATH=bare, HUB_DELIVER_URL=f"http://127.0.0.1:{busy.server_address[1]}"),  # portable: bash
                           capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
        self.assertIn("answered 503 to releases.json", r.stderr)

    def test_flags(self):
        r = self.install(HUB_INSTALL="--chanel main")
        self.assertEqual(r.returncode, 64)
        self.assertIn("unknown flag in HUB_INSTALL: --chanel", r.stderr)      # where the word came from
        for args, code, said in ((["--move", "--stay"], 64, "two answers"), (["--channel", "beta"], 64, "stable or main"),
                                 (["--instance", "Bad_Name"], 64, "small letters"), (["--what"], 64, "unknown flag"),
                                 (["--channel"], 64, "needs a value")):
            r = self.install(*args)
            self.assertEqual(r.returncode, code, args)
            self.assertIn(said, r.stderr, args)
        self.assertEqual(self.install("--help").returncode, 0)
        r = self.install("--instance=work", "--channel=stable", HUB_INSTALL="--channel main --no-open", HUB_STATE_DIR="/x")
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = self.got()["argv"]
        self.assertEqual(argv[argv.index("--channel") + 1], "stable")     # the command line wins over HUB_INSTALL
        self.assertEqual(argv[-6:], ["--state", "/x", "--instance", "work", "--no-open", "--no-input"])

    def test_root_no_room_and_no_git_are_refused(self):
        self.fake("id", 'echo 0\n')
        self.assertIn("not as root", self.install().stderr)
        os.unlink(os.path.join(self.bin, "id"))
        self.fake("df", 'echo "Filesystem 1024-blocks Used Available Capacity Mounted"; echo "x 1 1 1000 1% /"\n')
        r = self.install()
        self.assertIn("needs about 2 GB free", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.root, "shells")))

    def bare_path(self, drop=(), manager="apt-get"):
        """A PATH of only the programs the script uses (and the stand-ins), less `drop`; `manager` — the system's package
        manager (a stand-in)."""
        bare = os.path.join(self.dir, "bare-" + "-".join(drop) + "-" + manager)
        os.makedirs(bare)
        for p in ("bash", "uname", "getconf", "tar", "gzip", "sha256sum", "shasum", "curl", "wget", "git", "mkdir", "cat", "head",
                  "tr", "sed", "awk", "df", "date", "id", "rm", "mv", "ls", "grep", "cut", "wc", "mktemp", "sleep",
                  "tail", "hostname", "dirname", "sh", "env"):
            real = shutil.which(p, path=self.bin + os.pathsep + os.environ.get("PATH", ""))
            if real and p not in drop:
                os.symlink(real, os.path.join(bare, p))   # portable: the class is skipped on Windows
        with open(os.path.join(bare, manager), "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(os.path.join(bare, manager), 0o755)
        return bare

    def test_without_git_it_says_how_to_get_it(self):
        bare = self.bare_path(drop=("git",))
        r = subprocess.run([BASH, SH], env=self.env(PATH=bare), capture_output=True, text=True,  # portable: bash
                           stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no git; install it (sudo apt install git)", r.stderr)

    def test_without_git_the_command_is_the_system_s_package_manager_s(self):
        for manager, said in (("dnf", "sudo dnf install git"), ("zypper", "sudo zypper install git"),
                              ("pacman", "sudo pacman -S git")):
            bare = self.bare_path(drop=("git",), manager=manager)
            r = subprocess.run([BASH, SH], env=self.env(PATH=bare), capture_output=True, text=True,  # portable: bash
                               stdin=subprocess.DEVNULL, timeout=60)
            self.assertEqual(r.returncode, 1)
            self.assertIn(f"no git; install it ({said})", r.stderr)

    def git_stand(self, manager="apt-get", nopasswd=False):
        """A machine with no git, a sudo (stand-in) and a package manager (stand-in) that, asked to install, puts git on the
        PATH and writes down what it was asked. sudo: `-n` answers by `nopasswd`; without it, the password "secret" is read
        from the terminal."""
        bare = self.bare_path(drop=("git",), manager=manager)
        self.asked = os.path.join(self.dir, "asked-" + manager + str(nopasswd))
        with open(os.path.join(bare, manager), "w") as f:
            f.write(f'#!/bin/sh\necho "$*" >> {self.asked}\n'
                    f'case " $* " in *" install "*|*" -S "*) printf "#!/bin/sh\\nexit 0\\n" > {bare}/git; {shutil.which("chmod")} 755 {bare}/git ;; esac\n')
        with open(os.path.join(bare, "sudo"), "w") as f:
            f.write("#!/bin/sh\n"
                    'if [ "$1" = -n ]; then ' + ("exit 0" if nopasswd else "exit 1") + "; fi\n"
                    + ('exec "$@"\n' if nopasswd else "") +
                    'printf "Password: " > /dev/tty; read pw < /dev/tty\n'
                    '[ "$pw" = secret ] || { echo "sudo: Sorry, try again." >&2; exit 1; }\nexec "$@"\n')
        for p in (manager, "sudo"):
            os.chmod(os.path.join(bare, p), 0o755)
        return bare

    def on_terminal(self, bare, password):
        """The script run on a terminal (a pty with the script's own session), the password typed when asked."""
        import pty
        import select
        import time
        pid, fd = pty.fork()   # portable: the class is skipped on Windows
        if pid == 0:
            os.execve(BASH, [BASH, SH], self.env(PATH=bare))  # portable: bash
        out = b""
        sent = False
        end = time.time() + 60
        while time.time() < end:
            if not select.select([fd], [], [], 1)[0]:
                continue
            try:
                got = os.read(fd, 4096)
            except OSError:
                break
            if not got:
                break
            out += got
            if not sent and b"Password:" in out:
                os.write(fd, password.encode() + b"\n")
                sent = True
        os.close(fd)
        try:
            os.kill(pid, 9)   # a script that is still waiting is no result
        except OSError:
            pass
        _, status = os.waitpid(pid, 0)
        return os.waitstatus_to_exitcode(status), out.decode(errors="replace")

    def test_without_git_it_installs_it_when_sudo_asks_for_no_password(self):
        for manager, said in (("apt-get", "-q -y install git"), ("dnf", "install -y -q git"),
                              ("yum", "install -y -q git"), ("zypper", "--non-interactive -q install git"),
                              ("pacman", "-S --noconfirm --needed git")):
            with self.subTest(manager=manager):
                bare = self.git_stand(manager, nopasswd=True)
                r = subprocess.run([BASH, SH], env=self.env(PATH=bare), capture_output=True, text=True,  # portable: bash
                                   stdin=subprocess.DEVNULL, timeout=60, start_new_session=True)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("installing git", r.stderr)
                self.assertNotIn("install it", r.stderr)
                with open(self.asked) as f:
                    self.assertIn(said, f.read())

    def test_without_git_and_no_way_to_sudo_nothing_is_installed(self):
        # sudo wants a password and there is no terminal to ask on (a pipe from a script, a job): the old refusal, no call
        bare = self.git_stand()
        r = subprocess.run([BASH, SH], env=self.env(PATH=bare), capture_output=True, text=True,  # portable: bash
                           stdin=subprocess.DEVNULL, timeout=60, start_new_session=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no git; install it (sudo apt install git)", r.stderr)
        self.assertFalse(os.path.exists(self.asked))
        self.assertFalse(os.path.exists(os.path.join(self.root, "shells")))

    def test_without_git_it_asks_sudo_for_the_password_on_the_terminal(self):
        code, out = self.on_terminal(self.git_stand(), "secret")
        self.assertEqual(code, 0, out)
        self.assertIn("installing git: sudo asks for your password", out)
        self.assertNotIn("install it", out)
        with open(self.asked) as f:
            self.assertIn("install git", f.read())

    def test_without_git_a_wrong_password_is_the_old_refusal(self):
        bare = self.git_stand()
        code, out = self.on_terminal(bare, "wrong")
        self.assertEqual(code, 1, out)
        self.assertIn("no git; install it (sudo apt install git)", out)
        self.assertFalse(os.path.exists(self.asked))

    def test_without_git_a_manager_that_fails_is_the_old_refusal(self):
        bare = self.git_stand(nopasswd=True)
        with open(os.path.join(bare, "apt-get"), "w") as f:
            f.write("#!/bin/sh\nexit 100\n")
        r = subprocess.run([BASH, SH], env=self.env(PATH=bare), capture_output=True, text=True,  # portable: bash
                           stdin=subprocess.DEVNULL, timeout=60, start_new_session=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("apt-get did not install git", r.stderr)
        self.assertIn("no git; install it (sudo apt install git)", r.stderr)


class OpenText(unittest.TestCase):
    """The open build's copy of the scripts (tools/package/pages.py writes its address and flavor in) and the one list of
    the hosts its environments may come from."""

    def test_both_scripts_carry_the_hosts_of_core_fetch(self):
        from core import fetch
        sh = re.findall(r"(?m)^HUB_INSTALL_ASSET_HOSTS='([^']*)'", text(SH).decode())
        self.assertEqual(len(sh), 1)
        self.assertEqual(tuple(sh[0].split()), fetch.ASSET_HOSTS)
        ps = re.findall(r"(?m)^  \$AssetHosts = @\(([^)]*)\)", text(PS1).decode())
        self.assertEqual(len(ps), 1)
        self.assertEqual(tuple(re.findall(r"'([^']*)'", ps[0])), fetch.ASSET_HOSTS)

    SERVER_CASES = (("https://pages.example.test/agents-hub", True), ("http://127.0.0.1:8781", True),
                    ("http://127.0.0.1:8781/agents-hub", True), ("http://localhost/a/b/", True),
                    ("http://[::1]:9/x", True), ("http://203.0.113.5/agents-hub", False),
                    ("http://127.0.0.1.example.test/x", False), ("http://127.0.0.1@example.test/x", False),
                    ("http://localhost:80@example.test", False), ("ftp://127.0.0.1/x", False))

    def test_both_scripts_take_http_only_to_this_machine_with_or_without_a_path(self):
        # the stand of the open build's pages is at <host>:<port>/<repo>: install.ps1 takes a path as install.sh does
        rule = re.search(r"-notmatch '(\^https://)' -and \$u -notmatch '(\^http://[^']*)'", text(PS1).decode())
        self.assertTrue(rule)
        got = [bool(re.search(rule.group(1), u) or re.search(rule.group(2), u)) for u, _ in self.SERVER_CASES]
        self.assertEqual(got, [ok for _, ok in self.SERVER_CASES])

    @unittest.skipIf(BASH is None or os.name == "nt", "install.sh is for macOS and Linux")
    def test_install_sh_takes_the_same_addresses(self):
        d = tempfile.mkdtemp(prefix="oneline-server-")
        self.addCleanup(platforms.current.rmtree, d)
        script = os.path.join(d, "server.sh")
        with open(script, "w") as f:
            f.write(body(SH) + 'refuse() { return 0; }\nfor u in "$@"; do HUB_DELIVER_URL=$u; '
                               'if server_url >/dev/null; then echo 1; else echo 0; fi; done\n')
        r = subprocess.run([BASH, script, *[u for u, _ in self.SERVER_CASES]], capture_output=True, text=True,
                           timeout=60)  # portable: bash
        self.assertEqual(r.stdout.split(), ["1" if ok else "0" for _, ok in self.SERVER_CASES], r.stderr)

    @unittest.skipIf(BASH is None or os.name == "nt", "install.sh is for macOS and Linux")
    def test_install_sh_tells_the_addresses_as_core_fetch_does(self):
        from tests.fakes import pages
        d = tempfile.mkdtemp(prefix="oneline-asset-")
        self.addCleanup(platforms.current.rmtree, d)
        script = os.path.join(d, "asset.sh")
        with open(script, "w") as f:
            f.write(body(SH) + 'while [ $# -gt 0 ]; do SERVER=$1; if asset_ok "$2"; then echo 1; else echo 0; fi; shift 2; '
                               'done\n')
        args = [w for url, server, _ in pages.ASSET_CASES for w in (server, url)]
        r = subprocess.run([BASH, script, *args], capture_output=True, text=True, timeout=60)  # portable: bash
        self.assertEqual(r.stdout.split(), ["1" if ok else "0" for _, _, ok in pages.ASSET_CASES], r.stderr)


POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")   # Windows PowerShell 5.1, or a PowerShell 7


def ps_function(name):
    """The text of one helper of install.ps1 (they are nested in Install-AgentsHub, two spaces in)."""
    m = re.search(rf"(?ms)^  function {re.escape(name)}\b.*?^  }}$", text(PS1).decode())
    assert m, name
    return m.group(0)


@unittest.skipIf(POWERSHELL is None, "no PowerShell here (powershell.exe on Windows, pwsh elsewhere)")
class OpenPowerShell(unittest.TestCase):
    """install.ps1's own helpers of the open build run by a real PowerShell: the address rule (the table core/fetch and
    install.sh answer), and a download through github's redirect to the files' storage, with no key."""

    def run_ps(self, code):
        d = tempfile.mkdtemp(prefix="oneline-ps-")
        self.addCleanup(platforms.current.rmtree, d)
        path = os.path.join(d, "probe.ps1")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write("$ErrorActionPreference = 'Stop'\n$UA = 'agents-hub-install/1 (test)'\n"
                    + re.search(r"(?m)^  \$AssetHosts = .*$", text(PS1).decode()).group(0).strip() + "\n"
                    + "function Note([string]$text) { }\n"
                    + "\n".join(ps_function(n) for n in ("Server-Host", "Asset-Ok", "Resolve-Url", "Fetch")) + "\n"
                    + code)
        r = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", path],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                           stdin=subprocess.DEVNULL)
        return r

    def test_the_team_build_takes_only_a_plain_name_of_its_server(self):
        from tests.fakes import pages
        root = tempfile.mkdtemp(prefix="oneline-ps-rel-")
        self.addCleanup(platforms.current.rmtree, root)
        page, github, files, asked = pages.places(self, root, root)
        os.makedirs(os.path.join(root, "v1"))
        got = []
        for flavor, url in (("team", "/v1/app/a-linux.tar.gz"), ("team", "/v1/app/a-linux.tar.gz?y=1"),
                            ("team", "/v1/app/../layer/a-linux.tar.gz"), ("team", "https://github.com/a/a-linux.tar.gz"),
                            ("open", "https://github.com/a/a-linux.tar.gz?x=1"), ("open", "/v1/app/a-linux.tar.gz"),
                            ("open", "https://downloads.example.test/a-linux.tar.gz")):
            with open(os.path.join(root, "v1", "releases.json"), "w") as f:
                json.dump({"runtime": {"shells": {"windows-x64": {"url": url, "sha256": "0" * 64, "size": 5,
                                                                  "runtime": 1}}}}, f)
            r = self.run_ps(f"$Flavor = '{flavor}'\n"
                            "function Refuse([string]$text, [int]$code = 1) { throw \"HUBREFUSE:${code}:$text\" }\n"
                            + ps_function("Fetch-Hint") + "\n" + ps_function("Read-Release") + "\n"
                            + f"$S = @{{ Server = '{page.url}/agents-hub'; Key = 'k'; FetchError = '' }}\n"
                            "try { $r = Read-Release '" + root + "' 'windows-x64'; Write-Output ('ok ' + $r.archive) } "
                            "catch { Write-Output 'refused' }\n")
            got.append(r.stdout.strip() or r.stderr)
        self.assertEqual(got, ["ok a-linux.tar.gz", "refused", "refused", "refused", "ok a-linux.tar.gz",
                               "ok a-linux.tar.gz", "refused"])

    def test_the_address_rule_is_the_one_of_core_fetch(self):
        from tests.fakes import pages
        d = tempfile.mkdtemp(prefix="oneline-ps-cases-")
        self.addCleanup(platforms.current.rmtree, d)
        cases = os.path.join(d, "cases.json")
        with open(cases, "w", encoding="utf-8") as f:
            json.dump([[u, srv] for u, srv, _ in pages.ASSET_CASES], f)
        r = self.run_ps(f"$S = @{{ Server = '' }}\n"
                        f"foreach ($c in (Get-Content -Raw -Encoding UTF8 -LiteralPath '{cases}' | ConvertFrom-Json)) "
                        "{ $S.Server = $c[1]; if (Asset-Ok $c[0]) { Write-Output 1 } else { Write-Output 0 } }\n")
        self.assertEqual(r.stdout.split(), ["1" if ok else "0" for _, _, ok in pages.ASSET_CASES], r.stderr)

    def test_a_download_follows_the_redirect_with_no_key_and_stops_off_the_hosts(self):
        from tests.fakes import pages
        root = tempfile.mkdtemp(prefix="oneline-ps-files-")
        self.addCleanup(platforms.current.rmtree, root)
        data = b"the environment's bytes\n" * 100
        with open(os.path.join(root, "e.tar.gz"), "wb") as f:
            f.write(data)
        page, github, files, asked = pages.places(self, root, root)
        github.redirects["/r/e.tar.gz"] = files.url + "/e.tar.gz?sig=a%2Bb"
        github.redirects["/r/away.tar.gz"] = "http://203.0.113.5/e.tar.gz"
        out = os.path.join(root, "got")
        r = self.run_ps(f"$S = @{{ Server = '{page.url}/agents-hub'; Key = ''; FetchError = '' }}\n"
                        f"$u = Resolve-Url '{github.url}/r/e.tar.gz'\n"
                        f"Write-Output (Fetch $u '{out}' 0 0)\n"
                        f"Write-Output ('[' + (Resolve-Url '{github.url}/r/away.tar.gz') + ']')\n")
        self.assertEqual(r.stdout.split(), ["200", "[]"], r.stderr)
        with open(out, "rb") as f:
            self.assertEqual(f.read(), data)
        self.assertEqual({auth for _, _, auth in asked}, {None})
        # github, the files (the redirect's look and the download), github again — never the address it is sent off to
        self.assertEqual([w for w, _, _ in asked], ["github", "files", "files", "github"])


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows' own tar.exe")
class WindowsUnpack(unittest.TestCase):
    """install.ps1's Unpack with Windows' tar.exe under the folder of a user named in letters outside the ANSI code page,
    with a space: tar.exe reads its arguments in that code page, and the archive's path came to it as "???? ????"."""

    def test_the_environment_unpacks_under_a_folder_named_in_other_letters(self):
        base = tempfile.mkdtemp(prefix="oneline-ps-tar-")
        self.addCleanup(platforms.current.rmtree, base)
        root = os.path.join(base, "Ελένη Παπά ☃")
        downloads, work = os.path.join(root, "downloads"), os.path.join(root, "downloads", "w1")
        os.makedirs(work)
        a = os.path.join(downloads, "agents-hub-v9.9.9-windows-x64.tar.gz.part")
        with open(a, "wb") as f:
            f.write(archive("windows-x64", extra=[("runtime/python/python.exe", b"MZ")], link=False))
        with open(os.path.join(work, "unpack.py"), "w", encoding="utf-8") as f:
            f.write(HELPER.search(text(PS1).decode()).group(0))
        env_dir = os.path.join(root, "shells", "1-team")
        os.makedirs(os.path.dirname(env_dir))
        path = os.path.join(base, "probe.ps1")
        with open(path, "w", encoding="utf-8", newline="\n") as f:   # every path in a variable: the file stays ASCII
            f.write("$ErrorActionPreference = 'Stop'\n"
                    "$S = @{ Log = $null; Platform = 'windows-x64' }\n$Flavor = 'team'\n"
                    "function Note([string]$text) { }\nfunction Say([string]$text) { }\n"
                    "function Refuse([string]$text, [int]$code = 1) { throw \"HUBREFUSE:${code}:$text\" }\n"
                    # the archive's python.exe is a stand-in: the unpack program runs on this test's Python
                    "function Run-Native([string]$exe, [string[]]$argv) {\n"
                    "  if ($exe -like '*\\python.exe') { $exe = $env:T_PYTHON }\n"
                    "  $old = $ErrorActionPreference\n  $ErrorActionPreference = 'Continue'\n"
                    "  try { $out = & $exe @argv 2>&1 | ForEach-Object { \"$_\" }\n"
                    "        return @{ code = $LASTEXITCODE; out = (@($out) -join \"`n\") } }\n"
                    "  finally { $ErrorActionPreference = $old }\n}\n"
                    + "\n".join(ps_function(n) for n in ("Remove-Tree", "Set-Env", "Unpack")) + "\n"
                    "$rel = @{ runtime = 1; archive = 'agents-hub-v9.9.9-windows-x64.tar.gz'; sha256 = '0' }\n"
                    "$tar = Join-Path $env:SystemRoot 'System32\\tar.exe'\n"
                    "try { Unpack -rel $rel -archive $env:T_ARCHIVE -envDir $env:T_ENV -tar $tar -work $env:T_WORK;"
                    " Write-Output 'unpacked' } catch { Write-Output \"$_\" }\n")
        r = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", path],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
                           stdin=subprocess.DEVNULL, env=dict(os.environ, T_PYTHON=sys.executable, T_ARCHIVE=a,
                                                             T_ENV=env_dir, T_WORK=work))
        self.assertEqual(r.stdout.strip().splitlines()[-1:], ["unpacked"], r.stdout + r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(env_dir, "runtime", "python", "python.exe")))
        with open(os.path.join(env_dir, ".agents-hub-shell.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["top"], "agents-hub-v9.9.9-windows-x64")
        self.assertEqual(sorted(os.listdir(downloads)), ["w1"])   # the archive and tar.exe's Python are gone


@unittest.skipIf(BASH is None or os.name == "nt", "install.sh is for macOS and Linux")
class OpenInstall(Stand):
    """The open build's line against its stand: static pages with releases.json (no key), the environment an asset of a
    GitHub release — github answers with a redirect to the files' storage (tests/fakes/pages.py). The script asks for no
    key, sends none and follows a redirect only where core/fetch.asset_ok would."""

    ASSET = "/acme/agents-hub/releases/download/v9.9.9/agents-hub-v9.9.9-open-linux-x64.tar.gz"

    def setUp(self):
        super().setUp()
        from tests.fakes import pages
        self.fake("uname", 'case "$1" in -s) echo Linux ;; -m) echo x86_64 ;; esac\n')
        self.fake("getconf", 'echo "glibc 2.39"\n')
        self.fake("df", 'echo "Filesystem 1024-blocks Used Available Capacity Mounted"; echo "x 1 1 99999999 1% /"\n')
        if not shutil.which("git"):
            self.fake("git", "exit 0\n")
        self.pages_mod = load("pages_o", os.path.join(ROOT, "tools", "package", "pages.py"))
        self.pages_root = os.path.join(self.dir, "pages")
        self.files_root = os.path.join(self.dir, "files")
        os.makedirs(self.files_root)
        self.place, self.github, self.files, self.asked = pages.places(self, self.pages_root, self.files_root)
        self.base = self.place.url + "/agents-hub"
        name = os.path.basename(self.ASSET)
        with open(os.path.join(self.files_root, name), "wb") as f:
            f.write(self.arc)
        self.github.redirects[self.ASSET] = f"{self.files.url}/{name}?sig=a%2Bb&se=2026-11-11T10%3A00%3A00Z"
        self.write_releases(self.github.url + self.ASSET)
        self.script = os.path.join(self.dir, "install-open.sh")
        with open(self.script, "wb") as f:
            f.write(self.pages_mod.open_script("install.sh", text(SH), self.base))

    def write_releases(self, url):
        layer = {"version": "9.9.9", "commit": "c0ffee", "runtime_min": 1,
                 "archive": {"name": "code-9.9.9-open.tar.gz", "sha256": "0" * 64, "size": 1}}
        doc = self.pages_mod.releases(layer, {"linux-x64": {"archive": {"name": os.path.basename(self.ASSET),
                                                                         "sha256": sha(self.arc), "size": len(self.arc)},
                                                             "runtime": 1}}, os.path.dirname(url))
        doc["runtime"]["shells"]["linux-x64"]["url"] = url
        os.makedirs(os.path.join(self.pages_root, "v1"), exist_ok=True)
        with open(os.path.join(self.pages_root, "v1", "releases.json"), "w") as f:
            json.dump(doc, f, indent=1)

    def install_open(self, *args, **extra):
        env = self.env(HUB_DELIVER_URL=None, HUB_DELIVER_KEY=None, **extra)
        return subprocess.run([BASH, self.script, *args], env=env, capture_output=True, text=True,  # portable: bash
                              stdin=subprocess.DEVNULL, timeout=120, start_new_session=True)

    def where(self):
        return [(w, p.split("?")[0]) for w, p, _ in self.asked]

    def test_the_line_needs_no_key_and_sends_none(self):
        r = self.install_open("--no-open")
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = self.got()["argv"]
        env = os.path.join(self.root, "shells", "1-open")
        self.assertEqual(argv, ["--contract", "1", "--env", env, "--server", self.base, "--platform", "linux-x64",
                                "--flavor", "open", "--runtime", "1", "--channel", "stable", "--no-open", "--no-input"])
        self.assertIsNone(self.got()["key"])
        where = self.where()
        self.assertIn(("pages", "/agents-hub/v1/releases.json"), where)
        self.assertIn(("github", self.ASSET), where)
        self.assertIn(("files", "/" + os.path.basename(self.ASSET)), where)
        self.assertEqual({auth for _, _, auth in self.asked}, {None})
        with open(os.path.join(env, ".agents-hub-shell.json")) as f:
            mark = json.load(f)
        self.assertEqual((mark["flavor"], mark["archive"], mark["sha256"]), ("open", os.path.basename(self.ASSET),
                                                                             sha(self.arc)))

    def test_a_download_cut_short_goes_on_through_the_redirect(self):
        downloads = os.path.join(self.root, "downloads")
        os.makedirs(downloads)
        with open(os.path.join(downloads, os.path.basename(self.ASSET) + ".part"), "wb") as f:
            f.write(self.arc[:len(self.arc) // 2])
        r = self.install_open()
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(self.root, "install.log")) as f:
            self.assertIn(f"continuing {os.path.basename(self.ASSET)} from {len(self.arc) // 2}", f.read())

    def test_a_redirect_off_the_hosts_of_the_releases_is_refused(self):
        for away in ("http://203.0.113.5/x.tar.gz", "https://downloads.example.test/x.tar.gz"):
            self.asked.clear()
            self.github.redirects[self.ASSET] = away
            r = self.install_open()
            self.assertEqual(r.returncode, 1, r.stderr)
            self.assertIn("did not complete", r.stderr)
            self.assertNotIn("files", {w for w, _ in self.where()})
            self.assertFalse(os.path.exists(os.path.join(self.root, "shells", "1-open")))
            self.assertFalse(os.path.exists(self.out))

    def test_an_environment_named_off_the_hosts_is_refused_before_any_download(self):
        self.write_releases("https://downloads.example.test/a/agents-hub-v9.9.9-open-linux-x64.tar.gz")
        r = self.install_open()
        self.assertEqual(r.returncode, 1)
        self.assertIn("is not of the form this script knows", r.stderr)
        self.assertEqual(self.where(), [("pages", "/agents-hub/v1/releases.json")])

    def test_a_wrong_sum_removes_the_download(self):
        with open(os.path.join(self.files_root, os.path.basename(self.ASSET)), "wb") as f:
            f.write(self.arc[:-1] + bytes([self.arc[-1] ^ 1]))
        r = self.install_open()
        self.assertEqual(r.returncode, 1)
        self.assertIn("does not match its sha256", r.stderr)
        self.assertEqual(os.listdir(os.path.join(self.root, "downloads")), [])

    def test_with_wget_alone_it_follows_the_same_redirect(self):
        if not shutil.which("wget"):
            self.skipTest("no wget here")
        bare = Install.bare_path(self, drop=("curl",))
        r = self.install_open(PATH=bare)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(("files", "/" + os.path.basename(self.ASSET)), self.where())
        self.assertEqual({auth for _, _, auth in self.asked}, {None})
        self.asked.clear()
        self.github.redirects[self.ASSET] = "http://203.0.113.5/x.tar.gz"
        platforms.current.rmtree(os.path.join(self.root, "shells"))
        r = self.install_open(PATH=bare)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertNotIn("files", {w for w, _ in self.where()})

    def test_the_team_line_takes_only_a_plain_name_of_its_server(self):
        # as before the open build's lines: a query or another folder of the server is not its form
        for url in ("/v1/app/agents-hub-v9.9.9-linux-x64.tar.gz?y=1", "/v1/layer/agents-hub-v9.9.9-linux-x64.tar.gz",
                    "/v1/app/../layer/agents-hub-v9.9.9-linux-x64.tar.gz", "/v1/app/a/agents-hub-v9.9.9-linux-x64.tar.gz"):
            self.write_releases(url)
            self.asked.clear()
            r = self.install(HUB_DELIVER_URL=self.base)
            self.assertEqual(r.returncode, 1, url)
            self.assertIn("is not of the form this script knows", r.stderr)
            self.assertEqual(self.where(), [("pages", "/agents-hub/v1/releases.json")])

    def test_the_team_line_still_takes_its_own_server_only(self):
        # the team's script against pages that name an asset of a release: not its form, nothing downloaded
        r = self.install(HUB_DELIVER_URL=self.base)
        self.assertEqual(r.returncode, 1)
        self.assertIn("is not of the form this script knows", r.stderr)
        self.assertEqual(self.where(), [("pages", "/agents-hub/v1/releases.json")])
        self.assertEqual({auth for _, _, auth in self.asked}, {f"Bearer {READ}"})   # with its key, as ever
        r = self.install(HUB_DELIVER_KEY=None)   # and still its key
        self.assertIn("no build key", r.stderr)


@unittest.skipIf(BASH is None or os.name == "nt", "install.sh is for macOS and Linux")
@cut.needs_deliver()
class Lock(Stand):
    """One install at a time: the second waits for the first, a lock whose holder is gone is taken."""

    def take(self, wait=4):
        script = os.path.join(self.dir, "lock.sh")
        with open(script, "w") as f:
            f.write(body(SH) + f'ROOT="{self.dir}/root"; mkdir -p "$ROOT"; HUB_INSTALL_LOCK_WAIT={wait}\n'
                    'take_lock && echo "TOOK $(cat "$LOCK/owner" | cut -d" " -f1)"\n')
        return subprocess.run([BASH, script], capture_output=True, text=True, timeout=60)  # portable: bash

    def hold(self, pid):
        os.makedirs(os.path.join(self.dir, "root", "install.lock"))
        with open(os.path.join(self.dir, "root", "install.lock", "owner"), "w") as f:
            f.write(f"pid={pid} host=x since=2026-11-08T00:00:00Z\n")

    def test_a_live_holder_is_waited_for_and_then_it_is_a_refusal(self):
        self.hold(os.getpid())
        r = self.take(wait=2)
        self.assertNotIn("TOOK", r.stdout)
        self.assertIn("Another install of the hub is running (pid=%d" % os.getpid(), r.stderr)
        self.assertIn("another install still runs", r.stderr)

    def test_a_lock_whose_holder_is_gone_is_taken(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        self.hold(p.pid)
        r = self.take()
        self.assertIn("TOOK pid=", r.stdout, r.stderr)


# The system programs --server calls, as stand-ins over a system root in a temporary folder (HUB_INSTALL_SYSROOT):
# users live in $SYS/etc/passwd, what was installed in $SYS/installed, every call is written to $SYS/calls.
SYSTEM_FAKES = {
    "id": '''case "$1" in
  -u) [ -z "$2" ] && { echo "${FAKE_UID:-0}"; exit 0; }
      grep "^$2:" "$SYS/etc/passwd" | cut -d: -f3 | grep . || exit 1 ;;
  -nG) grep -q "^$2:" "$SYS/etc/passwd" || exit 1
       g=$(grep "^$2:" "$SYS/etc/groups" 2>/dev/null | cut -d: -f2); echo "${g:-$2}" ;;
  *) exit 1 ;;
esac
''',
    "getent": '''[ "$1" = passwd ] || exit 2
if [ -n "$2" ]; then grep "^$2:" "$SYS/etc/passwd"; else cat "$SYS/etc/passwd"; fi
''',
    "useradd": '''echo "useradd $*" >> "$SYS/calls"
for u; do :; done
uid=$((1001 + $(wc -l < "$SYS/etc/passwd")))
echo "$u:x:$uid:$uid::$SYS/home/$u:/bin/bash" >> "$SYS/etc/passwd"
mkdir -p "$SYS/home/$u"
if [ -n "${FAKE_SSH_FILE:-}" ]; then : > "$SYS/home/$u/.ssh"; fi   # its .ssh cannot be made
''',
    "userdel": '''echo "userdel $*" >> "$SYS/calls"
for u; do :; done
grep -v "^$u:" "$SYS/etc/passwd" > "$SYS/etc/passwd.new"; mv "$SYS/etc/passwd.new" "$SYS/etc/passwd"
rm -rf "$SYS/home/$u"
''',
    "loginctl": '''echo "loginctl $*" >> "$SYS/calls"
case "$1" in
  enable-linger) mkdir -p "$SYS/var/lib/systemd/linger"; touch "$SYS/var/lib/systemd/linger/$2" ;;
  disable-linger) rm -f "$SYS/var/lib/systemd/linger/$2" ;;
esac
''',
    "dpkg-query": '''for p; do :; done
grep -qx "$p" "$SYS/installed" 2>/dev/null && printf 'install ok installed'
''',
    "apt-get": '''echo "apt-get $*" >> "$SYS/calls"
case " $* " in *" install "*) for p; do case $p in -*|install) ;; *) echo "${p##*/}" | sed 's/_current_amd64.deb//' >> "$SYS/installed" ;; esac; done ;; esac
''',
    "runuser": '''echo "runuser $1 $2" >> "$SYS/calls"
home=$(grep "^$2:" "$SYS/etc/passwd" | cut -d: -f6)
uid=$(grep "^$2:" "$SYS/etc/passwd" | cut -d: -f3)
if [ "$1" = -u ]; then   # one command as that user: runuser -u <user> -- <command>
  shift 2; [ "$1" = -- ] && shift
  FAKE_UID=$uid exec "$@"
fi
echo "$4" >> "$SYS/runuser-commands"
f=${4##*bash }; f=${f%% *}; eval "cp $f \"\\$SYS/hub-run.sh\""   # the copy root wrote for the hub's user
FAKE_UID=1001 HOME=$home exec env -u HUB_INSTALL bash -c "$4"   # -l starts a clean environment
''',
    "sudo": '''echo "sudo $*" >> "$SYS/calls"
[ "$1" = -n ] && shift
[ "$1" = true ] && exit "${FAKE_SUDO:-1}"
SUDO_USER=$(awk -F: -v u="${FAKE_UID:-0}" '$3 == u {print $1; exit}' "$SYS/etc/passwd") FAKE_UID=0 exec "$@"
''',
    "fallocate": 'echo "fallocate $*" >> "$SYS/calls"; for f; do :; done; : > "$f"\n',
    "swapon": '[ "$1" = --show ] && { cat "$SYS/swaps" 2>/dev/null; exit 0; }; echo "swapon $*" >> "$SYS/calls"\n',
    "flock": "exit 0\n",
}
for _name in ("usermod", "chgrp", "chown", "mkswap", "swapoff", "systemctl"):
    SYSTEM_FAKES[_name] = f'echo "{_name} $*" >> "$SYS/calls"\n'


@unittest.skipIf(BASH is None or os.name == "nt", "install.sh is for macOS and Linux")
@cut.needs_deliver()
class Server(Stand):
    """--server: a fresh Ubuntu VM as root (a cloud's user data), the system's programs as stand-ins over a system root
    in a temporary folder; the hub's own install then runs for real as the stand's install does, under that user."""

    def setUp(self):
        super().setUp()
        self.sys = os.path.join(self.dir, "sys")
        for d in ("etc/apt/apt.conf.d", "etc/update-motd.d", "run/systemd/system", "proc", "root/.ssh", "home",
                  "var/lib/systemd/linger", "var/log"):
            os.makedirs(os.path.join(self.sys, d))
        self.write("etc/os-release", 'NAME="Ubuntu"\nID=ubuntu\nID_LIKE=debian\n')
        self.memory(4 * 1024 * 1024)
        self.write("installed", "git\ncurl\nca-certificates\nunattended-upgrades\ngoogle-chrome-stable\n")
        self.write("etc/apt/apt.conf.d/20auto-upgrades", "APT::Periodic::Unattended-Upgrade \"1\";\n")
        # a cloud's default user with the person's key, and root's line that sends them there (EC2)
        ubuntu = os.path.join(self.sys, "home", "ubuntu")
        os.makedirs(os.path.join(ubuntu, ".ssh"))
        self.write("etc/passwd", f"root:x:0:0::{self.sys}/root:/bin/bash\nubuntu:x:1000:1000::{ubuntu}:/bin/bash\n")
        self.write("home/ubuntu/.ssh/authorized_keys", "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPersonKey person@laptop\n")
        self.write("root/.ssh/authorized_keys",
                   'no-port-forwarding,no-agent-forwarding,no-X11-forwarding,command="echo \'Please login as the user '
                   '\\"ubuntu\\" rather than the user \\"root\\".\';echo;sleep 10;exit 142" '
                   "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPersonKey person@laptop\n"
                   'from="203.0.113.0/24",command="/usr/local/bin/backup" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBackupKey backup\n'
                   "  ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQRootKey root@desk\n")
        self.write("etc/groups", "ubuntu:ubuntu sudo adm\n")
        self.write("etc/fstab", "LABEL=root / ext4 defaults 0 1\n")
        for name, script in SYSTEM_FAKES.items():
            self.fake(name, script)
        self.fake("uname", 'case "$1" in -s) echo Linux ;; -m) echo x86_64 ;; esac\n')
        self.fake("getconf", 'echo "glibc 2.39"\n')
        self.fake("df", 'echo "Filesystem 1024-blocks Used Available Capacity Mounted"; echo "x 1 1 99999999 1% /"\n')
        if not shutil.which("git"):
            self.fake("git", "exit 0\n")

    def write(self, rel, data):
        with open(os.path.join(self.sys, rel), "w") as f:
            f.write(data)

    def read(self, rel):
        with open(os.path.join(self.sys, rel)) as f:
            return f.read()

    def memory(self, kb):
        self.write("proc/meminfo", f"MemTotal:       {kb} kB\nMemFree:        1000 kB\n")

    def env(self, **extra):
        return super().env(SYS=self.sys, HUB_INSTALL_SYSROOT=self.sys, HUB_INSTALL_METADATA="http://127.0.0.1:9",
                           **extra)

    def calls(self):
        try:
            return self.read("calls").splitlines()
        except FileNotFoundError:
            return []

    def state(self):
        with open(os.path.join(self.sys, "var/lib/agents-hub/server.json")) as f:
            return json.load(f)

    def test_root_makes_the_user_and_the_hub_installs_under_it(self):
        r = self.install("--server", "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.calls()
        self.assertIn("useradd --create-home --user-group --shell /bin/bash --comment Hub by Icons8 hub", calls)
        self.assertIn("usermod -p * hub", calls)
        self.assertIn("loginctl enable-linger hub", calls)
        self.assertIn("runuser -l hub", calls)
        self.assertFalse(any(c.startswith("apt-get") for c in calls), calls)   # all of it was there
        # the hub's own install ran under that user, nobody to ask, no browser to open
        self.assertEqual(self.got()["argv"][-3:], ["--yes", "--no-open", "--no-input"])
        self.assertTrue(self.got()["argv"][self.got()["argv"].index("--env") + 1].startswith(
            os.path.join(self.sys, "home", "hub")))
        # the person's key reaches the hub's user, without the line of root that would refuse the tunnel
        # (a limited key gets no shell there: the backup key stays where it was)
        self.assertEqual(self.read("home/hub/.ssh/authorized_keys"),
                         "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQRootKey root@desk\n"
                         "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPersonKey person@laptop\n")
        # the key of the line and the person's environment go in the copy, never on a command line every account sees
        self.assertNotIn(READ, self.read("runuser-commands"))
        self.assertIn(f"export HUB_DELIVER_KEY={READ}", self.read("hub-run.sh"))
        self.assertNotIn("export HUB_INSTALL=", self.read("hub-run.sh"))   # its flags are words: --yes --no-open
        # the accounts the machine had before the hub's user stay in server.json only: nothing reads them any more
        self.assertNotIn("HUB_INSTALL_ACCOUNTS", self.read("hub-run.sh"))
        st = self.state()
        self.assertEqual(st["accounts"], "root ubuntu ")
        self.assertEqual((st["user"], st["user_created"], st["linger"], st["status"], st["code"]),
                         ("hub", "1", "1", "done", "0"))
        self.assertEqual(st["tunnel"], "ssh -N -L 8787:127.0.0.1:8787 -L 8788:127.0.0.1:8788 hub@<server address>")
        self.assertIn(st["tunnel"], r.stderr)
        self.assertIn("http://localhost:8787", r.stderr)
        # a 4 GB machine without swap gets a swap file; the login message reads the state
        self.assertEqual(st["swap"], "/swapfile")
        self.assertIn("/swapfile none swap sw 0 0  # agents-hub", self.read("etc/fstab"))
        motd = os.path.join(self.sys, "etc/update-motd.d/90-agents-hub")
        self.assertTrue(os.access(motd, os.X_OK))
        self.assertIn("/var/lib/agents-hub/server.json", self.read("etc/update-motd.d/90-agents-hub"))

    def test_the_address_the_person_reached_by_ssh_goes_to_the_hub_s_run(self):
        r = self.install("--server", SSH_CONNECTION="198.51.100.7 51000 203.0.113.5 22")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("export SSH_CONNECTION=", self.read("hub-run.sh"))

    def test_the_login_message_says_the_state_and_the_tunnel(self):
        self.assertEqual(self.install("--server").returncode, 0)
        motd = self.read("etc/update-motd.d/90-agents-hub").replace("/var/lib/agents-hub/server.json",
                                                                      os.path.join(self.sys, "var/lib/agents-hub/server.json"))
        out = subprocess.run(["sh", "-c", motd], capture_output=True, text=True).stdout   # portable: sh
        self.assertIn("runs here under the user hub", out)
        self.assertIn("ssh -N -L 8787:127.0.0.1:8787 -L 8788:127.0.0.1:8788 hub@<server address>", out)
        self.assertIn("open http://localhost:8787 there", out)

    def test_a_run_again_changes_nothing_it_made(self):
        self.assertEqual(self.install("--server").returncode, 0)
        with open(os.path.join(self.sys, "etc/passwd"), "a") as f:   # an account added after the install
            f.write(f"guest:x:1500:1500::{os.path.join(self.sys, 'home', 'guest')}:/bin/bash\n")
        self.write("home/hub/.ssh/authorized_keys", "ssh-ed25519 AAAAOwnKey own\n")
        os.unlink(os.path.join(self.sys, "calls"))
        r = self.install("--server")
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.calls()
        self.assertFalse([c for c in calls if c.split()[0] in ("useradd", "usermod", "loginctl", "fallocate")], calls)
        self.assertEqual(self.read("home/hub/.ssh/authorized_keys"), "ssh-ed25519 AAAAOwnKey own\n")
        self.assertEqual(self.read("etc/fstab").count("/swapfile"), 1)
        self.assertEqual(self.state()["user_created"], "1")
        self.assertNotIn("guest", self.read("hub-run.sh"))   # the same list as the first run: never a later account

    def add_user(self, name, uid, key):
        home = os.path.join(self.sys, "home", name)
        os.makedirs(os.path.join(home, ".ssh"))
        with open(os.path.join(self.sys, "etc/passwd"), "a") as f:
            f.write(f"{name}:x:{uid}:{uid}::{home}:/bin/bash\n")
        self.write(f"home/{name}/.ssh/authorized_keys", key)

    def test_only_the_keys_of_the_one_who_ran_it_reach_the_hub_s_user(self):
        # a shared server: other people's keys get no shell as the hub's user unless named
        guest = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGuestKey guest@desk\n"
        self.add_user("guest", 1500, guest)
        # a cloud's root line naming a user who does not have that key unlimited: nothing from it
        with open(os.path.join(self.sys, "root/.ssh/authorized_keys"), "a") as f:
            f.write('no-port-forwarding,command="echo \'Please login as the user \\"guest\\" rather than the user '
                    '\\"root\\".\';echo;sleep 10;exit 142" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOtherKey other\n')
        r = self.install("--server")
        self.assertEqual(r.returncode, 0, r.stderr)
        keys = self.read("home/hub/.ssh/authorized_keys")
        self.assertEqual(keys, "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQRootKey root@desk\n"
                               "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPersonKey person@laptop\n")
        self.assertIn("runuser -u hub", self.calls())   # written as the hub's user, not by root

    def test_through_sudo_the_keys_are_of_the_user_who_ran_it(self):
        self.add_user("guest", 1500, "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGuestKey guest@desk\n")
        r = self.install("--server", FAKE_UID="1000", FAKE_SUDO="0")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read("home/hub/.ssh/authorized_keys"),
                         "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPersonKey person@laptop\n")
        self.assertIn("the SSH keys of ubuntu now reach hub too: 1", r.stderr)

    def test_keys_from_names_another_user_whose_keys_go_too(self):
        self.add_user("guest", 1500, "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGuestKey guest@desk\n")
        r = self.install("--server", "--keys-from", "guest", FAKE_UID="1000", FAKE_SUDO="0")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read("home/hub/.ssh/authorized_keys"),
                         "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPersonKey person@laptop\n"
                         "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGuestKey guest@desk\n")
        self.assertIn("runuser -u guest", self.calls())   # another user's file is read as that user
        self.assertIn("the SSH keys of ubuntu, guest now reach hub too: 2", r.stderr)
        # HUB_INSTALL's --keys-from and the command line's together
        self.add_user("tester", 1501, "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITesterKey tester@desk\n")
        os.unlink(os.path.join(self.sys, "home/hub/.ssh/authorized_keys"))
        r = self.install("--server", "--keys-from=tester", FAKE_UID="1000", FAKE_SUDO="0",
                         HUB_INSTALL="--keys-from guest")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("the SSH keys of ubuntu, guest, tester now reach hub too: 3", r.stderr)
        for flags, said in ((["--keys-from", "nobody"], "no user nobody here"),
                            (["--keys-from", "hub"], "the keys of another user"),
                            (["--keys-from", "-x"], "--keys-from takes a user name"),
                            (["--keys-from", "1000"], "--keys-from takes a user name"),   # a number is no name
                            (["--keys-from", "*"], "--keys-from takes a user name")):     # never a file name
            r = self.install("--server", *flags)
            self.assertEqual(r.returncode, 1 if flags[1] in ("nobody", "hub") else 64, (flags, r.stderr))
            self.assertIn(said, r.stderr, flags)
        self.assertEqual(self.install("--keys-from", "guest").returncode, 64)   # goes with --server only

    def test_keys_from_on_a_run_again_adds_only_what_is_missing(self):
        # the same key at two users and a name given twice: each key and each name once
        self.add_user("guest", 1500, "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPersonKey guest-copy\n")
        r = self.install("--server", "--keys-from", "ubuntu", "--keys-from", "guest", FAKE_UID="1000", FAKE_SUDO="0")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("the SSH keys of ubuntu now reach hub too: 1", r.stderr)
        self.write("home/hub/.ssh/authorized_keys", "ssh-ed25519 AAAAOwnKey own")   # no newline at its end
        self.add_user("tester", 1501, "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITesterKey tester@desk\n")
        r = self.install("--server", "--keys-from", "tester", "--keys-from", "guest")   # a file is there: added to
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read("home/hub/.ssh/authorized_keys"),
                         "ssh-ed25519 AAAAOwnKey own\n"
                         "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITesterKey tester@desk\n"
                         "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPersonKey guest-copy\n")
        r = self.install("--server", "--keys-from", "tester")   # all of them there: nothing new, nothing said
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("now reach", r.stderr)
        self.assertEqual(self.read("home/hub/.ssh/authorized_keys").count("TesterKey"), 1)

    def test_a_link_in_the_hub_s_ssh_folder_is_never_followed(self):
        # a run again: the hub's sessions own its home and may have put links there, root must not write through them
        self.assertEqual(self.install("--server").returncode, 0)
        victim = os.path.join(self.sys, "etc", "victim")
        os.makedirs(victim)
        os.chmod(victim, 0o755)
        ssh = os.path.join(self.sys, "home", "hub", ".ssh")
        shutil.rmtree(ssh)   # portable: the class runs on macOS and Linux only (install.sh)
        os.symlink(victim, ssh)
        r = self.install("--server")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.listdir(victim), [])
        self.assertEqual(os.stat(victim).st_mode & 0o777, 0o755)
        self.assertIn("is a link: no SSH key was put there", r.stderr)   # in the last words of the run
        os.unlink(ssh)
        os.makedirs(ssh)
        os.symlink(os.path.join(victim, "keys"), os.path.join(ssh, "authorized_keys"))   # a link to nothing yet
        r = self.install("--server")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.listdir(victim), [])
        self.add_user("guest", 1500, "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGuestKey guest@desk\n")
        r = self.install("--server", "--keys-from", "guest")   # nor when it adds to the file
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.listdir(victim), [])
        self.assertIn("is a link", r.stderr)

    def test_somebody_else_s_user_of_that_name_is_never_taken(self):
        with open(os.path.join(self.sys, "etc/passwd"), "a") as f:
            f.write(f"hub:x:1005:1005::{os.path.join(self.sys, 'home', 'hub')}:/bin/bash\n")
        r = self.install("--server")
        self.assertEqual(r.returncode, 1)
        self.assertIn("a user hub is here already, not made by --server", r.stderr)
        self.assertEqual(self.calls(), [])
        for left in ("var/lib/agents-hub", "var/log/agents-hub-server.log"):
            self.assertFalse(os.path.exists(os.path.join(self.sys, left)), left)
        self.assertFalse(os.path.exists(self.out))
        r = self.install("--server", "--user", "agents")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("runuser -l agents", self.calls())

    def test_a_user_with_a_group_that_is_root_in_disguise_does_not_get_the_hub(self):
        self.assertEqual(self.install("--server").returncode, 0)
        with open(os.path.join(self.sys, "etc/groups"), "a") as f:
            f.write("hub:hub docker\n")
        os.unlink(self.out)
        r = self.install("--server")
        self.assertEqual(r.returncode, 1)
        self.assertIn("in the groups hub docker", r.stderr)
        self.assertFalse(os.path.exists(self.out))

    def test_refusals_before_anything_is_changed(self):
        self.memory(2 * 1024 * 1024)
        r = self.install("--server")
        self.assertEqual(r.returncode, 1)
        self.assertIn("at least 3.5 GB of memory, this server has 2.0 GB", r.stderr)
        self.memory(8 * 1024 * 1024)
        self.write("etc/os-release", "ID=alpine\n")
        self.assertIn("sets up Ubuntu or Debian", self.install("--server").stderr)
        self.write("etc/os-release", "ID=debian\n")
        os.rmdir(os.path.join(self.sys, "run/systemd/system"))
        self.assertIn("needs systemd", self.install("--server").stderr)
        self.assertEqual(self.calls(), [])
        for left in ("var/lib/agents-hub", "var/log/agents-hub-server.log"):   # a refusal leaves nothing behind
            self.assertFalse(os.path.exists(os.path.join(self.sys, left)), left)
        for flags in (["--user", "x"], ["--remove"], ["--server", "--instance", "work"], ["--server", "--move"],
                      ["--server", "--user", "Root"]):
            self.assertEqual(self.install(*flags).returncode, 64, flags)

    def test_a_user_with_sudo_hands_it_to_root_and_one_without_is_refused(self):
        r = self.install("--server", FAKE_UID="1000")
        self.assertEqual(r.returncode, 1)
        self.assertIn("run it as root, or as a user with sudo without a password", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.sys, "var/lib/agents-hub/server.json")))
        r = self.install("--server", FAKE_UID="1000", FAKE_SUDO="0", HUB_INSTALL="--no-open")
        self.assertEqual(r.returncode, 0, r.stderr)
        sudo = [c for c in self.calls() if c.startswith("sudo -n bash ")]
        self.assertTrue(sudo, self.calls())
        self.assertNotIn(READ, sudo[0])
        self.assertEqual(self.state()["status"], "done")

    def test_chrome_comes_from_google_and_stays_up_to_date(self):
        self.write("installed", "git\ncurl\nca-certificates\nunattended-upgrades\n")
        os.unlink(os.path.join(self.sys, "etc/apt/apt.conf.d/20auto-upgrades"))   # Debian's: turned off
        real = shutil.which("curl")
        self.fake("curl", f'''case "$*" in *dl.google.com*)
  echo "chrome $*" >> "$SYS/calls"; while [ $# -gt 0 ]; do [ "$1" = -o ] && : > "$2"; shift; done; exit 0 ;; esac
exec "{real}" "$@"
''')
        self.memory(16 * 1024 * 1024)
        r = self.install("--server")
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.calls()
        self.assertTrue([c for c in calls if c.startswith("chrome ")], calls)
        chrome = [i for i, c in enumerate(calls) if c.startswith("apt-get") and "google-chrome-stable_current_amd64.deb" in c]
        update = [i for i, c in enumerate(calls) if c.startswith("apt-get") and c.endswith(" update")]
        self.assertTrue(chrome and update and update[0] < chrome[0], calls)   # a cloud image's index is old: 404s
        self.assertIn('"origin=Google LLC,codename=stable"', self.read("etc/apt/apt.conf.d/51agents-hub-chrome"))
        for conf in ("51agents-hub-chrome", "20auto-upgrades"):   # apt reads its configuration as every user
            self.assertEqual(os.stat(os.path.join(self.sys, "etc/apt/apt.conf.d", conf)).st_mode & 0o777, 0o644)
        self.assertFalse(os.path.exists(os.path.join(self.sys, "swapfile")))   # 16 GB: no swap file
        self.assertEqual(self.state()["swap"], "")

    def test_a_step_failing_after_it_began_is_said_as_a_failure_not_as_installing(self):
        r = self.install("--server", FAKE_SSH_FILE="1")
        self.assertEqual(r.returncode, 1)
        self.assertEqual((self.state()["status"], self.state()["code"]), ("failed", "1"))
        self.assertFalse(os.path.exists(self.out))

    def test_the_hub_s_install_failing_is_said_and_kept(self):
        r = self.install("--server", STUB_EXIT="7")
        self.assertEqual(r.returncode, 7)
        self.assertEqual((self.state()["status"], self.state()["code"]), ("failed", "7"))
        self.assertIn("ended with code 7", r.stderr)

    def test_remove_takes_off_what_it_put_on_and_nothing_else(self):
        self.write("swaps", "")
        self.assertEqual(self.install("--server").returncode, 0)
        with open(os.path.join(self.sys, "etc/fstab"), "a") as f:
            f.write("/data none ext4 defaults 0 2\n")
        r = self.install("--server", "--remove")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("userdel -r hub", self.calls())
        self.assertIn("loginctl disable-linger hub", self.calls())
        self.assertFalse(os.path.exists(os.path.join(self.sys, "home/hub")))
        self.assertFalse(os.path.exists(os.path.join(self.sys, "swapfile")))
        self.assertEqual(self.read("etc/fstab"), "LABEL=root / ext4 defaults 0 1\n/data none ext4 defaults 0 2\n")
        for gone in ("etc/update-motd.d/90-agents-hub", "var/lib/agents-hub", "var/log/agents-hub-server.log"):
            self.assertFalse(os.path.exists(os.path.join(self.sys, gone)), gone)
        self.assertTrue(os.path.exists(os.path.join(self.sys, "etc/apt/apt.conf.d/20auto-upgrades")))   # not its own
        self.assertIn("home/ubuntu", self.read("etc/passwd"))
        r = self.install("--server", "--remove")
        self.assertEqual(r.returncode, 1)
        self.assertIn("put nothing on this machine", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.sys, "var/lib/agents-hub")))   # and leaves nothing either

    def test_a_swap_there_from_before_is_left_alone(self):
        self.write("swaps", "/dev/sda2 partition 2G 0B -2\n")
        self.assertEqual(self.install("--server").returncode, 0)
        self.assertFalse([c for c in self.calls() if c.startswith("fallocate")])
        self.assertEqual(self.state()["swap"], "")


class ServerText(unittest.TestCase):
    def test_every_setting_of_the_script_goes_to_its_copies(self):
        # the run under the hub's user and through sudo is the script as self_text prints it: a setting left out of
        # HUB_INSTALL_CONSTANTS would be empty there
        data = re.sub(r"<<'(\w+)'\n.*?\n\1\n", "", text(SH).decode(), flags=re.S)   # not the texts it writes out
        top = set(re.findall(r"^([A-Za-z_][A-Za-z0-9_]*)=", data, re.M))
        listed = set(re.search(r"^HUB_INSTALL_CONSTANTS='([^']*)'", data, re.M).group(1).split())
        self.assertEqual(top, listed)

    def test_the_tunnel_names_the_address_the_person_reached_only_when_it_is_public(self):
        from core import remote
        env = {"SSH_CONNECTION": "198.51.100.7 51000 203.0.113.5 22"}
        self.assertEqual(remote.tunnel(8787, 8788, "hub", env, is_public=lambda a: a == "203.0.113.5"),
                         "ssh -N -L 8787:127.0.0.1:8787 -L 8788:127.0.0.1:8788 hub@203.0.113.5")
        self.assertTrue(remote.tunnel(8790, 8791, "maps", env).endswith("maps@<server address>"))   # a documentation net
        self.assertIn("-L 8790:127.0.0.1:8790 -L 8791:127.0.0.1:8791", remote.tunnel(8790, 8791, "maps", {}))
        self.assertTrue(remote.tunnel(8787, 8788, "hub", {"SSH_CONNECTION": "x"}).endswith("@<server address>"))


if __name__ == "__main__":
    unittest.main()
