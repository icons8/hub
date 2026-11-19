"""The Telegram adapter of the owner's chats (modules/telegram/business.py, spec d2108458 slice C0) against a fake Bot
API: only the paired owner's business connection counts; a person's private message becomes chat.received with the
bot's channel and owner false; a session's request the core took makes one sendMessage in his name at once, as an
answer, with the agent's signature — never a second one (a repeat of the event, a network failure, a restart in the
middle)."""
import tests  # noqa: F401 — keeps the machine's settings out
import os
import tempfile
import unittest
from unittest import mock

import platforms
from core import chats, journal, liaison, spool
from modules.telegram import bot as tg
from modules.telegram import business
from tests.test_telegram import FakeBotApi

OWNER = 777
PERSON = 2002


def connection(user=OWNER, enabled=True, reply=True, bid="bc-1"):
    return {"update_id": 1, "business_connection": {"id": bid, "user": {"id": user, "first_name": "Owner"},
                                                    "user_chat_id": user, "date": 1, "is_enabled": enabled,
                                                    "rights": {"can_reply": reply}}}


def business_message(text="when is the call?", frm=PERSON, mid=7, bid="bc-1", chat_type="private", **extra):
    return {"update_id": 2 + mid, "business_message": {
        "business_connection_id": bid, "message_id": mid, "date": 1, "text": text,
        "from": {"id": frm, "first_name": "Anna", "username": "anna_l", **extra}, "chat": {"id": frm, "type": chat_type}}}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hubtgb-")
        self.state = os.path.join(self.dir, "state")
        self.env = mock.patch.dict(os.environ, {"HUB_TELEGRAM_CONFIG": os.path.join(self.dir, "telegram.json"),
                                                "HUB_CONTACTS": os.path.join(self.dir, "contacts.json")})
        self.env.start()
        self.addCleanup(self.env.stop)
        tg.save_config({"token": "1:x", "owners": [OWNER]})
        self.fake = FakeBotApi()
        self.addCleanup(self.fake.server.server_close)
        self.addCleanup(self.fake.server.shutdown)
        self.addCleanup(platforms.current.rmtree, self.dir)
        self.j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        for name, data in (("hub", {"kind": "hub"}), (liaison.NAME, {"liaison": True, "role": "agent", "channel": "core"}),
                           ("maps", {})):
            self.j.append("session.requested", name, {"cwd": f"/w/{name}", "session_id": f"s-{name}", **data})
            self.j.append("session.started", name, {"session_id": f"s-{name}"})
        self.bot = self.start()

    def start(self):
        b = tg.Bot(self.state, tg.Api("1:x", base=self.fake.base))
        b.catch_up()
        return b

    def spooled(self):
        d = os.path.join(self.state, "spool")
        out = [e for _, e in spool.take(d)]
        for n in os.listdir(d) if os.path.isdir(d) else []:
            os.unlink(os.path.join(d, n))
        return out

    def updates(self, *us):
        self.fake.updates = list(us)
        self.bot.poll(timeout=0)

    def ingest(self):
        for e in self.spooled():
            self.j.append(e["type"], e.get("entity"), e.get("data"), src=e.get("src"))

    def connected_with_a_message(self):
        self.updates(connection(), business_message())
        self.ingest()
        self.bot.tick()

    def answer(self, text="Tomorrow at 10.", sender="maps"):
        """The liaison passes the message on and the session answers: the bot reads the request and sends it."""
        st = self.bot.reader.state
        cid = next(iter(st["chats"]["inbox"]))
        self.j.append("chat.pass_requested", None, {"inbox": cid, "session": "maps", "channel": "session",
                                                    "owner": False, "sender": liaison.NAME})
        self.bot.tick()
        self.spooled()
        self.j.append("chat.send_requested", None, {"inbox": cid, "text": text, "channel": "session", "owner": False,
                                                    "sender": sender})
        self.bot.tick()
        return f"o{self.j.seq}"

    def answers(self):
        return [b for b in self.fake.sent() if b.get("business_connection_id")]


