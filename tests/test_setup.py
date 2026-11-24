"""«Setup» (installer spec §6.1, §9, §10): the checklist of a new hub, its steps on fakes of claude and gh (no real
sign-in, no network), the hub's own GitHub sign-in for the core's git, Chrome for Testing on first use, the file for
support and `hub uninstall`."""
import tests  # noqa: F401 — keep the machine's settings out (~/.config/hub-core), see tests/__init__.py
import hashlib
import http.server
import io
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from unittest import mock

import platforms
from core import codestore, ghauth, redact, runtime, supportbundle, uninstall
from modules.browser import browsers
from modules.setup import auto, chrome, job, setup
from modules.testchrome import chrome as testchrome
from tests import winsafe
from tests.fakes import cut, exe

POSIX = sys.platform != "win32"

FAKE_CLAUDE = r'''
import json, os, subprocess, sys
home = os.environ["FAKE_HOME"]
mark = os.path.join(home, "logged")
args = sys.argv[1:]
if args == ["--version"]:
    print("2.1.309 (Claude Code)")
    sys.exit(0)
if args[:2] == ["auth", "status"]:
    if os.path.exists(os.path.join(home, "old")):
        print("error: unknown command 'auth'")
        sys.exit(1)
    ok = os.path.exists(mark)
    print(json.dumps({"loggedIn": ok, "authMethod": "claude.ai" if ok else "none",
                      **({"email": "someone@example.com"} if ok else {}), "configDirectory": "/x/.claude"}))
    sys.exit(0 if ok else 1)
if args[:2] == ["auth", "login"]:
    assert "--claudeai" in args, args
    with open(os.path.join(home, "login-env"), "w") as f:
        f.write(os.environ.get("BROWSER", ""))
    print("Opening browser to sign in…", flush=True)
    print("If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?code=true&client_id=c"
          "&response_type=code&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&state=s1",
          flush=True)
    url = ("https://claude.com/cai/oauth/authorize?code=true&client_id=c"
           "&response_type=code&redirect_uri=http%3A%2F%2Flocalhost%3A54545%2Fcallback&state=s1")
    # Claude Code passes the address in quotes (installer probe S0-1): unquoted, cmd cuts a .cmd's line at the first &
    subprocess.run(f'"{os.environ["BROWSER"]}" "{url}"' if os.name == "nt" else [os.environ["BROWSER"], url])
    sys.stdout.write("Paste code here if prompted > ")
    sys.stdout.flush()
    line = sys.stdin.readline().strip()
    if line == "good-code#s1":
        open(mark, "w").close()
        print("Login successful.")
        sys.exit(0)
    print("Login failed: Request failed with status code 400")
    sys.exit(1)
sys.exit(3)
'''

FAKE_GH = r'''
import os, sys, time
args = sys.argv[1:]
cfg = os.environ.get("GH_CONFIG_DIR", "")
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(" ".join(args) + "|" + cfg + "|" + os.environ.get("GIT_CONFIG_GLOBAL", "") + "\n")
if args[:2] == ["auth", "login"]:
    print("! First copy your one-time code: ABCD-1234", flush=True)
    print("Open this URL to continue in your web browser: https://github.com/login/device", flush=True)
    end = time.time() + 30
    while not os.path.exists(os.path.join(cfg, "go")) and time.time() < end:
        time.sleep(0.05)
    if os.path.exists(os.path.join(cfg, "no")):
        print("error: authorization denied")
        sys.exit(1)
    with open(os.path.join(cfg, "hosts.yml"), "w") as f:
        f.write("github.com:\n    users:\n        octo:\n    git_protocol: https\n    user: octo\n"
                "    oauth_token: test-token-value\n")
    print("Logged in as octo")
    sys.exit(0)
if args[:2] == ["auth", "git-credential"]:
    sys.stdin.read()
    print("protocol=https\nhost=github.com\nusername=octo\npassword=from-hub-gh\n")
    sys.exit(0)
if args[:1] == ["--version"]:
    print("gh version 2.0.0")
    sys.exit(0)
sys.exit(3)
'''


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def until(fn, wait=20.0, step=0.05):
    end = time.time() + wait
    while time.time() < end:
        got = fn()
        if got:
            return got
        time.sleep(step)
    return fn()


def signed(state, login="octo"):
    d = ghauth.config_dir(state)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "hosts.yml"), "w") as f:
        f.write(f"github.com:\n    git_protocol: https\n    user: {login}\n    oauth_token: test-token-value\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hubsetup-")
        self.addCleanup(platforms.current.rmtree, self.tmp)
        self.state = os.path.join(self.tmp, "state")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.state)
        os.makedirs(self.home)
        self.claude = exe.script(os.path.join(self.tmp, "claude"), FAKE_CLAUDE)
        self.gh = exe.script(os.path.join(self.tmp, "gh"), FAKE_GH)
        self.log = os.path.join(self.tmp, "gh.log")
        env = mock.patch.dict(os.environ, {"FAKE_HOME": self.home, "FAKE_LOG": self.log, "HUB_STATE_DIR": self.state,
                                           "HUB_GH": self.gh})
        env.start()
        self.addCleanup(env.stop)
        # what this machine's AppArmor says about the Chrome sandbox is not the test's business (tests.test_deb_v2 sets it)
        sandbox = mock.patch("modules.testchrome.chrome.USERNS_SWITCH", os.path.join(self.tmp, "no-such-switch"))
        sandbox.start()
        self.addCleanup(sandbox.stop)

    def thread(self, fn, *args, **kw):
        t = threading.Thread(target=fn, args=args, kwargs=kw, daemon=True)
        t.start()
        self.addCleanup(t.join, 30)
        return t

    def status(self, step):
        try:
            with open(job.status_path(self.state, step)) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}


class GitThroughTheHubsSignIn(Base):
    def test_no_sign_in_no_change_to_git(self):
        self.assertEqual(ghauth.git_env(self.state), {})
        self.assertNotIn("GIT_CONFIG_PARAMETERS", codestore.quiet_env())

    def test_the_hub_sign_in_is_a_credential_helper_after_the_callers_own(self):
        signed(self.state)
        got = ghauth.git_env(self.state, base={"GIT_CONFIG_PARAMETERS": "'core.longpaths=true'"})
        self.assertTrue(got["GIT_CONFIG_PARAMETERS"].startswith("'core.longpaths=true' 'credential.https://github.com.helper='"))
        self.assertIn("auth git-credential", got["GIT_CONFIG_PARAMETERS"])
        self.assertNotIn("GIT_CONFIG_COUNT", got)   # no empty environment variable (Windows may drop it)
        self.assertFalse([v for v in got.values() if v == ""])
        self.assertEqual(got["GH_CONFIG_DIR"], ghauth.config_dir(self.state))
        self.assertEqual(ghauth.login(self.state), "octo")

    def test_the_core_git_gets_the_password_from_the_hubs_gh_and_the_users_gitconfig_is_untouched(self):
        # real git, no network: `git credential fill` asks the helpers exactly as a fetch would
        # the person's own store holds a login of another account: it neither answers instead of the hub's sign-in nor
        # receives the hub's token after the approval a successful fetch makes
        signed(self.state)
        store = os.path.join(self.home, "store")
        with open(store, "w", newline="\n") as f:   # as git writes it: credential-store reads «github.com\r» as another host
            f.write("https://someone:their-own-password@github.com\n")
        with open(os.path.join(self.home, ".gitconfig"), "w") as f:
            # forward slashes: git reads a backslash in a config value as an escape (a Windows path — "bad config line")
            f.write(f"[user]\n\tname = someone\n[credential]\n\thelper = store --file={store.replace(os.sep, '/')}\n")
        before = read(os.path.join(self.home, ".gitconfig"))
        env = dict(codestore.quiet_env(), HOME=self.home, USERPROFILE=self.home, GIT_CONFIG_NOSYSTEM="1",
                   GIT_CONFIG_GLOBAL=os.path.join(self.home, ".gitconfig"))
        r = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n", text=True,
                           capture_output=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("password=from-hub-gh", r.stdout)
        r = subprocess.run(["git", "credential", "approve"], input=r.stdout + "\n", text=True, capture_output=True,
                           env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(read(store), "https://someone:their-own-password@github.com\n")
        self.assertEqual(read(os.path.join(self.home, ".gitconfig")), before)
        env.pop("GIT_CONFIG_PARAMETERS")   # without the hub's sign-in: the person's own store answers as before
        r = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n", text=True,
                           capture_output=True, env=env, timeout=30)
        self.assertIn("password=their-own-password", r.stdout)
        # another host: the helper is only for github.com
        r = subprocess.run(["git", "config", "--get-urlmatch", "credential.helper", "https://gitlab.example.test/x"],
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertNotIn("git-credential", r.stdout)

    def test_a_gh_path_with_a_space_and_a_quote_reaches_git_whole(self):
        odd = os.path.join(self.tmp, "it's here")
        os.makedirs(odd)
        gh = exe.script(os.path.join(odd, "gh"), FAKE_GH)
        signed(self.state)
        env = dict(os.environ, HOME=self.home, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   **ghauth.git_env(self.state, base={}, gh=gh))
        r = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n", text=True,
                           capture_output=True, env=env, timeout=30)
        self.assertIn("password=from-hub-gh", r.stdout, r.stderr)

    def test_a_windows_path_of_gh_goes_to_sh_with_forward_slashes(self):
        self.assertEqual(ghauth.helper_value(r"C:\Program Files\GitHub CLI\gh.exe"),
                         "!'C:/Program Files/GitHub CLI/gh.exe' auth git-credential")
        self.assertEqual(ghauth.helper_value("/opt/it's/gh"), "!'/opt/it'\\''s/gh' auth git-credential")


