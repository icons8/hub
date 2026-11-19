"""The digest of the owner's open tasks in Telegram (telegram.digest, off by default): the text is built from the core
state (grouping by the owner's task, a status emoji, limits, no names/ids/paths), the bot sends one message every
telegram.digest_minutes and erases the previous one."""
import tests  # noqa: F401 — keeps the machine's settings out (~/.config/hub-core), see tests/__init__.py
import os
import re
import tempfile
import unittest
from unittest import mock

import platforms
from core import forms, i18n, journal, spool
from modules.telegram import bot as tg
from modules.telegram import digest
from tests import test_telegram as base

NOW = 1_800_000_000.0


def session(name, state="busy", **kw):
    if kw.get("gist"):   # a gist the core recorded: with its moment
        kw["gist"] = {"ts": NOW - 60, **kw["gist"]}
    return {"name": name, "state": state, "since": NOW - 3600, "kind": "agent", **kw}


def task(did, session, words, state="working", **kw):
    """The owner's task dN: set by their message mN, the head of the chain whose root is mN."""
    n = did[1:]
    return {"id": did, "session": session, "words": words, "state": state, "for": f"m{n}", "for_session": None,
            "root": f"m{n}", "created": NOW - 7200, "since": NOW - 7200, **kw}


def sub(did, session, parent, root, state="working", **kw):
    """An executor's sub-task: set by a session, has no title and no words of the owner."""
    return {"id": did, "session": session, "context": "Brief with `code` and /some/path/file.py", "state": state,
            "for": None, "for_session": "dev", "parent": parent, "root": root, "created": NOW - 3600,
            "since": NOW - 3600, **kw}


def world(sessions, tasks, messages=None):
    st = {"sessions": {s["name"]: s for s in sessions}, "delegations": {d["id"]: d for d in tasks},
          "messages": {}, "owner": {"deferred": []}, "schedule": {}, "runs": {}}
    for t in tasks:   # the owner's message the chain hangs on
        if t.get("root"):
            st["messages"].setdefault(t["root"], {"id": t["root"], "from_owner": True, "state": "done", "session": "hub",
                                                  "text": t.get("words") or ""})
    st["messages"].update(messages or {})
    return st


def build(st, lang="en"):
    return digest.build(st, lang)


def visible(row):
    """What the reader counts: no list dash and no bold marks."""
    return row[2:].replace("**", "")


def rows(text):
    return [r for r in text.splitlines() if r.startswith("- ")]


