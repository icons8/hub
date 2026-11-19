"""Direction boards (the hub used to start `backlog browser` by hand). Since 16.10 the hub no longer starts its own
board servers (a direction's board lives on the hub page, the Backlog.md utility is not called); the module stays to
show and stop boards started by earlier versions.

Such a board has a fixed port (direction.json: board_port, from 6420) and a pid in board.pid in the direction's folder.
`stop` stops it. Running boards are in <state>/boards.json ({port: slug}): the tailnet module uses it to forward the
ports on the host's tailnet IP (to the owner only).
"""
import json
import os
import signal
import socket
import time

import platforms
from modules.directions import direction


class BoardError(RuntimeError):
    pass


def listening(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
        return True
    except OSError:
        return False


def pid_alive(pid):
    # not os.kill(pid, 0): on Windows that sends Ctrl+C to the whole console
    return bool(pid) and platforms.current.alive(pid)


def is_board(pid, port):
    """The process under this number is a board of an earlier version: `backlog browser -p <port>` (the node wrapper
    started it that way), the leader of its own group and not the caller's group. board.pid lives in the direction's
    folder and outlives the board: by now its number may be another program's (the audit of 2026-11-03). Windows: the
    earlier versions started no boards there (no process groups)."""
    if not platforms.real_pid(pid) or not port or not hasattr(os, "killpg"):
        return False
    try:
        if os.getpgid(pid) != pid or pid == os.getpgrp():
            return False
    except OSError:
        return False
    return f" browser -p {port} " in f" {platforms.current.command_line(pid)} "


class Boards:
    def __init__(self, root=None, state_dir=None):
        from core.daemon import default_state_dir
        self.root = root or direction.root()
        self.state_dir = state_dir or default_state_dir()

    def path(self, slug):
        p = os.path.join(self.root, slug)
        if not os.path.isdir(os.path.join(p, "backlog")):
            from core import i18n   # `hub direction board` prints it: the process's language
            raise BoardError(i18n.t("directions.board_no_direction", slug=slug))
        return p

    def slugs(self):
        try:
            return sorted(n for n in os.listdir(self.root)
                          if not n.startswith("_") and os.path.isdir(os.path.join(self.root, n, "backlog")))
        except FileNotFoundError:
            return []

    def port(self, slug):
        """Board port recorded by an earlier hub version (direction.json: board_port), or None: there was no board."""
        meta = direction.load_meta(self.path(slug))
        return int(meta["board_port"]) if meta.get("board_port") else None

    def pid(self, slug):
        try:
            with open(os.path.join(self.path(slug), "board.pid")) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None

    def info(self, slug):
        port, pid = self.port(slug), self.pid(slug)
        return {"slug": slug, "port": port, "pid": pid, "url": f"http://127.0.0.1:{port}/"}

    def alive(self, slug):
        pid = self.pid(slug)
        port = self.port(slug)
        return bool(pid and port and pid_alive(pid) and listening(port))

    def stop(self, slug):
        pid = self.pid(slug)
        if pid_alive(pid) and is_board(pid, self.port(slug)):
            try:
                os.killpg(pid, signal.SIGTERM)  # a board of an earlier version is a node wrapper around the binary: kill the whole group
            except OSError:
                pass
            for _ in range(30):
                if not pid_alive(pid):
                    break
                time.sleep(0.1)
        try:
            os.remove(os.path.join(self.path(slug), "board.pid"))
        except (OSError, BoardError):
            pass
        self.publish()

    def running(self):
        return [self.info(s) for s in self.slugs() if self.alive(s)]

    def publish(self):
        """boards.json: running boards for the tailnet module (forwarded on the host's tailnet IP)."""
        data = {str(b["port"]): b["slug"] for b in self.running()}
        path = os.path.join(self.state_dir, "boards.json")
        with open(path + ".tmp", "w") as f:
            json.dump(data, f)
        os.replace(path + ".tmp", path)
