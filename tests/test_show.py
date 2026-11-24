"""`hub show` — what a session did and wrote, from the core journal (owner decision 2026-09-30: a session wanted to see
another session's result and there was no way). `hub show <session>` — state, background and the
latest messages, replies and steps; `hub show --grep <text>` — who wrote about it and when."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest

from core import i18n, journal, views

HUB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")

EVENTS = [
    ("session.requested", "site-build", {"session_id": "s", "cwd": "/w"}),
    ("session.started", "site-build", {"session_id": "s"}),
    ("message.received", None, {"to": "site-build", "text": "start the site build", "channel": "web", "owner": True}),
    ("turn.started", "site-build", {"marker": "m3"}),
    ("turn.tool", "site-build", {"id": "t1", "name": "Bash", "input": {"command": "kubectl create job gen"}}),
    ("turn.tool_result", "site-build", {"id": "t1", "text": "job created"}),
    ("session.said", "site-build", {"text": "Generation done: 17 files per language, sync-pages passed."}),
    ("turn.result", "site-build", {"text": "Generation done: 17 files per language, sync-pages passed."}),
    ("turn.stopped", "site-build", {}),
    ("session.requested", "other", {"session_id": "o", "cwd": "/w"}),
    ("session.started", "other", {"session_id": "o"}),
    ("session.said", "other", {"text": "nothing about the build"}),
]


class Show(unittest.TestCase):
    def reader(self):
        r = views.Reader()
        for i, (t, e, d) in enumerate(EVENTS, 1):
            r.feed({"seq": i, "ts": 1790740000 + i * 60, "src": "t", "type": t, "entity": e, "data": d})
        return r

    def test_show_session(self):
        text = "\n".join(views.show(self.reader(), "site-build", tz="UTC"))
        self.assertIn("site-build — idle", text)
        self.assertIn(i18n.t("cli.show.owner") + ": start the site build", text)   # the owner's terminal
        self.assertIn("Bash: kubectl create job gen", text)
        self.assertIn("Generation done: 17 files per language", text)
        self.assertEqual(views.show(self.reader(), "nope"), [])

    def test_grep_across_sessions(self):
        hits = views.grep(self.reader(), "build", tz="UTC")
        self.assertTrue(any(h.startswith("site-build") and "start the site build" in h for h in hits))
        self.assertTrue(any(h.startswith("other") for h in hits))
        self.assertEqual(views.grep(self.reader(), "no such thing"), [])

    def test_cli(self):
        state = tempfile.mkdtemp(prefix="show-")
        j = journal.Journal(os.path.join(state, "journal.jsonl"))
        for t, e, d in EVENTS:
            j.append(t, e, d)
        env = {**os.environ, "HUB_STATE_DIR": state, "PYTHONUTF8": "1"}   # Windows: not the console code page
        r = subprocess.run([sys.executable, HUB, "show", "site-build"], env=env, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Generation done", r.stdout)
        r = subprocess.run([sys.executable, HUB, "show", "--grep", "sync-pages"], env=env, capture_output=True,
                           text=True, encoding="utf-8")
        self.assertIn("site-build", r.stdout)


LONG = "First line of the owner's words. " + "A very long thought about the build. " * 30 + "\nSecond line with the tail-ANCHOR."


class Full(unittest.TestCase):
    """--grep and the session view cut a message to 300 characters. `hub show --grep … --full` gives it whole;
    `hub show mN|qN|dN` gives one journal record: who, to whom, when, the whole text."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="show-full-")
        self.j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        self.j.append("session.requested", "site-build", {"session_id": "s", "cwd": "/w"})
        self.j.append("session.started", "site-build", {"session_id": "s"})
        self.m = self.j.append("message.received", None, {"to": "site-build", "text": LONG, "channel": "web",
                                                          "owner": True, "quote": "quote-" + "z" * 400})["seq"]
        self.s = self.j.append("message.received", None, {"to": "site-build", "text": "short one from a session",
                                                          "channel": "session", "owner": False,
                                                          "sender": "hub-dev"})["seq"]
        self.q = self.j.append("permission.requested", "site-build", {
            "request_id": "r1", "tool": "AskUserQuestion", "interactive": True,
            "input": {"questions": [{"question": "Delete the old folder?" + " detail" * 40, "header": "Duplicate",
                                     "options": [{"label": "Delete", "description": "the new one stays"},
                                                 {"label": "Keep", "description": "touch nothing"}]}]}})["seq"]
        self.j.append("permission.answered", "site-build", {"request_id": "r1", "behavior": "allow", "channel": "web",
                                                            "owner": True, "answers": {"Delete the old folder?": "Keep"}})
        self.d = self.j.append("delegation.created", None, {"session": "site-build", "sender": "hub-dev",
                                                            "context": LONG})["seq"]
        self.env = {**os.environ, "HUB_STATE_DIR": self.state, "PYTHONUTF8": "1"}

    def hub(self, *args):
        r = subprocess.run([sys.executable, HUB, *args], env=self.env, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_grep_is_cut_by_default_and_whole_with_full(self):
        out = self.hub("show", "--grep", "very long")
        self.assertNotIn("ANCHOR", out)
        self.assertIn("ANCHOR", self.hub("show", "--grep", "very long", "--full"))
        self.assertIn("ANCHOR", self.hub("show", "--full", "--grep", "very long"))

    def test_grep_full_keeps_the_lines_of_the_text(self):
        hits = views.grep(self.reader(), "very long", full=True, tz="UTC")
        hit = next(h for h in hits if "owner:" in h)
        self.assertEqual(len(hit.split("\n")), 2)
        self.assertTrue(hit.split("\n")[1].strip().startswith("Second line"))

    def test_session_view_full(self):
        self.assertNotIn("ANCHOR", self.hub("show", "site-build"))
        self.assertIn("ANCHOR", self.hub("show", "site-build", "--full"))

    def reader(self):
        from core import tail
        return tail.cached(self.state)

    def test_message_made_by_the_core(self):
        """A task creates message mN with delegation.created, not message.received; `show mN` still finds it."""
        out = self.hub("show", f"m{self.d}")
        self.assertIn(f"m{self.d}", out)
        self.assertIn("hub-dev", out)
        self.assertIn("site-build", out)
        self.assertIn("ANCHOR", out)

    def test_message_whole(self):
        out = self.hub("show", f"m{self.m}")
        self.assertIn(f"m{self.m}", out)
        self.assertIn("site-build", out)
        self.assertIn("ANCHOR", out)
        self.assertIn("z" * 400, out)
        self.assertIn(LONG.split("\n")[0], out)
        out = self.hub("show", f"m{self.s}")
        self.assertIn("hub-dev", out)
        self.assertIn("site-build", out)
        self.assertIn("short one from a session", out)

    def test_question_with_the_answer(self):
        out = self.hub("show", f"q{self.q}")
        self.assertIn("Delete the old folder?" + " detail" * 40, out)
        self.assertIn("Delete — the new one stays", out)
        self.assertIn("Keep", out.split("Delete — the new one stays")[1])
        self.assertIn("site-build", out)

    def test_delegation_whole(self):
        out = self.hub("show", f"d{self.d}")
        self.assertIn("hub-dev", out)
        self.assertIn("site-build", out)
        self.assertIn("ANCHOR", out)

    def test_an_old_record_is_read_from_the_archive(self):
        journal.rotate(self.state, {"sessions": {}}, keep=1, per_session=0)   # the new journal keeps only the tail
        with open(os.path.join(self.state, "journal.jsonl"), encoding="utf-8") as f:
            self.assertNotIn(f'"seq":{self.m},', f.read())
        self.assertIn("ANCHOR", self.hub("show", f"m{self.m}"))
        self.assertIn("Keep", self.hub("show", f"q{self.q}"))

    def test_a_rejected_record_is_not_shown(self):
        self.j.append("core.rejected", None, {"of": self.s, "why": "test"})   # hubd writes it on the next line
        with open(os.path.join(self.state, "journal.jsonl"), encoding="utf-8") as f:
            lines = f.read().splitlines()
        moved = [x for x in lines if x.startswith(f'{{"seq":{self.s},')] + [lines[-1]]
        rest = [x for x in lines[:-1] if not x.startswith(f'{{"seq":{self.s},')]
        at = next(i for i, x in enumerate(rest) if json.loads(x)["seq"] > self.s)
        with open(os.path.join(self.state, "journal.jsonl"), "w", encoding="utf-8") as f:
            f.write("\n".join(rest[:at] + moved + rest[at:]) + "\n")
        r = subprocess.run([sys.executable, HUB, "show", f"m{self.s}"], env=self.env, capture_output=True, text=True,
                           encoding="utf-8")
        self.assertNotIn("short one from a session", r.stdout)

    def test_a_missing_record_says_so(self):
        for ref in ("m999999", f"q{self.m}"):   # the second: the number exists but is not a question
            r = subprocess.run([sys.executable, HUB, "show", ref], env=self.env, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(r.returncode, 1)
            self.assertIn(ref, r.stderr)
            self.assertNotIn("Traceback", r.stderr)

    def test_a_session_named_like_a_record_wins(self):
        self.j.append("session.requested", "m1", {"session_id": "x", "cwd": "/w"})
        self.assertIn("m1 —", self.hub("show", "m1"))


class CardState(unittest.TestCase):
    """`hub show qN` says where the card stands: open, answered or closed without an answer (the owner's x, withdrawn,
    stale, the session gone). Sessions read it to settle a card they hold: a card the owner closed with the x used to end
    in "no answer yet" like an open one."""

    Q = {"questions": [{"question": "Rotate the key?", "options": [{"label": "Yes"}, {"label": "No"}]}]}
    OWNER = {"channel": "web", "owner": True}

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="show-card-")
        self.j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        self.j.append("session.requested", "maps", {"session_id": "s", "cwd": "/home/x/maps"})
        self.j.append("session.started", "maps", {"session_id": "s"})
        self.env = {**os.environ, "HUB_STATE_DIR": self.state, "PYTHONUTF8": "1"}

    def ask(self, rid, name="maps"):
        return self.j.append("permission.requested", name, {"request_id": rid, "tool": "AskUserQuestion",
                                                            "interactive": True, "input": self.Q})["seq"]

    def show(self, seq, now=None):
        from core import tail
        return views.entry(self.state, f"q{seq}", tail.cached(self.state), tz="UTC", now=now)

    def test_an_open_card(self):
        out = self.show(self.ask("r1"))
        self.assertEqual(out[1], "state: open — waits for the owner's answer")
        self.assertEqual(out[-1], out[1])
        self.assertNotIn("no answer yet", "\n".join(out))

    def test_an_answered_card(self):
        seq = self.ask("r1")
        self.j.append("permission.answered", "maps", {"request_id": "r1", "behavior": "allow", "answers": {
            "Rotate the key?": "No"}, **self.OWNER})
        out = self.show(seq)
        self.assertEqual(out[1], "state: answered")
        self.assertRegex(out[-1], r"^answer \(.*\): Rotate the key\? → No$")

    def test_a_card_closed_with_the_x(self):
        seq = self.ask("r1")
        self.j.append("question.dismissed", "maps", {"request_id": "r1", **self.OWNER})
        out = self.show(seq)
        self.assertRegex(out[1], r"^state: closed without an answer — the owner closed the card \(.*\)$")
        self.assertEqual(out[-1], out[1])
        self.assertNotIn("no answer yet", "\n".join(out))

    def test_a_withdrawn_card(self):
        seq = self.ask("r1")
        self.j.append("question.withdrawn", "maps", {"id": f"q{seq}", "why": "same as q5", "channel": "session",
                                                     "owner": False, "sender": "maps"})
        out = self.show(seq)
        self.assertRegex(out[1], r"^state: closed without an answer — withdrawn by maps \(.*\): same as q5$")
        self.assertEqual(out[-1], out[1])

    def test_a_stale_card(self):
        from core import owner
        out = self.show(self.ask("r1"), now=time.time() + owner.ASK_TTL + 60)
        self.assertEqual(out[1], "state: closed without an answer — stale: no answer for over 24 h")
        self.assertEqual(out[-1], out[1])

    def test_a_card_of_a_removed_session(self):
        seq = self.ask("r1")
        self.j.append("session.ended", "maps", {})   # it fell asleep: only a sleeping one is removed
        self.j.append("session.remove_requested", "maps", {"channel": "cli", "owner": True})
        out = self.show(seq)
        self.assertEqual(out[1], "state: closed without an answer — the session is gone")
        self.assertEqual(out[-1], out[1])

    def test_a_card_answered_in_words(self):
        """The owner's words to the session close the one open card with no permission.answered."""
        seq = self.ask("r1")
        self.j.append("message.received", None, {"to": "maps", "text": "yes, rotate it", "channel": "web",
                                                 "owner": True})
        out = self.show(seq)
        self.assertEqual(out[1], "state: answered")
        self.assertIn("yes, rotate it", out[-1])

    def test_the_cli_prints_the_state_after_the_first_line(self):
        seq = self.ask("r1")
        self.j.append("question.dismissed", "maps", {"request_id": "r1", **self.OWNER})
        r = subprocess.run([sys.executable, HUB, "show", f"q{seq}"], env=self.env, capture_output=True, text=True,
                           encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertTrue(lines[0].startswith(f"q{seq} ·"))
        self.assertTrue(lines[1].startswith("state: closed without an answer"))

    def test_a_refused_dismissal_leaves_the_card_open(self):
        seq = self.ask("r1")
        self.j.append("question.dismissed", "maps", {"request_id": "r1", "channel": "session", "owner": False,
                                                     "sender": "maps"})
        self.j.append("core.rejected", None, {"of": seq + 1, "why": "test"})
        self.assertEqual(self.show(seq)[1], "state: open — waits for the owner's answer")

    def test_a_tool_permission_card_says_its_state_and_answer(self):
        seq = self.j.append("permission.requested", "maps", {"request_id": "p1", "tool": "Bash", "interactive": False,
                                                             "input": {"command": "ls /home/x/maps"}})["seq"]
        self.assertEqual(self.show(seq)[1], "state: open — waits for the owner's answer")
        self.j.append("permission.answered", "maps", {"request_id": "p1", "behavior": "deny", **self.OWNER})
        out = self.show(seq)
        self.assertEqual(out[1], "state: answered")
        self.assertRegex(out[-1], r"^answer \(.*\): deny$")

    def ask_in_dialog(self, rid):
        """A form the session holds the turn for (not a card): the owner's words to the session answer it."""
        return self.j.append("permission.requested", "maps", {"request_id": rid, "tool": "AskUserQuestion",
                                                              "interactive": False, "input": self.Q})["seq"]

    def test_a_form_answered_with_the_owners_words_is_answered(self):
        """The core answers the open form with the owner's message: no permission.answered in the journal, only the
        session's record of the answer (how: answered) and the message."""
        seq = self.ask_in_dialog("r1")
        self.assertEqual(self.show(seq)[1], "state: open — waits for the owner's answer")
        self.j.append("message.received", None, {"to": "maps", "text": "no, leave the key", "channel": "web",
                                                 "owner": True})
        out = self.show(seq)
        self.assertEqual(out[1], "state: answered")
        self.assertIn("no, leave the key", out[-1])

    def test_a_card_that_left_with_no_trace_is_not_said_to_be_unanswered(self):
        """A form dropped when the session left its dialog: no record, no fact, no words: what happened to it is not
        known, so the line says only that it no longer waits."""
        seq = self.ask_in_dialog("r1")
        self.j.append("session.ended", "maps", {})
        out = self.show(seq)
        self.assertEqual(out[1], "state: closed — no longer waits for the owner")
        self.assertEqual(out[-1], out[1])
        self.assertNotIn("without an answer", "\n".join(out))

    def bus_card(self, holder):
        """A card of the bus (busstate._card): its qN is the core's bus event, the card is an ask of the liaison."""
        seq = self.j.append("core.bus_send_held", None, {"out": "o9", "card": {"input": self.Q,
                                                                               "labels": {"send": "Send"}}})["seq"]
        return seq, {"sessions": {"liaison": {"name": "liaison", "state": "idle", **holder(f"q{seq}")}}}

    def test_an_open_card_of_the_bus_is_open(self):
        """The idle rule names a card in "Waits for you" only while `hub show qN` says open: the liaison's card for the
        owner's Send used to have no entry at all."""
        seq, state = self.bus_card(lambda q: {"asks": [{"id": q, "request_id": "bus-o9", "tool": "AskUserQuestion",
                                                        "input": self.Q, "interactive": True, "since": time.time(),
                                                        "hubbus": {"kind": "out"}}]})
        out = views.entry(self.state, f"q{seq}", types.SimpleNamespace(state=state), tz="UTC")
        self.assertTrue(out[0].startswith(f"q{seq} · "))
        self.assertIn("liaison", out[0])
        self.assertEqual(out[1], "state: open — waits for the owner's answer")
        self.assertIn("Rotate the key?", "\n".join(out))

    def test_an_answered_card_of_the_bus_is_answered(self):
        seq = self.bus_card(lambda q: {})[0]
        got = self.j.append("permission.answered", "liaison", {"request_id": "bus-o9", "behavior": "allow",
                                                               "answers": {"Rotate the key?": "Send"}, **self.OWNER})
        state = {"sessions": {"liaison": {"name": "liaison", "state": "idle", "asks": [], "answered": [
            {"request_id": "bus-o9", "seq": got["seq"], "how": "answered", "id": f"q{seq}", "ts": got["ts"],
             "by": "owner"}]}}}
        out = views.entry(self.state, f"q{seq}", types.SimpleNamespace(state=state), tz="UTC")
        self.assertEqual(out[1], "state: answered")
        self.assertRegex(out[-1], r"^answer \(.*\): Rotate the key\? → Send$")

    def test_a_card_of_the_bus_nobody_holds_no_longer_waits(self):
        seq, state = self.bus_card(lambda q: {})
        out = views.entry(self.state, f"q{seq}", types.SimpleNamespace(state=state), tz="UTC")
        self.assertEqual(out[1], "state: closed — no longer waits for the owner")

    def test_a_contacts_card_is_no_question(self):
        """core.bus_join carries the other hub's card (its key, its name): not a question card, no entry as before."""
        seq = self.j.append("core.bus_join", None, {"card": {"fp": "hb1-x", "label": "Other hub"}})["seq"]
        self.assertEqual(views.entry(self.state, f"q{seq}", types.SimpleNamespace(state={}), tz="UTC"), [])

    def test_a_removed_sessions_answered_card_is_not_said_to_be_unanswered(self):
        """The session is gone, but its card is no longer among its cards: it was closed some way the state no longer
        keeps (an answer in words whose record rotated out), so "without an answer" is not said."""
        seq = self.ask("r1")
        self.j.append("message.received", None, {"to": "maps", "text": "yes, rotate it", "channel": "web",
                                                 "owner": True})
        self.j.append("session.ended", "maps", {})
        self.j.append("session.remove_requested", "maps", {"channel": "cli", "owner": True})
        from core import tail
        state = tail.cached(self.state).state
        self.assertEqual(state["sessions"]["maps"]["state"], "removed")
        state["sessions"]["maps"]["answered"], state["messages"] = [], {}   # both rotated out
        ev = journal.event_at(self.state, seq)
        self.assertEqual(views.card_state(self.state, ev, state, time.time())["state"], "closed")

    def test_the_note_of_the_x_is_not_the_owners_answer(self):
        seq = self.ask("r1")
        self.j.append("question.dismissed", "maps", {"request_id": "r1", **self.OWNER})
        from core import tail
        state = tail.cached(self.state).state
        state["sessions"]["maps"]["answered"] = []   # the record rotated out: the note of the x is still in messages
        self.assertTrue(any(m.get("card_dismissed") and m.get("answers_question") == f"q{seq}"
                            for m in state["messages"].values()))
        ev = journal.event_at(self.state, seq)
        self.assertEqual(views.card_state(self.state, ev, state, time.time())["state"], "dismissed")

    def test_no_empty_brackets_and_no_question_mark(self):
        self.assertEqual(views._card_line({"state": "withdrawn"}, "UTC"), "state: closed without an answer — withdrawn")
        dep = {"name": "Boris", "state": "asked"}
        self.assertEqual(views._card_line({"state": "open", "qid": "q7", "deputy": dep}, "UTC"),
                         "state: open — routed to Boris (deputy), waits for their answer")
        self.assertEqual(views._card_line({"state": "open", "qid": "q7", "deputy": {**dep, "state": "answered"}}, "UTC"),
                         'state: answered by Boris (deputy) — close the card: hub decided --card q7 '
                         '"<what> · Boris, deputy · how to roll back"')
        self.assertEqual(views._card_line({"state": "dismissed"}, "UTC"),
                         "state: closed without an answer — the owner closed the card")
        self.assertEqual(views._card_line({"state": "withdrawn", "by": "hub", "ts": 1795200000, "why": "dup"}, "UTC"),
                         "state: closed without an answer — withdrawn by hub (20.11.2026 18:40): dup")

    def test_a_refused_closing_is_skipped_for_a_later_accepted_one(self):
        seq = self.ask("r1")
        first = self.j.append("permission.answered", "maps", {"request_id": "r1", "behavior": "allow", "owner": False,
                                                             "channel": "session"})["seq"]
        self.j.append("core.rejected", None, {"of": first, "why": "test"})
        self.assertIsNone(journal.closing_of(self.state, seq, "r1"))
        second = self.j.append("question.dismissed", "maps", {"request_id": "r1", **self.OWNER})["seq"]
        self.assertEqual(journal.closing_of(self.state, seq, "r1")["seq"], second)

    def test_a_refusal_in_the_next_file_still_refuses(self):
        """The core's refusal of the archive's last line is the first line of the next file."""
        seq = self.ask("r1")
        bad = self.j.append("question.dismissed", "maps", {"request_id": "r1", "channel": "session", "owner": False})["seq"]
        self.j.append("core.rejected", None, {"of": bad, "why": "test"})
        path = os.path.join(self.state, "journal.jsonl")
        lines = open(path, encoding="utf-8").read().splitlines()
        cut = next(i for i, x in enumerate(lines) if json.loads(x)["seq"] == bad) + 1   # the archive ends on the fact
        arc = os.path.join(self.state, journal.ARCHIVE)
        os.makedirs(arc)
        with open(os.path.join(arc, f"journal-1-{bad}.jsonl"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines[:cut]) + "\n")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines[cut:]) + "\n")
        self.assertIsNone(journal.closing_of(self.state, seq, "r1"))
        ok = self.j.append("question.dismissed", "maps", {"request_id": "r1", **self.OWNER})["seq"]
        self.assertEqual(journal.closing_of(self.state, seq, "r1")["seq"], ok)

    def test_a_withdrawal_read_from_the_archive(self):
        seq = self.ask("r1")
        self.j.append("question.withdrawn", "maps", {"id": f"q{seq}", "why": "same as q5", "channel": "session",
                                                     "owner": False, "sender": "maps"})
        journal.rotate(self.state, {"sessions": {}}, keep=1, per_session=0)
        self.assertRegex(self.show(seq)[1], r"^state: closed without an answer — withdrawn by maps")

    def test_a_dismissal_read_from_the_archive(self):
        """The session's record keeps the last cards only; an older one is found in the journal."""
        seq = self.ask("r1")
        self.j.append("question.dismissed", "maps", {"request_id": "r1", **self.OWNER})
        for i in range(20):   # more closed cards than the session's record keeps
            self.ask(f"x{i}")
            self.j.append("question.dismissed", "maps", {"request_id": f"x{i}", **self.OWNER})
        self.assertRegex(self.show(seq)[1], r"^state: closed without an answer — the owner closed the card")