class Digest(unittest.TestCase):
    def chain(self):
        """One owner task with two executors under it: one writes, one asks the owner."""
        return world([
            session("dev", "idle"),
            session("ex1", gist={"text": "Grouping: tests first"}, recap={"text": "Writes the grouping"}, delegation="d11"),
            session("ex2", "idle", pending=[{"id": "q1"}], gist={"text": "Emoji: asks which one"},
                    delegation="d12")],
            [task("d1", "dev", "Digest by tasks", title="Digest by tasks"),
             sub("d11", "ex1", "d1", "m1"), sub("d12", "ex2", "d1", "m1")])

    def test_grouped_by_the_owners_task_not_by_status(self):
        text = build(self.chain())
        self.assertEqual(text.splitlines(), [
            "**📋 Open tasks · 1**", "",
            "**Digest by tasks**",
            "- ⏭ **Emoji**: asks which one → waiting for your word",
            "- 🔄 **Grouping**: tests first"])

    def test_a_sub_task_without_a_gist_is_named_by_its_stage(self):
        st = world([session("dev", "idle"), session("ex", recap={"text": "Adds the option to the bot and checks delivery"},
                                                    delegation="d11")],
                   [task("d1", "dev", "Notify me", title="Notify me"), sub("d11", "ex", "d1", "m1")])
        self.assertEqual(rows(build(st)), ["- 🔄 **Adds the option** to the bot and checks delivery"])

    def test_a_task_without_sub_tasks_is_one_row(self):
        st = world([session("dev", recap={"text": "Fixing the bot"}, delegation="d1")], [task("d1", "dev", "Fix the bot")])
        self.assertEqual(build(st).splitlines(), ["**📋 Open tasks · 1**", "", "- 🔄 **Fix the bot**: Fixing the bot"])

    def test_every_status_has_its_own_emoji(self):
        st = world([session("dev", "idle"), session("ask", "idle", pending=[{"id": "q"}]), session("w", delegation="d2"),
                    session("busy-one", delegation="d99"), session("zzz", "asleep"), session("dead", "removed"),
                    session("bad", "failed"), session("front", "idle", kind="hub")], [
            task("d1", "ask", "Asked"), task("d2", "w", "Working"), task("d3", "busy-one", "Taken"),
            task("d4", "dev", "Waiting for nothing"), task("d9", "front", "Nobody took it"), task("d5", "zzz", "Sleeping"),
            task("d6", "dead", "Orphan"), task("d7", "bad", "Crashed"),
            task("d8", "dev", "Replied for the owner", state="waiting-owner")])
        st["delegations"]["d99"] = task("d99", "busy-one", "Other work", title="Other work", **{"for": None})
        with mock.patch.object(digest, "TASKS", 20):
            by = {re.search(r"\*\*(.+?)\*\*", r).group(1): r[2:] for r in rows(build(st))}
        self.assertTrue(by["Asked"].startswith("⏭ "), by)
        self.assertTrue(by["Replied for the owner"].startswith("⏭ "), by)
        self.assertTrue(by["Taken"].startswith("⏳ "), by)
        self.assertIn("Other work", by["Taken"])
        self.assertTrue(by["Working"].startswith("🔄 "), by)
        self.assertTrue(by["Sleeping"].startswith("⏸ "), by)
        self.assertIn("session asleep", by["Sleeping"])
        self.assertTrue(by["Orphan"].startswith("⚠️ "), by)
        self.assertIn("no session", by["Orphan"])
        self.assertTrue(by["Crashed"].startswith("⚠️ "), by)

    def test_stuck_shows_the_reason_and_the_date(self):
        st = world([session("ops", "idle")], [task("d1", "ops", "Stuck", nudges=1, nudged_at=NOW - 600)])
        with mock.patch.object(digest.delegations, "stuck", return_value=NOW - 600):
            self.assertEqual(rows(build(st)), [f"- ⏸ **Stuck**: the core is nudging · since {digest.when(NOW - 600, st)}"])
            st["delegations"]["d1"].update(nudges=3, timed_out=True)
            self.assertIn("the hub decides", rows(build(st))[0])

    def test_chains_with_a_blocker_come_first(self):
        st = world([session("a"), session("ex", "idle", pending=[{"id": "q"}], gist={"text": "Emoji: pick"}, delegation="d21")],
                   [task("d1", "a", "Going", title="Going"), task("d2", "a", "Has a waiting sub-task",
                    title="Has a waiting sub-task"), sub("d21", "ex", "d2", "m2")])
        st["sessions"]["a"]["delegation"] = "d1"
        self.assertEqual(build(st).splitlines()[2:6], [
            "**Has a waiting sub-task**", "- ⏭ **Emoji**: pick → waiting for your word", "", "- 🔄 **Going**: in progress"])

    def test_only_open_work_is_shown(self):
        st = world([session("dev"), session("ex1"), session("ex2"), session("ex3"), session("ex4")], [
            task("d1", "dev", "Open task"),
            sub("d11", "ex1", "d1", "m1", state="replied"), sub("d12", "ex2", "d1", "m1", state="done"),
            sub("d13", "ex3", "d1", "m1", state="stopped"), sub("d14", "ex4", "d1", "m1"),
            task("d2", "dev", "Replied task", state="replied"), task("d3", "dev", "Closed", state="done"),
            task("d4", "dev", "Stopped", state="stopped")])
        text = build(st)
        self.assertEqual(text.count("\n- "), 1)          # d14 only: the others are not open
        self.assertTrue(text.startswith("**📋 Open tasks · 1**"))
        for gone in ("Replied task", "Closed", "Stopped"):
            self.assertNotIn(gone, text)

    def test_not_the_owners_chain(self):
        st = world([session("dev"), session("ops")], [
            task("d1", "dev", "Mine"),
            task("d2", "ops", "Set by a session", **{"for": None, "for_session": "dev", "root": "m500"}),
            sub("d3", "ops", None, "m9")], messages={"m500": {"id": "m500", "from_owner": False, "state": "done", "text": "x"},
                                                      "m9": {"id": "m9", "from_owner": False, "state": "done", "text": "y"}})
        st["delegations"]["d3"].pop("parent")
        text = build(st)
        self.assertNotIn("Set by a session", text)
        self.assertEqual(text.splitlines()[0], "**📋 Open tasks · 1**")

    def test_a_sub_task_below_a_sub_task_is_listed_under_the_task(self):
        st = world([session("dev", "idle"), session("ex1", delegation="d11", gist={"text": "Middle: one"}),
                    session("ex2", delegation="d12", gist={"text": "Deep: two"})], [
            task("d1", "dev", "Top task"), sub("d11", "ex1", "d1", "m1"), sub("d12", "ex2", "d11", "m1")])
        self.assertEqual(len(rows(build(st))), 2)

    def test_the_head_without_its_own_words_takes_the_owners_message(self):
        st = world([session("dev"), session("ex", gist={"text": "Covers: downloading"}, delegation="d5")], [
            sub("d5", "ex", None, "m7")], messages={"m7": {"id": "m7", "from_owner": True, "state": "done",
                                                            "text": "[Image #1] " + forms.VOICE + " animated covers are still missing"}})
        st["delegations"]["d5"].pop("parent")
        self.assertEqual(rows(build(st)), ["- 🔄 **Animated covers are still** missing: Covers: downloading"])
        self.assertNotIn("Image", build(st))
        self.assertNotIn("🎤", build(st))

    def test_four_sub_tasks_then_and_n_more(self):
        sessions = [session("dev", "idle")] + [session(f"ex{i}", delegation=f"d1{i}", gist={"text": f"Part {i}: do"})
                                                for i in range(1, 8)]
        tasks = [task("d1", "dev", "Big task")] + [sub(f"d1{i}", f"ex{i}", "d1", "m1", created=NOW - 100 + i)
                                                    for i in range(1, 8)]
        text = build(world(sessions, tasks))
        self.assertEqual(len(rows(text)), 5)
        self.assertEqual(rows(text)[-1], "- and 3 more")

    def test_eight_tasks_then_and_n_more_tasks(self):
        tasks = [task(f"d{i}", "dev", f"Task number {i}: do it", created=NOW - 100 + i) for i in range(1, 12)]
        text = build(world([session("dev")], tasks))
        self.assertEqual(text.splitlines()[0], "**📋 Open tasks · 11**")
        self.assertEqual(len(rows(text)), 9)
        self.assertEqual(rows(text)[-1], "- and 3 more tasks")

    def test_the_heading_of_a_task_is_all_bold(self):
        st = self.chain()
        st["delegations"]["d1"]["title"] = "Motion Gallery: refs as tags"
        self.assertEqual(build(st).splitlines()[2], "**Motion Gallery: refs as tags**")

    def test_a_row_is_up_to_70_characters(self):
        long = "word " * 40
        st = world([session("dev", "idle"), session("ex", delegation="d11", gist={"text": "Name: " + long},
                                                    recap={"text": long})], [
            task("d1", "dev", "Head: " + long), sub("d11", "ex", "d1", "m1"), task("d2", "dev", "Alone: " + long)])
        text = build(st)
        self.assertGreaterEqual(len(rows(text)), 2)
        for r in rows(text):
            self.assertLessEqual(len(visible(r)), digest.ROW, r)
        for line in text.splitlines():
            self.assertLessEqual(len(line.replace("**", "")), digest.ROW + 3, line)

    def test_no_session_names_ids_paths_or_code(self):
        st = world([session("secret-session", "idle"), session("secret-exec", gist=None, recap=None, delegation="d11")],
                   [task("d1", "secret-session", "Fix the bot [hub:m42] see d12345 and **more**"),
                    sub("d11", "secret-exec", "d1", "m1")])
        text = build(st)
        self.assertIn("**Sub-task**", text)       # no gist and no words: a plain label, not the brief
        for bad in ("secret-session", "secret-exec", "d12345", "m42", "hub:", "**more", "/some/path", "`", "file.py", "d11"):
            self.assertNotIn(bad, text)

    def test_voice_mark_and_nothing_open(self):
        st = world([session("dev", "idle", kind="hub")], [task("d1", "dev", forms.VOICE + " Make a digest")])
        self.assertEqual(rows(build(st)), ["- ⏸ **Make a digest**: no result yet"])
        self.assertEqual(build(world([], [])), "")
        self.assertEqual(build(world([session("dev")], [task("d1", "dev", "x", state="done")])), "")


