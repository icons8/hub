"""The hub's Telegram group (spec d2513429): the bot works in one group instead of the owner's private chat. An admin of
the group is the owner (his words and presses are the owner's, named in tg_by); any other member only asks (his words
go with owner false and member, nothing else of his is a fact); the rights are checked fresh on every word and press,
and a failed check gives nobody the owner's rights. A topic of the group can be given to a session (/here). The group is
bound by a code from its admin and `hub-telegram pair` in the hub's terminal, which shows first and writes after."""
import tests  # noqa: F401 — keeps the machine's settings out
import html
import importlib
import io
import os
import unittest
from unittest import mock

from core import journal, reducer, views
from modules.telegram import bot as tg
from tests import test_telegram as base

GROUP, OTHER = -1001, -2002
ADMIN, MEMBER, BOT, STRANGER = 777, 555, 4242, 888
ME = {"id": BOT, "username": "test_bot", "can_read_all_group_messages": False}


def admins(*ids, promote=False):
    """getChatAdministrators of a group: the creator, admins by id, and the bot itself as an admin."""
    rows = [{"status": "creator" if i == ids[0] else "administrator", "user": {"id": i, "first_name": f"P{i}"}}
            for i in ids]
    return rows + [{"status": "administrator", "user": {"id": BOT, "is_bot": True, "first_name": "bot"},
                    "can_promote_members": promote, "can_delete_messages": True}]


