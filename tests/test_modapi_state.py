"""A core module's own state (core/modstate.py, Module API spec §2.3, slice 6): mod.<name>.set/del with if_rev in the
common fold, limits, refusal, journal replay; a repeated fact with dedup_key; mods in the page and client snapshot only
for loaded modules; a page plugin from a module. Without modules the snapshot is as before."""
import tests  # noqa: F401 — ignore this machine's settings
import json
import os
import shutil
import tempfile
import unittest

from core import forms, journal, modrun, modstate, modules, reducer, views
from core import cutout

# the Claude account pool features: listed only where the pool is in the tree (not on the open flavor)
POOL = ["accounts", "accounts.use"] if cutout.present("profile") else []

KNOWN = {"seq": 0, "ts": 0, "type": "module.ready", "entity": "m",
         "data": {"module": "m", "version": "1.0.0", "points": [], "launch": "L1", "channel": "core"}}


class Fold(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ready("m")

    def put(self, kind, ts=100, entity=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts, "type": kind, "entity": entity,
                                                   "data": data})
        return why

    def ready(self, name):
        self.assertIsNone(self.put("module.ready", module=name, version="1.0.0", points=[], launch="L1",
                                   channel="core"))

    def mods(self, name="m"):
        return (self.st.get("mods") or {}).get(name)

    def test_set_keeps_value_and_the_event_number_as_revision(self):
        self.assertNotIn("mods", self.st)   # no key before the first write
        self.assertIsNone(self.put("mod.m.set", key="todo", value={"items": ["a"]}))
        self.assertEqual(self.mods(), {"todo": {"value": {"items": ["a"]}, "rev": self.seq}})
        self.assertIsNone(self.put("mod.m.set", key="todo", value=None))   # null is a value too
        self.assertEqual(self.mods()["todo"], {"value": None, "rev": self.seq})

    def test_if_rev_writes_only_on_the_revision_it_names(self):
        self.put("mod.m.set", key="k", value=1)
        rev = self.seq
        self.assertIsNone(self.put("mod.m.set", key="k", value=2, if_rev=rev))
        why = self.put("mod.m.set", key="k", value=3, if_rev=rev)   # a lost update: the key has already moved ahead
        self.assertIn(f"revision {self.seq - 1}", why)
        self.assertEqual(self.mods()["k"]["value"], 2)
        self.assertIsNotNone(self.put("mod.m.set", key="k", value=4, if_rev=0))   # 0 is only valid if the key does not exist
        self.assertIsNone(self.put("mod.m.set", key="new", value=1, if_rev=0))
        self.assertIsNotNone(self.put("mod.m.set", key="other", value=1, if_rev=5))
        for bad in (-1, True, "3", 1.5):
            self.assertIn("if_rev", self.put("mod.m.set", key="k", value=1, if_rev=bad))

    def test_del_and_recreated_key_gets_a_new_revision(self):
        self.put("mod.m.set", key="k", value=1)
        old = self.seq
        self.assertIsNotNone(self.put("mod.m.del", key="k", if_rev=old + 100))
        self.assertIsNone(self.put("mod.m.del", key="k", if_rev=old))
        self.assertEqual(self.st["mods"], {})   # the last key is gone — no module branch
        self.assertIsNone(self.put("mod.m.del", key="k"))   # deleting a missing key without if_rev — nothing happens
        self.put("mod.m.set", key="k", value=2)
        self.assertNotEqual(self.mods()["k"]["rev"], old)
        self.assertIsNotNone(self.put("mod.m.set", key="k", value=3, if_rev=old))   # an old revision does not fit

    def test_only_its_own_branch_and_only_a_loaded_module(self):
        self.ready("other")
        self.put("mod.other.set", key="k", value="theirs")
        self.put("mod.m.set", key="k", value="mine")
        self.assertEqual(self.st["mods"]["other"]["k"]["value"], "theirs")
        self.assertIsNotNone(self.put("mod.m.set", entity="other", key="k", value="x"))   # a foreign name in entity
        self.assertIn("not loaded", self.put("mod.ghost.set", key="k", value=1))   # did not answer hello
        self.assertIsNotNone(self.put("mod.Bad.set", key="k", value=1))
        self.assertIsNotNone(self.put("mod.m", key="k", value=1))
        self.assertIsNone(self.put("mod.m.closed", by="owner"))   # its own fact is history, it does not change the state
        self.assertEqual(self.mods(), {"k": {"value": "mine", "rev": self.seq - 5}})

    def test_bad_key_or_value_is_refused(self):
        for key in ("", "Big", "a" * 65, "with space", 5, None):
            self.assertIn("key", self.put("mod.m.set", key=key, value=1), key)
        self.assertIn("value", self.put("mod.m.set", key="k"))
        self.assertIn("JSON", self.put("mod.m.set", key="k", value=float("nan")))
        self.assertIn("JSON", self.put("mod.m.set", key="k", value=[float("inf")]))
        self.assertNotIn("mods", self.st)

    def test_limits(self):
        big = "x" * (modstate.MAX_MODULE - 100)
        self.assertIsNone(self.put("mod.m.set", key="a", value=big))
        why = self.put("mod.m.set", key="b", value="y" * 200)   # the module limit applies to its whole state
        self.assertIn(str(modstate.MAX_MODULE), why)
        self.assertIsNone(self.put("mod.m.set", key="a", value="small"))   # a replacement is counted without the old value
        self.assertIsNone(self.put("mod.m.set", key="b", value="y" * 200))
        for i in range(modstate.MAX_KEYS - 2):
            self.assertIsNone(self.put("mod.m.set", key=f"k{i}", value=i))
        self.assertIn(str(modstate.MAX_KEYS), self.put("mod.m.set", key="one-more", value=1))
        self.assertIsNone(self.put("mod.m.set", key="k0", value="still fine"))   # a replacement is not a new key

    def test_a_module_holds_256_kb(self):
        self.assertEqual((modstate.MAX_MODULE, modstate.MAX_ALL), (256 * 1024, 512 * 1024))   # slice 11: the measurement is in decisions
        self.assertIsNone(self.put("mod.m.set", key="a", value="x" * (256 * 1024 - 100)))

    def test_limit_on_all_modules(self):
        part, names = modstate.MAX_MODULE - 1000, "abcdfghi"[:modstate.MAX_ALL // modstate.MAX_MODULE]
        for n in names:
            self.ready(n)
            self.assertIsNone(self.put(f"mod.{n}.set", key="k", value="x" * part))
        self.ready("e")
        why = self.put("mod.e.set", key="k", value="x" * (modstate.MAX_ALL - len(names) * (part + 3)))
        self.assertIn(str(modstate.MAX_ALL), why)
        self.assertNotIn("e", self.st["mods"])
        self.assertIsNone(self.put("mod.e.set", key="k", value="x" * 100))

    def test_replay_of_the_journal_gives_the_same_state(self):
        d = tempfile.mkdtemp(prefix="modstate-")
        self.addCleanup(shutil.rmtree, d, True)
        j = journal.Journal(os.path.join(d, "journal.jsonl"))
        st = reducer.empty()

        def put(kind, entity=None, **data):
            nonlocal st
            ev = j.append(kind, entity, data)
            st, _, why = reducer.reduce(st, ev)
            if why:   # like hubd: the refusal goes on the next line
                rej = j.append("core.rejected", entity, {"of": ev["seq"], "type": kind, "reason": why})
                st, _, _ = reducer.reduce(st, rej)
            return why
        put("module.ready", "m", module="m", version="1.0.0", points=[], launch="L", channel="core")
        put("mod.m.set", key="a", value=1)
        rev = st["mods"]["m"]["a"]["rev"]
        put("mod.m.set", key="b", value={"x": [1, 2]})
        self.assertIsNotNone(put("mod.m.set", key="a", value=9, if_rev=rev + 1))
        put("mod.m.set", key="a", value=2, if_rev=rev)
        put("mod.m.set", key="huge", value="x" * modstate.MAX_MODULE)
        put("mod.m.del", key="b")
        put("mod.m.set", key="c", value="c", dedup_key="k1")
        self.assertIsNotNone(put("mod.m.set", key="c", value="c2", dedup_key="k1"))
        again = reducer.fold(reducer.empty(), journal.read(j.path))
        self.assertEqual(again["mods"], st["mods"])
        self.assertEqual(again["mods"], {"m": {"a": {"value": 2, "rev": again["mods"]["m"]["a"]["rev"]},
                                               "c": {"value": "c", "rev": again["mods"]["m"]["c"]["rev"]}}})
        self.assertEqual(again["mod_dedup"], st["mod_dedup"])
        r = views.Reader()   # the page reader: the same journal with verdicts gives the same state
        r.feed_all(journal.read(j.path))
        self.assertEqual(r.state["mods"], st["mods"])


class Hubd(unittest.TestCase):
    """A real hubd and a module process (a rig): the module's facts from the inbox go into the journal and the state; a new
    hubd on the same folder (a core restart) brings up the same state."""

    def test_module_facts_through_the_spool_survive_a_restart(self):
        from core import spool
        from core.daemon import Hub
        from tests.fakes.modstand import Stand
        s = Stand(self)
        box = os.path.join(s.state_dir, "spool")
        spool.put(box, "mod.fake.set", None, {"key": "early", "value": 1}, src="module")
        s.hub.ingest()   # before hello: the module is not loaded yet — a refusal with a reason
        self.assertIn("not loaded", s.events("core.rejected")[-1]["data"]["reason"])
        s.launch()
        s.until_ready()
        spool.put(box, "mod.fake.set", None, {"key": "todo", "value": {"n": 1}}, src="module")
        spool.put(box, "mod.fake.set", None, {"key": "todo", "value": {"n": 2}, "if_rev": 1}, src="module")
        s.hub.ingest()
        self.assertEqual(s.hub.state["mods"]["fake"]["todo"]["value"], {"n": 1})
        self.assertIn("revision", s.events("core.rejected")[-1]["data"]["reason"])
        s.hub.save_state()
        again = Hub(s.state_dir, s.hub.host, registry_dir=s.state_dir)
        self.assertEqual(again.state["mods"], s.hub.state["mods"])


class Slice(unittest.TestCase):
    """A module process reads only its own: hubd writes a slice (its own keys, the weight of others', readiness) to the file
    modrun/<name>.state.json, and hubmod.state and @on_ready take it instead of the whole state.json (on a hub with a big
    journal that is a +100 MB peak per module process). No file (hubd has not written yet) — the hub state, as before."""

    def setUp(self):
        from tests.fakes.modstand import Stand
        self.s = Stand(self)
        self.box = os.path.join(self.s.state_dir, "spool")
        env = {"HUB_MODULE": "fake", "HUB_STATE_DIR": self.s.state_dir, "HUB_MODULE_LAUNCH": None}
        self.addCleanup(lambda old={k: os.environ.get(k) for k in env}: [
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v) for k, v in old.items()])
        for k, v in env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def put(self, kind, **data):
        from core import spool
        spool.put(self.box, kind, None, data, src="module")
        self.s.hub.ingest()

    def path(self, name="fake"):
        return os.path.join(modrun.run_dir(self.s.state_dir), f"{name}.state.json")

    def file(self, name="fake"):
        with open(self.path(name), encoding="utf-8") as f:
            return json.load(f)

    def no_hub_state(self):
        """Reading the whole hub state fails the test."""
        from unittest import mock

        def whole(*a, **k):
            raise AssertionError("the module process reads the whole hub state")
        p = mock.patch.object(journal, "load_state", whole)
        p.start()
        self.addCleanup(p.stop)

    def test_the_module_reads_its_slice_and_not_the_hub_state(self):
        from core import hubmod
        self.s.hub.state["blob"] = "x" * (2 << 20)   # a big foreign state
        self.s.launch()
        self.s.until_ready()
        self.s.hub.record("module.ready", "other", {"module": "other", "version": "1.0.0", "points": [],
                                                    "launch": "O", "channel": "core"})
        self.put("mod.fake.set", key="todo", value={"n": 1})
        self.s.hub.record("mod.other.set", None, {"key": "k", "value": "hush other"})
        got = self.file()
        self.assertEqual(got, modstate.slice(journal.load_state(self.s.state_dir), "fake"))
        self.assertLess(os.path.getsize(self.path()), 2048)
        self.assertNotIn("hush", json.dumps(got, ensure_ascii=False))   # foreign state — only its weight
        self.assertEqual(got["ready"], self.s.rec()["ready"])
        self.no_hub_state()
        st = hubmod._State()
        self.assertEqual((st.get("todo"), st.rev("todo")), ({"n": 1}, self.s.hub.state["mods"]["fake"]["todo"]["rev"]))
        self.assertEqual(st._others, modstate.size("k", "hush other"))
        os.environ["HUB_MODULE_LAUNCH"] = got["ready"]
        called = []
        hubmod._when_ready([lambda: called.append(1)])
        self.assertEqual(called, [1])

    def test_the_slice_follows_every_change_of_the_module_state(self):
        self.s.launch()
        self.s.until_ready()
        self.put("mod.fake.set", key="a", value=1)
        rev = self.s.hub.state["mods"]["fake"]["a"]["rev"]
        self.put("mod.fake.set", key="b", value=[1, 2])
        self.put("mod.fake.set", key="a", value=9, if_rev=rev + 1)   # refusal: the slice does not change
        self.assertEqual(self.file()["mods"]["a"]["value"], 1)
        self.put("mod.fake.del", key="b")
        self.put("mod.fake.del", key="a")
        self.assertEqual(self.file(), modstate.slice(journal.load_state(self.s.state_dir), "fake"))
        self.assertEqual(self.file()["mods"], {})
        old = self.file()["ready"]
        self.s.cleanup()
        self.s.launch()   # a new launch: the readiness in the slice is its own
        self.s.until_ready()
        self.assertNotEqual(self.file()["ready"], old)
        self.assertEqual(self.file()["ready"], self.s.rec()["ready"])

    def test_a_wanted_module_gets_its_slice_before_any_record(self):
        self.s.hub.modules.tick()   # want is written — and the slice too, even an empty one: the process has no reason to go to state.json
        self.assertEqual(self.file(), {"ready": None, "mods": {}, "others": 0})

    def test_a_slice_of_an_earlier_life_is_not_read(self):
        """A slice left by the previous hubd (before a rollback, crashed between the line and the slice) is erased by the new
        hubd first thing — both one that would not change and one whose module is gone; a module no longer held has no slice."""
        from core.daemon import Hub
        os.makedirs(modrun.run_dir(self.s.state_dir), exist_ok=True)
        for n in ("fake", "gone"):
            modrun._write(self.path(n), {"ready": "old", "mods": {"a": {"value": "old", "rev": 1}}, "others": 0})
        hub = Hub(self.s.state_dir, self.s.hub.host, registry_dir=self.s.state_dir)
        hub.modules.reload(self.s.cfg, {})
        hub.modules.sync()
        hub.modules.publish()
        self.assertEqual(self.file(), {"ready": None, "mods": {}, "others": 0})
        self.assertFalse(os.path.exists(self.path("gone")))
        hub.modules.reload({"modules": {"paths": [self.s.root], "enabled": []}}, {})
        hub.modules.tick()
        self.assertFalse(os.path.exists(self.path()))

    def test_without_the_slice_the_hub_state_as_before(self):
        from core import hubmod
        self.s.launch()
        self.s.until_ready()
        self.put("mod.fake.set", key="a", value=1)
        self.s.hub.save_state()
        os.unlink(self.path())   # an earlier hubd version: no file
        self.assertEqual(hubmod._State().get("a"), 1)


