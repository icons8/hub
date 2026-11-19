"""Codex (experimental): the life of a Codex session beyond its turns — hosts/codex_life.py.

- the recap after a big turn: the thread's tail (thread/turns/list) and the core's model give session.recap by "codex",
  which the fold keeps as the session's "away" line and the timeline shows, as Claude Code's /recap;
- the direction's context after a compaction (hooks/hub_hook.compacted_context, as the core's SessionStart hook gives it
  to claude) goes into the thread by thread/inject_items; the session's input waits until it is in; when Codex compacts
  by itself inside a turn (a contextCompaction item only) nothing waits;
- orphans of a previous hubd life are told by the tree marker (the app-server's command line has no session id);
- a system agent comes back with its conversation when its Codex thread is on disk.
The fake app-server is tests/fakes/fake_codex.py (not edited by the slices): its answers come from $FAKE_CODEX_REPLIES."""
import tests  # noqa: F401 — do not see the machine's settings (~/.config/hub-core), see tests/__init__.py
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import platforms

from core import codexmodel, journal, reducer, spool, system, views
from core.daemon import Hub
from hosts import codex_life
from hosts.codex import CodexHost, CodexProcess, MuxHost
from hosts.streamjson import StreamHost

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
FAKE_CLAUDE = os.path.join(HERE, "fakes", "fake_claude_stream.py")
FAKE_CODEX = os.path.join(HERE, "fakes", "fake_codex.py")
MODEL = "codex/gpt-test"
OWNER = {"channel": "cli", "owner": True}
# thread/turns/list as codex-cli 0.162.1 answers it (probe with a fake Responses API, 05.11), newest turn first
TURNS = {"data": [
    {"id": "t2", "status": "completed", "itemsView": "full", "items": [
        {"type": "userMessage", "id": "u2", "content": [{"type": "text", "text": "fix the feed loading", "text_elements": []}]},
        {"type": "commandExecution", "id": "c1", "command": "/bin/bash -lc 'git status'", "cwd": "/w",
         "status": "completed", "aggregatedOutput": "clean\n", "exitCode": 0},
        {"type": "agentMessage", "id": "a2", "text": "Fixed the loader, tests are next.", "phase": "final_answer"}]},
    {"id": "t1", "status": "completed", "itemsView": "full", "items": [
        {"type": "userMessage", "id": "u1", "content": [{"type": "text", "text": "look at the feed", "text_elements": []}]},
        {"type": "agentMessage", "id": "a1", "text": "The feed reloads twice.", "phase": "final_answer"}]}],
    "nextCursor": None}