class In(Base):
    def test_the_owners_connection_becomes_a_fact_and_a_line_to_him(self):
        self.updates(connection())
        facts = self.spooled()
        self.assertEqual([(f["type"], f["data"]["channel"], f["data"]["owner"], f["src"]) for f in facts],
                         [("chat.connected", "telegram", False, "telegram")])
        self.assertEqual(facts[0]["data"]["user"], {"id": str(OWNER), "name": "Owner"})
        self.assertTrue(facts[0]["data"]["reply"])
        self.assertEqual(self.fake.sent()[-1]["chat_id"], OWNER)

    def test_a_strangers_connection_is_ignored(self):
        self.updates(connection(user=555), business_message(bid="bc-1"))
        self.assertEqual(self.spooled(), [])
        self.assertEqual(self.fake.sent(), [])

    def test_what_is_an_incoming_message(self):
        self.updates(connection())
        self.spooled()
        self.updates(business_message(frm=OWNER, mid=1),            # the owner's own answer in that chat
                     business_message(mid=2, is_bot=True),           # a bot
                     business_message(mid=3, bid="bc-other"),        # another connection
                     business_message(mid=4, chat_type="group"),     # not a private chat
                     business_message(text="  ", mid=5),             # no text
                     business_message(text="hi there", mid=6))
        facts = self.spooled()
        self.assertEqual([f["type"] for f in facts], ["chat.received"])
        d = facts[0]["data"]
        self.assertEqual((d["conv"], d["msg"], d["text"], d["from"]["id"], d["owner"]), (str(PERSON), "6", "hi there",
                                                                                          str(PERSON), False))
        self.assertNotIn("book", d)

    def test_the_book_name_by_the_telegram_id(self):
        from modules.contacts import contacts
        contacts.add_new(os.environ["HUB_CONTACTS"], "Anna Lee", telegram=f"@anna_l:{PERSON}", source="test")
        self.updates(connection(), business_message())
        self.assertEqual([f["data"].get("book") for f in self.spooled() if f["type"] == "chat.received"], ["Anna Lee"])

    def test_disconnecting(self):
        self.updates(connection())
        self.spooled()
        self.updates(connection(enabled=False))
        self.assertEqual([f["type"] for f in self.spooled()], ["chat.disconnected"])


