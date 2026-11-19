#!/usr/bin/env python3
"""Fake `claude -p --input-format stream-json --output-format stream-json`: protocol and hooks, no model.

Repeats what the 2026-09-29 probe on the VM showed (docs/decisions.md): init, a turn per user message,
a message in the middle of a turn waits for the next turn, `control_request can_use_tool` for permissions and
AskUserQuestion, hooks SessionStart / UserPromptSubmit / Stop / SessionEnd(other when stdin closes). Stdlib only,
any OS. Commands in the message text: `/ask` — AskUserQuestion; `/write` — permission for Write (bypassPermissions
does not ask); `/secret` — permission for a command with a token (whether it ran as it was); `/crash` — the process
dies in the middle of a turn.
"""
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid

if sys.argv[1:3] == ["auth", "status"]:   # a new hub asks it before a spawn (hosts/streamjson signed_in)
    print(json.dumps({"loggedIn": True, "authMethod": "claude.ai"}))
    sys.exit(0)

if os.environ.get("FAKE_LINGER_TERM"):
    import signal   # a claude that does not die on SIGTERM (a stop terminates it): only kill or the linger file end it
    signal.signal(signal.SIGTERM, signal.SIG_IGN)


class Fake:
    def __init__(self, argv):
        argv_dir = os.environ.get("FAKE_ARGV_DIR")
        if argv_dir:  # tests check which flags the core started the session with
            os.makedirs(argv_dir, exist_ok=True)
            with open(os.path.join(argv_dir, f"{os.environ.get('HUB_SESSION')}-{time.time_ns()}.json"), "w") as f:
                json.dump(argv, f)
        self.sid, self.mode, self.settings, self.source = None, "default", {}, "startup"
        resumed, fork = None, False
        it = iter(argv)
        for a in it:
            if a == "--session-id":
                self.sid = next(it)
            elif a == "--resume":
                resumed, self.source = next(it), "resume"
            elif a == "--fork-session":
                fork = True
            elif a == "--permission-mode":
                self.mode = next(it)
            elif a == "--settings":
                with open(next(it)) as f:
                    self.settings = json.load(f)
        self.hook_events = "--include-hook-events" in argv
        self.replays = "--replay-user-messages" in argv
        # FAKE_HOOKS_LOST=SessionStart,UserPromptSubmit: those hooks are cancelled by their timeout (a loaded machine:
        # the interpreter starts longer than the timeout), they write nothing; as with the real one, stdout says so
        self.lost = set(filter(None, os.environ.get("FAKE_HOOKS_LOST", "").split(",")))
        # FAKE_HOOKS_CUT=...: cancelled too, but after the hook wrote its fact (killed while the interpreter exits)
        self.cut = set(filter(None, os.environ.get("FAKE_HOOKS_CUT", "").split(",")))
        # FAKE_HOOKS_LATE=...: cancelled, and the hook outlives the timeout: it writes its fact after claude went on
        # (after the line it took), before the turn goes further — late() runs it
        self.outlive = set(filter(None, os.environ.get("FAKE_HOOKS_LATE", "").split(",")))
        self.outlived = []
        if resumed and fork:  # like the real --fork-session: a copy of the conversation under a new id (or its own --session-id)
            src, self.sid = resumed, self.sid or resumed + "-fork"
            if os.path.exists(self.history(src)):
                shutil.copy(self.history(src), self.history())
        elif resumed:
            self.sid = resumed
        self.turn_s = float(os.environ.get("FAKE_TURN_S", "0.3"))
        self.backlog = []
        # stdin is read by one thread into a queue: a long turn (/slow) waits for an interrupt with a timeout, and
        # select() works on sockets only on Windows (on a pipe it raised there at once: the "20 s turn" ended at its start)
        self.lines = queue.Queue()
        threading.Thread(target=self.pump, daemon=True).start()

    def pump(self):
        while True:
            line = sys.stdin.readline()
            self.lines.put(line)
            if not line:   # EOF
                return

    def out(self, obj):
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def hook(self, event, **data):
        payload = json.dumps({"hook_event_name": event, "session_id": self.sid, "cwd": os.getcwd(), **data})
        # like Claude Code 2.1.218-2.1.309: SessionStart hooks are always on stdout, the others with --include-hook-events
        told = event == "SessionStart" or (self.hook_events and event != "SessionEnd")
        name = f"{event}:{data['source']}" if event == "SessionStart" else event
        for group in self.settings.get("hooks", {}).get(event, []):
            for h in group.get("hooks", []):
                hid = str(uuid.uuid4())
                if told:
                    self.out({"type": "system", "subtype": "hook_started", "hook_id": hid, "hook_name": name,
                              "hook_event": event, "session_id": self.sid})
                # like Claude Code: stdin is UTF-8; with `args` — exec form without a shell (hosts/common.hook_command)
                cmd = [h["command"], *h["args"]] if "args" in h else h["command"]
                if event in self.outlive:
                    self.outlived.append((cmd, "args" not in h, payload))
                elif event not in self.lost:
                    subprocess.run(cmd, shell="args" not in h, input=payload.encode("utf-8"), timeout=10)
                if told:
                    cut = event in self.lost or event in self.cut or event in self.outlive
                    self.out({"type": "system", "subtype": "hook_response", "hook_id": hid, "hook_name": name,
                              "hook_event": event, "output": "", "stdout": "", "stderr": "", "exit_code": 1 if cut else 0,
                              "outcome": "cancelled" if cut else "success", "session_id": self.sid})

    def late(self):
        """The hooks that outlived their timeout write now: after what claude said meanwhile (the host reads a line at
        once, the hook's interpreter starts longer)."""
        while self.outlived:
            cmd, shell, payload = self.outlived.pop(0)
            subprocess.run(cmd, shell=shell, input=payload.encode("utf-8"), timeout=10)

    def read(self):
        line = self.lines.get()
        if not line:
            # FAKE_LINGER_FILE: stdin is closed, but it does not exit until the file appears (no longer than 30 s) —
            # like a real claude that got the next turn before EOF and is still running it
            linger, end = os.environ.get("FAKE_LINGER_FILE"), time.time() + 30
            while linger and not os.path.exists(linger) and time.time() < end:
                time.sleep(0.05)
            self.hook("SessionEnd", reason="other")
            sys.exit(0)
        return json.loads(line)

    def ask(self, tool, tool_input, interactive):
        """control_request and waiting for the reply; messages that arrive meanwhile go to the queue."""
        rid = str(uuid.uuid4())
        req = {"subtype": "can_use_tool", "tool_name": tool, "input": tool_input}
        if interactive:
            req["requires_user_interaction"] = True
        self.out({"type": "control_request", "request_id": rid, "request": req})
        while True:
            msg = self.read()
            if msg.get("type") == "control_response" and msg["response"].get("request_id") == rid:
                return msg["response"]["response"]
            self.backlog.append(msg)

    def turn(self, text):
        self.hook("UserPromptSubmit", prompt=text)
        if self.replays:   # --replay-user-messages: the line taken into the turn, after its hooks (as the real one)
            self.out({"type": "user", "message": {"role": "user", "content": text}, "session_id": self.sid,
                      "parent_tool_use_id": None, "isReplay": True})
        self.late()
        with open(self.history(), "a") as f:
            f.write(json.dumps({"user": text}, ensure_ascii=False) + "\n")
        time.sleep(self.turn_s)
        # core marker (core/reducer._typed): usually in front, "[hub:mN] text"; for a command it follows it, "/ask\n\n[hub:mN]"
        body = text.split("\n\n[hub:", 1)[0]
        body = body.split("] ", 1)[-1] if body.startswith("[hub:") else body
        if body == "/crash":
            os._exit(9)
        if body == "/work":  # like the real one: tool calls with results and a task list
            self.out({"type": "assistant", "message": {"content": [{"type": "text", "text": "Looking at the folder."}]}})
            self.out({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "TodoWrite",
                "input": {"todos": [{"content": "Look at the files", "status": "completed", "activeForm": "Looking at the files"},
                                    {"content": "Run the tests", "status": "in_progress", "activeForm": "Running the tests"},
                                    {"content": "Write the summary", "status": "pending", "activeForm": "Writing the summary"}]}}]}})
            self.out({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t2", "name": "Bash",
                                                                    "input": {"command": "ls -la /w"}}]}})
            self.out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t2",
                                                               "content": "a.py\nb.py\nc.py\nd.py\ne.py"}]}})
            self.out({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t3", "name": "Edit",
                "input": {"file_path": "/w/a.py", "old_string": "x = 1", "new_string": "x = 2"}}]}})
            self.out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t3", "is_error": True,
                                                               "content": "String not found"}]}})
        if body == "/slow":  # a long turn; control_request interrupt cuts it short, as with the real one
            end = time.time() + 20
            while time.time() < end:
                try:
                    line = self.lines.get(timeout=0.1)
                except queue.Empty:
                    continue
                msg = json.loads(line)   # EOF ("") fails the process, as before
                if msg.get("type") == "control_request" and msg["request"].get("subtype") == "interrupt":
                    self.out({"type": "control_response", "response": {"subtype": "success", "request_id": msg["request_id"]}})
                    self.out({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": ""})
                    self.hook("Stop")
                    return
                if msg.get("type") == "control_request" and msg["request"].get("subtype") == "mcp_set_servers":
                    # like a Claude Code that does not know the request: the core keeps the restart after the turn
                    self.out({"type": "control_response", "response": {
                        "subtype": "error", "request_id": msg["request_id"],
                        "error": "Unsupported control request subtype: mcp_set_servers"}})
                    continue
                self.backlog.append(msg)
        if body == "/progress":  # like a hub in the old role: a short reply first, then the work
            self.out({"type": "assistant", "message": {"content": [{"type": "text", "text": "on it, working"}]}})
            time.sleep(self.turn_s * 3)
        if body == "/ask":
            q = {"questions": [{"question": "Which option?", "options": [{"label": "A"}, {"label": "B"}]}]}
            resp = self.ask("AskUserQuestion", q, True)
            answer = resp.get("updatedInput", {}).get("answers", {}).get("Which option?") \
                if resp.get("behavior") == "allow" else "refused"
            result = f"chosen: {answer}"
        elif body == "/secret" and self.mode != "bypassPermissions":   # a command with a token: will it run as it was?
            asked = {"command": "GH_TOKEN=ghp_" + "Z" * 36 + " gh release create", "description": "release"}
            resp = self.ask("Bash", asked, False)
            result = ("denied: " + str(resp.get("message")) if resp.get("behavior") != "allow"
                      else "run as asked" if resp.get("updatedInput") == asked else "run otherwise")
        elif body == "/write" and self.mode != "bypassPermissions":
            resp = self.ask("Write", {"file_path": "/tmp/x", "content": "hi"}, False)
            result = "written" if resp.get("behavior") == "allow" else f"denied: {resp.get('message')}"
        elif body == "/md":  # a reply with Markdown and an attempt to inject a script — the page must show the first and strip the second
            result = ("## Summary\n\n**Payouts** for September:\n\n| month | amount |\n|---|---|\n| September | $12,400 |\n\n"
                      "- item `code`\n- [link](https://example.com)\n\n```python\nprint(42)\n```\n\n"
                      "<img src=x onerror=\"window.__xss=1\"><script>window.__xss=2</script>")
        elif body.startswith("HANDOFF-REQUEST"):  # hands over its work, as the request demands
            result = "HANDOFF-DONE\nDone: everything\nOpen: nothing\nHanded over: nobody\nBackground: none"
        elif getattr(self, "images", 0):
            result, self.images = f"images seen: {self.images}", 0
        else:
            result = f"ok: {body[:50]}"
        self.out({"type": "assistant", "message": {"content": [{"type": "text", "text": result}]}})
        self.out({"type": "result", "subtype": "success", "is_error": False, "result": result})
        self.hook("Stop")

    def history(self, sid=None):
        d = os.path.join(os.environ.get("HUB_STATE_DIR") or "/tmp", "fake-history")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{sid or self.sid}.jsonl")

    def run(self):
        # like the real one: the conversation appears on disk only after the first turn; --resume without it is an error
        if self.source == "resume" and not os.path.exists(self.history()):
            sys.stderr.write(f"No conversation found with session ID: {self.sid}\n")
            sys.exit(1)
        self.hook("SessionStart", source=self.source)
        self.late()
        version = {"claude_code_version": os.environ["FAKE_CLAUDE_VERSION"]} if "FAKE_CLAUDE_VERSION" in os.environ else {}
        self.out({"type": "system", "subtype": "init", "session_id": self.sid, "apiKeySource": "none",
                  "permissionMode": self.mode, **version})
        while True:
            msg = self.backlog.pop(0) if self.backlog else self.read()
            if msg.get("type") == "user":
                content = msg["message"]["content"]
                if isinstance(content, list):  # text + images, as the host sends for image attachments
                    text = next((c["text"] for c in content if c.get("type") == "text"), "")
                    self.images = sum(1 for c in content if c.get("type") == "image")
                    content = text
                self.turn(content)


if __name__ == "__main__":
    Fake(sys.argv[1:]).run()