class ClaudeLogin(Base):
    def run_login(self, life=20):
        j = job.Job(self.state, "login")
        return self.thread(job.step_login, j, claude=[self.claude], life=life, sleep=lambda s: time.sleep(0.02),
                           check=lambda j, sleep: None), j   # no hub check process from a test

    def test_both_links_then_the_pasted_code_signs_in(self):
        t, _ = self.run_login()
        st = until(lambda: (lambda s: s if s.get("url") and s.get("local_url") else None)(self.status("login")))
        self.assertIn("redirect_uri=https%3A%2F%2Fplatform.claude.com", st["url"])
        self.assertIn("redirect_uri=http%3A%2F%2Flocalhost", st["local_url"])
        self.assertEqual(st["state"], "running")
        s = setup.Setup(self.state, lambda: {}, claude=[self.claude])
        self.assertEqual(s.give("login", "good-code#s1")[0], 202)
        t.join(20)
        st = self.status("login")
        self.assertEqual((st["state"], st["account"]["email"]), ("done", "someone@example.com"))
        self.assertNotIn("good-code", read(os.path.join(self.state, "logs", "setup.log")))
        self.assertFalse(os.path.exists(os.path.join(job.folder(self.state), "login.in")))
        item = s.status(full=False)["items"][1]
        self.assertEqual((item["state"], item["via"], item["account"]["email"]), ("ok", "own", "someone@example.com"))

    def test_a_sign_in_that_went_through_starts_the_hub_check_before_it_says_done(self):
        # a check made when the first sign-in timed out said "not signed in": once the code goes in, the page reads
        # that check as running, never as the old red (owner 2026-11-19: the band stayed until Retry and a reload)
        seen = []

        def check(j, sleep):
            seen.append(self.status("login")["state"])
        j = job.Job(self.state, "login")
        t = self.thread(job.step_login, j, claude=[self.claude], life=20, sleep=lambda s: time.sleep(0.02), check=check)
        until(lambda: self.status("login").get("url"))
        setup.Setup(self.state, lambda: {}, claude=[self.claude]).give("login", "good-code#s1")
        t.join(20)
        self.assertEqual((seen, self.status("login")["state"]), (["running"], "done"))
        t = self.thread(job.step_login, job.Job(self.state, "login"), claude=[self.claude], life=20,
                        sleep=lambda s: time.sleep(0.02), check=check)
        until(lambda: self.status("login").get("url"))
        setup.Setup(self.state, lambda: {}, claude=[self.claude]).give("login", "nonsense")
        t.join(20)
        self.assertEqual(seen, ["running"])   # a wrong code: no check

    def test_the_check_after_a_sign_in_is_waited_for_until_it_runs(self):
        j = job.Job(self.state, "login")
        writes = []

        class Child:
            def poll(self):
                return None

        def spawn(state, step):
            writes.append(step)
            return Child()

        def sleep(s):   # the child writes its status a moment later, as a started step does
            if len(writes) == 1:
                job.Job(self.state, "doctor")
                writes.append("written")
        job.recheck(j, sleep=sleep, spawn=spawn)
        self.assertEqual(writes, ["doctor", "written"])
        self.assertEqual(self.status("doctor")["state"], "running")

    def test_a_wrong_code_ends_the_attempt_with_its_reason(self):
        t, _ = self.run_login()
        until(lambda: self.status("login").get("url"))
        setup.Setup(self.state, lambda: {}, claude=[self.claude]).give("login", "nonsense")
        t.join(20)
        self.assertEqual((self.status("login")["state"], self.status("login")["why"]), ("failed", "code"))

    def test_cancel_stops_the_program(self):
        t, _ = self.run_login()
        until(lambda: self.status("login").get("url"))
        setup.Setup(self.state, lambda: {}, claude=[self.claude]).cancel("login")
        t.join(20)
        self.assertEqual(self.status("login")["state"], "cancelled")

    def test_a_code_only_while_a_sign_in_runs(self):
        s = setup.Setup(self.state, lambda: {}, claude=[self.claude])
        self.assertEqual(s.give("login", "x")[0], 409)
        self.assertEqual(s.give("login", "two\nlines")[0], 400)
        self.assertEqual(s.give("claude", "x")[0], 400)

    def test_the_browser_helper_only_writes_the_address(self):
        j = job.Job(self.state, "login")
        prog, out = job.browser_helper(j)
        url = "https://claude.com/cai/oauth/authorize?a=1&redirect_uri=http%3A%2F%2Flocalhost%3A1%2Fcallback&state=z"
        # as Claude Code calls BROWSER: the address in quotes (installer probe S0-1) — Python quotes only spaces, and cmd
        # would cut the line at the first &
        subprocess.run(f'"{prog}" "{url}"' if os.name == "nt" else [prog, url], timeout=20, check=True)
        self.assertEqual(job._link_from(out), url)


