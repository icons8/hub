"""A sign-in from the page with a real claude (core/mcpwatch, hosts/streamjson): the host's control requests against a
stand-in OAuth MCP server (tests/fakes/fake_oauth_mcp.py). No network and no sign-in: HOME and the Claude Code settings
folder are temporary, the API address is dead, the "owner's browser" is an HTTP client that follows the link. Both ways
back: the browser reaches Claude Code's own listener (the hub's machine), and the address from the browser bar of
another device goes in by mcp_oauth_callback_url plus mcp_reconnect. No claude on the machine — skipped (HUB_TEST_CLAUDE
sets the path)."""
import tests  # noqa: F401 — do not see the machine's settings
import http.client
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from urllib.parse import urlparse

import platforms
from core import mcplogin
from hosts.streamjson import StreamHost, StreamProcess
from tests.fakes.fake_oauth_mcp import OAuthMcp
from tests.fakes.realclaude import real_claude

CLAUDE = real_claude()


def get(url):
    """One GET without following redirects -> (status, Location)."""
    u = urlparse(url)
    c = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
    c.request("GET", u.path + ("?" + u.query if u.query else ""))
    r = c.getresponse()
    r.read()
    c.close()
    return r.status, r.getheader("Location")


@unittest.skipUnless(CLAUDE, "needs claude (HUB_TEST_CLAUDE)")
class Live(unittest.TestCase):
    def setUp(self):
        self.mcp = OAuthMcp()
        self.addCleanup(self.mcp.stop)
        self.dir = tempfile.mkdtemp(prefix="mcp-live-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        home, work = os.path.join(self.dir, "home"), os.path.join(self.dir, "work")
        os.makedirs(os.path.join(home, ".claude"))
        os.makedirs(work)
        cfg = os.path.join(self.dir, "mcp.json")
        with open(cfg, "w") as f:
            json.dump({"mcpServers": {"docs": {"type": "http", "url": self.mcp.url}}}, f)
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "TMPDIR")}
        env.update(HOME=home, CLAUDE_CONFIG_DIR=os.path.join(home, ".claude"), ANTHROPIC_BASE_URL="http://127.0.0.1:9",
                   ANTHROPIC_API_KEY="test-key-not-real", DISABLE_AUTOUPDATER="1", DISABLE_TELEMETRY="1",
                   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", NO_PROXY="127.0.0.1,localhost",
                   XDG_CONFIG_HOME=os.path.join(home, ".config"), XDG_DATA_HOME=os.path.join(home, ".local", "share"))
        self.popen = subprocess.Popen(
            [CLAUDE, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
             "--setting-sources", "project,local", "--permission-prompt-tool", "stdio",
             "--permission-mode", "bypassPermissions", "--strict-mcp-config", "--mcp-config", cfg],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1, cwd=work,
            env=env, start_new_session=True)
        self.addCleanup(self.stop)
        self.state = os.path.join(self.dir, "state")
        os.makedirs(self.state)
        self.host = StreamHost(self.state)
        self.host.procs["a"] = StreamProcess(self.popen)
        threading.Thread(target=self.host.reader("a", self.popen), daemon=True).start()

    def stop(self):
        try:
            platforms.current.kill_detached(self.popen)   # its group / tree (Windows has no killpg)
            self.popen.wait(10)
        except (OSError, subprocess.TimeoutExpired):
            platforms.current.kill_detached(self.popen, hard=True)

    def fact(self, step, secs=30, again=None):
        """The next session.mcp_login fact of this step; again() is repeated each half second while waiting (polling)."""
        end = time.time() + secs
        while time.time() < end:
            if again:
                again()
            for f in self.host.poll():
                if f[0] == "session.mcp_login" and f[2].get("step") == step:
                    return f[2]
            time.sleep(0.5)
        self.fail(f"no step {step} within {secs} s")

    def link(self):
        for _ in range(30):   # the server is still connecting right after the start: the core asks again (mcpwatch.PENDING)
            self.host.mcp_login("a", server="docs", attempt="l1")
            got = self.fact("auth")
            if got.get("status") != "pending":
                break
            time.sleep(1)
        self.assertTrue(got["ok"], got)
        self.assertNotIn("http", json.dumps(got))       # the link is in the owner's file, not in the fact
        url = mcplogin.link_of(self.state, "l1")
        status, back = get(url)                         # the owner signs in: the server sends the browser back
        self.assertEqual(status, 302)
        return back

    def connected(self):
        return self.fact("status", again=lambda: self.host.mcp_login_poll("a", server="docs", attempt="l1"))

    def test_the_browser_on_the_hubs_machine_comes_back_to_claude_by_itself(self):
        back = self.link()
        self.assertTrue(back.startswith("http://localhost:"), back)
        get(back)                                       # Claude Code's own listener takes the code
        self.assertEqual(self.connected()["status"], "connected")
        self.assertIn(("POST", "/token"), self.mcp.seen)

    def test_the_address_from_another_device_goes_in_by_the_request_and_a_reconnect(self):
        back = self.link()
        self.host.mcp_callback("a", server="docs", attempt="l1", url=back)
        got = self.fact("callback")
        self.assertTrue(got["ok"], got)
        self.assertNotIn("code=", json.dumps(got))
        self.host.mcp_reconnect("a", server="docs")     # the core's next action (core/mcpwatch.login_step)
        self.assertEqual(self.connected()["status"], "connected")

    def test_a_connected_server_gets_no_second_sign_in(self):
        get(self.link())
        self.connected()
        self.host.mcp_login("a", server="docs", attempt="l2")
        got = self.fact("auth")
        self.assertEqual((got["ok"], got.get("status")), (False, "connected"))


if __name__ == "__main__":
    unittest.main()