class Dedup(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        for n in ("m", "n"):
            self.put("module.ready", module=n, version="1.0.0", points=[], launch="L", channel="core")

    def put(self, kind, ts=100, entity=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts, "type": kind, "entity": entity,
                                                   "data": data})
        return why

    def test_repeat_of_an_accepted_fact_is_refused_with_its_number(self):
        self.assertIsNone(self.put("mod.m.set", key="k", value=1, dedup_key="c-1"))
        first = self.seq
        why = self.put("mod.m.set", key="k", value=2, dedup_key="c-1", ts=200)
        self.assertIn(f"event {first}", why)
        self.assertEqual(forms.code_of(why), forms.DUPLICATE)   # the code next to the text: a repeat is recognized by it (2.6)
        self.assertEqual(self.st["mods"]["m"]["k"]["value"], 1)
        self.assertIsNone(self.put("mod.n.set", key="k", value=2, dedup_key="c-1"))   # another module has its own key
        self.assertIsNone(self.put("mod.m.set", key="k", value=3, dedup_key="c-1", ts=100 + modstate.DEDUP_WINDOW))

    def test_only_an_accepted_fact_takes_the_key(self):
        self.assertIsNotNone(self.put("mod.m.set", key="k", value=1, if_rev=7, dedup_key="c-2"))
        self.assertIsNone(self.put("mod.m.set", key="k", value=1, dedup_key="c-2"))

    def test_owner_word_from_a_module_handle_gets_the_message_number(self):
        self.put("session.requested", "a", session_id="s", cwd="/w")
        word = {"to": "a", "text": "hello", "channel": "mod:m", "owner": True, "dedup_key": "client-7"}
        self.assertIsNone(self.put("message.received", **word))
        mid = f"m{self.seq}"
        self.assertIn(mid, self.st["messages"])
        before = len(self.st["messages"])
        why = self.put("message.received", **word)
        self.assertIn(f"already accepted — {mid}", why)
        self.assertEqual(len(self.st["messages"]), before)

    def test_a_repeat_is_refused_before_the_decision_of_a_core_write_module(self):
        # a repeat with the same dedup_key is refused with the number of the accepted one, whatever the module with core.write on the same line decides
        # (reducer.reduce: dedup_check before the fold decision): the client learns the accepted mN, not the foreign module's reason
        self.put("session.requested", "a", session_id="s", cwd="/w")
        word = {"to": "a", "text": "hello", "channel": "mod:m", "owner": True, "dedup_key": "client-8"}
        self.assertIsNone(self.put("message.received", **word))
        mid = f"m{self.seq}"
        for fold in ({"mod": "n", "mode": "reject", "reason": "not now"}, {"mod": "n", "mode": "replace", "patch": []}):
            self.seq += 1
            self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 100, "type": "message.received",
                                                       "entity": None, "data": word, "fold": fold})
            self.assertIn(f"already accepted — {mid}", why, fold)

    def test_bad_key_is_refused_and_core_facts_ignore_it(self):
        self.assertIn("dedup_key", self.put("mod.m.set", key="k", value=1, dedup_key="x" * 129))
        self.assertIn("dedup_key", self.put("mod.m.set", key="k", value=1, dedup_key=5))
        self.put("session.requested", "a", session_id="s", cwd="/w")
        for _ in range(2):   # not from a module — the key means nothing, as before
            self.assertIsNone(self.put("message.received", to="a", text="x", channel="cli", owner=True,
                                       dedup_key="same"))
        self.assertNotIn("mod_dedup", self.st)

    def test_old_keys_leave_after_the_window_and_over_the_cap(self):
        self.put("mod.m.set", key="k", value=1, dedup_key="old", ts=0)
        self.put("mod.m.set", key="k", value=2, dedup_key="new", ts=modstate.DEDUP_WINDOW + 1)
        self.assertEqual(list(self.st["mod_dedup"]["m"]), ["new"])


