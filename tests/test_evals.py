"""Evals: a conversation slice, fact checks, a whole run on a fake claude (no model and no VM)."""
import tests  # noqa: F401 — ignore this machine's settings (~/.config/hub-core), see tests/__init__.py
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import platforms
from core import journal
from tests.fakes import lang

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "evals"))
import run as evals  # noqa: E402
from common import case_files, find_case  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# invented names the cases must not carry, in Russian letters: the stem of "Sasha" and "Nikolai"
SASHA = "".join(map(chr, (0x0421, 0x0430, 0x0448)))
NIKOLAI = "".join(map(chr, (0x041d, 0x0438, 0x043a, 0x043e, 0x043b, 0x0430, 0x0439)))
FAKE =os.path.join(REPO, "tests", "fakes", "fake_claude_stream.py")


class Pieces(unittest.TestCase):
    def test_cut_transcript_stops_at_the_user_line_with_marker(self):
        d = tempfile.mkdtemp()
        try:
            src = os.path.join(d, "s.jsonl")
            lines = [{"type": "user", "message": lang.head("m1", kind="owner") + " hello"},
                     {"type": "assistant", "message": "ok"},
                     {"type": "assistant", "message": "mentioning [hub:m2 in the text"},
                     {"type": "user", "message": lang.head("m2", kind="owner") + " yes"},
                     {"type": "assistant", "message": "later"}]
            open(src, "w").write("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines))
            self.assertEqual(evals.cut_transcript(src, "[hub:m2", os.path.join(d, "out", "s.jsonl")), 3)
            self.assertEqual(evals.slug("/data/hub/hub-home"), "-data-hub-hub-home")
        finally:
            shutil.rmtree(d)

    def test_checks_match_facts_after_the_cut(self):
        events = [{"seq": 5, "type": "tools.granted", "entity": "w", "data": {"tool": "browser:vm"}},
                  {"seq": 9, "type": "message.received", "data": {"text": "yes", "eval_inject": True}},
                  {"seq": 10, "type": "tools.granted", "entity": "w", "data": {"tool": "browser:mac", "for": "m9"}},
                  {"seq": 11, "type": "session.said", "entity": "hub", "data": {"text": "Connected browser:mac on your «yes»"}}]
        ctx = {"$inject": "m9", "$inject_seq": 9, "$target": "hub"}
        c = lambda **k: evals.check(k, events, ctx)[0]  # noqa: E731
        self.assertTrue(c(type="event", match={"type": "tools.granted", "data.tool": "browser:mac", "data.for": "$inject"}))
        self.assertTrue(c(type="no_event", match={"type": "tools.granted", "data.tool": ["browser:vm", "login@x"]}))  # before the slice — does not count
        self.assertFalse(c(type="no_event", match={"type": "tools.granted", "data.for": "$present"}))
        self.assertTrue(c(type="said", re="on your «yes»"))
        self.assertFalse(c(type="not_said", re="connected"))


    def test_a_made_up_history_in_the_case_is_fresh_at_run_time(self):
        # a case without a real journal: the events are right in it, the time is set before the run (the owner question is not stale)
        evs = evals.synthetic([{"type": "session.requested", "entity": "a", "data": {"cwd": "/w"}},
                               {"type": "turn.started", "entity": "a"}], now=10_000)
        self.assertEqual([(e["seq"], e["ts"], e["type"], e["data"]) for e in evs],
                         [(1, 9980, "session.requested", {"cwd": "/w"}), (2, 9990, "turn.started", {})])
        # an event with "ago" is placed that long before the run: a history where a deadline has already passed
        evs = evals.synthetic([{"type": "turn.started", "entity": "a", "ago": 7200},
                               {"type": "turn.result", "entity": "a"}], now=10_000)
        self.assertEqual([e["ts"] for e in evs], [2800, 9990])
        # every case with an invented history builds into a state without core refusals (the Russian owner's cases too, in
        # a copy that has them); only *.json — next to it in the hub folder there may be a .DS_Store
        for path in case_files("cases"):
            name = os.path.basename(path)
            case = json.load(open(path, encoding="utf-8"))
            if "events" in case:
                from core import reducer
                st = reducer.empty()
                for e in evals.synthetic(case["events"], time.time()):
                    st, _, why = reducer.reduce(st, e)
                    self.assertIsNone(why, f"{name}: {e['type']} — {why}")


class WholeRunOnFakeClaude(unittest.TestCase):
    def test_prepare_drive_judge(self):
        work = tempfile.mkdtemp(prefix="hubeval-")
        try:
            src = os.path.join(work, "source.jsonl")
            j = journal.Journal(src)
            j.append("session.requested", "hub", {"cwd": "/w", "session_id": "sid-hub", "kind": "hub"}, ts=1)
            j.append("session.started", "hub", {"session_id": "sid-hub"}, ts=2)
            j.append("session.requested", "a", {"cwd": work, "session_id": "sid-a"}, ts=3)
            j.append("session.started", "a", {"session_id": "sid-a"}, ts=4)
            j.append("message.received", None, {"to": "a", "text": "original", "channel": "web", "owner": True}, ts=5)
            home = os.path.join(work, "home")
            os.makedirs(os.path.join(home, ".config", "hub-core"))
            json.dump({"hub": {"enabled": True, "name": "hub"}}, open(os.path.join(home, ".config", "hub-core", "hub.json"), "w"))
            case = {"id": "smoke", "journal": src, "cut_seq": 5, "real": ["a"], "timeout": 60, "settle": 1,
                    "inject": [{"type": "message.received", "data": {"to": "a", "text": "hello", "channel": "web", "owner": True}}],
                    "checks": [{"id": "answered", "type": "said", "re": "ok: hello"},
                               {"id": "no_ask", "type": "no_event", "match": {"type": "permission.requested"}}]}
            run = os.path.join(work, "run")
            resolved = evals.prepare(case, REPO, run, user_home=home)
            self.assertEqual(len(journal.read(os.path.join(run, "state", "journal.jsonl"))), 4)
            env = {**os.environ, "EVAL_CLAUDE": f"{sys.executable} {FAKE}", "FAKE_TURN_S": "0.1"}
            out = subprocess.run([sys.executable, os.path.join(REPO, "evals", "driver.py"), REPO, run, resolved], env=env,
                                 capture_output=True, text=True, timeout=120)
            self.assertEqual(out.returncode, 0, out.stderr[-2000:])
            ok, results, _ = evals.judge(case, run)
            self.assertTrue(ok, results)
            self.assertFalse(os.path.exists(os.path.join(run, "claude-config", "projects")))  # conversation copies are not kept
        finally:
            shutil.rmtree(work, ignore_errors=True)


    def test_a_case_with_its_own_history_runs_without_a_live_hub(self):
        # a case carries its own history and settings: it runs on any machine, the live journal and hub.json are not read
        work = tempfile.mkdtemp(prefix="hubeval-")
        self.addCleanup(shutil.rmtree, work, True)
        home = os.path.join(work, "home")
        os.makedirs(os.path.join(home, ".config", "hub-core"))
        with open(os.path.join(home, ".config", "hub-core", "hub.json"), "w") as f:
            json.dump({"secret": "from the machine settings"}, f)
        case = {"id": "own", "real": ["w"], "timeout": 60, "settle": 1,
                "events": [{"type": "session.requested", "entity": "w", "data": {"cwd": "$work/w", "session_id": "sid-w",
                                                                                  "channel": "cli", "owner": True}}],
                "config": {"hub": {"enabled": False}, "timezone": "UTC"},
                "inject": [{"type": "message.received", "data": {"to": "w", "text": "hello", "channel": "web",
                                                                  "owner": True}}],
                "checks": [{"id": "answered", "type": "said", "re": "hello"}]}
        run = os.path.join(work, "run")
        resolved = evals.prepare(case, REPO, run, user_home=home)
        evs = journal.read(os.path.join(run, "state", "journal.jsonl"))
        self.assertEqual([e["type"] for e in evs], ["session.requested"])
        self.assertEqual(os.path.normpath(evs[0]["data"]["cwd"]), os.path.join(run, "work", "w"))
        self.assertTrue(os.path.isdir(os.path.join(run, "work", "w")))
        with open(os.path.join(run, "hub.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertEqual(cfg, {"hub": {"enabled": False, "cwd": os.path.join(run, "hub-home")}, "timezone": "UTC"})
        for name in os.listdir(os.path.join(REPO, "hub-home")):   # the hub folder — as in this code version
            if os.path.isfile(os.path.join(REPO, "hub-home", name)):
                self.assertTrue(os.path.exists(os.path.join(run, "hub-home", name)), name)
        env = {**os.environ, "EVAL_CLAUDE": f"{sys.executable} {FAKE}", "FAKE_TURN_S": "0.1"}
        out = subprocess.run([sys.executable, os.path.join(REPO, "evals", "driver.py"), REPO, run, resolved], env=env,
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        ok, results, _ = evals.judge(case, run)
        self.assertTrue(ok, results)


class ChatCases(unittest.TestCase):
    """The checks of the voice cases: a fake chat MCP with a call log, seeded files, the style file at the end of a run."""

    def setUp(self):
        self.run = tempfile.mkdtemp(prefix="hubeval-")
        import platforms
        self.addCleanup(platforms.current.rmtree, self.run)
        os.makedirs(os.path.join(self.run, "work"))
        os.makedirs(os.path.join(self.run, "state"))

    @staticmethod
    def read(*path):
        with open(os.path.join(*path), encoding="utf-8") as f:
            return f.read()

    def log(self, *calls, up=True):
        calls = ((0.0, "_init"),) * up + calls   # the fake logs its start first
        with open(os.path.join(self.run, "work", "chat.log"), "w") as f:
            f.write("".join(json.dumps({"t": t, "tool": tool, "args": {}}) + "\n" for t, tool in calls))

    def c(self, events, **check):
        ctx = {"$inject": "m1", "$inject_seq": 1, "$target": "w", "$run": self.run}
        return evals.check(check, events, ctx)[0]

    def test_the_chat_must_be_read_before_the_text_is_shown_or_sent(self):
        card = {"seq": 3, "ts": 110.0, "type": "permission.requested", "entity": "w", "data": {}}
        read = dict(type="mcp_read_first", tool="list-chat-messages")
        self.assertFalse(self.c([card], **read))   # no log: not read
        self.log((100.0, "list-chat-messages"))
        self.assertTrue(self.c([card], **read))
        self.log((120.0, "list-chat-messages"))
        self.assertFalse(self.c([card], **read))   # read after the card
        self.log((100.0, "list-chat-messages"), (105.0, "send-chat-message"))
        self.assertTrue(self.c([], **read))        # a send after the read, no card: fine for the order
        self.log((100.0, "send-chat-message"), (105.0, "list-chat-messages"))
        self.assertFalse(self.c([], **read))       # sent first
        said = {"seq": 3, "ts": 90.0, "type": "session.said", "entity": "w", "data": {"text": "draft"}}
        self.log((100.0, "list-chat-messages"))
        self.assertFalse(self.c([said], **read))   # the plain-text draft came first
        # without a card the FIRST plain-text draft counts, not the last: draft, then reading, then "waiting for yes" is red
        said_late = {"seq": 5, "ts": 130.0, "type": "session.said", "entity": "w", "data": {"text": "waiting for a yes"}}
        self.assertFalse(self.c([said, said_late], **read))
        self.assertTrue(self.c([{**said, "ts": 110.0}, said_late], **read))   # read first, then the draft and the question

    def test_tool_calls_are_counted_from_the_log(self):
        self.log((1.0, "send-chat-message"), (2.0, "send-chat-message"))
        self.assertTrue(self.c([], type="mcp_calls", tool="send-chat-message", min=2))
        self.assertFalse(self.c([], type="mcp_calls", tool="send-chat-message", max=1))
        self.assertTrue(self.c([], type="mcp_calls", tool="list-chat-messages", max=0))

    def test_no_calls_prove_nothing_when_the_fake_never_started(self):
        none = dict(type="mcp_calls", tool="list-chat-messages", max=0)
        self.assertFalse(self.c([], **none))       # no log at all: the server did not start
        self.log(up=False)
        self.assertFalse(self.c([], **none))       # an empty log: the same
        self.log()
        self.assertTrue(self.c([], **none))        # started, never called

    def test_the_style_file_is_checked_by_lines_and_text(self):
        f = lambda **k: self.c([], type="file", path="owner-voice.md", **k)  # noqa: E731
        self.assertFalse(f(lines_min=1))
        self.assertTrue(self.c([], type="no_file", path="owner-voice.md"))
        with open(os.path.join(self.run, "state", "owner-voice.md"), "w") as out:
            out.write("- no long dashes\n\n- address as «you»\n")
        self.assertTrue(f(lines_min=2, lines_max=2, re="dashes"))
        self.assertFalse(f(lines_max=1))
        self.assertFalse(f(not_re="dashes"))
        self.assertFalse(self.c([], type="no_file", path="owner-voice.md"))

    def test_an_observation_does_not_fail_the_run_but_keeps_its_note(self):
        events = [{"seq": 1, "type": "message.received", "entity": None, "data": {"to": "w", "eval_inject": True}},
                  {"seq": 2, "type": "session.said", "entity": "w", "data": {"text": "ok"}}]
        with open(os.path.join(self.run, "events.jsonl"), "w") as out:
            out.write("".join(json.dumps(e) + "\n" for e in events))
        with open(os.path.join(self.run, "driver.json"), "w") as out:
            json.dump({"finished": True}, out)
        self.log((1.0, "send-chat-message"))
        case = {"real": ["w"], "checks": [{"id": "sent", "type": "mcp_calls", "tool": "send-chat-message", "max": 0,
                                           "observe": True}]}
        ok, results, _ = evals.judge(case, self.run)
        self.assertTrue(ok)
        # a measurement is its own status, not "ok (not met)": nothing in the line reads as a pass
        self.assertEqual(results[0][1], evals.OBS)
        self.assertNotEqual(results[0][1], True)
        self.assertIn("calls of send-chat-message: 1", results[0][2])
        self.assertEqual(evals.mark(evals.OBS), "obs")
        self.assertEqual((evals.mark(True), evals.mark(False)), ("ok", "FAIL"))

    def test_files_of_a_case_land_in_the_run_and_the_fake_chat_logs_its_calls(self):
        case = own_case(files={"$work/history.json": [{"from": "x", "text": "hi"}], "owner-voice.md": "- a\n"},
                        config_files={"contacts.json": [{"name": "Z"}]})
        evals.prepare(case, REPO, os.path.join(self.run, "r"), user_home=self.run)
        r = os.path.join(self.run, "r")
        self.assertEqual(json.loads(self.read(r, "work", "history.json"))[0]["text"], "hi")
        self.assertEqual(self.read(r, "state", "owner-voice.md"), "- a\n")
        self.assertEqual(json.loads(self.read(r, "config", "contacts.json"))[0]["name"], "Z")
        env = {**os.environ, "CHAT_HISTORY": os.path.join(r, "work", "history.json"), "CHAT_LOG": os.path.join(r, "work", "chat.log")}
        calls = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                 {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list-chat-messages", "arguments": {"chatId": "c"}}},
                 {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "send-chat-message", "arguments": {"chatId": "c", "text": "t"}}}]
        out = subprocess.run([sys.executable, os.path.join(REPO, "evals", "fakes", "chat_mcp.py")], env=env, text=True,
                             input="".join(json.dumps(x) + "\n" for x in calls), capture_output=True, timeout=30)
        got = [json.loads(x) for x in out.stdout.splitlines()]
        self.assertEqual([t["name"] for t in got[1]["result"]["tools"]], ["list-chat-messages", "send-chat-message"])
        self.assertIn("hi", got[2]["result"]["content"][0]["text"])
        logged = [json.loads(x) for x in self.read(r, "work", "chat.log").splitlines()]
        self.assertEqual([(x["tool"], x["args"].get("text")) for x in logged],
                         [("_init", None), ("list-chat-messages", None), ("send-chat-message", "t")])
        # the console of a Windows machine is not UTF-8 — the fake answers in UTF-8 anyway
        word = lang.data("self_name", "name")   # a Russian word of the language data
        with open(os.path.join(r, "work", "history.json"), "w", encoding="utf-8") as out:
            json.dump([{"from": "x", "text": word}], out, ensure_ascii=False)
        cyr = subprocess.run([sys.executable, "-X", "utf8=0", os.path.join(REPO, "evals", "fakes", "chat_mcp.py")],
                             env={**env, "PYTHONIOENCODING": "cp1251", "PYTHONUTF8": "0"}, capture_output=True, timeout=30,
                             input=(json.dumps(calls[2]) + "\n").encode())
        self.assertIn(word, json.loads(cyr.stdout.decode("utf-8").splitlines()[0])["result"]["content"][0]["text"])
        # Claude Code drops a tool whose schema has a property name outside [A-Za-z0-9_.-]: no such name in the fake
        for t in got[1]["result"]["tools"]:
            for prop in t["inputSchema"]["properties"]:
                self.assertRegex(prop, r"^[A-Za-z0-9_.-]{1,64}$")


class VoiceCases(unittest.TestCase):
    """The voice cases as files: invented neutral names, a lower-case task key, the skill check where it is required."""

    @staticmethod
    def read_case(case_id):
        with open(find_case(case_id), encoding="utf-8") as f:
            return f.read()

    def test_names_in_the_cases_are_invented_and_the_task_key_is_lower_case(self):
        ids = [os.path.basename(p)[:-5] for p in case_files("cases") if os.path.basename(p).startswith("voice-")]
        self.assertGreaterEqual(len(ids), 6 if find_case("voice-jira-comment") else 3)   # the open tree has the English ones
        for case_id in ids:
            for word in (SASHA, "Sasha", "sasha", NIKOLAI, "MAPS"):
                self.assertNotIn(word, self.read_case(case_id), f"{case_id}: {word}")
        if find_case("voice-jira-comment"):   # a Russian owner's case: only in a copy that has evals/locale/ru/
            self.assertIn("maps-12", self.read_case("voice-jira-comment"))

    def test_a_jira_comment_may_open_the_skill_but_must_not_touch_the_chat_or_the_style_file(self):
        if not find_case("voice-jira-comment"):
            self.skipTest("a Russian owner's case: evals/locale/ru/ is not in this copy")
        case = json.loads(self.read_case("voice-jira-comment"))
        checks = {c["id"]: c for c in case["checks"]}
        # the description of the skill names «a comment»: opening it is allowed, so no check on it
        self.assertFalse([c for c in case["checks"] if "Skill" in json.dumps(c)])
        for cid, tool in (("no_chat_read", "list-chat-messages"), ("no_chat_send", "send-chat-message")):
            self.assertEqual((checks[cid]["type"], checks[cid]["tool"], checks[cid]["max"]), ("mcp_calls", tool, 0))
            self.assertFalse(checks[cid].get("observe"), cid)
        style = checks["style_untouched"]
        self.assertEqual((style["type"], style["path"]), ("file", "owner-voice.md"))
        self.assertIn("owner-voice.md", case["files"])
        # the check holds for the seeded file as it is and fails for a replaced line
        seeded = case["files"]["owner-voice.md"]
        self.assertEqual(len([x for x in seeded.splitlines() if x.strip()]), style["lines_min"])
        self.assertTrue(re.search(style["re"], seeded, re.I | re.S))
        self.assertFalse(re.search(style["re"], "- No em dashes.\n- Write briefly.\n", re.I | re.S))

    def test_a_correction_to_one_message_still_has_to_open_the_skill(self):
        case = json.loads(self.read_case("voice-correct-once"))
        opened = [c for c in case["checks"] if c["id"] == "skill_opened"]
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0]["type"], "event")


class NotLoggedIn(unittest.TestCase):
    def test_a_session_without_a_login_is_not_a_pass(self):
        # claude without a login answers every turn with a /login hint: "did not do" checks would pass on silence
        run = tempfile.mkdtemp(prefix="hubeval-")
        self.addCleanup(shutil.rmtree, run, True)
        events = [{"seq": 3, "type": "message.received", "entity": None, "data": {"to": "w", "eval_inject": True}},
                  {"seq": 4, "type": "session.said", "entity": "w", "data": {"text": "Not logged in · Please run /login"}}]
        with open(os.path.join(run, "events.jsonl"), "w") as f:
            f.write("".join(json.dumps(e) + "\n" for e in events))
        with open(os.path.join(run, "driver.json"), "w") as f:
            json.dump({"finished": True}, f)
        case = {"real": ["w"], "checks": [{"id": "quiet", "type": "no_event", "match": {"type": "tools.granted"}}]}
        ok, results, _ = evals.judge(case, run)
        self.assertIsNone(ok)
        self.assertIn("not logged in", results[0][2])


    def test_a_login_hint_before_the_message_is_not_a_logged_out_run(self):
        # the session talked about a login (MCP) before the submitted message — the run is not n/a
        run = tempfile.mkdtemp(prefix="hubeval-")
        self.addCleanup(shutil.rmtree, run, True)
        events = [{"seq": 2, "type": "session.said", "entity": "w", "data": {"text": "the server says: Please run /login"}},
                  {"seq": 3, "type": "message.received", "entity": None, "data": {"to": "w", "eval_inject": True}},
                  {"seq": 4, "type": "session.said", "entity": "w", "data": {"text": "done"}}]
        with open(os.path.join(run, "events.jsonl"), "w") as f:
            f.write("".join(json.dumps(e) + "\n" for e in events))
        with open(os.path.join(run, "driver.json"), "w") as f:
            json.dump({"finished": True}, f)
        case = {"real": ["w"], "checks": [{"id": "said", "type": "said", "re": "done"}]}
        ok, results, _ = evals.judge(case, run)
        self.assertTrue(ok, results)

    def test_several_fed_messages_are_judged_by_the_last_ones_turn(self):
        # a /compact, then the message under test: the driver waits for the last one's turn — the judge anchors there
        run = tempfile.mkdtemp(prefix="hubeval-")
        self.addCleanup(shutil.rmtree, run, True)   # portable: two plain json files the test writes, nothing read-only
        events = [{"seq": 3, "type": "message.received", "entity": None, "data": {"to": "w", "eval_inject": True}},
                  {"seq": 4, "type": "session.said", "entity": "w", "data": {"text": "compacting"}},
                  {"seq": 5, "type": "message.received", "entity": None, "data": {"to": "w", "eval_inject": True}},
                  {"seq": 6, "type": "session.said", "entity": "w", "data": {"text": "done"}}]
        with open(os.path.join(run, "events.jsonl"), "w") as f:
            f.write("".join(json.dumps(e) + "\n" for e in events))
        with open(os.path.join(run, "driver.json"), "w") as f:
            json.dump({"finished": True}, f)
        case = {"real": ["w"], "checks": [{"id": "mark", "type": "said", "re": "done"},
                                          {"id": "after_the_last", "type": "not_said", "re": "compacting"}]}
        ok, results, _ = evals.judge(case, run)
        self.assertTrue(ok, results)

def own_case(**kw):
    return {"id": "h", "real": ["w"], **kw,
            "events": [{"type": "session.requested", "entity": "w", "data": {"cwd": "$work/w", "session_id": "s"}}]}


@unittest.skipIf(os.name == "nt", "file permissions are POSIX")
class PrivateCopies(unittest.TestCase):
    def test_login_and_settings_copies_are_private(self):
        # the run folder gets the claude login, ~/.claude.json and hub.json: copies are 0600, whatever the source permissions
        work = tempfile.mkdtemp(prefix="hubeval-")
        self.addCleanup(shutil.rmtree, work, True)
        was = os.umask(0o002)
        self.addCleanup(os.umask, was)
        home = os.path.join(work, "home")
        os.makedirs(os.path.join(home, ".claude"))
        os.makedirs(os.path.join(home, ".config", "hub-core"))
        for path in (os.path.join(home, ".config", "hub-core", "hub.json"),
                     os.path.join(home, ".claude", ".credentials.json"), os.path.join(home, ".claude.json")):
            with open(path, "w") as f:
                f.write("{}")
            os.chmod(path, 0o644)
        src = os.path.join(work, "source.jsonl")
        journal.Journal(src).append("session.requested", "hub", {"cwd": "/w", "session_id": "s", "kind": "hub"}, ts=1)
        run = os.path.join(work, "run")
        evals.prepare({"id": "p", "journal": src, "cut_seq": 2}, REPO, run, user_home=home)
        for rel in ("hub.json", os.path.join("claude-config", ".credentials.json"),
                    os.path.join("claude-config", ".claude.json")):
            self.assertEqual(os.stat(os.path.join(run, rel)).st_mode & 0o077, 0, rel)
        self.assertEqual(os.stat(run).st_mode & 0o077, 0)


class RealHome(unittest.TestCase):
    def test_a_run_takes_the_login_from_the_home_it_is_given_and_never_from_the_real_one(self):
        # the machine's home folder holds the real claude login: the test (or a run under another account) does not copy it
        work = tempfile.mkdtemp(prefix="hubeval-")
        self.addCleanup(shutil.rmtree, work, True)
        homes = {}
        for name in ("real", "given"):
            homes[name] = os.path.join(work, name)
            os.makedirs(os.path.join(homes[name], ".claude"))
            for rel in (os.path.join(".claude", ".credentials.json"), ".claude.json"):
                with open(os.path.join(homes[name], rel), "w") as f:
                    f.write(json.dumps({"from": f"{name}-secret"}))
        run = os.path.join(work, "run")
        with mock.patch.dict(os.environ, {"HOME": homes["real"], "USERPROFILE": homes["real"]}):
            evals.prepare(own_case(), REPO, run, user_home=homes["given"])
        for rel in (".credentials.json", ".claude.json"):
            with open(os.path.join(run, "claude-config", rel), encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"from": "given-secret"}, rel)
        for root, _, names in os.walk(run):
            for name in names:
                with open(os.path.join(root, name), "rb") as f:
                    self.assertNotIn(b"real-secret", f.read(), os.path.join(root, name))