def wait(pred, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class Bare(unittest.TestCase):
    """A CodexHost with one process whose stdin is a buffer: what the host writes is visible, lines are fed by hand."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubcxl-")
        self.addCleanup(platforms.current.rmtree, self.state)
        self.host = CodexHost(self.state, codex=(sys.executable, FAKE_CODEX))
        self.popen = types.SimpleNamespace(stdin=io.StringIO(), poll=lambda: None, pid=4242)
        self.proc = CodexProcess(self.popen, "sid", False)
        self.proc.thread, self.proc.host = "t", self.host
        self.proc.info["version"] = "0.162.1"
        self.host.procs["maps"] = self.proc

    def sent(self, method=None):
        rows = [json.loads(x) for x in self.popen.stdin.getvalue().splitlines()]
        return [r for r in rows if method is None or r.get("method") == method]

    def lines(self, timeout=10.0):
        """The host's own poll after the module's thread posted its line: the facts it gives."""
        self.assertTrue(wait(lambda: not self.host.inbox.empty(), timeout), "no line from the module's thread")
        return [f[1:] for f in self.host.stamped()[1]]


class Recap(Bare):
    def test_recap_reads_the_thread_tail_and_the_model_writes_the_line(self):
        self.assertEqual(self.host.recap("maps"), "sent")
        (req,) = self.sent("thread/turns/list")
        self.assertEqual(req["params"], {"threadId": "t", "limit": codex_life.RECAP_TURNS, "itemsView": "full",
                                         "sortDirection": "desc"})
        with mock.patch.object(codexmodel, "ask", return_value={"recap": "  Fixing the feed;\n tests next. "}) as ask:
            self.assertEqual(self.host._line("maps", {"id": req["id"], "result": TURNS}), [])
            facts = self.lines()
        self.assertEqual(facts, [("session.recap", "maps", {"text": "Fixing the feed; tests next.", "by": "codex"})])
        text, prompt, schema = ask.call_args.args
        # oldest first: what was asked, the steps and what the agent said — not the outputs
        self.assertLess(text.index("look at the feed"), text.index("fix the feed loading"))
        self.assertIn("Step Bash", text)
        self.assertIn("Fixed the loader", text)
        self.assertNotIn("clean", text)
        self.assertEqual((ask.call_args.kwargs["state_dir"], ask.call_args.kwargs["by"]), (self.state, "recap"))

    def test_no_line_when_the_model_fails_or_the_thread_tail_is_refused(self):
        self.host.recap("maps")
        (req,) = self.sent("thread/turns/list")
        with mock.patch.object(codexmodel, "ask", side_effect=codexmodel.Error("no sign-in")) as ask:
            self.host._line("maps", {"id": req["id"], "result": TURNS})
            for t in [t for t in threading.enumerate() if t.name == "codex-recap-maps"]:
                t.join(10)
        ask.assert_called_once()
        self.assertTrue(self.host.inbox.empty())
        self.host.recap("maps")
        req = self.sent("thread/turns/list")[-1]
        with mock.patch.object(codexmodel, "ask") as ask:
            self.assertEqual(self.host._line("maps", {"id": req["id"], "error": {"code": -1, "message": "x"}}), [])
        ask.assert_not_called()

    def test_one_recap_at_a_time(self):
        self.assertEqual(self.host.recap("maps"), "sent")
        self.assertEqual(self.host.recap("maps"), "skipped")   # its thread tail is on its way
        (req,) = self.sent("thread/turns/list")
        gate = threading.Event()
        with mock.patch.object(codexmodel, "ask", side_effect=lambda *a, **k: gate.wait(10) and {"recap": "x"}):
            self.host._line("maps", {"id": req["id"], "result": TURNS})
            self.assertEqual(self.host.recap("maps"), "skipped")   # the model is still writing it
            gate.set()
            self.lines()
        self.assertEqual(self.host.recap("maps"), "sent")
        codex_life._recaps(self.host).update({"a", "b"})                       # RECAP_AT_ONCE of the host's other sessions
        self.host._line("maps", {"id": self.sent("thread/turns/list")[-1]["id"], "error": {"code": -1, "message": "x"}})
        self.assertEqual(self.host.recap("maps"), "skipped")
        codex_life._recaps(self.host).clear()

    def test_the_limit_holds_for_tails_already_on_their_way(self):
        other = CodexProcess(types.SimpleNamespace(stdin=io.StringIO(), poll=lambda: None, pid=4343), "sid2", False)
        other.thread, other.host = "t2", self.host
        self.host.procs["docs"] = other
        codex_life._recaps(self.host).add("a")
        self.assertEqual(self.host.recap("maps"), "sent")    # one model call running, one tail asked: two
        self.assertEqual(self.host.recap("docs"), "skipped")
        codex_life._recaps(self.host).add("b")                        # the limit filled up while the tail was on its way
        with mock.patch.object(codexmodel, "ask") as ask:
            self.host._line("maps", {"id": self.sent("thread/turns/list")[-1]["id"], "result": TURNS})
        ask.assert_not_called()
        self.assertNotIn("maps", codex_life._recaps(self.host))

    def test_recap_needs_a_live_process_with_a_thread(self):
        with self.assertRaises(RuntimeError):
            self.host.recap("nobody")
        self.proc.thread = None
        with self.assertRaises(RuntimeError):
            self.host.recap("maps")

    def test_tail_keeps_the_latest(self):
        many = {"data": [{"items": [{"type": "agentMessage", "text": f"message {i} " + "x" * 1400}]} for i in range(20)]}
        text = codex_life.tail(many)
        self.assertLessEqual(len(text), codex_life.RECAP_CHARS)
        self.assertIn("message 0 ", text)        # data is newest first: message 0 is the latest
        self.assertNotIn("message 19 ", text)


class RecapFold(unittest.TestCase):
    """The fold keeps the host's recap as "away" (the sidebar's line of an idle session); the timeline shows it."""

    def fold(self, by):
        st = reducer.empty()
        for seq, (type, entity, data) in enumerate((
                ("session.requested", "maps", {"cwd": "/w", "session_id": "s1", **OWNER}),
                ("session.recap", "maps", {"text": "Goal, now, next.", "by": by})), 1):
            st, _, why = reducer.reduce(st, {"seq": seq, "ts": 1000.0 + seq, "type": type, "entity": entity, "data": data})
            self.assertIsNone(why)
        return st["sessions"]["maps"]

    def test_codex_recap_is_the_away_line_like_claudes(self):
        for by in ("claude", "codex"):
            sess = self.fold(by)
            self.assertEqual((sess.get("away") or {}).get("text"), "Goal, now, next.", by)
            self.assertNotIn("recap", sess)
        self.assertNotIn("away", self.fold("model"))   # the watcher's line stays the "what it works on" line

    def test_timeline_row(self):
        ev = {"seq": 2, "ts": 1.0, "type": "session.recap", "entity": "maps", "data": {"text": "Goal.", "by": "codex"}}
        st = reducer.empty()
        rows = views._items(st, ev)
        self.assertEqual([(n, r["kind"], r["text"]) for n, r in rows], [("maps", "sys", "※ recap: Goal.")])


class Context(Bare):
    """After a compaction a direction's session gets `hub direction resume` (here a stand-in script) as context."""

    def setUp(self):
        super().setUp()
        self.script = os.path.join(self.state, "ctx.py")
        self.say("CTX for the direction")
        patcher = mock.patch.object(codex_life, "HOOK", self.script)
        patcher.start()
        self.addCleanup(patcher.stop)

    def say(self, text, pause=0.3):
        with open(self.script, "w", encoding="utf-8") as f:
            f.write(f"import os, sys, time\ntime.sleep({pause})\nassert sys.argv[1:] == ['--compacted']\n"
                    + (f"sys.stdout.write({text!r} + ' ' + os.environ['HUB_SESSION'])\n" if text else ""))

    def compacted(self):
        """The core's compaction ends (the host's compaction turn completed)."""
        self.proc.compacts, self.proc.compacting, self.proc.turn = 1, "cmp", "cmp"
        return self.host._line("maps", {"method": "turn/completed", "params": {"turn": {"id": "cmp", "status": "completed"}}})

    def inject_answered(self, n=-1, error=None):
        rid = self.sent("thread/inject_items")[n]["id"]
        return self.host._line("maps", {"id": rid, **({"error": error} if error else {"result": {}})})

    def test_the_next_turn_waits_for_the_context_and_comes_after_it(self):
        facts = self.compacted()
        self.assertEqual(facts[0][0], "session.compacted")
        self.host.type("maps", "[hub:m7] next task", message="m7")
        self.assertEqual(self.sent("turn/start"), [])   # held: the context is on its way
        self.lines()
        (inject,) = self.sent("thread/inject_items")
        self.assertEqual(inject["params"], {"threadId": "t", "items": [{"type": "message", "role": "developer", "content": [
            {"type": "input_text", "text": "CTX for the direction maps"}]}]})
        self.assertEqual(self.sent("turn/start"), [])   # still held: not in the history yet
        self.inject_answered()
        (start,) = self.sent("turn/start")
        self.assertIn("next task", start["params"]["input"][0]["text"])
        self.assertNotIn("additionalContext", start["params"])   # needs experimentalApi: refused without it
        self.assertEqual(self.proc.held, set())

    def test_a_refused_inject_still_lets_the_input_go(self):
        self.compacted()
        self.host.type("maps", "[hub:m7] next", message="m7")
        self.lines()
        self.inject_answered(error={"code": -32600, "message": "no"})
        self.assertEqual(len(self.sent("turn/start")), 1)

    def test_no_context_releases_the_input_without_it(self):
        self.say("")
        self.compacted()
        self.host.type("maps", "[hub:m7] next", message="m7")
        self.lines()
        self.assertEqual(len(self.sent("turn/start")), 1)
        self.assertEqual(self.sent("thread/inject_items"), [])

    def test_a_failing_script_releases_the_input_too(self):
        with open(self.script, "w") as f:
            f.write("import sys\nsys.exit(3)\n")
        self.compacted()
        self.host.type("maps", "[hub:m7] next", message="m7")
        self.lines()
        self.assertEqual(len(self.sent("turn/start")), 1)
        self.assertEqual((self.proc.held, self.sent("thread/inject_items")), (set(), []))

    def test_an_older_compactions_late_context_is_dropped(self):
        self.compacted()
        self.say("NEWER", pause=0.6)
        self.compacted()
        self.assertTrue(wait(lambda: self.host.inbox.qsize() >= 2, 10))
        self.assertEqual([f[1:] for f in self.host.stamped()[1]], [])
        (inject,) = self.sent("thread/inject_items")
        self.assertEqual(inject["params"]["items"][0]["content"][0]["text"], "NEWER maps")
        self.assertEqual(len(self.proc.held), 1)   # only the newer one's, until its inject is answered
        self.inject_answered()
        self.assertEqual(self.proc.held, set())

    def own_compaction(self, turn="t5"):
        """Codex compacted by itself inside a session's turn: 0.162.1 sends only the contextCompaction item (live probe
        05.11: before the turn's userMessage or after a step), no thread/compacted."""
        self.proc.turn = turn
        item = {"type": "contextCompaction", "id": "cc1"}
        self.host._line("maps", {"method": "item/started", "params": {"threadId": "t", "turnId": turn, "item": item}})
        return self.host._line("maps", {"method": "item/completed", "params": {"threadId": "t", "turnId": turn, "item": item}})

    def test_the_held_input_does_not_wait_for_an_unanswered_inject_forever(self):
        self.compacted()
        self.host.type("maps", "[hub:m7] next", message="m7")
        with mock.patch.object(codex_life, "INJECT_WAIT", 0.2):
            self.lines()                     # the context: thread/inject_items goes, no answer ever
            self.assertEqual(self.sent("turn/start"), [])
            self.lines()                     # INJECT_WAIT later: released anyway
        self.assertEqual(len(self.sent("turn/start")), 1)
        self.assertEqual((self.proc.held, self.proc.life["inject"]), (set(), {}))

    def test_inside_a_running_turn_it_is_injected_without_a_hold(self):
        self.assertEqual(self.own_compaction(), [])
        self.host.type("maps", "insert", message="m8")   # an insertion mid-turn is not held
        self.assertEqual(len(self.sent("turn/steer")), 1)
        self.lines()
        (inject,) = self.sent("thread/inject_items")
        self.assertEqual(inject["params"]["items"][0]["content"][0]["text"], "CTX for the direction maps")
        self.assertEqual((self.proc.held, self.proc.life["inject"]), (set(), {}))

    def test_thread_compacted_inside_a_turn_asks_once_and_never_after_the_cores_compaction(self):
        # a Codex that also sends thread/compacted for its own compaction inside a turn (0.162.1 does not)
        self.own_compaction()
        self.lines()
        self.host._line("maps", {"method": "thread/compacted", "params": {"threadId": "t"}})   # the same turn: not again
        self.assertEqual([t for t in threading.enumerate() if t.name == "codex-context-maps"], [])
        self.proc.turn = None   # the core's compaction turn is over: a late thread/compacted is not Codex's own
        self.host._line("maps", {"method": "thread/compacted", "params": {"threadId": "t"}})
        self.assertEqual([t for t in threading.enumerate() if t.name == "codex-context-maps"], [])
        self.proc.turn = "t6"   # another turn, only thread/compacted: asked
        self.host._line("maps", {"method": "thread/compacted", "params": {"threadId": "t"}})
        self.lines()
        self.assertEqual(len(self.sent("thread/inject_items")), 2)

    def test_the_core_compaction_turn_asks_once_at_its_end(self):
        # the core's compaction turn has the same contextCompaction item: it is not "Codex compacted by itself"
        self.proc.compacts = 1
        item = {"type": "contextCompaction", "id": "cmp-1"}
        for method in ("item/started", "item/completed"):
            self.host._line("maps", {"method": method, "params": {"threadId": "t", "turnId": "cmp", "item": item}})
        self.assertEqual([t for t in threading.enumerate() if t.name == "codex-context-maps"], [])   # none started yet
        self.assertEqual((self.proc.compacting, self.proc.held), ("cmp", set()))
        self.host._line("maps", {"method": "turn/completed", "params": {"turn": {"id": "cmp", "status": "completed"}}})
        self.assertEqual(len(self.proc.held), 1)   # one ask, held, at the compaction's end


