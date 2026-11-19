"""A delegation has an "archived" end (owner decision 26.10; deleg-archive spec): a closed delegation of a removed
executor gets the archived outcome after ARCHIVE_AFTER — the core trims context and reply (the full ones stay in the task
record and the journal) and releases the messages the delegation held back from core.pruned. "Done" is not set
implicitly, sessions are not touched."""
import tests  # noqa: F401 — ignore this machine's settings
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

from core import corewrite, daemon, delegations, executors, i18n, journal, machines, reducer, spool, taskrec, views
from core.daemon import Hub

OWNER = {"channel": "cli", "owner": True}


def by(name):
    return {"channel": "session", "owner": False, "sender": name}


class Base(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.actions = reducer.empty(), 0, []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.started", "hub")
        self.ev("session.requested", "maps", cwd="/m", session_id="s-maps", role="agent", about="Maps", **OWNER)
        self.ev("session.started", "maps")

    def ev(self, type, entity=None, ts=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts or 1000 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.actions += acts
        return why

    def executor(self, name, text="fix the map", reply="done: the map is fixed", raiser="maps"):
        """The executor is started, answered with a result (replied) and removed; returns its delegation number."""
        self.assertIsNone(self.ev("task.run_requested", name, task=text, **by(raiser)))
        run = self.st["runs"][name]
        for type, entity, data in executors.events_for(self.st, name, run, cwd="/m/.claude/worktrees/x", worktree=None):
            self.assertIsNone(self.ev(type, entity, **data), (type, data))
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == name]
        self.ev("session.started", name)
        self.ev("turn.started", name, marker=d["message"], prompt_head="task")
        self.ev("turn.result", name, text=reply)
        self.ev("turn.stopped", name)
        self.assertEqual(self.st["delegations"][d["id"]]["state"], "replied")
        self.remove(name)
        return d["id"]

    def remove(self, name):
        if self.st["sessions"][name]["state"] not in ("asleep", "failed", "closed", "removed"):
            self.ev("session.ended", name, reason="prompt_input_exit")
        if self.st["sessions"][name]["state"] != "removed":
            self.assertIsNone(self.ev("session.remove_requested", name, **OWNER))
        self.assertEqual(self.st["sessions"][name]["state"], "removed")


class TheMachine(Base):
    def test_a_replied_task_of_a_removed_executor_goes_to_the_archive_trimmed(self):
        did = self.executor("maps-fix", text="C" * 2000, reply="R" * 3000)
        d = self.st["delegations"][did]
        before = {k: v for k, v in d.items() if k not in ("context", "reply", "state", "since")}
        self.assertIsNone(self.ev("core.archived", None, ids=[did]))
        d = self.st["delegations"][did]
        self.assertEqual(d["state"], "archived")
        self.assertEqual(len(d["context"]), delegations.CONTEXT_KEEP + 1)
        self.assertTrue(d["context"].endswith("…"))
        self.assertEqual(d["reply"], "R" * delegations.REPLY_KEEP + "…")
        self.assertTrue(d["trimmed"])
        self.assertEqual(d["since"], 1000 + self.seq)
        self.assertEqual({k: v for k, v in d.items() if k not in ("context", "reply", "state", "since", "trimmed")},
                         before)   # the links and the owner's words are intact

    def test_short_texts_stay_as_they_are(self):
        did = self.executor("maps-fix", reply="ok")
        self.st["delegations"][did]["context"] = "C" * delegations.CONTEXT_KEEP
        self.ev("core.archived", None, ids=[did])
        d = self.st["delegations"][did]
        self.assertEqual((d["state"], d["context"], d["reply"]), ("archived", "C" * delegations.CONTEXT_KEEP, "ok"))

    def test_the_owners_words_are_kept_whole(self):
        self.assertIsNone(self.ev("task.run_requested", "maps-fix", task="T", **by("maps")))
        for type, entity, data in executors.events_for(self.st, "maps-fix", self.st["runs"]["maps-fix"], cwd="/m",
                                                       worktree=None):
            self.ev(type, entity, **data)
        self.ev("session.started", "maps-fix")
        self.ev("message.received", None, to="hub", text="the owner's words " * 100, **OWNER)
        mid = f"m{self.seq}"
        self.assertIsNone(self.ev("delegation.created", None, session="maps-fix", context="C" * 1000, **{"for": mid},
                                  **by("hub")))
        d = self.st["delegations"][f"d{self.seq}"]
        words = d["words"]
        self.ev("turn.started", "maps-fix", marker=d["message"], prompt_head="task")
        self.ev("turn.result", "maps-fix", text="done")
        self.ev("turn.stopped", "maps-fix")
        self.remove("maps-fix")
        self.ev("core.archived", None, ids=[d["id"]])
        d = self.st["delegations"][d["id"]]
        self.assertEqual((d["state"], d["words"]), ("archived", words))
        self.assertEqual(len(d["context"]), delegations.CONTEXT_KEEP + 1)

    def test_a_held_monitor_reply_is_dropped(self):
        did = self.executor("maps-fix")
        self.st["delegations"][did]["held"] = {"reply": "waiting", "seq": 1, "ts": 1}
        self.ev("core.archived", None, ids=[did])
        self.assertNotIn("held", self.st["delegations"][did])

    def test_a_stopped_task_of_a_removed_executor_is_archived_too(self):
        did = self.executor("maps-fix")
        self.st["delegations"][did]["state"] = "stopped"
        self.ev("core.archived", None, ids=[did])
        self.assertEqual(self.st["delegations"][did]["state"], "archived")

    def test_a_done_task_of_a_removed_executor_is_trimmed_and_stays_done(self):
        """Done tasks were never archived: their full texts stayed in the state forever (the bulk of a long-lived
        hub's delegations). They are trimmed in place; the state stays "done" — "archived" could be reopened by a
        revived executor, a closed task must not."""
        did = self.executor("maps-fix", text="t" * 2000, reply="r" * 3000)
        self.ev("delegation.done", did, **by("hub"))
        d = self.st["delegations"][did]
        before = {k: v for k, v in d.items() if k not in ("context", "reply")}
        self.assertIsNone(self.ev("core.archived", None, ids=[did]))
        d = self.st["delegations"][did]
        self.assertEqual(d["state"], "done")
        self.assertEqual(len(d["context"]), delegations.CONTEXT_KEEP + 1)
        self.assertEqual(d["reply"], "r" * delegations.REPLY_KEEP + "…")
        self.assertTrue(d["trimmed"])
        self.assertEqual({k: v for k, v in d.items() if k not in ("context", "reply", "trimmed")},
                         before)   # since, the links and the owner's words are intact
        again = reducer.copy_state(self.st["delegations"])
        self.assertIsNone(self.ev("core.archived", None, ids=[did]))   # a repeat is harmless
        self.assertEqual(self.st["delegations"], again)

    def test_a_done_task_of_a_live_executor_is_not_trimmed(self):
        self.assertIsNone(self.ev("task.run_requested", "maps-fix", task="t" * 2000, **by("maps")))
        for type, entity, data in executors.events_for(self.st, "maps-fix", self.st["runs"]["maps-fix"], cwd="/m",
                                                       worktree=None):
            self.ev(type, entity, **data)
        (did,) = [d["id"] for d in self.st["delegations"].values() if d["session"] == "maps-fix"]
        self.ev("delegation.done", did, **by("hub"))
        self.ev("core.archived", None, ids=[did])
        self.assertNotIn("trimmed", self.st["delegations"][did])

    def test_ids_that_do_not_qualify_are_skipped_silently(self):
        gone = self.executor("maps-gone")
        self.assertIsNone(self.ev("task.run_requested", "maps-live", task="more", **by("maps")))
        for type, entity, data in executors.events_for(self.st, "maps-live", self.st["runs"]["maps-live"], cwd="/m",
                                                       worktree=None):
            self.ev(type, entity, **data)
        (live,) = [d["id"] for d in self.st["delegations"].values() if d["session"] == "maps-live"]   # working
        self.ev("delegation.created", None, session="maps", context="to the agent", **by("hub"))
        agent = f"d{self.seq}"
        done = self.executor("maps-done")
        self.ev("delegation.done", done, **by("hub"))
        snap = reducer.copy_state(self.st["delegations"])
        self.assertIsNone(self.ev("core.archived", None, ids=[live, agent, done, "d999999", gone, gone]))
        self.assertEqual(self.st["delegations"][gone]["state"], "archived")
        snap[gone] = self.st["delegations"][gone]
        self.assertEqual(self.st["delegations"][done]["state"], "done")   # its executor is removed: trimmed in place
        snap[done] = self.st["delegations"][done]
        self.assertEqual(self.st["delegations"], snap)   # the rest as it was
        again = reducer.copy_state(self.st)
        self.assertIsNone(self.ev("core.archived", None, ids=[gone]))   # a repeat is harmless
        self.assertEqual(self.st["delegations"], again["delegations"])

    def test_a_live_executors_reply_is_not_archived(self):
        self.assertIsNone(self.ev("task.run_requested", "maps-fix", task="T", **by("maps")))
        for type, entity, data in executors.events_for(self.st, "maps-fix", self.st["runs"]["maps-fix"], cwd="/m",
                                                       worktree=None):
            self.ev(type, entity, **data)
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == "maps-fix"]
        self.ev("session.started", "maps-fix")
        self.ev("turn.started", "maps-fix", marker=d["message"], prompt_head="task")
        self.ev("turn.result", "maps-fix", text="done")
        self.ev("core.archived", None, ids=[d["id"]])
        self.assertEqual(self.st["delegations"][d["id"]]["state"], "replied")

    def test_a_removed_sessions_task_is_not_archived_unless_it_was_an_executor(self):
        """Only "removed executors" were asked for: a delegation of an ordinary session that was removed stays as it is."""
        self.ev("session.requested", "geo", cwd="/g", session_id="s-geo", **OWNER)
        self.ev("session.started", "geo")
        self.ev("delegation.created", None, session="geo", context="to the session", **by("hub"))
        did = f"d{self.seq}"
        self.ev("turn.started", "geo", marker=self.st["delegations"][did]["message"], prompt_head="x")
        self.ev("turn.result", "geo", text="done")
        self.ev("turn.stopped", "geo")
        self.remove("geo")
        self.ev("core.archived", None, ids=[did])
        self.assertEqual(self.st["delegations"][did]["state"], "replied")

    def test_a_malformed_event_is_refused(self):
        self.assertIsNotNone(self.ev("core.archived", None, ids="d1"))
        self.assertIsNone(self.ev("core.archived", None, ids=[{"x": 1}, None, 5]))

    def test_the_archived_task_can_still_be_closed_explicitly(self):
        did = self.executor("maps-fix")
        self.ev("core.archived", None, ids=[did])
        self.assertIsNone(self.ev("delegation.done", did, **by("hub")))
        self.assertEqual(self.st["delegations"][did]["state"], "done")

    def test_the_archive_is_not_done_and_says_so(self):
        self.assertEqual(machines.ENDED, ("done", "archived"))
        self.assertIn("in the archive", machines.DELEGATION["said"]["archived"])
        did = self.executor("maps-fix")
        self.ev("core.archived", None, ids=[did])
        why = self.ev("delegation.created", None, session="maps-fix", context="more", **by("maps"))
        self.assertIsNotNone(why)   # to one that is gone — still not allowed