class GitHubByCode(Base):
    def setUp(self):
        super().setUp()
        pin = os.path.join(self.tmp, "private.json")   # an active pin of its own: the tree's may retire the modules
        with open(pin, "w", encoding="utf-8") as f:
            json.dump({"repo": "acme/team-modules", "url": "https://example.invalid/pm.git", "ref": "pm-v1.0.0",
                       "commit": "a" * 40, "modules": {"accounts": {"subdir": "accounts", "cap": "profile"}}}, f)
        env = mock.patch.dict(os.environ, {"HUB_TEST_PRIVATE_PIN": pin})
        env.start()
        self.addCleanup(env.stop)

    @cut.needs_pin()
    def test_device_code_then_the_invitation_then_modules(self):
        calls, ensured = [], []

        def access(url, ref):
            calls.append((url, ref, ghauth.git_env(self.state).get("GIT_CONFIG_PARAMETERS", "")))
            return ("ok", "c" * 40, "") if len(calls) >= 3 else ("no-access", None, "remote: Repository not found.")
        j = job.Job(self.state, "github")
        with mock.patch.object(job, "INVITE_EVERY", 0.05):
            t = self.thread(job.step_github, j, gh=self.gh, access=access, ensure=lambda: ensured.append(1) or
                            {"state": "done"}, sleep=lambda s: time.sleep(0.01))
            st = until(lambda: (lambda s: s if s.get("code") else None)(self.status("github")))
            self.assertEqual((st["code"], st["verify"]), ("ABCD-1234", "https://github.com/login/device"))
            open(os.path.join(ghauth.config_dir(self.state), "go"), "w").close()
            t.join(20)
        st = self.status("github")
        self.assertEqual((st["state"], st["access"], st["login"], st["modules"]), ("done", "ok", "octo", "done"))
        self.assertEqual(ensured, [1])
        self.assertIn("auth git-credential", calls[0][2])   # the check went through the hub's gh
        # gh ran with the hub's own folders: never the person's gh, never their ~/.gitconfig
        line = read(self.log).splitlines()[0]
        self.assertEqual(line.split("|")[1:], [ghauth.config_dir(self.state),
                                               os.path.join(ghauth.config_dir(self.state), "gitconfig")])
        self.assertIn("--insecure-storage", line)
        self.assertEqual(stat.S_IMODE(os.stat(ghauth.config_dir(self.state)).st_mode) & 0o077, 0) if POSIX else None

    @cut.needs_pin()
    def test_while_not_invited_the_page_gets_the_invitation_link(self):
        signed(self.state)
        j = job.Job(self.state, "access")
        with mock.patch.object(job, "INVITE_EVERY", 0.05):
            t = self.thread(job.step_access, j, access=lambda u, r: ("no-access", None, "remote: Repository not found."),
                            sleep=lambda s: time.sleep(0.01))
            st = until(lambda: (lambda s: s if s.get("invite") else None)(self.status("access")))
            self.assertTrue(st["invite"].startswith("https://github.com/") and st["invite"].endswith("/invitations"))
            self.assertEqual((st["access"], st["kind"]), ("no-access", "missing"))
            open(os.path.join(job.folder(self.state), "access.cancel"), "w").close()
            t.join(20)
        self.assertEqual(self.status("access")["state"], "cancelled")

    def test_a_refused_sign_in_is_said(self):
        os.makedirs(ghauth.config_dir(self.state))
        for f in ("go", "no"):
            open(os.path.join(ghauth.config_dir(self.state), f), "w").close()
        j = job.Job(self.state, "github")
        job.step_github(j, gh=self.gh, access=lambda u, r: self.fail("no access check"), sleep=lambda s: None)
        self.assertEqual((self.status("github")["state"], self.status("github")["why"]), ("failed", "github"))
        self.assertFalse(ghauth.signed_in(self.state))

    def test_no_gh_program(self):
        j = job.Job(self.state, "github")
        with mock.patch.object(ghauth, "gh_program", return_value=None):
            job.step_github(j)
        self.assertEqual(self.status("github")["why"], "no-gh")


class ChromeOnFirstUse(Base):
    def zip_with(self, entries):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name, data, mode in entries:
                info = zipfile.ZipInfo(name)
                info.external_attr = mode << 16
                z.writestr(info, data)
        return buf.getvalue()

    def serve(self, body):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}/chrome.zip"

    def test_unpack_refuses_what_lands_outside(self):
        for bad in (("../x", b"1", 0o100644), ("/abs", b"1", 0o100644), ("a/../../x", b"1", 0o100644)):
            p = os.path.join(self.tmp, "bad.zip")
            with open(p, "wb") as f:
                f.write(self.zip_with([bad]))
            root = tempfile.mkdtemp(dir=self.tmp)
            with self.assertRaises(chrome.Refused):
                chrome.unpack(p, root)
        if POSIX:
            p = os.path.join(self.tmp, "link.zip")
            with open(p, "wb") as f:
                f.write(self.zip_with([("top/l", b"../../outside", 0o120777)]))
            with self.assertRaises(chrome.Refused):
                chrome.unpack(p, tempfile.mkdtemp(dir=self.tmp))
            # a chain: `a/up -> .` is inside, then a file written through a link the archive made earlier
            outside = tempfile.mkdtemp(dir=self.tmp)
            root = os.path.join(outside, "root")
            os.makedirs(root)
            winsafe.symlink(self, outside, os.path.join(root, "out"))   # as if an earlier entry had made it
            p = os.path.join(self.tmp, "chain.zip")
            with open(p, "wb") as f:
                f.write(self.zip_with([("out/x", b"1", 0o100644)]))
            with self.assertRaises(chrome.Refused):
                chrome.unpack(p, root)
            self.assertFalse(os.path.exists(os.path.join(outside, "x")))
            # links downward stay (the frameworks of the macOS build)
            p = os.path.join(self.tmp, "fw.zip")
            with open(p, "wb") as f:
                f.write(self.zip_with([("t/Versions/A/f", b"1", 0o100644), ("t/Versions/Current", b"A", 0o120777)]))
            r = tempfile.mkdtemp(dir=self.tmp)
            chrome.unpack(p, r)
            self.assertEqual(read(os.path.join(r, "t", "Versions", "Current", "f")), "1")

    def test_pinned_build_is_fetched_checked_unpacked_and_found_by_the_browser(self):
        prog = "#!/bin/sh\necho 'Chromium 155.0.0'\n" if POSIX else "@echo Chromium 155.0.0\r\n"
        name = "chrome-test/chrome" if POSIX else "chrome-test/chrome.exe"
        body = self.zip_with([("chrome-test/", b"", 0o40755), (name, prog.encode(), 0o100755),
                              ("chrome-test/lib/x.so", b"lib", 0o100644)])
        pin = {"version": "155.0.1", "url": self.serve(body), "sha256": hashlib.sha256(body).hexdigest(),
               "size": len(body)}
        j = job.Job(self.state, "chrome")
        run = subprocess.run if POSIX else (lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "155", ""))
        job.step_chrome(j, pin=pin, run=run)
        st = self.status("chrome")
        self.assertEqual(st["state"], "done", st)
        self.assertEqual(runtime.chrome(self.state), st["path"])
        self.assertTrue(st["path"].startswith(os.path.join(self.state, "runtime", "chrome", "155.0.1")))
        from modules.browser import browsers
        with mock.patch.object(browsers, "CHROME_CANDIDATES", []), mock.patch.object(browsers, "CHROME_NAMES", []), \
                mock.patch.dict(os.environ, {"HUB_CHROME": ""}):
            self.assertEqual(browsers.find_chrome({}), st["path"])
        self.assertFalse(os.listdir(os.path.join(self.state, "downloads")))

    def test_a_build_that_did_not_start_is_tried_again_without_a_second_download(self):
        prog = "#!/bin/sh\necho 155\n" if POSIX else "@echo 155\r\n"
        body = self.zip_with([("chrome-test/" + ("chrome" if POSIX else "chrome.exe"), prog.encode(), 0o100755)])
        pin = {"version": "155.0.2", "url": self.serve(body), "sha256": hashlib.sha256(body).hexdigest(), "size": len(body)}

        def no_libs(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 60)
        job.step_chrome(job.Job(self.state, "chrome"), pin=pin, run=no_libs)
        self.assertEqual((self.status("chrome")["state"], self.status("chrome")["why"]), ("failed", "start"))
        self.assertIsNone(runtime.chrome(self.state))
        pin["url"] = "http://127.0.0.1:9/gone"   # a second download would fail
        job.step_chrome(job.Job(self.state, "chrome"), pin=pin,
                        run=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "155", ""))
        self.assertEqual(self.status("chrome")["state"], "done", self.status("chrome"))
        self.assertTrue(runtime.chrome(self.state))

    def test_a_build_that_differs_from_the_pin_is_not_unpacked(self):
        body = self.zip_with([("chrome-test/chrome", b"x", 0o100755)])
        pin = {"version": "1", "url": self.serve(body), "sha256": "0" * 64, "size": len(body)}
        j = job.Job(self.state, "chrome")
        job.step_chrome(j, pin=pin)
        self.assertEqual((self.status("chrome")["state"], self.status("chrome")["why"]), ("failed", "checksum"))
        self.assertIsNone(runtime.chrome(self.state))

    def test_the_pin_needs_a_size_and_a_platform_key(self):
        p = os.path.join(self.tmp, "v.json")
        with open(p, "w") as f:
            json.dump({"chrome": {"version": "155.0.8059.39 (CfT)", "files": {
                "linux-x64": {"url": "https://x.example.test/c.zip", "sha256": "a" * 64, "size": 10},
                "linux-arm64": {"url": "https://x.example.test/c.zip", "sha256": "a" * 64}}}}, f)
        self.assertEqual(chrome.pinned(p, "linux-x64")["version"], "155.0.8059.39")
        self.assertIsNone(chrome.pinned(p, "linux-arm64"))
        self.assertIsNone(chrome.pinned(p, "windows-x64"))
        # the platform names of the release factory (tools/package/build.py PLATFORMS)
        self.assertEqual((chrome.key("linux", "x86_64"), chrome.key("linux", "aarch64"), chrome.key("darwin", "arm64"),
                          chrome.key("win32", "AMD64"), chrome.key("darwin", "x86_64"), chrome.key("win32", "ARM64")),
                         ("linux-x64", "linux-arm64", "mac-arm64", "windows-x64", "mac-x64", "windows-arm64"))
        with mock.patch("platform.machine", return_value="x86_64"), \
                mock.patch("platforms.current.claude_platform", return_value="darwin-arm64", create=True):
            chrome._translated.cache_clear()
            self.addCleanup(chrome._translated.cache_clear)
            self.assertEqual(chrome.key("darwin"), "mac-arm64")                 # Rosetta: the arm build runs
        for plat in ("linux-x64", "linux-arm64", "mac-arm64", "windows-x64", "mac-x64", "windows-arm64"):
            self.assertIsNotNone(chrome.pinned(plat=plat), f"versions.json: Chrome for {plat} needs url, sha256 and size")