class NoModules(unittest.TestCase):
    """Without modules: the state and the snapshot are the same as without the slice."""

    def test_state_and_snapshot_do_not_grow_new_keys(self):
        st = reducer.empty()
        st, _, _ = reducer.reduce(st, {"seq": 1, "ts": 1, "type": "session.requested", "entity": "a",
                                       "data": {"session_id": "s", "cwd": "/w"}})
        self.assertFalse({"mods", "mod_dedup"} & set(st))
        r = views.Reader()
        self.assertNotIn("mods", r.snapshot())
        from modules.web.server import Web
        web = Web(tempfile.mkdtemp(prefix="hubweb-"))
        self.addCleanup(shutil.rmtree, web.state_dir, True)
        web.config = lambda: {}
        snap = web.with_clock(r.snapshot())
        self.assertFalse({"mods", "mod_versions"} & set(snap))
        self.assertEqual(snap["features"], ["upload.raw", *POOL, "send.tasks", "feed.post_by_id", "bus", "about.addresses", "about.lite", "send.app", "setup", "feed.on_behalf", "session.resume", "stream.delta2", "stream.feed", "page.password", "file.thumb"])
        self.assertFalse([e for e in snap["extensions"] if e.get("base")])


class Snapshot(unittest.TestCase):
    """mods in the page and client snapshot are only for loaded modules (hubd holds them: the want files)."""

    def setUp(self):
        from modules.web.server import Web
        self.state = tempfile.mkdtemp(prefix="hubweb-")
        self.addCleanup(shutil.rmtree, self.state, True)
        self.web = Web(self.state)
        self.web.config = lambda: {}
        self.dir = os.path.join(self.state, "mods", "todo")
        os.makedirs(os.path.join(self.dir, "web"))
        self.manifest(web="web/main.js")
        with open(os.path.join(self.dir, "web", "main.js"), "w") as f:
            f.write("export default (hub) => hub.addView({ id: 'todo', title: 'To-do', render() {} });\n")
        with open(os.path.join(self.dir, "web", "style.css"), "w") as f:
            f.write(".x{}")
        with open(os.path.join(self.dir, "main.py"), "w") as f:
            f.write("SECRET = 1\n")

    def manifest(self, **extra):
        with open(os.path.join(self.dir, "module.json"), "w") as f:
            json.dump({"name": "todo", "version": "1.2.0", "api": "^2.1", "run": ["python3", "main.py"], **extra}, f)

    def want(self, name="todo", order=0):
        os.makedirs(modrun.run_dir(self.state), exist_ok=True)
        with open(os.path.join(modrun.run_dir(self.state), f"{name}.want.json"), "w") as f:
            json.dump({"name": name, "cmd": ["python3", "main.py"], "cwd": self.dir, "version": "1.2.0",
                       "order": order, "key": f"k-{name}"}, f)

    def test_loaded_modules_get_mods_versions_and_their_plugin(self):
        mods = {"todo": {"list": {"value": [1], "rev": 5}}, "gone": {"x": {"value": 1, "rev": 3}}}
        snap = self.web.with_clock({"seq": 9, "mods": mods})
        self.assertFalse({"mods", "mod_versions"} & set(snap))   # none is loaded — as without modules
        self.want()
        snap = self.web.with_clock({"seq": 9, "mods": mods})
        self.assertEqual(snap["mods"], {"todo": {"list": {"value": [1], "rev": 5}}})   # a disabled one is not in the snapshot
        self.assertEqual(snap["mod_versions"], {"todo": "1.2.0"})
        self.assertEqual(snap["features"], ["modules", "upload.raw", *POOL, "send.tasks", "feed.post_by_id", "bus", "about.addresses", "about.lite", "send.app", "setup", "feed.on_behalf", "session.resume", "stream.delta2", "stream.feed", "page.password", "file.thumb"])
        self.assertIn({"name": "mod:todo", "main": "main.js", "base": "/mod/todo/web/"}, snap["extensions"])

    def test_module_without_state_still_shows_as_loaded(self):
        self.want()
        snap = self.web.with_clock({"seq": 1})
        self.assertEqual(snap["mods"], {})
        self.assertEqual(snap["mod_versions"], {"todo": "1.2.0"})

    def test_plugin_files_only_from_its_folder_and_only_while_loaded(self):
        self.assertIsNone(modstate.web_file(self.state, "todo", "main.js"))   # not loaded
        self.want()
        self.assertTrue(modstate.web_file(self.state, "todo", "main.js").endswith(os.path.join("web", "main.js")))
        self.assertIsNotNone(modstate.web_file(self.state, "todo", "style.css"))
        for bad in ("../main.py", "../module.json", "", "nope.js", "/etc/passwd"):
            self.assertIsNone(modstate.web_file(self.state, "todo", bad), bad)
        self.assertIsNone(modstate.web_file(self.state, "../todo", "main.js"))

    def test_web_field_is_checked_in_the_manifest(self):
        base = {"name": "x", "version": "1.0.0", "api": "^2.1", "run": ["python3", "m.py"]}
        self.assertEqual(modules.validate({**base, "web": "web/main.js"}), [])
        self.assertEqual(modules.validate({**base, "web": "main.js"}), [])
        for bad in ("/abs/main.js", "../x.js", "web/../../x.js", "web\\main.js", "main.py", "", 5, "c:/x.js"):
            self.assertTrue(modules.validate({**base, "web": bad}), bad)


