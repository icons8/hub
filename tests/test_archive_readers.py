"""Readers of a compacted archive build the feed and the "Flow" from events alone. A compacted archive keeps one state
snapshot, the first; what the core decided by its rules then and by the snapshots that compact dropped is not there to
fold from, and a turn or a call may last several archives. So: a message the core sent is in the feed (from the facts of
sending and the state at the archive's end), a call that ends archives later gets its result, a turn chain started
archives earlier goes to whoever started it."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import shutil
import tempfile
import unittest

from core import flow, forms, history, journal, reducer, views
from tests.fakes import lang

OWNER = {"channel": "web", "owner": True}
PHOTO = {"path": "/nowhere/photo.png", "size": 0}


def ev(seq, kind, entity=None, data=None, src="host"):
    return {"seq": seq, "ts": 1000.0 + seq, "src": src, "type": kind, "entity": entity, "data": data or {}}


class Archives(unittest.TestCase):
    """Archives as rotation and compact leave them: the first is whole, each next one starts with the rotation snapshot
    that names the one before it, then only its own events."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="readers-")
        os.makedirs(os.path.join(self.dir, journal.ARCHIVE))
        self.events, self.names = [], []
        flow._archive.clear()
        flow._current.clear()
        self.addCleanup(flow._archive.clear)
        self.addCleanup(flow._current.clear)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def archive(self, events, state=None):
        """The next archive of events; state — the core's state at its start (by default the fold of all before)."""
        first = events[0]["seq"]
        lines = []
        if self.names:
            st = state if state is not None else {k: v for k, v in reducer.replay(self.events).items()
                                                  if not k.startswith("_")}
            lines.append({"seq": first - 1, "ts": 1000.0 + first - 1, "src": "core", "type": "core.snapshot", "entity": None,
                          "data": {"why": "rotate", "archive": self.names[-1], "compact": True, "state": st}})
        name = f"journal-{lines[0]['seq'] if lines else first}-{events[-1]['seq']}.jsonl"
        with open(os.path.join(self.dir, journal.ARCHIVE, name), "w", encoding="utf-8") as f:
            for e in lines + events:
                f.write(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.events += events
        self.names.append(name)

    def current(self, carry, state):
        """The current journal as rotation starts it: the snapshot before the carry, the last events of the newest
        archive, the snapshot of now naming that archive (state)."""
        last = self.events[-1]["seq"]
        snap = lambda seq, why, st: {"seq": seq, "ts": 1000.0 + seq, "src": "core", "type": "core.snapshot",  # noqa: E731
                                     "entity": None, "data": {"why": why, "archive": self.names[-1], "compact": True,
                                                              "state": st}}
        with open(os.path.join(self.dir, "journal.jsonl"), "w", encoding="utf-8") as f:
            for e in [snap(carry[0]["seq"] - 1, "rotate-start", {})] + carry + [snap(last + 1, "rotate", state)]:
                f.write(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n")

    def feed(self, name):
        h = history.History(self.dir)
        h.build_all()
        return h.older(name, 10 ** 9, 10 ** 6, [])["items"]

    def start(self, *names):
        out = []
        for i, name in enumerate(names):
            out += [ev(2 * i + 1, "session.requested", name, {"session_id": f"s-{name}", "cwd": "/w", **OWNER}, "core"),
                    ev(2 * i + 2, "session.started", name)]
        return out

    def filler(self, first, last):
        return [ev(seq, "account.limits", None, {"status": "allowed"}) for seq in range(first, last + 1)]

    def test_a_message_the_core_sent_is_in_the_feed_though_the_fold_does_not_create_it(self):
        # the core reminded the session of a task (its delegation came with a snapshot compact dropped): the fold by
        # the current code has no such task and creates nothing, yet the core typed the message and the session took it
        self.archive(self.start("w") + self.filler(3, 10))
        head = f"{lang.head('m12', kind='core')} Task d99 is open and you are free for 30 min."
        self.archive([ev(12, "timer.fired", "d99", {"attempt": None, "n": None}, "core"),
                      ev(13, "host.done", "w", {"do": "type", "message": "m12", "attempt": 1, "result": "sent"}, "core"),
                      ev(14, "turn.started", "w", {"marker": "m12", "prompt_head": head}, "hook"),
                      ev(15, "turn.result", "w", {"text": "did it"}),
                      ev(16, "timer.fired", "d98", {"attempt": None, "n": None}, "core"),
                      ev(17, "turn.started", "w", {"marker": "m16",
                                                   "prompt_head": (f"{lang.head('m16', kind='core')} The second "
                                                                   + "x" * 200)[:200]}, "hook"),
                      ev(18, "turn.result", "w", {"text": "and this"})])
        full = "Task d99 is open and you are free for 30 min. Done: say so in the result."
        state = {k: v for k, v in reducer.replay(self.events).items() if not k.startswith("_")}
        state["messages"]["m12"] = {"id": "m12", "to": "w", "session": "w", "text": full, "origin": lang.origin("core"),
                                    "state": "delivered", "created": 1012.0}
        self.current(self.events[-2:], state)   # the newest archive: its end is in the current journal, past the carry
        rows = {it["id"]: it for it in self.feed("w") if it.get("kind") == "in"}
        self.assertEqual(rows["m12"]["text"], full)   # the text the session got: from the state at the archive's end
        self.assertEqual((rows["m12"]["seq"], rows["m12"]["sender"]), (12, ":core"))
        self.assertTrue(rows["m16"]["text"].startswith("The second x"))   # gone from that state: from the turn's start
        self.assertTrue(rows["m16"]["text"].endswith("…"))

    def test_a_message_sent_archives_after_it_was_made_keeps_its_row(self):
        # the owner wrote while the session was busy for long: his row is in the archive his message came in; the one
        # it was typed in adds no copy of it (a copy from the newer archive would replace his row)
        self.archive(self.start("w") + [
            ev(3, "message.received", None, {"to": "w", "text": "do it when you are free", **OWNER}, "web")]
            + self.filler(4, 10))
        self.archive(self.filler(12, 20))
        self.archive([ev(22, "host.done", "w", {"do": "type", "message": "m3", "attempt": 1, "result": "sent"}, "core"),
                      ev(23, "turn.started", "w", {"marker": "m3", "prompt_head": "[hub:m3] do it when you are free"},
                         "hook")] + self.filler(24, 26))
        self.archive(self.filler(28, 30))
        rows = [it for it in self.feed("w") if it.get("id") == "m3"]
        self.assertEqual([(it["seq"], it["text"], it.get("channel"), it["from"]) for it in rows],
                         [(3, "do it when you are free", "web", {"kind": "owner"})])

    def test_a_message_sent_in_a_carry_whose_archive_is_gone_is_in_the_feed(self):
        # a whole archive starts with the rotation carry; the archive the carry came from was removed, so the carry is
        # the only copy of its events (compact keeps it): a message the core sent in it has its row
        head = f"{lang.head('m5', kind='core')} Task d1 is open and you are free for 30 min."
        events = [{"seq": 2, "ts": 1002.0, "src": "core", "type": "core.snapshot", "entity": None,
                   "data": {"why": "rotate-start", "archive": "journal-1-9.jsonl", "state": {}}}] \
            + [ev(3, "session.requested", "w", {"session_id": "s-w", "cwd": "/w", **OWNER}, "core"),
                                     ev(4, "session.started", "w"),
                                     ev(5, "timer.fired", "d1", {"attempt": None, "n": None}, "core"),
                                     ev(6, "host.done", "w", {"do": "type", "message": "m5", "result": "sent"}, "core"),
                                     ev(7, "turn.started", "w", {"marker": "m5", "prompt_head": head}, "hook")]
        rotate = {"seq": 10, "ts": 1010.0, "src": "core", "type": "core.snapshot", "entity": None,
                  "data": {"why": "rotate", "archive": "journal-1-9.jsonl", "state": {}}}
        with open(os.path.join(self.dir, journal.ARCHIVE, "journal-2-12.jsonl"), "w", encoding="utf-8") as f:
            for e in events + [rotate] + self.filler(11, 12):
                f.write(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.assertIn("m5", [it.get("id") for it in self.feed("w")])

    def test_a_message_that_came_in_keeps_its_channel_and_author_when_the_fold_does_not_create_it(self):
        # the fold knows no session "w" (the snapshot that brought it is dropped): it routes the message that came in
        # for "w" nowhere, yet the core typed it to "w" — its row is the one that came in, with its channel and files
        self.archive(self.start("x") + self.filler(3, 10))
        self.archive([ev(12, "message.received", None, {"to": "w", "text": "look at the photo", "channel": "telegram",
                                                         "owner": True, "attachments": [PHOTO]}, "telegram"),
                      ev(13, "host.done", "w", {"do": "type", "message": "m12", "attempt": 1, "result": "sent"}, "core"),
                      ev(14, "turn.started", "w", {"marker": "m12", "prompt_head": "[hub:m12] look at the photo"}, "hook"),
                      ev(15, "turn.result", "w", {"text": "I see"})])
        state = {k: v for k, v in reducer.replay(self.events).items() if not k.startswith("_")}
        state["messages"]["m12"] = {"id": "m12", "to": "w", "session": "w", "text": "look at the photo", "from_owner": True,
                                    "attachments": [PHOTO], "state": "delivered", "created": 1012.0}
        self.current(self.events[-1:], state)
        rows = [it for it in self.feed("w") if it.get("id") == "m12"]
        self.assertEqual([(it["seq"], it["text"], it.get("channel"), it.get("attachments"), it["from"]) for it in rows],
                         [(12, "look at the photo", "telegram", [PHOTO], {"kind": "owner"})])

    def test_the_owner_s_words_the_fold_does_not_create_are_not_written_on_his_behalf(self):
        # his own words (the state names him the author): his row, not the core's note in his name
        self.archive(self.start("x") + self.filler(3, 10))
        self.archive([ev(12, "message.received", None, {"to": "w", "text": "yes, that one", **OWNER}, "web"),
                      ev(13, "host.done", "w", {"do": "type", "message": "m12", "attempt": 1, "result": "sent"}, "core"),
                      ev(14, "turn.started", "w", {"marker": "m12", "prompt_head": "[hub:m12] yes, that one"}, "hook")])
        state = {k: v for k, v in reducer.replay(self.events).items() if not k.startswith("_")}
        state["messages"]["m12"] = {"id": "m12", "to": "w", "session": "w", "text": "yes, that one", "from": {"kind": "owner"},
                                    "from_owner": True, "state": "delivered", "created": 1012.0}
        self.current(self.events[-1:], state)
        rows = [it for it in self.feed("w") if it.get("id") == "m12"]
        self.assertEqual([(it["text"], it["from"], it.get("sender"), it.get("on_behalf")) for it in rows],
                         [("yes, that one", {"kind": "owner"}, None, None)])

    def test_the_owner_s_words_under_a_tailed_id_the_fold_does_not_create_stay_his_row(self):
        # the message.received made his words under m12x1 (the id was taken): his row, as before, not the core's
        self.archive(self.start("x") + self.filler(3, 10))
        self.archive([ev(12, "message.received", None, {"to": "w", "text": "yes, that one", **OWNER}, "web"),
                      ev(13, "host.done", "w", {"do": "type", "message": "m12x1", "attempt": 1, "result": "sent"}, "core"),
                      ev(14, "turn.started", "w", {"marker": "m12x1", "prompt_head": "[hub:m12x1] yes, that one"}, "hook")])
        state = {k: v for k, v in reducer.replay(self.events).items() if not k.startswith("_")}
        state["messages"]["m12x1"] = {"id": "m12x1", "to": "w", "session": "w", "text": "yes, that one",
                                      "from": {"kind": "owner"}, "from_owner": True, "state": "delivered", "created": 1012.0}
        self.current(self.events[-1:], state)
        rows = [it for it in self.feed("w") if it.get("id") == "m12x1"]
        self.assertEqual([(it["text"], it["from"], it.get("sender"), it.get("on_behalf")) for it in rows],
                         [("yes, that one", {"kind": "owner"}, None, None)])

    def retold_rows(self, **mark):
        # the owner answered a card; the fold knows no session "w" and creates nothing, yet the core typed its retelling
        # "The owner's answer to question …" to "w"
        self.archive(self.start("x") + self.filler(3, 10))
        self.archive([ev(12, "permission.answered", "w", {"request_id": "r1", "behavior": "allow",
                                                          "answers": {"Which?": "B"}, **OWNER}, "web"),
                      ev(13, "host.done", "w", {"do": "type", "message": "m12", "attempt": 1, "result": "sent"}, "core"),
                      ev(14, "turn.started", "w", {"marker": "m12", "prompt_head": "[hub:m12 from user] The owner's answer"}, "hook")])
        state = {k: v for k, v in reducer.replay(self.events).items() if not k.startswith("_")}
        state["messages"]["m12"] = {"id": "m12", "to": "w", "session": "w", "text": self.RETOLD, "origin": forms.ORIGIN_OWNER,
                                    "from": {"kind": "owner"}, "from_owner": True, "answers_question": "q5",
                                    "channel": "web", "state": "delivered", "created": 1012.0, **mark}
        self.current(self.events[-1:], state)
        return [(it["text"], it["from"], it["sender"], it.get("on_behalf"), it.get("answer_to"))
                for it in self.feed("w") if it.get("id") == "m12"]

    RETOLD = "The owner's answer to question q5:\n— Which?: B"

    def test_the_core_s_retelling_of_an_answer_the_fold_does_not_create_is_the_core_s_row(self):
        # the reducer marked it as written on his behalf: the core's row — not his own words
        self.assertEqual(self.retold_rows(on_behalf="owner"),
                         [(self.RETOLD, {"kind": "core"}, views.CORE_SENDER, "owner", "q5")])

    def test_a_retelling_written_before_the_mark_keeps_its_old_row_not_the_owner_s_words(self):
        # written before the reducer marked such notes: the row it had in the live feed (with a sender) — not the owner's
        # own words (no sender), which a client draws as his bubble
        self.assertEqual(self.retold_rows(),
                         [(self.RETOLD, {"kind": "owner"}, views.OWNER_SENDER, None, "q5")])

    def test_the_retelling_of_a_typed_answer_written_before_the_mark_is_not_the_owner_s_words(self):
        # he answered an open form by typing: his words are m12, the core's retelling m12x1 — made by the same
        # message.received, unmarked before the update; the fold creates neither. The retelling is not his bubble
        self.archive(self.start("x") + self.filler(3, 10))
        self.archive([ev(12, "message.received", None, {"to": "w", "text": "B", **OWNER}, "web"),
                      ev(13, "host.done", "w", {"do": "type", "message": "m12x1", "attempt": 1, "result": "sent"}, "core"),
                      ev(14, "turn.started", "w", {"marker": "m12x1", "prompt_head": "[hub:m12x1 from user] The owner's"},
                         "hook")])
        state = {k: v for k, v in reducer.replay(self.events).items() if not k.startswith("_")}
        state["messages"]["m12x1"] = {"id": "m12x1", "to": "w", "session": "w", "text": self.RETOLD,
                                      "origin": forms.ORIGIN_OWNER, "from": {"kind": "owner"}, "from_owner": True,
                                      "answers_question": "q5", "channel": "web", "state": "delivered", "created": 1012.0}
        self.current(self.events[-1:], state)
        rows = [it for it in self.feed("w") if it.get("id") == "m12x1"]
        self.assertEqual([(it["from"], it["sender"], it.get("answer_to")) for it in rows],
                         [({"kind": "owner"}, views.OWNER_SENDER, "q5")])

    def test_a_session_saying_it_is_the_owner_does_not_get_his_row(self):
        # the message is gone from the core's state at the end: its author is decided by the core's rule, and a session
        # is not one of the owner's channels whatever its flag says
        self.archive(self.start("x") + self.filler(3, 10))
        self.archive([ev(12, "message.received", None, {"to": "w", "text": "do", "channel": "session",
                                                         "sender": "x", "owner": True}, "session"),
                      ev(13, "host.done", "w", {"do": "type", "message": "m12", "attempt": 1, "result": "sent"}, "core"),
                      ev(14, "turn.started", "w", {"marker": "m12", "prompt_head": "[hub:m12] do"}, "hook")])
        rows = [it for it in self.feed("w") if it.get("id") == "m12"]
        self.assertEqual([(it["channel"], it["from"]) for it in rows], [("session", {"kind": "session", "name": "x"})])

    def test_a_message_that_came_in_an_archive_before_keeps_its_channel(self):
        # it came in while the session was busy and was typed one archive later: its row is built from the event that
        # created it all the same
        self.archive(self.start("x") + self.filler(3, 8)
                     + [ev(9, "message.received", None, {"to": "w", "text": "look", "channel": "telegram",
                                                          "owner": True}, "telegram")])
        self.archive([ev(11, "host.done", "w", {"do": "type", "message": "m9", "attempt": 1, "result": "sent"}, "core"),
                      ev(12, "turn.started", "w", {"marker": "m9", "prompt_head": "[hub:m9] look"}, "hook")])
        rows = [it for it in self.feed("w") if it.get("id") == "m9"]
        self.assertEqual([(it["seq"], it["text"], it["channel"], it["from"]) for it in rows],
                         [(9, "look", "telegram", {"kind": "owner"})])

    def test_a_pressed_button_that_came_in_an_archive_before_keeps_its_caption(self):
        # a page button that sets a session a task (PR #528): the row names the button, not the whole task, in the
        # archive too when the press was typed one archive later
        self.archive(self.start("x") + self.filler(3, 8)
                     + [ev(9, "message.received", None, {"to": "w", "text": "The owner pressed Fix. Steps: a long task",
                                                          "action": True, "button": "Fix", **OWNER}, "web")])
        self.archive([ev(11, "host.done", "w", {"do": "type", "message": "m9", "attempt": 1, "result": "sent"}, "core"),
                      ev(12, "turn.started", "w", {"marker": "m9", "prompt_head": "[hub:m9] The owner pressed Fix"}, "hook")])
        state = {k: v for k, v in reducer.replay(self.events).items() if not k.startswith("_")}
        state["messages"]["m9"] = {"id": "m9", "to": "w", "session": "w", "text": "The owner pressed Fix. Steps: a long task",
                                   "from_owner": True, "action": True, "state": "delivered", "created": 1009.0}
        self.current(self.events[-1:], state)
        rows = [it for it in self.feed("w") if it.get("id") == "m9"]
        self.assertEqual([(it["text"], it.get("label")) for it in rows],
                         [("The owner pressed Fix. Steps: a long task", "you pressed \u201cFix\u201d")])

    def test_a_call_gets_its_result_two_archives_later(self):
        # a command for hours: the session is in its turn through a whole archive, the result comes in the one after
        self.archive(self.start("w") + [
            ev(3, "message.received", None, {"to": "w", "text": "collect", **OWNER}, "web"),
            ev(4, "turn.started", "w", {"marker": "m3", "prompt_head": "[hub:m3] collect"}, "hook"),
            ev(5, "turn.tool", "w", {"id": "t1", "name": "Bash", "input": {"command": "make all"}})] + self.filler(6, 10))
        self.archive(self.filler(12, 20))
        self.archive([ev(22, "turn.tool_result", "w", {"id": "t1", "text": "collected"}),
                      ev(23, "turn.result", "w", {"text": "done"})] + self.filler(24, 26))
        self.archive(self.filler(28, 30))
        call = next(it for it in self.feed("w") if it.get("kind") == "tool")
        self.assertEqual((call["status"], call["result"]), ("done", "collected"))

    def test_an_empty_archive_before_does_not_break_the_parse(self):
        # the archive before is empty (truncated, only a torn line): no lead-in, the archive's own rows are parsed
        self.archive(self.start("w"))
        self.archive([ev(4, "message.received", None, {"to": "w", "text": "check", **OWNER}, "web"),
                      ev(5, "turn.started", "w", {"marker": "m4", "prompt_head": "[hub:m4] check"}, "hook"),
                      ev(6, "turn.result", "w", {"text": "checked"})])
        open(os.path.join(self.dir, journal.ARCHIVE, self.names[0]), "w").close()
        line = history.parse(os.path.join(self.dir, journal.ARCHIVE, self.names[1]))["w"]
        self.assertIn("check", [it.get("text") for it in line])

    def test_a_result_after_the_turn_ended_goes_on_the_call(self):
        # the reader marked the call stopped (the turn ended, or the state it folds saw the session asleep); the result is a fact
        r = views.Reader()
        r.feed_all(self.start("w") + [
            ev(3, "message.received", None, {"to": "w", "text": "check", **OWNER}, "web"),
            ev(4, "turn.started", "w", {"marker": "m3", "prompt_head": "[hub:m3] check"}, "hook"),
            ev(5, "turn.tool", "w", {"id": "t1", "name": "Agent", "input": {"description": "review"}}),
            ev(6, "turn.result", "w", {"text": "launched"}),
            ev(7, "turn.tool_result", "w", {"id": "t1", "text": "agent launched"})])
        call = next(it for it in r.timelines["w"] if it.get("kind") == "tool")
        self.assertEqual((call["status"], call["result"]), ("done", "agent launched"))

    def test_a_repeated_rename_of_a_busy_session_keeps_the_feed_moved_first(self):
        # a busy session is renamed when its turn ends (rename_to), the reader moves its feed at once; the turn goes on
        # writing under the old name, a repeated rename moved those few rows over the moved feed and it was lost
        r = views.Reader()
        rename = {"to": "new", **OWNER}
        r.feed_all(self.start("old") + [
            ev(3, "message.received", None, {"to": "old", "text": "collect", **OWNER}, "web"),
            ev(4, "turn.started", "old", {"marker": "m3", "prompt_head": "[hub:m3] collect"}, "hook"),
            ev(5, "turn.tool", "old", {"id": "t1", "name": "Bash", "input": {"command": "make"}}),
            ev(6, "session.renamed", "old", rename, "web"),
            ev(7, "session.said", "old", {"text": "collecting"}),
            ev(8, "session.renamed", "old", rename, "web")])
        self.assertEqual(r.state["sessions"]["old"]["rename_to"], "new")
        self.assertEqual([it["seq"] for it in r.timelines["new"] if it["seq"] in (5, 7)], [5, 7])

    def test_a_rename_to_the_name_of_a_removed_session_does_not_take_its_feed(self):
        # a name is free again once its session is removed: the renamed one gets only its own rows under it
        r = views.Reader()
        r.feed_all(self.start("w", "gone") + [
            ev(5, "session.said", "gone", {"text": "a line of the removed one"}),
            ev(6, "session.parked", "gone", {"channel": "core", "why": "restart"}, "core"),
            ev(7, "session.remove_requested", "gone", OWNER, "web"),
            ev(8, "session.renamed", "w", {"to": "gone", **OWNER}, "web")])
        self.assertEqual(r.state["sessions"]["gone"]["session_id"], "s-w")
        self.assertNotIn(5, [it["seq"] for it in r.timelines["gone"]])

    def test_a_turn_chain_started_two_archives_earlier_goes_to_whoever_started_it(self):
        # the session took a task from boss; archives later Claude Code woke it itself (its background ended): the
        # turn without a label continues the chain, its result goes to boss
        self.archive(self.start("w", "boss") + [
            ev(5, "turn.started", "w", {"marker": "m4", "prompt_head": f"{lang.head('m4', 'boss')} collect the release"}, "hook"),
            ev(6, "turn.result", "w", {"text": "launched the build"})] + self.filler(7, 10))
        self.archive(self.filler(12, 20))
        self.archive([ev(22, "turn.started", "w", {"marker": None, "prompt_head": "<task-notification>"}, "hook"),
                      ev(23, "turn.result", "w", {"text": "the release is collected"})] + self.filler(24, 26))
        got = [(s["seq"], s["from"], s["to"]) for s in flow.history(self.dir, 0) if s["kind"] == "result"]
        self.assertIn((23, "w", "boss"), got)
        self.assertIn((6, "w", "boss"), got)


if __name__ == "__main__":
    unittest.main()
