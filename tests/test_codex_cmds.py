"""Codex (experimental): commands typed into a Codex session — `/model`, `/effort`, `/clear`,
`/compact <text>`, `/<skill>` and the other Claude Code commands the core may type — are done by the host
(hosts/codex_cmds.py), not handed to the model as text. An effort the session's model does not have (model/list) is never
sent to Codex: `/effort` refuses it in words, and a launch effort is lowered to the nearest one the model has."""
import tests  # noqa: F401 — do not see the machine's settings (~/.config/hub-core), see tests/__init__.py
import io
import json
import os
import queue
import sys
import tempfile
import threading
import time
import types
import unittest

import platforms

from core import i18n, journal, spool
from core.daemon import Hub
from hosts import codex, codex_cmds
from hosts.codex import CodexHost, MuxHost
from hosts.streamjson import StreamHost

HERE = os.path.dirname(__file__)
FAKE_CLAUDE = os.path.join(HERE, "fakes", "fake_claude_stream.py")
FAKE_CODEX = os.path.join(HERE, "fakes", "fake_codex.py")


def model(name, efforts, default, is_default=False, hidden=False):
    """A model/list entry shaped like codex-cli 0.162.1's (probe 2026-11-05, without a sign-in)."""
    return {"id": name, "model": name, "displayName": name, "description": "", "hidden": hidden, "isDefault": is_default,
            "defaultReasoningEffort": default,
            "supportedReasoningEfforts": [{"reasoningEffort": e, "description": ""} for e in efforts]}


# gpt-5.5 has no max/ultra (model/list of 0.162.1), the newer one has both; a hidden one (includeHidden: 0.162.1 lists
# codex-auto-review and the like with hidden true) the page's menu does not show
MODELS = {"data": [model("gpt-new", ["low", "medium", "high", "xhigh", "max", "ultra"], "low", True),
                   model("gpt-5.5", ["low", "medium", "high", "xhigh"], "medium"),
                   model("gpt-hidden", ["low", "medium"], "low", hidden=True)], "nextCursor": None}