class Checklist(Base):
    def make(self, claude=None, cfg=None):
        return setup.Setup(self.state, lambda: cfg or {}, claude=claude or [self.claude], repo=self.tmp)

    def test_no_claude_holds_the_hub_and_installs_by_itself_once(self):
        s = self.make(claude=[os.path.join(self.tmp, "none")])
        st = s.status()
        self.assertEqual((st["ready"], st["blocking"], st["auto"]), (False, ["claude", "login"], ["claude"]))
        self.assertEqual(st["items"][1]["state"], "wait")
        job.Job(self.state, "claude").finish("failed", why="network", text="no network")
        st = s.status()
        self.assertEqual((st["items"][0]["state"], st["auto"]), ("failed", []))

    def test_a_hub_that_has_claude_or_works_is_never_installed_behind_its_back(self):
        broken = exe.script(os.path.join(self.tmp, "broken"), "import sys\nsys.exit(1)\n")
        self.assertEqual(self.make(claude=[broken]).status()["auto"], [])   # the program is there, --version stumbled
        self.assertEqual(self.make(claude=[os.path.join(self.tmp, "none")]).status(working=True)["auto"], [])

    def test_signed_out_is_red_signed_in_is_green(self):
        s = self.make()
        st = s.status()
        self.assertEqual((st["ready"], st["items"][1]["state"]), (False, "todo"))
        open(os.path.join(self.home, "logged"), "w").close()
        s.cache.clear()
        self.assertTrue(s.status()["ready"])

    def test_a_working_hub_is_never_asked_to_sign_in_by_the_band(self):
        s = self.make()
        st = s.status(full=False, working=True)
        self.assertTrue(st["ready"])
        self.assertEqual(st["items"][1]["via"], "working")
        self.assertNotIn("auth", s.cache)   # the band did not start `claude auth status`

    def test_a_running_download_does_not_start_claude_on_every_poll(self):
        s = self.make()
        calls = []
        real = setup._run
        with mock.patch.object(setup, "_run", lambda argv, t=30: calls.append(argv) or real(argv, t)):
            j = job.Job(self.state, "chrome")
            s.status()
            for i in range(3):
                j.progress(i + 1, 10)
                j._shown = 0
                s.status()
            self.assertEqual(len([c for c in calls if c[-1] == "--version"]), 1)
            j.finish("done", path="x")
            s.status()
            self.assertEqual(len([c for c in calls if c[-1] == "--version"]), 2)   # a step ended: asked again

    def system_chrome_only(self, mac, downloaded=None):
        """A machine whose only Chrome is the system one (Google Chrome in /Applications); -> its path."""
        system = os.path.join(self.tmp, "Google Chrome")
        open(system, "w").close()
        env = mock.patch.dict(os.environ)   # the run's own test Chrome (HUB_TEST_CHROME) would be "set" for find()
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(testchrome.ENV, None)
        for obj, name, value in ((browsers, "find_chrome", lambda cfg, b=None: system), (testchrome, "MAC", mac),
                                 (testchrome, "shell", lambda: None), (testchrome, "testing_build", lambda: None),
                                 (testchrome, "downloaded", lambda: downloaded),
                                 (chrome, "pinned", lambda *a, **k: {"size": 150_000_000})):
            patch = mock.patch.object(obj, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        return system

    def test_on_macos_the_system_chrome_is_no_chrome_for_snapshots_so_the_hub_downloads_its_own(self):
        # 13.11: a hub from a package on a Mac with Google Chrome in /Applications took the row for done and downloaded
        # nothing, while testchrome.find() never takes that Chrome there (a dock icon): `hub shot` had no browser at all
        self.system_chrome_only(mac=True)
        s = self.make()
        started = []
        with mock.patch.object(s, "run", lambda step, lang=None: started.append(step) or (202, {})):
            auto.Auto(self.state, s, working=True).round()   # a hub already installed picks it up by itself, no one clicks
        self.assertEqual([x for x in started if x == "chrome"], ["chrome"])
        item = next(i for i in s.status()["items"] if i["id"] == "chrome")
        self.assertEqual((item["state"], item["available"]), ("todo", True))

    def test_the_chrome_row_is_done_once_the_hub_has_its_own_on_macos_and_with_the_system_one_elsewhere(self):
        own = os.path.join(self.tmp, "runtime-chrome")
        open(own, "w").close()
        self.system_chrome_only(mac=True, downloaded=own)
        item = self.make().item_chrome({})
        self.assertEqual((item["state"], item["path"]), ("ok", own))
        started = []
        s = self.make()
        with mock.patch.object(s, "run", lambda step, lang=None: started.append(step) or (202, {})):
            auto.Auto(self.state, s, working=True).round()
        self.assertNotIn("chrome", started)   # nothing to download

    def test_with_the_system_chrome_only_the_row_is_done_off_macos(self):
        system = self.system_chrome_only(mac=False)   # Linux, Windows: the system Chrome serves, as before
        item = self.make().item_chrome({})
        self.assertEqual((item["state"], item["path"]), ("ok", system))

    def test_an_older_claude_without_auth_status_is_unknown_not_red(self):
        open(os.path.join(self.home, "old"), "w").close()
        st = self.make().status()
        self.assertEqual((st["ready"], st["items"][1]["state"]), (True, "unknown"))

    def test_a_running_step_whose_process_is_gone_is_broken(self):
        j = job.Job(self.state, "chrome")
        j.set(pid=2 ** 22 + 12345)
        self.assertEqual(self.make().job("chrome")["state"], "failed")
        self.assertEqual(self.make().job("chrome")["why"], "gone")

    @cut.needs_pin()
    def test_team_only_where_its_modules_are_in_play(self):
        os.makedirs(os.path.join(self.tmp, "modules"))
        with open(os.path.join(self.tmp, "modules", "private.json"), "w", encoding="utf-8") as f:   # an active pin
            json.dump({"repo": "acme/team-modules", "url": "https://example.invalid/pm.git", "ref": "pm-v1.0.0",
                       "commit": "a" * 40, "modules": {"accounts": {"subdir": "accounts", "cap": "profile"}}}, f)
        cfgdir = os.path.join(self.tmp, "cfg")
        os.makedirs(cfgdir)
        with mock.patch.dict(os.environ, {"HUB_CONFIG_DIR": cfgdir, "CLAUDE_CONFIG_DIR": self.home}):
            team = next(i for i in self.make().status()["items"] if i["id"] == "team")
            self.assertFalse(team["visible"])
            with open(os.path.join(cfgdir, "accounts.json"), "w") as f:
                json.dump({"accounts": [{"name": "a"}]}, f)
            team = next(i for i in self.make().status()["items"] if i["id"] == "team")
            self.assertTrue(team["visible"])
        os.remove(os.path.join(self.tmp, "modules", "private.json"))
        self.assertFalse([i for i in self.make().status()["items"] if i["id"] == "team"])

    def test_a_step_runs_in_its_own_process_and_reports(self):
        port = socket.socket()
        port.bind(("127.0.0.1", 0))
        dead = port.getsockname()[1]
        port.close()
        s = self.make(claude=[os.path.join(self.tmp, "none")])
        with mock.patch.dict(os.environ, {"HUB_CLAUDE_DOWNLOADS": f"http://127.0.0.1:{dead}",
                                          "HUB_TEST_CLAUDE_RELEASE": "2.1.400"}):
            self.assertEqual(s.run("claude", "en")[0], 202)
        st = until(lambda: (lambda x: x if x.get("state") in ("failed", "done") else None)(self.status("claude")), 60)
        self.assertEqual((st["state"], st["why"]), ("failed", "network"), st)
        self.assertEqual(s.run("nonsense")[0], 400)


class DoctorLanguage(Base):
    def test_the_check_names_the_language_of_its_rows_and_the_page_sees_it(self):
        # its rows are text: the page asks it again when they are not in its own language (setup.js otherLang)
        out = "ok    settings  /x/hub.json\nfail  Claude sign-in  no\n"
        run = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, out, "")   # noqa: E731
        j = job.Job(self.state, "doctor")
        with mock.patch.object(job.i18n, "current", return_value="ru"):
            job.step_doctor(j, run=run)
        self.assertEqual((self.status("doctor")["lang"], self.status("doctor")["rows"][1][0]), ("ru", "fail"))
        s = setup.Setup(self.state, lambda: {}, claude=[self.claude])
        self.assertEqual(s.item_doctor()["job"]["lang"], "ru")


