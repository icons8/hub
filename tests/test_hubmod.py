"""The hubmod helper (core/hubmod.py, Module API 2.3): a Python module is functions with the @hook, @route, @view
decorators and a state helper; the server, secret, hello and checking an answer against the point's schema live in the
library. The module process is launched the way hub-up launches it (core/modrun.launch) and is talked to as the core
(modrun.post) and as the page (modproxy)."""
import tests  # noqa: F401 — ignore this machine's settings
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

from core import hubmod, modrun, modules, spool
from tests import test_modapi_http
from tests.fakes.procs import kill

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MAIN = '''
import os, sys, time
from core import hubmod

@hubmod.hook("doctor")
def doctor(req):
    return [{"name": "base", "ok": True, "detail": "api " + str(req.get("api"))}]

@hubmod.hook("cli")
def cli(req):
    return {"out": 5} if req["args"] == ["bad"] else {"out": "ok\\n"}

@hubmod.hook("migrate")
def migrate(req):
    return {"error": "the old data cannot be moved"}

@hubmod.route("/items", methods=["GET", "POST"])
def items(req):
    if req.method == "POST":
        return {"got": req.json(), "owner": req.owner, "view": req.view}
    return {"items": [1, 2], "q": req.query.get("q"), "owner": req.owner}

@hubmod.route("/items/<id>", methods=["DELETE"])
def drop(req):
    return hubmod.Response({"dropped": req.params["id"]}, status=202)

@hubmod.route("/oops")
def oops(req):
    raise hubmod.HTTPError(409, "busy")

@hubmod.route("/crash")
def crash(req):
    raise RuntimeError("crashed")

@hubmod.route("/upload", methods=["POST"], body="file")
def upload(req):
    with open(req.file, "rb") as f:
        data = f.read()
    return {"size": len(data), "head": data[:5].decode(), "file_in_tmp": os.path.exists(req.file)}

@hubmod.route("/events", stream=True)
def events(req):
    for i in range(int(req.query.get("n", "3"))):
        yield {"n": i}
        time.sleep(0.05)
    yield "end"

@hubmod.route("/save", methods=["POST"])
def save(req):
    hubmod.state.set("note", req.json())
    try:
        hubmod.state.set("note", 1, if_rev=hubmod.state.rev("note"))   # own write before refresh: no revision yet
        rev = "written without a revision"
    except ValueError as e:
        rev = str(e)
    try:
        hubmod.state.set("big", "x" * (300 * 1024))
        big = "accepted"
    except ValueError as e:
        big = str(e)
    return {"note": hubmod.state.get("note"), "big": big, "rev": rev}

@hubmod.view("/")
def page(req):
    return "<!doctype html><p>view</p>"

if __name__ == "__main__":
    hubmod.run()
'''