class Out(Base):
    def test_a_sessions_answer_goes_out_at_once_once_in_his_name(self):
        self.connected_with_a_message()
        self.spooled()
        oid = self.answer()
        self.assertEqual([b for b in self.fake.sent() if b.get("reply_markup")], [])   # no card in his Telegram
        got = self.answers()
        self.assertEqual(len(got), 1)
        self.assertEqual((got[0]["business_connection_id"], got[0]["chat_id"], got[0]["reply_parameters"]["message_id"]),
                         ("bc-1", PERSON, 7))
        self.assertTrue(got[0]["text"].startswith("Tomorrow at 10.\n\n— 🤖 "), got[0]["text"])
        self.assertEqual([(f["type"], f["data"]["out"]) for f in self.spooled()], [("chat.sent", oid)])
        # the same event again (a journal reread, a retry of the event) sends nothing
        ev = next(e for e in map(__import__("json").loads, open(os.path.join(self.state, "journal.jsonl")))
                  if e["type"] == "chat.send_requested")
        self.bot.business.outgoing(ev, self.bot.reader.state)
        self.assertEqual(len(self.answers()), 1)

    def test_a_request_the_core_refused_sends_nothing(self):
        self.connected_with_a_message()
        self.answer(sender="hub-x")            # not the liaison, the hub or the session it was passed to
        self.answer("ghp_" + "a" * 36)         # a secret
        self.assertEqual(self.answers(), [])

    def test_no_signature_when_it_is_off(self):
        self.connected_with_a_message()
        with mock.patch.object(business.catalog, "config_or_last", lambda d: {"chats": {"signature": False}}):
            self.answer()
        self.assertEqual(self.answers()[0]["text"], "Tomorrow at 10.")

    def test_a_network_failure_is_unknown_not_a_second_try(self):
        self.connected_with_a_message()
        self.fake.fail["sendMessage"] = [502]
        oid = self.answer()
        self.assertEqual([(f["type"], f["data"]["out"]) for f in self.spooled()], [("chat.send_unknown", oid)])
        self.bot.tick()
        self.assertEqual(self.answers(), [])

    def test_a_refusal_is_a_failure_with_the_reason(self):
        self.connected_with_a_message()
        self.fake.fail["sendMessage"] = [(400, "Bad Request: chat not found")]
        oid = self.answer()
        facts = self.spooled()
        self.assertEqual([(f["type"], f["data"]["out"]) for f in facts], [("chat.send_failed", oid)])
        self.assertIn("chat not found", facts[0]["data"]["why"])

    def test_a_restart_in_the_middle_is_unknown(self):
        self.connected_with_a_message()
        with mock.patch.object(self.bot.business, "send"):   # the bot stops after reading the request, before the call
            oid = self.answer()
        self.bot.st["chat_sent"][oid] = "sending"   # stopped after marking, before Telegram answered
        self.bot.save()
        self.spooled()
        self.bot = self.start()
        self.assertEqual([(f["type"], f["data"]["out"]) for f in self.spooled()], [("chat.send_unknown", oid)])
        ev = next(e for e in map(__import__("json").loads, open(os.path.join(self.state, "journal.jsonl")))
                  if e["type"] == "chat.send_requested")
        self.bot.business.outgoing(ev, self.bot.reader.state)   # even if the request is read again, it does not go
        self.assertEqual(self.answers(), [])

    def test_a_limit_is_a_failure_not_unknown(self):
        self.connected_with_a_message()
        self.fake.fail["sendMessage"] = [429]
        oid = self.answer()
        self.assertEqual([(f["type"], f["data"]["out"]) for f in self.spooled()], [("chat.send_failed", oid)])

    def test_a_dropped_connection_is_not_tried_again(self):
        self.connected_with_a_message()
        self.fake.fail["sendMessage"] = ["drop"]   # a retry on a new connection would succeed — and send it twice
        oid = self.answer()
        self.assertEqual(self.answers(), [])
        self.assertEqual([(f["type"], f["data"]["out"]) for f in self.spooled()], [("chat.send_unknown", oid)])

    def test_no_connection_is_a_failure(self):
        self.connected_with_a_message()
        with mock.patch.object(tg, "_connection", side_effect=ConnectionRefusedError("proxy down")):
            oid = self.answer()
        self.assertEqual([(f["type"], f["data"]["out"]) for f in self.spooled()], [("chat.send_failed", oid)])

    def test_the_fence_marker_is_random(self):
        self.updates(connection(), business_message(mid=1), business_message(mid=2))
        marks = [f["data"]["marker"] for f in self.spooled() if f["type"] == "chat.received"]
        self.assertEqual(len(set(marks)), 2)
        self.assertTrue(all(chats.MARKER.fullmatch(m) for m in marks), marks)


class OnceOnly(unittest.TestCase):
    """An answer to a person goes over a new connection with no retry: a kept connection the proxy dropped after
    Telegram took the message would otherwise send it a second time on a new one."""
    def test_no_second_request_after_a_drop(self):
        import http.client
        requests = []

        class Conn:
            def __init__(self, fail, refuse=False):
                self.fail, self.refuse, self.sock, self.timeout = fail, refuse, None, None

            def connect(self):
                if self.refuse:
                    raise ConnectionRefusedError("refused")
                self.sock = mock.Mock()

            def request(self, *a, **k):
                requests.append(self)
                if self.fail:
                    raise http.client.RemoteDisconnected("dropped")

            def getresponse(self):
                raise AssertionError("no answer expected")

            def close(self):
                self.sock = None

        api = tg.Api("1:x", base="http://203.0.113.5")
        kept = Conn(fail=True)
        kept.connect()
        api.local.conn, api.local.used = kept, api.clock()   # a kept connection, as after earlier calls
        with mock.patch.object(tg, "_connection", lambda *a, **k: Conn(fail=True)):
            with self.assertRaises(tg.TgError) as e:
                api.call("sendMessage", _once=True, chat_id=1, text="x")
        self.assertTrue(e.exception.transient)
        self.assertFalse(e.exception.unsent)   # it left: unknown whether it arrived
        self.assertEqual(len(requests), 1)
        self.assertIsNot(requests[0], kept)
        self.assertIs(api.local.conn, kept)    # the kept one stays for getUpdates
        with mock.patch.object(tg, "_connection", lambda *a, **k: Conn(fail=False, refuse=True)):
            with self.assertRaises(tg.TgError) as e:
                api.call("sendMessage", _once=True, chat_id=1, text="x")
        self.assertTrue(e.exception.unsent)    # no connection at all: certainly not sent


if __name__ == "__main__":
    unittest.main()