class Page(unittest.TestCase):
    """The page serves a module plugin at /mod/<name>/web/… and puts mods into /api/snapshot."""

    def setUp(self):
        from tests.test_web import WebTest
        self.h = WebTest("test_page_and_snapshot")
        self.h.setUp()
        self.addCleanup(self.h.tearDown)
        self.dir = os.path.join(self.h.state, "mods", "todo")
        os.makedirs(os.path.join(self.dir, "web"))
        with open(os.path.join(self.dir, "module.json"), "w") as f:
            json.dump({"name": "todo", "version": "1.0.0", "api": "^2.1", "run": ["python3", "main.py"],
                       "web": "web/main.js"}, f)
        with open(os.path.join(self.dir, "web", "main.js"), "w") as f:
            f.write("export default () => {};\n")
        with open(os.path.join(self.dir, "main.py"), "w") as f:
            f.write("SECRET = 1\n")
        os.makedirs(modrun.run_dir(self.h.state))
        with open(os.path.join(modrun.run_dir(self.h.state), "todo.want.json"), "w") as f:
            json.dump({"name": "todo", "cmd": ["python3", "main.py"], "cwd": self.dir, "version": "1.0.0",
                       "order": 0, "key": "k1"}, f)

    def test_plugin_files_and_state_in_the_snapshot(self):
        h = self.h
        h.j.append("module.ready", "todo", {"module": "todo", "version": "1.0.0", "points": [], "launch": "L",
                                            "channel": "core"})
        h.j.append("mod.todo.set", None, {"key": "list", "value": ["a"]})
        h.seed()
        snap = json.loads(h.req("GET", "/api/snapshot")[1])
        self.assertEqual(snap["mods"]["todo"]["list"]["value"], ["a"])
        self.assertEqual(snap["mod_versions"], {"todo": "1.0.0"})
        code, js = h.req("GET", "/mod/todo/web/main.js")
        self.assertEqual(code, 200)
        self.assertIn(b"export default", js)
        for bad in ("/mod/todo/web/../main.py", "/mod/todo/web/../module.json", "/mod/todo/main.py",
                    "/mod/other/web/main.js", "/mod/todo/web/", "/mod/"):
            self.assertEqual(h.req("GET", bad)[0], 404, bad)
        self.assertEqual(h.req("GET", "/mod/todo/web/main.js", host="evil.example")[0], 403)
        with open(os.path.join(self.dir, "web", "intro.mp4"), "wb") as f:   # a plugin's own media (API 1.32)
            f.write(b"0123456789")
        self.assertEqual(h.req("GET", "/mod/todo/web/intro.mp4", headers={"Range": "bytes=2-4"}), (206, b"234"))
        self.assertEqual(h.req("GET", "/mod/todo/web/%2e%2e/module.json")[0], 404)
        self.assertEqual(h.req("GET", "/mod/todo/web/intro.exe")[0], 404)


if __name__ == "__main__":
    unittest.main()
