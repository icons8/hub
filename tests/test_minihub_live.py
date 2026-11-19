"""The edit ban for a mini-hub (core/minihub.deny) on a real claude: in bypassPermissions, Write and Edit inside its
folder are refused and the file does not change; outside the folder it writes. The shape of the rule depends on the
Claude Code version (Write(...) without Edit(...) did not hold), so the check runs a live claude against a fake API:
no network, no login, an empty HOME, a made-up key. No claude on the machine — skipped (HUB_TEST_CLAUDE sets its path)."""
import tests  # noqa: F401 — hide the machine's settings
import http.server
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest

from core import minihub
from hosts import common
from tests.fakes.realclaude import real_claude


class ToolApi:
    """Fake API: the first request with tools gets a call of the tool `tool` with `input`, then "ok".
    `result` is the first tool_result that claude returned to the model."""

    def __init__(self, tool, input):
        api = self
        self.result = None

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                if not self.path.startswith("/v1/messages") or self.path.startswith("/v1/messages/"):
                    data = b'{"input_tokens": 1}'   # count_tokens and the like are not a model turn
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                body = json.loads(raw or b"{}")
                got = [c for m in body.get("messages") or [] if isinstance(m.get("content"), list)
                       for c in m["content"] if c.get("type") == "tool_result"]
                if got and api.result is None:
                    api.result = got[0]
                call = body.get("tools") and not got
                block = {"type": "tool_use", "id": "toolu_1", "name": tool, "input": {}} if call else \
                    {"type": "text", "text": ""}
                delta = {"type": "input_json_delta", "partial_json": json.dumps(input)} if call else \
                    {"type": "text_delta", "text": "ok"}

                def ev(name, d):
                    return f"event: {name}\ndata: {json.dumps(d)}\n\n"
                data = "".join([
                    ev("message_start", {"type": "message_start", "message": {
                        "id": "msg_1", "type": "message", "role": "assistant", "model": body.get("model"),
                        "content": [], "stop_reason": None, "usage": {"input_tokens": 1, "output_tokens": 1}}}),
                    ev("content_block_start", {"type": "content_block_start", "index": 0, "content_block": block}),
                    ev("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta}),
                    ev("content_block_stop", {"type": "content_block_stop", "index": 0}),
                    ev("message_delta", {"type": "message_delta",
                                         "delta": {"stop_reason": "tool_use" if call else "end_turn"},
                                         "usage": {"output_tokens": 1}}),
                    ev("message_stop", {"type": "message_stop"})]).encode()
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.send_response(404)
                self.send_header("content-length", "0")
                self.end_headers()
            do_HEAD = do_GET

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@unittest.skipUnless(real_claude(), "there is no claude on this machine — the live check of the mini-hub ban does not run")
class LiveDeny(unittest.TestCase):
    def setUp(self):
        self.claude = real_claude()
        self.home = tempfile.mkdtemp(prefix="mh-live-")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.work = os.path.join(self.home, "work")   # the mini-hub's folder
        self.state = os.path.join(self.home, "hub")   # the hub's folder — outside the mini-hub's folder, as in a live hub
        os.makedirs(os.path.join(self.work, "notes"))
        os.makedirs(self.state)

    def run_tool(self, tool, input, hook=False):
        """hook — the way the hub launches a mini-hub: its settings file (the refusal hook on every Bash call) and the
        denials in the environment, which the hook reads."""
        api = ToolApi(tool, input)
        self.addCleanup(api.stop)
        dead = "http://127.0.0.1:9"   # anything not going to the fake API goes nowhere
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.home, "USERPROFILE": self.home,
               "CLAUDE_CONFIG_DIR": os.path.join(self.home, ".claude"), "ANTHROPIC_BASE_URL": api.url,
               "ANTHROPIC_API_KEY": "sk-test-0123456789", "HTTPS_PROXY": dead, "HTTP_PROXY": dead,
               "https_proxy": dead, "http_proxy": dead, "NO_PROXY": "127.0.0.1"}
        deny = minihub.deny(self.work, keep=(self.state,))
        cmd = [self.claude, "-p", "go", "--permission-mode", "bypassPermissions", "--setting-sources",
               "project,local", "--max-turns", "3"]
        if hook:
            settings = common.write_hook_settings(os.path.join(self.home, "hooks.json"))
            cmd += ["--settings", settings]
            env["HUB_NESTED_DENY"] = json.dumps(deny)
            env["HUB_SESSION"] = "maps"
            env["HUB_STATE_DIR"] = self.state
        cmd += ["--disallowedTools", *deny]
        r = subprocess.run(cmd, cwd=self.work, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120)
        self.assertIsNotNone(api.result, f"claude did not return the tool result: {r.stderr[-500:]} {r.stdout[-500:]}")
        # 10-21: Claude Code does not match Write(path) and warns on every launch "is not matched by file permission
        # checks" — a rule that holds nothing
        self.assertNotIn("not matched", r.stderr + r.stdout)
        return api.result

    def test_write_in_own_folder_is_denied(self):
        path = os.path.join(self.work, "notes", "PLAN.md")
        r = self.run_tool("Write", {"file_path": path, "content": "plan\n"})
        self.assertTrue(r.get("is_error"), r)
        self.assertIn("denied", json.dumps(r))
        self.assertFalse(os.path.exists(path))

    def test_edit_in_own_folder_is_denied(self):
        path = os.path.join(self.work, "notes", "PLAN.md")
        with open(path, "w") as f:
            f.write("a\n")
        r = self.run_tool("Edit", {"file_path": path, "old_string": "a", "new_string": "b"})
        self.assertTrue(r.get("is_error"), r)
        self.assertIn("denied", json.dumps(r))   # an actual ban, not "file not read yet"
        with open(path) as f:
            self.assertEqual(f.read(), "a\n")

    def test_shell_write_in_own_folder_is_denied_with_the_way_that_works(self):
        """Bash writes are refused by the same Edit rule (sed -i, >, tee): with the hub's hook the refusal names the
        executor instead of a bare "denied" — the agents were left to look for a way around it. The file stays."""
        path = os.path.join(self.work, "notes", "PLAN.md")
        with open(path, "w") as f:
            f.write("a\n")
        for command in (f"sed -i 's/a/b/' {path}", f"echo b > {path}", f"echo b | tee {path}"):
            r = self.run_tool("Bash", {"command": command}, hook=True)
            self.assertTrue(r.get("is_error"), r)
            self.assertIn("hub task run", json.dumps(r), command)
            self.assertIn("executor", json.dumps(r), command)
            with open(path) as f:
                self.assertEqual(f.read(), "a\n", command)
        outside = os.path.join(self.state, "note.md")   # a write elsewhere is none of the hook's business
        r = self.run_tool("Bash", {"command": f"echo x > {outside}"}, hook=True)
        self.assertFalse(r.get("is_error"), r)
        self.assertTrue(os.path.exists(outside))

    def test_write_outside_own_folder_works(self):
        path = os.path.join(self.state, "note.md")
        r = self.run_tool("Write", {"file_path": path, "content": "x\n"})
        self.assertFalse(r.get("is_error"), r)
        self.assertTrue(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