class Group(unittest.TestCase):
    # helpers by copy, not inheritance: a subclass of TelegramTest would run all its tests again
    T = base.TelegramTest
    tearDown, spooled, ingest = T.tearDown, T.spooled, T.ingest

    def setUp(self):
        self.T.setUp(self)
        for name in ("maps",):
            self.j.append("session.requested", name, {"cwd": "/w", "session_id": name, "channel": "web", "owner": True})
            self.j.append("session.started", name, {"session_id": name})
        self.fake.results.update({"getMe": ME, "getChatAdministrators": lambda body: admins(ADMIN)})
        tg.save_config({"token": "1:x", "owners": [ADMIN], "group": {"id": GROUP, "title": "maps team"}})
        self.bot.cfg = tg.load_config()
        self.bot.tick()
        self.ingest()   # the bot's telegram.group (a group is bound) — into the journal, as hubd does

    def gmsg(self, text, mid, user=ADMIN, thread=None, reply_to=None, chat=GROUP, **extra):
        m = {"message_id": mid, "from": {"id": user, "first_name": f"P{user}"}, "chat": {"id": chat, "type": "supergroup"},
             "text": text, **extra}
        if thread:
            m.update(message_thread_id=thread, is_topic_message=True)
        if reply_to:
            m["reply_to_message"] = {"message_id": reply_to}
        return {"update_id": mid, "message": m}

    def poll(self, *updates):
        self.fake.updates = list(updates)
        self.bot.poll(timeout=0)

    def press(self, data, user, mid=900, chat=GROUP):
        self.poll({"update_id": mid, "callback_query": {"id": f"cq{mid}", "from": {"id": user, "first_name": f"P{user}"},
                                                        "data": data, "message": {"message_id": 1, "chat": {"id": chat}}}})

    def notes(self):
        return [b.get("text") for b in self.fake.sent("answerCallbackQuery")]

    # --- who is who --------------------------------------------------------------------------------------------------
    def test_an_admin_speaks_as_the_owner_and_is_named(self):
        self.poll(self.gmsg("render it", 5))
        got = self.spooled()
        self.assertEqual([(e["type"], e["data"]["owner"], e["data"]["tg_by"]) for e in got],
                         [("message.received", True, {"tg": ADMIN, "name": f"P{ADMIN}"})])
        self.assertNotIn("member", got[0]["data"])

    def test_a_member_only_asks(self):
        self.poll(self.gmsg("what is the status?", 5, user=MEMBER))
        got = self.spooled()
        self.assertEqual([(e["type"], e["data"]["owner"], e["data"]["member"]) for e in got],
                         [("message.received", False, {"tg": MEMBER, "name": f"P{MEMBER}"})])
        self.assertNotIn("tg_by", got[0]["data"])

    def test_strangers_bots_channels_and_service_messages_are_not_heard(self):
        bot = self.gmsg("hi", 6)
        bot["message"]["from"]["is_bot"] = True
        channel = self.gmsg("post", 7, sender_chat={"id": -5, "type": "channel"})
        auto = self.gmsg("post", 8, is_automatic_forward=True)
        joined = self.gmsg("", 9, new_chat_members=[{"id": 1}])
        self.poll(self.gmsg("hi", 5, chat=OTHER), bot, channel, auto, joined,
                  base.TelegramTest.msg(self, "private words", 10, user=ADMIN))
        self.assertEqual(self.spooled(), [])
        self.assertEqual(self.fake.sent(), [])

    def test_an_anonymous_admin_is_asked_to_speak_as_himself(self):
        self.poll(self.gmsg("do it", 5, sender_chat={"id": GROUP, "type": "supergroup"}))
        self.assertEqual(self.spooled(), [])
        self.assertIn("anonym", self.fake.last_text().lower())

    def test_a_person_forwarding_a_message_speaks_in_his_own_role(self):
        self.poll(self.gmsg("look at this", 5, user=MEMBER, forward_origin={"type": "channel", "chat": {"id": -9}}))
        self.assertEqual(self.spooled()[0]["data"]["member"]["tg"], MEMBER)

    def test_the_rights_are_fresh_on_every_word(self):
        self.poll(self.gmsg("one", 5))
        self.assertTrue(self.spooled()[0]["data"]["owner"])
        self.fake.results["getChatAdministrators"] = lambda body: admins(MEMBER)   # he is no longer an admin
        self.poll(self.gmsg("two", 6))
        data = self.spooled()[0]["data"]
        self.assertEqual((data["owner"], data["member"]["tg"]), (False, ADMIN))
        self.assertTrue([t for t in self.fake.texts() if "P555" in t])   # the group is told who runs the hub now

    def test_a_failed_check_gives_nobody_the_owners_rights(self):
        self.fake.results.pop("getChatAdministrators")
        self.fake.fail["getChatAdministrators"] = [502, 502]
        self.poll(self.gmsg("deploy", 5))
        data = self.spooled()[0]["data"]
        self.assertEqual((data["owner"], data["member"]["tg"]), (False, ADMIN))
        self.press("k:whatever", ADMIN)
        self.assertEqual(self.spooled(), [])
        self.assertEqual(len(self.notes()), 1)

    def test_words_reach_the_hub_even_when_the_line_about_the_check_fails(self):
        # the check failed and so did the line about it: the words were lost (the update number had moved on)
        self.fake.results.pop("getChatAdministrators")
        self.fake.fail.update(getChatAdministrators=[502], sendMessage=[(400, "Bad Request: chat not found")])
        self.poll(self.gmsg("deploy", 5))
        self.assertEqual([e["data"]["text"] for e in self.spooled()], ["deploy"])

    def test_a_members_words_for_a_session_that_is_gone_go_to_the_hub(self):
        # the core passes the owner's words for a gone session to the hub, not a member's: they failed silently
        self.bot.st["routes"]["50"] = {"session": "gone-one"}
        self.poll(self.gmsg("is it done?", 5, user=MEMBER, reply_to=50))
        data = self.spooled()[0]["data"]
        self.assertNotIn("to", data)
        self.assertEqual(data["member"]["tg"], MEMBER)

    def test_the_owners_answers_to_people_do_not_go_out_from_a_group(self):
        self.bot.st["business"] = {"on": True, "id": "c1"}
        self.bot.business.send("o1", {"conv": "12", "text": "hi", "reply_to": "3"})
        got = self.spooled()
        self.assertEqual([e["type"] for e in got], ["chat.send_failed"])
        self.assertEqual(self.fake.sent(), [])

    def test_a_bot_that_may_appoint_admins_makes_nobody_an_admin(self):
        self.fake.results["getChatAdministrators"] = lambda body: admins(ADMIN, promote=True)
        self.poll(self.gmsg("one", 5), self.gmsg("two", 6))
        self.assertEqual([e["data"]["owner"] for e in self.spooled()], [False, False])
        warned = [t for t in self.fake.texts() if t == tg.i18n.t("telegram.group.bot_promotes")]
        self.assertEqual(len(warned), 1)   # said once, not on every word

    # --- presses and commands ------------------------------------------------------------------------------------------
    def ask(self):
        self.j.append("permission.requested", "hub", {"request_id": "r1", "tool": "AskUserQuestion", "interactive": True,
                                                      "input": {"questions": [{"question": "Ship it?", "options": [
                                                          {"label": "Yes"}, {"label": "No"}]}]}})
        self.bot.tick()
        ask = next(iter(self.bot.st["asks"].values()))
        key = next(k for k, v in self.bot.st["keys"].items() if v.get("label") == "Yes")
        return ask, f"k:{key}"

    def test_on_a_hub_nobody_sits_at_a_form_still_comes_to_the_group(self):
        """Bus mode remote refuses forms — except where a group is bound: its admins are the owner (the same rule as the
        core's, busstate.remote_form)."""
        self.bot.tick()
        self.bot.reader.state.setdefault("bus", {}).update(mode="remote", enabled=True)
        self.ask()
        self.assertEqual(self.fake.sent()[-1]["chat_id"], GROUP)
        self.assertIn("Ship it?", self.fake.sent()[-1]["text"])

    def test_only_an_admin_confirms(self):
        ask, yes = self.ask()
        self.assertEqual(self.fake.sent()[-1]["chat_id"], GROUP)
        self.press(yes, MEMBER)
        self.assertEqual(self.spooled(), [])
        self.assertEqual(self.notes(), [tg.i18n.t("telegram.group.press_admins")])
        self.press(yes, ADMIN, mid=901)
        got = self.spooled()
        self.assertEqual([(e["type"], e["data"]["tg_by"]["tg"]) for e in got], [("permission.answered", ADMIN)])

    def test_a_members_words_to_a_card_are_not_its_answer(self):
        ask, _ = self.ask()
        self.poll(self.gmsg("the second one", 5, user=MEMBER, reply_to=ask["msgs"][-1]))
        got = self.spooled()
        self.assertEqual([e["type"] for e in got], ["message.received"])
        self.assertEqual((got[0]["data"]["owner"], got[0]["data"]["to"]), (False, "hub"))
        self.assertFalse(ask.get("done"))

    def test_a_member_may_ask_the_status_but_not_steer(self):
        self.poll(self.gmsg("/stop", 5, user=MEMBER), self.gmsg("/away 2", 6, user=MEMBER))
        self.assertEqual(self.spooled(), [])
        self.assertEqual(self.fake.texts()[-2:], [tg.i18n.t("telegram.group.admins_only")] * 2)
        self.poll(self.gmsg("/status", 7, user=MEMBER))
        self.assertNotEqual(self.fake.last_text(), tg.i18n.t("telegram.group.admins_only"))

    def test_a_member_puts_nothing_but_words(self):
        # the last line behind every refusal above: whatever path reaches put() for a member, only words come out
        self.bot.speaker = {"role": "member", "tg": MEMBER, "name": "P555"}
        try:
            for kind in ("owner.away", "permission.answered", "session.stop_requested", "tools.approved"):
                self.bot.put(kind, "hub", {})
            self.bot.put("message.received", None, {"text": "hi"})
        finally:
            self.bot.speaker = None
        self.assertEqual([(e["type"], e["data"]["owner"]) for e in self.spooled()], [("message.received", False)])

    def test_a_member_cannot_pass_a_bus_link(self):
        from core import bus
        self.poll(self.gmsg(f"link {bus.LINK_PREFIX}abcdefghijklmnopqrstuvwxyz0123456789", 5, user=MEMBER))
        self.assertEqual(self.spooled(), [])
        self.assertEqual(self.fake.last_text(), tg.i18n.t("telegram.group.no_member_link"))

    def test_the_bus_link_command_does_not_show_a_secret_to_the_group(self):
        self.poll(self.gmsg("/link", 5))
        self.assertEqual(self.fake.last_text(), tg.i18n.t("telegram.group.no_link"))

    def test_the_owners_chats_with_people_are_off_in_the_group(self):
        self.fake.updates = [{"update_id": 5, "business_connection": {"id": "c1", "user": {"id": ADMIN},
                                                                       "is_enabled": True}}]
        with mock.patch.object(self.bot.business, "on_connection") as conn:
            self.bot.poll(timeout=0)
        conn.assert_not_called()

    # --- topics --------------------------------------------------------------------------------------------------------
    def test_a_topic_given_to_a_session_carries_its_words_both_ways(self):
        self.poll(self.gmsg("/here maps", 5, thread=9))
        self.assertEqual(self.bot.st["topics"], {"9": "maps"})
        self.assertEqual(self.fake.sent()[-1].get("message_thread_id"), 9)
        self.poll(self.gmsg("render three", 6, thread=9))
        self.ingest()
        last = journal.read(self.j.path)[-1]
        self.assertEqual(last["data"]["to"], "maps")
        self.assertEqual(self.bot.where(session="maps"), {"chat_id": GROUP, "message_thread_id": 9})
        self.assertEqual(self.bot.where(), {"chat_id": GROUP})   # the rest — General
        mid = f"m{last['seq']}"
        self.j.append("turn.started", "maps", {"marker": mid})
        self.j.append("turn.result", "maps", {"text": "three renders are ready"})
        self.bot.tick()
        answer = [b for b in self.fake.sent() if "three renders" in b.get("text", "")]
        self.assertEqual([(b["chat_id"], b.get("message_thread_id")) for b in answer], [(GROUP, 9)])

    def test_a_member_cannot_bind_a_topic_and_an_executor_is_not_bound(self):
        self.poll(self.gmsg("/here maps", 5, user=MEMBER, thread=9))
        self.assertEqual(self.bot.st["topics"], {})
        self.bot.reader.state["sessions"]["maps"]["role"] = "executor"
        self.poll(self.gmsg("/here maps", 6, thread=9))
        self.assertEqual(self.bot.st["topics"], {})
        self.poll(self.gmsg("/here", 7))
        self.assertEqual(html.unescape(self.fake.last_text()), tg.i18n.t("telegram.group.here_general"))

    def test_a_deleted_topic_sends_to_general_and_unbinds(self):
        self.bot.st["topics"] = {"9": "maps"}
        self.fake.fail["sendMessage"] = [(400, "Bad Request: message thread not found")]
        self.bot.send("hello", route={"session": "maps"})
        sent = self.fake.sent()
        self.assertEqual(self.bot.st["topics"], {})
        self.assertTrue(all("message_thread_id" not in b for b in sent))
        self.assertEqual([b["text"] for b in sent],
                         [tg.i18n.t("telegram.group.topic_gone", name="maps"), "hello"])

    def test_a_long_message_to_a_deleted_topic_goes_to_general_whole(self):
        # the first part fell to General, the rest kept knocking at the dead topic, and a reply's topic was the session's
        self.bot.st["topics"] = {"9": "maps"}
        self.fake.fail["sendMessage"] = [(400, "Bad Request: message thread not found")]
        with mock.patch.object(tg.fmt, "to_messages", return_value=["part one", "part two"]):
            ids = self.bot.send("long", route={"session": "maps"})
        sent = self.fake.sent()
        self.assertEqual([b["text"] for b in sent],
                         [tg.i18n.t("telegram.group.topic_gone", name="maps"), "part one", "part two"])
        self.assertTrue(all("message_thread_id" not in b for b in sent))
        self.assertEqual([self.bot.st["topic_of"][str(i)] for i in ids], [None, None])

    def test_a_broken_check_still_passes_the_words_without_rights(self):
        # the check itself broke (not the network): lost() keeps the words, as a member's
        with mock.patch.object(self.bot, "admins", side_effect=KeyError("user")):
            self.poll(self.gmsg("deploy", 5))
        data = self.spooled()[0]["data"]
        self.assertEqual((data["owner"], data["member"]["tg"]), (False, ADMIN))

    def test_lost_words_are_matched_by_chat_too(self):
        # an admin's message 42 in the group, then message 42 of another chat breaks: it is not credited to the admin
        self.poll(self.gmsg("hi", 42))
        self.spooled()
        stranger = self.gmsg("/start", 42, user=MEMBER, chat=OTHER)["message"]
        self.bot.lost(stranger, ValueError("x"))
        self.assertEqual(self.spooled(), [])

    # --- the chat changes --------------------------------------------------------------------------------------------------
    def test_a_new_chat_is_followed_before_its_first_update(self):
        # pair ran while the bot polled: the group's reply to its message 50 met the private chat's route 50
        self.bot.st["chat"] = ADMIN
        self.bot.st["routes"]["50"] = {"session": "maps"}
        self.poll(self.gmsg("which one?", 7, reply_to=50))
        self.assertNotIn("to", self.spooled()[0]["data"])

    def test_a_supergroup_takes_the_bot_with_it(self):
        self.poll(self.gmsg("", 5, migrate_to_chat_id=-1009))
        self.assertEqual(tg.group_of(tg.load_config())["id"], -1009)
        self.bot.cfg = tg.load_config()
        self.bot.tick()
        self.assertEqual(self.fake.sent()[-1]["chat_id"], -1009)

    def test_another_chat_forgets_the_old_numbers_and_gets_the_open_cards(self):
        ask, _ = self.ask()
        self.bot.st["routes"]["1"] = {"session": "maps"}
        tg.save_config({**tg.load_config(), "group": {"id": OTHER, "title": "new"}})   # pair, or the file edited by hand
        self.bot.cfg = tg.load_config()
        self.bot.tick()
        told = [b for b in self.fake.sent() if b["chat_id"] == GROUP]
        self.assertEqual(told[-1]["text"], tg.i18n.t("telegram.group.moved", title="new"))
        self.assertNotIn("1", self.bot.st["routes"])
        self.assertTrue([b for b in self.fake.sent() if b["chat_id"] == OTHER and "Ship it?" in b["text"]])

    def test_the_core_learns_the_group_is_bound_and_unbound(self):
        """The bot tells the core whether a group is bound (telegram.group) — once per change, also for a group bound
        before this code; group off says it is gone."""
        self.bot.tick()
        self.assertTrue(self.bot.reader.state.get("telegram_group"))   # setUp: the bound group was said and folded
        self.assertEqual(self.spooled(), [])                             # the core knows — said once
        tg.save_config({"token": "1:x", "owners": [ADMIN]})              # group off
        self.bot.cfg = tg.load_config()
        self.bot.tick()
        self.bot.tick()                                                  # not folded yet: not said twice
        got = [e for e in self.spooled() if e["type"] == "telegram.group"]
        self.assertEqual([(e["data"]["on"], e["src"]) for e in got], [(False, "telegram")])
        self.bot.clock = lambda: tg.time.time() + tg.SAY_GROUP_AGAIN + 1   # the core never took it — said again
        self.bot.tick()
        self.assertEqual([e["data"]["on"] for e in self.spooled() if e["type"] == "telegram.group"], [False])

    def test_without_a_group_the_bot_talks_as_before(self):
        tg.save_config({"token": "1:x", "owners": [ADMIN]})
        self.bot.cfg = tg.load_config()
        self.bot.tick()
        self.poll(base.TelegramTest.msg(self, "hello", 5, user=ADMIN), self.gmsg("in the group", 6))
        got = self.spooled()
        self.assertEqual([e["data"]["on"] for e in got if e["type"] == "telegram.group"], [False])   # the core learns it
        got = [e for e in got if e["type"] == "message.received"]
        self.assertEqual([(e["data"]["text"], e["data"]["owner"]) for e in got], [("hello", True)])
        self.assertNotIn("tg_by", got[0]["data"])
        self.assertNotIn("getChatAdministrators", [m for m, _ in self.fake.calls])

    # --- one turn, several people ----------------------------------------------------------------------------------
    def test_words_typed_into_a_running_turn_learn_the_answer_is_above(self):
        self.poll(self.gmsg("first", 5), self.gmsg("and also", 6, user=MEMBER))
        self.ingest()
        evs = [e for e in journal.read(self.j.path) if e["type"] == "message.received"]
        first, second = (f"m{e['seq']}" for e in evs[-2:])
        self.bot.tick()
        self.j.append("turn.started", "hub", {"marker": first})
        self.bot.tick()
        msg = self.bot.reader.state["messages"][second]
        msg.update(injected=True, session="hub", delivered_at=0)
        self.j.append("turn.result", "hub", {"text": "both done"})
        self.bot.tick()
        texts = self.fake.texts()
        self.assertIn("both done", texts)
        above = [b for b in self.fake.sent() if b.get("text") == tg.i18n.t("telegram.group.answer_above")]
        self.assertEqual([b["reply_parameters"]["message_id"] for b in above], [6])

    # --- binding -------------------------------------------------------------------------------------------------------
    def pair(self, *args, tty=True, **env):
        with mock.patch.dict(os.environ, {"HUB_TELEGRAM_API": self.fake.base, **env}), \
                mock.patch("sys.stdin.isatty", return_value=tty):
            importlib.reload(tg)
            try:
                return tg.main(["pair", *args])
            finally:
                importlib.reload(tg)

    def code(self, user=ADMIN, chat=OTHER):
        self.fake.results["getMe"] = {**ME, "can_read_all_group_messages": True}
        self.bot._me = None
        self.poll(self.gmsg("/start", 50 + len(self.fake.calls), user=user, chat=chat))
        return next((c for c, v in tg.load_config().get("pending", {}).items()
                     if v.get("chat") == chat and v.get("user") == user), None)

    def unbound(self):
        """A fresh hub: no owner yet, no group — a group's admin gets a code for `hub-telegram pair`."""
        tg.save_config({"token": "1:x"})
        self.bot.cfg = tg.load_config()

    def test_only_an_admin_gets_a_group_code(self):
        self.unbound()
        self.assertIsNone(self.code(user=MEMBER))
        code = self.code()
        self.assertIn(code, self.fake.last_text())
        self.assertNotIn(code, str(self.spooled()))

    def other_admins(self, *ids):
        """getChatAdministrators: OTHER is run by these people, the bound group by ADMIN."""
        self.fake.results["getChatAdministrators"] = \
            lambda body: admins(*ids) if str(body.get("chat_id")) == str(OTHER) else admins(ADMIN)

    def test_once_bound_another_groups_admin_gets_no_code_the_owner_a_card(self):
        # verify №21: an admin of any group got a code while the owner was bound; a session could then pair it
        self.other_admins(STRANGER)
        self.fake.calls.clear()
        code = self.code(user=STRANGER)
        self.assertIsNotNone(code)   # the request is kept (its place), the code goes to nobody
        self.assertNotIn(code, str(self.fake.calls))
        said = {str(b.get("chat_id")): b for b in self.fake.sent()}
        self.assertEqual(said[str(OTHER)]["text"], tg.i18n.t("telegram.group.asked_owner"))
        card = said[str(GROUP)]
        self.assertIn(f"P{STRANGER}", card["text"])
        buttons = [b for row in card["reply_markup"]["inline_keyboard"] for b in row]
        self.assertEqual([b["text"] for b in buttons],
                         [tg.i18n.t("telegram.group.bind_yes"), tg.i18n.t("telegram.group.bind_no")])
        self.assertIsNone(card.get("parse_mode"))   # the title and names are strangers' words: no markup
        # a member of the bound group does not decide; the owner's press (the bound group's admin) moves the bot
        self.press(buttons[0]["callback_data"], MEMBER, mid=901)
        self.assertEqual(tg.group_of(tg.load_config())["id"], GROUP)
        self.press(buttons[0]["callback_data"], ADMIN, mid=902)
        self.assertEqual(tg.group_of(tg.load_config())["id"], OTHER)
        self.assertNotIn(code, tg.load_config().get("pending") or {})
        self.assertIn(tg.i18n.t("telegram.group.bound", admins=f"P{STRANGER}"), self.fake.texts())

    def test_a_card_that_did_not_go_holds_no_place(self):
        self.other_admins(STRANGER)
        self.fake.fail["sendMessage"] = [400]   # the card to the owner's chat
        self.code(user=STRANGER)
        self.assertEqual(tg.load_config().get("pending") or {}, {})
        self.assertEqual(self.fake.last_text(), tg.i18n.t("telegram.group.ask_failed"))   # the admin knows to repeat
        self.fake.fail.pop("sendMessage", None)
        self.code(user=STRANGER)   # the next /start asks again
        self.assertTrue([b for b in self.fake.sent() if str(b.get("chat_id")) == str(GROUP) and b.get("reply_markup")])

    def test_the_owner_keeps_the_bot_where_it_is(self):
        self.other_admins(STRANGER)
        self.code(user=STRANGER)
        card = [b for b in self.fake.sent() if str(b.get("chat_id")) == str(GROUP)][-1]
        no = card["reply_markup"]["inline_keyboard"][0][1]["callback_data"]
        self.press(no, ADMIN, mid=903)
        self.assertEqual(tg.group_of(tg.load_config())["id"], GROUP)
        self.assertEqual(tg.load_config().get("pending") or {}, {})
        self.assertIn(tg.i18n.t("telegram.group.bind_kept"), self.notes())

    def test_strangers_requests_do_not_flood_the_owners_chat_his_own_still_goes(self):
        # verify №16 for groups: the places are PAIR_MAX; strangers wait for a free one, the owner's takes the oldest
        users = [STRANGER + i for i in range(tg.PAIR_MAX + 1)]
        self.other_admins(*users, ADMIN)
        for u in users:
            self.code(user=u)
        cards = [b for b in self.fake.sent() if str(b.get("chat_id")) == str(GROUP) and b.get("reply_markup")]
        self.assertEqual(len(cards), tg.PAIR_MAX)
        self.assertIsNotNone(self.code(user=ADMIN))   # the bound group's admin: his request is always taken
        self.assertEqual(len(tg.load_config()["pending"]), tg.PAIR_MAX)
        self.assertNotIn(users[0], [v["user"] for v in tg.load_config()["pending"].values()])

    def test_pair_yes_only_in_the_owners_terminal(self):
        # the amendment to №21: --yes is no session's, schedule job's or pipe's step
        self.unbound()
        code = self.code()
        for env, tty in (({}, False), ({"HUB_SCHEDULE_JOB": "nightly"}, True), ({"HUB_SESSION": "maps"}, True)):
            with mock.patch("sys.stdout", io.StringIO()), self.assertRaises(SystemExit, msg=env):
                self.pair(code, "--yes", tty=tty, **env)
            self.assertIsNone(tg.group_of(tg.load_config()), env)
        with mock.patch("sys.stdout", io.StringIO()):
            self.pair(code, "--yes")
        self.assertEqual(tg.group_of(tg.load_config())["id"], OTHER)

    def test_no_code_where_the_bot_could_appoint_admins_or_is_blind(self):
        self.fake.results["getChatAdministrators"] = lambda body: admins(ADMIN, promote=True)
        self.assertIsNone(self.code())
        self.assertEqual(self.fake.last_text(), tg.i18n.t("telegram.group.fix_promote"))
        self.fake.results["getChatAdministrators"] = lambda body: admins(ADMIN)[:-1]   # the bot is a plain member
        self.fake.results["getMe"] = ME
        self.bot._me = None
        self.poll(self.gmsg("/start", 60, chat=OTHER))
        self.assertEqual(self.fake.last_text(), tg.i18n.t("telegram.group.fix_privacy"))

    def test_pair_shows_first_and_writes_only_on_yes(self):
        self.unbound()
        code = self.code()
        with mock.patch("sys.stdin", io.StringIO("")), mock.patch("sys.stdout", io.StringIO()) as out, \
                self.assertRaises(SystemExit):
            self.pair(code, tty=False)   # not a terminal: nothing written
        self.assertIn(f"P{ADMIN}", out.getvalue())
        self.assertIsNone(tg.group_of(tg.load_config()))
        with mock.patch("sys.stdout", io.StringIO()):
            self.pair(code, "--yes")
        self.assertEqual(tg.group_of(tg.load_config())["id"], OTHER)
        self.assertNotIn(code, tg.load_config().get("pending") or {})

    def test_pair_without_the_admins_list_writes_nothing(self):
        self.unbound()
        code = self.code()
        self.fake.results.pop("getChatAdministrators")
        self.fake.fail["getChatAdministrators"] = [400]
        with mock.patch("sys.stdout", io.StringIO()), self.assertRaises(SystemExit):
            self.pair(code, "--yes")
        self.assertIsNone(tg.group_of(tg.load_config()))

    def test_group_off_and_pair_refuse_a_session(self):
        with mock.patch.dict(os.environ, {"HUB_SESSION": "maps"}), self.assertRaises(SystemExit):
            tg.main(["group", "off"])
        self.assertIsNotNone(tg.group_of(tg.load_config()))
        with mock.patch("sys.stdout", io.StringIO()):
            tg.main(["group", "off"])
        self.assertIsNone(tg.group_of(tg.load_config()))


