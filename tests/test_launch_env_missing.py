"""A session that came up without a core module's launch.env (the module was coming up after a hub or module restart
past the spawn's wait, paused, stopped, or its call failed) is not silent: the spawn fact names the modules
(host.done without), the session carries env_missing (hub status shows it), and once such a module answers hello again
(module.ready) a free session relaunches with its environment — a busy one, or one with background work, waits; one
with no task parks and gets the environment on its next start. Before, such a session lived for hours without the
module's variables and nothing said so."""
import tests  # noqa: F401 — keep the machine's settings out
import json
import os
import sys
import tempfile
import time
import types
import unittest

from core import modrun, reducer, restart
from hosts.codex import CodexHost, MuxHost
from hosts.streamjson import StreamHost
from tests.fakes.modstand import Stand
from tests.test_modapi_hooks import Mods

OWNER = {"channel": "cli", "owner": True}


def not_ready(mods, name):
    """The module's process was relaunched (hub restart mod:<name>) and has not answered hello yet: the launch file is
    the new launch, the ready record the previous one."""
    path = os.path.join(modrun.run_dir(mods.dir), f"{name}.run.json")
    with open(path) as f:
        run = json.load(f)
    modrun._write(path, {**run, "launch": "L-new"})


class HostNamesTheMissing(unittest.TestCase):
    """The host's launch: modules that declared launch.env and gave this launch nothing are named per session."""

    def mods(self):
        return Mods(self, {"example": {"env": {"CLAUDE_EXAMPLE_X": "1"}}, "other": {"env": {"CLAUDE_OTHER_X": "1"}}})

    def test_a_module_coming_up_is_named_and_a_ready_one_is_not(self):
        m = self.mods()
        h = StreamHost(tempfile.mkdtemp(prefix="envmiss-"), claude=(sys.executable,))
        h.modules = m.mods
        env = h.launch_env("maps")
        self.assertEqual((env.get("CLAUDE_EXAMPLE_X"), env.get("CLAUDE_OTHER_X")), ("1", "1"))
        self.assertNotIn("maps", h.env_missing)
        not_ready(m, "example")
        env = h.launch_env("maps")
        self.assertNotIn("CLAUDE_EXAMPLE_X", env)
        self.assertEqual(env.get("CLAUDE_OTHER_X"), "1")
        self.assertEqual(h.env_missing["maps"], ["example"])
        # the next launch with the module ready clears it
        m2 = self.mods()
        h.modules = m2.mods
        h.launch_env("maps")
        self.assertNotIn("maps", h.env_missing)

    def test_a_failed_call_is_missing_too(self):
        m = Mods(self, {"example": ("timeout", "no answer in 5 s")})
        h = StreamHost(tempfile.mkdtemp(prefix="envmiss-"), claude=(sys.executable,))
        h.modules = m.mods
        h.launch_env("maps")
        self.assertEqual(h.env_missing["maps"], ["example"])

    def test_a_module_refused_by_the_login_rule_is_not_missing(self):
        # it answered: its module.error says why, every session goes without it alike, a relaunch is refused the same
        m = Mods(self, {"example": {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:9", "CLAUDE_EXAMPLE_X": "1"}}})
        h = StreamHost(tempfile.mkdtemp(prefix="envmiss-"), claude=(sys.executable,))
        h.modules = m.mods
        env = h.launch_env("maps")
        self.assertNotIn("CLAUDE_EXAMPLE_X", env)
        self.assertNotIn("maps", h.env_missing)

    def test_a_module_that_never_answered_hello_is_known_by_its_right(self):
        # the first start: no record of its points yet, only the launch.env right in its want file
        m = self.mods()
        m.mods.records().pop("example")
        h = StreamHost(tempfile.mkdtemp(prefix="envmiss-"), claude=(sys.executable,))
        h.modules = m.mods
        env = h.launch_env("maps")
        self.assertEqual((env.get("CLAUDE_EXAMPLE_X"), env.get("CLAUDE_OTHER_X")), (None, "1"))
        self.assertEqual(h.env_missing["maps"], ["example"])

    def test_the_codex_half_writes_into_the_same_marks(self):
        m = self.mods()
        state = tempfile.mkdtemp(prefix="envmiss-")
        stream, codex = StreamHost(state, claude=(sys.executable,)), CodexHost(state, codex=(sys.executable,))
        mux = MuxHost(stream, codex)
        mux.modules = m.mods
        not_ready(m, "example")
        codex._env("maps", None, {"cwd": state})
        self.assertEqual(mux.env_missing["maps"], ["example"])


class SpawnRecordsIt(unittest.TestCase):
    """hubd: the spawn that went without a module after the wait (or without any wait) says so in host.done, and the
    session carries it."""

    def stand(self, clock):
        s = Stand(self, name="example", points=["launch.env"], caps=["launch.env"], clock=clock)
        s.update(**{"launch.env": {"do": "ok", "answer": {"env": {"HUB_MOD_EXAMPLE_X": "1"}}}})
        h = StreamHost(tempfile.mkdtemp(prefix="envmiss-host-"), claude=(sys.executable,))
        h.modules = s.hub.modules.mods
        self.spawned = []
        test = self

        class Host:
            kind, screen_dialogs = "stream", False
            tool_flags = staticmethod(lambda **kw: [])
            env_missing = h.env_missing

            def spawn(self, name, **kw):
                test.spawned.append(h.launch_env(name))
                return "spawned"

            def alive(self, name):
                return False

            def info(self, name):
                return {"pid": 1}

            def poll(self):
                return []
        s.hub.host = Host()
        s.hub.modules.sync()
        return s

    def request(self, s, name):
        s.hub.record("session.requested", name, {"cwd": s.state_dir, "session_id": f"s-{name}", **OWNER})

    def test_late_module_is_named_on_the_session_and_its_ready_marks_the_relaunch(self):
        ahead = [0]   # the module's hello runs on real time; the spawn's wait is passed by moving the clock ahead
        s = self.stand(lambda: time.time() + ahead[0])
        s.launch()
        s.until_ready()
        # the session that comes up after the module is ready gets its environment, no mark
        self.request(s, "maps")
        self.assertEqual(self.spawned[-1].get("HUB_MOD_EXAMPLE_X"), "1")
        self.assertNotIn("env_missing", s.hub.state["sessions"]["maps"])
        # the module is restarted (hub restart mod:example): hub-up drops its launch file, the new process is slow
        p = s.procs[-1]
        modrun.halted(s.state_dir, "example")
        p.kill()
        p.wait(10)
        self.request(s, "notes")
        self.assertEqual(len(self.spawned), 1)              # the spawn waits for the module, as before
        ahead[0] += modrun.LAUNCH_WAIT + 1
        s.hub.run_delayed()
        self.assertEqual(len(self.spawned), 2)
        self.assertNotIn("HUB_MOD_EXAMPLE_X", self.spawned[-1])
        done = [e for e in s.events("host.done") if e["entity"] == "notes" and e["data"].get("do") == "spawn"]
        self.assertEqual(done[-1]["data"].get("without"), ["example"])
        self.assertEqual(s.hub.state["sessions"]["notes"].get("env_missing"), ["example"])
        # the module was late once in this launch: a further spawn does not wait — and is named all the same
        self.request(s, "docs")
        self.assertEqual(len(self.spawned), 3)
        self.assertEqual(s.hub.state["sessions"]["docs"].get("env_missing"), ["example"])
        # the module comes up: the sessions without it are marked for a relaunch
        s.launch()
        s.until_ready()
        for name in ("notes", "docs"):
            self.assertTrue(s.hub.state["sessions"][name].get("env_stale"), name)
        self.assertFalse(s.hub.state["sessions"]["maps"].get("env_stale"))

    def test_a_paused_module_does_not_hold_the_spawn_and_is_named(self):
        ahead = [0]   # the module's hello runs on real time; the spawn's wait is passed by moving the clock ahead
        s = self.stand(lambda: time.time() + ahead[0])
        s.launch()
        s.until_ready()
        for _ in range(modrun.FAILS):
            s.hub.record("module.error", "example", {"module": "example", "hook": "launch.env", "kind": "timeout",
                                                     "error": "x", "channel": "core", "owner": False})
        self.assertEqual(s.hub.modules.mods.status("example"), "suspended")
        self.request(s, "maps")
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(s.hub.state["sessions"]["maps"].get("env_missing"), ["example"])


class RelaunchWhenFree(unittest.TestCase):
    """The reducer: host.done without -> env_missing; module.ready of such a module -> env_stale; _pump relaunches a
    free session the way an updated plugin does."""

    def setUp(self):
        self.st, self.seq, self.actions = reducer.empty(), 0, []
        for n in ("maps", "notes"):
            self.ev("session.requested", n, cwd="/home/x/" + n, session_id="s-" + n, **OWNER)
            self.ev("host.done", n, do="spawn", result="spawned", **({"without": ["example"]} if n == "maps" else {}))
            self.ev("session.started", n)
            self.st["sessions"][n]["has_history"] = True
        self.actions = []

    def ev(self, type, entity=None, ts=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts or 1000 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.assertIsNone(why, (type, data, why))
        self.actions += acts
        return why

    def ready(self, module="example", ts=None):
        self.ev("module.ready", module, ts=ts, module=module, launch="L2", version="1.0.0", points=["launch.env"],
                channel="core")

    def did(self, do):
        return [a["session"] for a in self.actions if a["do"] == do]

    def busy(self, name):
        self.ev("message.received", None, to=name, text="task", **OWNER)
        self.ev("turn.started", name, marker=f"m{self.seq}")
        self.assertEqual(self.st["sessions"][name]["state"], "busy")
        self.actions = []

    def test_the_spawn_fact_puts_and_replaces_the_mark(self):
        self.assertEqual(self.st["sessions"]["maps"]["env_missing"], ["example"])
        self.assertNotIn("env_missing", self.st["sessions"]["notes"])

    def test_another_module_ready_does_nothing(self):
        self.ready("other")
        self.assertEqual(self.actions, [])
        self.assertFalse(self.st["sessions"]["maps"].get("env_stale"))

    def test_a_ready_without_the_launch_env_point_relaunches_nothing_and_leaves_the_list(self):
        # known by its right before its first hello, it turned out to declare other points only: nothing was missing
        self.ev("module.ready", "example", module="example", launch="L2", version="1.0.0", points=["doctor"],
                channel="core")
        self.assertEqual(self.actions, [])
        self.assertFalse({"env_missing", "env_stale"} & set(self.st["sessions"]["maps"]))

    def test_ready_while_the_relaunch_waits_does_not_relaunch_twice(self):
        # a reload sends it to starting; the module answers while the spawn waits in hubd's queue; the spawn then has it
        self.ev("session.reload_requested", "maps", **OWNER)
        self.assertEqual(self.st["sessions"]["maps"]["state"], "starting")
        self.ready()
        self.ev("host.done", "maps", do="spawn", result="spawned")
        self.actions = []
        self.ev("session.started", "maps")
        self.ev("owner.seen", "notes", channel="web", owner=True)
        self.assertEqual(self.did("release"), [])
        self.assertFalse({"env_missing", "env_stale"} & set(self.st["sessions"]["maps"]))

    def test_a_late_spawn_fact_does_not_mark_a_session_with_no_process(self):
        self.ev("session.parked", "notes", channel="core", why="idle")
        self.assertEqual(self.st["sessions"]["notes"]["state"], "parked")
        self.ev("host.done", "notes", do="spawn", result="spawned", without=["example"])
        self.assertNotIn("env_missing", self.st["sessions"]["notes"])

    def test_a_free_session_with_no_task_parks_and_comes_up_with_the_module(self):
        self.ready()
        self.assertEqual((self.did("release"), self.did("spawn")), (["maps"], []))
        self.assertEqual(self.st["sessions"]["maps"]["state"], "parked")
        self.assertNotIn("env_missing", self.st["sessions"]["maps"])   # no process — nothing goes without
        self.assertNotIn("env_stale", self.st["sessions"]["maps"])

    def test_a_busy_session_is_not_cut_it_relaunches_when_free_with_its_queue(self):
        self.busy("maps")
        self.ev("message.received", None, to="maps", text="more", **OWNER)
        self.ready()
        self.assertEqual(self.did("release"), [])
        self.ev("turn.result", "maps", text="done", subtype="success")
        self.assertEqual((self.did("release"), self.did("spawn")), (["maps"], ["maps"]))
        self.assertNotIn("env_stale", self.st["sessions"]["maps"])
        self.ev("host.done", "maps", do="spawn", result="spawned")   # this time with the module
        self.assertNotIn("env_missing", self.st["sessions"]["maps"])

    def test_background_work_holds_the_relaunch(self):
        self.ev("session.background", "maps", tasks=[{"id": "t1", "title": "e2e"}])
        self.ready()
        self.assertEqual(self.did("release"), [])
        self.assertTrue(self.st["sessions"]["maps"].get("env_stale"))
        late = 1000 + self.seq + restart.SERVICE_AFTER + 60
        self.ev("session.info", "maps", ts=late)
        self.assertEqual(self.did("release"), ["maps"])

    def test_a_state_without_the_new_fields_is_folded_as_before(self):
        st = json.loads(json.dumps(self.st))
        st["sessions"]["maps"].pop("env_missing")
        self.st = st
        self.ready()
        self.assertEqual(self.actions, [])


class StatusRow(unittest.TestCase):
    def test_hub_status_names_the_modules(self):
        from core import i18n
        text = i18n.t("cli.status.env_missing_row", modules="example", name="maps")
        self.assertIn("example", text)
        self.assertIn("hub session reload maps", text)
        self.assertNotEqual(text, "cli.status.env_missing_row")


if __name__ == "__main__":
    unittest.main()
