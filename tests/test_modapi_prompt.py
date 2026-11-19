"""Module API points into session prompts (the Module API spec, C7): prompt.parts — a module gets the core's system
addendum as a list of named parts and returns its own list (append, replace, remove); prompt.input — the text the host
writes to the session. A module failure leaves the core parts and the text as they are; what a module changed is the
mod.prompt fact, shown by `hub show`. Without a module the command line and the addendum file are byte-for-byte as before."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import sys
import tempfile
import time
import unittest

from core import i18n, modprompt, modrun, reducer, views
from hosts import streamjson
from hosts.streamjson import StreamHost, StreamProcess
from tests.fakes.modstand import Stand

CORE = ["language", "background", "correction", "first_action", "ask_what", "shared_state", "peer_rules", "autonomy", "idle", "findings", "contacts", "gist", "cards", "progress", "skills"]


class Stub:
    """Modules without processes: answering — who answers a point, answers[name](point, payload) -> a reply or None (failure)."""

    def __init__(self, **answers):
        self.answers, self.calls, self.emitted = answers, [], []

    def answering(self, point):
        # by default — the prompt points (prompt.parts, prompt.input): the host calls launch.env and the others too
        return [n for n in self.answers if point in getattr(self.answers[n], "points", ("prompt.parts", "prompt.input"))]

    def call(self, name, point, payload, ctx=None):
        self.calls.append((name, point, json.loads(json.dumps(payload))))
        return self.answers[name](point, payload)

    def emit(self, kind, entity=None, data=None):
        self.emitted.append((kind, entity, data))


def parts_of(payload):
    return [dict(p) for p in payload["parts"]]


def append(name, text):
    return lambda point, payload: parts_of(payload) + [{"name": name, "text": text}]


class Pipe:
    def __init__(self):
        self.lines = []

    def write(self, s):
        self.lines.append(s)

    def flush(self):
        pass


class Base(unittest.TestCase):
    def host(self, mods=None, fresh=False):
        state = tempfile.mkdtemp(prefix="modprompt-")
        path = os.path.join(state, "hub.json")
        with open(path, "w") as f:
            json.dump({}, f)
        old = os.environ.get("HUB_CONFIG")
        os.environ["HUB_CONFIG"] = path
        self.addCleanup(lambda: os.environ.__setitem__("HUB_CONFIG", old) if old else os.environ.pop("HUB_CONFIG"))
        h = StreamHost(state, claude=("echo",))
        h.fresh_prompt = fresh
        if mods is not None:
            h.modules = mods
        return h

    def agent(self, host, text="# Agent\n\nown rules\n"):
        path = os.path.join(host.state_dir, "agents", "p", "CLAUDE.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def prompt(self, cmd):
        """The system addendum text from the command line: a file (one flag) or a string; none — None."""
        if "--append-system-prompt-file" in cmd:
            self.assertNotIn("--append-system-prompt", cmd)   # older Claude Code versions do not take two flags at once
            with open(cmd[cmd.index("--append-system-prompt-file") + 1], encoding="utf-8") as f:
                return f.read()
        if "--append-system-prompt" in cmd:
            return cmd[cmd.index("--append-system-prompt") + 1]
        return None


class WithoutModules(Base):
    def test_agent_session_prompt_is_byte_for_byte_the_core_one(self):
        h = self.host()
        f = self.agent(h)
        cmd = h.command("w", "s-1", agent_file=f)
        core = [streamjson.LANGUAGE, streamjson.BACKGROUND, streamjson.CORRECTION, streamjson.FIRST_ACTION, streamjson.ASK_WHAT,
                streamjson.SHARED_STATE, streamjson.PEER_RULES, streamjson.AUTONOMY, streamjson.IDLE, streamjson.FINDINGS, streamjson.CONTACTS, streamjson.GIST,
                streamjson.CARDS, streamjson.PROGRESS, streamjson.LESSONS, streamjson.SKILLS]
        self.assertEqual(self.prompt(cmd), "# Agent\n\nown rules\n\n" + "\n\n".join(core) + "\n")

    def test_no_module_and_a_module_that_changes_nothing_give_the_same_command(self):
        for kw in ({}, {"agent": True}, {"agent": True, "executor_of": "maps", "agents": [("maps", "geo")]},
                   {"resume": True}):
            plain, same = self.host(fresh=True), self.host(Stub(m=lambda p, d: parts_of(d)), fresh=True)
            args = {k: v for k, v in kw.items() if k != "agent"}
            a = plain.command("w", "s-1", agent_file=self.agent(plain) if kw.get("agent") else None, **args)
            b = same.command("w", "s-1", agent_file=self.agent(same) if kw.get("agent") else None, **args)
            self.assertEqual(self.prompt(a), self.prompt(b))
            self.assertEqual([x.replace(same.state_dir, "<s>") for x in b], [x.replace(plain.state_dir, "<s>") for x in a])
            self.assertEqual(same.modules.emitted, [])

    def test_a_host_without_hubd_has_no_modules(self):
        self.assertIsNone(getattr(self.host(), "modules", None))


class Parts(Base):
    def test_the_module_gets_named_parts_in_prompt_order(self):
        mods = Stub(m=lambda p, d: None)
        h = self.host(mods)
        h.command("w", "s-1", agent_file=self.agent(h), executor_of="maps", agents=[("maps", "geo")])
        name, point, payload = mods.calls[0]   # an executor reports to whoever raised it: no progress part, and no queue to walk: no idle part
        self.assertEqual((name, point), ("m", "prompt.parts"))
        self.assertEqual([p["name"] for p in payload["parts"]], ["agent_file", "language", "background", "correction", "first_action", "ask_what", "shared_state", "peer_rules", "autonomy", "findings", "contacts", "gist", "cards", "executor", "roster", "skills"])
        self.assertEqual(payload["parts"][0]["text"], "# Agent\n\nown rules")
        self.assertEqual(payload["parts"][1]["text"], streamjson.LANGUAGE)
        self.assertEqual((payload["session"], payload["resume"]), ("w", False))

    def test_append_replace_remove_in_one_file_for_an_agent(self):
        def edit(point, payload):
            out = [p for p in parts_of(payload) if p["name"] != "findings"]
            for p in out:
                if p["name"] == "autonomy":
                    p["text"] = "OWN AUTONOMY"
            return out + [{"name": "mine", "text": "OWN RULE"}]
        h = self.host(Stub(m=edit))
        cmd = h.command("w", "s-1", agent_file=self.agent(h))
        self.assertEqual(cmd.count("--append-system-prompt-file"), 1)
        text = self.prompt(cmd)
        self.assertTrue(text.startswith("# Agent\n\nown rules\n\n" + streamjson.LANGUAGE))
        self.assertIn("OWN AUTONOMY", text)
        self.assertNotIn(streamjson.AUTONOMY, text)
        self.assertNotIn(streamjson.FINDINGS, text)
        self.assertTrue(text.endswith("\n\nOWN RULE\n"))

    def test_a_plain_session_gets_the_changed_prompt_as_a_file_too(self):
        # the length of a module's text is not limited, but an argument string is (128 KB on Linux, ~32 thousand chars on Windows)
        big = "x" * 300000
        h = self.host(Stub(m=lambda p, d: [{"name": "only", "text": "ONLY THIS"}, {"name": "big", "text": big}]))
        cmd = h.command("w", "s-1")
        self.assertEqual(self.prompt(cmd), "ONLY THIS\n\n" + big + "\n")
        self.assertLess(max(len(x) for x in cmd), 1000)

    def test_the_module_can_remove_the_agent_file_too(self):
        h = self.host(Stub(m=lambda p, d: [x for x in parts_of(d) if x["name"] != "agent_file"]))
        text = self.prompt(h.command("w", "s-1", agent_file=self.agent(h)))
        self.assertNotIn("own rules", text)
        self.assertTrue(text.startswith(streamjson.LANGUAGE))

    def test_removing_everything_leaves_no_prompt_flag_but_keeps_the_snapshot_off(self):
        h = self.host(Stub(m=lambda p, d: []), fresh=True)
        cmd = h.command("w", "s-1", resume=True, agent_file=self.agent(h))
        self.assertIsNone(self.prompt(cmd))
        self.assertIn("--system-prompt-snapshot", cmd)   # otherwise --resume brings back the prompt of the first request
        old = self.host(Stub(m=lambda p, d: []), fresh=False)
        self.assertNotIn("--system-prompt-snapshot", old.command("w", "s-1", agent_file=self.agent(old)))

    def test_modules_go_in_turn_each_sees_the_previous_result(self):
        mods = Stub(a=append("a", "FROM A"), b=append("b", "FROM B"))
        h = self.host(mods)
        text = self.prompt(h.command("w", "s-1"))
        self.assertTrue(text.endswith("FROM A\n\nFROM B\n"))
        self.assertEqual([p["name"] for p in mods.calls[1][2]["parts"]][-1], "a")

    def test_one_adds_the_other_removes_is_no_change(self):
        mods = Stub(a=append("x", "X"), b=lambda p, d: [q for q in parts_of(d) if q["name"] != "x"])
        h = self.host(mods)
        self.assertEqual(self.prompt(h.command("w", "s-1")), self.prompt(self.host().command("w", "s-1")))
        self.assertEqual(mods.emitted, [])

    def test_a_failing_module_leaves_the_parts_as_they_were(self):
        h = self.host(Stub(a=lambda p, d: None, b=append("b", "FROM B")))
        text = self.prompt(h.command("w", "s-1"))
        self.assertTrue(text.startswith(streamjson.LANGUAGE))
        self.assertTrue(text.endswith(streamjson.SKILLS + "\n\nFROM B\n"))
        alone = self.host(Stub(a=lambda p, d: None))
        self.assertEqual(self.prompt(alone.command("w", "s-1")), self.prompt(self.host().command("w", "s-1")))

    def test_on_resume_without_a_fresh_prompt_the_module_is_not_asked(self):
        # Claude Code without --system-prompt-snapshot sends the first request's conversation prompt on --resume — changes will not arrive
        mods = Stub(m=append("x", "X"))
        h = self.host(mods, fresh=False)
        h.command("w", "s-1", resume=True)
        self.assertEqual(mods.calls, [])
        fresh = self.host(Stub(m=append("x", "X")), fresh=True)
        cmd = fresh.command("w", "s-1", resume=True)
        self.assertTrue(self.prompt(cmd).endswith("\n\nX\n"))
        self.assertIn("--system-prompt-snapshot", cmd)


class Fact(Base):
    def edit(self, point, payload):
        out = [p for p in parts_of(payload) if p["name"] != "findings"]
        out[0] = {**out[0], "text": "other"}
        return out + [{"name": "mine", "text": "+"}]

    def test_what_changed_is_a_fact_on_each_launch(self):
        mods = Stub(m=self.edit)
        h = self.host(mods)
        os.makedirs(os.path.join(h.state_dir, "modrun"))
        h.command("w", "s-1")
        h.note_prompt("w")
        self.assertEqual(mods.emitted, [("mod.prompt", "w", {"parts": {"m": {"added": ["mine"], "replaced": ["language"],
                                                                             "removed": ["findings"]}},
                                                             "input": [], "channel": "core", "owner": False})])
        mods.answers["m"] = lambda p, d: parts_of(d)
        h.command("w", "s-1")
        h.note_prompt("w")
        self.assertEqual(mods.emitted[-1][2]["parts"], {})   # stopped changing — the entry is removed
        h.note_prompt("w")
        self.assertEqual(len(mods.emitted), 2)               # without command — there was no launch

    def test_a_hub_that_never_had_modules_writes_no_fact(self):
        mods = Stub()
        h = self.host(mods)
        h.command("w", "s-1")
        h.note_prompt("w")
        self.assertEqual(mods.emitted, [])

    def test_the_fact_comes_only_after_the_process_started(self):
        mods = Stub(m=self.edit)
        h = self.host(mods)
        os.makedirs(os.path.join(h.state_dir, "modrun"))
        h.claude = [os.path.join(h.state_dir, "no-such-program")]
        with self.assertRaises(OSError):
            h.spawn("w", "s-1")
        self.assertEqual(mods.emitted, [])
        h.claude = [sys.executable, "-c", "pass"]
        self.assertEqual(h.spawn("w", "s-1"), "spawned")
        self.assertEqual([e[0] for e in mods.emitted], ["mod.prompt"])

    def test_a_launch_that_did_not_start_leaves_nothing_for_the_next(self):
        mods = Stub(m=self.edit)
        h = self.host(mods, fresh=False)
        os.makedirs(os.path.join(h.state_dir, "modrun"))
        h.command("w", "s-1")                 # the launch did not come up: no note_prompt
        h.command("w", "s-1", resume=True)    # an old Claude Code on --resume: the conversation's addendum is unchanged
        h.note_prompt("w")
        self.assertEqual([e[2] for e in mods.emitted], [{"input": [], "channel": "core", "owner": False}])

    def test_a_reorder_is_a_change(self):
        mods = Stub(m=lambda p, d: list(reversed(parts_of(d))))
        h = self.host(mods)
        os.makedirs(os.path.join(h.state_dir, "modrun"))
        self.assertTrue(self.prompt(h.command("w", "s-1")).startswith(streamjson.SKILLS))
        h.note_prompt("w")
        self.assertTrue(mods.emitted[0][2]["parts"]["m"]["moved"])

    def test_hub_show_says_which_module_changed_what(self):
        st = reducer.empty()
        r = views.Reader()
        seq = 0
        for kind, data in (("session.requested", {"session_id": "s", "cwd": "/w"}),
                           ("mod.prompt", {"parts": {"m": {"added": ["mine"], "replaced": ["autonomy"],
                                                           "removed": ["findings"], "moved": ["rules"]}}, "input": [],
                                           "channel": "core", "owner": False}),
                           ("mod.prompt", {"input": ["n"], "channel": "core", "owner": False})):
            seq += 1
            ev = {"seq": seq, "ts": 1000 + seq, "type": kind, "entity": "w", "data": data}
            st, _, why = reducer.reduce(st, ev)
            self.assertIsNone(why)
            r.feed(ev)
        self.assertEqual(st["sessions"]["w"]["mod_prompt"]["parts"]["m"]["added"], ["mine"])
        i18n.use("ru")   # a Russian owner's terminal
        try:
            text = "\n".join(views.show(r, "w"))
        finally:
            i18n.use("en")
        did = "; ".join(f"{i18n.tr('ru', 'core.modprompt.' + k)} {x}" for k, x in
                        (("added", "mine"), ("replaced", "autonomy"), ("removed", "findings"), ("moved", "rules")))
        self.assertIn(i18n.tr("ru", "core.modprompt.parts", module="m", what=did), text)   # the owner's terminal
        self.assertIn(i18n.tr("ru", "core.modprompt.input", module="n"), text)
        text = "\n".join(views.show(r, "w"))   # a session's process reads English
        self.assertIn("module m: addendum — added mine; replaced autonomy; removed findings; moved rules", text)
        self.assertIn("module n: changes the text of the session's messages", text)

    def test_the_fact_is_only_the_cores_and_only_about_a_session(self):
        st = reducer.empty()
        st, _, _ = reducer.reduce(st, {"seq": 1, "ts": 1, "type": "session.requested", "entity": "w",
                                       "data": {"session_id": "s"}})
        _, _, why = reducer.reduce(st, {"seq": 2, "ts": 2, "type": "mod.prompt", "entity": "w",
                                        "data": {"input": ["n"], "channel": "cli", "owner": True}})
        self.assertTrue(why)
        _, _, why = reducer.reduce(st, {"seq": 3, "ts": 3, "type": "mod.prompt", "entity": "nobody",
                                        "data": {"input": ["n"], "channel": "core", "owner": False}})
        self.assertTrue(why)


class Input(Base):
    def live(self, h, name="w"):
        pipe = Pipe()
        popen = type("P", (), {"stdin": pipe, "poll": lambda self: None, "pid": 1})()
        h.procs[name] = StreamProcess(popen)
        return pipe

    def written(self, pipe):
        return [json.loads(x)["message"]["content"] for x in pipe.lines]

    def test_the_module_rewrites_the_text_the_session_gets(self):
        mods = Stub(m=lambda p, d: d["text"].replace("[hub:m1] ", "") + " (signature)")
        mods.answers["m"].points = ("prompt.input",)
        h = self.host(mods)
        pipe = self.live(h)
        self.assertEqual(h.type("w", "[hub:m1] hello", message="m1"), "sent")
        self.assertEqual(self.written(pipe), ["hello (signature)"])
        self.assertEqual(mods.calls[0][2], {"session": "w", "text": "[hub:m1] hello", "message": "m1"})
        self.assertEqual(h.type("w", "[hub:m1] hello", message="m1"), "already sent")   # bookkeeping belongs to the core, not to markers
        h.type("w", "[hub:m2] more", message="m2")
        self.assertEqual([e[2]["input"] for e in mods.emitted], [["m"]])   # one fact per process
        self.live(h)                                                        # a new process — again
        h.type("w", "[hub:m3] and more", message="m3")
        self.assertEqual([e[2]["input"] for e in mods.emitted], [["m"], ["m"]])

    def test_a_failing_module_leaves_the_text_and_images_stay(self):
        mods = Stub(m=lambda p, d: None)
        mods.answers["m"].points = ("prompt.input",)
        h = self.host(mods)
        pipe = self.live(h)
        h.type("w", "[hub:m1] as is", message="m1")
        self.assertEqual(self.written(pipe), ["[hub:m1] as is"])
        self.assertEqual(mods.emitted, [])

    def test_without_modules_the_text_is_untouched(self):
        h = self.host()
        pipe = self.live(h)
        h.type("w", "[hub:m1] as is", message="m1")
        self.assertEqual(self.written(pipe), ["[hub:m1] as is"])


class Schema(unittest.TestCase):
    def test_answers_are_checked(self):
        parts, text = modrun.POINTS["prompt.parts"]["check"], modrun.POINTS["prompt.input"]["check"]
        self.assertEqual(parts({"parts": [{"name": "a", "text": "x"}]}, {}), [{"name": "a", "text": "x"}])
        self.assertEqual(parts({"parts": []}, {}), [])
        for bad in (None, {}, {"parts": "x"}, {"parts": [{"name": "a"}]}, {"parts": [{"name": "", "text": "x"}]},
                    {"parts": [{"name": "a", "text": 1}]}, {"parts": [{"name": "a", "text": "x"}, {"name": "a", "text": "y"}]}):
            with self.assertRaises(modrun.BadResult):
                parts(bad, {})
        self.assertEqual(text({"text": "y"}, {}), "y")
        for bad in (None, {}, {"text": 1}, {"text": None}, {"text": ""}, {"text": " \n"}):
            with self.assertRaises(modrun.BadResult):
                text(bad, {})

    def test_prompt_points_need_the_prompt_right(self):
        ctx = {"name": "m", "version": "1.0.0", "capabilities": []}
        with self.assertRaises(modrun.BadResult):
            modrun._hello({"name": "m", "version": "1.0.0", "points": ["prompt.parts"]}, ctx)
        got = modrun._hello({"name": "m", "version": "1.0.0", "points": ["prompt.parts", "prompt.input"]},
                            {**ctx, "capabilities": ["prompt"]})
        self.assertEqual(got["points"], ["prompt.input", "prompt.parts"])
        self.assertEqual((modrun.budget_of("prompt.parts"), modrun.budget_of("prompt.input")), (1.0, 1.0))

    def test_diff_names_added_replaced_removed(self):
        before = [{"name": "a", "text": "1"}, {"name": "b", "text": "2"}, {"name": "c", "text": "3"}]
        after = [{"name": "b", "text": "2!"}, {"name": "a", "text": "1"}, {"name": "d", "text": "4"}]
        self.assertEqual(modprompt.diff(before, after), {"added": ["d"], "replaced": ["b"], "removed": ["c"],
                                                         "moved": ["a"]})
        self.assertEqual(modprompt.diff(before, before), {})
        names = [{"name": n, "text": n} for n in ("language", "background", "correction", "findings")]
        self.assertEqual(modprompt.diff(names, names[1:] + names[:1]), {"moved": ["language"]})   # one, not all


class Process(Base):
    """A real module process (tests/fakes/fake_module.py) under the hubd stand: a call, a failure, suspension."""

    def stand(self, how):
        s = Stand(self, points=["prompt.parts"], caps=["prompt"])
        s.control(points=["prompt.parts"], **{"prompt.parts": how})
        s.launch()
        s.until_ready()
        h = StreamHost(s.state_dir, claude=("echo",))
        h.fresh_prompt = False
        h.modules = s.hub.modules.mods
        return s, h

    def test_hubd_gives_the_host_its_modules(self):
        s = Stand(self)
        self.assertIs(s.hub.host.modules, s.hub.modules.mods)

    def test_the_modules_answer_becomes_the_prompt(self):
        s, h = self.stand({"do": "ok", "answer": {"parts": [{"name": "only", "text": "ONLY THIS"}]}})
        self.assertEqual(self.prompt(h.command("w", "s-1")), "ONLY THIS\n")
        body = s.calls("prompt.parts")[0]["body"]
        self.assertEqual([p["name"] for p in body["parts"]], CORE)
        h.note_prompt("w")
        self.assertEqual([e["data"]["parts"] for e in s.events("mod.prompt")],
                         [{"fake": {"added": ["only"], "removed": CORE}}])

    def test_silent_garbage_or_dead_module_leaves_the_core_parts(self):
        core = self.prompt(self.host().command("w", "s-1"))
        for how in ({"do": "silent"}, {"do": "garbage"}, {"do": "ok", "answer": {"parts": [{"name": "x"}]}}):
            s, h = self.stand(how)
            t0 = time.monotonic()
            self.assertEqual(self.prompt(h.command("w", "s-1")), core)
            self.assertLess(time.monotonic() - t0, modrun.budget_of("prompt.parts") + 1.0)
            errs = [e["data"] for e in s.events("module.error")]
            self.assertEqual([e["hook"] for e in errs], ["prompt.parts"], how)


if __name__ == "__main__":
    unittest.main()
