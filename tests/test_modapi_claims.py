"""A module for a client with a different API (Module API 2.4, core/modclaims.py): the module answers on its own /api/…
path (http.paths) and its own HTML view (http.views) on the page port, and puts its top-level keys and features
(snapshot.keys, snapshot.features) into the snapshot while it is ready. The core always wins: a module does not get a
core path, key or feature; two modules claiming the same path or key both lose it until modules.use names one."""
import tests  # noqa: F401 — do not see the machine's settings
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from core import journal, modclaims, modrun, modstate, modules, reducer, version
from core.daemon import Hub
from modules.web import modproxy
from modules.web.server import Web
from tests.fakes.procs import kill
from tests.test_modapi_http import ModHttp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(ROOT, "examples", "module-client-api")


def man(**extra):
    return {"name": "m", "version": "1.0.0", "api": "^2.4", "run": ["x"], "capabilities": ["http"], **extra}


class Manifest(unittest.TestCase):
    def test_paths_views_and_snapshot_fields(self):
        self.assertGreaterEqual(version.parse_version(version.MODULE_API), version.parse_version("2.4"))
        ok = man(http={"paths": ["/api/files", "/api/usage/cycles", "/api/session/rename"], "views": ["/api/visual"],
                       "max_body": 10},
                 snapshot={"keys": ["todo", "todo_done", "model_names"], "features": ["session.rename", "ask-feed"]})
        self.assertEqual(modules.validate(ok), [])
        self.assertEqual(modules.validate(man(snapshot={"keys": ["todo"]}, capabilities=[])), [])   # keys need no capability
        for bad in ({"paths": "/api/x"}, {"paths": ["api/x"]}, {"paths": ["/files"]}, {"paths": ["/api/"]},
                    {"paths": ["/api/x/"]}, {"paths": ["/api/x?y=1"]}, {"paths": ["/api/X"]}, {"paths": ["/api/a b"]},
                    {"paths": ["/api//x"]}, {"paths": ["/api/../send"]}, {"paths": ["/api/./x"]},
                    {"paths": ["/api/mod/x/y"]}, {"paths": ["/api/mod"]}, {"paths": ["/api/x", "/api/x"]},
                    {"paths": ["/api/x"], "views": ["/api/x"]}, {"views": [5]},
                    {"paths": [f"/api/p{i}" for i in range(modclaims.MAX_PATHS + 1)]}):
            self.assertTrue(modules.validate(man(http=bad)), bad)
        for bad in ({"keys": "todo"}, {"keys": ["Todo"]}, {"keys": ["_gone"]}, {"keys": ["a.b"]}, {"keys": ["x" * 65]},
                    {"features": ["Bad"]}, {"features": [""]}, {"other": []}, [], {"keys": ["a", "a"]},
                    {"keys": [f"k{i}" for i in range(modclaims.MAX_KEYS + 1)]}):
            self.assertTrue(modules.validate(man(snapshot=bad)), bad)
        # paths need the http capability
        self.assertTrue(modules.validate(man(http={"paths": ["/api/x"]}, capabilities=[])))

    def test_new_fields_need_api_2_4(self):
        for extra in ({"http": {"paths": ["/api/x"]}}, {"http": {"views": ["/api/x"]}}, {"snapshot": {"keys": ["x"]}}):
            errs = modules.validate({**man(**extra), "api": "^2.3"})
            self.assertTrue(any("2.4" in e for e in errs), (extra, errs))
        self.assertEqual(modules.validate({**man(http={"max_body": 5}), "api": "^2.1"}), [])   # the old behaviour is unchanged