class Readers(Base):
    """Six places that read "not done" as "open" skip an archived one (machines.ENDED); --about sees it."""
    def three(self):
        """Three delegations of removed executors: replied, archived, done."""
        replied = self.executor("maps-r")
        archived = self.executor("maps-a")
        done = self.executor("maps-d")
        self.ev("delegation.done", done, **by("hub"))
        self.ev("core.archived", None, ids=[archived])
        self.assertEqual([self.st["delegations"][d]["state"] for d in (replied, archived, done)],
                         ["replied", "archived", "done"])
        return replied, archived, done

    def test_the_board_of_the_page_snapshot(self):
        replied, archived, done = self.three()
        r = views.Reader()
        r.state = self.st
        board = {d["id"] for d in r.snapshot()["delegations"]}
        self.assertEqual(board & {replied, archived, done}, set())   # the board is open tasks (stream-lean): "replied" is not read

    def test_messages_held_from_pruning(self):
        replied, archived, done = self.three()
        mids = {k: self.st["delegations"][k]["message"] for k in (replied, archived, done)}
        self.ev("core.pruned", None, before=10 ** 9)
        self.assertEqual({k for k, m in mids.items() if m in self.st["messages"]}, {replied})

    def test_open_task_after_a_restart(self):
        replied, archived, done = self.three()
        self.assertEqual(delegations.open_task(self.st, "maps-r")[0], replied)
        self.assertEqual(delegations.open_task(self.st, "maps-a"), (None, None))
        self.assertEqual(delegations.open_task(self.st, "maps-d"), (None, None))

    def test_about_still_finds_the_archived_task(self):
        replied, archived, done = self.three()
        for did, found in ((replied, True), (archived, True), (done, False)):
            self.ev("message.received", None, to="maps", text="more on this", about=did, **by("hub"))
            msg = self.st["messages"][f"m{self.seq}"]
            self.assertEqual(msg.get("about"), did if found else None, did)
            self.assertEqual(msg["session"], "maps", did)   # a removed one is not redirected

    def test_a_tool_for_a_session_with_only_archived_tasks_does_not_resume_one(self):
        replied, archived, done = self.three()
        for name, did in (("maps-r", replied), ("maps-a", None), ("maps-d", None)):
            st = reducer.copy_state(self.st)
            sess = st["sessions"][name]
            sess.update(state="idle", new_tools=[{"tool": "mail@kit", "asked": False}])
            reducer._tools_apply(st, sess, {"seq": 900, "ts": 5000, "type": "x"}, [])
            said = [m["text"] for m in st["messages"].values() if m["session"] == name and "Connected" in m["text"]]
            self.assertEqual(bool(said), did is not None, name)

    def test_a_core_write_that_removes_the_session_of_an_archived_task(self):
        replied, archived, done = self.three()
        before = self.st
        for name, did in (("maps-r", replied), ("maps-a", None), ("maps-d", None)):
            after = reducer.copy_state(before)
            del after["sessions"][name]
            problems = corewrite.entity_problems(before, after, [{"op": "del", "path": ["sessions", name]}])
            self.assertEqual(any(did in p for p in problems) if did else problems, bool(did) if did else [], name)

    def test_the_task_record_of_an_archived_task_stays_open(self):
        replied, archived, done = self.three()
        self.assertEqual(taskrec.resolve(self.st, "maps-a"), archived)
        self.assertFalse(taskrec.closed(self.st, archived))
        self.assertTrue(taskrec.may_write(self.st, archived, "geo"))


