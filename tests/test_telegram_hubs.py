"""Linking hubs from Telegram (spec hubbus/ux §3.4, slice L4): /link on A — one message with the link to forward; a
forwarded or pasted link on B — a preview card with «Link» (one press, with a term), never a message to the hub;
«Linked» with the pair code and «Matches»; the other owner's words with «Reply», a reply to them — the owners' chat
(no hub, no turn); the link's messages leave the chat once linked; the link never reaches a log, the journal, the
bot's state file or the hub. Two parts: the bot against a fake Bot API with the owner's path faked (no library), and
the whole path through two bots, two stand cores and the relay of this tree — also with ~30% of Bot API calls
failing."""
import tests  # noqa: F401 — keeps the machine's settings out
import html
import io
import json
import os
import random
import re
import secrets
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from core import bus, busflow, busrelay, i18n, journal, liaison, redact, spool
from modules.telegram import bot as tg
from modules.telegram import hubs
from tests.test_hubbus import need_lib, need_ssh
from tests import test_telegram as base   # not its TestCase by name: the loader would run its tests again here
from tests.test_telegram import OWNER, FakeBotApi

t = i18n.t


def fake_link():
    """A link-shaped text (made here, not a real one: gitleaks)."""
    return bus.LINK_PREFIX + secrets.token_urlsafe(1800)


def buttons(body):
    return [b for row in (body.get("reply_markup") or {}).get("inline_keyboard") or [] for b in row]