class CoreGroup(unittest.TestCase):
    """The core: a member's words are not the owner's; the person is named only by the Telegram module."""

    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "hub", src="web", cwd="/w", session_id="h", kind="hub", channel="web", owner=True)
        self.ev("session.started", "hub", src="hook")

    def ev(self, type, entity=None, src="telegram", **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 100 + self.seq, "type": type, "src": src,
                                                   "entity": entity, "data": data})
        return why

    def words(self, src="telegram", **who):
        return self.ev("message.received", None, src=src, text="restart it", channel="telegram", **who)

    def test_a_member_asks_with_his_own_label_and_note(self):
        self.assertIsNone(self.words(owner=False, member={"tg": 5, "name": "Pat"}))
        msg = self.st["messages"][f"m{self.seq}"]
        self.assertEqual((msg["from"], msg["from_owner"]), ({"kind": "member"}, False))
        self.assertIn("group member", msg["origin"])
        self.assertIn("Pat", msg["routing_note"])

    def test_an_admin_is_the_owner_and_named(self):
        self.assertIsNone(self.words(owner=True, tg_by={"tg": 7, "name": "Alex"}))
        msg = self.st["messages"][f"m{self.seq}"]
        self.assertTrue(msg["from_owner"])
        self.assertIn("Alex", msg["routing_note"])

    def test_the_feed_row_names_a_member_and_not_an_admin(self):
        """views: an `in` row carries `member` {name} for a member (the name as the note has it); an admin's words are the owner's."""
        def row(**who):
            self.words(**who)
            ev = {"seq": self.seq, "ts": 100 + self.seq, "type": "message.received",
                  "data": {"text": "restart it", "channel": "telegram", **who}}
            return views._items(self.st, ev)[0][1]
        member = row(owner=False, member={"tg": 5, "name": "Pat\u200b  " + "x" * 100})
        self.assertEqual(member["member"], {"name": "Pat  " + "x" * 75})   # unprintable removed, cut to GROUP_NAME_MAX
        self.assertEqual(member["from"], {"kind": "member"})
        admin = row(owner=True, tg_by={"tg": 7, "name": "Alex"})
        self.assertNotIn("member", admin)
        self.assertNotIn("member", row(owner=True))   # the owner's own chat

    def test_only_the_telegram_module_names_the_groups_people(self):
        for src, who in (("web", {"owner": False, "member": {"tg": 5, "name": "Pat"}}),
                         ("hook", {"owner": True, "tg_by": {"tg": 7, "name": "Alex"}}),
                         ("telegram", {"owner": True, "member": {"tg": 5, "name": "Pat"}}),   # a member claiming the owner
                         ("telegram", {"owner": False, "tg_by": {"tg": 7, "name": "Alex"}})):
            self.assertIsNotNone(self.words(src=src, **who), (src, who))

    def test_only_the_bot_says_a_group_is_bound(self):
        """telegram.group: the bot's word whether a group is bound (bus mode remote sends forms to it then); a fact from
        another source (a session's hook, the page, the CLI) or a broken value does not set it."""
        for src, on in (("hook", True), ("web", True), ("cli", True), ("telegram", "yes"), ("telegram", None)):
            self.assertIsNotNone(self.ev("telegram.group", None, src=src, on=on, channel="telegram", owner=True), (src, on))
            self.assertNotIn("telegram_group", self.st)
        self.assertIsNone(self.ev("telegram.group", None, on=True, channel="telegram", owner=True))
        self.assertTrue(self.st["telegram_group"])
        self.assertIsNone(self.ev("telegram.group", None, on=False, channel="telegram", owner=True))
        self.assertNotIn("telegram_group", self.st)

    def test_a_members_words_open_no_owner_gate(self):
        self.words(owner=False, member={"tg": 5, "name": "Pat"})
        mid = f"m{self.seq}"
        self.assertFalse(self.st["messages"][mid]["from_owner"])
        self.assertEqual(self.st["owner"]["state"], "present")


if __name__ == "__main__":
    unittest.main()