class CoreList(unittest.TestCase):
    """The core path list covers every path the page answers on: a module cannot take it, and a path the core adds
    later cannot be taken either, if the list is updated. The test keeps the list complete."""

    def test_every_page_path_is_the_cores(self):
        web = os.path.join(ROOT, "modules", "web")
        seen = 0
        for name in sorted(os.listdir(web)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(web, name), encoding="utf-8") as f:
                src = f.read()
            for m in re.finditer(r'(startswith\(\s*)?"(/api/[^"]*)"', src):
                lit = m.group(2)
                if "(" in lit or lit == "/api/":   # a modproxy regex pattern; the path namespace itself
                    continue
                base = re.split(r"[?{]", lit)[0]
                seen += 1
                self.assertTrue(modclaims.core_path(base), f"{name}: {lit}")
                if m.group(1) and not lit.endswith("?"):   # the page matches by prefix — and everything after it
                    self.assertTrue(modclaims.core_path(base + "x"), f"{name}: {lit}")
                    self.assertTrue(modclaims.core_path(base + "/x"), f"{name}: {lit}")
        self.assertGreater(seen, 50)

    def test_core_paths_and_under_them(self):
        for p in ("/api/send", "/api/stream", "/api/snapshot", "/api/upload/raw", "/api/mod", "/api/usage",
                  "/api/mod/x/y", "/api/schedule/anything", "/api/flowers", "/api/file", "/api/answer"):
            self.assertTrue(modclaims.core_path(p), p)
        # the page matches whole paths: what sits under a core path is not the core's
        for p in ("/api/files", "/api/usage/cycles", "/api/session/rename", "/api/visual", "/api/ask",
                  "/api/voice/stream", "/api/send/x"):
            self.assertFalse(modclaims.core_path(p), p)

    def test_every_snapshot_key_and_feature_is_the_cores(self):
        state = tempfile.mkdtemp(prefix="claims-keys-")
        self.addCleanup(shutil.rmtree, state, True)
        web = Web(state)
        web.config = lambda: {}
        d = os.path.join(state, "mods", "x")
        os.makedirs(d)
        with open(os.path.join(d, "module.json"), "w") as f:
            json.dump({"name": "x", "version": "1.0.0", "api": "^2.4", "run": ["x"]}, f)
        os.makedirs(modrun.run_dir(state))
        modrun._write(os.path.join(modrun.run_dir(state), "x.want.json"),
                      {"name": "x", "cmd": ["x"], "cwd": d, "version": "1.0.0", "order": 0, "key": "k"})
        base = web.tail.reader.snapshot()
        with mock.patch.object(web.push, "settings", return_value={"key": 1}), \
                mock.patch("modules.web.server.reactions_on", return_value=True):
            snap = web.with_clock({**base, "mods": {"x": {}}, "core_written": {}})
        self.assertEqual(set(snap) - modclaims.CORE_KEYS, set())
        self.assertEqual(set(snap["features"]) - modclaims.CORE_FEATURES, set())
        self.assertLessEqual({"_gone", "_delta", "_n"}, modclaims.CORE_KEYS)   # the stream patch names are not a module's either


def found(name, http=None, snapshot=None, state="ok"):
    m = man(name=name, **({"http": http} if http else {}), **({"snapshot": snapshot} if snapshot else {}))
    return {"name": name, "manifest": m, "state": state}


class Resolve(unittest.TestCase):
    def test_core_names_are_dropped_and_the_rest_kept(self):
        claims, notes = modclaims.resolve(
            [found("a", http={"paths": ["/api/files", "/api/send"], "views": ["/api/visual", "/api/stream"]},
                   snapshot={"keys": ["todo", "voice", "sessions"], "features": ["session.rename", "push"]})], {})
        self.assertEqual(claims, {"a": {"paths": ["/api/files"], "views": ["/api/visual"], "keys": ["todo"],
                                        "features": ["session.rename"]}})
        self.assertEqual(len(notes["a"]), 5)
        self.assertTrue(all("the core" in n for n in notes["a"]), notes)

    def test_two_modules_with_one_name_both_lose_it_until_use_names_one(self):
        plan = [found("a", http={"paths": ["/api/files", "/api/a"]}, snapshot={"keys": ["todo", "a"]}),
                found("b", http={"views": ["/api/files"]}, snapshot={"keys": ["todo"], "features": ["f"]}),
                found("c", http={"paths": ["/api/files"]}, state="disabled")]   # not loaded — does not contest
        claims, notes = modclaims.resolve(plan, {})
        self.assertEqual(claims, {"a": {"paths": ["/api/a"], "keys": ["a"]}, "b": {"features": ["f"]}})
        self.assertTrue(any("/api/files" in n and "modules.use.paths" in n for n in notes["a"]), notes)
        self.assertTrue(any("todo" in n and "modules.use.keys" in n for n in notes["b"]), notes)
        claims, notes = modclaims.resolve(plan, {"modules": {"use": {"paths": {"/api/files": "b"},
                                                                     "keys": {"todo": "a"}}}})
        self.assertEqual(claims, {"a": {"paths": ["/api/a"], "keys": ["todo", "a"]},
                                  "b": {"views": ["/api/files"], "features": ["f"]}})
        self.assertEqual(notes["b"], ["snapshot key todo — given to the module a (modules.use.keys)"])
        self.assertTrue(any("/api/files" in n and "b" in n for n in notes["a"]), notes)
        # a choice that contests nothing (or has the wrong shape) changes nothing
        for use in ({"paths": {"/api/files": "c"}}, {"paths": ["a"]}, {"keys": "a"}):
            self.assertEqual(modclaims.resolve(plan, {"modules": {"use": use}})[0]["a"], {"paths": ["/api/a"],
                                                                                         "keys": ["a"]}, use)

    def test_module_without_claims_has_none(self):
        self.assertEqual(modclaims.resolve([found("a")], {}), ({}, {}))


