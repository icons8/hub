"""Keys in tool input and output stay out of the journal (oss-sec spec §2.2): known forms in turn.tool,
turn.tool_result, session.background and session.background_output are masked in journal.event() — one point for all
writers (Journal.append, Hub.record, the inbox), before folding: hubd's state and the reader's fold of the journal agree.
The host masks before clipping (otherwise clipping leaves a key tail shorter than the pattern). A permission request goes
into the journal masked; hubd keeps the input as is in memory and hands it to Claude Code on "Allow". All values are made up."""
import tests  # noqa: F401 — keep the machine's settings out of the test (tests/__init__.py)
import json
import os
import shutil
import tempfile
import types
import unittest

from core import journal, langdata, redact, spool
from core.daemon import Hub
from hosts.streamjson import StreamHost

TOKEN = "ghp_" + "Z" * 36
OWNER = {"channel": "cli", "owner": True}


def line(**msg):
    return json.dumps(msg) + "\n"


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class JournalMasks(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="mask-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.j = journal.Journal(os.path.join(self.dir, "journal.jsonl"))

    def text(self):
        with open(self.j.path, encoding="utf-8") as f:
            return f.read()

    def test_tool_input_and_result_are_masked(self):
        ev = self.j.append("turn.tool", "w1", {"id": "t1", "name": "Bash", "input": {"command": f"export GH={TOKEN}"}})
        self.j.append("turn.tool_result", "w1", {"id": "t1", "error": False, "text": f"token: {TOKEN}"})
        self.assertNotIn(TOKEN, json.dumps(ev))   # what hubd folds is the masked version
        self.assertNotIn(TOKEN, self.text())
        first, second = journal.read(self.j.path)
        self.assertEqual(first["data"]["input"]["command"], f"export GH={redact.MASK}")
        self.assertEqual((second["data"]["id"], second["data"]["error"]), ("t1", False))

    def test_a_password_in_words_in_an_mcp_message_and_in_a_chat_list_is_masked(self):
        # a session sends a password through a chat MCP tool, then the tool's list of chats shows it in the preview of
        # the last message: the label is a word of a language with data (core/langdata.py), in the input and inside the
        # JSON of the output (escaped or not)
        word = langdata.get("ru", "secrets", "labels")[0].capitalize()
        value = "Xq7" + "zP9wK2"
        self.j.append("turn.tool", "w1", {"id": "t1", "name": "mcp__chat__send_message",
                                          "input": {"chat_id": 42, "text": f"login: admin\n{word}: {value}"}})
        chats = [{"id": 42, "title": "x", "lastMessage": {"text": f"{word} - {value}"}, "unread": 0}]
        self.j.append("turn.tool_result", "w1", {"id": "t1", "error": False, "text": json.dumps(chats)})
        preview = json.dumps({"preview": f"{word}: {value}"}, ensure_ascii=False)
        self.j.append("turn.tool_result", "w1", {"id": "t2", "error": False, "text": preview})
        self.assertNotIn(value, self.text())
        first, second, third = journal.read(self.j.path)
        self.assertEqual(first["data"]["input"], {"chat_id": 42, "text": f"login: admin\n{word}: {redact.MASK}"})
        self.assertEqual(json.loads(second["data"]["text"])[0]["lastMessage"]["text"], f"{word} - {redact.MASK}")
        self.assertEqual(json.loads(third["data"]["text"]), {"preview": f"{word}: {redact.MASK}"})

    def test_the_start_of_a_turn_is_masked_and_its_label_stays(self):
        # the hook masks it already (hooks/hub_hook.translate); the journal does it for any other writer, the same way
        ev = self.j.append("turn.started", "w1", {"marker": "m5", "also": ["m7"], "prompt_head": f"[hub:m5] use {TOKEN}",
                                                  "notice": {"summary": f"curl {TOKEN}", "ended": ["b1"]}})
        self.assertNotIn(TOKEN, json.dumps(ev))
        self.assertNotIn(TOKEN, self.text())
        self.assertEqual(ev["data"], {"marker": "m5", "also": ["m7"], "prompt_head": f"[hub:m5] use {redact.MASK}",
                                      "notice": {"summary": f"curl {redact.MASK}", "ended": ["b1"]}})

    def test_background_titles_and_output_are_masked(self):
        # a background title is the first 120 characters of the command: a clipped token lost its shape but not its name
        cut = "GITHUB_TOKEN=ghp_" + "Z" * 20
        self.j.append("session.background", "w1", {"id": "b1", "kind": "bash", "title": f"{cut} npm publish",
                                                   "output": "/tmp/tasks/b1.output"})
        self.j.append("session.background", "w1", {"tasks": [{"id": "b1", "kind": "bash", "title": f"{cut} npm publish"}]})
        self.j.append("session.background_output", "w1", {"id": "b1", "at": 1.0, "line": f"token is {TOKEN}"})
        self.assertNotIn(cut, self.text())
        self.assertNotIn(TOKEN, self.text())
        first, second, _ = journal.read(self.j.path)
        self.assertEqual(first["data"]["title"], f"GITHUB_TOKEN={redact.MASK} npm publish")
        self.assertEqual(first["data"]["output"], "/tmp/tasks/b1.output")   # the watchdog reads the output file
        self.assertEqual(second["data"]["tasks"][0]["title"], f"GITHUB_TOKEN={redact.MASK} npm publish")

    def test_what_goes_on_as_written_is_not_masked(self):
        # messages are delivered verbatim, the owner gets a session's line as is; hubd masks the permission request
        # (the input as is stays in its memory for the answer)
        self.j.append("message.received", None, {"to": "w1", "text": f"keep {TOKEN}"})
        self.j.append("session.said", "w1", {"text": f"said {TOKEN}"})
        self.j.append("permission.requested", "w1", {"request_id": "r1", "tool": "Bash", "input": {"command": TOKEN}})
        self.assertEqual(self.text().count(TOKEN), 3)


class Base(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="maskhub-")
        self.addCleanup(shutil.rmtree, self.state, True)
        self.clock = Clock()
        self.answers = []
        self.h = self.hub()
        self.h.record("session.requested", "a", {"cwd": self.state, "session_id": "s-a", **OWNER})
        self.h.record("session.started", "a", {"session_id": "s-a"})
        self.h.record("turn.started", "a", {"marker": None, "prompt_head": "release"}, src="hook")

    def hub(self):
        host = StreamHost(self.state, claude=("claude-not-run",))
        host.clock = self.clock
        host.spawn = lambda *a, **kw: "spawned"   # the process is "alive": no spawn, no "process gone"
        host.alive, host.sessions, host.info = (lambda name: True), (lambda: ["a"]), (lambda name: {"pid": 1, "pane": None})
        host.respond = lambda name, request_id, response, **_: self.answers.append((name, request_id, response)) \
            or "answered"
        self.host = host
        return Hub(self.state, host, registry_dir=os.path.join(self.state, "registry"), clock=self.clock)

    def stdout(self, *lines):
        """Session a's stdout lines — through the same reader thread as a real process, plus a core tick."""
        self.clock.t += 1
        self.host.reader("a", types.SimpleNamespace(stdout=list(lines)))()
        self.h.tick()

    def text(self):
        with open(self.h.journal.path, encoding="utf-8") as f:
            return f.read()

    def request(self):
        (ev,) = [e for e in journal.read(self.h.journal.path) if e["type"] == "permission.requested"]
        return ev["data"]

    def ask(self, rid, command, **req):
        self.stdout(line(type="control_request", request_id=rid, request={
            "subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": command, "description": "release"},
            **req}))