class SupportBundle(Base):
    def test_masked_and_without_the_journal(self):
        key = "sk-ant-" + "q" * 40
        os.makedirs(os.path.join(self.state, "logs"))
        with open(os.path.join(self.state, "logs", "hub-up.log"), "w") as f:
            f.write("".join(f"line {i}\n" for i in range(3000)) + f"ANTHROPIC_API_KEY={key}\n")
        with open(os.path.join(self.state, "journal.jsonl"), "w") as f:
            f.write('{"type":"message.received","data":{"text":"private words"}}\n')
        cfg = os.path.join(self.tmp, "hub.json")
        with open(cfg, "w") as f:
            json.dump({"title": "my hub", "telegram": {"token": "123:" + "z" * 35}, "api_tokens": ["plain-one", "plain-two"],
                       "mcp": {"args": ["--port", "1", "--api-key", "plain-three", "--secret=plain-four", "serve"]},
                       "max_tokens": 4096, "pin_secret": 1,
                       "tools": {"x": {"server": {"command": "npx", "env": {"API": "visible-looking"}}}}}, f)
        with mock.patch.dict(os.environ, {"HUB_CONFIG": cfg}):
            name, data = supportbundle.build(self.state, claude=[self.claude], doctor_text="ok doctor\n")
        z = zipfile.ZipFile(io.BytesIO(data))
        names = set(z.namelist())
        self.assertTrue(name.startswith("hub-support-") and name.endswith(".zip"))
        self.assertEqual(names, {"versions.json", "doctor.txt", "logs/hub-up.log", "hub.json", "state-files.txt"})
        log = z.read("logs/hub-up.log").decode()
        self.assertEqual(len(log.splitlines()), supportbundle.LOG_LINES)
        self.assertNotIn(key, log)
        self.assertIn(redact.MASK, log)
        hub = json.loads(z.read("hub.json"))
        self.assertEqual((hub["title"], hub["telegram"]["token"], hub["tools"]["x"]["server"]["env"]["API"]),
                         ("my hub", redact.MASK, redact.MASK))
        self.assertIn("journal.jsonl", z.read("state-files.txt").decode())
        self.assertNotIn(b"private words", data)
        self.assertEqual(hub["api_tokens"], redact.MASK)
        self.assertEqual(hub["mcp"]["args"], ["--port", "1", "--api-key", redact.MASK, "--secret=" + redact.MASK, "serve"])
        self.assertEqual((hub["max_tokens"], hub["pin_secret"]), (4096, redact.MASK))
        self.assertNotIn(b"plain-", data)
        self.assertEqual(json.loads(z.read("versions.json"))["claude"], "2.1.309 (Claude Code)")