class Review(unittest.TestCase):
    """Points of the review of the first version: each failed on it."""

    def test_an_open_sub_task_under_a_closed_one_is_shown(self):
        st = world([session("dev", "idle"), session("ex1", "idle"), session("ex2", delegation="d111",
                                                                          gist={"text": "Deep: still going"})],
                   [task("d1", "dev", "Top task"), sub("d11", "ex1", "d1", "m1", state="replied"),
                    sub("d111", "ex2", "d11", "m1")])
        self.assertIn("**Deep**", build(st))

    def test_a_sub_task_without_a_title_does_not_take_its_sessions_other_work(self):
        st = world([session("dev", "idle"), session("ex", gist={"text": "Other job: elsewhere"},
                                                    recap={"text": "Other job recap"}, delegation="d99")],
                   [task("d1", "dev", "Top task", title="Top task"), sub("d11", "ex", "d1", "m1")])
        st["delegations"]["d99"] = task("d99", "ex", "Other job", title="Other job", **{"for": None, "root": None})
        text = build(st)
        self.assertIn("**Sub-task**", text)
        self.assertNotIn("Other job:", text.replace("busy with Other job", ""))
        self.assertNotIn("elsewhere", text)
        self.assertNotIn("recap", text)

    def test_a_one_row_task_waiting_for_the_owner_comes_before_a_working_chain(self):
        st = world([session("dev", "idle"), session("w", delegation="d11", gist={"text": "Part: goes"}),
                    session("ask", "idle")],
                   [task("d1", "dev", "Chain", title="Chain", created=NOW - 9000), sub("d11", "w", "d1", "m1"),
                    task("d2", "ask", "Asks you", state="waiting-owner")])
        body = build(st).splitlines()[2:]
        self.assertTrue(body[0].startswith("- ⏭ **Asks you**"), body)

    def test_busy_with_another_task_is_not_the_owners_emoji(self):
        st = world([session("busy-one", delegation="d99")], [task("d3", "busy-one", "Taken")])
        st["delegations"]["d99"] = task("d99", "busy-one", "Other work", title="Other work", **{"for": None, "root": None})
        row = rows(build(st))[0]
        self.assertIn("Other work", row)
        self.assertFalse(row.startswith("- ⏭"), row)

    def test_statuses_are_the_pages_ones(self):
        retired = world([session("ex", "retired")], [task("d1", "ex", "Handed in")])
        self.assertIn("session asleep", rows(build(retired))[0])
        card = world([session("ex", "idle", asks=[{"id": "q5", "request_id": "r5", "tool": "AskUserQuestion",
                                                   "since": NOW, "delegation": "d1",
                                                   "input": {"questions": [{"question": "Which emoji?"}]}}])],
                     [task("d1", "ex", "Asks by card")])
        self.assertTrue(rows(build(card))[0].startswith("- ⏭ "), build(card))

    def test_a_stale_gist_is_not_shown(self):
        st = world([session("dev", delegation="d1", gist={"text": "Old: stale words", "ts": NOW - 900, "turn": 1},
                            out_turn=2, last_out=NOW, owner_out=NOW - 1000)], [task("d1", "dev", "Fix the bot")])
        self.assertNotIn("stale words", build(st))

    def test_work_after_a_card_answer_is_not_headed_by_the_relay(self):
        for relay in ("The user's answer to question q12 (the question is stale): ship the second one",
                      "Answer to question q12 (\"Which one?\"): ship the second one"):
            st = world([session("dev", recap={"text": "Ships it"}, delegation="d1")], [task("d1", "dev", relay)])
            self.assertEqual(rows(build(st)), ["- 🔄 **Ship the second one**: Ships it"], relay)

    def test_waiting_for_the_owner_shows_the_question_of_the_card(self):
        st = world([session("ex", "idle", asks=[{"id": "q5", "request_id": "r5", "tool": "AskUserQuestion",
                                                 "since": NOW, "delegation": "d8",
                                                 "input": {"questions": [{"question": "Which emoji fits"}]}}])],
                   [task("d8", "ex", "Pick an emoji", state="waiting-owner", reply="Need from you: something else")])
        row = rows(build(st))[0]
        self.assertIn("Which emoji fits", row)
        self.assertNotIn("something else", row)