class ThroughTheHub(Base):
    def test_a_key_through_the_host_and_the_spool_reaches_neither_the_journal_nor_the_state(self):
        self.stdout(line(type="assistant", message={"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": f"GH={TOKEN} gh release list"}}]}),
            line(type="user", message={"content": [{"type": "tool_result", "tool_use_id": "t1", "content": TOKEN}]}))
        spool.put(self.h.spool_dir, "turn.tool", "a", {"id": "t2", "name": "Bash", "input": {"command": TOKEN}},
                  src="hook")
        self.h.tick()
        self.h.snapshot("test")
        self.h.save_state()
        self.assertGreaterEqual(self.text().count(redact.MASK), 3)
        for text in (self.text(), json.dumps(self.h.state, ensure_ascii=False),
                     json.dumps(journal.load_state(self.state), ensure_ascii=False)):
            self.assertNotIn(TOKEN, text)

    def test_the_host_masks_before_it_clips(self):
        # a key at the clipping boundary (1500 characters): the clipped tail is shorter than the pattern, and after clipping the mask does not recognize it
        long = "x" * 1480 + " " + TOKEN
        self.stdout(line(type="assistant", message={"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": long}}]}),
            line(type="user", message={"content": [{"type": "tool_result", "tool_use_id": "t1", "content": long}]}))
        self.assertNotIn("ghp_", self.text())

    def test_a_background_title_is_masked_before_it_is_cut(self):
        command = "echo " + "y" * 100 + " " + TOKEN
        self.stdout(line(type="assistant", message={"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": command, "run_in_background": True}}]}),
            line(type="user", message={"content": [{"type": "tool_result", "tool_use_id": "t1",
                                                    "content": "Command running in background with ID: b1."}]}),
            line(type="system", subtype="background_tasks_changed",
                 tasks=[{"task_id": "b1", "task_type": "local_bash", "description": "z" * 190 + " " + TOKEN}]))
        self.assertNotIn("ghp_", self.text())
        self.assertNotIn("ghp_", json.dumps(self.h.state))

    def test_the_reason_of_a_request_is_masked(self):
        self.ask("r1", "ls", decision_reason=f"the command contains {TOKEN}")
        self.assertNotIn(TOKEN, self.text())