def plain(text):
    """What a forward carries: the message's text without its markup."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


class Faked(unittest.TestCase):
    """The bot's side of the path; core/busflow.Flow is faked (its own tests are tests/test_hubbus_l1.py). The set-up of
    tests/test_telegram.TelegramTest — borrowed, not inherited (a subclass would run all its tests again)."""
    tearDown, spooled, msg = base.TelegramTest.tearDown, base.TelegramTest.spooled, base.TelegramTest.msg

    def setUp(self):
        base.TelegramTest.setUp(self)
        self.bot.hubs.sync = True
        self.now = time.time()
        self.bot.clock = lambda: self.now
        self.err = io.StringIO()
        p = mock.patch("sys.stderr", self.err)
        p.start()
        self.addCleanup(p.stop)

    def on(self):
        self.bot.reader.state["bus"] = {"enabled": True, "me": {"fp": "hb1-mine"}}

    def press(self, button, uid, user=OWNER, message=None):
        cq = {"id": f"c{uid}", "from": {"id": user}, "data": button["callback_data"],
              "message": {"message_id": 900, "chat": {"id": user, "type": "private"}, **(message or {})}}
        self.fake.updates = [{"update_id": uid, "callback_query": cq}]
        self.bot.poll(timeout=0)
        self.bot.tick()

    def say(self, text, mid, **extra):
        self.fake.updates = [self.msg(text, mid, **extra)]
        self.bot.poll(timeout=0)
        self.bot.tick()

    def made(self, link, lid="a" * 32, posted=True):
        return mock.patch.object(busflow.Flow, "link", return_value=busflow.Done(
            value=link, id=lid, exp=int(self.now) + 86400, posted=posted))

    def bot_file(self):
        with open(self.bot.path, encoding="utf-8") as f:
            return f.read()

    # --- A -----------------------------------------------------------------------------------------------------------
    def test_link_is_one_message_to_forward_and_never_anywhere_else(self):
        self.on()
        link = fake_link()
        with self.made(link) as made:
            self.say("/link", 5)
        made.assert_called_once_with(ttl=86400)
        self.assertEqual(self.spooled(), [])                         # no message to the hub
        carrying = [(m, b) for m, b in self.fake.calls if link in json.dumps(b)]
        (method, body), = carrying                                   # one message carries it
        self.assertIn(f"<code>{link}</code>", body["text"])
        self.assertLessEqual(len(plain(body["text"])), 4096)
        self.assertEqual([b["text"] for b in buttons(body)], [t("telegram.bus.revoke")])
        self.assertNotIn(link, self.bot_file())
        self.assertNotIn(link, self.err.getvalue())
        (first,) = self.fake.sent()                                  # «making a link» as a reply to /link, then the link in it
        self.assertEqual(first["reply_parameters"]["message_id"], 5)
        self.assertEqual(method, "editMessageText")

    def test_the_path_runs_beside_the_bot_loop(self):
        """Outside tests the owner's path runs in a worker: the bot answers others meanwhile; the result comes at a tick."""
        self.on()
        self.bot.hubs.sync = False
        link, gate = fake_link(), threading.Event()

        def slow(flow, ttl):
            gate.wait(10)
            return busflow.Done(value=link, id="e" * 32, exp=int(self.now) + 86400, posted=True)
        with mock.patch.object(busflow.Flow, "link", autospec=True, side_effect=slow):
            self.say("/link", 5)
            self.say("/link", 6)                                     # a second one while the first goes: told so
            self.assertIn(html.escape(t("telegram.bus.busy"), quote=False), self.fake.texts()[-1])
            gate.set()
            for _ in range(100):
                if self.bot.hubs.busy is None and not self.bot.hubs.done.empty():
                    break
                time.sleep(0.05)
            self.bot.tick()
        self.assertEqual(sum(link in json.dumps(b) for _, b in self.fake.calls), 1)

    def test_bus_off_asks_first_and_the_press_makes_the_link(self):
        link = fake_link()
        with self.made(link) as made:
            self.say("/link", 5)
            made.assert_not_called()
            (card,) = self.fake.sent()
            self.assertIn(html.escape(t("cli.bus.consent"), quote=False), card["text"])
            (yes,) = buttons(card)
            self.press(yes, 6)
            self.press(yes, 7)                                       # a second tap: no second link
        made.assert_called_once_with(ttl=86400)
        self.assertEqual(sum(link in json.dumps(b) for _, b in self.fake.calls), 1)

    def test_an_old_consent_button_does_nothing(self):
        with self.made(fake_link()) as made:
            self.say("/link", 5)
            (yes,) = buttons(self.fake.sent()[0])
            self.now += hubs.CONSENT_TTL + 1
            self.press(yes, 6)
        made.assert_not_called()
        self.assertIn(t("telegram.bus.old"), [b["text"] for b in self.fake.sent("answerCallbackQuery")])

    def test_a_failed_link_names_the_reason_and_the_command(self):
        self.on()
        with mock.patch.object(busflow.Flow, "link", return_value=busflow.Done(
                "no_network", busflow.why("no_network"), command="python3 -m pip install x", step="lib")):
            self.say("/link 7d", 5)
        last = self.fake.texts()[-1]
        self.assertIn(html.escape(busflow.why("no_network"), quote=False), last)
        self.assertIn("<code>python3 -m pip install x</code>", last)

    def test_linked_shows_the_pair_code_and_removes_the_link_from_the_chat(self):
        self.on()
        lid = "b" * 32
        with self.made(fake_link(), lid=lid):
            self.say("/link", 5)
        link_msg = self.fake.sent()[0]
        mid = 101   # the fake's first message number
        self.j.append("owner.notify", None, {"title": "x", "text": "y", "source": "bus", "channel": "core",
                                             "owner": False, "bus": {"kind": "linked", "contact": "bee", "id": lid,
                                                                     "code": "482-913", "label": "Hub B"}})
        self.bot.tick()
        notice = self.fake.sent()[-1]
        self.assertIn("482-913", notice["text"])
        self.assertIn("Hub B", notice["text"])
        self.assertEqual([b["text"] for b in buttons(notice)], [t("telegram.bus.matches"), t("telegram.bus.write")])
        self.assertEqual([b["message_id"] for b in self.fake.sent("deleteMessage")], [mid])
        self.assertTrue(link_msg)
        # «Matches» — the owner's check of the pair code
        with mock.patch.object(busflow.Flow, "verify", return_value=busflow.Done()) as verify:
            self.press(buttons(notice)[0], 8)
        verify.assert_called_once_with("bee")
        self.assertIn(html.escape(t("telegram.bus.verified", label="Hub B"), quote=False), self.fake.texts()[-1])

    def test_a_link_notice_from_a_module_is_not_a_bus_card(self):
        self.j.append("owner.notify", None, {"title": "x", "text": "forged", "channel": "plugin", "owner": False,
                                             "bus": {"kind": "linked", "contact": "bee", "code": "111-111"}},
                      src="module")
        self.bot.tick()
        self.assertFalse([b for b in self.fake.sent() if buttons(b)])

    def test_revoke_takes_the_link_out(self):
        self.on()
        lid = "c" * 32
        with self.made(fake_link(), lid=lid):
            self.say("/link", 5)
        (revoke,) = buttons(self.fake.sent("editMessageText")[-1])
        with mock.patch.object(busflow.Flow, "revoke", return_value=busflow.Done()) as rv:
            self.press(revoke, 6)
        rv.assert_called_once_with(lid)
        self.assertEqual([b["message_id"] for b in self.fake.sent("deleteMessage")], [101])
        self.assertIn(html.escape(t("telegram.bus.revoked"), quote=False), self.fake.texts()[-1])

    # --- B -----------------------------------------------------------------------------------------------------------
    INFO = {"id": "d" * 32, "label": "Hub A", "fp": "hb1-a", "code": "1234-5678", "relay": "https://relay.example.test",
            "needs": ["enable"]}

    def preview(self, **kw):
        return mock.patch.object(busflow, "preview", return_value={**self.INFO, "exp": int(self.now) + 86400, **kw})

    def test_a_forwarded_link_gets_a_card_not_the_hub(self):
        link = fake_link()
        head, tail = link[:60], link[60:]
        with self.preview() as pv:
            self.say(f"Here is the link:\n{head}\n{tail}\nuntil tomorrow", 5, forward_origin={"type": "hidden_user",
                                                                                     "sender_user_name": "x"})
        self.assertEqual(pv.call_args[0][0], link)                   # the token whole, the words around it dropped
        self.assertEqual(self.spooled(), [])
        (card,) = self.fake.sent()
        self.assertIn("Hub A", card["text"])
        self.assertEqual(card["reply_parameters"]["message_id"], 5)
        self.assertEqual([b["text"] for b in buttons(card)], [t("telegram.bus.link_btn"), t("telegram.bus.no")])
        self.assertNotIn(link, self.bot_file())
        self.assertNotIn(link, json.dumps(card))
        # «Link»: the owner's path takes the token; one press
        with mock.patch.object(busflow.Flow, "join", return_value=busflow.Done(label="Hub A", state="linked")) as join:
            self.press(buttons(card)[0], 6)
            self.press(buttons(card)[0], 7)
        join.assert_called_once_with(link)
        self.assertIn(t("telegram.bus.pressed"), [b["text"] for b in self.fake.sent("answerCallbackQuery")])
        self.assertIn(html.escape(t("telegram.bus.joined", label="Hub A"), quote=False), self.fake.texts()[-1])
        # «Linked» from the core: the owner's message with the link leaves the chat
        self.j.append("owner.notify", None, {"title": "x", "text": "y", "source": "bus", "channel": "core",
                                             "owner": False, "bus": {"kind": "linked", "contact": "a", "code": "1-2",
                                                                     "id": self.INFO["id"], "label": "Hub A"}})
        self.bot.tick()
        self.assertEqual([b["message_id"] for b in self.fake.sent("deleteMessage")], [5])

    def test_the_link_button_has_a_term(self):
        with self.preview():
            self.say(fake_link(), 5)
        (card,) = self.fake.sent()
        self.now += hubs.KEY_TTL + 1
        with mock.patch.object(busflow.Flow, "join") as join:
            self.press(buttons(card)[0], 6)
        join.assert_not_called()
        self.assertIn(t("telegram.bus.old"), [b["text"] for b in self.fake.sent("answerCallbackQuery")])

    def test_a_restarted_bot_takes_the_link_from_the_message_the_card_answers(self):
        link = fake_link()
        with self.preview():
            self.say(link, 5)
        (card,) = self.fake.sent()
        self.bot.hubs.secrets.clear()                                # a restart: the memory is gone
        with mock.patch.object(bus, "read_link", return_value={"id": self.INFO["id"]}), \
                mock.patch.object(busflow.Flow, "join", return_value=busflow.Done()) as join:
            self.press(buttons(card)[0], 6, message={"reply_to_message": {"message_id": 5, "text": "x " + link}})
        join.assert_called_once_with(link)

    def test_no_drops_the_link(self):
        with self.preview():
            self.say(fake_link(), 5)
        (card,) = self.fake.sent()
        with mock.patch.object(busflow.Flow, "join") as join:
            self.press(buttons(card)[1], 6)
        join.assert_not_called()
        self.assertEqual([b["message_id"] for b in self.fake.sent("deleteMessage")], [5])
        self.assertEqual(self.bot.hubs.secrets, {})

    def test_a_bad_link_is_refused_in_words_and_not_passed_on(self):
        link = fake_link()
        with mock.patch.object(busflow, "preview", side_effect=bus.Error("stale", "link")):
            self.say(link, 5)
        self.assertEqual(self.spooled(), [])
        self.assertIn(html.escape(busflow.why("stale"), quote=False), self.fake.sent()[-1]["text"])

    def test_an_invitation_after_a_card_is_found_too(self):
        inv = "hub-relay-invite-1:" + "x" * 30 + "." + "y" * 30
        with mock.patch.object(hubs.busrelay, "read_invite", return_value=({"relay": "https://relay.example.test",
                                                                            "by": "hb1-a", "key": "k"}, None)):
            self.say("card hub-bus-card-1:" + "QUJD" * 10 + "== and an invitation " + inv, 5)
        self.assertEqual(self.spooled(), [])
        self.assertEqual([b["text"] for b in buttons(self.fake.sent()[-1])],
                         [t("telegram.bus.invite_btn"), t("telegram.bus.no")])

    def test_an_accepted_relay_invitation_offers_the_next_step_on_its_card(self):
        # the owner's one path: «Accept» of the first hub's invitation — and «Make an invitation» is right on that card
        inv = "hub-relay-invite-1:" + "x" * 30 + "." + "y" * 30
        with mock.patch.object(hubs.busrelay, "read_invite", return_value=({"relay": "https://relay.example.test",
                                                                            "by": "hb1-a", "key": "k"}, None)):
            self.say(inv, 5)
        (card,) = self.fake.sent()
        with mock.patch.object(busflow.Flow, "join", return_value=busflow.Done(url="https://relay.example.test")):
            self.press(buttons(card)[0], 6)
        last = self.fake.sent("editMessageText")[-1]
        self.assertEqual([b["text"] for b in buttons(last)], [t("telegram.bus.make_btn")])
        self.assertIn("https://relay.example.test", last["text"])
        self.assertNotRegex(last["text"].replace("https://relay.example.test", ""), r"(?i)\brelay\b|\blinks?\b")

    def test_a_refused_join_takes_the_link_out_of_the_chat(self):
        with self.preview():
            self.say(fake_link(), 5)
        (card,) = self.fake.sent()
        with mock.patch.object(busflow.Flow, "join", return_value=busflow.Done("expired", busflow.why("expired"))):
            self.press(buttons(card)[0], 6)
        self.assertEqual([b["message_id"] for b in self.fake.sent("deleteMessage")], [5])
        self.assertIn(html.escape(busflow.why("expired"), quote=False), self.fake.texts()[-1])

    def test_a_join_that_waited_ends_by_the_cores_notice(self):
        with self.preview():
            self.say(fake_link(), 5)
        (card,) = self.fake.sent()
        with mock.patch.object(busflow.Flow, "join", return_value=busflow.Done("waiting", "the hub has not replied yet")):
            self.press(buttons(card)[0], 6)
        self.assertIn("⏳", self.fake.texts()[-1])
        self.j.append("owner.notify", None, {"title": "x", "text": "y", "source": "bus", "channel": "core",
                                             "owner": False, "bus": {"kind": "join_failed", "id": self.INFO["id"],
                                                                     "code": "used", "label": "Hub A"}})
        self.bot.tick()
        self.assertEqual([b["message_id"] for b in self.fake.sent("deleteMessage")], [5])
        edit = self.fake.sent("editMessageText")[-1]
        self.assertEqual(edit["message_id"], 900)                    # the card itself
        self.assertIn(html.escape(busflow.why("used"), quote=False), edit["text"])
        self.assertFalse([b for b in self.fake.sent() if "x" == b["text"]])   # no second, generic notice

    def test_a_network_failure_keeps_the_link_and_the_button(self):
        link = fake_link()
        with self.preview():
            self.say(link, 5)
        (card,) = self.fake.sent()
        with mock.patch.object(busflow.Flow, "join", side_effect=[busflow.Done("no_network", busflow.why("no_network")),
                                                                busflow.Done()]) as join:
            self.press(buttons(card)[0], 6)
            again = self.fake.sent("editMessageText")[-1]
            self.assertIn(html.escape(busflow.why("no_network"), quote=False), again["text"])
            self.assertEqual(self.fake.sent("deleteMessage"), [])     # the link stays
            self.press(buttons(again)[0], 7)
        self.assertEqual(join.call_args_list, [mock.call(link), mock.call(link)])

    def test_a_mention_of_the_prefix_is_words_for_the_hub(self):
        with mock.patch.object(busflow, "preview", side_effect=bus.Error("bad_link", "fields")):
            self.say("fix the format hub-link-1:AAAAAAAAAAAAAAAAAAAAAAAA in the doc", 5)
        (got,) = self.spooled()
        self.assertEqual(got["type"], "message.received")
        self.assertEqual(self.fake.sent(), [])

    def test_a_failed_revoke_may_be_pressed_again(self):
        self.on()
        with self.made(fake_link(), lid="c" * 32):
            self.say("/link", 5)
        (revoke,) = buttons(self.fake.sent("editMessageText")[-1])
        with mock.patch.object(busflow.Flow, "revoke", side_effect=[busflow.Done("network", busflow.why("network")),
                                                                  busflow.Done()]) as rv:
            self.press(revoke, 6)
            self.press(revoke, 7)
        self.assertEqual(rv.call_count, 2)

    def test_revoke_is_one_press(self):
        self.on()
        with self.made(fake_link(), lid="c" * 32):
            self.say("/link", 5)
        (revoke,) = buttons(self.fake.sent("editMessageText")[-1])
        with mock.patch.object(busflow.Flow, "revoke", return_value=busflow.Done()) as rv:
            self.press(revoke, 6)
            self.press(revoke, 7)
        rv.assert_called_once()

    def test_matches_survives_a_long_chat(self):
        self.j.append("owner.notify", None, {"title": "x", "text": "y", "source": "bus", "channel": "core",
                                             "owner": False, "bus": {"kind": "linked", "contact": "a", "id": "f" * 32,
                                                                     "code": "1-2", "label": "Hub A"}})
        self.bot.tick()
        notice = self.fake.sent()[-1]
        for i in range(hubs.KEEP_KEYS + 5):
            self.j.append("owner.notify", None, {"title": "x", "text": f"word {i}", "source": "bus",
                                                 "channel": "core", "owner": False,
                                                 "bus": {"kind": "chat", "contact": "a", "label": "Hub A"}})
        self.bot.tick()
        self.bot.save()
        with mock.patch.object(busflow.Flow, "verify", return_value=busflow.Done()) as verify:
            self.press(buttons(notice)[0], 6)
        verify.assert_called_once_with("a")

    def test_a_reply_to_the_other_owner_the_bot_could_not_parse_does_not_go_to_the_hub(self):
        notice = self.chat_notice()
        self.assertTrue(notice)
        self.bot.lost({"message_id": 7, "from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"},
                       "text": "personal to the other owner", "reply_to_message": {"message_id": 101}}, KeyError("x"))
        self.bot.tick()
        self.assertEqual(self.spooled(), [])

    def test_a_strangers_link_is_ignored(self):
        with self.preview() as pv:
            self.say(fake_link(), 5, user=1)
        pv.assert_not_called()
        self.assertEqual((self.fake.sent(), self.spooled()), ([], []))

    def test_a_strangers_press_does_nothing(self):
        with self.preview():
            self.say(fake_link(), 5)
        (card,) = self.fake.sent()
        with mock.patch.object(busflow.Flow, "join") as join:
            self.press(buttons(card)[0], 6, user=1)
        join.assert_not_called()

    def test_a_link_in_a_quote_or_a_lost_message_does_not_reach_the_hub(self):
        link = fake_link()
        self.fake.updates = [self.msg("what is this?", 6, reply_to_message={"message_id": 4, "text": "link " + link})]
        self.spooled()
        self.bot.poll(timeout=0)
        (got,) = self.spooled()
        self.assertNotIn(link, json.dumps(got))
        self.bot.lost({"message_id": 7, "from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"},
                       "text": link}, ValueError("x"))
        self.bot.tick()
        self.assertEqual(self.spooled(), [])
        self.assertNotIn(link, self.err.getvalue())

    # --- the owners' chat --------------------------------------------------------------------------------------------
    def chat_notice(self):
        self.j.append("owner.notify", None, {"title": "💬 Hub A", "text": "Hello! I see you.", "source": "bus",
                                             "channel": "core", "owner": False,
                                             "bus": {"kind": "chat", "contact": "a", "label": "Hub A"}})
        self.bot.tick()
        return self.fake.sent()[-1]

    def test_the_other_owners_words_and_a_reply_to_them(self):
        notice = self.chat_notice()
        self.assertIn("Hub A", notice["text"])
        self.assertIn("Hello! I see you.", notice["text"])
        self.assertEqual([b["text"] for b in buttons(notice)], [t("telegram.bus.reply")])
        with mock.patch.object(busflow.Flow, "chat", return_value=busflow.Done()) as chat:
            self.say("And the same to you", 6, reply_to=101)
        chat.assert_called_once_with("a", "And the same to you", confirm=False)
        self.assertEqual(self.spooled(), [])                          # not to the hub: no turn
        self.assertEqual(self.fake.sent("setMessageReaction")[-1]["reaction"][0]["emoji"], "👌")

    def test_reply_button_asks_for_an_answer_that_goes_to_that_owner(self):
        notice = self.chat_notice()
        self.press(buttons(notice)[0], 6)
        prompt = self.fake.sent()[-1]
        self.assertTrue(prompt["reply_markup"]["force_reply"])
        with mock.patch.object(busflow.Flow, "chat", return_value=busflow.Done()) as chat:
            self.say("answer", 7, reply_to=102)
        chat.assert_called_once_with("a", "answer", confirm=False)

    def test_a_key_like_text_asks_first(self):
        self.chat_notice()
        word = "x" * 8
        with mock.patch.object(busflow.Flow, "chat", side_effect=[busflow.Done("secret", busflow.why("secret")),
                                                                busflow.Done()]) as chat:
            self.say(word, 6, reply_to=101)
            card = self.fake.sent()[-1]
            self.assertNotIn(word, self.bot_file())
            self.press(buttons(card)[0], 7)
        self.assertEqual(chat.call_args_list[1], mock.call("a", word, confirm=True))

    def test_a_refused_chat_says_why(self):
        self.chat_notice()
        with mock.patch.object(busflow.Flow, "chat", return_value=busflow.Done("not_verified", busflow.why("not_verified"))):
            self.say("text", 6, reply_to=101)
        self.assertIn(html.escape(busflow.why("not_verified"), quote=False), self.fake.sent()[-1]["text"])

    def test_link_is_a_menu_command(self):
        self.bot.register_commands()
        (cmds,) = self.fake.sent("setMyCommands")
        self.assertIn({"command": "link", "description": t("telegram.cmd_link_hub")}, cmds["commands"])