class ReviewOfTheCarry(unittest.TestCase):
    """What the review of the carry onto main found: each red on the code of the PR."""

    def test_a_card_of_the_sessions_other_task_does_not_make_this_one_wait_for_the_owner(self):
        st = world([session("dev", "idle", pending=[{"id": "q", "tool": "AskUserQuestion"}], delegation="d2")],
                   [task("d1", "dev", "Old work"), task("d2", "dev", "New work")])
        by = {re.search(r"\*\*(.+?)\*\*", r).group(1): r[2:] for r in rows(build(st))}
        self.assertTrue(by["New work"].startswith("⏭ "), by)
        self.assertTrue(by["Old work"].startswith("⏳ "), by)

    def test_a_card_of_this_task_counts_while_the_session_is_on_another(self):
        st = world([session("dev", "idle", delegation="d2")], [task("d1", "dev", "Old work"), task("d2", "dev", "New work")])
        with mock.patch.object(digest.owner, "open_asks", return_value=[{"id": "q", "tool": "AskUserQuestion", "delegation": "d1"}]):
            by = {re.search(r"\*\*(.+?)\*\*", r).group(1): r[2:] for r in rows(build(st))}
        self.assertTrue(by["Old work"].startswith("⏭ "), by)

    def test_a_big_digest_is_one_message(self):
        long = "Step with a long description of what the executor is doing right now and why it matters"
        tasks, sessions = [], [session("dev", "idle")]
        for n in range(1, 9):
            tasks.append(task(f"d{n}", "dev", f"Task number {n} " + long, title=f"Task number {n} " + long,
                              state="waiting-owner"))
            for k in range(1, 6):
                sid = f"d{n}{k}"
                sessions.append(session(f"x{sid}", "idle", pending=[{"id": "q" + sid}], delegation=sid))
                tasks.append(sub(sid, f"x{sid}", f"d{n}", f"m{n}", words=long))
        text = build(world(sessions, tasks))
        self.assertLessEqual(len(text), 3200)
        self.assertIn("more tasks", text)

    def test_a_form_answer_heads_the_task_by_its_question_without_the_dash(self):
        st = world([session("dev", recap={"text": "Fixing"}, delegation="d1")], [task("d1", "dev", "")])
        st["messages"]["m1"]["text"] = "Answer to question q123:\n— Ship the fix today?\nYes"
        self.assertTrue(rows(build(st))[0].startswith("- 🔄 **Ship the fix today"), build(st))

    def test_a_merge_request_number_keeps_its_space(self):
        self.assertEqual(digest.clean("merge !483 then !484, done"), "merge !483 then !484, done")
        self.assertEqual(digest.clean("see `x`). Done !"), "see). Done!")

    def test_a_long_word_does_not_stretch_the_row(self):
        row = digest.row("Investigate-" + "x" * 80 + ": details", "busy with another task")
        self.assertLessEqual(len(row.replace("**", "")), digest.ROW + 2, row)