class Module(unittest.TestCase):
    """A hubmod module in its own folder and its process."""
    caps = ["doctor", "cli", "http"]

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubmod-")
        self.addCleanup(shutil.rmtree, self.state, True)

    def start(self, main=MAIN, caps=None, name="notes", wait=True):
        d = os.path.join(self.state, "src", name)
        os.makedirs(d, exist_ok=True)
        man = {"name": name, "version": "1.0.0", "api": "^2.1", "run": [sys.executable, "main.py"],
               "capabilities": self.caps if caps is None else caps, "commands": {"notes": "notes"},
               "http": {"max_body": 10 << 20}}
        if "cli" not in man["capabilities"]:
            man.pop("commands")
        if "http" not in man["capabilities"]:
            man.pop("http")
        self.assertEqual(modules.validate(man), [])
        with open(os.path.join(d, "module.json"), "w") as f:
            json.dump(man, f)
        with open(os.path.join(d, "main.py"), "w") as f:
            f.write(textwrap.dedent(main))
        want = {"name": name, "cmd": man["run"], "cwd": d, "version": "1.0.0", "order": 0, "budget": {},
                "capabilities": man["capabilities"], "key": "k"}
        os.makedirs(modrun.run_dir(self.state), exist_ok=True)
        modrun._write(os.path.join(modrun.run_dir(self.state), f"{name}.want.json"), want)
        self.err = os.path.join(self.state, f"{name}.err")

        def spawn(cmd, cwd, env):
            with open(self.err, "w") as err:
                p = subprocess.Popen(cmd, cwd=cwd, env={**env, "PYTHONPATH": ROOT}, stdin=subprocess.DEVNULL,
                                     stdout=err, stderr=subprocess.STDOUT)
            self.addCleanup(kill, p.pid)
            return p
        self.proc = modrun.launch(self.state, name, want, spawn)
        self.run_ = modrun.run_of(self.state, name)
        if wait:
            end = time.time() + 15
            while True:
                try:
                    socket.create_connection(("127.0.0.1", self.run_["port"]), 0.2).close()
                    break
                except OSError:
                    self.assertIsNone(self.proc.poll(), self.stderr())
                    self.assertLess(time.time(), end, "the module process is not listening")
                    time.sleep(0.05)
        return self.proc

    def stderr(self):
        with open(self.err, encoding="utf-8", errors="replace") as f:
            return f.read()

    def hook(self, point, payload):
        return modrun.post(self.run_["port"], self.run_["secret"], f"/hook/{point}", payload, 5)

    def http(self, method, path, body=None, headers=None, secret=True):
        c = http.client.HTTPConnection("127.0.0.1", self.run_["port"], timeout=10)
        h = {**({"X-Hub-Secret": self.run_["secret"]} if secret else {}), **(headers or {})}
        c.request(method, path, body, h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data, r


class Hooks(Module):
    def test_hello_lists_the_decorated_hooks(self):
        self.start()
        self.assertEqual(self.hook("hello", {}), (None, {"name": "notes", "version": "1.0.0",
                                                         "points": ["cli", "doctor", "migrate"]}))
        self.assertEqual(self.hook("doctor", {"api": "2.3"})[1], [{"name": "base", "ok": True, "detail": "api 2.3"}])

    def test_an_answer_out_of_schema_is_caught_in_the_module_with_the_reason(self):
        self.start()
        kind, text = self.hook("cli", {"command": "notes", "args": ["bad"]})
        self.assertEqual(kind, "status")   # the core will record a failure anyway — but the cause is visible in the module log
        self.assertIn("cli: {out, err, code", self.stderr())
        self.assertEqual(self.hook("cli", {"command": "notes", "args": []})[1], {"out": "ok\n"})

    def test_a_migration_refusal_with_its_reason_is_an_answer_not_a_schema_failure(self):
        self.start()
        self.assertEqual(self.hook("migrate", {"from": "0.9.0", "to": "1.0.0"}),
                         (None, {"error": "the old data cannot be moved"}))

    def test_without_the_secret_nothing_answers(self):
        self.start()
        self.assertEqual(modrun.post(self.run_["port"], "foreign", "/hook/hello", {}, 2)[0], "status")
        self.assertEqual(self.http("GET", "/api/mod/notes/items", secret=False)[0], 403)

    def test_a_hook_or_route_without_its_right_stops_the_start_with_the_reason(self):
        self.start(caps=["doctor"], wait=False)
        self.assertNotEqual(self.proc.wait(15), 0)
        out = self.stderr()
        self.assertIn("cli", out)
        self.assertIn("http", out)
        main = MAIN.replace('@hubmod.hook("doctor")', '@hubmod.hook("no.such")')
        self.start(main=main, wait=False)
        self.assertNotEqual(self.proc.wait(15), 0)
        self.assertIn("no.such", self.stderr())


class Routes(Module):
    def test_json_query_params_status_and_errors(self):
        self.start()
        code, data, _ = self.http("GET", "/api/mod/notes/items?q=a%20b", headers={"X-Hub-Owner": "1"})
        self.assertEqual((code, json.loads(data)), (200, {"items": [1, 2], "q": "a b", "owner": True}))
        code, data, _ = self.http("POST", "/api/mod/notes/items", json.dumps({"t": "x"}),
                                  {"Content-Type": "application/json", "X-Hub-View": "1"})
        self.assertEqual(json.loads(data), {"got": {"t": "x"}, "owner": False, "view": True})
        code, data, _ = self.http("DELETE", "/api/mod/notes/items/42")
        self.assertEqual((code, json.loads(data)), (202, {"dropped": "42"}))
        code, data, _ = self.http("DELETE", "/api/mod/notes/items/a%2Fb%20c")   # an encoded "/" is part of the value
        self.assertEqual((code, json.loads(data)), (202, {"dropped": "a/b c"}))
        self.assertEqual(self.http("PUT", "/api/mod/notes/items")[0], 405)
        self.assertEqual(self.http("GET", "/api/mod/notes/nope")[0], 404)
        code, data, _ = self.http("GET", "/api/mod/notes/oops")
        self.assertEqual((code, json.loads(data)), (409, {"error": "busy"}))
        code, data, _ = self.http("GET", "/api/mod/notes/crash")
        self.assertEqual(code, 500)
        self.assertIn("crashed", self.stderr())

    def test_body_as_a_file_is_removed_after_the_answer(self):
        self.start()
        body = b"hello" + os.urandom(3 << 20)
        code, data, _ = self.http("POST", "/api/mod/notes/upload", body)
        self.assertEqual((code, json.loads(data)), (200, {"size": len(body), "head": "hello", "file_in_tmp": True}))
        # the handle's finally removes the file after the answer — under load not at once (red in a full run)
        end = time.time() + 10
        while (left := [n for n in os.listdir(tempfile.gettempdir()) if n.startswith("hubmod-body-")]) \
                and time.time() < end:
            time.sleep(0.05)
        self.assertEqual(left, [])

    def test_a_stream_sends_events(self):
        self.start()
        code, data, r = self.http("GET", "/api/mod/notes/events?n=2")
        self.assertEqual((code, r.getheader("Content-Type")), (200, "text/event-stream; charset=utf-8"))
        self.assertEqual(data.decode(), 'data: {"n": 0}\n\ndata: {"n": 1}\n\ndata: end\n\n')

    def test_a_view_is_html(self):
        self.start()
        code, data, r = self.http("GET", "/mod/notes/view/")
        self.assertEqual((code, r.getheader("Content-Type"), data.decode()),
                         (200, "text/html; charset=utf-8", "<!doctype html><p>view</p>"))
        self.assertEqual(self.http("POST", "/mod/notes/view/")[0], 405)


class State(Module):
    def test_set_puts_a_fact_and_a_too_big_value_is_refused_in_the_module(self):
        self.start()
        code, data, _ = self.http("POST", "/api/mod/notes/save", json.dumps({"a": 1}))
        got = json.loads(data)
        self.assertEqual(got["note"], {"a": 1})   # the module sees what it has just written at once
        self.assertIn("256", got["big"])          # the core's limit — before the inbox, with a reason
        self.assertIn("refresh", got["rev"])      # if_rev=None does not silently drop the protection
        facts = [e for _, e in spool.take(os.path.join(self.state, "spool"))]
        self.assertEqual([(e["type"], e["data"]["key"], e["data"]["value"]) for e in facts],
                         [("mod.notes.set", "note", {"a": 1})])


class StateCopy(unittest.TestCase):
    """get and all return a copy, set takes a copy: a module that read, edited and got a refusal (its own or the core's)
    does not leave a half-changed value in the process memory — the next write will not carry it into the journal."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubmod-copy-")
        self.addCleanup(shutil.rmtree, self.state, True)
        env = {"HUB_MODULE": "notes", "HUB_STATE_DIR": self.state}
        self.addCleanup(lambda old={k: os.environ.get(k) for k in env}: [
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v) for k, v in old.items()])
        os.environ.update(env)
        self.st = hubmod._State()
        self.st._data, self.st._others = {"todo": {"value": {"items": [{"id": 1, "done": False}]}, "rev": 5}}, 0

    def test_a_change_of_what_was_read_stays_out_of_the_module_state(self):
        got = self.st.get("todo")
        got["items"][0]["done"] = True
        self.st.all()["todo"]["items"].append({"id": 2})
        self.assertEqual(self.st.get("todo"), {"items": [{"id": 1, "done": False}]})
        self.assertEqual(self.st.rev("todo"), 5)

    def test_a_change_after_set_stays_out_of_the_module_state(self):
        value = {"items": [{"id": 1, "done": True}]}
        self.st.set("todo", value)
        value["items"].append({"id": 2})
        self.assertEqual(self.st.get("todo"), {"items": [{"id": 1, "done": True}]})
        (_, e), = spool.take(os.path.join(self.state, "spool"))
        self.assertEqual(e["data"]["value"], {"items": [{"id": 1, "done": True}]})
        self.st.set("pair", {1: (2, 3)})   # in the process — as it lands in the journal, not one thing before refresh and another after
        self.assertEqual(self.st.get("pair"), {"1": [2, 3]})


class OwnerWord(unittest.TestCase):
    """hubmod.owner — the owner's word from a module handle: only on the owner's request (the page's X-Hub-Owner); from a
    view, or from the module process without a request — refused before the inbox; do not forget the check."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubmod-owner-")
        self.addCleanup(shutil.rmtree, self.state, True)
        env = {"HUB_MODULE": "notes", "HUB_STATE_DIR": self.state}
        self.addCleanup(lambda old={k: os.environ.get(k) for k in env}: [
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v) for k, v in old.items()])
        os.environ.update(env)

    def facts(self):
        got = list(spool.take(os.path.join(self.state, "spool")))
        for path, _ in got:
            os.unlink(path)
        return [e for _, e in got]

    def req(self, owner, view=False):
        return hubmod.Request("POST", "/send", {}, {}, {}, owner, view)

    def test_the_owner_word_goes_to_the_box_with_the_module_channel_and_its_key(self):
        hubmod.owner(self.req(True), "message.received", None, {"to": "maps", "text": "yes", "owner": False,
                                                                 "channel": "web"}, dedup_key="c-1")
        (e,) = self.facts()
        self.assertEqual((e["type"], e["entity"], e["src"]), ("message.received", None, "module"))
        self.assertEqual(e["data"], {"to": "maps", "text": "yes", "channel": "mod:notes", "owner": True,
                                     "dedup_key": "c-1"})
        hubmod.owner(self.req(True), "session.interrupt_requested", "maps")
        (e,) = self.facts()
        self.assertEqual((e["entity"], e["data"]), ("maps", {"channel": "mod:notes", "owner": True}))

    def test_not_from_the_owner_is_refused_before_the_box(self):
        for req in (self.req(False), self.req(True, view=True), self.req(False, view=True), None):
            with self.assertRaises(hubmod.HTTPError) as got:
                hubmod.owner(req, "message.received", None, {"to": "maps", "text": "x"})
            self.assertEqual(got.exception.status, 403)
        for key in ("", "k" * 129, 7):   # a key the core would reject — before the inbox, not after the handle's answer
            # the key usually comes from client data (its client_id, a request id): a handle that did not catch the refusal gets 400, not 500
            with self.assertRaises(hubmod.HTTPError) as got:
                hubmod.owner(self.req(True), "message.received", None, {"to": "maps", "text": "x"}, dedup_key=key)
            self.assertEqual(got.exception.status, 400)
            self.assertIn("128", got.exception.text)
            self.assertIsInstance(got.exception, ValueError)   # the module's old `except ValueError` catches it, as before 2.6
        hubmod.owner(self.req(True), "message.received", None, {"to": "maps", "text": "x"}, dedup_key="k" * 128)
        self.assertEqual([e["data"]["dedup_key"] for e in self.facts()], ["k" * 128])   # 128 is still allowed
        self.assertEqual(self.facts(), [])

    def test_the_core_takes_it_as_the_owner_word_and_a_repeat_is_refused(self):
        from core import reducer
        st = reducer.empty()
        for seq, (t, ent, d) in enumerate((("session.requested", "maps", {"cwd": "/m", "session_id": "s",
                                                                           "channel": "web", "owner": True}),
                                           ("module.ready", "notes", {"module": "notes", "version": "1.0.0",
                                                                      "points": [], "channel": "core",
                                                                      "owner": False})), 1):
            st, _, why = reducer.reduce(st, {"seq": seq, "ts": 1000 + seq, "type": t, "entity": ent, "data": d})
            self.assertIsNone(why)
        for _ in range(2):
            hubmod.owner(self.req(True), "message.received", None, {"to": "maps", "text": "yes"}, dedup_key="c-1")
        whys = []
        for seq, e in enumerate(self.facts(), 3):
            st, _, why = reducer.reduce(st, {"seq": seq, "ts": 1000 + seq, "type": e["type"], "entity": e["entity"],
                                             "data": e["data"]})
            whys.append(why)
        self.assertIsNone(whys[0])
        self.assertTrue(st["messages"]["m3"]["from_owner"])
        self.assertIn("m3", whys[1])