class LiveFolders(unittest.TestCase):
    def test_the_core_and_sessions_of_a_run_get_only_the_runs_own_folders(self):
        # 2026-11-04: a run had its own hub.json, but the settings folder (core/catalog.config_dir: contacts, accounts,
        # bus keys) was the owner's live one — a session under test found a real person in the owner's contacts, and
        # `hub contacts add` would have written there. A run started from a hub session also carries that session's paths
        work = tempfile.mkdtemp(prefix="hubeval-")
        self.addCleanup(platforms.current.rmtree, work)
        appdata = os.path.join(work, "appdata")
        live = {"config": os.path.join(appdata, "hub-core"), "state": os.path.join(work, "live-state")}
        for d in live.values():
            os.makedirs(d)
        env = {k: v for k, v in os.environ.items() if k != "HUB_CONFIG_DIR"}   # the owner's hub: the OS settings folder
        env.update({"APPDATA": appdata, "XDG_CONFIG_HOME": appdata, "HUB_STATE_DIR": live["state"], "HUB_SESSION": "agent-dev",
                    "HUB_AGENT_FILE": os.path.join(live["state"], "agents", "a", "CLAUDE.md"),
                    "HUB_CONTACTS": os.path.join(live["config"], "contacts.json"),
                    "HUB_TELEGRAM_CONFIG": os.path.join(live["config"], "telegram.json"),
                    "HUB_WATCH_CONFIG": os.path.join(live["config"], "watch.json"),
                    "HUB_GATEWAY_STUB": "hub-gateway", "HUB_GATEWAY_UPSTREAM": "http://127.0.0.1:9"})   # the run's sign-in
        seen = os.path.join(work, "seen")   # the environment each session got
        os.makedirs(seen)
        claude = os.path.join(work, "claude_env.py")
        with open(claude, "w", encoding="utf-8") as f:
            f.write("import json, os, runpy, sys\n"
                    f"with open(os.path.join({seen!r}, os.environ['HUB_SESSION'] + '.json'), 'w', encoding='utf-8') as f:\n"
                    "    json.dump(dict(os.environ), f)\n"
                    f"sys.argv[0] = {FAKE!r}\n"
                    f"runpy.run_path({FAKE!r}, run_name='__main__')\n")
        case = {**own_case(), "timeout": 60, "settle": 1, "config": {"timezone": "UTC"},
                "inject": [{"type": "message.received", "data": {"to": "w", "text": "hello", "channel": "web", "owner": True}}],
                "checks": [{"id": "answered", "type": "said", "re": "hello"}]}
        run = os.path.join(work, "run")
        resolved = evals.prepare(case, REPO, run, user_home=os.path.join(work, "nohome"))
        out = subprocess.run([sys.executable, os.path.join(REPO, "evals", "driver.py"), REPO, run, resolved],
                             env={**env, "EVAL_CLAUDE": f"{sys.executable} {claude}", "FAKE_TURN_S": "0.1"},
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertIn("w.json", os.listdir(seen))
        for name in os.listdir(seen):
            with open(os.path.join(seen, name), encoding="utf-8") as f:
                got = json.load(f)
            self.assertTrue(os.path.isdir(got.get("HUB_CONFIG_DIR", "")), name)
            self.assertEqual(os.path.commonpath([got["HUB_CONFIG_DIR"], run]), run, name)
            # a run started from a session on a pool account: the nested claude takes its token stub back from these
            # (hosts/nested), without them every real claude of the run is "Not logged in"
            self.assertEqual((got.get("HUB_GATEWAY_STUB"), got.get("HUB_GATEWAY_UPSTREAM")), ("hub-gateway", "http://127.0.0.1:9"), name)
            self.assertNotIn("HUB_AGENT_FILE", got, name)
            for key, value in got.items():
                for where in live.values():
                    self.assertNotIn(where, value, f"{name}: {key}")
        for d in live.values():   # nothing written into the owner's folders by the run's core either
            self.assertEqual(os.listdir(d), [], d)


class MachineMemory(unittest.TestCase):
    def test_the_session_under_test_gets_none_of_the_machines_claude_md(self):
        # 2026-11-06: an eval's compaction summary quoted the person's own ~/.claude/CLAUDE.md. Claude Code reads
        # <dir>/.claude/CLAUDE.md (and CLAUDE.md, CLAUDE.local.md, .claude/rules) of every folder above the session's
        # one as project memory, so a run under the home folder got it whatever CLAUDE_CONFIG_DIR and --setting-sources
        work = tempfile.mkdtemp(prefix="hubeval-")
        self.addCleanup(platforms.current.rmtree, work)
        home = os.path.join(work, "home")
        personal = [os.path.join(home, ".claude", "CLAUDE.md"), os.path.join(home, "CLAUDE.md"),
                    os.path.join(home, ".claude", "rules", "mine.md"), os.path.join(home, ".cache", "CLAUDE.md")]
        for path in personal:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write("personal rules\n")
        seen = os.path.join(work, "seen")
        os.makedirs(seen)
        claude = os.path.join(work, "claude_argv.py")
        with open(claude, "w", encoding="utf-8") as f:
            f.write("import json, os, runpy, sys\n"
                    f"with open(os.path.join({seen!r}, os.environ['HUB_SESSION'] + '.json'), 'w', encoding='utf-8') as f:\n"
                    "    json.dump(sys.argv[1:], f)\n"
                    f"sys.argv[0] = {FAKE!r}\n"
                    f"runpy.run_path({FAKE!r}, run_name='__main__')\n")
        case = {**own_case(), "timeout": 60, "settle": 1, "config": {"timezone": "UTC"},
                "inject": [{"type": "message.received", "data": {"to": "w", "text": "hello", "channel": "web", "owner": True}}],
                "checks": [{"id": "answered", "type": "said", "re": "hello"}]}
        run = os.path.join(home, ".cache", "evals", "run")   # runs live under the home folder (common.WORK)
        resolved = evals.prepare(case, REPO, run, user_home=home)
        own = [os.path.join(run, "hub-home", "CLAUDE.md"), os.path.join(run, "work", "w", "CLAUDE.md")]
        with open(own[1], "w", encoding="utf-8") as f:
            f.write("the session's own folder\n")
        env = {**os.environ, "HOME": home, "USERPROFILE": home, "EVAL_CLAUDE": f"{sys.executable} {claude}",
               "FAKE_TURN_S": "0.1"}
        out = subprocess.run([sys.executable, os.path.join(REPO, "evals", "driver.py"), REPO, run, resolved],
                             env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        with open(os.path.join(seen, "w.json"), encoding="utf-8") as f:
            argv = json.load(f)
        with open(argv[argv.index("--settings") + 1], encoding="utf-8") as f:
            settings = json.load(f)
        with open(os.path.join(run, "state", "hooks-settings.json"), encoding="utf-8") as f:
            self.assertEqual(settings.get("hooks"), json.load(f).get("hooks"))   # the core's hooks stay

        def excluded(path):   # picomatch on "/" paths; no glob characters in a temporary folder's name
            return any(fnmatch.fnmatchcase(path.replace(os.sep, "/"), p) for p in settings.get("claudeMdExcludes") or [])
        for path in personal:
            self.assertTrue(excluded(path), path)
        for path in own:
            self.assertTrue(os.path.exists(path), path)
            self.assertFalse(excluded(path), path)

    def test_a_session_in_a_repo_keeps_the_repo_and_loses_what_is_above_it(self):
        # a case from the journal: the session starts in a repo of the machine; its CLAUDE.md, CLAUDE.local.md and the
        # repo's subfolders are the project's, the folders between the home folder and the repo are the machine's. A
        # folder name with glob characters: claude matches only an exact path there (a glob or an escape — nothing)
        from common import machine_memory
        work = tempfile.mkdtemp(prefix="hubeval-")
        self.addCleanup(platforms.current.rmtree, work)
        home = os.path.join(work, "home (x)+[y]")
        repo = os.path.join(home, "code", "repo")
        cwd = os.path.join(repo, "sub")
        os.makedirs(os.path.join(repo, ".git"))
        os.makedirs(os.path.join(home, ".claude", "rules", "more"))
        os.makedirs(cwd)
        machine = [os.path.join(home, ".claude", "rules", "more", "deep.md"), os.path.join(home, "code", "CLAUDE.md")]
        project = [os.path.join(repo, "CLAUDE.md"), os.path.join(repo, "CLAUDE.local.md"), os.path.join(cwd, "CLAUDE.md"),
                   os.path.join(repo, ".claude", "CLAUDE.md")]
        for path in machine + project:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w").close()
        run = os.path.join(work, "evals", "run")
        got = machine_memory(home, run, cwd)
        for path in machine + [os.path.join(home, ".claude", "CLAUDE.md"), os.path.join(work, "CLAUDE.md")]:
            self.assertIn(path.replace(os.sep, "/"), got)
        for path in project:
            self.assertNotIn(path.replace(os.sep, "/"), got)
        self.assertFalse([p for p in got if "*" in p or "\\" in p], got)   # exact paths, nothing a glob would read
        # a session whose folder is missing on this machine starts in the home folder, and a home folder may be a
        # dotfiles repo: the home folder is the machine's all the same; a folder outside any repo keeps only itself
        os.makedirs(os.path.join(home, ".git"))
        for where in (home, os.path.join(run, "work", "w")):
            self.assertIn(os.path.join(home, ".claude", "CLAUDE.md").replace(os.sep, "/"), machine_memory(home, run, where))
        lone = os.path.join(work, "else", "a", "b")
        os.makedirs(lone)
        got = machine_memory(home, run, lone)
        self.assertIn(os.path.join(work, "else", "a", "CLAUDE.md").replace(os.sep, "/"), got)
        self.assertNotIn(os.path.join(lone, "CLAUDE.md").replace(os.sep, "/"), got)


class LiveHubJson(unittest.TestCase):
    def test_a_case_from_the_journal_gets_none_of_the_owners_mcp_connectors_extensions_or_projects(self):
        # 2026-11-04: a case with cut_seq copied the owner's hub.json whole — a session under test given an MCP tool got
        # the owner's real one (a chat with a live sign-in); extensions and projects led into the owner's folders
        work = tempfile.mkdtemp(prefix="hubeval-")
        self.addCleanup(platforms.current.rmtree, work)
        home = os.path.join(work, "home")
        os.makedirs(os.path.join(home, ".config", "hub-core"))
        live = {"hub": {"name": "owners-hub"}, "timezone": "Asia/Tokyo", "connectors": True,
                "mcp": {"chat": {"server": {"type": "http", "url": "https://chat.example/mcp"}}},
                "projects": {"repo": {"path": os.path.join(work, "live-repo")}},
                "extensions": {"x:chat": {"path": os.path.join(work, "live-ext"), "run": ["x"]}}}
        with open(os.path.join(home, ".config", "hub-core", "hub.json"), "w", encoding="utf-8") as f:
            json.dump(live, f)
        src = os.path.join(work, "source.jsonl")
        journal.Journal(src).append("session.requested", "hub", {"cwd": "/w", "session_id": "s", "kind": "hub"}, ts=1)
        fake = {"chat": {"server": {"type": "stdio", "command": "fake"}}}
        for name, extra, mcp in (("bare", {}, None), ("own-mcp", {"config": {"mcp": fake}}, fake)):
            run = os.path.join(work, name)
            evals.prepare({"id": name, "journal": src, "cut_seq": 2, **extra}, REPO, run, user_home=home)
            with open(os.path.join(run, "hub.json"), encoding="utf-8") as f:
                got = json.load(f)
            self.assertEqual(got.get("mcp"), mcp, name)
            for key in ("connectors", "projects", "extensions"):
                self.assertNotIn(key, got, name)
            self.assertEqual((got["timezone"], got["hub"]["name"]), ("Asia/Tokyo", "owners-hub"), name)   # the rest stays


class WorkFolderInCase(unittest.TestCase):
    def test_a_work_folder_with_a_backslash_goes_into_the_case_whole(self):
        # a Windows path — C:\\Users\\…; unescaped in the case's JSON text it breaks the document
        work = tempfile.mkdtemp(prefix="hubeval-")
        self.addCleanup(shutil.rmtree, work, True)
        run = os.path.join(work, "a\\b")
        path = evals.prepare(own_case(), REPO, run, user_home=os.path.join(work, "nohome"))
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["events"][0]["data"]["cwd"], os.path.join(run, "work") + "/w")


if __name__ == "__main__":
    unittest.main()
