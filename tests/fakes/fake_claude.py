#!/usr/bin/env python3
"""A fake `claude` for host tests: a terminal interface and hooks, with no model and no tokens.

It understands the same flags the core uses (--session-id/--resume, -n, --settings), calls the hooks from --settings
with the same JSON on input, and writes the registry `<FAKE_REGISTRY_DIR>/<pid>.json` with status idle/busy/waiting.
Behavior that reproduces known failures:
- a turn lasts FAKE_TURN_S seconds; input during a turn piles up in the input line, like the real one;
- `/dialog` — a turn with an AskUserQuestion window: while the window is open, everything typed is lost (failure 4); `1` closes it;
- `/reload-plugins` — a local command with no turn; the next Enter is lost (failure 2);
- a paste longer than FAKE_COLLAPSE_LINES lines is shown as "[Pasted text #N +K lines]", like the real one;
- `/exit` — SessionEnd(prompt_input_exit); SIGTERM/SIGHUP — SessionEnd(other).
"""
import json
import os
import select
import signal
import subprocess
import sys
import termios
import time
import tty

PASTE_ON, PASTE_OFF = b"\x1b[200~", b"\x1b[201~"


class Fake:
    def __init__(self, argv):
        self.sid, self.name, self.settings, self.source = None, None, {}, "startup"
        it = iter(argv)
        for a in it:
            if a == "--session-id":
                self.sid = next(it)
            elif a == "--resume":
                self.sid, self.source = next(it), "resume"
            elif a == "-n":
                self.name = next(it)
            elif a == "--settings":
                with open(next(it)) as f:
                    self.settings = json.load(f)
        self.turn_s = float(os.environ.get("FAKE_TURN_S", "0.5"))
        self.registry = os.environ.get("FAKE_REGISTRY_DIR")
        self.buf, self.shown, self.status, self.busy_until = "", "", "idle", 0.0
        self.collapse = int(os.environ.get("FAKE_COLLAPSE_LINES", "10"))
        self.pastes = 0
        self.dialog, self.drop_enter, self.pending = False, False, []

    # --- external -------------------------------------------------------------------------------------------
    def hook(self, event, **data):
        payload = json.dumps({"hook_event_name": event, "session_id": self.sid, "cwd": os.getcwd(), **data})
        for group in self.settings.get("hooks", {}).get(event, []):
            for h in group.get("hooks", []):
                # like Claude Code: stdin is UTF-8; with `args` — exec form without a shell (hosts/common.hook_command)
                cmd = [h["command"], *h["args"]] if "args" in h else h["command"]
                subprocess.run(cmd, shell="args" not in h, input=payload.encode("utf-8"), timeout=10)

    def set_status(self, status):
        self.status = status
        if self.registry:
            os.makedirs(self.registry, exist_ok=True)
            path = os.path.join(self.registry, f"{os.getpid()}.json")
            rec = {"pid": os.getpid(), "sessionId": self.sid, "name": self.name, "status": status,
                   "messagingSocketPath": f"/tmp/fake-cc/{self.sid}.sock"}
            if status == "waiting":
                rec["waitingFor"] = "input needed"
            with open(path + ".tmp", "w") as f:
                json.dump(rec, f)
            os.replace(path + ".tmp", path)

    def out(self, s):
        os.write(1, s.encode())

    def draw(self):
        self.out("\r\x1b[K> " + self.shown.replace("\n", "⏎"))

    # --- logic -------------------------------------------------------------------------------------------
    def submit(self, text):
        self.out(f"\r\x1b[K  USER: {text}\r\n")
        if text == "/reload-plugins":
            self.out("  (plugins reloaded)\r\n")
            self.drop_enter = True
            return
        if text == "/exit":
            self.hook("SessionEnd", reason="prompt_input_exit")
            self.quit()
        self.hook("UserPromptSubmit", prompt=text)
        self.set_status("busy")
        if text.startswith("/dialog"):
            self.dialog = True
            self.set_status("waiting")
            self.out("  DIALOG: choose 1\r\n")
        else:
            self.busy_until = time.time() + self.turn_s

    def end_turn(self):
        self.out("  ASSISTANT: ok\r\n")
        self.hook("Stop")
        self.set_status("idle")
        if self.pending:
            self.submit(self.pending.pop(0))

    def key(self, data):
        if self.dialog:
            if b"1" in data and PASTE_ON not in data:
                self.dialog = False
                self.set_status("busy")
                self.out("  DIALOG closed\r\n")
                self.busy_until = time.time() + self.turn_s
            return  # the window swallows everything else
        i = 0
        while i < len(data):
            if data.startswith(PASTE_ON, i):
                end = data.find(PASTE_OFF, i)
                end = len(data) if end < 0 else end
                # tmux, like a terminal, pastes a newline as CR; the TUI inside a paste reads it as \n
                chunk = data[i + len(PASTE_ON):end].decode(errors="replace").replace("\r", "\n")
                self.buf += chunk
                lines = chunk.count("\n") + 1
                self.pastes += 1
                self.shown += f"[Pasted text #{self.pastes} +{lines} lines]" if lines > self.collapse else chunk
                i = end + len(PASTE_OFF)
                continue
            ch = data[i:i + 1]
            i += 1
            if ch == b"\r":
                if self.drop_enter:
                    self.drop_enter = False  # the TUI was redrawing — Enter lost
                    continue
                text, self.buf, self.shown = self.buf, "", ""
                if not text:
                    continue
                if self.status == "idle":
                    self.submit(text)
                else:
                    self.pending.append(text)
            elif ch == b"\x15":  # Ctrl+U
                self.buf = self.shown = ""
            elif ch == b"\x7f":
                self.buf, self.shown = self.buf[:-1], self.shown[:-1]
            elif ch >= b" ":
                self.buf += ch.decode(errors="replace")
                self.shown += ch.decode(errors="replace")
        self.draw()

    def quit(self, *_):
        if self.registry:
            try:
                os.unlink(os.path.join(self.registry, f"{os.getpid()}.json"))
            except OSError:
                pass
        sys.exit(0)

    def run(self):
        def ended(*_):
            self.hook("SessionEnd", reason="other")
            self.quit()
        signal.signal(signal.SIGTERM, ended)
        signal.signal(signal.SIGHUP, ended)
        old = termios.tcgetattr(0)
        tty.setraw(0)
        try:
            self.out("\x1b[?2004h")  # enable bracketed paste, like the real TUI
            self.out(f"fake claude {self.name} {self.sid} ({self.source})\r\n")
            self.hook("SessionStart", source=self.source)
            self.set_status("idle")
            self.draw()
            while True:
                timeout = max(0.0, self.busy_until - time.time()) if self.busy_until else None
                ready, _, _ = select.select([0], [], [], timeout)
                if ready:
                    data = os.read(0, 65536)
                    if not data:
                        ended()
                    self.key(data)
                if self.busy_until and time.time() >= self.busy_until:
                    self.busy_until = 0.0
                    self.end_turn()
                    self.draw()
        finally:
            termios.tcsetattr(0, termios.TCSADRAIN, old)


if __name__ == "__main__":
    Fake(sys.argv[1:]).run()