class Supervisor(unittest.TestCase):
    """hubd puts what is claimed into want; what a module lost goes to the journal as a module.claims line (once per
    change) and into hub modules."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="claims-hubd-")
        self.addCleanup(shutil.rmtree, self.state, True)
        self.root = os.path.join(self.state, "mods")
        self.put("a", {"paths": ["/api/files", "/api/send"]}, {"keys": ["todo"], "features": ["x.y"]})
        self.put("b", {"paths": ["/api/files", "/api/b"]})
        self.cfg = {"modules": {"paths": [self.root], "enabled": ["a", "b"]}}
        self.hub = Hub(self.state, types.SimpleNamespace(), registry_dir=self.state)

    def put(self, name, http, snapshot=None):
        d = os.path.join(self.root, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "module.json"), "w") as f:
            json.dump(man(name=name, http=http, **({"snapshot": snapshot} if snapshot else {})), f)

    def events(self, kind):
        with open(os.path.join(self.state, "journal.jsonl"), encoding="utf-8") as f:
            return [e for e in map(json.loads, f) if e["type"] == kind]

    def test_want_carries_claims_and_losses_go_to_the_journal_once(self):
        self.hub.modules.reload(self.cfg, {})
        want = self.hub.modules.sync()
        self.assertEqual(want["a"]["claims"], {"keys": ["todo"], "features": ["x.y"]})
        self.assertEqual(want["b"]["claims"], {"paths": ["/api/b"]})
        self.assertEqual(modrun.wants(self.state)["a"]["claims"], want["a"]["claims"])
        for _ in range(3):
            self.hub.modules.tick()
        got = {e["data"]["module"]: e["data"]["lost"] for e in self.events("module.claims")}
        self.assertEqual(len(self.events("module.claims")), 2)
        self.assertEqual(len(got["a"]), 2)   # /api/send is the core's, /api/files is contested with b
        self.assertTrue(any("/api/send" in n for n in got["a"]))
        self.assertTrue(any("/api/files" in n for n in got["b"]))
        # the owner chose — new lines: the chosen one loses nothing, the other gave the path up
        self.cfg["modules"]["use"] = {"paths": {"/api/files": "b"}}
        self.hub.modules.reload(self.cfg, {})
        self.hub.modules.tick()
        self.hub.modules.tick()
        last = {e["data"]["module"]: e["data"]["lost"] for e in self.events("module.claims")}
        self.assertEqual(len(self.events("module.claims")), 4)
        self.assertEqual(last["b"], [])
        self.assertEqual(modrun.wants(self.state)["b"]["claims"], {"paths": ["/api/files", "/api/b"]})
        # hub modules: what the module did not get
        table = modules.rows(self.cfg, self.state, self.hub.state)
        row = next(r for r in table if r["name"] == "a")
        self.assertTrue(any("/api/send" in n for n in row["lost"]), row)
        self.assertIn("/api/send", modules.text(table))

    def test_module_claims_is_written_by_the_core_only(self):
        st = reducer.empty()
        ev = {"seq": 1, "ts": 1.0, "type": "module.claims", "entity": "a", "data": {"module": "a", "lost": ["x"],
                                                                                    "channel": "core"}}
        st2, _, why = reducer.reduce(st, ev)
        self.assertIsNone(why)
        self.assertEqual(st2.get("modules"), st.get("modules"))   # only a history line
        _, _, why = reducer.reduce(st, {**ev, "data": {"module": "a", "lost": [], "channel": "plugin"}})
        self.assertIsNotNone(why)


class Paths(ModHttp):
    """A module path on the page port: the same entry as /api/mod/<name>/…; the module receives /api/mod/<name><path>."""

    def claim(self, name="fake", **claims):
        path = os.path.join(modrun.run_dir(self.state), f"{name}.want.json")
        with open(path) as f:
            w = json.load(f)
        modrun._write(path, {**w, "claims": claims})

    def test_declared_path_reaches_its_module_with_the_owner_entry(self):
        self.module()
        self.claim(paths=["/api/echo", "/api/usage/cycles"])
        st, body, _ = self.api("POST", "/api/echo?n=3&before=x", b'{"a": 1}', {"Content-Type": "application/json"})
        self.assertEqual(st, 200)
        got = json.loads(body)
        self.assertEqual((got["method"], got["path"], got["size"]), ("POST", "/api/mod/fake/api/echo?n=3&before=x", 8))
        self.assertEqual(got["headers"]["x-hub-owner"], "1")
        self.assertEqual(got["headers"]["x-hub-secret"], modrun.run_of(self.state, "fake")["secret"])
        self.assertEqual(json.loads(self.api("GET", "/api/echo")[1])["path"], "/api/mod/fake/api/echo")
        self.assertEqual(self.api("DELETE", "/api/echo")[0], 200)
        self.assertEqual(self.api("GET", "/api/usage/cycles?window=five_hour")[0], 404)   # the module's answer: no such handler
        n = len(self.requests_seen())
        self.assertEqual(self.req("GET", "/api/echo")[0], 403)                          # no X-Hub
        self.assertEqual(self.req("POST", "/api/echo", b"{}")[0], 403)
        self.assertEqual(self.req("GET", "/api/echo", headers={"X-Hub": "1"}, host="evil.example:80")[0], 403)
        self.assertEqual(len(self.requests_seen()), n)

    def test_other_paths_stay_with_the_core(self):
        self.module()
        # a want that names core paths (an old file, someone's edit): the page does not hand them to the module
        self.claim(paths=["/api/send", "/api/snapshot", "/api/answer", "/api/upload/raw"], views=["/api/stream"])
        self.web.tail.poll()
        self.assertEqual(self.api("GET", "/api/snapshot")[0], 200)
        st, body, _ = self.api("POST", "/api/send", json.dumps({"to": "nobody", "text": "x"}).encode(),
                               {"Content-Type": "application/json"})
        self.assertNotIn(b"/api/mod/", body)
        self.assertEqual(self.api("POST", "/api/answer", b"{}", {"Content-Type": "application/json"})[0] // 100, 4)
        self.assertEqual(self.api("GET", "/api/nothing")[:2], (404, '{"error":"No such page"}'.encode()))
        self.assertEqual(self.api("POST", "/api/echo", b"{}")[0] // 100, 4)   # not named — not the module's
        self.assertEqual(self.requests_seen(), [])

    def test_not_ready_module_is_503_and_unloaded_one_is_unknown(self):
        self.module(ready=False)
        self.claim(paths=["/api/echo"])
        st, body, _ = self.api("GET", "/api/echo")
        self.assertEqual(st, 503)
        self.assertIn("not ready", json.loads(body)["error"])
        os.unlink(os.path.join(modrun.run_dir(self.state), "fake.want.json"))   # hubd removed the module
        self.assertEqual(self.api("GET", "/api/echo")[:2], (404, '{"error":"No such page"}'.encode()))
        self.assertEqual(self.requests_seen(), [])

    def test_declared_view_is_sandboxed_get_only_and_carries_no_owner_word(self):
        self.module()
        self.claim(views=["/api/page"])
        st, body, r = self.req("GET", "/api/page?path=/x/y.html", headers={"X-Hub-Owner": "1"})   # no X-Hub
        self.assertEqual(st, 200)
        self.assertIn(b"<script>", body)
        self.assertIn(modproxy.VIEW_CSP, r.headers.get_all("Content-Security-Policy"))
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")
        seen = self.requests_seen()[-1]
        self.assertEqual(seen["path"], "/mod/fake/view/api/page?path=/x/y.html")
        self.assertNotIn("x-hub-owner", seen["headers"])
        n = len(self.requests_seen())
        st, _, r = self.api("POST", "/api/page", b"{}")
        self.assertEqual((st, r.getheader("Allow")), (405, "GET"))
        self.assertEqual(self.req("GET", "/api/page", host="evil.example:80")[0], 403)
        self.assertEqual(len(self.requests_seen()), n)


class Snapshot(unittest.TestCase):
    """A module's keys and features are in the snapshot while it is ready (answered hello in this launch, not
    suspended); a key is shown only if this launch wrote it. A core key is never given to a module."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="claims-snap-")
        self.addCleanup(shutil.rmtree, self.state, True)
        self.web = Web(self.state)
        self.web.config = lambda: {}
        self.dir = os.path.join(self.state, "mods", "todo")
        os.makedirs(self.dir)
        with open(os.path.join(self.dir, "module.json"), "w") as f:
            json.dump({"name": "todo", "version": "1.0.0", "api": "^2.4", "run": ["x"]}, f)
        os.makedirs(modrun.run_dir(self.state))
        self.want({"keys": ["todo", "todo_done", "voice"], "features": ["session.rename", "upload.raw"]})
        modrun._write(os.path.join(modrun.run_dir(self.state), "todo.run.json"),
                      {"pid": 1, "port": 1, "secret": "s", "launch": "L2", "started": 1})
        self.mods = {"todo": {"todo": {"value": [{"id": "q1"}], "rev": 7, "launch": "L2"},
                              "todo_done": {"value": [], "rev": 5, "launch": "L1"},   # previous launch
                              "voice": {"value": "foreign", "rev": 8, "launch": "L2"},
                              "other": {"value": 1, "rev": 9, "launch": "L2"}}}   # not named in snapshot.keys

    def want(self, claims):
        modrun._write(os.path.join(modrun.run_dir(self.state), "todo.want.json"),
                      {"name": "todo", "cmd": ["x"], "cwd": self.dir, "version": "1.0.0", "order": 0, "key": "k",
                       "claims": claims})

    def snap(self, rec):
        self.web.tail.records = {"todo": rec} if rec is not None else {}
        return self.web.with_clock({"seq": 9, "mods": self.mods})

    def test_keys_and_features_while_ready(self):
        snap = self.snap({"ready": "L2", "version": "1.0.0", "state": "ok"})
        self.assertEqual(snap["todo"], [{"id": "q1"}])
        self.assertNotIn("todo_done", snap)              # the previous launch's value: absent until the first write
        self.assertNotIn("other", snap)
        self.assertNotEqual(snap["voice"], "foreign")      # a core key stays the core's
        self.assertEqual(snap["features"].count("upload.raw"), 1)
        self.assertIn("session.rename", snap["features"])
        # mods — as before: {value, rev}
        self.assertEqual(snap["mods"]["todo"]["todo"], {"value": [{"id": "q1"}], "rev": 7})

    def test_nothing_while_not_ready(self):
        for rec in (None, {"version": "1.0.0", "state": "ok"},                        # has not answered hello yet
                    {"ready": "L1", "version": "1.0.0", "state": "ok"},              # the previous launch was ready
                    {"ready": "L2", "version": "1.0.0", "state": "suspended", "until": time.time() + 600},
                    {"ready": "L2", "version": "1.0.0", "state": "error"},
                    {"ready": "L2", "version": "1.0.0", "state": "blocked"}):
            snap = self.snap(rec)
            self.assertNotIn("todo", snap, rec)
            self.assertNotIn("session.rename", snap["features"], rec)
        os.unlink(os.path.join(modrun.run_dir(self.state), "todo.want.json"))   # disabled
        snap = self.snap({"ready": "L2", "version": "1.0.0", "state": "ok"})
        self.assertNotIn("todo", snap)

    def test_launch_of_a_write_is_kept_and_checked(self):
        st = reducer.empty()
        evs = [("module.ready", "todo", {"module": "todo", "version": "1", "launch": "L2", "points": [],
                                         "channel": "core"}),
               ("mod.todo.set", None, {"key": "todo", "value": [1], "launch": "L2"}),
               ("mod.todo.set", None, {"key": "old", "value": 1})]
        for i, (t, e, d) in enumerate(evs, 1):
            st, _, why = reducer.reduce(st, {"seq": i, "ts": float(i), "type": t, "entity": e, "data": d})
            self.assertIsNone(why, t)
        self.assertEqual(st["mods"]["todo"]["todo"], {"value": [1], "rev": 2, "launch": "L2"})
        self.assertEqual(st["mods"]["todo"]["old"], {"value": 1, "rev": 3})
        for bad in (5, "x" * 41, ""):
            _, _, why = reducer.reduce(st, {"seq": 9, "ts": 9.0, "type": "mod.todo.set", "entity": None,
                                            "data": {"key": "todo", "value": 1, "launch": bad}})
            self.assertIsNotNone(why, bad)


