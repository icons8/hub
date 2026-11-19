"""telegram.page_results (off by default): what the core marks for the owner (a file from `hub send-file`, a result with
owner_reply) goes to Telegram whatever the chain began with, once. Off — the rule "answer where he asked" as before."""
import tests  # noqa: F401 — do not see the machine's settings
import os
import unittest

from core import i18n, journal
from modules.telegram import bot as tg
from tests import test_telegram as base

S = {"channel": "session", "owner": False}


class PageResults(unittest.TestCase):
    # a fake Bot API and journal — as in tests/test_telegram.py (without its tests: they run there)
    setUp, tearDown = base.TelegramTest.setUp, base.TelegramTest.tearDown
    spooled, msg, ingest = base.TelegramTest.spooled, base.TelegramTest.msg, base.TelegramTest.ingest

    ON = {"telegram": {"page_results": True}}

    def session(self, *names):
        for n in names:
            self.j.append("session.requested", n, {"cwd": f"/{n}", "session_id": n})
            self.j.append("session.started", n, {"session_id": n})

    def from_web(self, text, to="hub"):
        ev = self.j.append("message.received", None, {"to": to, "text": text, "channel": "web", "owner": True})
        return f"m{ev['seq']}"

    def run_(self, cfg, times=1):
        self.bot.hub_config = lambda: cfg
        self.fake.calls.clear()
        for _ in range(times):
            self.bot.tick()
        return [s["text"] for s in self.fake.sent()]

    def png(self):
        path = os.path.join(self.dir, "shot.png")   # removed with the test's folder
        with open(path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"x" * 100)
        return path

    def photos(self, cfg, times=1):
        self.bot.hub_config = lambda: cfg
        self.fake.calls.clear()
        for _ in range(times):
            self.bot.tick()
        return [m for m, _ in self.fake.calls if m in ("sendPhoto", "sendDocument")]

    # --- a file ------------------------------------------------------------------------------------------------
    def test_a_file_by_his_page_message_goes_to_telegram_with_the_setting_once(self):
        path = self.png()
        web = self.from_web("show the page")
        self.j.append("turn.started", "hub", {"marker": web})
        self.j.append("owner.file", None, {"path": path, "caption": "here", **S, "sender": "hub"})
        self.j.append("turn.result", "hub", {"text": "—"})
        self.j.append("turn.stopped", "hub", {})
        self.assertEqual(self.photos(self.ON), ["sendPhoto"])
        self.assertEqual(self.photos(self.ON, 2), [])   # a repeated tick is not a second time

    def test_a_file_by_his_page_message_stays_on_the_page_without_the_setting(self):
        path = self.png()
        web = self.from_web("show the page")
        self.j.append("turn.started", "hub", {"marker": web})
        self.j.append("owner.file", None, {"path": path, "caption": "here", **S, "sender": "hub"})
        self.assertEqual(self.photos({}), [])
        self.assertEqual(self.photos({"telegram": {"page_results": False}}), [])

    def test_a_file_in_his_telegram_chain_goes_once_with_or_without_the_setting(self):
        path = self.png()
        for cfg in ({}, self.ON):
            self.fake.updates = [self.msg("show the page", 5)]
            self.bot.poll(timeout=0)
            self.ingest()
            tg = [e for e in journal.read(self.j.path) if e["type"] == "message.received"][-1]
            self.j.append("turn.started", "hub", {"marker": f"m{tg['seq']}"})
            self.j.append("owner.file", None, {"path": path, "caption": "here", **S, "sender": "hub"})
            self.assertEqual(self.photos(cfg, 2), ["sendPhoto"])

    # --- a result with owner_reply -----------------------------------------------------------------------------------
    def task_from_page(self):
        self.session("w")
        word = self.from_web("make the icons")
        task = self.j.append("delegation.created", None, {"session": "w", "for": word, "sender": "hub", **S})
        self.j.append("turn.started", "w", {"marker": f"m{task['seq']}"})
        self.j.append("turn.result", "w", {"text": "The icons are ready"})
        self.j.append("turn.stopped", "w", {})

    def test_a_result_of_a_page_task_goes_to_telegram_with_the_setting_once(self):
        self.task_from_page()
        got = self.run_(self.ON)
        self.assertEqual(len(got), 1)
        self.assertIn("The icons are ready", got[0])
        self.assertEqual(self.run_(self.ON, 2), [])

    def test_every_result_of_a_page_task_is_a_session_line_with_the_task_not_a_reply(self):
        # nothing of his in Telegram to quote: not "w replies:" (each one would be "the first"), but "w · on the task «…»"
        self.task_from_page()
        first = self.run_(self.ON)
        self.j.append("turn.started", "w", {})
        self.j.append("turn.result", "w", {"text": "Two more icons"})
        self.j.append("turn.stopped", "w", {})
        got = first + self.run_(self.ON)
        self.assertEqual(len(got), 2)
        self.assertIn("The icons are ready", got[0])
        self.assertIn("Two more icons", got[1])
        for line in got:
            self.assertTrue(line.startswith(i18n.t("telegram.by_task", who="<b>w</b>", task="make the icons") + ":\n"), line)

    def test_a_result_of_a_page_task_stays_on_the_page_without_the_setting(self):
        self.task_from_page()
        self.assertEqual(self.run_({}), [])

    def test_his_own_page_message_to_a_session_is_answered_on_the_page_even_with_the_setting(self):
        self.session("w")
        word = self.from_web("how is it going?", to="w")
        self.j.append("turn.started", "w", {"marker": word})
        self.j.append("turn.result", "w", {"text": "All is well"})
        self.j.append("turn.stopped", "w", {})
        self.assertEqual(self.run_(self.ON), [])

    def test_a_turn_without_a_marker_in_his_own_page_talk_with_a_session_stays_on_the_page(self):
        # a background command ended: Claude Code starts a turn without a marker, the chain is still his page message to w
        self.session("w")
        word = self.from_web("how is it going?", to="w")
        self.j.append("turn.started", "w", {"marker": word})
        self.j.append("turn.result", "w", {"text": "Looking now"})
        self.j.append("turn.stopped", "w", {})
        self.j.append("turn.started", "w", {})
        self.j.append("turn.result", "w", {"text": "Looked: all is well"})
        self.j.append("turn.stopped", "w", {})
        self.assertEqual(self.run_(self.ON), [])

    def test_his_talk_with_a_session_begun_in_telegram_is_not_his_page_talk(self):
        # his message to w came from Telegram, and the bot has no thread of it (forgotten: the last 20 only, or a reply to a
        # card): a later turn without a marker that the core counts to him goes with the setting — it is not his page talk
        self.session("w")
        ev = self.j.append("message.received", None, {"to": "w", "text": "how is it going?", "channel": "telegram", "owner": True})
        self.j.append("turn.started", "w", {"marker": f"m{ev['seq']}"})
        self.j.append("turn.result", "w", {"text": "Looking now"})
        self.j.append("turn.stopped", "w", {})
        self.j.append("turn.started", "w", {})
        self.j.append("turn.result", "w", {"text": "Looked: all is well"})
        self.j.append("turn.stopped", "w", {})
        got = self.run_(self.ON)
        self.assertEqual(len(got), 2, got)
        self.assertIn("Looked: all is well", got[1])

    def test_a_late_page_task_result_is_judged_as_of_the_queue(self):
        # the network failed: the result waits in the outbox while the session already gave the next one; on the retry it
        # still goes, once (owner_reply has moved on)
        self.task_from_page()
        self.bot.hub_config = lambda: self.ON
        down = self.bot.api.call
        self.addCleanup(setattr, self.bot.api, "call", down)
        self.bot.api.call = lambda *a, **k: (_ for _ in ()).throw(tg.TgError("network", transient=True))
        self.bot.tick()
        self.j.append("turn.started", "w", {})
        self.j.append("turn.result", "w", {"text": "Two more icons"})
        self.j.append("turn.stopped", "w", {})
        self.bot.tick()
        self.bot.api.call = down
        self.bot.hold_until = 0   # the pause after the failure is over
        got = self.run_(self.ON, 2)
        self.assertEqual(len(got), 2, got)
        self.assertIn("The icons are ready", got[0])
        self.assertIn("Two more icons", got[1])
        self.assertEqual(self.run_(self.ON, 2), [])   # and not a second time

    def test_a_late_page_task_result_keeps_its_verdict_when_the_chain_start_leaves_the_state(self):
        # held by a network failure; meanwhile the task is closed and the core prunes long-delivered messages (the start of the
        # chain too): the retry still knows it was a page task, not his own talk
        self.task_from_page()
        self.bot.hub_config = lambda: self.ON
        down = self.bot.api.call
        self.addCleanup(setattr, self.bot.api, "call", down)
        self.bot.api.call = lambda *a, **k: (_ for _ in ()).throw(tg.TgError("network", transient=True))
        self.bot.tick()
        d = next(iter(self.bot.reader.state["delegations"]))
        word = self.bot.reader.state["delegations"][d]["for"]
        self.j.append("turn.started", "hub", {"marker": word})   # the hub took his word: it is delivered
        self.j.append("turn.result", "hub", {"text": "—"})
        self.j.append("turn.stopped", "hub", {})
        self.j.append("delegation.done", d, {"sender": "hub", **S})
        self.j.append("core.pruned", None, {"before": 10 ** 12, "channel": "core"})
        self.bot.tick()
        self.assertNotIn(word, self.bot.reader.state["messages"])
        self.bot.api.call = down
        self.bot.hold_until = 0
        got = self.run_(self.ON, 2)
        self.assertEqual(len(got), 1, got)
        self.assertIn("The icons are ready", got[0])

    def test_a_dash_and_talk_between_sessions_do_not_go_with_the_setting(self):
        self.session("dir", "w")
        word = self.from_web("one more fix", to="dir")
        self.j.append("turn.started", "dir", {"marker": word})
        self.j.append("turn.result", "dir", {"text": "—"})
        self.j.append("turn.stopped", "dir", {})
        peer = self.j.append("delegation.created", None, {"session": "w", "for_session": "dir", "sender": "dir", **S})
        self.j.append("turn.started", "w", {"marker": f"m{peer['seq']}"})
        self.j.append("turn.result", "w", {"text": "Status: an answer for dir, not for the owner"})
        self.j.append("turn.stopped", "w", {})
        self.assertEqual(self.run_(self.ON), [])

    def test_a_result_of_his_telegram_task_is_one_message_with_the_setting(self):
        self.session("w")
        self.fake.updates = [self.msg("make a logo", 7)]
        self.bot.poll(timeout=0)
        self.ingest()
        tg = [e for e in journal.read(self.j.path) if e["type"] == "message.received"][-1]
        task = self.j.append("delegation.created", None, {"session": "w", "for": f"m{tg['seq']}", "sender": "hub", **S})
        self.j.append("turn.started", "w", {"marker": f"m{task['seq']}"})
        self.j.append("turn.result", "w", {"text": "The logo is ready"})
        self.j.append("turn.stopped", "w", {})
        got = self.run_(self.ON, 2)
        self.assertEqual(len(got), 1)
        self.assertIn("The logo is ready", got[0])


if __name__ == "__main__":
    unittest.main()
