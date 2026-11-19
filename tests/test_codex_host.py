"""Codex (experimental): a hub session on the Codex CLI — a `codex app-server` host
next to claude, with a fake codex (tests/fakes/fake_codex.py). The model `codex/<model>` on a session moves it to Codex;
the others stay on claude, as they were. Delivery is by fact: a turn with a marker is recognised by the userMessage in
the Codex stream, there are no hooks."""
import tests  # noqa: F401 — do not see the machine's settings (~/.config/hub-core), see tests/__init__.py
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

import platforms

from core import journal, spool
from core.daemon import Hub
from hosts.codex import CodexHost, MuxHost
from hosts.streamjson import StreamHost

HERE = os.path.dirname(__file__)
FAKE_CLAUDE = os.path.join(HERE, "fakes", "fake_claude_stream.py")
FAKE_CODEX = os.path.join(HERE, "fakes", "fake_codex.py")
MODEL = "codex/gpt-test"


class CodexHostTest(unittest.TestCase):
    MODEL_SEEN = "gpt-test"   # the model Codex was started with
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubcx-")
        self.argv = os.path.join(self.state, "argv")
        self.codex_home = os.path.join(self.state, "codex-home")
        self.hub = self.new_hub()
        self.hub.start()

    def tearDown(self):
        self.hub.host.kill_server()
        time.sleep(0.2)
        platforms.current.rmtree(self.state)

    def new_hub(self, extra_env=None):
        env = {"FAKE_TURN_S": "0.2", "FAKE_ARGV_DIR": self.argv, **(extra_env or {})}
        stream = StreamHost(self.state, claude=(sys.executable, FAKE_CLAUDE), env=env)
        codex = CodexHost(self.state, codex=(sys.executable, FAKE_CODEX), env=env)
        return Hub(self.state, MuxHost(stream, codex), registry_dir=os.path.join(self.state, "registry"))

    def cx(self):
        """The Codex host: in a mix, its half; in a hub entirely on Codex, the host itself."""
        return self.hub.host.codex if isinstance(self.hub.host, MuxHost) else self.hub.host

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

    def send(self, to, text, **extra):
        before = set(self.hub.state["messages"])
        self.cli("message.received", to=to, text=text, **extra)
        self.until(lambda: set(self.hub.state["messages"]) - before)
        (mid,) = set(self.hub.state["messages"]) - before
        return mid

    def delivered(self, mid):
        return self.hub.state["messages"][mid]["state"] == "delivered"

    def said(self, name):
        return [e["data"]["text"] for e in self.events("session.said", name)]

    def codex_session(self, name):
        """A new session the owner moved to a codex/… model: the process is restarted, now as codex app-server."""
        self.cli("session.requested", name, cwd=self.state, session_id=f"sid-{name}")
        self.until(lambda: self.sess(name).get("state") == "idle")
        self.cli("session.model_set", name, model=MODEL, channel="web", owner=True)
        self.until(lambda: self.hub.host.host_of(name) == "codex" and self.sess(name).get("state") == "idle"
                   and self.sess(name).get("model") == MODEL)

    def test_message_reaches_codex_and_turn_is_seen(self):
        self.codex_session("a")
        mid = self.send("a", "hello", channel="web", owner=True)
        self.until(lambda: self.delivered(mid) and self.sess("a")["state"] == "idle"
                   and self.events("turn.result", "a"))
        started = self.events("turn.started", "a")
        self.assertEqual(started[-1]["data"]["marker"], mid)   # delivery is by the userMessage in the Codex stream
        self.assertTrue(any(t.startswith("ok: ") and "hello" in t for t in self.said("a")), self.said("a"))
        result = self.events("turn.result", "a")[-1]["data"]
        self.assertFalse(result["is_error"])
        self.assertIn("hello", result["text"])
        # the turn's tokens are the sum of its model calls (two in the fake), the conversation length is the last call
        self.assertEqual((result["tokens_in"], result["tokens_out"], result["context"]), (210, 25, 110))
        info = self.sess("a").get("info") or {}
        self.assertEqual(info.get("host"), "codex")
        self.assertEqual(info.get("model"), self.MODEL_SEEN)

    def test_tool_step_is_in_the_feed(self):
        self.codex_session("a")
        with open(os.path.join(self.state, "marker-file.txt"), "w") as f:
            f.write("x")
        mid = self.send("a", "/ls")
        self.until(lambda: self.delivered(mid) and self.events("turn.tool_result", "a"))
        tool = self.events("turn.tool", "a")[-1]["data"]
        self.assertEqual(tool["name"], "Bash")
        self.assertIn("ls", tool["input"]["command"])
        res = self.events("turn.tool_result", "a")[-1]["data"]
        self.assertEqual(res["id"], tool["id"])
        self.assertIn("marker-file.txt", res["text"])
        self.assertFalse(res["error"])

    def test_conversation_continues_after_the_process_restarts(self):
        # the codex process crashed mid-turn — the core starts it again, the conversation is the same (thread/resume)
        self.codex_session("a")
        mid = self.send("a", "first")
        self.until(lambda: self.delivered(mid) and self.sess("a")["state"] == "idle")
        self.send("a", "/crash")
        self.until(lambda: self.events("session.exited", "a"))
        self.until(lambda: self.sess("a").get("state") == "idle" and self.events("session.started", "a", source="resume"))
        mid = self.send("a", "after the crash")
        self.until(lambda: self.delivered(mid) and any("after the crash" in t for t in self.said("a")))
        self.assertTrue(any("after the crash" in t and "remembers" in t for t in self.said("a")), self.said("a"))
        self.assertEqual(self.sess("a").get("session_id"), "sid-a")   # the core keeps its own session id, the Codex thread is the host's

    def test_resume_failure_does_not_start_a_new_conversation(self):
        # a thread/resume failure other than "no such conversation" (network, sign-in) — no new thread with an empty history
        self.codex_session("a")
        mid = self.send("a", "first")
        self.until(lambda: self.delivered(mid) and self.sess("a")["state"] == "idle")
        self.cx().env["FAKE_RESUME_ERROR"] = "stream disconnected"
        spawns = len(self.events("host.done", "a", do="spawn"))
        self.send("a", "/crash")
        self.until(lambda: len(self.events("host.done", "a", do="spawn")) >= spawns + 2)
        with open(os.path.join(self.state, "sessions", "a", "codex-threads.json")) as f:
            threads = json.load(f)
        self.assertEqual(len(set(threads.values())), 1)   # the same thread, no second one
        fresh = [e for e in self.events("session.started", "a", source="startup") if e["src"] == "host"]
        self.assertEqual(len(fresh), 1)   # Codex started a thread once — when the session was moved
        del self.cx().env["FAKE_RESUME_ERROR"]
        self.until(lambda: self.events("session.started", "a", source="resume"), timeout=30)

    def slow_turns(self, step_s):
        """/slow of the fake in steps of step_s: with 0.2 s ones a slow machine ended the turn before the test's
        interrupt or insert came (winstand, 2026-11-05)."""
        self.hub.host.kill_server()
        self.hub = self.new_hub({"FAKE_TURN_S": str(step_s)})
        self.hub.start()

    def test_message_mid_turn_is_steered_into_the_same_turn(self):
        self.slow_turns(1.5)   # 4.5 s: the insert comes while the turn runs
        self.codex_session("a")
        first = self.send("a", "/slow")
        self.until(lambda: self.sess("a")["state"] == "busy")
        later = self.send("a", "more", now=True, channel="web", owner=True)   # urgent — mid-turn (like an insert into claude's stdin)
        self.until(lambda: self.delivered(first) and self.delivered(later) and self.sess("a")["state"] == "idle"
                   and self.events("turn.result", "a"))
        # one turn: Codex took the insert into the running turn (turn/steer), not as the next turn
        self.assertTrue(any("/slow" in t and "more" in t for t in self.said("a")), self.said("a"))
        self.assertEqual(len(self.events("turn.result", "a")), 1)

    def test_question_becomes_a_card_and_answer_reaches_codex(self):
        self.codex_session("a")
        self.send("a", "/ask")
        self.until(lambda: self.sess("a").get("asks"))
        ask = self.sess("a")["asks"][0]
        self.assertIn("Which color?", json.dumps(ask, ensure_ascii=False))
        self.until(lambda: self.sess("a")["state"] == "idle")   # the question is a card, it does not hold the turn
        self.cli("permission.answered", "a", request_id=ask["request_id"], behavior="allow",
                 answers={"Which color?": "blue"}, channel="web", owner=True)
        self.until(lambda: any("blue" in t for t in self.said("a")))

    def test_async_question_becomes_a_card_and_answer_reaches_codex(self):
        """Pilot 2026-10-19: the Codex model asks with the request_user_input_async tool — this is not a request to the
        client but an agentMessage with a questions field (schema 0.162: AsyncUserInputQuestion {title, options}); the
        turn goes on and ends. The question is a card, the owner's answer is the next message in the same thread."""
        self.codex_session("a")
        self.send("a", "/ask_async")
        self.until(lambda: self.sess("a").get("asks"))
        ask = self.sess("a")["asks"][0]
        q = ask["input"]["questions"][0]
        self.assertEqual((q["question"], [o["label"] for o in q["options"]]), ("Which color?", ["blue", "red"]))
        self.until(lambda: self.sess("a")["state"] == "idle" and self.events("turn.result", "a"))
        self.assertFalse([e for e in self.events("host.error", "a") if e["data"].get("do") == "respond"])
        self.assertFalse([t for t in self.said("a") if "- blue" in t])   # in the feed as a card, not also as a reply
        self.cli("permission.answered", "a", request_id=ask["request_id"], behavior="allow",
                 answers={"Which color?": "blue"}, channel="web", owner=True)
        self.until(lambda: any("blue" in t and "Which color?" in t for t in self.said("a")))
        self.assertFalse(self.sess("a").get("asks"))

    def test_async_question_refused_by_the_core_tells_the_session_why(self):
        """A core refusal with its own text (the owner is away — decide yourself; a mini-hub executor may not ask) goes
        to the session as an insert: Codex does not wait for an answer to an async question, and without it the session
        would wait for an answer that will not come. "Shown as a card" is not forwarded (an extra turn)."""
        self.codex_session("a")
        self.cli("owner.away", None, channel="web", owner=True)
        self.send("a", "/ask_async")
        self.until(lambda: any("The owner is away" in t for t in self.said("a")))
        # main since 2026-10: the card of an away owner stays (marked away) and waits for his return
        self.assertTrue(all(q.get("away") for q in self.sess("a").get("asks") or []))
        self.cli("owner.back", None, channel="web", owner=True)
        before = len(self.events("turn.started", "a"))
        self.send("a", "/ask_async")
        self.until(lambda: self.sess("a").get("asks") and self.sess("a")["state"] == "idle")
        time.sleep(0.5)   # portable: a check that nothing more starts — a pause is what it waits through
        self.hub.tick()
        self.assertEqual(len(self.events("turn.started", "a")), before + 1)   # the question's turn and nothing more

    def test_turn_result_is_every_final_message_of_the_turn(self):
        """Codex writes several messages per turn: the question to the owner is not always in the last one (pilot
        2026-10-19: the result is "waiting for your decision", the question is in the one before). The result is every
        final_answer message of the turn, without commentary."""
        self.codex_session("a")
        self.send("a", "/many")
        self.until(lambda: self.events("turn.result", "a"))
        text = self.events("turn.result", "a")[-1]["data"]["text"]
        self.assertIn("Done: a fix in the file.", text)
        self.assertIn("roll out to prod", text)
        self.assertNotIn("Looking at the code.", text)
        self.send("a", "/ask_async")
        self.until(lambda: len(self.events("turn.result", "a")) == 2)
        text = self.events("turn.result", "a")[-1]["data"]["text"]
        self.assertNotIn("Which color?", text)   # the question is a card, not a second "waiting for you" from the result (as with Claude)
        self.assertEqual(text, "Waiting for your decision.")
        self.send("a", "plain turn")   # without phases (old models) — as before: the last message
        self.until(lambda: len(self.events("turn.result", "a")) == 3)
        self.assertIn("plain turn", self.events("turn.result", "a")[-1]["data"]["text"])

    def test_interrupt_ends_the_turn(self):
        self.slow_turns(3)   # the interrupt ends it at the next step, long before its 9 s
        self.codex_session("a")
        self.send("a", "/slow")
        self.until(lambda: self.sess("a")["state"] == "busy")
        self.cli("session.interrupt_requested", "a", channel="web", owner=True)
        self.until(lambda: self.sess("a")["state"] == "idle" and self.events("turn.result", "a", is_error=True))
        self.assertFalse(self.events("turn.result", "a", is_error=False))   # ended by the interrupt, not by itself

    def test_compact_is_not_a_turn(self):
        # /compact on Codex is thread/compact/start, compaction runs as its own turn without input: the core gets session.compacted, not a result
        self.codex_session("a")
        mid = self.send("a", "first")
        self.until(lambda: self.delivered(mid) and self.sess("a")["state"] == "idle")
        results = len(self.events("turn.result", "a"))
        self.cli("session.compact_requested", "a", channel="web", owner=True)
        self.until(lambda: self.events("session.compacted", "a", ok=True))
        self.assertEqual(len(self.events("turn.result", "a")), results)
        mid = self.send("a", "after compaction")
        self.until(lambda: self.delivered(mid) and any("after compaction" in t for t in self.said("a")))

    def test_claude_login_does_not_reach_codex(self):
        # Codex does not get the Claude Code environment (sign-in, gateway, account token); the session gets its name and the hub
        self.cx().env.update({"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat-x", "ANTHROPIC_BASE_URL": "http://gw",
                                        "ANTHROPIC_CUSTOM_HEADERS": "x-hub: 1", "ANTHROPIC_API_KEY": "sk-ant-x"})
        self.codex_session("a")
        (path,) = glob.glob(os.path.join(self.argv, "codex-a-*.json"))
        with open(path) as f:
            dump = json.load(f)
        env = dump["env"]
        self.assertFalse([k for k in env if k.startswith(("CLAUDE", "ANTHROPIC"))], sorted(env))
        self.assertEqual(env["HUB_SESSION"], "a")
        self.assertEqual(env["HUB_STATE_DIR"], self.state)

    def test_other_sessions_stay_on_claude(self):
        self.codex_session("a")
        self.cli("session.requested", "b", cwd=self.state, session_id="sid-b")
        self.until(lambda: self.sess("b").get("state") == "idle")
        mid = self.send("b", "hello")
        self.until(lambda: self.delivered(mid) and self.said("b"))
        self.assertEqual(self.hub.host.host_of("b"), "stream")
        self.assertEqual(self.said("b")[-1], "ok: hello")   # the fake claude's reply

    def test_back_to_claude_model_moves_session_back(self):
        self.codex_session("a")
        self.cli("session.model_set", "a", model="default", channel="web", owner=True)
        self.until(lambda: self.hub.host.host_of("a") == "stream" and self.sess("a").get("state") == "idle")
        mid = self.send("a", "claude again")
        self.until(lambda: self.delivered(mid) and "ok: claude again" in self.said("a"))