class Cache(unittest.TestCase):
    """2026-10-07: hub show took 10.7 and 5.2 s on a 14.7 MB journal: every call folded the whole journal. The parsed
    data lies next to the journal; the next call reads only the new events."""

    def setUp(self):
        from core import tail
        self.tail = tail
        self.state = tempfile.mkdtemp(prefix="show-cache-")
        self.j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        for t, e, d in EVENTS:
            self.j.append(t, e, d)

    def test_second_call_reads_only_the_new_events(self):
        self.assertIn("Generation done", "\n".join(views.show(self.tail.cached(self.state), "site-build")))
        self.j.append("session.said", "site-build", {"text": "second reply"})
        from unittest import mock
        fed, real = [], views.Reader.feed_all
        with mock.patch.object(views.Reader, "feed_all", lambda r, evs: (fed.extend(evs), real(r, evs))):
            text = "\n".join(views.show(self.tail.cached(self.state), "site-build"))
        self.assertEqual([e["type"] for e in fed], ["session.said"])
        self.assertIn("second reply", text)
        self.assertIn("Generation done", text)

    def test_sessions_and_the_owner_keep_their_own_parse(self):
        # captions are parsed in the process language: a session reads English, the owner his own — each keeps its
        # parse, turns do not throw the other's away
        from unittest import mock
        from core import i18n
        self.addCleanup(setattr, i18n, "_current", i18n._current)
        with mock.patch.dict(os.environ, {"HUB_LANG": "ru"}):
            os.environ.pop("HUB_SESSION", None)
            i18n._current = None
            self.tail.cached(self.state)
            with mock.patch.dict(os.environ, {"HUB_SESSION": "maps"}):
                i18n._current = None
                self.tail.cached(self.state)
            self.assertTrue(os.path.exists(os.path.join(self.state, self.tail.CACHE)))
            self.assertTrue(os.path.exists(os.path.join(self.state, self.tail.SESSION_CACHE)))
            self.j.append("session.said", "site-build", {"text": "second reply"})
            fed, real = [], views.Reader.feed_all
            with mock.patch.object(views.Reader, "feed_all", lambda r, evs: (fed.extend(evs), real(r, evs))):
                i18n._current = None
                self.tail.cached(self.state)
                with mock.patch.dict(os.environ, {"HUB_SESSION": "maps"}):
                    i18n._current = None
                    self.tail.cached(self.state)
            self.assertEqual([e["type"] for e in fed], ["session.said", "session.said"])

    def test_the_search_reader_holds_what_the_recent_feeds_leave_out(self):
        # `hub show` folds from the last snapshot that has 1500 events after it and keeps 400 rows per feed; the search
        # reads the whole journal: a row from before that snapshot, and the ones past the 400, are in its feeds
        from core import reducer
        self.j.append("session.said", "other", {"text": "narwhal before the snapshot"})
        self.j.append("core.snapshot", None, {"state": reducer.empty()})
        for i in range(1500):
            self.j.append("session.said", "site-build", {"text": f"pad {i}"})
        texts = lambda r, name: [it.get("text") for it in r.timelines.get(name, [])]   # noqa: E731
        self.assertNotIn("narwhal before the snapshot", texts(self.tail.cached(self.state), "other"))
        full = self.tail.cached(self.state, full=True)
        self.assertIn("narwhal before the snapshot", texts(full, "other"))
        self.assertIn("Generation done: 17 files per language, sync-pages passed.", texts(full, "site-build"))   # the 400 do not cut it
        self.assertGreater(len(full.timelines["site-build"]), views.TIMELINE_LIMIT)
        self.assertLessEqual(len(self.tail.cached(self.state).timelines["site-build"]), views.TIMELINE_LIMIT)
        self.j.append("session.said", "other", {"text": "second reply"})   # the next call reads only the new events
        fed, real = [], views.Reader.feed_all
        from unittest import mock
        with mock.patch.object(views.Reader, "feed_all", lambda r, evs: (fed.extend(evs), real(r, evs))):
            self.tail.cached(self.state, full=True)
        self.assertEqual([e["type"] for e in fed], ["session.said"])

    def test_a_new_journal_is_read_from_its_start(self):
        self.tail.cached(self.state)
        os.replace(self.j.path, self.j.path + ".old")      # rotation: a new file, another inode
        j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        for t, e, d in EVENTS[9:]:
            j.append(t, e, d)
        r = self.tail.cached(self.state)
        self.assertEqual(views.show(r, "site-build"), [])
        self.assertTrue(views.show(r, "other"))

    def test_a_broken_cache_is_rebuilt(self):
        self.tail.cached(self.state)
        for f in os.listdir(os.path.join(self.state, "cache")):
            with open(os.path.join(self.state, "cache", f), "wb") as out:
                out.write(b"garbage")
        self.assertTrue(views.show(self.tail.cached(self.state), "site-build"))

    def test_a_cache_written_by_someone_else_is_not_unpickled(self):
        # pickle runs code on load: the cache is loaded only if signed with this hub's key
        import pickle
        self.tail.cached(self.state)
        store = os.path.join(self.state, self.tail.CACHE)
        ran = os.path.join(self.state, "ran")

        class Payload:
            def __reduce__(self):
                return (open, (ran, "w"))
        with open(store, "wb") as out:
            pickle.dump({"key": None, "offset": 0, "reader": Payload()}, out)
        self.assertTrue(views.show(self.tail.cached(self.state), "site-build"))
        self.assertFalse(os.path.exists(ran))
        if os.name == "posix":
            for f in os.listdir(os.path.join(self.state, "cache")):
                self.assertEqual(os.stat(os.path.join(self.state, "cache", f)).st_mode & 0o077, 0, f)

    def test_no_such_session_is_answered_without_the_journal(self):
        # "no such session" — by the core state file, the journal is not read (2026-10-07)
        with open(os.path.join(self.state, journal.STATE_FILE), "w") as f:
            json.dump({"seq": 12, "offset": os.path.getsize(self.j.path), "first": 1,
                       "state": {"sessions": {"site-build": {}, "other": {}}}}, f)
        from unittest import mock
        with mock.patch.object(journal, "read", side_effect=AssertionError("read the journal")), \
                mock.patch.object(views.Reader, "feed_all", side_effect=AssertionError("folded the journal")):
            self.assertFalse(self.tail.known(self.state, "nope"))
            self.assertTrue(self.tail.known(self.state, "other"))



