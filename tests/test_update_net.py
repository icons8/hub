"""The update's trip to the shared repository (modules/update Updater.git with net=True): it runs in the background,
so it must not wait for a person — no password or key prompt, no sign-in window of the credential helper, no controlling
terminal. Without access the check ends with an error at once, not after the timeout on every pass."""
import tests  # noqa: F401 — keep the machine's settings out
import http.server
import os
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

import platforms


class Denied(http.server.BaseHTTPRequestHandler):
    """A shared repository that wants a login: every request is 401 (as GitHub for a private repository)."""

    def do_GET(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="repo"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_POST = do_GET

    def log_message(self, *a):
        pass


class Net(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="update-net-")
        self.state = os.path.join(self.root, "state")
        os.makedirs(os.path.join(self.state, "spool"))
        self.up = os.path.join(self.root, "up.git")
        self.g(self.root, "init", "-q", "--bare", "-b", "main", self.up)
        author = os.path.join(self.root, "author")
        self.g(self.root, "clone", "-q", self.up, author)
        with open(os.path.join(author, "core.py"), "w") as f:
            f.write("a = 1\n")
        self.g(author, "add", "-A")
        self.g(author, "commit", "-qm", "start")
        self.g(author, "push", "-q", "origin", "main")
        self.mine = os.path.join(self.root, "mine")
        self.g(self.root, "clone", "-q", self.up, self.mine)

    def g(self, cwd, *a):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True,
                              capture_output=True, text=True).stdout.strip()

    def updater(self):
        from modules.update import update
        up = {"enabled": True, "remote": "origin", "branch": "main"}
        return update.Updater(self.mine, self.state, {"update": up, "dev": {"name": "hub-dev"}},
                              tests=lambda repo: (True, "OK"))

    def server(self):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Denied)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.g(self.mine, "remote", "set-url", "origin", f"http://127.0.0.1:{srv.server_address[1]}/hub.git")

    def helper(self, body):
        """A credential helper as the Git Credential Manager: `get` goes into the repository's own settings."""
        path = os.path.join(self.root, "helper.sh")
        with open(path, "w") as f:
            f.write("#!/bin/sh\n[ \"$1\" = get ] || exit 0\ncat > /dev/null\n" + body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        self.g(self.mine, "config", "credential.helper", f"!{path}")
        return path

    def test_the_fetch_runs_quiet_and_without_a_terminal(self):
        seen, real = [], platforms.current.run

        def spy(args, *a, **kw):
            if args[:1] == ["git"] and "fetch" in args:
                seen.append(((kw.get("env") or {}).get("GCM_INTERACTIVE"), (kw.get("env") or {}).get("GIT_TERMINAL_PROMPT"),
                             kw.get("start_new_session"), kw.get("stdin"), "core.askPass=" in args))
            return real(args, *a, **kw)
        with mock.patch.dict(os.environ, {"GCM_INTERACTIVE": "always"}), mock.patch.object(platforms.current, "run", spy):
            self.assertEqual(self.updater().check(apply=False), "current")   # nothing new in the shared repo: the fetch passed
        want = ("never", "0", platforms.current.no_terminal().get("start_new_session"), subprocess.DEVNULL, True)
        self.assertEqual(seen, [want, want])   # the branch and the release tags: both trips

    @unittest.skipIf(os.name == "nt", "the login helper is an sh script")
    def test_a_sign_in_window_of_the_helper_is_not_waited_for(self):
        """Without a sign-in the Git Credential Manager (Windows) opens a window and waits for a person: with
        GCM_INTERACTIVE=never it answers "no" at once, and the check ends with git's error, not a timeout."""
        self.server()
        log = os.path.join(self.root, "helper.log")
        self.helper(f'echo "GCM_INTERACTIVE=${{GCM_INTERACTIVE:-unset}}" >> {log}\n'
                    '[ "$GCM_INTERACTIVE" = never ] && exit 1\nexec sleep 60\n')
        began = time.time()
        with self.assertRaises(RuntimeError) as got:
            self.updater().check(apply=False)
        self.assertLess(time.time() - began, 30)
        self.assertIn("git fetch", str(got.exception))
        self.assertNotIn("no answer within", str(got.exception))
        with open(log) as f:
            self.assertEqual(set(f.read().split()), {"GCM_INTERACTIVE=never"})

    @unittest.skipIf(os.name == "nt", "the login helper is an sh script")
    def test_a_silent_hang_ends_with_a_readable_error(self):
        """Whatever hangs git (a dead network, a helper that ignores the variable), the update says in words that
        there was no answer — not Python's "Command [...] timed out" — and does not leave git running."""
        from modules.update import update
        self.server()
        marker = os.path.join(self.root, "alive")
        self.helper(f"echo $$ > {marker}\nexec sleep 60\n")
        began = time.time()
        with mock.patch.object(update, "NET_WAIT", 2), self.assertRaises(RuntimeError) as got:
            self.updater().check(apply=False)
        self.assertLess(time.time() - began, 30)
        self.assertIn("no answer within 2 s", str(got.exception))
        self.assertNotIn("Command", str(got.exception))
        with open(marker) as f:
            pid = int(f.read())
        deadline = time.time() + 5
        while platforms.current.alive(pid) and time.time() < deadline:
            time.sleep(0.1)
        self.assertFalse(platforms.current.alive(pid))   # the helper was in git's group: it went with it


if __name__ == "__main__":
    unittest.main()