class AnswerTest(unittest.TestCase):
    """The turn result and a question without a host: a long answer, a question with empty text."""

    def test_long_answer_keeps_the_last_message_whole(self):
        from types import SimpleNamespace
        from hosts import codex
        proc = SimpleNamespace(texts=[("final_answer", "a" * 5000), ("final_answer", "Question: ship to prod now?")],
                               last_text="Question: ship to prod now?")
        text = codex._answer(proc, limit=4000)
        self.assertLessEqual(len(text), 4000)
        self.assertTrue(text.startswith("aaa") and text.endswith("Question: ship to prod now?"), text[-60:])
        proc.texts = [("final_answer", "x" + "y" * 5000)]   # a single long one — from the start, as before
        self.assertEqual(codex._answer(proc, limit=4000), "x" + "y" * 3999)

    def test_a_long_result_goes_whole_up_to_the_journal_limit(self):
        # as from claude (hosts/streamjson): the journal's limit, not 4000; longer — cut with its length, lessons scanned
        from hosts import codex
        from core import journal
        host = codex.CodexHost.__new__(codex.CodexHost)
        proc = codex.CodexProcess.__new__(codex.CodexProcess)
        proc.texts, proc.last_text, proc.spent, proc.usage = [("final_answer", "x" * 6000)], "", [0, 0], {}
        proc.turn = proc.compacting = proc.thread = None
        proc.started = None
        host.procs, host._flush = {"a": proc}, lambda p: None
        facts = host._line("a", {"method": "turn/completed", "params": {"turn": {"id": "t1", "status": "completed"}}})
        res = next(f[2] for f in facts if f[0] == "turn.result")
        self.assertEqual(len(res["text"]), 6000)
        self.assertNotIn("cut", res)
        proc.texts = [("final_answer", "y" * (journal.TEXT_MAX + 10) + "\n\nLessons\n- one lesson")]
        facts = host._line("a", {"method": "turn/completed", "params": {"turn": {"id": "t2", "status": "completed"}}})
        res = next(f[2] for f in facts if f[0] == "turn.result")
        self.assertEqual((len(res["text"]), res["cut"]), (journal.TEXT_MAX, len(proc.texts[0][1])))
        self.assertIn("lessons", res)

    def test_only_the_cores_card_reply_is_not_forwarded(self):
        from hosts import codex
        from core import reducer
        self.assertTrue(codex._detached(reducer.ASK_DETACHED.format(qid="q709", hours=24)))
        self.assertFalse(codex._detached("The owner is away until 18:00" + reducer.ASK_DEFERRED))
        self.assertFalse(codex._detached(reducer.ASK_DETACHED.format(qid="q709", hours=24) + " And more."))

    def test_question_with_empty_text_is_still_a_card(self):
        from hosts import codex
        host = codex.CodexHost.__new__(codex.CodexHost)
        proc = codex.CodexProcess(None, "sid", False)
        proc.thread = "t"
        host.procs = {"a": proc}
        facts = host._line("a", {"method": "item/completed", "params": {"threadId": "t", "item": {
            "type": "agentMessage", "id": "call_1", "text": "", "questions": [{"title": "Yes?", "options": None}]}}})
        self.assertEqual([f[0] for f in facts], ["permission.requested"])
        self.assertEqual(facts[0][2]["input"]["questions"][0]["question"], "Yes?")
        self.assertEqual(proc.texts, [])
        for bad in (["text"], {"title": "Yes?"}, [{"options": ["a"]}]):   # not by the schema — an ordinary message
            facts = host._line("a", {"method": "item/completed", "params": {"threadId": "t", "item": {
                "type": "agentMessage", "id": "m", "text": "result", "questions": bad}}})
            self.assertEqual([f[0] for f in facts], ["session.said"], bad)


