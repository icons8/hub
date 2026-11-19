#!/usr/bin/env python3
"""tests/fakes/fake_codex.py on Windows the way codex.exe 0.162.1 is there for an ordinary user (slice W, probes
2026-11-05 on winstand): initialize says platformFamily "windows" (FAKE_PLATFORM, "unix" for a POSIX one);
windowsSandbox/readiness is "ready" when $CODEX_HOME/config.toml has a [windows] sandbox mode — read once at the
process's start, as the real one caches it (after a setup the same process still says "notConfigured"), else
"notConfigured" (FAKE_READINESS overrides it: "updateRequired"); windowsSandbox/setupStart {mode} answers
{started: true}, then windowsSandbox/setupCompleted — success writes `[windows] sandbox = "<mode>"` into config.toml,
FAKE_SETUP=fail gives success false with an error, FAKE_SETUP=never no notification at all, FAKE_SETUP=error
an error answer to setupStart itself. FAKE_CODEX_CALLS logs every
request, as the fake does. hooks/list also names hooks given on the command line (`-c hooks.<Event>=…`, source
sessionFlags) by the path codex.exe gives them, "C:\\<session-flags>\\config.toml" (FAKE_FLAGS overrides it; a POSIX
platform — "/<session-flags>/config.toml"). A slice's own wrapper: the fake is not edited."""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake_codex  # noqa: E402

CONFIG = os.path.join(fake_codex.HOME, "config.toml")


def configured():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return bool(re.search(r'^\[windows\]\nsandbox = "(elevated|unelevated)"', f.read(), re.M))
    except OSError:
        return False


def session_flags():
    windows = os.environ.get("FAKE_PLATFORM", "windows") == "windows"
    path = os.environ.get("FAKE_FLAGS") or ("C:\\<session-flags>\\config.toml" if windows else "/<session-flags>/config.toml")
    out = []
    for a, b in zip(sys.argv, sys.argv[1:]):
        m = re.match(r"hooks\.([A-Za-z]+)=", b) if a == "-c" else None
        if m:
            snake = re.sub(r"(?<!^)([A-Z])", r"_\1", m.group(1)).lower()
            out.append({"key": f"{path}:{snake}:0:0", "source": "sessionFlags", "sourcePath": path,
                        "currentHash": "sha256:0", "trustStatus": "untrusted", "eventName": snake})
    return out


class Fake(fake_codex.Fake):
    def __init__(self):
        super().__init__()
        self.ready = os.environ.get("FAKE_READINESS") or ("ready" if configured() else "notConfigured")

    def handle(self, msg):
        method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if method == "initialize":
            out = self.out

            def platform(m):   # the fake's own answer, with this platform
                if m.get("id") == rid and "result" in m:
                    m["result"]["platformFamily"] = os.environ.get("FAKE_PLATFORM", "windows")
                out(m)
            self.out = platform
            try:
                return super().handle(msg)
            finally:
                self.out = out
        if method == "hooks/list":
            out = self.out

            def flags(m):   # the fake's own records, then those of the command line's hooks
                if m.get("id") == rid and "result" in m:
                    for entry in m["result"].get("data") or []:
                        entry["hooks"] = list(entry.get("hooks") or []) + session_flags()
                out(m)
            self.out = flags
            try:
                return super().handle(msg)
            finally:
                self.out = out
        if method == "windowsSandbox/readiness":
            self.log(method, params)
            return self.out({"id": rid, "result": {"status": self.ready}})
        if method == "windowsSandbox/setupStart":
            self.log(method, params)
            how = os.environ.get("FAKE_SETUP", "ok")
            if how == "error":   # refused at once, no setupCompleted
                return self.out({"id": rid, "error": {"code": -32600, "message": "setup is not allowed here"}})
            self.out({"id": rid, "result": {"started": True}})
            if how == "ok":
                with open(CONFIG, "a", encoding="utf-8") as f:
                    f.write(f'\n[windows]\nsandbox = "{params.get("mode")}"\n')
                self.note("windowsSandbox/setupCompleted", mode=params.get("mode"), success=True, error=None)
            elif how == "fail":
                self.note("windowsSandbox/setupCompleted", mode=params.get("mode"), success=False,
                          error="setup refused by policy")
            return None
        return super().handle(msg)


if __name__ == "__main__":
    fake_codex.Fake = Fake
    fake_codex.main()