class Stream(unittest.TestCase):
    """The page stream: a module's key and feature arrive as a patch when the module is ready and has written, and
    leave as a patch (_gone) when the module crashed or was removed — even without a new journal event."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="claims-stream-")
        self.addCleanup(shutil.rmtree, self.state, True)
        self.j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        self.dir = os.path.join(self.state, "mods", "todo")
        os.makedirs(self.dir)
        with open(os.path.join(self.dir, "module.json"), "w") as f:
            json.dump({"name": "todo", "version": "1.0.0", "api": "^2.4", "run": ["x"]}, f)
        os.makedirs(modrun.run_dir(self.state))
        modrun._write(os.path.join(modrun.run_dir(self.state), "todo.want.json"),
                      {"name": "todo", "cmd": ["x"], "cwd": self.dir, "version": "1.0.0", "order": 0, "key": "k",
                       "claims": {"keys": ["todo"], "features": ["todo.list"]}})
        modrun._write(os.path.join(modrun.run_dir(self.state), "todo.run.json"),
                      {"pid": 1, "port": 1, "secret": "s", "launch": "L1", "started": 1})
        self.j.append("session.requested", "a", {"session_id": "s", "cwd": "/w"})
        self.web = Web(self.state)
        self.web.config = lambda: {}
        self.server = self.web.serve(port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.web.stop.set(), self.server.shutdown(), self.server.server_close()))
        self.web.tail.poll()

    def events(self):
        """The /api/stream reader: [(event, data)] as they arrive."""
        got, done = [], threading.Event()

        def read():
            c = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=30)
            c.request("GET", "/api/stream", headers={"Host": f"127.0.0.1:{self.server.server_address[1]}"})
            r = c.getresponse()
            kind = None
            while not done.is_set():
                line = r.fp.readline()
                if not line:
                    break
                line = line.decode().rstrip("\n")
                if line.startswith("event: "):
                    kind = line[7:]
                elif line.startswith("data: "):
                    got.append((kind, json.loads(line[6:])))
            c.close()
        threading.Thread(target=read, daemon=True).start()
        self.addCleanup(done.set)
        return got

    def until(self, got, pred, timeout=10, after=0):
        end = time.time() + timeout
        while time.time() < end:
            hit = [d for _, d in got[after:] if pred(d)]
            if hit:
                return hit[-1]
            time.sleep(0.05)
        self.fail(f"gave up waiting: {got[-3:]}")

    def test_keys_come_and_go_by_patch(self):
        with mock.patch("modules.web.server.STREAM_WAIT", 0.5):
            got = self.events()
            first = self.until(got, lambda d: "sessions" in d)
            self.assertNotIn("todo", first)
            self.assertNotIn("todo.list", first["features"])
            self.j.append("module.ready", "todo", {"module": "todo", "version": "1.0.0", "points": [], "launch": "L1",
                                                   "channel": "core"})
            self.j.append("mod.todo.set", None, {"key": "todo", "value": ["q1"], "launch": "L1"})
            self.web.tail.poll()
            patch = self.until(got, lambda d: "todo" in d)
            self.assertEqual(patch["todo"], ["q1"])
            self.assertIn("todo.list", patch["features"])
            # the process crashed: hub-up writes the failure with the launch — the key and feature leave by patch
            self.j.append("module.error", "todo", {"module": "todo", "hook": "process", "kind": "exit", "error": "x",
                                                   "launch": "L1", "uptime": 100, "channel": "core"})
            self.web.tail.poll()
            gone = self.until(got, lambda d: "todo" in (d.get("_gone") or []))
            self.assertNotIn("todo.list", gone["features"])
            # ready again — and the key of this launch
            modrun._write(os.path.join(modrun.run_dir(self.state), "todo.run.json"),
                          {"pid": 1, "port": 1, "secret": "s", "launch": "L2", "started": 2})
            self.j.append("module.ready", "todo", {"module": "todo", "version": "1.0.0", "points": [], "launch": "L2",
                                                   "channel": "core"})
            self.j.append("mod.todo.set", None, {"key": "todo", "value": ["q2"], "launch": "L2"})
            self.web.tail.poll()
            self.assertEqual(self.until(got, lambda d: d.get("todo") == ["q2"])["todo"], ["q2"])
            # hubd removed the module (the want file) — no journal events, yet the patch arrives
            n = len(got)
            os.unlink(os.path.join(modrun.run_dir(self.state), "todo.want.json"))
            gone = self.until(got, lambda d: "todo" in (d.get("_gone") or []), after=n)
            self.assertNotIn("todo.list", gone["features"])


class Example(unittest.TestCase):
    """The examples/module-client-api sample: hubd starts it, the page serves its path and view, and its key and
    feature are in the snapshot while it is ready."""

    def test_the_manifest_is_valid(self):
        self.assertEqual(modules.read(EXAMPLE, "path")["errors"], [])

    def test_the_example_answers_on_its_path_and_shows_in_the_snapshot(self):
        state = tempfile.mkdtemp(prefix="claims-example-")
        self.addCleanup(shutil.rmtree, state, True)
        hub = Hub(state, types.SimpleNamespace(), registry_dir=state)
        cfg = {"modules": {"paths": [EXAMPLE], "enabled": ["client-api"]}}
        hub.modules.reload(cfg, {})
        want = hub.modules.sync()["client-api"]
        self.assertEqual(want["claims"], {"paths": ["/api/notes"], "views": ["/api/notes-view"], "keys": ["notes"],
                                          "features": ["notes"]})

        def spawn(cmd, cwd, env):   # like hub-up; python3 means the same interpreter (Windows)
            p = subprocess.Popen([sys.executable] + cmd[1:], cwd=cwd, env={**env, "PYTHONPATH": ROOT},
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.addCleanup(lambda: (kill(p.pid), p.wait(10)))
            return p

        def start():
            proc = modrun.launch(state, "client-api", want, spawn)
            launch = modrun.run_of(state, "client-api")["launch"]
            end = time.time() + 15
            while time.time() < end and (hub.state.get("modules") or {}).get("client-api", {}).get("ready") != launch:
                hub.modules.tick()
                time.sleep(0.05)
            self.assertEqual(hub.state["modules"]["client-api"].get("ready"), launch)
            return proc
        proc = start()
        web = Web(state)
        web.config = lambda: {}
        server = web.serve(port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (web.stop.set(), server.shutdown(), server.server_close()))
        port = server.server_address[1]

        def req(method, path, body=None, headers=None):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request(method, path, body, {"Host": f"127.0.0.1:{port}", **(headers or {})})
            r = c.getresponse()
            data = r.read()
            c.close()
            return r.status, data, r
        web.tail.poll()
        snap = json.loads(req("GET", "/api/snapshot")[1])
        self.assertNotIn("notes", snap)   # ready, but has not written in this launch yet
        self.assertIn("notes", snap["features"])
        st, body, _ = req("POST", "/api/notes", json.dumps({"text": "buy bread"}).encode(),
                          {"X-Hub": "1", "Content-Type": "application/json"})
        self.assertEqual((st, json.loads(body)), (200, {"notes": ["buy bread"]}))
        hub.ingest()   # hubd: the module's fact goes from the mailbox into the journal
        web.tail.poll()
        snap = json.loads(req("GET", "/api/snapshot")[1])
        self.assertEqual(snap["notes"], ["buy bread"])
        st, body, r = req("GET", "/api/notes-view")
        self.assertEqual(st, 200)
        self.assertIn("buy bread".encode(), body)
        self.assertIn(modproxy.VIEW_CSP, r.headers.get_all("Content-Security-Policy"))
        self.assertEqual(req("GET", "/api/notes")[0], 403)   # no X-Hub
        # a new launch: the previous key is not shown until the module writes it again (@hubmod.on_ready)
        kill(proc.pid)
        proc.wait(10)
        start()
        web.tail.poll()
        snap = json.loads(req("GET", "/api/snapshot")[1])
        self.assertIn("notes", snap["features"])   # the new launch is ready,
        self.assertNotIn("notes", snap)            # but hubd has not accepted its write yet — the previous key is hidden
        end = time.time() + 15
        while time.time() < end and "notes" not in json.loads(req("GET", "/api/snapshot")[1]):
            hub.ingest()
            web.tail.poll()
            time.sleep(0.05)
        self.assertEqual(json.loads(req("GET", "/api/snapshot")[1])["notes"], ["buy bread"])


if __name__ == "__main__":
    unittest.main()
