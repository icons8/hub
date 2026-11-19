#!/usr/bin/env python3
"""tests/fakes/fake_codex.py plus the approval requests of codex-cli 0.162.1, shaped as the
probe of 2026-11-05 saw them (a fake Responses API, no model): the text `/approve_cmd` — the request
item/commandExecution/requestApproval; `/approve_patch` — item/started of a fileChange step, then
item/fileChange/requestApproval with its itemId only (the paths and the diff are in the step); `/approve_perms` —
item/permissions/requestApproval. The turn waits for the answer, then ends with the agentMessage
"decision: <the answer as JSON>" plus whatever was steered into the turn meanwhile."""
import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fake_codex  # noqa: E402

KINDS = ("/approve_cmd", "/approve_patch", "/approve_perms")


class Fake(fake_codex.Fake):
    def run_turn(self, tid, inp):
        text = " ".join(i.get("text", "") for i in inp if i.get("type") == "text")
        kind = next((k for k in KINDS if k in text), None)
        if kind is None:
            return super().run_turn(tid, inp)
        t0 = time.time()
        self.interrupted = None
        self.note("turn/started", threadId=self.thread, turn={"id": tid, "status": "inProgress", "items": []})
        self.user(inp)
        rid, item_id = self.next_id, "call_" + uuid.uuid4().hex[:8]
        self.next_id += 1
        base = {"threadId": self.thread, "turnId": tid, "itemId": item_id, "startedAtMs": int(t0 * 1000)}
        if kind == "/approve_cmd":
            method = "item/commandExecution/requestApproval"
            params = {**base, "kind": "command", "environmentId": "local", "reason": "needs to write outside the folder",
                      "command": "/bin/bash -lc 'echo X > /home/x/out.txt'", "cwd": os.getcwd(),
                      "commandActions": [{"type": "unknown", "command": "echo X > /home/x/out.txt"}],
                      "availableDecisions": ["accept", "cancel"]}
        elif kind == "/approve_patch":
            change = {"type": "fileChange", "id": item_id, "status": "inProgress",
                      "changes": [{"path": os.path.join(os.getcwd(), "notes.txt"), "kind": {"type": "add"},
                                   "diff": "hello\n"}]}
            self.item("started", change)
            method, params = "item/fileChange/requestApproval", {**base, "reason": None, "grantRoot": None}
        else:
            method = "item/permissions/requestApproval"
            params = {**base, "cwd": os.getcwd(), "reason": "needs the network",
                      "permissions": {"network": {"enabled": True}, "fileSystem": None}}
        self.out({"id": rid, "method": method, "params": params})
        while rid not in self.answers:
            time.sleep(0.02)
        time.sleep(self.turn_s)   # what the host steered in after the answer reaches the turn
        later = [self.user(self.steered.pop(0)) for _ in list(self.steered)]
        reply = "decision: " + json.dumps(self.answers[rid], ensure_ascii=False, sort_keys=True) \
            + "".join(f" | steered: {t}" for t in later)
        msg = {"type": "agentMessage", "id": "msg-" + uuid.uuid4().hex, "text": reply, "phase": "final_answer"}
        self.item("started", {**msg, "text": ""})
        self.item("completed", msg)
        self.save()
        self.turn = None
        self.note("turn/completed", threadId=self.thread, turn={
            "id": tid, "status": "completed", "items": [msg], "error": None, "startedAt": int(t0),
            "completedAt": int(time.time()), "durationMs": int((time.time() - t0) * 1000)})


def main():
    fake = Fake()
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        fake.handle(msg)


if __name__ == "__main__":
    main()