class DirectionContext(unittest.TestCase):
    """`hub_hook.py --compacted` — the hook's context alone, for a host without hooks: no fact to the spool."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cxl-dir-")
        self.addCleanup(platforms.current.rmtree, self.tmp)
        self.state, self.dirs, repo = (os.path.join(self.tmp, x) for x in ("state", "directions", "repo"))
        os.makedirs(self.state)
        subprocess.run(["git", "init", "-q", repo], check=True)
        subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@example.test", "commit", "-q",
                        "--allow-empty", "-m", "init"], check=True)   # a direction's repo has a commit
        env = {**{k: v for k, v in os.environ.items() if not k.startswith("HUB_")}, "HUB_STATE_DIR": self.state,
               "HUB_DIRECTIONS": self.dirs}
        new = subprocess.run([sys.executable, os.path.join(REPO, "modules", "directions", "direction.py"), "new", "demo",
                              "--repo", repo], env=env, capture_output=True, text=True)
        self.assertEqual(new.returncode, 0, new.stderr)
        doc = os.path.join(self.dirs, "demo", "backlog", "docs", "doc-1 - Rules.md")
        os.makedirs(os.path.dirname(doc), exist_ok=True)
        with open(doc, "w", encoding="utf-8") as f:
            f.write("---\nid: doc-1\ntitle: Rules\n---\n\nRule 7: the orders table changes only by a migration\n")
        with open(os.path.join(self.state, "journal.jsonl"), "w", encoding="utf-8") as f:
            for seq, (type, entity, data) in enumerate((
                    ("session.requested", "lead", {"cwd": repo, "session_id": "s-lead", **OWNER}),
                    ("session.requested", "plain", {"cwd": repo, "session_id": "s-plain", **OWNER}),
                    ("task.backlog", None, {"slug": "demo", "session": "lead", **OWNER})), 1):
                f.write(json.dumps({"seq": seq, "ts": 1000 + seq, "type": type, "entity": entity, "data": data}) + "\n")
        self.host = types.SimpleNamespace(state_dir=self.state, env={"HUB_DIRECTIONS": self.dirs},
                                          lock=threading.Lock(), inbox=__import__("queue").Queue(), clock=time.time)

    def context(self, name):
        codex_life._context(self.host, name, "tok")
        (_, line, _), = [self.host.inbox.get_nowait()]
        self.assertEqual((line["method"], line["params"]["token"]), (codex_life.LIFE, "tok"))
        return line["params"]["context"]

    def test_a_direction_gets_its_backlog_and_nothing_goes_to_the_spool(self):
        text = self.context("lead")
        self.assertIn("hub direction resume demo", text)
        self.assertIn("Rule 7", text)
        self.assertEqual(self.context("plain"), "")
        self.assertFalse(os.path.isdir(os.path.join(self.state, "spool")) and os.listdir(os.path.join(self.state, "spool")))


UP = "import sys, time; print('up', flush=True); time.sleep(60)"


@unittest.skipIf(os.name == "nt", "Windows: the session's job stops the tree together with hubd (reap_tagged)")
class Reap(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubcxr-")
        self.addCleanup(platforms.current.rmtree, self.state)
        self.host = CodexHost(self.state, codex=(sys.executable, FAKE_CODEX))
        self.kids = []

    def tearDown(self):
        for p in self.kids:
            if p.poll() is None:
                p.kill()
            p.wait()

    def orphan(self, tag, *argv):
        env = {k: v for k, v in os.environ.items() if k != platforms.TREE_ENV}
        if tag:
            env[platforms.TREE_ENV] = tag
        p = subprocess.Popen([sys.executable, "-c", UP, *argv], env=env, stdout=subprocess.PIPE, text=True)
        self.kids.append(p)
        self.assertEqual(p.stdout.readline().strip(), "up")   # running with its environment and command line
        threading.Thread(target=p.wait, daemon=True).start()   # collected at once: no zombie looks alive
        return p

    def test_an_app_server_of_this_hub_from_a_previous_life_is_stopped(self):
        p = self.orphan(self.host.tree_tag("maps"), "app-server", "--enable", "x")
        self.assertTrue(self.host.reap(p.pid, "sid-maps"))
        self.assertTrue(wait(lambda: p.poll() is not None, 10))

    def test_foreign_processes_are_left_alone(self):
        other = self.orphan(f"maps@{self.state}-other", "app-server")          # another hub's folder
        plain = self.orphan(self.host.tree_tag("maps"), "--serve")              # a session's own command, not app-server
        none = self.orphan(None, "app-server")                                   # no marker: not a session's process
        for p in (other, plain, none):
            self.assertFalse(self.host.reap(p.pid, "sid"), p.args)
            self.assertIsNone(p.poll())

    def test_a_process_of_this_life_and_no_pid_are_not_reaped(self):
        p = self.orphan(self.host.tree_tag("maps"), "app-server")
        self.host.procs["maps"] = types.SimpleNamespace(popen=p)
        self.assertFalse(self.host.reap(p.pid, "sid"))
        self.assertFalse(self.host.reap(None, "sid"))
        self.assertIsNone(p.poll())

    def test_the_session_of_this_id_is_matched_exactly(self):
        self.host._remember("maps", "sid-maps", "th-1")   # the thread file names the session of this core id
        other = self.orphan(self.host.tree_tag("docs"), "app-server")   # another session's app-server at that number
        self.assertFalse(self.host.reap(other.pid, "sid-maps"))
        self.assertIsNone(other.poll())
        own = self.orphan(self.host.tree_tag("maps"), "app-server")
        self.assertTrue(self.host.reap(own.pid, "sid-maps"))
        self.assertTrue(wait(lambda: own.poll() is not None, 10))

    def test_a_mixed_hub_asks_both_hosts(self):
        mux = MuxHost(StreamHost(self.state, claude=(sys.executable, FAKE_CLAUDE)), self.host)
        p = self.orphan(self.host.tree_tag("maps"), "app-server")
        self.assertTrue(mux.reap(p.pid, "sid"))
        self.assertTrue(wait(lambda: p.poll() is not None, 10))


class Returning(unittest.TestCase):
    """A system agent removed or failed comes back with its conversation when its next process can continue it."""
    DATA = {"cwd": "/w", "about": "dev", "icon": "x"}

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubcxs-")
        self.addCleanup(platforms.current.rmtree, self.state)
        self.host = CodexHost(self.state, codex=(sys.executable, FAKE_CODEX), all_sessions=True)
        self.st = {"sessions": {"hub-dev": {"name": "hub-dev", "state": "failed", "has_history": True, "cwd": "/w",
                                            "session_id": "old", "role": "agent", "profile": "p1"}}}

    def thread(self, rollout=True):
        self.host._remember("hub-dev", "old", "th-1")
        if rollout:
            day = os.path.join(self.host.home, "sessions", "2026", "11", "05")
            os.makedirs(day, exist_ok=True)
            open(os.path.join(day, "rollout-2026-11-05T09-00-00-th-1.jsonl"), "w").close()

    def back(self, host=None, transcript=lambda *_: False):
        host = host or self.host
        return system.returning(self.st, "hub-dev", self.DATA, "new", transcript=transcript, resumable=host.resumable)

    def test_its_codex_thread_on_disk_brings_the_same_conversation(self):
        self.thread()
        (ev,) = self.back()
        self.assertEqual((ev[0], ev[2]["session_id"], ev[2].get("revive")), ("session.requested", "old", True))

    def test_no_thread_or_no_rollout_is_a_new_conversation_with_the_profile(self):
        (ev,) = self.back()
        self.assertEqual((ev[2]["session_id"], ev[2]["profile"]), ("new", "p1"))
        self.thread(rollout=False)
        (ev,) = self.back()
        self.assertEqual(ev[2]["session_id"], "new")

    def test_a_claude_transcript_does_not_count_for_codex(self):
        (ev,) = self.back(transcript=lambda *_: True)
        self.assertEqual(ev[2]["session_id"], "new")

    def test_a_mixed_hub_routes_by_the_model(self):
        mixed = CodexHost(self.state, codex=(sys.executable, FAKE_CODEX))
        mux = MuxHost(StreamHost(self.state, claude=(sys.executable, FAKE_CLAUDE)), mixed)
        self.thread()
        # a claude session: the Codex thread does not matter, Claude Code's transcript does
        self.assertIsNone(mux.resumable("hub-dev", self.st["sessions"]["hub-dev"]))
        (ev,) = self.back(host=mux, transcript=lambda *_: True)
        self.assertEqual(ev[2]["session_id"], "old")
        self.st["sessions"]["hub-dev"]["model"] = MODEL
        self.assertTrue(mux.resumable("hub-dev", self.st["sessions"]["hub-dev"]))
        self.assertEqual(self.back(host=mux, transcript=lambda *_: False)[0][2]["session_id"], "old")
        # the liaison goes to claude even on a codex/… model (MuxHost.spawn): its transcript decides
        self.assertIsNone(mux.resumable("liaison", {"model": MODEL, "liaison": True, "session_id": "old"}))

    def test_a_thread_id_is_not_a_path(self):
        self.host._remember("hub-dev", "old", "../../x")
        self.assertFalse(codex_life.has_thread(self.host, "hub-dev", "old"))

    def test_the_daemon_hands_the_host_over(self):
        seen = []

        def returning(st, name, data, new_id, transcript=None, resumable=None):
            seen.append(resumable)
            return []
        for host, want in ((self.host, self.host.resumable),
                           (StreamHost(self.state, claude=(sys.executable, FAKE_CLAUDE)), None)):
            hub = Hub.__new__(Hub)
            hub.host, hub.state, hub.config, hub.state_dir, hub.settings_known = host, {"sessions": {}}, {}, self.state, True
            hub.record, hub.ensure_jobs = lambda *a, **k: None, lambda: None
            with mock.patch.object(system, "off", return_value=False), \
                    mock.patch.object(system, "specs", return_value=[("hub-dev", self.DATA), ("agent-dev", self.DATA)]), \
                    mock.patch.object(system, "returning", returning):
                hub.ensure_dev()
            self.assertEqual(seen[-1], want)


class WatchLines(unittest.TestCase):
    """The watcher's "what it works on" line on a hub on Codex: from the plan by default; the core's model only with
    watch.json "codex_recap": true."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hubcxw-")
        self.addCleanup(platforms.current.rmtree, self.tmp)
        with open(os.path.join(self.tmp, "hub.json"), "w") as f:
            json.dump({"host": "codex"}, f)
        env = mock.patch.dict(os.environ, {"HUB_CONFIG": os.path.join(self.tmp, "hub.json"), "HUB_STATE_DIR": self.tmp})
        env.start()
        self.addCleanup(env.stop)

    def lines(self, cfg):
        from modules.watch import watch
        called = []
        w = watch.Watcher(self.tmp, cfg=cfg, recap=lambda text: called.append(text) or "Fixing the feed")
        w.state["sessions"]["maps"] = {"name": "maps", "state": "busy", "turn_seq": 5, "since": 1.0}
        w.steps["maps"] = [{"seq": 5, "type": "turn.started", "entity": "maps", "ts": 1.0, "data": {}},
                           {"seq": 6, "type": "turn.tool", "entity": "maps", "ts": 2.0,
                            "data": {"name": "Bash", "input": {"command": "ls"}}}]
        w.counted["maps"] = 50
        with mock.patch.object(spool, "put") as put:
            w.recaps(1000.0)
        return called, [c.args[1:4] for c in put.call_args_list]

    def test_off_by_default_on_with_codex_recap(self):
        self.assertEqual(self.lines({}), ([], []))
        self.assertEqual(self.lines({"codex_recap": "yes"})[0], [])   # only a real true turns it on
        called, put = self.lines({"codex_recap": True})
        self.assertEqual(len(called), 1)
        self.assertEqual(put[0][:2], ("session.recap", "maps"))
        self.assertEqual((put[0][2]["text"], put[0][2]["by"]), ("Fixing the feed", "model"))