class Revived(Base):
    """A removed executor was brought back (hub session revive) after the archive: its next turn on the word of whoever
    started it continues exactly one of its tasks (the one named by --about, or the last), which is "replied" again, the others stay archived."""
    def revive(self, name):
        sess = self.st["sessions"][name]
        self.assertIsNone(self.ev("session.requested", name, session_id=sess["session_id"], cwd=sess["cwd"],
                                  revive=True, **by("hub")))
        self.ev("session.started", name)

    def two(self):
        old = self.executor("maps-fix", text="the first task")
        self.revive("maps-fix")
        self.ev("delegation.created", None, session="maps-fix", for_session="maps", context="the second task",
                **by("maps"))
        new = f"d{self.seq}"
        self.ev("turn.started", "maps-fix", marker=self.st["delegations"][new]["message"], prompt_head="x")
        self.ev("turn.result", "maps-fix", text="the second is done")
        self.ev("turn.stopped", "maps-fix")
        self.remove("maps-fix")
        self.ev("core.archived", None, ids=[old, new])
        self.assertEqual([self.st["delegations"][d]["state"] for d in (old, new)], ["archived", "archived"])
        self.revive("maps-fix")
        return old, new

    def say(self, text, **extra):
        """The starter writes to the returned executor, which answers with a turn; what went to the starter as "a reply to the task"."""
        seen = set(self.st["messages"])
        self.ev("message.received", None, to="maps-fix", text=text, **extra, **by("maps"))
        mid = f"m{self.seq}"
        self.ev("turn.started", "maps-fix", marker=mid, prompt_head=text)
        self.ev("turn.result", "maps-fix", text=f"result: {text}")
        self.ev("turn.stopped", "maps-fix")
        return [m for k, m in self.st["messages"].items() if m["session"] == "maps" and m.get("task") and k not in seen]

    def test_the_named_task_comes_back_and_the_other_stays_archived(self):
        old, new = self.two()
        (back,) = self.say("more on the first", about=old)
        self.assertEqual(back["task"], old)
        self.assertIn("result: more on the first", back["text"])
        self.assertEqual([self.st["delegations"][d]["state"] for d in (old, new)], ["replied", "archived"])
        self.assertTrue(self.st["delegations"][old]["trimmed"])   # the texts in the record are still the beginnings

    def test_a_trimmed_done_task_does_not_come_back(self):
        did = self.executor("maps-fix", text="the first task")
        self.ev("delegation.done", did, **by("hub"))
        self.ev("core.archived", None, ids=[did])
        self.revive("maps-fix")
        self.assertEqual(self.say("more on the first one", about=did), [])
        self.assertEqual(self.st["delegations"][did]["state"], "done")

    def test_without_about_the_latest_comes_back(self):
        old, new = self.two()
        (back,) = self.say("and more")
        self.assertEqual(back["task"], new)
        self.assertEqual([self.st["delegations"][d]["state"] for d in (old, new)], ["archived", "replied"])

    def test_the_hub_gets_no_extra_replied_for_a_continuation(self):
        old, new = self.two()
        hub = len([m for m in self.st["messages"].values() if m["session"] == "hub"])
        self.say("and more")
        self.assertEqual(len([m for m in self.st["messages"].values() if m["session"] == "hub"]), hub)

    def test_a_tool_for_the_revived_one_resumes_with_the_trimmed_note(self):
        old, new = self.two()
        self.say("and more")
        sess = self.st["sessions"]["maps-fix"]
        sess.update(state="idle", new_tools=[{"tool": "mail@kit", "asked": False}])
        reducer._tools_apply(self.st, sess, {"seq": 9000, "ts": 9000, "type": "x"}, [])
        (said,) = [m["text"] for m in self.st["messages"].values() if "Connected" in m["text"]]
        self.assertIn(f"the task text in the record is shortened — the full one: hub task show {new}", said)
        self.assertEqual(self.st["delegations"][old]["state"], "archived")

    def test_the_yes_note_line_is_not_cut_in_half_by_the_trim(self):
        """Review: trimming at 300 characters cut the line "Waiting for the owner's yes…" — its stub went into "continue". The line
        is kept in the yes_note field — trimming removes it from context before cutting; task lines that look like it stay."""
        did = self.executor("maps-fix")
        d = self.st["delegations"][did]
        note = "Waiting for the owner's yes: mail@kit. Do not ask them yourself; once it is connected, the core will write."
        d.update(yes_note=note, context="Y" * 280 + "\n" + note)
        self.ev("core.archived", None, ids=[did])
        self.assertEqual(self.st["delegations"][did]["context"], "Y" * 280)
        old = self.executor("maps-old")
        self.st["delegations"][old]["context"] = "C" * 290 + "\nAwait reply from geo, then go on"   # without yes_note
        self.ev("core.archived", None, ids=[old])
        self.assertEqual(self.st["delegations"][old]["context"], "C" * 290 + "\nAwait rep…")

    def test_revive_keeps_the_raiser_so_the_result_of_a_turn_goes_to_it(self):
        """Handed in, removed, revive, a turn on the starter's word — the result goes to the starter (without the archive and without a manual edit)."""
        did = self.executor("maps-fix")
        self.revive("maps-fix")
        self.assertEqual(self.st["sessions"]["maps-fix"]["raised_by"], "maps")
        (back,) = self.say("more")
        self.assertEqual(back["task"], did)
        self.assertIn("result: more", back["text"])

    def test_revive_does_not_give_a_raiser_to_another_conversation(self):
        """Another conversation under the same name (a different session_id) does not inherit a foreign starter."""
        self.executor("maps-fix")
        self.assertIsNone(self.ev("session.requested", "maps-fix", session_id="s-other", cwd="/m", revive=True,
                                  **by("hub")))
        self.assertIsNone(self.st["sessions"]["maps-fix"].get("raised_by"))


