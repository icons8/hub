"""`hub mcp login` on a hub without a browser (2026-10-11: the hub moved to a server without a screen — `claude mcp
login` opened the sign-in where nobody sees it, and caught the reply on the server's localhost). Without a browser the
sign-in goes by link: the command starts the sign-in in the background and prints a link, the owner signs in on their
side and sends back the localhost…?code= address from the browser's address bar, a second call with --redirect hands it
to the waiting sign-in. With a browser — as before. And an MCP from the hub catalog (the core gives it to a session
with its own --mcp-config) is now visible to the sign-in: it runs from its own folder with the same server description."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest

from core import mcplogin

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(REPO, "bin", "hub")
OWNER = {"channel": "cli", "owner": True}

# a fake claude: `mcp login --no-browser <server>` prints a link and waits for the return address on stdin, like the real one
FAKE = """#!{python}
import json, os, sys
if not os.isatty(0):
    print("stdin isn't a terminal"); sys.exit(1)
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps({{"cwd": os.getcwd(), "args": sys.argv[1:],
                        "mcp": open(".mcp.json").read() if os.path.exists(".mcp.json") else None}}) + "\\n")
if os.environ.get("FAKE_APPROVAL"):   # like the real one (2.1.302): a server from .mcp.json joins only if approved for the folder
    proj = json.load(open(os.path.join(os.environ["CLAUDE_CONFIG_DIR"], ".claude.json")))["projects"].get(
        os.path.realpath(os.getcwd()).replace(os.sep, "/"), {{}})
    if not proj.get("hasTrustDialogAccepted") or sys.argv[-1] not in proj.get("enabledMcpjsonServers", []):
        print(f'"{{sys.argv[-1]}}" is from .mcp.json and awaiting approval.'); sys.exit(1)
print("Open this URL:\\n  " + os.environ.get("FAKE_AUTH", "https://mcp.example.com/authorize") +
      "?response_type=code&code_challenge=x&redirect_uri=http%3A%2F%2Flocalhost%3A5%2Fcallback"
      "\\nWaiting for authorization… Or paste the redirect URL here: ", end="", flush=True)
line = sys.stdin.readline()
if "code=" in line:
    print("Authenticated with the server."); sys.exit(0)