@unittest.skipUnless(POSIX, "the service layouts of macOS and Linux")
class Uninstall(Base):
    def wrapper(self, name, text):
        d = os.path.join(self.home, ".local", "bin")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "w") as f:
            f.write(text)
        return os.path.join(d, name)

    def test_the_plan_takes_only_this_hubs_service_and_commands(self):
        repo = os.path.join(self.tmp, "code")
        own = self.wrapper("hub-mine", f'#!/bin/sh\nHUB_STATE_DIR="{self.state}" HUB_CONFIG_DIR="x" exec py "{repo}/bin/hub" "$@"\n')
        other = self.wrapper("hub-other", f'#!/bin/sh\nHUB_STATE_DIR="/elsewhere" exec py "{repo}/bin/hub" "$@"\n')
        bare = self.wrapper("hub", f'#!/bin/sh\nexec py "{repo}/bin/hub" "$@"\n')
        foreign = self.wrapper("hub-thing", "#!/bin/sh\necho someone else's\n")
        unit = {"kind": "systemd", "id": "hub-core.service", "path": os.path.join(self.tmp, "hub-core.service")}
        with mock.patch.object(platforms.current, "uninstall_service", platforms.posix.uninstall_service):
            steps = uninstall.plan(self.state, os.path.join(self.tmp, "cfg"), home=self.home, repo=repo, unit=unit)
        paths = [s.get("path") for s in steps]
        self.assertIn(own, paths)
        self.assertNotIn(other, paths)
        self.assertNotIn(foreign, paths)
        self.assertNotIn(bare, paths)   # the bare `hub` leads to the default hub, which this one is not
        argv = [s["argv"] for s in steps if s["do"] == "run"]
        self.assertEqual(argv[0], ["systemctl", "--user", "disable", "--now", "hub-core.service"])
        self.assertIn(["systemctl", "--user", "daemon-reload"], argv)
        self.assertFalse([s for s in steps if s["do"] == "tree"])   # the data stays without --purge

    def test_the_bare_command_goes_with_the_default_hub(self):
        repo = os.path.join(self.tmp, "code")
        bare = self.wrapper("hub", f'#!/bin/sh\nexec py "{repo}/bin/hub" "$@"\n')
        os.makedirs(os.path.join(self.home, ".config", "hub-core"))
        with open(os.path.join(self.home, ".config", "hub-core", "state-dir"), "w") as f:
            f.write(self.state + "\n")
        self.assertIn(bare, uninstall.commands(self.state, repo, self.home))

    def test_purge_only_a_hub_folder_and_after_confirmation(self):
        with mock.patch.dict(os.environ, {"HOME": self.home}):
            self.assertFalse(uninstall.looks_like_hub(self.home, self.home))
            self.assertFalse(uninstall.looks_like_hub(self.state, self.home))   # empty: not a hub's
            os.makedirs(os.path.join(self.state, "logs"))
            self.assertFalse(uninstall.looks_like_hub(self.state, self.home))   # a project with logs/ is no hub
            with open(os.path.join(self.state, "journal.jsonl"), "w") as f:
                f.write(HUB_JOURNAL)
            self.assertTrue(uninstall.looks_like_hub(self.state, self.home))
            cfg = os.path.join(self.tmp, "hub-cfg")
            os.makedirs(cfg)
            open(os.path.join(cfg, "hub.json"), "w").close()
            out = io.StringIO()
            with mock.patch("sys.stdout", out), mock.patch.object(uninstall, "execute", return_value=0) as ex, \
                    mock.patch("sys.stderr", io.StringIO()):
                code = uninstall.main(["--purge"], self.state, cfg, None, ask=lambda q: "y", tty=False)
                self.assertEqual(code, 2)   # no terminal and no --yes: nothing happens
                code = uninstall.main(["--purge"], self.state, cfg, None, ask=lambda q, it=iter(["y", "wrong"]): next(it), tty=True)
                self.assertEqual(code, 1)
                ex.assert_not_called()
                code = uninstall.main(["--purge"], self.state, cfg, None,
                                      ask=lambda q, it=iter(["y", os.path.basename(self.state)]): next(it), tty=True)
                steps = ex.call_args[0][0]
            self.assertEqual({s["path"] for s in steps if s["do"] == "tree"}, {self.state, cfg})
            self.assertEqual(code, 0)

    def test_a_second_hub_keeps_the_default_hubs_settings_and_commands(self):
        # `HUB_STATE_DIR=<second> hub uninstall --purge` without HUB_CONFIG_DIR: the settings folder is the default
        # hub's (its pointer names another state) — never removed; a wrapper of a longer path is not this hub's
        repo = os.path.join(self.tmp, "code")
        default_state = os.path.join(self.tmp, "main-hub")
        cfg = os.path.join(self.home, ".config", "hub-core")
        os.makedirs(cfg)
        with open(os.path.join(cfg, "state-dir"), "w") as f:
            f.write(default_state + "\n")
        open(os.path.join(cfg, "hub.json"), "w").close()
        longer = self.wrapper("hub-longer", f"#!/bin/sh\nHUB_STATE_DIR={self.state}-two exec py {repo}/bin/hub\n")
        own = self.wrapper("hub-mine", f"#!/bin/sh\nHUB_STATE_DIR={self.state} exec py {repo}/bin/hub\n")
        quoted = self.wrapper("hub-quoted", f"#!/bin/sh\nHUB_STATE_DIR='{self.state}' exec py {repo}/bin/hub\n")
        os.makedirs(self.state, exist_ok=True)
        steps = uninstall.plan(self.state, cfg, home=self.home, repo=repo, purge=True)
        self.assertEqual({s["path"] for s in steps if s["do"] == "tree"}, {self.state})
        paths = [s.get("path") for s in steps]
        self.assertIn(own, paths)
        self.assertIn(quoted, paths)
        self.assertNotIn(longer, paths)
        with mock.patch.object(platforms.current, "config_dir", return_value=cfg):   # %APPDATA% or XDG_CONFIG_HOME
            other_cfg = uninstall.plan(self.state, cfg, home=os.path.join(self.tmp, "elsewhere"), repo=repo, purge=True)
        self.assertNotIn(cfg, {s["path"] for s in other_cfg if s["do"] == "tree"})
        # the default hub itself takes its own settings folder
        os.makedirs(default_state)
        steps = uninstall.plan(default_state, cfg, home=self.home, repo=repo, purge=True)
        self.assertIn(cfg, {s["path"] for s in steps if s["do"] == "tree"})

    def test_dry_run_changes_nothing_and_execute_goes_on_past_a_failure(self):
        own = self.wrapper("hub-mine", f'#!/bin/sh\nHUB_STATE_DIR="{self.state}" exec py "{uninstall.REPO}/bin/hub"\n')
        with mock.patch("sys.stdout", io.StringIO()), mock.patch.dict(os.environ, {"HOME": self.home}):
            self.assertEqual(uninstall.main(["--dry-run"], self.state, self.tmp, None), 0)
        self.assertTrue(os.path.exists(own))
        said = []
        bad = uninstall.execute([{"do": "run", "argv": ["x"], "what": "a"}, {"do": "remove", "path": own, "what": "b"}],
                                run=lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "boom"), say=said.append)
        self.assertEqual(bad, 1)
        self.assertFalse(os.path.exists(own))
        self.assertTrue(said[0].startswith("  ✗ a") and said[1] == "  ✓ b")

    def hub_up(self, deaf=False, **up):
        """A stand-in hub-up of this state folder: it writes about itself as hub-up does (core/restart.UP) and exits a
        moment after the stop mark appears, as `hub stop` asks it to; deaf — it never does."""
        script = os.path.join(self.tmp, "bin", "hub-up")
        os.makedirs(os.path.dirname(script))
        with open(script, "w") as f:
            f.write("import os, sys, time\nstop = os.path.join(sys.argv[1], 'stop')\n"
                    + ("while True:\n    time.sleep(0.05)\n" if deaf else
                       "while not os.path.exists(stop):\n    time.sleep(0.05)\ntime.sleep(0.5)\n"))
        p = subprocess.Popen([sys.executable, script, self.state])
        self.addCleanup(lambda: p.poll() is None and (p.kill(), p.wait()))
        self.thread(p.wait)   # reaped at once: a zombie still answers signal 0
        with open(os.path.join(self.state, "hub-up.json"), "w") as f:
            json.dump({"pid": p.pid, "started": platforms.current.process_start(p.pid), **up}, f)
        return p

    def purge_steps(self, unit=None):
        with open(os.path.join(self.state, "journal.jsonl"), "w") as f:
            f.write(HUB_JOURNAL)
        return uninstall.plan(self.state, os.path.join(self.tmp, "cfg"), home=self.home, unit=unit, purge=True)

    def test_purge_signs_out_of_codex_before_its_folder_goes(self):
        # the hub's Codex sign-in and a pool account's (the spec of several hubs, 2.6): out first, then the folder; a
        # sign-out that fails is said and the data goes anyway; without --purge nothing of Codex is touched
        hub = os.path.join(self.state, "codex-home")
        pool = os.path.join(self.state, "moddata", "accounts", "codex", "a1")
        for d in (hub, pool):
            os.makedirs(d)
            for name in ("auth.json", "config.toml"):
                open(os.path.join(d, name), "w").close()
        os.makedirs(os.path.join(self.state, "moddata", "accounts", "empty"))
        steps = self.purge_steps()
        kinds = [s["do"] for s in steps]
        outs = [s["home"] for s in steps if s["do"] == "codex_logout"]
        self.assertEqual(outs, [hub, pool])
        self.assertLess(max(i for i, k in enumerate(kinds) if k == "codex_logout"), kinds.index("tree"))
        self.assertGreater(kinds.index("codex_logout"), kinds.index("halt"))
        self.assertNotIn("codex_logout", [s["do"] for s in uninstall.plan(self.state, os.path.join(self.tmp, "cfg"),
                                                                           home=self.home)])
        done, said = [], []

        def logout(state, home):
            done.append(home)
            if home == pool:
                raise RuntimeError("account/logout: refused")
        with mock.patch.object(uninstall, "codex_logout", logout):
            bad = uninstall.execute(steps, say=said.append)
        self.assertEqual(done, [hub, pool])
        self.assertEqual(bad, 1, said)
        self.assertFalse(os.path.exists(self.state), said)

    def test_a_codex_that_is_not_there_is_a_failed_sign_out(self):
        # no program: "not found" is not "already gone" (execute takes FileNotFoundError for a step done)
        os.makedirs(os.path.join(self.state, "codex-home"))
        open(os.path.join(self.state, "codex-home", "auth.json"), "w").close()
        said = []
        with mock.patch.dict(os.environ, {"HUB_CODEX_BIN": os.path.join(self.tmp, "no-such-codex")}):
            bad = uninstall.execute([s for s in self.purge_steps() if s["do"] == "codex_logout"], say=said.append)
        self.assertEqual(bad, 1, said)
        self.assertTrue(said[0].lstrip().startswith("✗"), said)

    def test_a_codex_sign_in_does_not_change_whose_settings_folder_goes(self):
        # the default hub with a Codex sign-in: its own settings folder still goes with it
        repo = os.path.join(self.tmp, "code")
        default_state = os.path.join(self.tmp, "main-hub")
        cfg = os.path.join(self.home, ".config", "hub-core")
        os.makedirs(cfg)
        with open(os.path.join(cfg, "state-dir"), "w") as f:
            f.write(default_state + "\n")
        open(os.path.join(cfg, "hub.json"), "w").close()
        os.makedirs(os.path.join(default_state, "codex-home"))
        open(os.path.join(default_state, "codex-home", "auth.json"), "w").close()
        steps = uninstall.plan(default_state, cfg, home=self.home, repo=repo, purge=True)
        self.assertIn(cfg, {s["path"] for s in steps if s["do"] == "tree"})
        self.assertIn("codex_logout", [s["do"] for s in steps])

    def test_nothing_more_is_done_after_a_hub_that_would_not_stop(self):
        # no data deleted under it, and its `hub` command stays to stop it with, as the failure tells
        p = self.hub_up(deaf=True)
        own = self.wrapper("hub-mine", f'#!/bin/sh\nHUB_STATE_DIR="{self.state}" exec py "{uninstall.REPO}/bin/hub"\n')
        said, steps = [], self.purge_steps()
        self.assertIn(own, [s.get("path") for s in steps])
        with mock.patch.object(uninstall, "HALT_WAIT", 1):
            bad = uninstall.execute(steps, say=said.append)
        self.assertIsNone(p.poll())
        self.assertTrue(os.path.exists(os.path.join(self.state, "journal.jsonl")), said)
        self.assertTrue(os.path.exists(own), said)
        after = [s for s in steps[[s["do"] for s in steps].index("halt"):]]
        self.assertEqual(bad, len(after), said)   # the stop and everything after it

    def test_a_service_that_did_not_go_off_keeps_its_hub_up(self):
        # `systemctl disable --now` (or launchctl bootout) failed: the service may start hub-up again, so it is not
        # stopped behind it — the data stays
        p = self.hub_up(service="systemctl --user stop agents-hub.service")
        path = os.path.join(self.tmp, "agents-hub.service")
        open(path, "w").close()
        unit = {"kind": "systemd", "id": "agents-hub.service", "path": path}
        with mock.patch.object(platforms.current, "uninstall_service", platforms.posix.uninstall_service):
            steps = self.purge_steps(unit)
        said = []
        uninstall.execute(steps, run=lambda argv, **kw: subprocess.CompletedProcess(argv, 3, "", "boom"),
                          say=said.append)
        self.assertIsNone(p.poll(), said)
        self.assertFalse(os.path.exists(os.path.join(self.state, "stop")))
        self.assertTrue(os.path.exists(os.path.join(self.state, "journal.jsonl")))

    def test_hub_up_by_its_start_time(self):
        from core import restart
        p = self.hub_up()
        self.assertEqual(restart.running(self.state)["pid"], p.pid)
        with open(os.path.join(self.state, "hub-up.json"), "w") as f:   # the number now another process's
            json.dump({"pid": p.pid, "started": -1}, f)
        self.assertEqual(restart.running(self.state), {})
        # the start time cannot be read now: the command line decides, a live hub-up is not taken for gone
        with mock.patch.object(platforms.current, "process_start", return_value=None):
            self.assertEqual(restart.running(self.state)["pid"], p.pid)

    def test_a_hub_up_its_service_holds_is_not_stopped_behind_its_back(self):
        # launchd or systemd would bring it up again at once: stopping it is the service's job (`hub off`), and its data
        # stays while it runs
        p = self.hub_up(service="launchctl bootout gui/1/x")
        said = []
        bad = uninstall.execute(self.purge_steps(), say=said.append)
        self.assertIsNone(p.poll())
        self.assertFalse(os.path.exists(os.path.join(self.state, "stop")))
        self.assertTrue(os.path.exists(os.path.join(self.state, "journal.jsonl")))
        self.assertEqual(bad, 2, said)

    def test_a_hub_run_without_a_service_is_stopped_and_waited_for(self):
        # a container or a terminal: no service to take off, hub-up runs on its own. Its data must not go while it still
        # writes (its snapshot), so the plan stops it and waits until it has gone
        p = self.hub_up()
        steps = uninstall.plan(self.state, os.path.join(self.tmp, "cfg"), home=self.home)
        said = []
        self.assertEqual(uninstall.execute(steps, say=said.append), 0, said)
        self.assertIsNotNone(p.poll(), "hub-up still runs after the plan")
        self.assertTrue(os.path.exists(os.path.join(self.state, "stopped")))   # the scheduler's retry leaves it down

    def test_a_hub_that_is_already_down_is_no_failure(self):
        # `hub off`, then `hub uninstall`: nothing to stop — every step succeeds, no «not everything came off»
        steps = uninstall.plan(self.state, os.path.join(self.tmp, "cfg"), home=self.home)
        said = []
        self.assertEqual(uninstall.execute(steps, say=said.append), 0, said)

    def test_a_service_manager_that_cannot_be_reached_is_no_failure(self):
        # a container: the installer wrote the unit, but no systemd --user runs it (systemctl: «Failed to connect to
        # bus», exit 1). Disabling and reloading it has nothing to do, the hub-up started by hand is stopped all the same
        p = self.hub_up()
        path = os.path.join(self.tmp, "agents-hub.service")
        open(path, "w").close()
        unit = {"kind": "systemd", "id": "agents-hub.service", "path": path}
        bus = {k: v for k, v in os.environ.items() if k not in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")}
        with mock.patch.object(platforms.current, "uninstall_service", platforms.posix.uninstall_service), \
                mock.patch.dict(os.environ, bus, clear=True):
            steps = uninstall.plan(self.state, os.path.join(self.tmp, "cfg"), home=self.home,
                                   repo=os.path.join(self.tmp, "code"), unit=unit)
        said = []
        bad = uninstall.execute(steps, run=lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "no bus"),
                                say=said.append)
        self.assertEqual(bad, 0, said)
        self.assertIsNotNone(p.poll())
        self.assertFalse(os.path.exists(path))
        # a systemd --user that is there: its failure to reload is said
        open(os.path.join(self.tmp, "bus"), "w").close()
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp}):
            reload = [s for s in platforms.posix.uninstall_service(unit, []) if "daemon-reload" in s.get("argv", [])]
        self.assertEqual(uninstall.execute(reload, run=lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", ""),
                                           say=said.append), 1)


