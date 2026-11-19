#!/usr/bin/env python3
"""tests/fakes/fake_codex.py with MCP the way codex-cli 0.162.1 does it (probe 2026-11-05, hosts/codex_mcp.py): the
thread's servers (thread/start config.mcp_servers) start after thread/start — mcpServer/startupStatus/updated starting,
then ready, or failed for a server with a url and no sign-in; mcpServerStatus/list {threadId} — runtimeStatus connected |
authenticationRequired | failed; config/mcpServer/reload restarts them all; mcpServer/oauth/login on an http server gives
a link and, at once (as if the person had signed in), mcpServer/oauthLogin/completed and the server's restart — it is
connected after that. FAKE_CODEX_CALLS logs every request, as the fake does. A slice's own wrapper: the fake is not edited."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake_codex  # noqa: E402


class Fake(fake_codex.Fake):
    def __init__(self):
        super().__init__()
        self.servers = {}    # name -> its config from the thread
        self.signed = set()  # servers with a url that are signed in

    def runtime(self, name):
        cfg = self.servers.get(name) or {}
        if cfg.get("url"):
            return "connected" if name in self.signed else "authenticationRequired"
        return "failed" if str(cfg.get("command") or "").startswith("/nonexistent") else "connected"

    def start_servers(self, names):
        for name in names:
            self.note("mcpServer/startupStatus/updated", threadId=self.thread, name=name, status="starting")
        for name in names:
            ok = self.runtime(name) == "connected"
            self.note("mcpServer/startupStatus/updated", threadId=self.thread, name=name,
                      status="ready" if ok else "failed", error=None if ok else f"{name}: not started")

    def handle(self, msg):
        method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if method == "thread/start":
            self.servers = dict(((params.get("config") or {}).get("mcp_servers")) or {})
            super().handle(msg)
            self.start_servers(sorted(self.servers))
            return
        if method == "mcpServerStatus/list":
            self.log(method, params)
            data = [{"name": n, "runtimeStatus": self.runtime(n), "tools": {}, "resources": [], "resourceTemplates": [],
                     "authStatus": ("oAuth" if n in self.signed else "notLoggedIn") if c.get("url") else "unsupported",
                     "httpOrigin": None} for n, c in sorted(self.servers.items())] if params.get("threadId") else []
            self.out({"id": rid, "result": {"data": data, "nextCursor": None}})
            return
        if method == "config/mcpServer/reload":
            self.log(method, params)
            self.out({"id": rid, "result": {}})
            self.start_servers(sorted(self.servers))
            return
        if method == "mcpServer/oauth/login":
            self.log(method, params)
            name = params.get("name")
            if not (self.servers.get(name) or {}).get("url"):
                self.out({"id": rid, "error": {"code": -32600, "message": "OAuth login is only supported for streamable "
                                                                          "HTTP servers."}})
                return
            self.out({"id": rid, "result": {"loginId": "L1", "authorizationUrl":
                      "https://mcp.example.test/authorize?redirect_uri=http%3A%2F%2F127.0.0.1%3A40229%2Fcallback%2Fx"}})
            self.signed.add(name)
            self.note("mcpServer/oauthLogin/completed", threadId=self.thread, name=name, loginId="L1", success=True)
            self.start_servers([name])
            return
        super().handle(msg)


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