class OnTheHub(unittest.TestCase):
    """The whole path on a hub with the fake app-server: recap -> the fold's "away"; compaction -> the context goes in
    before the next message's turn/start."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubcxh-")
        self.addCleanup(platforms.current.rmtree, self.state)
        self.calls = os.path.join(self.state, "calls.jsonl")
        replies = os.path.join(self.state, "replies.json")
        with open(replies, "w") as f:
            json.dump({"thread/turns/list": TURNS}, f)
        env = {"FAKE_TURN_S": "0.1", "FAKE_CODEX_CALLS": self.calls, "FAKE_CODEX_REPLIES": replies}
        stream = StreamHost(self.state, claude=(sys.executable, FAKE_CLAUDE), env=env)
        codex = CodexHost(self.state, codex=(sys.executable, FAKE_CODEX), env=env)
        self.hub = Hub(self.state, MuxHost(stream, codex), registry_dir=os.path.join(self.state, "registry"))
        self.hub.start()
        self.addCleanup(lambda: (self.hub.host.kill_server(), time.sleep(0.2)))
        script = os.path.join(self.state, "ctx.py")
        with open(script, "w") as f:
            f.write("import os, sys, time\ntime.sleep(0.3)\nsys.stdout.write('CTX ' + os.environ['HUB_SESSION'])\n")
        patcher = mock.patch.object(codex_life, "HOOK", script)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cli("session.requested", "a", cwd=self.state, session_id="sid-a")
        self.until(lambda: self.sess("a").get("state") == "idle")
        self.cli("session.model_set", "a", model=MODEL, channel="web", owner=True)
        self.until(lambda: self.hub.host.host_of("a") == "codex" and self.sess("a").get("state") == "idle"
                   and self.sess("a").get("model") == MODEL)

    def cli(self, type, entity=None, **data):
        spool.put(os.path.join(self.state, "spool"), type, entity, data)

    def until(self, pred, timeout=15.0):
        end = time.time() + timeout
        while time.time() < end:
            self.hub.tick()
            if pred():
                return
            time.sleep(0.03)
        self.fail(f"did not wait; {self.hub.state['sessions']}")

    def sess(self, name):
        return self.hub.state["sessions"].get(name, {})

    def sent(self, method):
        with open(self.calls, encoding="utf-8") as f:
            return [c["params"] for c in map(json.loads, f) if c["method"] == method]

    def test_recap_becomes_the_away_line(self):
        with mock.patch.object(codexmodel, "ask", return_value={"recap": "Fixing the feed; tests next."}):
            self.assertEqual(self.hub.host.recap("a"), "sent")
            self.until(lambda: (self.sess("a").get("away") or {}).get("by") == "codex")
        self.assertEqual(self.sess("a")["away"]["text"], "Fixing the feed; tests next.")

    def test_compaction_then_a_message_carries_the_context(self):
        self.cli("session.compact_requested", "a", **{"channel": "web", "owner": True})
        self.until(lambda: any(e["type"] == "session.compacted" for e in journal.read(self.hub.journal.path)))
        self.cli("message.received", to="a", text="next step", channel="web", owner=True)
        self.until(lambda: any("next step" in json.dumps(p) for p in self.sent("turn/start")))
        start = [p for p in self.sent("turn/start") if "next step" in json.dumps(p)][0]
        (inject,) = self.sent("thread/inject_items")
        self.assertEqual(inject["items"][0]["content"][0]["text"], "CTX a")
        with open(self.calls, encoding="utf-8") as f:
            order = [c["method"] for c in map(json.loads, f)]
        self.assertLess(order.index("thread/inject_items"), len(order) - 1 - order[::-1].index("turn/start"))


if __name__ == "__main__":
    unittest.main()