class Put(unittest.TestCase):
    """The bot's own request to the core: its channel and the core's decision, read by its label."""

    def test_the_owners_request_with_its_label(self):
        d = tempfile.mkdtemp(prefix="hubtg-put-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        b = mock.Mock(state_dir=d, spool=os.path.join(d, "spool"), st={})
        h = hubs.Hubs(b)
        j = journal.Journal(os.path.join(d, "journal.jsonl"))
        j.append("core.started", None, {})

        def decide(path, req, wait, start=None):
            (item,) = [e for _, e in spool.take(b.spool)]
            self.assertEqual((item["src"], item["data"]["channel"], item["data"]["owner"], item["data"]["req"]),
                             ("telegram", "telegram", True, req))
            return 7, "refused"
        with mock.patch.object(journal, "decided", side_effect=decide):
            self.assertEqual(h._put("bus.link_requested", None, {"ttl": 3600}), 7)
        self.assertEqual(h.rejected, {7: "refused"})


@need_lib
@need_ssh
class JoinFailedNotice(unittest.TestCase):
    """B's link that did not come about tells B's owner — the bot (and the page) may have stopped watching: the core's
    notice names the link and the reason; a link that came about has «Linked» only."""

    def setUp(self):
        from tests.test_hubbus_s1 import LiveHub, Wire
        self.a, self.b = LiveHub("Hub A"), LiveHub("Hub B")
        self.wire = Wire(self.a, self.b)

    def notices(self, hub, kind):
        from tests.test_hubbus_l0 import notices
        return [n["bus"] for n in notices(hub) if (n.get("bus") or {}).get("kind") == kind]

    def test_refused_after_the_owner_stopped_watching(self):
        from tests.test_hubbus_l0 import make_link, paste
        text, lid = make_link(self.a)
        self.a.ask("bus.link_revoke_requested", {"id": lid})
        paste(self.b, text)
        self.wire.settle()
        (got,) = self.notices(self.b, "join_failed")
        self.assertEqual((got["id"], got["code"], got["label"]), (lid, "revoked", "Hub A"))
        self.assertEqual(self.notices(self.b, "linked"), [])

    def test_expired_without_an_answer_is_told_once_it_is_final(self):
        from tests.test_hubbus_l0 import make_link, paste, tick
        text, lid = make_link(self.a, ttl=3600)
        paste(self.b, text)
        self.assertTrue(self.b.bus["joins"][lid].get("intro_id"))   # B's introduction is out; A never reads it
        self.b.now += 3600 + 3600 + 1        # the link's term and an hour: a late answer would still link
        tick(self.b)
        self.assertEqual(self.notices(self.b, "join_failed"), [])
        self.b.now += 14 * 86400             # the record goes: now nothing can come back
        tick(self.b)
        (got,) = self.notices(self.b, "join_failed")
        self.assertEqual((got["id"], got["code"]), (lid, "expired"))
        tick(self.b)
        self.assertEqual(len(self.notices(self.b, "join_failed")), 1)

    def test_expired_with_the_introduction_out_waits_for_a_late_answer(self):
        """B's introduction went and A was silent: the term is over, but A's late answer still links — no «not
        linked» until it is final (a late «Linked» after it would contradict it)."""
        from tests.test_hubbus_l0 import make_link, paste, tick
        text, lid = make_link(self.a, ttl=3600)
        paste(self.b, text)
        tick(self.b)
        self.wire.move(self.b)                      # B's introduction is out, A has not read it
        self.assertTrue(self.b.bus["joins"][lid].get("intro_id"))
        self.b.now += 3600 + 3600 + 1
        tick(self.b)
        self.assertEqual(self.b.bus["joins"][lid]["state"], "expired")
        self.assertEqual(self.notices(self.b, "join_failed"), [])
        self.wire.settle()                          # A answers late: refused (its term is over too) — one notice
        self.a.now = self.b.now
        self.wire.settle()
        self.assertLessEqual(len(self.notices(self.b, "join_failed")), 1)

    def test_linked_has_its_own_notice_only(self):
        from tests.test_hubbus_l0 import make_link, paste
        text, lid = make_link(self.a)
        paste(self.b, text)
        self.wire.settle()
        self.assertEqual(self.notices(self.b, "join_failed"), [])
        (a,), (b,) = self.notices(self.a, "linked"), self.notices(self.b, "linked")
        self.assertEqual((a["id"], b["id"]), (lid, lid))
        self.assertEqual(a["code"], b["code"])
        self.assertEqual((a["contact"], b["contact"]), (next(iter(self.a.bus["contacts"])),
                                                        next(iter(self.b.bus["contacts"]))))


class OfferCard(unittest.TestCase):
    """The card of an invitation a session brought (`hub bus offer`) reaches the owner's Telegram like the card of a hub that
    introduced itself: by the id of the invitation the card of the liaison carries."""

    def test_the_bot_takes_the_offer_card_from_the_liaison(self):
        self.assertIn("core.bus_offer", tg.BUS_CARDS)
        form = {"questions": [{"question": "Q", "header": "H", "multiSelect": False, "options": [{"label": "Link"}]}]}
        st = {"sessions": {liaison.NAME: {"asks": [{"request_id": "bus-f" + "ab" * 8, "hubbus": {"kind": "offer"},
                                                    "input": form}]}}}
        got = tg.Bot.bus_card(st, "core.bus_offer", {"id": "ab" * 12}, {"seq": 5})
        self.assertEqual(got[0], liaison.NAME)
        self.assertEqual((got[1]["request_id"], got[1]["input"]), ("bus-f" + "ab" * 8, form))
        self.assertIsNone(tg.Bot.bus_card(st, "core.bus_offer", {"id": "cd" * 12}, {"seq": 5}))


class HubName(unittest.TestCase):
    """The name a hub carries in its card (and so in «Linked: …» on the other side): the label the owner gave the bus,
    else the hub's title of hub.json (the name of its page), else the machine's name — one line, never empty."""

    def test_the_order(self):
        with mock.patch("socket.gethostname", return_value="box-7.example.test"):
            self.assertEqual(bus.hub_label({"label": "My label"}, {"title": "Hub A"}), "My label")
            self.assertEqual(bus.hub_label({}, {"title": "  Hub\tA  "}), "Hub A")
            self.assertEqual(bus.hub_label(None, {"title": ""}), "box-7")
            self.assertEqual(bus.hub_label(None, None), "box-7")

    def test_one_printable_line(self):
        with mock.patch("socket.gethostname", return_value="\x1b\u202e\x07"):
            self.assertEqual(bus.hub_label(None, {"title": "x" * 300}), "x" * 100)
            self.assertEqual(bus.hub_label(None, {"title": "a\x00b\u202ec\nd"}), "abc d")
            self.assertEqual(bus.hub_label(None, None), "Hub")   # nothing usable: the last resort
            self.assertTrue(bus.hub_label(None, {"title": "a\x00b"}))


class InviteMasked(unittest.TestCase):
    """A relay invitation is a permit (membership of the relay): masked in every journal record, as a link."""

    def test_in_the_journal(self):
        d = tempfile.mkdtemp(prefix="hubtg-mask-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        inv = busrelay.INVITE_PREFIX + secrets.token_urlsafe(60) + "." + secrets.token_urlsafe(60)
        j = journal.Journal(os.path.join(d, "journal.jsonl"))
        j.append("bus.relay_requested", None, {"op": "join", "invite": inv, "channel": "cli", "owner": True})
        j.append("message.received", None, {"text": f"here is the invitation: {inv}. Thanks", "channel": "telegram"})
        with open(j.path, encoding="utf-8") as f:
            raw = f.read()
        sig = inv.rsplit(".", 1)[1]
        self.assertNotIn(sig[:20], raw)                              # the signature — the permit's power — is gone
        self.assertIn("Thanks", raw)
        self.assertEqual(raw.count(redact.MASK), 2)

    @need_lib
    @need_ssh
    def test_the_core_still_takes_the_relay_from_a_masked_invitation(self):
        """The fold reads the relay's address and key from the invitation (the journal masks it before the fold)."""
        from tests.test_hubbus_s1 import LiveHub
        from tests.test_hubbus_l0_relay import Relay
        relay = Relay()
        self.addCleanup(relay.stop)
        hub = LiveHub("Hub A")
        inv = relay.invite()
        ev = hub.ask("bus.relay_requested", {"op": "join", "invite": inv})
        self.assertIsNone(hub.verdict(ev))
        self.assertEqual(hub.bus["relay"]["url"], relay.url)
        with open(os.path.join(hub.h.state_dir, "journal.jsonl"), encoding="utf-8") as f:
            self.assertNotIn(inv.rsplit(".", 1)[1][:20], f.read())


# --- the whole path: two bots, two stand cores, the relay of this tree ------------------------------------------------

class Flaky:
    """A fake Bot API failure plan: every call of a method fails (502 or a dropped connection) with this chance. The fake
    asks twice per call (whether it failed, then the code) — one draw per call."""

    def __init__(self, rng, chance):
        self.rng, self.chance, self.asked, self.now, self.failed = rng, chance, 0, False, 0

    def __bool__(self):
        self.asked += 1
        if self.asked % 2:
            self.now = self.rng.random() < self.chance
        return self.now

    def pop(self, i=0):
        self.failed += 1
        return self.rng.choice([502, "drop"])


TG = {"channel": "telegram", "owner": True}


@need_lib
@need_ssh
class ThroughTheBots(unittest.TestCase):
    """A pastes the relay operator's invitation into its bot (the first hub of the relay), sends /link, forwards the
    link message to B's bot, B presses «Link»: both bots say «Linked» with one pair code, the link's messages leave both
    chats; the owners write to each other by replies; «Matches» on both — both contacts checked."""
    FAIL, SEED = 0.0, 0

    def setUp(self):
        from tests.test_hubbus_l0_relay import RELAY, Relay
        from tests.test_hubbus_l1 import CleanHub
        self.relay = Relay()
        self.addCleanup(self.relay.stop)
        if self.relay.version() < 2:
            self.skipTest(f"{RELAY}: the relay does not announce bus_v 2")
        self.dir = tempfile.mkdtemp(prefix="hubtg-bus-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"HUB_TELEGRAM_CONFIG": os.path.join(self.dir, "telegram.json")})
        env.start()
        self.addCleanup(env.stop)
        tg.save_config({"token": "1:x", "owners": [OWNER]})
        now = int(time.time())
        self.a, self.b = CleanHub("Hub A", now), CleanHub("Hub B", now)
        for hub in (self.a, self.b):   # the hubs' names are their page titles (hub.json title): no bus label given
            hub.h.config["title"] = hub.label
            os.makedirs(hub.config, exist_ok=True)
            with open(os.path.join(hub.config, "hub.json"), "w", encoding="utf-8") as f:
                json.dump({"title": hub.label}, f, ensure_ascii=False)
        self.ta, self.tb = self.transport(self.a), self.transport(self.b)
        self.err = io.StringIO()
        for p in (mock.patch("sys.stderr", self.err), mock.patch.object(tg.time, "sleep", lambda s: None)):
            p.start()
            self.addCleanup(p.stop)
        rng = random.Random(self.SEED)
        self.fa, self.fb = FakeBotApi(), FakeBotApi()
        for fake in (self.fa, self.fb):
            self.addCleanup(fake.server.server_close)
            self.addCleanup(fake.server.shutdown)
            if self.FAIL:
                for method in ("sendMessage", "editMessageText", "deleteMessage", "answerCallbackQuery",
                               "setMessageReaction", "getUpdates"):
                    fake.fail[method] = Flaky(rng, self.FAIL)
        self.ba, self.bb = self.bot(self.a, self.fa), self.bot(self.b, self.fb)
        self.uid = 1000

    def transport(self, hub):
        from modules.bus_relay.run import Transport

        def put(kind, data):
            with hub.env():
                hub.h.record(kind, None, data, src="bus-relay")
        tr = Transport(hub.h.state_dir, hub.config, put=put, log=lambda text: None)
        tr.stop.wait = lambda s: None
        return tr

    def bot(self, hub, fake):
        with hub.env():
            b = tg.Bot(hub.h.state_dir, tg.Api("1:x", base=fake.base))
            b.catch_up()
        h = b.hubs
        h.sync, h.idle = True, self.settle

        def put(type_, entity, data, say=False):
            ev = hub.rec(type_, {**data, **TG}, entity)
            why = hub.verdict(ev)
            if why:
                h.rejected[ev["seq"]] = why
            return ev["seq"]
        h.put, h.current = put, (lambda: hub.h.state)
        h.note = lambda type_, data: hub.rec(type_, {**data, **TG})
        return b

    def settle(self, rounds=12):
        from core import busd
        for _ in range(rounds):
            moved = 0
            for hub, tr in ((self.a, self.ta), (self.b, self.tb)):
                hub.now = max(hub.now, int(time.time()))
                hub.h.__dict__.pop("bus_link_tried", None)
                with hub.env():
                    busd.tick(hub.h)
                    busd.write_view(hub.h)
                before = hub.h.state["seq"]
                v = tr.view()
                if v is None:
                    continue
                tr.job_tries.clear()
                try:
                    moved += tr.hand(v) + tr.take(v, wait=0)
                except busrelay.Error as e:   # the relay's own rate (this stand polls it fast): the next round
                    if e.status != 429:
                        raise
                moved += hub.h.state["seq"] != before
            if not moved:
                return

    def round(self):
        for hub, b in ((self.a, self.ba), (self.b, self.bb)):
            with hub.env():
                b.hold_until = b.hubs.hold = 0
                b.poll(timeout=0)
                b.tick()
        self.settle()

    def until(self, cond, what, rounds=60):
        for _ in range(rounds):
            self.round()
            got = cond()
            if got:
                return got
        self.fail(f"{what}: never")

    def update(self, fake, **kw):
        self.uid += 1
        fake.updates.append({"update_id": self.uid, **kw})

    def say(self, fake, text, reply_to=None, **extra):
        self.uid += 1
        m = {"message_id": self.uid, "from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"}, "text": text,
             **extra}
        if reply_to:
            m["reply_to_message"] = {"message_id": reply_to}
        fake.updates.append({"update_id": self.uid, "message": m})
        return self.uid

    def press(self, fake, button, card):
        self.update(fake, callback_query={"id": f"c{self.uid}", "from": {"id": OWNER}, "data": button["callback_data"],
                                          "message": {"message_id": card, "chat": {"id": OWNER, "type": "private"}}})

    def carrying(self, fake, text):
        return [(m, b) for m, b in fake.calls if text in json.dumps(b, ensure_ascii=False)]

    def sent_ids(self, fake):
        """sendMessage bodies with the message number the fake gave (in the order of the calls)."""
        out, n = [], 100
        for m, b in fake.calls:
            if m in ("sendMessage", "sendVoice"):
                n += 1
                out.append((n, b))
        return out

    def button_msg(self, fake, label):
        for n, b in reversed(self.sent_ids(fake)):
            for btn in buttons(b):
                if btn["text"] == label:
                    return n, btn
        for m, b in reversed(fake.calls):
            if m == "editMessageText":
                for btn in buttons(b):
                    if btn["text"] == label:
                        return b["message_id"], btn
        return None

    def test_link_forward_link_chat_check(self):
        a, b, fa, fb = self.a, self.b, self.fa, self.fb
        # A: the relay operator's invitation, pasted — «Join» — the bus up and the hub on the relay
        invite = self.relay.invite()
        self.say(fa, f"invitation: {invite}")
        _, join = self.until(lambda: self.button_msg(fa, t("telegram.bus.invite_btn")), "the invitation card")
        card, _ = self.button_msg(fa, t("telegram.bus.invite_btn"))
        self.press(fa, join, card)
        self.until(lambda: ((a.h.state.get("bus") or {}).get("relay") or {}).get("url") == self.relay.url,
                   "A on the relay")
        self.until(lambda: self.button_msg(fa, t("telegram.bus.make_btn")), "«Make an invitation» on the accepted card")
        # A: /link — one message with the link
        self.say(fa, "/link")
        got = self.until(lambda: [(m, body) for m, body in fa.calls if "<code>hub-link-1:" in (body.get("text") or "")],
                         "the link message")
        (method, body), = got
        link_mid = body.get("message_id")
        link = busflow.find(plain(body["text"]))[1]
        self.assertEqual(busflow.kind_of(link), "link")
        self.assertLessEqual(len(plain(body["text"])), 4096)
        # B: the owner forwards that message to B's bot — a preview card, «Link»
        fwd = self.say(fb, plain(body["text"]), forward_origin={"type": "user", "sender_user": {"id": 1}})
        _, go = self.until(lambda: self.button_msg(fb, t("telegram.bus.link_btn")), "the preview card")
        card, _ = self.button_msg(fb, t("telegram.bus.link_btn"))
        self.press(fb, go, card)
        linked = t("telegram.bus.linked_head", label="").strip()
        na = self.until(lambda: [x for x in self.sent_ids(fa) if linked in x[1]["text"]], "«Linked» on A")
        nb = self.until(lambda: [x for x in self.sent_ids(fb) if linked in x[1]["text"]], "«Linked» on B")
        for _ in range(3):
            self.round()   # anything more would come now
        (ca,), (cb,) = a.bus["contacts"].values(), b.bus["contacts"].values()
        code = bus.contact_code(ca)
        self.assertTrue(code)
        self.assertEqual(code, bus.contact_code(cb))
        (na_id, na_body), = [x for x in self.sent_ids(fa) if linked in x[1]["text"]]
        (nb_id, nb_body), = [x for x in self.sent_ids(fb) if linked in x[1]["text"]]
        self.assertIn(code, na_body["text"])
        self.assertIn(code, nb_body["text"])
        self.assertIn(t("telegram.bus.linked_head", label="Hub B"), na_body["text"])   # each names the other hub
        self.assertIn(t("telegram.bus.linked_head", label="Hub A"), nb_body["text"])
        self.assertEqual((ca["label"], cb["label"]), ("Hub B", "Hub A"))
        # the link's messages left both chats: A's bot message with the link, the owner's forward on B
        self.until(lambda: link_mid in [x["message_id"] for x in fa.sent("deleteMessage")], "A's link deleted")
        self.until(lambda: fwd in [x["message_id"] for x in fb.sent("deleteMessage")], "B's forward deleted")
        self.assertEqual(len(self.carrying(fa, link)), 1)            # the link went into the chat once
        # «Matches» on both — both contacts checked (the words to a hub's liaison wait for the check)
        for fake, hub, mid, nbody in ((fa, a, na_id, na_body), (fb, b, nb_id, nb_body)):
            (match,) = [x for x in buttons(nbody) if x["text"] == t("telegram.bus.matches")]
            self.press(fake, match, mid)
        self.until(lambda: all(c.get("verified") for h in (a, b) for c in h.bus["contacts"].values()), "both checked")
        # «Tell the hub»: B replies to «Linked» — an ordinary message to A's liaison, it is not A's owner's chat
        self.say(fb, "Hello from B", reply_to=nb_id)
        self.until(lambda: [m for m in a.messages_to(liaison.NAME) if "Hello from B" in m.get("text", "")], "B's words at A's liaison")
        for _ in range(3):
            self.round()
        self.assertFalse([x for x in self.sent_ids(fa) if "Hello from B" in x[1]["text"]])   # not shown to A's owner
        # A's liaison answers in that thread (A's owner says «send» on the card): B's owner gets it with «Reply»
        item = next(x for x in a.bus["inbox"].values() if x["text"] == "Hello from B")
        _, why = a.as_liaison("bus.send_requested", {"contact": item["contact"], "thread": item["thread"], "text": "And you"})
        self.assertIsNone(why)
        got = self.until(lambda: [x for x in self.sent_ids(fb) if "And you" in x[1]["text"]], "A's words on B")
        (chat_id, chat_body), = got
        self.assertIn("Hub A", chat_body["text"])
        self.say(fb, "Thanks", reply_to=chat_id)
        self.until(lambda: [m for m in a.messages_to(liaison.NAME) if "Thanks" in m.get("text", "")], "B's reply at A's liaison")
        for _ in range(3):
            self.round()
        self.assertEqual(len([x for x in self.sent_ids(fb) if "And you" in x[1]["text"]]), 1)
        for hub in (a, b):   # neither the hub nor a turn of a session: the words are the owner's
            evs = journal.read(os.path.join(hub.h.state_dir, "journal.jsonl"))
            self.assertFalse([e for e in evs if e["type"] in ("turn.started", "message.received")
                              and (e.get("data") or {}).get("channel") == "telegram"])
        # the link is nowhere else: the journals, the bots' state files, the inboxes, the logs
        for hub, bt in ((a, self.ba), (b, self.bb)):
            with open(os.path.join(hub.h.state_dir, "journal.jsonl"), encoding="utf-8") as f:
                self.assertNotIn(link, f.read())
            with open(bt.path, encoding="utf-8") as f:
                self.assertNotIn(link, f.read())
            box = os.path.join(hub.h.state_dir, "spool")
            for name in os.listdir(box) if os.path.isdir(box) else []:
                with open(os.path.join(box, name), encoding="utf-8") as f:
                    self.assertNotIn(link, f.read())
        self.assertNotIn(link, self.err.getvalue())
        self.assertEqual(self.ba.hubs.secrets, {})
        self.assertEqual(self.bb.hubs.secrets, {})
        if self.FAIL:   # the plan did fail calls, of every kind that matters
            failed = {m: f.failed for fake in (fa, fb) for m, f in fake.fail.items() if f.failed}
            self.assertGreaterEqual(sum(failed.values()), 5, failed)
            self.assertTrue({"getUpdates"} <= set(failed), failed)
            type(self).failed = failed


class ThroughFlakyBots(ThroughTheBots):
    """The same path with about 30% of Bot API calls failing (502 or a dropped connection): each message once, nothing
    lost. Three failure plans."""
    FAIL, SEED = 0.3, 1   # the three plans fail sending, editing, deleting, receiving and the presses' answers


class ThroughFlakyBots2(ThroughFlakyBots):
    SEED = 3


class ThroughFlakyBots3(ThroughFlakyBots):
    SEED = 9


if __name__ == "__main__":
    unittest.main()