HUB_JOURNAL = '{"seq":1,"ts":1,"src":"core","type":"core.started","entity":null,"data":{}}\n'


class PurgeMarks(Base):
    """`hub uninstall --purge` removes a folder only by a mark the hub itself leaves there, never by a name: a wrong
    HUB_STATE_DIR or HUB_CONFIG_DIR may name a project or the home folder."""

    def write(self, path, text=""):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def test_files_named_like_the_hubs_are_no_mark(self):
        self.write(os.path.join(self.state, "state.json"), "{}")   # any program keeps a state.json
        self.assertFalse(uninstall.looks_like_hub(self.state, self.home))
        self.write(os.path.join(self.state, "journal.jsonl"), '{"event": "x"}\n')   # another program's journal
        self.assertFalse(uninstall.looks_like_hub(self.state, self.home))
        with mock.patch.object(uninstall, "execute", return_value=0) as ex, mock.patch("sys.stdout", io.StringIO()), \
                mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(uninstall.main(["--purge", "--yes"], self.state, self.tmp, None), 2)
        ex.assert_not_called()
        self.write(os.path.join(self.state, "journal.jsonl"), HUB_JOURNAL)
        self.assertTrue(uninstall.looks_like_hub(self.state, self.home))
        os.unlink(os.path.join(self.state, "journal.jsonl"))
        self.write(os.path.join(self.state, "hub-home", "CLAUDE.md"), "rules")   # a hub the install made, never started
        self.assertTrue(uninstall.looks_like_hub(self.state, self.home))

    def trees(self, config_dir):
        self.write(os.path.join(self.state, "journal.jsonl"), HUB_JOURNAL)
        steps = uninstall.plan(self.state, config_dir, home=self.home, repo=os.path.join(self.tmp, "code"), purge=True)
        return {s["path"] for s in steps if s["do"] == "tree"}

    def test_a_settings_folder_named_hub_without_the_hubs_settings_stays(self):
        notes = os.path.join(self.tmp, "hub-notes")
        self.write(os.path.join(notes, "todo.txt"), "mine")
        self.assertEqual(self.trees(notes), {self.state})
        self.write(os.path.join(notes, "hub.json"), "{}")
        self.assertEqual(self.trees(notes), {self.state, notes})

    def test_the_standard_settings_folder_goes_without_hub_json_too(self):
        # the default hub's own folder (its bot, its accounts) is the hub's by its place
        cfg = os.path.join(self.home, ".config", "hub-core")
        self.write(os.path.join(cfg, "state-dir"), self.state + "\n")   # the install's pointer: this is the default hub
        self.write(os.path.join(cfg, "telegram.json"), "{}")
        self.assertEqual(self.trees(cfg), {self.state, cfg})

    def test_the_home_folder_and_above_are_never_removed_as_settings(self):
        self.write(os.path.join(self.home, "hub.json"), "{}")
        self.write(os.path.join(self.tmp, "hub.json"), "{}")   # self.tmp holds the home folder
        self.assertEqual(self.trees(self.home), {self.state})
        self.assertEqual(self.trees(self.tmp), {self.state})
        shared = os.path.join(self.home, ".config")   # HUB_CONFIG_DIR=~/.config: every program's settings
        self.write(os.path.join(shared, "hub.json"), "{}")
        self.assertEqual(self.trees(shared), {self.state})
        link = os.path.join(self.tmp, "hub-settings")
        if winsafe.try_symlink(self.home, link):
            self.assertEqual(self.trees(link), {self.state})