class Flow(unittest.TestCase):
    """The core and the Codex host together, with the fake codex and model/list, skills/list from a replies file."""
    MODEL = "codex/gpt-5.5"

    def setUp(self):
        codex_cmds.KNOWN.clear()
        self.state = tempfile.mkdtemp(prefix="hubcxa-")
        self.calls = os.path.join(self.state, "calls.jsonl")
        self.skill = os.path.join(self.state, "skills", "helper", "SKILL.md")
        replies = os.path.join(self.state, "replies.json")
        with open(replies, "w", encoding="utf-8") as f:
            json.dump({"model/list": MODELS, "skills/list": {"data": [{"cwd": self.state, "errors": [], "skills": [
                {"name": "helper", "description": "", "path": self.skill, "scope": "user", "enabled": True},
                {"name": "off", "description": "", "path": self.skill + "x", "scope": "user", "enabled": False}]}]}}, f)
        env = {"FAKE_TURN_S": "0.1", "FAKE_CODEX_REPLIES": replies, "FAKE_CODEX_CALLS": self.calls}
        stream = StreamHost(self.state, claude=(sys.executable, FAKE_CLAUDE), env=env)
        cx = CodexHost(self.state, codex=(sys.executable, FAKE_CODEX), env=env)
        self.hub = Hub(self.state, MuxHost(stream, cx), registry_dir=os.path.join(self.state, "registry"))
        self.hub.start()

    def tearDown(self):
        self.hub.host.kill_server()
        time.sleep(0.2)
        platforms.current.rmtree(self.state)
        codex_cmds.KNOWN.clear()

    def cli(self, type, entity=None, **data):
        spool.put(os.path.join(self.state, "spool"), type, entity, data)

    def until(self, pred, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            self.hub.tick()
            if pred():
                return
            time.sleep(0.03)
        self.fail(f"did not wait; {self.hub.state['sessions']} {self.hub.state['messages']}")

    def sess(self, name):
        return self.hub.state["sessions"].get(name, {})

    def events(self, type, entity=None, **match):
        return [e for e in journal.read(self.hub.journal.path) if e["type"] == type
                and (entity is None or e.get("entity") == entity)
                and all(e["data"].get(k) == v for k, v in match.items())]

    def sent(self, method):
        try:
            with open(self.calls, encoding="utf-8") as f:
                return [c["params"] for c in map(json.loads, f) if c["method"] == method]
        except OSError:
            return []

    def send(self, text, **extra):
        before = set(self.hub.state["messages"])
        self.cli("message.received", to="a", text=text, channel="web", owner=True, **extra)
        self.until(lambda: set(self.hub.state["messages"]) - before)
        (mid,) = set(self.hub.state["messages"]) - before
        return mid

    def delivered(self, mid):
        return self.hub.state["messages"][mid]["state"] == "delivered"

    def command(self, text):
        """The owner types a command: it is delivered without a model turn, the answer is a result with no turn."""
        results = len(self.events("turn.result", "a"))
        mid = self.send(text)
        self.until(lambda: self.delivered(mid) and len(self.events("turn.result", "a")) > results)
        self.assertEqual(self.sess("a")["state"], "idle")
        return self.events("turn.result", "a")[-1]["data"]

    def turn(self, text):
        """A plain message: one model turn; the turn/start Codex got."""
        starts = len(self.sent("turn/start"))
        mid = self.send(text)
        self.until(lambda: self.delivered(mid) and self.sess("a")["state"] == "idle"
                   and len(self.sent("turn/start")) > starts and self.events("turn.result", "a")
                   and self.events("turn.result", "a")[-1]["data"].get("turns") == 1)
        return self.sent("turn/start")[-1]

    def codex_session(self, effort=None):
        self.cli("session.requested", "a", cwd=self.state, session_id="sid-a")
        self.until(lambda: self.sess("a").get("state") == "idle")
        self.cli("session.model_set", "a", model=self.MODEL, channel="web", owner=True)
        if effort:
            self.cli("session.effort_set", "a", level=effort, channel="web", owner=True)
        self.until(lambda: self.hub.host.host_of("a") == "codex" and self.sess("a").get("state") == "idle"
                   and self.sess("a").get("model") == self.MODEL and (self.sess("a").get("info") or {}).get("host")
                   == "codex" and self.sent("model/list"))

    def test_effort_the_model_lacks_is_refused_in_words_and_never_reaches_codex(self):
        self.codex_session()
        res = self.command("/effort max")
        self.assertEqual(res["turns"], 0)
        self.assertFalse(res["is_error"])
        for word in ("max", "gpt-5.5", "low, medium, high, xhigh"):
            self.assertIn(word, res["text"])
        self.assertFalse([p for p in self.sent("turn/start") if "/effort" in json.dumps(p)])   # not a turn of the model
        self.assertNotIn("max", json.dumps(self.turn("next")))
        self.assertNotEqual((self.sess("a").get("info") or {}).get("effort"), "max")

    def test_effort_the_model_has_is_set_for_the_thread_and_the_next_turns(self):
        self.codex_session()
        res = self.command("/effort high")
        self.assertIn("high", res["text"])
        self.assertEqual(self.sess("a")["info"]["effort"], "high")
        self.assertEqual(self.turn("next").get("effort"), "high")
        self.assertFalse(self.sent("thread/settings/update"))   # the experimental API: the stable turn/start does it

    def test_launch_effort_the_model_lacks_is_lowered_to_what_it_has(self):
        # `hub session effort max` (or the hub's effort) on a model without max: Codex would send it to the API as is
        self.codex_session(effort="max")
        self.until(lambda: self.sess("a").get("effort") == "max" and self.sess("a")["state"] == "idle"
                   and (self.sess("a").get("info") or {}).get("effort") == "xhigh")
        self.assertEqual(self.turn("next").get("effort"), "xhigh")
        self.assertFalse([p for p in self.sent("turn/start") if p.get("effort") == "max"])

    def test_model_is_switched_in_the_thread_and_the_effort_follows_it(self):
        self.codex_session()
        self.command("/model codex/gpt-new")
        self.assertEqual(self.sess("a")["info"]["model"], "gpt-new")
        res = self.command("/effort ultra")   # gpt-new has it, but the core keeps no such level (catalog.EFFORTS)
        refused = i18n.t("core.codex_cmds.no_effort", model="gpt-new", effort="ultra", efforts="\0").split("\0")[0]
        self.assertIn(refused, res["text"])   # the owner reads the answer: in the hub's language
        self.command("/effort max")                                      # gpt-new has it
        first = self.turn("one")
        self.assertEqual((first.get("model"), first.get("effort")), ("gpt-new", "max"))
        res = self.command("/model codex/gpt-5.5")                       # it has no max
        self.assertIn("xhigh", res["text"])
        self.assertEqual((self.sess("a")["info"]["model"], self.sess("a")["info"]["effort"]), ("gpt-5.5", "xhigh"))
        second = self.turn("two")
        self.assertEqual((second.get("model"), second.get("effort")), ("gpt-5.5", "xhigh"))
        self.command("/clear")                                           # a new thread starts on the chosen model
        self.assertEqual(self.sent("thread/start")[-1].get("model"), "gpt-5.5")

    def test_model_without_the_codex_prefix_or_default_next_to_claude_is_refused(self):
        """The core keeps the owner's /model only as codex/<id>: with a bare id the session would come back on another
        model after its next start — on a hub with claude, on claude."""
        self.codex_session()
        for text in ("/model gpt-new", "/model default"):
            res = self.command(text)
            self.assertIn("/model" if "default" not in text else "hub session model", res["text"])
        self.assertEqual(self.sess("a")["info"]["model"], "gpt-5.5")
        self.assertEqual(self.sess("a")["model"], self.MODEL)
        self.assertEqual(self.turn("next").get("model"), "gpt-5.5")

    def test_unknown_model_is_refused_with_the_list(self):
        self.codex_session()
        res = self.command("/model codex/gpt-nope")
        self.assertIn("gpt-nope", res["text"])
        self.assertIn("gpt-new", res["text"])
        self.assertEqual(self.sess("a")["info"]["model"], "gpt-5.5")
        self.assertEqual(self.turn("next").get("model"), "gpt-5.5")

    def test_what_codex_refused_is_not_kept_for_the_session(self):
        """One rule for the host and the fold (slice M): the core keeps a typed /model or /effort only once the host took
        it. Kept at once, a model Codex lacks would come back at the next start (then a turn that fails), an effort
        the model lacks would be fitted down on every launch."""
        self.codex_session()
        self.command("/model codex/gpt-nope")
        self.assertEqual(self.sess("a")["model"], self.MODEL)
        self.command("/effort max")   # gpt-5.5 has no max
        self.assertIsNone(self.sess("a").get("effort"))
        self.command("/model default")
        self.assertEqual(self.sess("a")["model"], self.MODEL)

    def test_what_codex_took_is_kept_for_the_session(self):
        self.codex_session()
        self.command("/model codex/gpt-new")
        self.assertEqual(self.sess("a")["model"], "codex/gpt-new")
        self.command("/effort high")
        self.assertEqual(self.sess("a")["effort"], "high")

    def test_a_hidden_model_is_not_taken_as_the_menu_does_not_show_it(self):
        self.codex_session()
        res = self.command("/model codex/gpt-hidden")
        self.assertIn("gpt-hidden", res["text"])
        self.assertIn("codex/gpt-new", res["text"])
        self.assertEqual((self.sess("a")["info"]["model"], self.sess("a")["model"]), ("gpt-5.5", self.MODEL))
        self.assertEqual(self.turn("next").get("model"), "gpt-5.5")
        listing = self.command("/model")["text"]
        self.assertIn("codex/gpt-5.5", listing)
        self.assertNotIn("gpt-hidden", listing)

    def test_clear_starts_a_new_conversation(self):
        self.codex_session()
        self.turn("first")
        res = self.command("/clear")
        self.assertEqual(res["turns"], 0)
        (start,) = [p for p in self.sent("thread/start") if p.get("sessionStartSource") == "clear"]
        self.assertEqual(start["cwd"], self.state)
        (cleared,) = self.events("session.started", "a", source="clear")
        new = cleared["data"]["session_id"]
        self.assertNotIn(new, (None, "sid-a"))
        self.assertEqual(self.sess("a")["session_id"], new)
        with open(os.path.join(self.state, "sessions", "a", "codex-threads.json")) as f:
            threads = json.load(f)
        self.assertNotEqual(threads[new], threads["sid-a"])
        self.assertEqual(self.turn("second")["threadId"], threads[new])   # the next turn goes to the new thread

    def test_clear_survives_a_restart_with_the_new_conversation(self):
        self.codex_session()
        self.turn("first")
        self.command("/clear")
        self.turn("second")
        self.send("/crash")
        self.until(lambda: self.events("session.exited", "a"))
        self.until(lambda: self.sess("a").get("state") == "idle" and self.events("session.started", "a", source="resume"))
        with open(os.path.join(self.state, "sessions", "a", "codex-threads.json")) as f:
            threads = json.load(f)
        new = self.sess("a")["session_id"]
        self.assertNotEqual(new, "sid-a")
        self.assertEqual(self.sent("thread/resume")[-1]["threadId"], threads[new])   # the new conversation comes back
        self.assertEqual(self.turn("third")["threadId"], threads[new])

    def test_compact_with_instructions_compacts_and_says_they_are_not_used(self):
        self.codex_session()
        self.turn("first")
        res = self.command("/compact keep the API notes")
        self.assertTrue(self.sent("thread/compact/start"))
        self.assertTrue(self.events("session.compacted", "a"))
        self.assertIn("keep the API notes", res["text"])
        self.assertFalse([p for p in self.sent("turn/start") if "/compact" in json.dumps(p)])

    def test_claude_code_command_codex_lacks_is_refused_not_sent_to_the_model(self):
        self.codex_session()
        res = self.command("/cost")
        self.assertIn("/cost", res["text"])
        self.assertFalse([p for p in self.sent("turn/start") if "/cost" in json.dumps(p)])

    def test_the_host_asks_for_the_experimental_api(self):
        """codex-cli 0.162.1 refuses thread/settings/update and turn/start.additionalContext (the seam of slice 0)
        without it: "requires experimentalApi capability" (live probe 2026-11-05; the fake does not check it)."""
        self.codex_session()
        init = self.sent("initialize")[-1]   # the session's process (the earlier ones: the host's hook check)
        self.assertIs((init.get("capabilities") or {}).get("experimentalApi"), True)

    def test_builtins_name_the_commands_the_host_takes(self):
        self.codex_session()
        builtins = self.sess("a")["info"]["builtins"]
        self.assertTrue({"model", "effort", "clear", "compact", "cost"} <= set(builtins), builtins)

    def test_skill_command_goes_as_a_skill_input(self):
        self.codex_session()
        start = self.turn("/helper go")
        skill, text = start["input"][0], start["input"][1]
        self.assertEqual(skill, {"type": "skill", "name": "helper", "path": self.skill})
        self.assertTrue(text["text"].startswith("/helper go"))
        self.assertEqual(self.turn("/plug:helper go")["input"][0]["name"], "helper")   # a Claude plugin skill's name
        self.assertNotIn("skill", json.dumps(self.turn("/off go")["input"]))   # a disabled one is plain text
        self.assertNotIn("skill", json.dumps(self.turn("/nope go")["input"]))  # so is an unknown name


class Host(unittest.TestCase):
    """The host's parts without a process: what goes to Codex and which facts come back."""

    def setUp(self):
        codex_cmds.KNOWN.clear()
        self.tmp = tempfile.mkdtemp(prefix="hubcxa-")
        host = CodexHost.__new__(CodexHost)
        host.state_dir, host.procs, host.lock, host.inbox = self.tmp, {}, threading.Lock(), queue.Queue()
        host.clock, host.modules = time.time, None
        host.default_model, host.default_effort, host.all_sessions = None, None, False
        self.popen = types.SimpleNamespace(stdin=io.StringIO(), poll=lambda: None, pid=1)
        proc = codex.CodexProcess(self.popen, "sid", False)
        proc.leaving = False
        proc.model, proc.effort, proc.start_params = "gpt-5.5", None, {"cwd": self.tmp}
        host.procs["a"] = proc
        self.host, self.proc = host, proc

    def tearDown(self):
        platforms.current.rmtree(self.tmp)
        codex_cmds.KNOWN.clear()

    def written(self, method=None):
        got = [json.loads(x) for x in self.popen.stdin.getvalue().splitlines()]
        return [m for m in got if method is None or m.get("method") == method]

    def answer(self, method, result=None, error=None):
        msg = self.written(method)[-1]
        out = {"id": msg["id"], **({"error": error} if error else {"result": result if result is not None else {}})}
        return self.host._line("a", out)

    def start(self, result=None):
        self.proc.call("thread/start", {"cwd": self.tmp})
        return self.answer("thread/start", result or {"thread": {"id": "t1"}, "model": "gpt-5.5", "cwd": self.tmp})

    def queued(self):
        out = []
        while not self.host.inbox.empty():
            name, msg, _ = self.host.inbox.get()
            out += self.host._line(name, msg)
        return out

    def test_session_info_of_the_start_is_kept_on_the_process(self):
        """The core replaces info whole: a slice changing a field starts from what the host gave (hosts/codex.py)."""
        facts = self.start()
        (info,) = [f[2] for f in facts if f[0] == "session.info"]
        self.assertIs(self.proc.session_info, info)
        self.assertEqual(info["model"], "gpt-5.5")

    def test_effort_without_an_argument_names_the_current_and_the_choice(self):
        self.start()
        self.answer("model/list", MODELS)
        self.host.type("a", "/effort", message="m1")
        (res,) = [f[2] for f in self.queued() if f[0] == "turn.result"]
        self.assertIn("low, medium, high, xhigh", res["text"])
        self.assertFalse(self.written("thread/settings/update"))

    def test_effort_is_checked_once_the_model_list_comes(self):
        """A message typed before model/list answered: the launch effort is lowered when the list comes."""
        self.proc.effort = "max"
        self.start()
        self.assertTrue(self.written("model/list"))
        facts = self.answer("model/list", MODELS)
        self.assertEqual(self.proc.effort, "xhigh")
        (info,) = [f[2] for f in facts if f[0] == "session.info"]
        self.assertEqual(info["effort"], "xhigh")

    def test_every_turn_start_carries_an_effort_the_model_has(self):
        """Whatever set the effort (a launch, an executor's own level, another slice): turn/start never carries one the
        model lacks — Codex would send it to the API as is."""
        self.start()
        self.answer("model/list", MODELS)
        self.proc.effort = "max"
        self.host.type("a", "x", message="m1")
        self.assertEqual(self.written("turn/start")[-1]["params"]["effort"], "xhigh")

    def test_effort_outside_codex_levels_is_refused_before_the_model_list(self):
        self.start()
        self.host.type("a", "/effort hihg", message="m1")
        (res,) = [f[2] for f in self.queued() if f[0] == "turn.result"]
        self.assertIn("hihg", res["text"])
        self.assertIsNone(self.proc.effort)

    def test_compaction_codex_refused_is_answered_and_not_counted_as_running(self):
        self.start()
        self.host.type("a", "/compact keep", message="m1")
        self.assertEqual(self.proc.compacts, 1)
        self.answer("thread/compact/start", error={"code": -32600, "message": "nothing to compact"})
        self.assertEqual(self.proc.compacts, 0)
        (res,) = [f[2] for f in self.queued() if f[0] == "turn.result"]
        self.assertIn("nothing to compact", res["text"])
        self.host.type("a", "/clear", message="m2")   # not "busy" for good
        self.assertTrue([m for m in self.written("thread/start") if m["params"].get("sessionStartSource") == "clear"])

    def test_clear_refused_while_a_turn_is_starting(self):
        self.start()
        self.host.type("a", "x", message="m1")       # turn/start sent, no answer yet
        self.host.type("a", "/clear", message="m2")
        self.assertFalse([m for m in self.written("thread/start") if m["params"].get("sessionStartSource")])
        self.assertEqual(self.proc.thread, "t1")

    def test_clear_refused_while_a_subagent_runs(self):
        # slice C routes a subagent's lines by the session's thread: no new thread under a running one; a command left
        # in the background does not hold /clear (as with Claude Code)
        from hosts import codex_items
        self.start()
        work = codex_items._work(self.proc)
        work.running["child"] = "u9"
        self.host.type("a", "/clear", message="m2")
        self.assertFalse([m for m in self.written("thread/start") if m["params"].get("sessionStartSource")])
        self.assertEqual(self.proc.thread, "t1")
        work.running.clear()
        work.cmds[("t1", "c1")] = {}
        self.host.type("a", "/clear", message="m3")
        self.assertTrue([m for m in self.written("thread/start") if m["params"].get("sessionStartSource") == "clear"])

    def test_efforts_are_the_levels_the_core_keeps(self):
        # /effort, its listing and the fit on /model use one rule: the model's levels in catalog.EFFORTS (reducer keeps
        # only those), all its levels when it has none of them
        saved = dict(codex_cmds.KNOWN)
        try:
            codex_cmds.KNOWN.clear()
            codex_cmds.KNOWN.update({"m": {"efforts": ["minimal", "medium"], "default": "minimal", "is_default": False},
                                     "odd": {"efforts": ["none", "minimal"], "default": "none", "is_default": False}})
            self.assertEqual(codex_cmds.fit("m", "low"), "medium")
            self.assertEqual(codex_cmds.fit("odd", "low"), "minimal")
        finally:
            codex_cmds.KNOWN.clear()
            codex_cmds.KNOWN.update(saved)

    def test_a_listing_cut_short_keeps_the_known_models(self):
        self.start()
        self.answer("model/list", MODELS)
        self.host.procs["b"] = proc = codex.CodexProcess(self.popen, "sid-b", False)
        codex_cmds.start(proc)
        rid = self.written("model/list")[-1]["id"]
        self.host._line("b", {"id": rid, "result": {"data": [model("gpt-other", ["low"], "low")], "nextCursor": "p2"}})
        self.host._line("b", {"id": self.written("model/list")[-1]["id"], "error": {"code": -1, "message": "network"}})
        self.assertEqual(sorted(codex_cmds.KNOWN), ["gpt-5.5", "gpt-hidden", "gpt-new"])   # hidden kept, flagged

    def test_an_effort_lowered_right_before_the_turn_reaches_session_info_at_its_end(self):
        self.start()
        self.answer("model/list", MODELS)
        self.proc.effort = "max"
        self.host.type("a", "x", message="m1")
        turn = self.answer("turn/start", {"turn": {"id": "u1"}})
        self.assertFalse([f for f in turn if f[0] == "session.info"])
        facts = self.host._line("a", {"method": "turn/completed", "params": {"threadId": "t1", "turn": {
            "id": "u1", "status": "completed"}}})
        (info,) = [f[2] for f in facts if f[0] == "session.info"]
        self.assertEqual(info["effort"], "xhigh")

    def test_clear_takes_a_new_session_id_only_once_codex_opened_the_thread(self):
        self.start()
        self.host.type("a", "/clear", message="m1")
        self.assertEqual(self.proc.session_id, "sid")       # the process may still leave under the core's id
        self.answer("thread/start", error={"code": -1, "message": "no"})
        self.assertEqual((self.proc.session_id, self.proc.thread), ("sid", "t1"))
        self.host.type("a", "/clear", message="m2")
        facts = self.answer("thread/start", {"thread": {"id": "t3"}, "model": "gpt-5.5", "cwd": self.tmp})
        (started,) = [f[2] for f in facts if f[0] == "session.started"]
        self.assertEqual((started["source"], started["session_id"]), ("clear", self.proc.session_id))
        self.assertNotEqual(self.proc.session_id, "sid")

    def test_a_typed_command_is_answered_once(self):
        self.start()
        self.answer("model/list", MODELS)
        self.host.type("a", "/effort high", message="m1")
        self.assertEqual(self.host.type("a", "/effort high", message="m1"), "already sent")
        facts = self.queued()
        self.assertEqual([f[0] for f in facts], ["session.info", "turn.result"])
        self.assertEqual((facts[0][2]["effort"], self.proc.session_info["effort"]), ("high", "high"))

    def test_clear_holds_input_until_the_new_thread(self):
        self.start()
        self.proc.backlog.clear()
        self.host.type("a", "/clear\n\n[hub:m1]", message="m1")
        self.host.type("a", "after", message="m2")
        self.assertFalse(self.written("turn/start"))   # not into the old thread
        self.answer("thread/start", {"thread": {"id": "t2"}, "model": "gpt-5.5", "cwd": self.tmp})
        self.assertEqual(self.written("turn/start")[-1]["params"]["threadId"], "t2")


class Chosen(unittest.TestCase):
    """The fold keeps a Codex choice only from the host's answer to the owner's own command, with the value he typed."""

    def setUp(self):
        from core import reducer
        self.reducer = reducer
        self.st = {"codex": {"host": True}}
        self.sess = {"name": "a", "info": {"host": "codex"}, "model": "codex/gpt-5.5"}

    def chosen(self, text, chosen, owner=True):
        self.reducer._codex_chosen(self.st, self.sess, {"text": text, "from_owner": owner}, chosen)
        return self.sess.get("model"), self.sess.get("effort")

    def test_only_the_owners_command_with_the_value_he_typed(self):
        self.assertEqual(self.chosen("/model codex/gpt-new", {"model": "codex/gpt-other"}), ("codex/gpt-5.5", None))
        self.assertEqual(self.chosen("/model codex/gpt-new", {"model": "codex/gpt-new"}, owner=False),
                         ("codex/gpt-5.5", None))
        self.assertEqual(self.chosen("/effort high", {"model": "high"}), ("codex/gpt-5.5", None))
        self.assertEqual(self.chosen("/effort ultra", {"effort": "ultra"}), ("codex/gpt-5.5", None))   # not a core level
        self.assertEqual(self.chosen("/model codex/gpt-new", {"model": "codex/gpt-new"}), ("codex/gpt-new", None))
        self.assertEqual(self.chosen("/effort high", {"effort": "high"}), ("codex/gpt-new", "high"))

    def test_a_codex_session_before_its_first_info_waits_for_the_host_too(self):
        # spawned, no session.info yet: by its codex/… model, or on a hub entirely on Codex
        for sess, st, model in (({"name": "a", "model": "codex/gpt-5.5"}, {"codex": {"host": True}}, "codex/gpt-5.5"),
                                ({"name": "a"}, {"codex": {"host": True, "whole": True}}, None)):
            self.reducer._remember_choice(st, sess, "model", ["codex/gpt-nope"])
            self.reducer._remember_choice(st, sess, "effort", ["max"])
            self.assertEqual((sess.get("model"), sess.get("effort")), (model, None))

    def test_a_session_moved_to_claude_keeps_the_choice_on_receipt(self):
        # info says claude while the stored model is still the codex/… one it moved off: Claude Code took it
        sess = {"name": "a", "model": "codex/gpt-5.5", "info": {"host": "claude"}}
        self.reducer._remember_choice({"codex": {"host": True}}, sess, "effort", ["high"])
        self.assertEqual(sess.get("effort"), "high")

    def test_a_claude_session_keeps_the_choice_on_receipt_as_on_main(self):
        sess = {"name": "b", "info": {}}
        self.reducer._remember_choice({}, sess, "model", ["claude-opus-4-1"])
        self.reducer._remember_choice({}, sess, "effort", ["high"])
        self.assertEqual((sess.get("model"), sess.get("effort")), ("claude-opus-4-1", "high"))


if __name__ == "__main__":
    unittest.main()
