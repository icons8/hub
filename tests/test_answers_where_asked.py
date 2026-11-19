"""Answers go where the question was asked (owner requirement 2026-10-08): the hub's result goes to Telegram only if the
chain began with the owner's message from Telegram (or the owner is away — /away); what was written on the page is
answered on the page. An executor on a task the hub set from the owner's Telegram message also answers there. The
chain root is in the core (core/chain.py): turn label → message → re / task / schedule / the turn of whoever wrote →
the owner's message and its channel. Question cards, monitors and the "you are back" digest — as they were."""
import tests  # noqa: F401 — do not see the machine's settings
import os
import tempfile
import unittest

from core import chain, executors, journal, reducer
from tests import test_telegram as base


class AnswersWhereAsked(unittest.TestCase):
    # a fake Bot API and journal — as in tests/test_telegram.py (without its tests: they run there)
    setUp, tearDown = base.TelegramTest.setUp, base.TelegramTest.tearDown
    spooled, msg, ingest = base.TelegramTest.spooled, base.TelegramTest.msg, base.TelegramTest.ingest

    # --- helpers-----------------------------------------------------------------------------------------------
    def from_telegram(self, text, tg_id):
        """The owner's message to the hub from Telegram; its number in the core is mN."""
        self.fake.updates = [self.msg(text, tg_id)]
        self.bot.poll(timeout=0)
        self.ingest()
        ev = [e for e in journal.read(self.j.path) if e["type"] == "message.received"][-1]
        return f"m{ev['seq']}"

    def from_web(self, text, to=None):
        ev = self.j.append("message.received", None, {"text": text, "channel": "web", "owner": True,
                                                      **({"to": to} if to else {})})
        return f"m{ev['seq']}"

    def session(self, name, **extra):
        self.j.append("session.requested", name, {"cwd": "/w", "session_id": name, **extra})
        self.j.append("session.started", name, {"session_id": name})

    def by(self, sender):
        return {"channel": "session", "owner": False, "sender": sender}

    def hub_turn(self, marker, text):
        self.j.append("turn.started", "hub", {"marker": marker})
        self.j.append("turn.result", "hub", {"text": text})
        self.j.append("turn.stopped", "hub", {})

    def core(self):
        return reducer.replay(journal.read(self.j.path))

    def texts(self):
        self.bot.tick()
        return [(s["text"], (s.get("reply_parameters") or {}).get("message_id")) for s in self.fake.sent()]

    # --- core: the chain root------------------------------------------------------------------------------------
    def test_core_names_the_root_and_its_channel(self):
        tg = self.from_telegram("count the revenue", 5)
        web = self.from_web("and this from the page")
        st = self.core()
        self.assertEqual((chain.root(st, tg), chain.channel(st, tg)), (tg, "telegram"))
        self.assertEqual((chain.root(st, web), chain.channel(st, web)), (web, "web"))
        # a hub task on the owner's word (--for) and the core's verdict to the hub about it — the same chain
        self.session("w")
        ev = self.j.append("delegation.created", None, {"session": "w", "for": tg, **self.by("hub")})
        did = f"d{ev['seq']}"
        self.j.append("turn.started", "w", {"marker": f"m{ev['seq']}"})
        res = self.j.append("turn.result", "w", {"text": "42 000 $"})
        st = self.core()
        replied = next(m for m in st["messages"].values() if m["text"].startswith(f"REPLIED {did}"))
        self.assertEqual((replied["id"], chain.root(st, replied["id"])), (f"m{res['seq']}", tg))
        # a session's message to the hub with no link to the owner: no root
        ev = self.j.append("message.received", None, {"to": "hub", "text": "I need access", **self.by("w")})
        self.assertIsNone(chain.root(self.core(), f"m{ev['seq']}"))

    # --- hub results------------------------------------------------------------------------------------------------
    def test_hub_answer_to_a_page_message_stays_on_the_page(self):
        mid = self.from_web("check the mail")
        self.hub_turn(mid, "The mail is clean")
        self.assertEqual(self.texts(), [])

    def test_hub_answer_to_a_telegram_message_is_a_reply_there(self):
        mid = self.from_telegram("check the mail", 5)
        self.hub_turn(mid, "The mail is clean")
        self.assertEqual(self.texts(), [("The mail is clean", 5)])

    def test_hub_reacting_to_a_session_or_the_core_alone_is_not_sent(self):
        self.session("w")
        ev = self.j.append("message.received", None, {"to": "hub", "text": "done, check it", **self.by("w")})
        self.hub_turn(f"m{ev['seq']}", "Checked, all good")
        self.hub_turn(None, "The background check is over")
        self.assertEqual(self.texts(), [])

    def test_when_he_is_away_everything_goes_to_telegram_as_before(self):
        mid = self.from_web("check the mail")   # wrote and left: a message written in "away" mode would bring the result back (2026-10-09)
        self.j.append("owner.away", None, {"hours": 2, "channel": "web", "owner": True})
        self.hub_turn(mid, "The mail is clean")
        self.session("w")
        ev = self.j.append("message.received", None, {"to": "hub", "text": "done", **self.by("w")})
        self.hub_turn(f"m{ev['seq']}", "w is done")
        self.assertEqual(self.texts(), [("The mail is clean", None), ("w is done", None)])

    def test_a_session_writing_to_the_hub_inside_his_telegram_chain_keeps_it(self):
        # the hub, on the owner's Telegram message, asked a session, it answered the hub (`hub send hub`) — the hub's result goes to the owner there too
        mid = self.from_telegram("ask w for the status", 5)
        self.session("w")
        self.j.append("turn.started", "hub", {"marker": mid})
        ask = self.j.append("message.received", None, {"to": "w", "text": "what status?", **self.by("hub")})
        self.j.append("turn.result", "hub", {"text": "—"})
        self.j.append("turn.stopped", "hub", {})
        self.j.append("turn.started", "w", {"marker": f"m{ask['seq']}"})
        back = self.j.append("message.received", None, {"to": "hub", "text": "status: green", **self.by("w")})
        self.j.append("turn.result", "w", {"text": "replied to the hub"})
        self.j.append("turn.stopped", "w", {})
        self.hub_turn(f"m{back['seq']}", "w is all green")
        self.assertEqual(self.texts(), [("w is all green", 5)])

    def test_a_turn_without_a_marker_continues_the_previous_turn_chain(self):
        # Claude Code itself wakes the hub when its background work finishes: that is a continuation of the same turn
        tg = self.from_telegram("build a report", 5)
        self.hub_turn(tg, "—")
        self.hub_turn(None, "The report is ready")
        web = self.from_web("and one more")
        self.hub_turn(web, "—")
        self.hub_turn(None, "And this is done")
        self.assertEqual(self.texts(), [("The report is ready", 5)])

    # --- executors------------------------------------------------------------------------------------------------
    def delegate(self, mid, to="w"):
        ev = self.j.append("delegation.created", None, {"session": to, "for": mid, **self.by("hub")})
        return f"d{ev['seq']}", f"m{ev['seq']}"

    def test_executor_on_a_telegram_task_answers_there_and_again_later(self):
        tg = self.from_telegram("fix the feed", 5)
        self.session("w")
        did, task = self.delegate(tg)
        self.j.append("turn.started", "w", {"marker": task})
        self.j.append("turn.result", "w", {"text": "waiting for the agent"})
        self.j.append("turn.stopped", "w", {})
        self.j.append("turn.started", "w", {"marker": None})
        res = self.j.append("turn.result", "w", {"text": "done: MR !31"})
        self.j.append("turn.stopped", "w", {})
        # a quote of the owner's message goes on the first reply; the result later arrives in the same session message, without a quote (2026-10-25)
        self.assertEqual(self.texts(), [("<b>w</b> replies:\nwaiting for the agent", 5),
                                        ("<b>w</b> · on the task “fix the feed”:\ndone: MR !31", None)])
        # the owner received the continuation himself — the hub is not sent "REPLIED (continuation)" (owner decision 2026-10-16)
        self.assertFalse(any("(continuation)" in m["text"] for m in self.core()["messages"].values()))
        self.assertTrue(res)

    def test_executor_on_a_page_task_answers_on_the_page(self):
        web = self.from_web("fix the feed")
        self.session("w")
        did, task = self.delegate(web)
        self.j.append("turn.started", "w", {"marker": task})
        res = self.j.append("turn.result", "w", {"text": "done: MR !31"})
        self.j.append("turn.stopped", "w", {})
        self.hub_turn(f"m{res['seq']}", "w is done")
        self.assertEqual(self.texts(), [])

    def test_a_task_run_worker_raised_in_his_telegram_chain_reaches_him_through_the_hub(self):
        # `hub task run` (an executor for a task): the result goes to the hub as a "reply to task", the hub passes it on — to the owner in Telegram
        tg = self.from_telegram("bring up an executor for the report", 5)
        self.j.append("turn.started", "hub", {"marker": tg})
        self.j.append("task.run_requested", "rep", {"task": "build a report", **self.by("hub")})
        self.j.append("turn.result", "hub", {"text": "—"})
        self.j.append("turn.stopped", "hub", {})
        st = self.core()
        run = st["runs"]["rep"]
        for type, entity, data in executors.events_for(st, "rep", run, "/w", None):
            ev = self.j.append(type, entity, data)
        task = f"m{ev['seq']}"
        self.j.append("session.started", "rep", {"session_id": self.core()["sessions"]["rep"]["session_id"]})
        self.j.append("turn.started", "rep", {"marker": task})
        res = self.j.append("turn.result", "rep", {"text": "report: 12 rows"})
        self.j.append("turn.stopped", "rep", {})
        st = self.core()
        answer = next(m for m in st["messages"].values() if "reply to task" in m["origin"] and m["session"] == "hub")
        self.assertEqual(chain.root(st, answer["id"]), tg)
        self.hub_turn(answer["id"], "The report is ready: 12 rows")
        self.assertIn(("The report is ready: 12 rows", 5), self.texts())
        self.assertNotIn("rep", [t for t, _ in self.texts() if t.startswith("<b>rep")])
        self.assertTrue(res)

    def test_nested_task_answer_continues_the_executors_telegram_task(self):
        # the executor of an owner's Telegram task set a subtask further on; the reply to it continues its task:
        # its result after that goes to the owner too (in the same session message, without a new quote)
        tg = self.from_telegram("make a landing page", 5)
        self.session("w")
        self.session("v")
        did, task = self.delegate(tg)
        self.j.append("turn.started", "w", {"marker": task})
        sub = self.j.append("delegation.created", None, {"session": "v", "for_session": "w", **self.by("w")})
        self.j.append("turn.result", "w", {"text": "handed the pictures to v, waiting"})
        self.j.append("turn.stopped", "w", {})
        self.j.append("turn.started", "v", {"marker": f"m{sub['seq']}"})
        self.j.append("turn.result", "v", {"text": "the pictures are ready"})
        self.j.append("turn.stopped", "v", {})
        st = self.core()
        back = next(m for m in st["messages"].values() if m["session"] == "w" and "reply to task" in m["origin"])
        self.assertEqual(chain.root(st, back["id"]), tg)
        self.j.append("turn.started", "w", {"marker": back["id"]})
        self.j.append("turn.result", "w", {"text": "the landing page is ready"})
        self.assertEqual(self.texts(), [("<b>w</b> replies:\nhanded the pictures to v, waiting", 5),
                                        ("<b>w</b> · on the task “make a landing page”:\nthe landing page is ready", None)])

    # --- files and schedule-------------------------------------------------------------------------------------------
    def shot(self):
        path = os.path.join(tempfile.mkdtemp(), "shot.png")
        with open(path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"x" * 100)
        return path

    def photos(self):
        self.bot.tick()
        return [m for m, _ in self.fake.calls if m in ("sendPhoto", "sendDocument")]

    def test_a_file_follows_the_chain_of_the_turn_that_sent_it(self):
        path = self.shot()
        web = self.from_web("show the page")
        self.j.append("turn.started", "hub", {"marker": web})
        self.j.append("owner.file", None, {"path": path, "caption": "here", **self.by("hub")})
        self.j.append("turn.result", "hub", {"text": "—"})
        self.j.append("turn.stopped", "hub", {})
        self.assertEqual(self.photos(), [])
        tg = self.from_telegram("show the page", 5)
        self.j.append("turn.started", "hub", {"marker": tg})
        self.j.append("owner.file", None, {"path": path, "caption": "here", **self.by("hub")})
        self.assertEqual(self.photos(), ["sendPhoto"])
        # without a chain (an owner command from the terminal, a session outside a turn) — as before
        self.j.append("owner.file", None, {"path": path, "caption": "here", "channel": "cli", "owner": True})
        self.assertEqual(self.photos(), ["sendPhoto", "sendPhoto"])

    def test_a_schedule_set_in_his_telegram_chain_reports_there(self):
        import time
        at = round(time.time()) + 3600
        tg = self.from_telegram("remind me in an hour about the report", 5)
        self.j.append("turn.started", "hub", {"marker": tg})
        self.j.append("schedule.set", "rem", {"kind": "at", "at": at, "session": "hub", "prompt": "remind about the report",
                                              **self.by("hub")})
        self.j.append("turn.result", "hub", {"text": "Set"})
        self.j.append("turn.stopped", "hub", {})
        self.j.append("schedule.due", "rem", {"slot": at, "channel": "core"}, ts=at)
        fired = next(m for m in self.core()["messages"].values() if m.get("schedule") == "rem")
        self.assertEqual(chain.root(self.core(), fired["id"]), tg)
        self.hub_turn(fired["id"], "Reminding: the report")
        # the same job set from the page — answers on the page
        web = self.from_web("remind me in an hour about the bill")
        self.j.append("turn.started", "hub", {"marker": web})
        self.j.append("schedule.set", "bill", {"kind": "at", "at": at, "session": "hub", "prompt": "about the bill",
                                               **self.by("hub")})
        self.j.append("turn.result", "hub", {"text": "—"})
        self.j.append("turn.stopped", "hub", {})
        self.j.append("schedule.due", "bill", {"slot": at, "channel": "core"}, ts=at)
        fired = next(m for m in self.core()["messages"].values() if m.get("schedule") == "bill")
        self.hub_turn(fired["id"], "Reminding: the bill")
        self.assertEqual(self.texts(), [("Set", 5), ("Reminding: the report", 5)])