print("Couldn't complete authentication"); sys.exit(1)
"""


def helper_pid(work):
    """The pid of the waiting sign-in, wherever in its folder it lies."""
    for folder, _, files in os.walk(work):
        if "pid" in files:
            with open(os.path.join(folder, "pid")) as f:
                return int(f.read())
    return None


def gone(pid, secs):
    """Whether the process exited within secs. Whether it is alive is decided by platforms: on Windows os.kill(pid, 0)
    is Ctrl+C; on POSIX the zombie must also be reaped."""
    import platforms
    end = time.time() + secs
    while time.time() < end:
        if not platforms.current.alive(pid):
            return True
        if os.name != "nt":
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
        time.sleep(0.1)
    return False


def fake_claude():
    d = tempfile.mkdtemp(prefix="bin-")
    path = os.path.join(d, "claude")
    with open(path, "w", encoding="utf-8") as f:
        f.write(FAKE.format(python=sys.executable))
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    if os.name == "nt":   # a shebang does not work there — claude.cmd, like the real one
        with open(path + ".cmd", "w") as f:
            f.write(f'@"{sys.executable}" "{path}" %*\r\n')
    return d


class Browser(unittest.TestCase):
    def test_which_machines_have_a_browser(self):
        from platforms import posix, windows
        self.assertTrue(posix.has_browser({}, "darwin"))
        self.assertTrue(posix.has_browser({"DISPLAY": ":0"}, "linux"))
        self.assertTrue(posix.has_browser({"WAYLAND_DISPLAY": "wayland-0"}, "linux"))
        self.assertFalse(posix.has_browser({}, "linux"))                # a server without a screen
        self.assertTrue(windows.has_browser({}))


class CatalogServer(unittest.TestCase):
    def test_login_folder_carries_the_sessions_own_server_definition(self):
        # 2026-10-11: "No MCP server named conduit" — a server from the hub catalog exists only in the mcp.json the core gives the session
        state = tempfile.mkdtemp()
        os.makedirs(os.path.join(state, "sessions", "maps"))
        srv = {"type": "http", "url": "https://data.example.com/mcp"}
        with open(os.path.join(state, "sessions", "maps", "mcp.json"), "w") as f:
            json.dump({"mcpServers": {"data": srv, "other": {"type": "http", "url": "https://o.example.com"}}}, f)
        d = mcplogin.login_dir(state, "maps", "data")
        with open(os.path.join(d, ".mcp.json")) as f:
            self.assertEqual(json.load(f), {"mcpServers": {"data": srv}})   # the same description — the sign-in lands under the same key
        with open(os.path.join(d, ".claude", "settings.local.json")) as f:
            self.assertTrue(json.load(f)["enableAllProjectMcpServers"])     # without approval the server is "Pending approval"
        self.assertIsNone(mcplogin.login_dir(state, "maps", "pay"))         # not from the catalog — the session folder, as before
        self.assertIsNone(mcplogin.login_dir(state, "nobody", "data"))


class Link(unittest.TestCase):
    def setUp(self):
        self.bin = fake_claude()
        self.log = os.path.join(self.bin, "log")
        self.env = {**os.environ, "PATH": self.bin + os.pathsep + os.environ["PATH"], "FAKE_LOG": self.log}
        self.env.pop("DISPLAY", None)
        self.env.pop("WAYLAND_DISPLAY", None)
        self.env.pop("HUB_SESSION", None)

    def calls(self):
        with open(self.log) as f:
            return [json.loads(x) for x in f]

    def test_link_then_redirect_signs_in(self):
        work, cwd = tempfile.mkdtemp(), tempfile.mkdtemp()
        old = dict(os.environ)
        os.environ.update(self.env)
        try:
            url, why = mcplogin.start("pay", cwd, work)
            self.assertTrue(url.startswith("https://mcp.example.com/authorize?"), why)
            self.assertEqual(self.calls()[0]["args"], ["mcp", "login", "--no-browser", "pay"])
            code, out = mcplogin.finish(work, "http://localhost:5/callback?code=abc&state=s")
            self.assertEqual(code, 0, out)
            self.assertIn("Authenticated", out)
            code, out = mcplogin.finish(work, "http://localhost:5/callback?code=abc&state=s")   # the sign-in has already finished
            self.assertNotEqual(code, 0)
            self.assertIn("start again", out)
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_a_server_whose_sign_in_page_is_not_called_authorize_still_gives_a_link(self):
        # 2026-10-23: the server's sign-in address is /oauth2/auth?… (from its OAuth metadata) — the link was looked up
        # only by /authorize?, and `hub mcp login` answered "no link within 60 s" although claude had printed it
        work, cwd = tempfile.mkdtemp(), tempfile.mkdtemp()
        old = dict(os.environ)
        os.environ.update(self.env, FAKE_AUTH="https://mcp.example.com/oauth2/auth")
        try:
            url, why = mcplogin.start("pay", cwd, work, wait=20)
            self.assertTrue((url or "").startswith("https://mcp.example.com/oauth2/auth?response_type=code"), why)
            code, out = mcplogin.finish(work, "http://localhost:5/callback?code=abc&state=s")
            self.assertEqual(code, 0, out)
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_the_browser_sign_in_finds_the_same_link(self):
        out = (b"Visit this URL to authorize:\r\n  https://mcp.example.com/oauth2/auth?response_type=code&client_id=c"
               b"&redirect_uri=http%3A%2F%2Flocalhost%3A9%2Fcallback\x1b[1CWaiting for authorization")
        self.assertEqual(mcplogin.URL.search(out).group(0),
                         b"https://mcp.example.com/oauth2/auth?response_type=code&client_id=c"
                         b"&redirect_uri=http%3A%2F%2Flocalhost%3A9%2Fcallback")
        self.assertIsNone(mcplogin.URL.search(b"Docs: https://example.com/help?topic=mcp"))

    def test_a_link_that_only_names_the_client_is_not_the_sign_in(self):
        # the sign-in link is the whole authorization request (response_type and redirect_uri); an address with just a
        # client_id before it (an app page, help) is not a sign-in: the waiting process remembers the first link found
        out = (b"Client: https://mcp.example.com/apps?client_id=c\r\nVisit this URL to authorize:\r\n"
               b"  https://mcp.example.com/oauth2/auth?client_id=c&response_type=code"
               b"&redirect_uri=http%3A%2F%2Flocalhost%3A9%2Fcallback\r\n")
        self.assertEqual(mcplogin.URL.search(out).group(0),
                         b"https://mcp.example.com/oauth2/auth?client_id=c&response_type=code"
                         b"&redirect_uri=http%3A%2F%2Flocalhost%3A9%2Fcallback")
        self.assertIsNone(mcplogin.URL.search(b"https://mcp.example.com/x?response_type=code&scope=a"))

    def test_a_new_link_replaces_the_waiting_one(self):
        work, cwd = tempfile.mkdtemp(), tempfile.mkdtemp()
        old = dict(os.environ)
        os.environ.update(self.env)
        try:
            mcplogin.start("pay", cwd, work)
            first = helper_pid(work)
            mcplogin.start("pay", cwd, work)
            self.assertTrue(gone(first, 10), "the earlier login is still waiting")
            code, out = mcplogin.finish(work, "http://localhost:5/callback?code=abc")
            self.assertEqual(code, 0, out)
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_a_new_link_replaces_the_waiting_one_without_ps(self):
        # a server without ps (a thin container): the previous sign-in was not stopped, wrote its old link where the new
        # one was awaited, and the second call returned the old link
        work, cwd = tempfile.mkdtemp(), tempfile.mkdtemp()
        old = dict(os.environ)
        os.environ.update({**self.env, "PATH": self.bin})   # no ps
        try:
            first, why = mcplogin.start("pay", cwd, work)
            self.assertTrue(first, why)
            helper = helper_pid(work)
            claude = os.path.join(self.bin, "claude")
            with open(claude) as f:
                src = f.read()
            with open(claude, "w") as f:   # the next sign-in gives a different link, and the real claude needs a second
                f.write(src.replace("code_challenge=x", "code_challenge=second")
                        .replace("import json, os, sys", "import json, os, sys, time; time.sleep(1.5)"))
            second, why = mcplogin.start("pay", cwd, work)
            self.assertIn("code_challenge=second", second or "", why)
            self.assertTrue(gone(helper, 10), "the earlier login is still waiting")
            code, out = mcplogin.finish(work, "http://localhost:5/callback?code=abc")
            self.assertEqual(code, 0, out)
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_a_stranger_under_the_recorded_pid_is_left_alone(self):
        # the pid file outlived its sign-in, and the pid now belongs to another process of the user
        work, cwd = tempfile.mkdtemp(), tempfile.mkdtemp()
        stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(stranger.wait)
        self.addCleanup(stranger.kill)
        for where in (work, os.path.join(work, "0123456789abcdef")):   # the old layout and the sign-in folder
            os.makedirs(where, exist_ok=True)
            with open(os.path.join(where, "pid"), "w") as f:
                f.write(str(stranger.pid))
        with open(os.path.join(work, "current"), "w") as f:
            f.write("0123456789abcdef")
        old = dict(os.environ)
        os.environ.update(self.env)
        try:
            url, why = mcplogin.start("pay", cwd, work)
            self.assertTrue(url, why)
            time.sleep(0.3)
            self.assertIsNone(stranger.poll())
            mcplogin.finish(work, "http://localhost:5/callback?code=abc")
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_a_link_that_came_too_late_does_not_leave_the_sign_in_waiting(self):
        # no link within the allotted time — "the sign-in did not start": the waiting process must not accept the return address later
        work, cwd = tempfile.mkdtemp(), tempfile.mkdtemp()
        claude = os.path.join(self.bin, "claude")
        with open(claude) as f:
            src = f.read()
        with open(claude, "w") as f:
            f.write(src.replace("import json, os, sys", "import json, os, sys, time; time.sleep(3)"))
        old = dict(os.environ)
        os.environ.update(self.env)
        try:
            url, why = mcplogin.start("pay", cwd, work, wait=1)
            self.assertIsNone(url, why)
            helper = helper_pid(work)
            self.assertTrue(gone(helper, 15), "the abandoned login is still waiting")
            code, out = mcplogin.finish(work, "http://localhost:5/callback?code=abc")
            self.assertNotEqual(code, 0, out)
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_the_link_sign_in_says_it_trusted_the_folder(self):
        # a sign-in by link silently marked the folder as trusted in the background; a browser sign-in says so. A trust
        # mark is not enough (2026-10-17, claude 2.1.302: "awaiting approval") — the server being signed in to must also be approved for the folder
        work, cwd, conf = tempfile.mkdtemp(), tempfile.mkdtemp(), tempfile.mkdtemp()
        os.mkdir(os.path.join(cwd, ".git"))   # its own key: a .git above the temp folder would key it by that root
        with open(os.path.join(conf, ".claude.json"), "w") as f:
            json.dump({"projects": {}}, f)
        with open(os.path.join(cwd, ".mcp.json"), "w") as f:
            json.dump({"mcpServers": {"pay": {"type": "http", "url": "https://pay.example.com/mcp"}}}, f)
        said = []
        old = dict(os.environ)
        os.environ.update({**self.env, "CLAUDE_CONFIG_DIR": conf, "FAKE_APPROVAL": "1"})
        try:
            url, why = mcplogin.start("pay", cwd, work, say=said.append)
            self.assertTrue(url, why)
            code, out = mcplogin.finish(work, "http://localhost:5/callback?code=abc")
            self.assertEqual(code, 0, out)
        finally:
            os.environ.clear()
            os.environ.update(old)
        self.assertEqual(said, [mcplogin.approved({"trust", "server"}, "pay")])
        self.assertIn("pay", said[0])
        with open(os.path.join(conf, ".claude.json")) as f:
            self.assertEqual(json.load(f)["projects"][os.path.realpath(cwd).replace(os.sep, "/")],
                             {"hasTrustDialogAccepted": True, "enabledMcpjsonServers": ["pay"]})

    def test_command_on_a_hub_without_a_browser(self):
        state, work = tempfile.mkdtemp(prefix="st-"), tempfile.mkdtemp(prefix="w-")
        from core import journal
        j = journal.Journal(os.path.join(state, "journal.jsonl"))
        j.append("session.requested", "maps", {"cwd": work, "session_id": "s", **OWNER})
        os.makedirs(os.path.join(state, "sessions", "maps"))
        with open(os.path.join(state, "sessions", "maps", "mcp.json"), "w") as f:
            json.dump({"mcpServers": {"data": {"type": "http", "url": "https://data.example.com/mcp"}}}, f)
        env = {**self.env, "HUB_STATE_DIR": state, "HUB_CLI_WAIT": "0"}
        r = subprocess.run([sys.executable, HUB, "mcp", "login", "maps", "data", "--no-browser"], env=env, capture_output=True,
                           text=True, timeout=90)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("https://mcp.example.com/authorize?", r.stdout)
        self.assertIn("hub mcp login maps data --redirect", r.stdout)
        call = self.calls()[0]
        self.assertIn('"data"', call["mcp"])                          # from its own folder with the server description
        self.assertNotEqual(os.path.realpath(call["cwd"]), os.path.realpath(work))
        self.assertFalse(os.path.isdir(os.path.join(state, "spool")) and os.listdir(os.path.join(state, "spool")))
        r = subprocess.run([sys.executable, HUB, "mcp", "login", "maps", "data", "--redirect",
                            "http://localhost:5/callback?code=abc&state=s"], env=env, capture_output=True, text=True,
                           timeout=90)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        spooled = []
        for n in sorted(os.listdir(os.path.join(state, "spool"))):
            with open(os.path.join(state, "spool", n)) as f:
                spooled.append(json.load(f))
        self.assertEqual([(e["type"], e["entity"], e["data"]["server"]) for e in spooled],
                         [("mcp.logged_in", "maps", "data")])


if __name__ == "__main__":
    unittest.main()
