"""A fake core module process (Module API, core/modrun.py): listens on HUB_MODULE_PORT and answers POST /hook/<point>
as the control file says — the test changes it on the fly, the process re-reads it on every request.

Run: `python3 fake_module.py <module folder>` (cwd is the folder with module.json; name and version for hello come from it).
Environment: HUB_MODULE_PORT, HUB_MODULE_SECRET (given by the core), FAKE_MODULE_CTL — the control file (JSON),
FAKE_MODULE_LOG — where to write requests (JSONL: {point, body, secret_ok, at — time.time() of the request}).

Control file:
  {"start": "ok" | "exit" | "late",        # listen at once | exit with code 3 | start listening after "late_s" seconds
   "points": ["cli"],                      # what to declare in hello (default [])
   "hello": {...},                         # own hello answer instead of {name, version, points}
   "<point>": {"do": "ok" | "silent" | "garbage" | "status" | "slow" | "big" | "exit",
               "answer": <JSON answer>, "delay": seconds for slow,
               "with_stdin": <JSON answer to a request with a stdin field>}}
  ok — answer (default {}); silent — accept and say nothing; garbage — not JSON; status — 500; slow — answer after delay;
  big — JSON larger than 1 MB; exit — exit with code 4 in the middle of a request.
A request without the right X-Hub-Secret gets 403 (and is logged: secret_ok false).
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def ctl():
    try:
        with open(os.environ["FAKE_MODULE_CTL"], encoding="utf-8") as f:
            return json.load(f)
    except (KeyError, OSError, ValueError):
        return {}


def log(entry):
    path = os.environ.get("FAKE_MODULE_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def manifest():
    with open("module.json", encoding="utf-8") as f:
        return json.load(f)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def answer(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        size = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(size) if size else b""
        point = self.path[len("/hook/"):] if self.path.startswith("/hook/") else self.path
        ok = self.headers.get("X-Hub-Secret") == os.environ.get("HUB_MODULE_SECRET")
        try:
            parsed = json.loads(body or b"null")
        except ValueError:
            parsed = None
        log({"point": point, "body": parsed, "secret_ok": ok, "at": time.time()})
        if not ok:
            return self.answer(403, b'{"error": "secret"}')
        c = ctl()
        if point == "hello" and "hello" not in c:
            man = manifest()
            return self.answer(200, json.dumps({"name": man["name"], "version": man["version"],
                                                "points": c.get("points", [])}).encode())
        how = c.get(point) or {}
        do = how.get("do", "ok")
        if point == "hello":
            how, do = {"answer": c["hello"]}, "ok"
        if do == "silent":
            time.sleep(3600)
        if do == "exit":
            os._exit(4)
        if do == "garbage":
            return self.answer(200, b"<html>not json")
        if do == "status":
            return self.answer(500, b'{"error": "boom"}')
        if do == "big":
            return self.answer(200, json.dumps({"x": "y" * (2 << 20)}).encode())
        if do == "slow":
            time.sleep(float(how.get("delay") or 0.05))
        # cli: second call with the stdin already read (the module asked for it with the answer {"stdin": true}) — its own answer
        if isinstance(parsed, dict) and "stdin" in parsed and "with_stdin" in how:
            return self.answer(200, json.dumps(how["with_stdin"], ensure_ascii=False).encode())
        return self.answer(200, json.dumps(how.get("answer", {}), ensure_ascii=False).encode())


def main():
    if len(sys.argv) > 1:
        os.chdir(sys.argv[1])
    c = ctl()
    if c.get("start") == "exit":
        sys.exit(3)
    if c.get("start") == "late":
        time.sleep(float(c.get("late_s") or 1))
    srv = ThreadingHTTPServer(("127.0.0.1", int(os.environ["HUB_MODULE_PORT"])), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log({"point": "-started", "pid": os.getpid()})
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