class WholeHubOnCodexTest(CodexHostTest):
    """A hub entirely on Codex (hub.json host: codex): no claude at all, every session is Codex with the hub's default
    model; the hub has its own Codex folder (CODEX_HOME in the state folder), the personal ~/.codex takes no part."""

    MODEL_SEEN = "gpt-def"

    def new_hub(self, extra_env=None):
        env = {"FAKE_TURN_S": "0.2", "FAKE_ARGV_DIR": self.argv, **(extra_env or {})}
        host = CodexHost(self.state, codex=(sys.executable, FAKE_CODEX), env=env, all_sessions=True,
                         default_model="gpt-def", default_effort="low")
        return Hub(self.state, host, registry_dir=os.path.join(self.state, "registry"))

    def dumps(self, name):
        out = []
        for path in sorted(glob.glob(os.path.join(self.argv, f"codex-{name}-*.json"))):
            with open(path) as f:
                out.append(json.load(f))
        return out

    def repo(self):
        d = tempfile.mkdtemp(prefix="hubcx-repo-", dir=self.state)
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@x"}
        for args in (("init", "-q"), ("commit", "-q", "--allow-empty", "-m", "x")):
            subprocess.run(["git", "-C", d, *args], check=True, capture_output=True, env=env)
        return d

    def test_a_personal_codex_home_in_the_hubs_environment_does_not_reach_a_session(self):
        # the main risk of the experiment (report 2026-10-19): the owner's personal ~/.codex (his config, AGENTS.md, MCP,
        # connectors, memory, sign-in) in a hub session. Even when the hub itself was started with CODEX_HOME set,
        # the session gets the hub's folder, and nothing is written into the personal one
        personal = tempfile.mkdtemp(prefix="hubcx-personal-")
        from unittest import mock
        with mock.patch.dict(os.environ, {"CODEX_HOME": personal}):
            self.cli("session.requested", "a", cwd=self.state, session_id="sid-a")
            self.until(lambda: self.sess("a").get("state") == "idle")
        (dump,) = self.dumps("a")
        self.assertEqual(dump["env"]["CODEX_HOME"], os.path.join(self.state, "codex-home"))
        self.assertEqual(os.listdir(personal), [])

    def test_any_session_is_codex_with_the_hub_model_and_own_codex_home(self):
        self.cli("session.requested", "a", cwd=self.state, session_id="sid-a")
        self.until(lambda: self.sess("a").get("state") == "idle")
        mid = self.send("a", "hello")
        self.until(lambda: self.delivered(mid) and self.said("a"))
        info = self.sess("a")["info"]
        self.assertEqual((info["host"], info["model"]), ("codex", "gpt-def"))
        home = os.path.join(self.state, "codex-home")
        (dump,) = self.dumps("a")
        self.assertEqual(dump["env"]["CODEX_HOME"], home)   # the hub's own Codex folder, not the owner's ~/.codex
        with open(os.path.join(home, "config.toml")) as f:
            conf = f.read()
        self.assertIn('project_doc_fallback_filenames = ["CLAUDE.md"]', conf)   # the folder's CLAUDE.md is Codex's instructions
        self.assertIn("apps = false", conf)   # without the ChatGPT account's connectors

    def test_a_project_codex_config_cannot_turn_the_isolation_off(self):
        # probe 2026-11-05 on codex 0.162.1: a project's .codex/config.toml is a layer above the hub's config.toml, and
        # `[features] hooks = false` there silently dropped the core's bans (the denied command ran), memories and
        # multi_agent came on. The session's command line is above the project: the features are pinned there (the
        # subagents the hub turns on itself — `--enable`, also above the project: hosts/codex_items.py)
        self.cli("session.requested", "a", cwd=self.repo(), session_id="sid-a")
        self.until(lambda: self.sess("a").get("state") == "idle")
        (dump,) = self.dumps("a")
        argv = dump["argv"]
        pins = {argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "-c"}
        self.assertLessEqual({"features.hooks=true", "features.memories=false",
                              "features.apps=false", "features.plugins=false", "features.remote_plugin=false"}, pins)
        enabled = {argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--enable"}
        self.assertLessEqual({"multi_agent", "multi_agent_v2"}, enabled)
        self.assertFalse({p for p in pins if p.startswith("features.multi_agent")})
        self.assertNotIn("--disable", argv)   # a feature the version does not know would not start app-server at all
        with open(os.path.join(self.state, "codex-home", "config.toml")) as f:
            conf = f.read()
        # the account's remote plugins brought their skills into a session; MCP sign-ins — a file, not the keychain
        self.assertIn("plugins = false", conf)
        self.assertIn('mcp_oauth_credentials_store = "file"', conf)

    def test_the_users_own_skills_do_not_reach_a_session(self):
        # Codex reads $HOME/.agents/skills (its USER scope) into every thread — a session of a hub on Claude does not get
        # ~/.claude/skills either; Codex switches skills off only one SKILL.md at a time, links followed
        from unittest import mock
        home = tempfile.mkdtemp(prefix="hubcx-home-")
        store = tempfile.mkdtemp(prefix="hubcx-store-")
        self.addCleanup(platforms.current.rmtree, home)
        self.addCleanup(platforms.current.rmtree, store)
        for d in (os.path.join(home, ".agents", "skills", "mine"), os.path.join(store, "linked")):
            os.makedirs(d)
            with open(os.path.join(d, "SKILL.md"), "w") as f:
                f.write("---\nname: x\ndescription: x\n---\n")
        from tests import winsafe   # an ordinary Windows user has no right to links — then without the linked one
        linked = winsafe.try_symlink(os.path.join(store, "linked"), os.path.join(home, ".agents", "skills", "linked"))
        with mock.patch.dict(os.environ, {"HOME": home}):
            self.cli("session.requested", "a", cwd=self.state, session_id="sid-a")
            self.until(lambda: self.sess("a").get("state") == "idle")
        off = self.cx().procs["a"].start_params["config"]["skills"]["config"]
        want = [os.path.join(home, ".agents", "skills", "mine", "SKILL.md")]
        if linked:
            want.append(os.path.join(home, ".agents", "skills", "linked", "SKILL.md"))
        self.assertEqual(sorted(e["path"] for e in off if "path" in e), sorted(want))
        self.assertTrue(all(e["enabled"] is False for e in off))

    def test_the_users_own_skills_by_the_windows_profile_folder_too(self):
        # on Windows Codex's home is the profile folder and HOME is usually unset
        from hosts import codex_set
        home = tempfile.mkdtemp(prefix="hubcx-prof-")
        self.addCleanup(platforms.current.rmtree, home)
        os.makedirs(os.path.join(home, ".agents", "skills", "mine"))
        with open(os.path.join(home, ".agents", "skills", "mine", "SKILL.md"), "w") as f:
            f.write("---\nname: x\ndescription: x\n---\n")
        want = [os.path.join(home, ".agents", "skills", "mine", "SKILL.md")]
        for env in ({"USERPROFILE": home}, {"HOME": home, "USERPROFILE": home}):
            off = codex_set.thread_config({}, env)["skills"]["config"]
            self.assertEqual([e["path"] for e in off if "path" in e], want)

    def test_codex_skill_installer_is_off_in_a_session(self):
        # skill-installer puts skills into $CODEX_HOME/skills — the hub's Codex folder, shared by every session
        self.cli("session.requested", "a", cwd=self.state, session_id="sid-a")
        self.until(lambda: self.sess("a").get("state") == "idle")
        off = self.cx().procs["a"].start_params["config"]["skills"]["config"]
        self.assertIn({"name": "skill-installer", "enabled": False}, off)

    def test_an_mcp_server_gets_what_claude_gives_it(self):
        # a real claude (probe 2026-11-05) gives a stdio MCP server its whole environment and expands ${NAME} and
        # ${NAME:-default} in --mcp-config; Codex passes only HOME, PATH, USER… and expands nothing — hub-browser lost
        # HUB_STATE_DIR, a project's ${TOKEN} would go as the literal string
        from unittest import mock
        repo = self.repo()
        with open(os.path.join(repo, ".mcp.json"), "w") as f:
            json.dump({"mcpServers": {
                "local": {"command": "${PROBE_BIN}/srv", "args": ["--token", "${PROBE_TOKEN}", "${PROBE_NONE:-dflt}"],
                          "env": {"KEY": "${PROBE_TOKEN}", "KEEP": "${PROBE_NONE}"}},
                "remote": {"type": "http", "url": "https://mcp.example.test/${PROBE_PATH}",
                           "headers": {"Authorization": "Bearer ${PROBE_TOKEN}"}}}}, f)
        with mock.patch.dict(os.environ, {"PROBE_BIN": "/opt/x", "PROBE_TOKEN": "t0", "PROBE_PATH": "v1"}):
            self.cli("session.requested", "a", cwd=repo, session_id="sid-a")
            self.until(lambda: self.sess("a").get("state") == "idle")
        servers = self.cx().procs["a"].start_params["config"]["mcp_servers"]
        local, remote = servers["local"], servers["remote"]
        self.assertEqual((local["command"], local["args"]), ("/opt/x/srv", ["--token", "t0", "dflt"]))
        self.assertEqual(local["env"], {"KEY": "t0", "KEEP": "${PROBE_NONE}"})   # unknown, no default: as written
        self.assertIn("HUB_SESSION", local["env_vars"])   # the session's environment goes to the server
        self.assertIn("PROBE_TOKEN", local["env_vars"])
        self.assertNotIn("KEY", local["env_vars"])        # its own env wins
        self.assertFalse([n for n in local["env_vars"] if n.startswith(("ANTHROPIC", "CLAUDE"))])
        self.assertEqual(remote, {"url": "https://mcp.example.test/v1", "http_headers": {"Authorization": "Bearer t0"}})

    def test_executor_of_a_specialist_is_codex_too(self):
        # a task executor asks for a claude pair (sonnet/high by --work) — on a hub on Codex this is the hub's model
        self.cli("session.requested", "ag", cwd=self.repo(), session_id="sid-ag", role="agent", about="profile")
        self.until(lambda: self.sess("ag").get("state") in ("idle", "parked"))
        self.cli("task.run_requested", "ag-task", task="count something", work="normal", channel="session",
                 owner=False, sender="ag")
        self.until(lambda: self.sess("ag-task").get("state") == "idle"
                   and any(e["entity"] == "ag-task" for e in self.events("turn.result")))
        self.assertEqual(self.sess("ag-task")["info"]["model"], "gpt-def")
        self.assertTrue(any(m["session"] == "ag" and "ag-task" in m.get("origin", "")
                            for m in self.hub.state["messages"].values()))

    def test_hub_setting_picks_codex_host_over_tmux(self):
        from unittest import mock
        from core import daemon
        # no codex: make_host trusts the hook through `codex app-server` (hooks/list) — not the machine's codex in a test
        missing = os.path.join(self.state, "no-codex")
        with mock.patch.object(daemon.catalog, "load_config", return_value={"host": "codex", "codex": {"model": "m1"}}), \
                mock.patch.dict(os.environ, {"HUB_HOST": "tmux", "HUB_CODEX_BIN": missing}):
            host = daemon.make_host(self.state)
        self.assertEqual(host.codex, [missing])
        self.assertIsInstance(host, CodexHost)
        self.assertEqual((host.all_sessions, host.default_model), (True, "m1"))

    # from the shared class — only about the claude and Codex mix: there is no claude here
    test_other_sessions_stay_on_claude = test_back_to_claude_model_moves_session_back = None

    def codex_session(self, name):
        self.cli("session.requested", name, cwd=self.state, session_id=f"sid-{name}")
        self.until(lambda: self.sess(name).get("state") == "idle" and (self.sess(name).get("info") or {}).get("host"))



class CodexNote(unittest.TestCase):
    def test_his_own_ask_me_wins_over_decide_yourself(self):
        # pilot 2026-10-19: "end the turn with a question to me… you will not go on without my answer" -> "this is a
        # technical choice, by your rule no question is needed" — Codex reads "decide yourself" literally, over his request
        from hosts import codex
        self.assertIn("directly asks you to ask them — ask", codex.NOTE)



class PersonalCodexHome(unittest.TestCase):
    """Every way the hub starts codex on a hub on Codex — the core's model and the critic, the sign-in, `hub take` —
    goes with the hub's CODEX_HOME, not one from the environment (the owner's personal ~/.codex)."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubcx-state-")
        self.addCleanup(platforms.current.rmtree, self.state)
        self.home = os.path.join(self.state, "codex-home")
        self.base = {"CODEX_HOME": "/home/x/.codex", "PATH": os.environ.get("PATH", "")}

    def test_core_model_and_critic(self):
        from core import codexmodel
        self.assertEqual(codexmodel.env(self.state, self.base)["CODEX_HOME"], self.home)

    def test_take(self):
        from core import codexmodel
        os.makedirs(os.path.join(self.state, "sessions", "a"))
        with open(os.path.join(self.state, "sessions", "a", "codex-threads.json"), "w") as f:
            json.dump({"sid-a": "thr-1"}, f)
        cmd, env = codexmodel.take_command(self.state, "a", "sid-a", base=self.base, codex="codex")
        self.assertEqual(env["CODEX_HOME"], self.home)
        self.assertEqual(cmd[-1], "thr-1")

    def test_sign_in(self):
        from unittest import mock
        from core import codexcli
        seen = {}

        def popen(cmd, **kw):
            seen.update(kw["env"])
            raise OSError("no codex in this test")
        with mock.patch.dict(os.environ, {"CODEX_HOME": "/home/x/.codex"}), \
                mock.patch.object(codexcli.subprocess, "Popen", popen):
            with self.assertRaises(OSError):
                codexcli.Rpc(self.state)
        self.assertEqual(seen["CODEX_HOME"], self.home)


    def test_doctor(self):
        # 2026-10-29 stand: `codex --version` of hub doctor ran without CODEX_HOME and wrote codex's helper folder into
        # the personal ~/.codex/tmp/arg0
        from unittest import mock
        from core import modcli
        seen = []

        def run(cmd, **kw):
            seen.append(kw.get("env") or {})
            raise OSError("no codex in this test")
        with mock.patch.object(modcli.subprocess, "run", run), mock.patch("hosts.codex.codex_bin", return_value=sys.executable), \
                mock.patch.dict(os.environ, {"CODEX_HOME": "/home/x/.codex"}), \
                mock.patch("core.codexcli.Rpc", side_effect=OSError("no codex in this test")):
            modcli.codex_checks(self.state)
        self.assertEqual(seen[0].get("CODEX_HOME"), self.home)



class ReviewFixes(unittest.TestCase):
    """Review high of 2026-10-29 on the port to main."""

    def host(self):
        import io
        import types
        from hosts import codex
        host = codex.CodexHost.__new__(codex.CodexHost)
        popen = types.SimpleNamespace(stdin=io.StringIO(), poll=lambda: None, pid=1)
        proc = codex.CodexProcess(popen, "sid", False)
        proc.thread = "t"
        host.procs = {"a": proc}
        return host, proc, popen

    def sent(self, popen, method):
        return [json.loads(x) for x in popen.stdin.getvalue().splitlines() if json.loads(x).get("method") == method]

    def test_an_input_refused_every_time_is_not_retried_forever(self):
        # effort the model does not know, a vanished image: Codex refuses every turn/start — at most RETRIES more tries
        from hosts import codex
        host, proc, popen = self.host()
        proc.backlog.append([{"type": "text", "text": "x"}])
        host._flush(proc)
        for _ in range(10):
            last = self.sent(popen, "turn/start")[-1]
            host._response("a", proc, {"id": last["id"], "error": {"code": -32600, "message": "unsupported effort"}})
        self.assertEqual(len(self.sent(popen, "turn/start")), codex.RETRIES + 1)
        self.assertEqual((proc.backlog, proc.starting, proc.refused), ([], False, {}))

    def test_a_race_with_a_turn_start_still_retries(self):
        host, proc, popen = self.host()
        proc.backlog.append([{"type": "text", "text": "x"}])
        host._flush(proc)
        first = self.sent(popen, "turn/start")[-1]
        host._response("a", proc, {"id": first["id"], "error": {"code": -32600, "message": "turn already active"}})
        again = self.sent(popen, "turn/start")[-1]
        self.assertNotEqual(again["id"], first["id"])
        host._response("a", proc, {"id": again["id"], "result": {"turn": {"id": "u1"}}})
        self.assertEqual((proc.turn, proc.refused), ("u1", {}))

    def test_compact_before_the_thread_is_ready_is_refused(self):
        host, proc, popen = self.host()
        proc.thread = None
        with self.assertRaises(RuntimeError):
            host.compact("a")
        self.assertEqual((proc.compacts, self.sent(popen, "thread/compact/start")), (0, []))

    def test_one_compaction_gives_one_fact(self):
        host, proc, popen = self.host()
        host.compact("a")
        host._line("a", {"method": "item/started", "params": {"threadId": "t", "turnId": "c1",
                                                               "item": {"type": "contextCompaction"}}})
        facts = host._line("a", {"method": "thread/compacted", "params": {"threadId": "t"}})
        facts += host._line("a", {"method": "turn/completed", "params": {"threadId": "t",
                                                                          "turn": {"id": "c1", "status": "completed"}}})
        self.assertEqual([f[0] for f in facts], ["session.compacted"])
        self.assertEqual(proc.compacts, 0)

    def test_config_keeps_the_hook_trust_across_starts(self):
        # every hubd start rewrote config.toml and lost trusted_hash: two app-server runs to trust the hook again
        from hosts import codex
        home = tempfile.mkdtemp(prefix="hubcx-home-")
        self.addCleanup(platforms.current.rmtree, home)
        codex.write_config(home)
        path = os.path.join(home, "config.toml")
        with open(path, "a") as f:
            f.write('\n[hooks.state."/x/hooks.json:pre_tool_use:0:0"]\ntrusted_hash = "h1"\n')
        with open(path) as f:
            before = f.read()
        os.utime(path, ns=(10 ** 18, 10 ** 18))   # its own time: a rewrite in the same tick of a coarse clock is seen too
        codex.write_config(home)
        with open(path) as f:
            self.assertEqual(f.read(), before)
        # portable: the time is set above (os.utime), not taken from the clock
        self.assertEqual(os.stat(path).st_mtime_ns, 10 ** 18)   # unchanged — not rewritten
        with open(path, "w") as f:   # an old file with a foreign setting and project trust: back to the core's, hook trust kept
            f.write('model = "x"\n\n[hooks.state."/x/hooks.json:pre_tool_use:0:0"]\ntrusted_hash = "h1"\n'
                    '\n[projects."/x/repo"]\ntrust_level = "trusted"\n')
        codex.write_config(home)
        with open(path) as f:
            text = f.read()
        self.assertTrue(text.startswith(codex.CONFIG))
        self.assertIn('trusted_hash = "h1"', text)
        self.assertNotIn('model = "x"', text)
        self.assertNotIn("[projects.", text)   # one per executor copy — would pile up; Codex adds it again itself

    def test_an_input_dropped_after_refusals_can_be_typed_again(self):
        # review 2026-10-29: the dropped input's message stayed in proc.sent — the core's retype answered "already sent"
        host, proc, popen = self.host()
        host.mod_input = lambda name, text, message=None: text
        self.assertEqual(host.type("a", "x", message="m1"), "sent")
        for _ in range(10):
            last = self.sent(popen, "turn/start")[-1]
            host._response("a", proc, {"id": last["id"], "error": {"code": -32600, "message": "unsupported effort"}})
        self.assertNotIn("m1", proc.sent)
        self.assertEqual(host.type("a", "x", message="m1"), "sent")

    def test_codex_compaction_inside_a_turn_is_not_the_compaction_turn(self):
        # a manual compact pending while a turn with input runs and Codex compacts inside it: the turn's result stays
        host, proc, popen = self.host()
        host.compact("a")
        host._line("a", {"method": "turn/started", "params": {"threadId": "t", "turn": {"id": "u1"}}})
        host._line("a", {"method": "item/started", "params": {"threadId": "t", "turnId": "u1", "item": {
            "type": "userMessage", "content": [{"type": "text", "text": "x"}]}}})
        host._line("a", {"method": "item/started", "params": {"threadId": "t", "turnId": "u1",
                                                               "item": {"type": "contextCompaction"}}})
        facts = host._line("a", {"method": "turn/completed", "params": {"threadId": "t",
                                                                         "turn": {"id": "u1", "status": "completed"}}})
        self.assertIn("turn.result", [f[0] for f in facts])
        self.assertEqual(proc.compacts, 1)   # the manual compaction is still ahead

    def test_mux_lines_after_the_poll_moment_wait_for_the_next_poll(self):
        import types
        lines = {"stream": (10.0, [(9.0, "turn.tool", "s", {})]), "codex": (12.0, [(8.0, "turn.tool", "c", {}),
                                                                                     (11.0, "turn.result", "c", {})])}
        stream = types.SimpleNamespace(stamped=lambda: lines["stream"])
        cx = types.SimpleNamespace(stamped=lambda: lines["codex"])
        mux = MuxHost(stream, cx)
        until, got = mux.stamped()
        self.assertEqual((until, [f[0] for f in got]), (10.0, [8.0, 9.0]))
        lines.update(stream=(13.0, []), codex=(13.5, []))
        until, got = mux.stamped()
        self.assertEqual((until, [f[0] for f in got]), (13.0, [11.0]))

    def test_a_codex_model_is_no_claude_flag(self):
        # a hub without the Codex host: /model codex/… must not start claude --model codex/… (it would fail every start)
        from core import catalog
        self.assertEqual(catalog.model_flags({}, "codex/gpt-x"), [])
        self.assertEqual(catalog.model_flags({}, "sonnet"), ["--model", "sonnet"])

    def test_codex_models_are_known_only_where_the_hub_has_the_codex_host(self):
        # a Claude hub refuses codex/… as main does (the session, the task's pin, /model); the daemon's codex.host
        # (a hub with "host": "codex" or HUB_CODEX=1) makes them known to the fold
        from core import catalog, reducer
        st, seq = reducer.empty(), [0]

        def ev(t, e=None, **d):
            seq[0] += 1
            nonlocal st
            st, _, why = reducer.reduce(st, {"seq": seq[0], "ts": 100 + seq[0], "type": t, "entity": e, "data": d})
            return why
        ev("session.requested", "maps", cwd="/m", session_id="s-m", channel="cli", owner=True)
        self.assertIsNotNone(ev("session.model_set", "maps", model=MODEL, channel="cli", owner=True))
        self.assertIsNone(ev("session.model_set", "maps", model="sonnet", channel="cli", owner=True))
        self.assertIsNotNone(ev("codex.host", None, on=True, channel="cli", owner=True))   # the core's fact only
        self.assertIsNone(ev("codex.host", None, on=True, channel="core"))
        self.assertIsNone(ev("session.model_set", "maps", model=MODEL, channel="cli", owner=True))
        self.assertEqual(st["sessions"]["maps"]["model"], MODEL)
        self.assertIsNone(ev("codex.host", None, on=False, channel="core"))
        self.assertIsNotNone(ev("session.model_set", "maps", model="codex/other", channel="cli", owner=True))
        self.assertFalse(catalog.known_model(MODEL))
        self.assertFalse(catalog.codex_host({}))
        self.assertTrue(catalog.codex_host({"host": "codex"}))


if __name__ == "__main__":
    unittest.main()