class ThroughThePage(test_modapi_http.ModHttp):
    """The same module behind the hub page: a handle, a stream and a view — through the module proxy."""

    def test_route_stream_and_view_through_the_page(self):
        d = os.path.join(self.state, "mods", "notes")
        os.makedirs(d)
        with open(os.path.join(d, "main.py"), "w") as f:
            f.write(textwrap.dedent(MAIN))
        man = {"name": "notes", "version": "1.0.0", "api": "^2.1", "run": [sys.executable, "main.py"],
               "capabilities": ["doctor", "cli", "http"], "commands": {"notes": "notes"}}
        with open(os.path.join(d, "module.json"), "w") as f:
            json.dump(man, f)
        want = {"name": "notes", "cmd": man["run"], "cwd": d, "version": "1.0.0", "order": 0, "budget": {},
                "capabilities": man["capabilities"], "key": "k"}
        os.makedirs(modrun.run_dir(self.state), exist_ok=True)
        modrun._write(os.path.join(modrun.run_dir(self.state), "notes.want.json"), want)

        def spawn(cmd, cwd, env):
            p = subprocess.Popen(cmd, cwd=cwd, env={**env, "PYTHONPATH": ROOT}, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.procs.append(p)
            return p
        modrun.launch(self.state, "notes", want, spawn)
        run = modrun.run_of(self.state, "notes")
        self.until(lambda: modrun.post(run["port"], run["secret"], "/hook/hello", {}, 1)[0] is None)
        self.j.append("module.ready", "notes", {"module": "notes", "version": "1.0.0", "points": ["cli", "doctor"],
                                                "launch": run["launch"], "channel": "core", "owner": False})
        self.web.tail.poll()
        code, data, _ = self.api("GET", "/api/mod/notes/items?q=z")
        self.assertEqual((code, json.loads(data)), (200, {"items": [1, 2], "q": "z", "owner": True}))
        code, data, _ = self.api("GET", "/api/mod/notes/events?n=1")
        self.assertEqual(data.decode(), 'data: {"n": 0}\n\ndata: end\n\n')
        code, data, r = self.req("GET", "/mod/notes/view/")
        self.assertEqual((code, data.decode()), (200, "<!doctype html><p>view</p>"))
        self.assertIn("sandbox", r.getheader("Content-Security-Policy"))


def load_tests(loader, standard, pattern):
    """Without the tests inherited from ModHttp: they run in test_modapi_http."""
    suite = unittest.TestSuite()
    for cls in (Hooks, Routes, State, StateCopy, OwnerWord):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    suite.addTests(ThroughThePage(n) for n in ThroughThePage.__dict__ if n.startswith("test"))
    return suite


if __name__ == "__main__":
    unittest.main()