class Archived(unittest.TestCase):
    """The journal rotates: what was written before the last rotation is in the archives. `hub show --grep` looks there
    too (the page's "Show earlier" does), and `--grep` may stand in any place, with a session — a search inside it."""

    def setUp(self):
        from core.daemon import Hub
        from tests.test_snapshot import NullHost
        self.state = tempfile.mkdtemp(prefix="show-arch-")
        self.hub = Hub(self.state, NullHost(), registry_dir=self.state)
        for name in ("a", "b"):
            self.hub.record("session.requested", name, {"session_id": "s-" + name, "cwd": "/w", "channel": "web", "owner": True})
            self.hub.record("session.started", name, {})
        self.say("a", "the zebra answer is yes")
        self.say("b", "zebra of the other session")
        for i in range(3):
            self.say("b", f"b filler {i}")
        for _ in range(2):
            for i in range(5):
                self.say("a", f"filler {i}")
            self.rotate()
        self.say("a", "fresh zebra news")
        self.env = {**os.environ, "HUB_STATE_DIR": self.state, "PYTHONUTF8": "1"}

    def say(self, to, text):
        self.hub.record("message.received", None, {"to": to, "text": text, "channel": "web", "owner": True})

    def rotate(self):
        from core import history
        from core.daemon import Hub
        from tests.test_snapshot import NullHost
        journal.rotate(self.state, self.hub.state, keep=2, per_session=2)
        self.hub = Hub(self.state, NullHost(), registry_dir=self.state)
        history.History(self.state).build_all()

    def run_hub(self, *args, code=0):
        r = subprocess.run([sys.executable, HUB, *args], env=self.env, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, code, r.stderr)
        return r.stdout

    def test_grep_finds_what_the_rotation_took_out_of_the_live_window(self):
        out = self.run_hub("show", "--grep", "zebra")
        self.assertIn("the zebra answer is yes", out)
        self.assertIn("zebra of the other session", out)
        self.assertIn("fresh zebra news", out)
        self.assertLess(out.index("the zebra answer"), out.index("fresh zebra news"))   # newest at the bottom
        self.assertEqual(out.count("fresh zebra news"), 1)   # a row carried by the rotation is not twice

    def test_grep_with_a_session_searches_inside_it(self):
        out = self.run_hub("show", "b", "--grep", "zebra")
        self.assertIn("zebra of the other session", out)
        self.assertNotIn("the zebra answer", out)
        out = self.run_hub("show", "--grep", "zebra", "a", "-n", "1")   # the flag first, the count after the session
        self.assertEqual([line for line in out.splitlines() if "zebra" in line][0].split()[0], "a")
        self.assertNotIn("other session", out)
        self.assertEqual(len(out.strip().splitlines()), 1)

    def test_grep_flag_is_not_swallowed_by_the_session_view(self):
        self.assertNotIn("filler 3", self.run_hub("show", "a", "--grep", "zebra"))
        self.assertIn(i18n.t("cli.show.grep_nobody", text="narwhal"), self.run_hub("show", "a", "--grep", "narwhal"))
        self.run_hub("show", "--grep", code=1)   # no value is an error, not a silent session view

    def test_unparsed_archives_are_named_not_hidden(self):
        for path in journal.archives(self.state):
            os.remove(path + ".index.json")
        out = self.run_hub("show", "--grep", "zebra")
        self.assertIn("fresh zebra news", out)
        self.assertIn(i18n.t("cli.show.grep_unparsed", n=2), out)

    def test_grep_finds_a_row_the_live_window_dropped(self):
        # a session wrote more than the feed keeps (TIMELINE_LIMIT) since the last rotation: its early row lies in the
        # journal file, no archive has it yet
        self.say("a", "quokka early word")
        for i in range(views.TIMELINE_LIMIT + 5):
            self.say("a", f"pad {i}")
        out = self.run_hub("show", "--grep", "quokka early")
        self.assertEqual(out.count("quokka early word"), 1)
        self.assertEqual(self.run_hub("show", "a", "--grep", "quokka early").count("quokka early word"), 1)
        out = self.run_hub("show", "--grep", "zebra")   # the archive, the journal and the window meet: nothing twice
        for text in ("the zebra answer is yes", "zebra of the other session", "fresh zebra news"):
            self.assertEqual(out.count(text), 1, text)

    def test_nothing_found_in_a_part_of_the_archives_is_not_nobody(self):
        for path in journal.archives(self.state):
            os.remove(path + ".index.json")
        out = self.run_hub("show", "--grep", "narwhal")
        self.assertNotIn(i18n.t("cli.show.grep_nobody", text="narwhal"), out)
        self.assertIn(i18n.t("cli.show.grep_none_here", text="narwhal"), out)
        self.assertIn(i18n.t("cli.show.grep_unparsed", n=2), out)

    def test_time_limit_stops_between_archives_and_says_where(self):
        from core import history
        found, info = history.grep(self.state, "zebra", budget=-1)   # one archive per answer
        newest = [os.path.basename(p) for p in reversed(journal.archives(self.state))]
        self.assertEqual(found, [])   # the zebra lines are in the oldest archive
        self.assertEqual(info, {"unparsed": 0, "stopped": newest[1]})
        found, info = history.grep(self.state, "zebra")
        self.assertEqual(info["stopped"], None)
        self.assertIn("the zebra answer is yes", [row["text"] for _, row in found])

    def test_enough_found_stops_before_the_older_archives(self):
        from core import history
        found, info = history.grep(self.state, "filler", need=1)
        everything, _ = history.grep(self.state, "filler", need=100)
        self.assertGreater(len(found), 0)
        self.assertLess(len(found), len(everything))   # an archive is whole, the older ones were not opened
        self.assertEqual(info["stopped"], None)   # enough is not a limit that cut the search

    def test_a_row_in_two_feeds_is_found_in_both(self):
        # one event, one row in two feeds (the owner's words to a mini-hub's executor: the mini-hub's feed and the
        # executor's): a search across sessions lists it under each, as the live part does
        from core import history
        oldest = journal.archives(self.state)[0]
        with open(oldest + history.CACHE, encoding="utf-8") as f:
            timelines = json.load(f)
        (row,) = [r for r in timelines["a"] if r.get("text") == "the zebra answer is yes"]
        timelines["b"] = sorted(timelines["b"] + [row], key=lambda r: r.get("seq", 0))
        history._write(oldest, timelines)
        found, _ = history.grep(self.state, "zebra answer")
        self.assertEqual(sorted(who for who, _ in found), ["a", "b"])

    def test_renamed_session_is_one_session_in_the_answer(self):
        from core import history, tail
        live = tail.cached(self.state).timelines
        live["c"] = live.pop("b")   # as the reader holds it after the rename; the archive still says "b"
        found, _ = history.grep(self.state, "b filler", live=live, renamed={"b": "c"})
        self.assertEqual({who for who, _ in found}, {"c"})
        self.assertEqual([row["text"] for _, row in found if row["seq"] in {r["seq"] for r in live["c"]}], [])   # not twice
        again, _ = history.grep(self.state, "b filler", renamed={"b": "c"})
        self.assertEqual({who for who, _ in again}, {"c"})
        self.assertGreater(len(again), len(found))


if __name__ == "__main__":
    unittest.main()