class TaskList(Base):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp(prefix="deleg-archive-cli-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def hub(self, *args, lang=None):
        with open(os.path.join(self.dir, "journal.jsonl"), "w", encoding="utf-8") as f:   # the state as a file
            f.write('{"seq":1,"ts":1,"type":"core.started","entity":null,"data":{}}\n')
        journal.save_state(self.dir, {**self.st, "seq": self.seq})
        env = {**os.environ, "HUB_STATE_DIR": self.dir, "HUB_CLI_WAIT": "0", **({"HUB_LANG": lang} if lang else {})}
        env.pop("HUB_SESSION", None)
        hub = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")
        r = subprocess.run([sys.executable, hub, *args], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        return r.stdout

    def test_task_list_hides_the_archive_and_all_shows_it_signed(self):
        replied = self.executor("maps-r")
        archived = self.executor("maps-a")
        self.ev("core.archived", None, ids=[archived])
        out = self.hub("task", "list")
        self.assertIn(replied, out)
        self.assertNotIn(archived, out)
        self.assertIn("(archived: 1 — hub task list --all)", out)
        line = next(x for x in self.hub("task", "list", "--all").splitlines() if archived in x)
        self.assertIn("archived", line)   # the catalog's mark (cli.task.archived_mark), in English the same word as the state
        # a Russian owner tells the mark from the raw state word
        line = next(x for x in self.hub("task", "list", "--all", lang="ru").splitlines() if archived in x)
        self.assertIn(i18n.tr("ru", "cli.task.archived_mark"), line)
        self.assertNotIn("archived", line)

    def test_task_show_says_it_is_archived(self):
        archived = self.executor("maps-a")
        self.ev("core.archived", None, ids=[archived])
        self.assertIn("(archived: handed in long ago, the executor is removed", self.hub("task", "show", archived))
        self.assertNotIn("(archived", self.hub("task", "show", self.executor("maps-r")))


class FromTheSpool(unittest.TestCase):
    def test_a_forged_archive_from_the_spool_is_refused(self):
        """Only the core writes core.*: a fake core.archived from the inbox gets a spool.refused stub, the delegation stays as it was."""
        state = tempfile.mkdtemp(prefix="deleg-archive-")
        self.addCleanup(shutil.rmtree, state, ignore_errors=True)
        h = Hub(state, types.SimpleNamespace())
        h.execute = lambda action: None
        spool.put(h.spool_dir, "core.archived", None, {"ids": ["d1"]}, src="cli")
        h.ingest()
        kinds = [e["type"] for e in journal.read(h.journal.path)]
        self.assertNotIn("core.archived", kinds)
        self.assertIn("spool.refused", kinds)


class TheDaemon(unittest.TestCase):
    """hubd on the prune pass: what to archive, the task records before the event, then core.pruned as usual."""
    DAY = 86400

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="deleg-archive-hubd-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.now = 1_000_000.0
        self.h = Hub(self.dir, types.SimpleNamespace(), clock=lambda: self.now)
        self.h.execute = lambda action: None
        self.rec("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.rec("session.started", "hub")
        self.rec("session.requested", "maps", cwd="/m", session_id="s-maps", role="agent", **OWNER)
        self.rec("session.started", "maps")

    def rec(self, type, entity=None, **data):
        self.h.record(type, entity, data)

    def tick(self, seconds=0):
        self.now += seconds
        self.h.next_prune = 0
        self.h.prune()

    def kinds(self, kind):
        return [e for e in journal.read(self.h.journal.path) if e["type"] == kind]

    def executor(self, name, context=None):
        self.rec("task.run_requested", name, task=context or f"task {name}", **by("maps"))
        for type, entity, data in executors.events_for(self.h.state, name, self.h.state["runs"][name], cwd="/m",
                                                       worktree=None):
            self.rec(type, entity, **data)
        (d,) = [d for d in self.h.state["delegations"].values() if d["session"] == name]
        self.rec("session.started", name)
        self.rec("turn.started", name, marker=d["message"], prompt_head="task")
        self.rec("turn.result", name, text=f"result {name}")
        self.rec("turn.stopped", name)
        return d["id"]

    def remove(self, name):
        self.rec("session.ended", name, reason="prompt_input_exit")
        self.rec("session.remove_requested", name, **OWNER)
        self.assertEqual(self.h.state["sessions"][name]["state"], "removed")

    def state(self, did):
        return self.h.state["delegations"][did]["state"]

    def test_tasks_of_executors_removed_long_ago_are_archived_once_and_their_messages_pruned(self):
        did = self.executor("maps-fix")
        mid = self.h.state["delegations"][did]["message"]
        self.remove("maps-fix")
        self.tick(delegations.ARCHIVE_AFTER - 1)
        self.assertEqual((self.state(did), self.kinds("core.archived")), ("replied", []))
        self.assertIn(mid, self.h.state["messages"])   # the delegation holds its message
        self.tick(2)
        self.assertEqual(self.state(did), "archived")
        self.assertEqual([e["data"]["ids"] for e in self.kinds("core.archived")], [[did]])
        self.assertNotIn(mid, self.h.state["messages"])   # on the same pass — core.pruned
        self.assertNotIn(mid, reducer.replay(journal.read(self.h.journal.path))["messages"])   # and for readers
        self.tick(3600)
        self.assertEqual(len(self.kinds("core.archived")), 1)   # a repeat writes nothing

    def test_the_task_record_is_on_disk_with_the_full_context_before_the_event(self):
        did = self.executor("maps-fix", context="context " * 100)
        full = self.h.state["delegations"][did]["context"]
        self.remove("maps-fix")
        self.tick(delegations.ARCHIVE_AFTER + 1)
        self.assertEqual(self.state(did), "archived")
        with open(taskrec.path(self.dir, did), encoding="utf-8") as f:
            self.assertIn(full, f.read())

    def test_a_record_with_only_the_owners_words_gets_the_context_verbatim(self):
        did = self.executor("maps-fix")
        d = self.h.state["delegations"][did]
        d["words"] = "the owner's words"
        d["context"] = "table:\n| a  |  b |\n```\n  code  with   spaces\n```"
        taskrec.note(self.dir, self.h.state, did, "core", "a turn line")   # a record exists: the header is made of the owner's words
        with open(taskrec.path(self.dir, did), encoding="utf-8") as f:
            self.assertNotIn(d["context"], f.read())
        self.remove("maps-fix")
        self.tick(delegations.ARCHIVE_AFTER + 1)
        self.assertEqual(self.state(did), "archived")
        with open(taskrec.path(self.dir, did), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("table:\n| a  |  b |\n```\n  code  with   spaces\n```", text)
        self.assertIn("the owner's words", text)
        self.assertIn("a turn line", text.split(taskrec.LOG)[1])   # the turn is intact
        self.assertLess(text.index("code"), text.index(taskrec.LOG))   # the context is in "Task"

    def test_a_task_archived_again_does_not_get_its_cut_text_appended(self):
        """Review: the executor was brought back, the task is "replied" again (trimmed), it was removed again — a stub
        with "…" is not appended to the record: the full text is already in it since the first archive."""
        did = self.executor("maps-fix", context="context " * 100)
        self.remove("maps-fix")
        self.tick(delegations.ARCHIVE_AFTER + 1)
        with open(taskrec.path(self.dir, did), encoding="utf-8") as f:
            first = f.read()
        d = self.h.state["delegations"][did]
        d["state"] = "replied"   # as after _followup of the returned one
        self.tick(3600)
        self.assertEqual(self.state(did), "archived")
        with open(taskrec.path(self.dir, did), encoding="utf-8") as f:
            self.assertEqual(f.read(), first)

    def test_the_record_is_checked_in_one_locked_pass(self):
        """Review: the first pass on a big hub — hundreds of tasks; one record lock per task, not three."""
        ids = [self.executor(f"maps-{i}") for i in range(3)]
        for i in range(3):
            self.remove(f"maps-{i}")
        passes = []
        real = taskrec._locked
        taskrec._locked = lambda state_dir, did: (passes.append(did), real(state_dir, did))[1]
        self.addCleanup(setattr, taskrec, "_locked", real)
        self.tick(delegations.ARCHIVE_AFTER + 1)
        self.assertEqual([self.state(d) for d in ids], ["archived"] * 3)
        self.assertEqual(sorted(passes), sorted(ids))

    def test_a_task_whose_record_cannot_be_written_is_not_archived(self):
        did = self.executor("maps-fix")
        self.remove("maps-fix")
        os.makedirs(os.path.join(self.dir, "tasks"), exist_ok=True)
        os.makedirs(taskrec.path(self.dir, did))   # a folder where the file should be: cannot write
        self.tick(delegations.ARCHIVE_AFTER + 1)
        self.assertEqual(self.state(did), "replied")
        self.assertEqual(self.kinds("core.archived"), [])

    def test_done_tasks_of_executors_removed_long_ago_are_trimmed_once_with_the_record_on_disk(self):
        did = self.executor("maps-fix", context="context " * 100)
        full = self.h.state["delegations"][did]["context"]
        self.rec("delegation.done", did, **by("hub"))
        self.remove("maps-fix")
        self.tick(delegations.ARCHIVE_AFTER - 1)
        self.assertEqual(self.kinds("core.archived"), [])
        self.tick(2)
        d = self.h.state["delegations"][did]
        self.assertEqual((d["state"], d.get("trimmed")), ("done", True))
        self.assertEqual([e["data"]["ids"] for e in self.kinds("core.archived")], [[did]])
        with open(taskrec.path(self.dir, did), encoding="utf-8") as f:
            self.assertIn(full, f.read())
        self.tick(3600)
        self.tick(3600)
        self.assertEqual(len(self.kinds("core.archived")), 1)   # trimmed once: the hourly pass writes nothing more

    def test_live_and_specialists_tasks_stay(self):
        did = self.executor("maps-fix")
        self.h.state["delegations"][did]["state"] = "stopped"   # a live session, the turn was cut off
        self.rec("delegation.created", None, session="maps", context="to the agent", **by("hub"))
        agent = f"d{self.h.journal.seq}"
        self.tick(30 * self.DAY)
        self.assertEqual((self.state(did), self.state(agent)), ("stopped", "working"))
        self.assertEqual(self.kinds("core.archived"), [])

    def test_pruned_then_archived_then_pruned(self):
        """A message held only by an archived delegation is removed by core.pruned after the archive; messages that
        other (live) delegations also hold stay."""
        a = self.executor("maps-a")
        b = self.executor("maps-b")
        self.remove("maps-a")
        self.tick(daemon.PRUNE_AFTER + 1)   # core.pruned: both messages are held by delegations
        self.assertIn(self.h.state["delegations"][a]["message"], self.h.state["messages"])
        self.tick(delegations.ARCHIVE_AFTER)
        self.assertEqual((self.state(a), self.state(b)), ("archived", "replied"))
        self.assertNotIn(self.h.state["delegations"][a]["message"], self.h.state["messages"])
        self.assertIn(self.h.state["delegations"][b]["message"], self.h.state["messages"])


if __name__ == "__main__":
    unittest.main()