class Language(unittest.TestCase):
    """Texts go to Telegram in the hub language: English by default, Russian when the hub speaks Russian."""

    def st(self):
        return world([session("dev", "idle"), session("ex", "idle", pending=[{"id": "q"}], gist={"text": "Emoji: pick"},
                                              delegation="d11")],
                     [task("d1", "dev", "Fix the bot", title="Fix the bot"), sub("d11", "ex", "d1", "m1")])

    def test_the_language_is_the_hubs(self):
        with mock.patch.object(i18n, "current", return_value="ru"):
            self.assertEqual(digest.build(self.st()).splitlines(), [
                digest.tr("ru", "head", n=1), "", "**Fix the bot**",
                "- ⏭ **Emoji**: pick → " + digest.tr("ru", "owner")])
        with mock.patch.object(i18n, "current", return_value="en"):
            self.assertEqual(digest.build(self.st()).splitlines()[0], "**📋 Open tasks · 1**")

    def test_an_unknown_language_falls_back_to_english(self):
        self.assertEqual(build(self.st(), "nl").splitlines()[0], "**📋 Open tasks · 1**")   # no catalog
        self.assertEqual(build(self.st(), "RU").splitlines()[0], build(self.st(), "ru").splitlines()[0])   # any case
        self.assertNotEqual(build(self.st(), "ru").splitlines()[0], "**📋 Open tasks · 1**")

    def test_both_catalogs_have_the_same_keys_and_placeholders(self):
        en, ru = i18n.catalog("en"), i18n.catalog("ru")
        keys = {k for k in en if k.startswith("telegram.digest.")}
        self.assertTrue(keys)
        self.assertEqual(keys, {k for k in ru if k.startswith("telegram.digest.")})
        for k in keys:
            self.assertEqual(set(re.findall(r"\{(\w+)\}", en[k])), set(re.findall(r"\{(\w+)\}", ru[k])), k)