class Screen(Base):
    def test_the_checklist_says_whether_this_machine_shows_a_browser(self):
        # a server opened through an SSH tunnel asks from 127.0.0.1 like its own keyboard; only «no screen here» tells
        # the page that the sign-in's localhost link would land on the person's computer, not on this one
        for screen in (True, False):
            s = setup.Setup(self.state, lambda: {}, claude=[self.claude], screen=lambda v=screen: v)
            got = s.status(local=True, full=False)
            self.assertEqual((got["local"], got["screen"]), (True, screen))

    def test_the_default_check_is_the_platforms_and_is_not_asked_on_every_poll(self):
        import platforms
        asked = []
        with mock.patch.object(platforms.current, "has_screen", lambda env: asked.append(env) or False):
            s = setup.Setup(self.state, lambda: {}, claude=[self.claude])
            self.assertIsNone(s.status(local=False, full=False)["screen"])   # a remote browser: not its business
            self.assertEqual(asked, [])
            self.assertFalse(s.status(local=True, full=False)["screen"])
            self.assertFalse(s.status(local=True, full=False)["screen"])
        self.assertEqual(len(asked), 1)   # a service asks systemd and loginctl for it: once a minute

    def test_a_linux_service_without_a_display_still_has_the_users_screen(self):
        # the installer's hub is a systemd --user service: its environment has no DISPLAY on a desktop too, and the
        # one-click sign-in must stay there; a server with only ssh sessions is the one without a screen
        from platforms import posix, windows
        which = {"systemctl": "/bin/systemctl", "loginctl": "/bin/loginctl"}.get
        me = 1000

        def run(manager="", sessions=None):
            def fake(cmd, **kw):
                out = ""
                if cmd[:3] == ["systemctl", "--user", "show-environment"]:
                    out = manager
                elif cmd[:2] == ["loginctl", "list-sessions"]:
                    out = "".join(f"{sid} {uid} user seat0 -\n" for sid, (uid, _) in (sessions or {}).items())
                elif cmd[:2] == ["loginctl", "show-session"]:
                    out = (sessions or {}).get(cmd[2], (0, ""))[1] + "\n"
                return subprocess.CompletedProcess(cmd, 0, out, "")
            return fake
        with mock.patch.object(posix.os, "getuid", lambda: me, create=True):
            self.assertTrue(posix.has_screen({"DISPLAY": ":0"}, "linux", which, run()))
            self.assertTrue(posix.has_screen({"WAYLAND_DISPLAY": "wayland-0"}, "linux", which, run()))
            self.assertTrue(posix.has_screen({}, "linux", which, run(manager="PATH=/bin\nDISPLAY=:0\n")))
            self.assertTrue(posix.has_screen({}, "linux", which, run(sessions={"3": (me, "wayland")})))
            self.assertTrue(posix.has_screen({}, "darwin", {}.get, run()))   # a Mac under launchd has no DISPLAY either
            for sessions in ({"5": (me, "tty")}, {"7": (me + 1, "x11")}, {}):
                self.assertFalse(posix.has_screen({}, "linux", which, run(sessions=sessions)), sessions)
            self.assertFalse(posix.has_screen({}, "linux", {}.get, run(sessions={"3": (me, "x11")})))   # no loginctl
        self.assertTrue(windows.has_screen({}))


class PageRoutes(Base):
    def setUp(self):
        super().setUp()
        from tests import test_web
        self.w = test_web.WebTest("run")
        self.w.setUp()
        self.addCleanup(self.w.tearDown)
        self.w.web._setup = setup.Setup(self.w.state, lambda: {}, claude=[self.claude])

    def test_the_checklist_and_its_actions(self):
        code, body = self.w.req("GET", "/api/setup")
        self.assertEqual(code, 200)
        got = json.loads(body)
        self.assertEqual([i["id"] for i in got["items"]][:3], ["claude", "login", "chrome"])
        self.assertEqual(got["blocking"], ["login"])
        self.assertTrue(got["local"])
        self.assertIn("screen", got)
        code, body = self.w.req("POST", "/api/setup/run", {"step": "nonsense"})
        self.assertEqual(code, 400)
        code, body = self.w.req("POST", "/api/setup/input", {"step": "login", "text": "x"})
        self.assertEqual(code, 409)
        self.assertTrue(json.loads(body)["error"])
        self.assertIn("setup", self.w.snap()["features"])

    def test_the_file_for_support_only_by_a_post_from_the_page(self):
        with mock.patch.object(supportbundle, "doctor", lambda state: "ok\n"), \
                mock.patch.object(supportbundle, "versions", lambda state, claude=None: {"hub": {}}):
            code, data = self.w.req("POST", "/api/setup/bundle", {})
            self.assertEqual(code, 200)
            self.assertIn("doctor.txt", zipfile.ZipFile(io.BytesIO(data)).namelist())
            code, _ = self.w.req("POST", "/api/setup/bundle", {}, headers={"X-Hub": ""})
            self.assertEqual(code, 403)
            code, data = self.w.req("GET", "/api/setup/bundle")
            self.assertNotIn(b"doctor.txt", data)


if __name__ == "__main__":
    unittest.main()
