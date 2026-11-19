#!/usr/bin/env python3
"""A fake `codex app-server` whose sign-in file in $CODEX_HOME/auth.json is dead (a revoked refresh token): every
account request while the file is there is refused like the real one did ("workspace routing discovery unauthorized
(401)"). $FAKE_DEAD: "401" — account/read and account/logout are refused while the file is there (a sign-out
that does not work), "read" — only account/read is, "init" — initialize itself is. $FAKE_ERR — the text of the refusal
(default: the 401 one), so a test can make it a network error. A sign-out that works deletes the file; a sign-in
(account/login/start, then account/login/completed) writes a new one and account/read then answers. $FAKE_CODEX_CALLS — a file of the requests."""
import json
import os
import sys
import time

HOME = os.environ.get("CODEX_HOME") or ""
AUTH = os.path.join(HOME, "auth.json")
MODE = os.environ.get("FAKE_DEAD", "401")
DEAD = os.environ.get("FAKE_ERR") or "workspace routing discovery unauthorized (401)"


def out(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        msg = json.loads(line)
        method, rid = msg.get("method"), msg.get("id")
        if os.environ.get("FAKE_CODEX_CALLS"):
            with open(os.environ["FAKE_CODEX_CALLS"], "a", encoding="utf-8") as f:
                f.write(json.dumps({"method": method, "had_auth": os.path.exists(AUTH)}) + "\n")
        if rid is None:
            continue
        bad = {"code": -32603, "message": DEAD}
        if method == "initialize":
            if MODE == "init" and os.path.exists(AUTH):
                out({"id": rid, "error": bad})
            else:
                out({"id": rid, "result": {"userAgent": "fake/0.167", "codexHome": HOME}})
        elif method == "account/read":
            if os.path.exists(AUTH) and "signed_in_now" in open(AUTH, encoding="utf-8").read():
                out({"id": rid, "result": {"account": {"type": "chatgpt", "email": "new@example.com",
                                                       "planType": "plus"}, "requiresOpenaiAuth": True}})
            elif os.path.exists(AUTH):
                out({"id": rid, "error": bad})
            else:
                out({"id": rid, "result": {"account": None, "requiresOpenaiAuth": True}})
        elif method == "account/logout":
            if os.path.exists(AUTH) and MODE == "401":
                out({"id": rid, "error": bad})
            else:
                if os.path.exists(AUTH):
                    os.remove(AUTH)
                out({"id": rid, "result": {}})
        elif method == "account/login/start":
            out({"id": rid, "result": {"type": "chatgptDeviceCode", "loginId": "l1", "userCode": "ABCD-1234",
                                       "verificationUrl": "https://auth.example.com/codex/device"}})
            time.sleep(0.2)
            with open(AUTH, "w", encoding="utf-8") as f:
                json.dump({"signed_in_now": True}, f)
            out({"method": "account/login/completed", "params": {"success": True, "loginId": "l1"}})
        else:
            out({"id": rid, "result": {}})


main()