class Bot(unittest.TestCase):
    """The bot: off by default, one message every telegram.digest_minutes, the new one erases the previous one."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hubtg-digest-")
        self.state = os.path.join(self.dir, "state")
        self.old_cfg = os.environ.get("HUB_TELEGRAM_CONFIG")
        os.environ["HUB_TELEGRAM_CONFIG"] = os.path.join(self.dir, "telegram.json")
        tg.save_config({"token": "1:x", "owners": [base.OWNER]})
        self.fake = base.FakeBotApi()
        self.j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        self.j.append("session.requested", "hub", {"cwd": "/w", "session_id": "h", "kind": "hub"})
        self.j.append("session.started", "hub", {"session_id": "h"})
        self.bot = tg.Bot(self.state, tg.Api("1:x", base=self.fake.base))
        self.bot.catch_up()
        self.config = {}
        self.bot.hub_config = lambda: {"telegram": dict(self.config)}
        self.now = 1_800_000_000.0
        self.bot.clock = lambda: self.now
        self.fake.updates = [self.msg("fix the bot", 5)]
        self.bot.poll(timeout=0)
        self.ingest()                                   # m3: the owner's message, the chat is known
        self.j.append("session.requested", "w", {"cwd": "/w", "session_id": "w"})
        self.j.append("session.started", "w", {"session_id": "w"})
        self.j.append("delegation.created", None, {"session": "w", "for": "m3", "channel": "session", "owner": False,
                                                   "sender": "hub"})
        self.bot.tick()
        self.fake.calls.clear()

    def tearDown(self):
        self.fake.server.shutdown()
        self.fake.server.server_close()
        os.environ["HUB_TELEGRAM_CONFIG"] = self.old_cfg or ""
        platforms.current.rmtree(self.dir)

    def msg(self, text, mid):
        return {"update_id": mid, "message": {"message_id": mid, "from": {"id": base.OWNER},
                                              "chat": {"id": base.OWNER, "type": "private"}, "text": text}}

    def ingest(self):
        """Like hubd: facts from the inbox go to the journal."""
        d = os.path.join(self.state, "spool")
        for _, e in spool.take(d):
            self.j.append(e["type"], e.get("entity"), e.get("data"), src=e.get("src"))
        for n in os.listdir(d) if os.path.isdir(d) else []:
            os.unlink(os.path.join(d, n))

    def set(self, config):
        """The owner changed hub.json; the bot rereads it every 5 seconds."""
        self.config = config
        self.now += 6

    def digests(self):
        return [b["text"] for b in self.fake.sent() if b["text"].startswith("<b>📋")]

    def erased(self):
        return [b["message_id"] for b in self.fake.sent("deleteMessage")]

    def test_off_by_default_nothing_is_sent(self):
        for _ in range(3):
            self.now += 3600
            self.bot.tick()
        self.assertEqual(self.fake.calls, [])
        self.set({"digest": False})
        self.bot.tick()
        self.assertEqual(self.fake.calls, [])

    def test_on_one_digest_with_the_grouping(self):
        self.set({"digest": True})
        self.bot.tick()
        self.bot.tick()
        self.assertEqual(len(self.digests()), 1)
        self.assertIn(digest.tr(i18n.current(), "head", n=1).strip("*"), self.digests()[0])
        self.assertEqual(self.erased(), [])
        self.assertTrue(all(b.get("disable_notification") for b in self.fake.sent()))   # a periodic notice: no sound

    def test_the_next_one_comes_after_the_minutes_and_erases_the_previous(self):
        self.set({"digest": True, "digest_minutes": 10})
        self.bot.tick()
        first = self.fake.next_id
        self.now += 9 * 60
        self.bot.tick()
        self.assertEqual((len(self.digests()), self.erased()), (1, []))
        self.now += 2 * 60
        self.bot.tick()
        self.assertEqual(len(self.digests()), 2)
        self.assertEqual(self.erased(), [first])
        self.assertEqual(self.bot.st["digest"]["ids"], [self.fake.next_id])

    def test_minutes_are_kept_within_five_and_a_day(self):
        self.set({"digest": True, "digest_minutes": 1})
        self.bot.tick()
        self.now += 4 * 60
        self.bot.tick()
        self.assertEqual(len(self.digests()), 1)
        self.now += 2 * 60
        self.bot.tick()
        self.assertEqual(len(self.digests()), 2)

    def test_after_an_erase_the_interval_still_holds(self):
        self.set({"digest": True, "digest_minutes": 10})
        self.bot.tick()
        first = self.fake.next_id
        self.j.append("delegation.done", "d6", {"text": "done"})
        self.now += 60
        self.bot.tick()
        self.assertEqual((len(self.digests()), self.erased()), (1, [first]))
        self.j.append("delegation.created", None, {"session": "w", "for": "m3", "channel": "session", "owner": False,
                                                   "sender": "hub"})
        self.now += 60
        self.bot.tick()
        self.assertEqual(len(self.digests()), 1)        # two minutes after the last one, not ten
        self.now += 9 * 60
        self.bot.tick()
        self.assertEqual(len(self.digests()), 2)

    def test_a_failed_erase_is_retried(self):
        self.set({"digest": True, "digest_minutes": 10})
        self.bot.tick()
        first = self.fake.next_id
        self.fake.fail["deleteMessage"] = [502]
        self.now += 11 * 60
        self.bot.tick()
        self.assertEqual((len(self.digests()), self.erased()), (2, []))
        self.now += 61
        self.bot.tick()
        self.assertEqual(self.erased(), [first])

    def test_the_digest_is_not_built_on_every_tick(self):
        self.set({"digest": True})
        with mock.patch.object(digest, "build", wraps=digest.build) as built:
            for _ in range(10):
                self.bot.tick()
                self.now += 3
        self.assertEqual(built.call_count, 1)

    def test_a_fault_in_the_digest_does_not_stop_the_bot(self):
        self.set({"digest": True})
        with mock.patch.object(digest, "build", side_effect=KeyError("ts")), mock.patch("sys.stderr"):
            self.bot.tick()
            self.now += 61
            self.bot.tick()
        self.assertEqual(self.digests(), [])
        self.now += 61
        self.bot.tick()
        self.assertEqual(len(self.digests()), 1)

    def test_switched_off_or_nothing_open_erases_the_last_one_once(self):
        self.set({"digest": True})
        self.bot.tick()
        mid = self.fake.next_id
        self.set({"digest": False})
        self.now += 60
        self.bot.tick()
        self.bot.tick()
        self.assertEqual((len(self.digests()), self.erased()), (1, [mid]))
        self.set({"digest": True})
        self.now += 3600
        self.bot.tick()
        mid = self.fake.next_id
        self.assertEqual(len(self.digests()), 2)
        self.j.append("delegation.done", "d6", {"text": "done"})
        self.now += 3600
        self.bot.tick()
        self.bot.tick()
        self.assertEqual(len(self.digests()), 2)
        self.assertEqual(self.erased()[-1], mid)
        self.assertEqual(len(self.erased()), 2)

    def test_a_restart_does_not_send_it_again(self):
        self.set({"digest": True})
        self.bot.tick()
        self.bot.save()
        again = tg.Bot(self.state, tg.Api("1:x", base=self.fake.base), clock=lambda: self.now + 60)
        again.hub_config = self.bot.hub_config
        again.catch_up()
        again.tick()
        self.assertEqual(len(self.digests()), 1)

    def test_a_deaf_bot_or_an_unknown_chat_stays_quiet(self):
        self.set({"digest": True})
        self.bot.cfg["receive"] = False
        self.bot.tick()
        self.assertEqual(self.digests(), [])

    def test_a_telegram_failure_does_not_break_the_bot_and_is_retried(self):
        self.set({"digest": True})
        self.fake.fail["sendMessage"] = [502, 502, 502, 502, 502]
        self.bot.tick()
        self.assertEqual(self.digests(), [])
        self.bot.tick()                                  # not sooner than a minute
        self.assertEqual(self.digests(), [])
        self.fake.fail["sendMessage"] = []
        self.now += 61
        self.bot.tick()
        self.assertEqual(len(self.digests()), 1)

    def test_a_refusal_of_telegram_is_retried_in_an_hour_not_every_minute(self):
        self.set({"digest": True})
        self.fake.fail["sendMessage"] = [403] * 5
        self.bot.tick()
        self.fake.fail["sendMessage"] = []
        self.now += 61
        self.bot.tick()
        self.assertEqual(self.digests(), [])
        self.now += 3600
        self.bot.tick()
        self.assertEqual(len(self.digests()), 1)

    def test_a_refusal_does_not_hold_turning_it_off(self):
        self.set({"digest": True})
        self.bot.tick()
        first = self.fake.next_id
        self.now += 31 * 60
        self.fake.fail["sendMessage"] = [403] * 5
        self.bot.tick()                                   # the next one refused
        self.fake.fail["sendMessage"] = []
        self.set({"digest": False})
        self.bot.tick()
        self.assertEqual(self.erased(), [first])



class SpecOn(unittest.TestCase):
    def test_a_session_with_spec_mode_on_has_its_status(self):
        # release review 2026-11-09: `/spec on` stores True, not a dict — the digest raised and stopped
        from core import views
        self.assertEqual(views.session_status({"state": "idle", "spec": True}), "idle")
        self.assertEqual(views.session_status({"state": "idle", "spec": {"ready": True}}), "waiting")


if __name__ == "__main__":
    unittest.main()
