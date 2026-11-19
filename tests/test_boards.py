"""Direction boards of earlier versions (owner decision 2026-09-30: support boards properly in the directions module;
the hub used to start `backlog browser` by hand). Since 16.10 the hub starts no boards and does not call the utility:
the module shows and stops the ones left over from earlier versions; the tailnet module forwards their port to the Mac's
tailnet IP (TCP) and admits only the owner (whois)."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest import mock

from modules.directions import boards
from modules.tailnet import forward

SERVER = """
import http.server, os, sys
port = int(sys.argv[3])   # argv as the board's: browser -p <port> --no-open
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"board"
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
"""


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@unittest.skipIf(os.name == "nt", "board of the previous version: process group (killpg), macOS and Linux only")
class Boards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="boards-")
        self.root = os.path.join(self.tmp, "directions")
        self.procs = []
        for slug in ("sso", "mail"):
            os.makedirs(os.path.join(self.root, slug, "backlog"))
            json.dump({"repo": "/x"}, open(os.path.join(self.root, slug, "direction.json"), "w"))
        self.b = boards.Boards(self.root, state_dir=self.tmp)

    def tearDown(self):
        for slug in ("sso", "mail"):
            self.b.stop(slug)
        for p in self.procs:
            p.kill()
            p.wait()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def leftover(self, slug):
        """A board left over from an earlier version: its own server in its own process group, pid and port recorded."""
        port = free_port()
        proc = subprocess.Popen([sys.executable, "-c", SERVER, "browser", "-p", str(port), "--no-open"],
                                start_new_session=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(proc)
        for _ in range(100):
            if boards.listening(port):
                break
            time.sleep(0.1)
        path = os.path.join(self.root, slug)
        with open(os.path.join(path, "board.pid"), "w") as f:
            f.write(str(proc.pid))
        json.dump({"repo": "/x", "board_port": port}, open(os.path.join(path, "direction.json"), "w"))
        return port

    def get(self, port):
        return urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read().decode()

    def test_leftover_boards_are_listed_and_published(self):
        a, m = self.leftover("sso"), self.leftover("mail")
        self.assertEqual(sorted(x["slug"] for x in self.b.running()), ["mail", "sso"])
        self.b.publish()
        self.assertEqual(json.load(open(os.path.join(self.tmp, "boards.json"))), {str(a): "sso", str(m): "mail"})

    def test_stop_ends_the_old_board(self):
        a = self.leftover("sso")
        self.assertEqual(self.get(a), "board")
        self.b.stop("sso")
        self.assertEqual(self.b.running(), [])
        with self.assertRaises(OSError):
            self.get(a)
        self.assertFalse(os.path.exists(os.path.join(self.root, "sso", "board.pid")))

    def test_stop_leaves_a_program_that_got_the_number(self):
        # the audit of 03.11: board.pid outlives the board (the folder is copied, the machine rebooted); its number went
        # to another program, a group leader — stop killed its whole group
        port = self.leftover("sso")
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        self.procs.append(other)
        with open(os.path.join(self.root, "sso", "board.pid"), "w") as f:
            f.write(str(other.pid))
        self.assertTrue(self.b.alive("sso"))   # alive and the port listened: what rename sees before stop
        self.b.stop("sso")   # a signal sent, stop waits until the process is gone: no pause needed to see it
        self.assertIsNone(other.poll())
        self.assertFalse(os.path.exists(os.path.join(self.root, "sso", "board.pid")))
        self.assertEqual(self.get(port), "board")   # not that board's process either: left as it is

    def test_stop_never_signals_its_own_group(self):
        port = self.leftover("sso")
        with open(os.path.join(self.root, "sso", "board.pid"), "w") as f:
            f.write(str(os.getpgrp()))
        line = f"node /usr/local/bin/backlog browser -p {port} --no-open"
        with mock.patch.object(boards.platforms.current, "command_line", return_value=line), \
                mock.patch.object(boards.os, "killpg") as killpg:
            self.b.stop("sso")
        killpg.assert_not_called()

    def test_no_board_no_server_is_started(self):
        self.assertEqual(self.b.running(), [])
        self.assertFalse(hasattr(self.b, "start"))   # the hub starts no boards (16.10)
        self.assertFalse(self.b.alive("sso"))
        self.assertNotIn("board_port", json.load(open(os.path.join(self.root, "sso", "direction.json"))))

    def test_unknown_direction(self):
        with self.assertRaises(boards.BoardError):
            self.b.port("nope")


class Forward(unittest.TestCase):
    def test_only_owner_passes(self):
        target = free_port()
        srv = subprocess.Popen([sys.executable, "-m", "http.server", str(target), "--bind", "127.0.0.1"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=tempfile.mkdtemp())
        try:
            for _ in range(50):
                try:
                    socket.create_connection(("127.0.0.1", target), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.1)
            listen = free_port()
            who = {"owner": True}
            stop = threading.Event()
            fw = forward.Forward("127.0.0.1", listen, target, allow=lambda peer: who["owner"], stop=stop)
            threading.Thread(target=fw.serve, daemon=True).start()
            time.sleep(0.3)
            self.assertEqual(urllib.request.urlopen(f"http://127.0.0.1:{listen}/", timeout=5).status, 200)
            who["owner"] = False
            with self.assertRaises(Exception):
                urllib.request.urlopen(f"http://127.0.0.1:{listen}/", timeout=3).read()
            stop.set()
        finally:
            srv.kill()


if __name__ == "__main__":
    unittest.main()