class PermissionInput(Base):
    def test_the_journal_and_the_card_get_a_masked_copy_and_an_allow_runs_the_input_as_sent(self):
        command = f"GH_TOKEN={TOKEN} gh release create"
        self.ask("r1", command)
        (req,) = self.h.state["sessions"]["a"]["pending"]
        self.assertEqual(req["input"]["command"], f"GH_TOKEN={redact.MASK} gh release create")   # what the owner sees
        self.assertNotIn(TOKEN, self.text())
        self.assertTrue(self.request()["masked"])
        spool.put(self.h.spool_dir, "permission.answered", "a", {"request_id": "r1", "behavior": "allow", **OWNER})
        self.h.tick()
        (answer,) = [r for _, rid, r in self.answers if rid == "r1"]
        self.assertEqual(answer, {"behavior": "allow", "updatedInput": {"command": command, "description": "release"}})
        self.assertEqual(self.h.inputs, {})   # after the answer the input is not kept
        self.assertNotIn(TOKEN, self.text())

    def test_a_deny_keeps_nothing(self):
        self.ask("r1", f"GH_TOKEN={TOKEN} make")
        spool.put(self.h.spool_dir, "permission.answered", "a", {"request_id": "r1", "behavior": "deny",
                                                                  "message": "no", **OWNER})
        self.h.tick()
        self.assertEqual(self.answers[-1][2]["behavior"], "deny")
        self.assertEqual(self.h.inputs, {})

    def test_an_input_without_keys_is_not_marked(self):
        self.ask("r1", "ls -la")
        self.assertNotIn("masked", self.request())
        self.assertEqual(self.h.inputs, {})

    def test_after_a_hubd_restart_an_allow_is_a_refusal_not_a_masked_command(self):
        self.ask("r1", f"GH_TOKEN={TOKEN} gh release create")
        self.h = self.hub()   # the new hubd has no input in memory
        spool.put(self.h.spool_dir, "permission.answered", "a", {"request_id": "r1", "behavior": "allow", **OWNER})
        self.h.tick()
        (answer,) = [r for _, rid, r in self.answers if rid == "r1"]
        self.assertEqual(answer["behavior"], "deny")
        self.assertNotIn(redact.MASK, json.dumps(answer))

    def test_an_auto_allowed_request_runs_as_sent(self):
        self.host.auto_allow_on = lambda: True
        command = f"GH_TOKEN={TOKEN} gh release create"
        self.ask("r1", command)
        (answer,) = [r for _, rid, r in self.answers if rid == "r1"]
        self.assertEqual(answer["updatedInput"]["command"], command)
        self.assertNotIn(TOKEN, self.text())
        self.assertEqual(self.h.inputs, {})

    def test_a_question_to_the_owner_is_kept_as_written(self):
        # form answers are matched by the question text — it is not masked
        q = {"questions": [{"question": f"Take {TOKEN}?", "options": [{"label": "yes"}]}]}
        self.stdout(line(type="control_request", request_id="r2", request={
            "subtype": "can_use_tool", "tool_name": "AskUserQuestion", "input": q, "requires_user_interaction": True}))
        self.assertIn(TOKEN, self.text())


class WatcherAlert(unittest.TestCase):
    def test_an_alert_quoting_background_output_is_masked(self):
        from modules.watch import watch
        state = tempfile.mkdtemp(prefix="maskwatch-")
        self.addCleanup(shutil.rmtree, state, True)
        w = watch.Watcher(state, {}, ask=None, needs=None, urgent=None, recap=lambda text: "")
        w.emit("a", 1000.0, {"kind": "background", "reason": f"last output: \"{TOKEN}\""})
        files = [f for f in os.listdir(os.path.join(state, "spool")) if f.endswith(".json")]
        self.assertTrue(files)
        for name in files:
            with open(os.path.join(state, "spool", name), encoding="utf-8") as f:
                self.assertNotIn(TOKEN, f.read())


if __name__ == "__main__":
    unittest.main()
