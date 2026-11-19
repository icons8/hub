"""A task record (decision of 2026-10-04: a task is a hub entity, everyone present can write and read it with a
guard against overwriting each other; the task holds all the information; a shared journal is built from tasks
later). The file tasks/<dN>.md in the hub folder: a head taken from the delegation, and the log section as
lines "when · who: what" (append-only, so concurrent writes do not break), the head is edited with a fingerprint
check (so another's edit is not overwritten); the journal gets a task.noted event."""
import tests  # noqa: F401 — hide the machine's settings
import os
import subprocess
import sys
import tempfile
import unittest

from core import journal, spool, taskrec

HUB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")
OWNER = {"channel": "cli", "owner": True}


class TaskRecord(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="rec-")
        j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        j.append("session.requested", "hub", {"cwd": "/h", "session_id": "s-hub", "kind": "hub", **OWNER})
        for n in ("frontend", "frontend-ci"):
            j.append("session.requested", n, {"cwd": "/w", "session_id": f"s-{n}", **OWNER})
        ev = j.append("delegation.created", None, {"session": "frontend-ci", "for_session": "frontend",
                                                   "context": "fix the CI secrets\nwhen done: the pipeline is green",
                                                   "channel": "session", "owner": False, "sender": "frontend"})
        self.did = f"d{ev['seq']}"

    def hub(self, *args, me=None, ok=True):
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_CLI_WAIT": "0"}
        env.pop("HUB_SESSION", None)
        if me:
            env["HUB_SESSION"] = me
        r = subprocess.run([sys.executable, HUB, *args], env=env, capture_output=True, text=True)
        if ok:
            self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        return r

    def record(self):
        with open(os.path.join(self.state, "tasks", f"{self.did}.md"), encoding="utf-8") as f:
            return f.read()

    def test_show_creates_the_record_from_the_task(self):
        out = self.hub("task", "show", self.did).stdout
        self.assertIn("fix the CI secrets", out)
        self.assertIn("frontend-ci", out)                                      # who is doing it
        self.assertIn("fingerprint", out)
        self.assertIn("## Progress", self.record())

    def test_note_appends_with_author_and_time_and_lands_in_the_journal(self):
        self.hub("task", "note", self.did, "secrets", "are", "in", "vault", me="frontend")
        self.hub("task", "note", self.did, "MR !12 is open", me="frontend-ci")
        rec = self.record()
        self.assertIn("frontend: secrets are in vault", rec)
        self.assertIn("frontend-ci: MR !12 is open", rec)
        self.assertLess(rec.index("vault"), rec.index("MR !12"))
        evs = [e for _, e in spool.take(os.path.join(self.state, "spool")) if e["type"] == "task.noted"]
        self.assertEqual([e["entity"] for e in evs], [self.did, self.did])

    def test_a_head_that_cannot_be_built_leaves_no_empty_record(self):
        # an empty record would stay forever: later lines of the log would land without a head, and a section edit
        # would fail on it
        with self.assertRaises(KeyError):
            taskrec.note(self.state, {"delegations": {}}, "d900", "hub", "first line")
        self.assertFalse(os.path.exists(taskrec.path(self.state, "d900")))
        st = {"delegations": {"d900": {"session": "maps", "context": "draw the map"}}}
        taskrec.note(self.state, st, "d900", "hub", "first line")
        text = taskrec.read(self.state, st, "d900")
        self.assertTrue(text.startswith("# d900 — draw the map"), text)
        self.assertIn("first line", text)

    def test_a_session_names_its_own_task_or_an_executor_by_name(self):
        self.hub("task", "note", "task", "by", "name", me="frontend-ci")         # without a number — its own task
        self.hub("task", "note", "frontend-ci", "clarification", me="frontend")     # by the executor's name
        rec = self.record()
        self.assertIn("frontend-ci: task by name", rec)
        self.assertIn("frontend: clarification", rec)

    def test_a_note_without_a_number_refuses_when_two_tasks_are_open(self):
        # it used to go silently to the newest unclosed task, and after a task was closed - to an older open one
        j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        ev = j.append("delegation.created", None, {"session": "frontend-ci", "for_session": "frontend",
                                                   "context": "second job: tidy the pipeline names",
                                                   "channel": "session", "owner": False, "sender": "frontend"})
        second = f"d{ev['seq']}"
        r = self.hub("task", "note", "which one?", me="frontend-ci", ok=False)
        self.assertNotEqual(r.returncode, 0)
        for did in (self.did, second):
            self.assertIn(did, r.stderr)
        for did in (self.did, second):   # nothing was written anywhere
            self.assertFalse(os.path.exists(taskrec.path(self.state, did)))
        self.hub("task", "note", second, "named, so it goes there", me="frontend-ci")   # a number settles it
        with open(taskrec.path(self.state, second), encoding="utf-8") as f:
            self.assertIn("named, so it goes there", f.read())
        # one open task left (the first is closed): a note without a number goes to it, as before
        self.close()
        self.hub("task", "note", "to the one left", me="frontend-ci")
        with open(taskrec.path(self.state, second), encoding="utf-8") as f:
            self.assertIn("to the one left", f.read())

    def test_handed_in_tasks_do_not_make_a_note_without_a_number_ambiguous(self):
        # replied is the usual end of a task, not an open one: two handed-in tasks and one in work - the note goes to
        # the one in work, no refusal
        from core import reducer
        self.close()   # the task of setUp is out of the way
        j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        j.append("session.started", "frontend-ci", {"session_id": "s-frontend-ci"})
        ids = []
        for n in range(3):
            ev = j.append("delegation.created", None, {"session": "frontend-ci", "for_session": "frontend",
                                                       "context": f"job {n}", "channel": "session", "owner": False,
                                                       "sender": "frontend"})
            ids.append(f"d{ev['seq']}")
            if n < 2:   # answered: the turn that carried the task ends with a result
                st = reducer.fold(reducer.empty(), journal.read(j.path))
                j.append("turn.started", "frontend-ci", {"marker": st["delegations"][ids[-1]]["message"],
                                                         "prompt_head": "task"})
                j.append("turn.result", "frontend-ci", {"text": "done"})
                j.append("turn.stopped", "frontend-ci", {})
        st = reducer.fold(reducer.empty(), journal.read(j.path))
        self.assertEqual([st["delegations"][i]["state"] for i in ids], ["replied", "replied", "working"])
        self.assertEqual([d["id"] for d in taskrec.open_of(st, "frontend-ci")], [ids[2]])
        self.hub("task", "note", "to the one in work", me="frontend-ci")
        with open(taskrec.path(self.state, ids[2]), encoding="utf-8") as f:
            self.assertIn("to the one in work", f.read())

    def test_tasks_made_in_the_same_millisecond_resolve_to_the_latest(self):
        # the event time is rounded to the millisecond: on a fast machine two tasks of one session tie, and max() took
        # the first of them - the note without a number went to a handed-in task, not the one in work
        st = {"sessions": {"frontend-ci": {}}, "delegations": {
            f"d{n}": {"id": f"d{n}", "session": "frontend-ci", "state": "replied" if n < 15 else "working",
                      "created": 1700000000.648} for n in (7, 11, 15)}}
        self.assertEqual(taskrec.resolve(st, None, "frontend-ci", latest=True), "d15")
        self.assertEqual(taskrec.resolve(st, "frontend-ci", None), "d15")

    def close(self):
        j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        j.append("delegation.done", self.did, dict(OWNER))

    def test_a_closed_task_takes_notes_from_its_own_sessions_and_says_it_is_closed(self):
        # multi-day work on the owner's direct messages: the original task is closed and there is no new one — the
        # log has nowhere to go
        self.close()
        self.hub("task", "note", self.did, "decided to keep vault", me="frontend-ci")   # the one who did it
        self.hub("task", "note", self.did, "checked", me="frontend")                  # the one who set it
        self.hub("task", "note", self.did, "from the owner")                              # the owner
        r = self.hub("task", "note", self.did, "a foreign line", me="hub")                # the hub
        rec = self.record()
        for line in ("frontend-ci: (the task is closed) decided to keep vault", "frontend: (the task is closed) checked",
                     "owner: (the task is closed) from the owner", "hub: (the task is closed) a foreign line"):
            self.assertIn(line, rec)
        self.assertIn(self.did, r.stdout)

    def test_a_stranger_cannot_note_a_closed_task(self):
        j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        j.append("session.requested", "maps", {"cwd": "/m", "session_id": "s-maps", **OWNER})
        self.hub("task", "note", self.did, "while open, everyone writes", me="maps")
        self.close()
        r = self.hub("task", "note", self.did, "after closing", me="maps", ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("is closed", r.stderr)   # a session's command: in English
        self.assertNotIn("after closing", self.record())
        evs = [e for _, e in spool.take(os.path.join(self.state, "spool")) if e["type"] == "task.noted"]
        self.assertEqual(len(evs), 1)

    def test_without_a_number_a_session_with_no_open_task_writes_into_its_last_one(self):
        self.close()
        r = self.hub("task", "note", "progress", "on", "direct", "messages", me="frontend-ci")
        self.assertIn(self.did, r.stdout)                                      # which one it chose is in the reply
        self.assertIn("frontend-ci: (the task is closed) progress on direct messages", self.record())
        # show without a number does not take a closed one: a session woken with a new task would read the old one
        # before the new one is written
        r = self.hub("task", "show", me="frontend-ci", ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(self.did, self.hub("task", "show", self.did, me="frontend-ci").stdout)

    def test_a_plugin_cannot_note_a_closed_task(self):
        self.close()
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_CLI_WAIT": "0", "HUB_EXTENSION": "frontend-ci"}
        env.pop("HUB_SESSION", None)
        r = subprocess.run([sys.executable, HUB, "task", "note", self.did, "from a plugin"], env=env,
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("is closed", r.stderr)
        self.assertFalse(os.path.exists(taskrec.path(self.state, self.did)))

    def test_a_session_without_any_task_gets_a_clear_refusal(self):
        r = self.hub("task", "note", "nowhere", me="frontend", ok=False)          # set tasks but did none
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no tasks", r.stderr)   # a session's command: in English
        self.assertIn("hub task new", r.stderr)
        r = self.hub("task", "note", "nowhere", me="hub", ok=False)               # for the hub — not "ask the hub"
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("hub send hub", r.stderr)
        r = self.hub("task", "show", me="frontend", ok=False)                   # reading — the old refusal
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no such task", r.stderr)   # a session's command: in English

    def test_head_edit_checks_the_fingerprint(self):
        out = self.hub("task", "show", self.did).stdout
        sha = out.rsplit("fingerprint ", 1)[1].split()[0]
        self.hub("task", "note", self.did, "a foreign line", me="frontend")      # someone appended after the read
        r = self.hub("task", "head", self.did, "--sha", sha, "a new statement", me="frontend", ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("has changed since you read it", r.stderr)   # a session's command: in English
        sha2 = self.hub("task", "show", self.did).stdout.rsplit("fingerprint ", 1)[1].split()[0]
        self.hub("task", "head", self.did, "--sha", sha2, "a new", "statement", me="frontend")
        rec = self.record()
        self.assertIn("a new statement", rec)
        self.assertIn("a foreign line", rec)                                      # the log is untouched

    def test_the_doer_does_not_get_its_brief_twice(self):
        # executors call hub task show first (REPORT_RULE) and got the task text again — ~6k chars
        # that already came as their first message and stay in the context to the end of the run
        self.hub("task", "note", self.did, "secrets are in vault", me="frontend")
        own = self.hub("task", "show", me="frontend-ci").stdout
        self.assertNotIn("when done: the pipeline is green", own)
        self.assertIn(f"hub task show {self.did} --full", own)                  # the way to the whole text
        self.assertIn("secrets are in vault", own)                             # notes stay
        self.assertIn("fingerprint", own)
        full = self.hub("task", "show", "--full", me="frontend-ci").stdout
        self.assertIn("when done: the pipeline is green", full)
        self.assertEqual(own.rsplit("fingerprint ", 1)[1], full.rsplit("fingerprint ", 1)[1])   # the record's, not the view's
        self.assertIn("when done: the pipeline is green", self.hub("task", "show", self.did, me="frontend").stdout)
        sha = full.rsplit("fingerprint ", 1)[1].split()[0]
        self.hub("task", "head", self.did, "--sha", sha, "the new statement", me="frontend")
        self.assertIn("the new statement", self.hub("task", "show", me="frontend-ci").stdout)   # changed: shown whole

    def test_the_hint_of_the_folded_brief_names_its_task(self):
        # a closed task, opened by number: the hint must lead to the whole text of this very task, not of the latest one
        self.close()
        own = self.hub("task", "show", self.did, me="frontend-ci").stdout
        self.assertNotIn("when done: the pipeline is green", own)
        hint = own.split("the whole text: ")[1].split(")")[0]
        full = self.hub(*hint.split()[1:], me="frontend-ci").stdout             # the hint, run as written
        self.assertIn("when done: the pipeline is green", full)


class RecordHeadings(unittest.TestCase):
    """A new record has the headings of the hub's language (English without a known one); one an earlier core wrote keeps its own (every hub's tasks/ has them):
    the head edit and the full context land in their sections."""
    def setUp(self):
        from core import langdata
        self.state = tempfile.mkdtemp(prefix="rec-")
        self.st = {"delegations": {"d1": {"id": "d1", "session": "maps", "for": None, "for_session": "seo",
                                          "context": "check the tiles", "state": "working"}}}
        self.old = langdata.legacy("task_record")

    def write(self, text):
        os.makedirs(os.path.join(self.state, "tasks"), exist_ok=True)
        with open(taskrec.path(self.state, "d1"), "w", encoding="utf-8") as f:
            f.write(text)

    def test_a_new_record_is_written_in_english(self):
        text = taskrec.read(self.state, self.st, "d1")
        self.assertIn("Set by: seo · done by: maps", text)
        self.assertIn(taskrec.TASK + "check the tiles\n" + taskrec.LOG, text)

    def test_an_earlier_record_keeps_its_headings_for_the_head_edit_and_the_context(self):
        self.write("# d1 — check the tiles\n\n" + self.old["task"] + "check the tiles\n" + self.old["log"]
                   + "- 01.10 10:00 · maps: started\n")
        sha = taskrec.fingerprint(taskrec.read(self.state, self.st, "d1"))
        sha = taskrec.set_task(self.state, self.st, "d1", "the tiles of the north", sha)
        self.assertTrue(taskrec.add_context(self.state, self.st, "d1", "the full brief"))
        text = taskrec.read(self.state, self.st, "d1")
        self.assertEqual(text, "# d1 — check the tiles\n\n" + self.old["task"] + "the tiles of the north\n\nthe full brief\n"
                         + self.old["log"] + "- 01.10 10:00 · maps: started\n")
        self.assertNotIn(taskrec.TASK, text)

    def test_a_russian_hub_writes_the_record_in_russian_and_reads_every_form(self):
        from core import i18n
        self.st["owner"] = {"lang": "ru"}
        text = taskrec.read(self.state, self.st, "d1")
        task, log = taskrec.heads_of("ru")
        self.assertEqual((task, log), (self.old["task"], self.old["log"]))   # the same headings the earlier core wrote
        self.assertIn(i18n.tr("ru", "core.taskrec.set_by", asked="seo", session="maps"), text)
        sha = taskrec.set_task(self.state, self.st, "d1", "the tiles of the north", taskrec.fingerprint(text))
        self.assertIn(task + "the tiles of the north\n" + log, taskrec.read(self.state, self.st, "d1"))
        self.assertTrue(sha)
        # a record made in English on this hub before its language was known keeps its headings too
        self.write("# d1\n\n" + taskrec.TASK + "see\n" + taskrec.LOG)
        taskrec.set_task(self.state, self.st, "d1", "new", taskrec.fingerprint(taskrec.read(self.state, self.st, "d1")))
        self.assertEqual(taskrec.read(self.state, self.st, "d1"), "# d1\n\n" + taskrec.TASK + "new\n" + taskrec.LOG)

    def test_an_english_heading_inside_an_earlier_record_is_text(self):
        self.write("# d1\n\n" + self.old["task"] + "see" + taskrec.LOG + "more\n" + self.old["log"])
        sha = taskrec.fingerprint(taskrec.read(self.state, self.st, "d1"))
        taskrec.set_task(self.state, self.st, "d1", "new", sha)
        self.assertEqual(taskrec.read(self.state, self.st, "d1"), "# d1\n\n" + self.old["task"] + "new\n" + self.old["log"])

    def test_the_doer_folds_its_brief_in_a_record_of_earlier_headings(self):
        # every hub's tasks/ holds records an earlier core wrote with the old headings: the fold finds the section there too
        self.write(f"# d1\n\n{self.old['task']}check the tiles\n{self.old['log']}")
        folded = taskrec.for_doer(taskrec.read(self.state, self.st, "d1"), self.st, "d1")
        self.assertNotIn("check the tiles", folded)
        self.assertIn("hub task show d1 --full", folded)


class EveryTaskNamesItsRecord(unittest.TestCase):
    """Decision of 2026-10-04: everything must be written into the task — do sessions do it? No: the rule
    existed only for the executor, while an ordinary task arrived without a number and without a word about the
    record."""

    def test_the_task_text_carries_its_number_and_the_record_rule(self):
        from core import reducer
        st = reducer.empty()
        evs = [("session.requested", "hub", {"cwd": "/h", "session_id": "s-hub", "kind": "hub", **OWNER}),
               ("session.requested", "maps", {"cwd": "/w", "session_id": "s-m", **OWNER}),
               ("message.received", None, {"to": "hub", "text": "a map of the offices", **OWNER}),
               ("delegation.created", None, {"session": "maps", "for": "m3", "context": "",
                                             "channel": "session", "owner": False, "sender": "hub"}),
               ("delegation.created", None, {"session": "maps", "for_session": "hub", "context": "check the addresses",
                                             "channel": "session", "owner": False, "sender": "hub"})]
        for seq, (t, e, d) in enumerate(evs, 1):
            st = reducer.reduce(st, {"seq": seq, "ts": 100 + seq, "type": t, "entity": e, "data": d})[0]
        for did in ("d4", "d5"):
            text = st["messages"][st["delegations"][did]["message"]]["text"]
            self.assertIn(f"Task {did}: its record", text)
            self.assertIn("hub task note", text)
            self.assertIn("hub task show", text)


class CoreKeepsTheRecord(unittest.TestCase):
    """Decision of 2026-10-04: everything that starts must write to task.note — that is why it exists, so nothing is lost.
    The core keeps the task trail itself: set, question, answer, result, sleep, closed; the model only adds to it."""

    def setUp(self):
        from core import reducer
        self.reducer, self.st, self.seq, self.notes = reducer, reducer.empty(), 0, []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.requested", "maps", cwd="/w", session_id="s-m", **OWNER)
        self.ev("session.started", "hub")
        self.ev("session.started", "maps")

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, acts, why = self.reducer.reduce(self.st, {"seq": self.seq, "ts": 100 + self.seq, "type": type,
                                                           "entity": entity, "data": data})
        self.notes += [a for a in acts if a["do"] == "task_note"]
        return why

    def test_the_trail_of_a_task(self):
        hub = {"channel": "session", "owner": False, "sender": "hub"}
        self.ev("message.received", None, to="hub", text="a map of the offices", **OWNER)
        self.ev("delegation.created", None, session="maps", **{"for": f"m{self.seq}"}, context="", **hub)
        d = self.st["delegations"][f"d{self.seq}"]
        self.ev("turn.started", "maps", marker=d["message"], prompt_head="x")
        self.ev("permission.requested", "maps", request_id="r1", tool="AskUserQuestion", interactive=True,
                input={"questions": [{"question": "What scale?", "options": [{"label": "city"}]}]})
        self.ev("turn.result", "maps", text="asked about the scale")
        self.ev("turn.stopped", "maps")
        self.ev("permission.answered", "maps", request_id="r1", behavior="allow", answers={"What scale?": "city"},
                channel="web", owner=True)
        ans = next(m for m in self.st["messages"].values() if m.get("answers_question"))
        self.ev("turn.started", "maps", marker=ans["id"], prompt_head="x")
        self.ev("turn.result", "maps", text="Done: the map is in docs/map.md")
        self.ev("delegation.done", d["id"], **OWNER)
        self.assertTrue(all(n["delegation"] == d["id"] for n in self.notes))
        trail = " | ".join(n["text"] for n in self.notes)
        for part in ("task set", "question", "What scale?", "answer", "city", "result: ", "docs/map.md", "task closed"):
            self.assertIn(part, trail)

    def test_the_daemon_writes_the_trail_into_the_record(self):
        import types
        from core.daemon import Hub
        state = tempfile.mkdtemp(prefix="trail-")
        h = Hub(state, types.SimpleNamespace())
        h.state = self.st
        hub = {"channel": "session", "owner": False, "sender": "hub"}
        self.ev("delegation.created", None, session="maps", context="check the addresses", **hub)
        h.state = self.st
        for a in self.notes:
            h.execute(a)
        with open(os.path.join(state, "tasks", f"d{self.seq}.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("check the addresses", text)
        self.assertIn("task set", text)

    def test_a_russian_hub_gets_the_trail_in_russian(self):
        import types
        from core import i18n
        from core.daemon import Hub
        self.st["owner"]["lang"] = "ru"   # the owner writes Russian: the record is in his language, as before
        state = tempfile.mkdtemp(prefix="trail-")
        h = Hub(state, types.SimpleNamespace())
        hub = {"channel": "session", "owner": False, "sender": "hub"}
        self.ev("delegation.created", None, session="maps", context="check the addresses", **hub)
        h.state = self.st
        for a in self.notes:
            h.execute(a)
        with open(os.path.join(state, "tasks", f"d{self.seq}.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn(taskrec.heads_of("ru")[0], text)
        line = text.splitlines()[-1]
        self.assertNotIn("task set", line)
        self.assertIn(i18n.tr("ru", "core.delegations.by_core"), line)


if __name__ == "__main__":
    unittest.main()
