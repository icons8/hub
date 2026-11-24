"""The Telegram module against a fake Bot API: owner pairing, incoming messages as facts into the core's inbox, outgoing
ones from the journal (replies, executors' replies as reply-to, questions as buttons, tools, receipts as reactions)."""
import tests  # noqa: F401 — keeps the machine's settings out (~/.config/hub-core), see tests/__init__.py
import contextlib
import html
import io
import http.client
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

from core import i18n, journal, spool

VOICE = f"🎤 {i18n.t('telegram.voice_head')}: "   # the head the bot writes, in its language
MISSING = i18n.t("telegram.preview_missing", path="").split("`")[0].strip()   # "picture unavailable", in the bot's language
from modules.telegram import bot as tg
from modules.telegram import fmt
from tests.fakes import exe, lang, tz

OWNER = 777


class FakeBotApi:
    def __init__(self):
        self.calls, self.updates, self.next_id, self.fail = [], [], 100, {}
        self.results = {}   # method -> its result, or a function of the request body (the group: getChatAdministrators)
        api = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                method = self.path.rsplit("/", 1)[-1]
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if "multipart/form-data" in (self.headers.get("Content-Type") or ""):  # file upload
                    body = {"multipart": True, "size": len(raw), "raw": raw}
                else:
                    body = json.loads(raw or b"{}")
                fail = api.fail.get(method)
                if not fail:
                    api.calls.append((method, body))   # failed calls are not "sent"
                if fail:   # a failure ordered by the test: an HTTP status of the Bot API (429, 502, 400) or a dropped connection
                    code = fail.pop(0)
                    if code == "drop":
                        self.close_connection = True
                        self.connection.shutdown(2)
                        return
                    if code == "cut":   # the answer breaks off in the middle of the body: http.client.IncompleteRead
                        self.close_connection = True
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", "100")
                        self.end_headers()
                        self.wfile.write(b'{"ok":tr')
                        self.wfile.flush()
                        self.connection.shutdown(2)
                        return
                    why = f"fail {code}"
                    if isinstance(code, tuple):   # (status, the Bot API description) - "message is not modified" and the like
                        code, why = code
                    return self.reply({"ok": False, "error_code": code, "description": why,
                                       **({"parameters": {"retry_after": 1}} if code == 429 else {})})
                result = True
                if method in api.results:
                    got = api.results[method]
                    result = got(body) if callable(got) else got
                    if method in ("sendMessage", "sendPhoto", "sendVoice", "sendDocument", "sendVideo"):
                        api.next_id += 1
                elif method == "getUpdates":
                    result, api.updates = api.updates, []
                elif method in ("sendMessage", "sendVoice", "sendRichMessage"):
                    api.next_id += 1
                    result = {"message_id": api.next_id}
                elif method == "getMe":
                    result = {"username": "test_bot"}
                elif method == "getFile":
                    result = {"file_path": "photos/p.jpg"}
                self.reply({"ok": True, "result": result})

            def do_GET(self):
                fail = api.fail.get("file")   # the file itself (GET /file/bot…): an HTTP status or a dropped connection
                if fail:
                    code = fail.pop(0)
                    if code == "drop":
                        self.close_connection = True
                        self.connection.shutdown(2)
                        return
                    self.send_response(code)
                    self.send_header("Content-Length", "4")
                    self.end_headers()
                    self.wfile.write(b"fail")
                    return
                self.send_response(200)
                self.send_header("Content-Length", "8")
                self.end_headers()
                self.wfile.write(b"JPEGDATA")

            def reply(self, obj):
                data = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def sent(self, method="sendMessage"):
        return [b for m, b in self.calls if m == method]

    def texts(self):
        """What the owner reads in the chat, in order: new messages and card edits (a closed card's result is in
        the card itself)."""
        return [b.get("text") or "" for m, b in self.calls if m in ("sendMessage", "editMessageText")]

    def last_text(self):
        return self.texts()[-1]


class TelegramTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hubtg-")
        self.state = os.path.join(self.dir, "state")
        self.old_cfg = os.environ.get("HUB_TELEGRAM_CONFIG")
        os.environ["HUB_TELEGRAM_CONFIG"] = os.path.join(self.dir, "telegram.json")
        tg.save_config({"token": "1:x", "owners": [OWNER]})
        self.fake = FakeBotApi()
        self.j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        self.j.append("session.requested", "hub", {"cwd": "/w", "session_id": "h", "kind": "hub"})
        self.j.append("session.started", "hub", {"session_id": "h"})
        self.bot = tg.Bot(self.state, tg.Api("1:x", base=self.fake.base))
        self.bot.catch_up()

    def tearDown(self):
        self.fake.server.shutdown()
        self.fake.server.server_close()
        os.environ["HUB_TELEGRAM_CONFIG"] = self.old_cfg  # do not expose the real telegram.json to the following tests
        shutil.rmtree(self.dir, ignore_errors=True)

    def spooled(self):
        d = os.path.join(self.state, "spool")
        out = [e for _, e in spool.take(d)]
        for n in os.listdir(d) if os.path.isdir(d) else []:
            os.unlink(os.path.join(d, n))
        return out

    def msg(self, text, mid, user=OWNER, reply_to=None, **extra):
        m = {"message_id": mid, "from": {"id": user}, "chat": {"id": user, "type": "private"}, "text": text, **extra}
        if reply_to:
            m["reply_to_message"] = {"message_id": reply_to}
        return {"update_id": mid, "message": m}

    def ingest(self):
        """Like hubd: facts from the inbox go to the journal (no fold: the module only cares about recorded events)."""
        for e in self.spooled():
            self.j.append(e["type"], e.get("entity"), e.get("data"), src=e.get("src"))

    def test_history_not_resent_and_owner_message_goes_to_hub(self):
        self.assertEqual(self.fake.sent(), [])  # the first start reads from the end of the journal
        self.fake.updates = [self.msg("check the mail", 5), self.msg("I am a stranger", 6, user=1)]
        self.bot.poll(timeout=0)
        got = self.spooled()
        self.assertEqual(len(got), 1)
        self.assertEqual((got[0]["type"], got[0]["data"]["text"], got[0]["data"]["channel"], got[0]["data"]["owner"]),
                         ("message.received", "check the mail", "telegram", True))
        self.assertNotIn("to", got[0]["data"])  # without an address — to the hub

    def test_message_removed_from_the_queue_is_not_reported_as_a_failure(self):
        # PR #8: the owner took their message out of the queue on the page — Telegram gets a quiet mark, not "👎 Not delivered"
        self.j.append("turn.started", "hub", {"marker": None, "prompt_head": "busy"})
        self.fake.updates = [self.msg("extra from the voice note", 5)]
        self.bot.poll(timeout=0)
        self.ingest()
        self.bot.tick()
        mid = next(f"m{e['seq']}" for e in journal.read(self.j.path) if e["type"] == "message.received")
        self.j.append("message.cancel", mid, {"channel": "web", "owner": True})
        self.bot.tick()
        reactions = [b.get("reaction") for b in self.fake.sent("setMessageReaction")]
        self.assertNotIn([{"type": "emoji", "emoji": "👎"}], reactions)
        texts = [b["text"] for b in self.fake.sent()]
        self.assertFalse([t for t in texts if "Not delivered" in t], texts)
        self.assertTrue([t for t in texts if "Removed from the queue" in t], texts)

    def test_hub_answer_is_a_reply_with_receipts(self):
        self.fake.updates = [self.msg("hello", 5)]
        self.bot.poll(timeout=0)
        self.ingest()  # m3
        self.j.append("turn.started", "hub", {"marker": "m3"})
        self.j.append("session.said", "hub", {"text": "intermediate note"})
        self.j.append("turn.result", "hub", {"text": "**Done**: finished"})
        self.bot.tick()
        sent = self.fake.sent()
        self.assertEqual(len(sent), 1)  # an intermediate one is not sent
        self.assertEqual(sent[0]["text"], "<b>Done</b>: finished")
        self.assertEqual(sent[0]["reply_parameters"]["message_id"], 5)
        self.assertEqual([r["reaction"][0]["emoji"] for r in self.fake.sent("setMessageReaction")], ["👀", "👍"])

    def test_a_handover_result_reaches_the_owner_without_the_core_mark(self):
        self.fake.updates = [self.msg("hello", 5)]
        self.bot.poll(timeout=0)
        self.ingest()  # m3
        self.j.append("turn.started", "hub", {"marker": "m3"})
        self.j.append("turn.result", "hub", {"text": "HANDOFF-DONE\nDone: the report is in the folder"})
        self.bot.tick()
        sent = self.fake.sent()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["text"], "Done: the report is in the folder")

    def test_receipts_off_by_telegram_receipts_false(self):
        self.bot.hub_config = lambda: {"telegram": {"receipts": False}}
        self.fake.updates = [self.msg("hello", 5)]
        self.bot.poll(timeout=0)
        self.ingest()  # m3
        self.j.append("turn.started", "hub", {"marker": "m3"})
        self.j.append("turn.result", "hub", {"text": "**Done**: finished"})
        self.bot.tick()
        self.assertEqual(len(self.fake.sent()), 1)   # the answer itself is there
        self.assertEqual(self.fake.sent("setMessageReaction"), [])

    def test_a_prompt_refused_by_a_session_hook_is_not_delivered_not_answered(self):
        # the result of a hook refusal (Claude Code 2.1.218–2.1.302: success, turns 0) is not an answer:
        # no reply with the hook text, no 👍
        self.fake.updates = [self.msg("key sk-x", 5)]
        self.bot.poll(timeout=0)
        self.ingest()  # m3
        self.j.append("turn.started", "hub", {"marker": "m3"})
        self.j.append("turn.result", "hub", {"text": "UserPromptSubmit operation blocked by hook:\nsecrets in the prompt"
                                             "\n\nOriginal prompt: [hub:m3] key sk-x", "turns": 0, "is_error": False,
                                             "subtype": "success"})
        self.bot.tick()
        texts = [b["text"] for b in self.fake.sent()]
        self.assertEqual(texts, ["Not delivered: blocked by hook: secrets in the prompt."])
        self.assertNotIn("👍", [r["reaction"][0]["emoji"] for r in self.fake.sent("setMessageReaction") if r["reaction"]])

    # --- live feedback: "typing…", the first line of a long turn, "⏳ delayed" — without extra messages -----
    def clocked(self, mid=5, text="hello", **extra):
        """The bot's clock is the test's; the owner's Telegram message is already in the journal (m3)."""
        import time
        self.now = time.time()
        self.bot.clock = lambda: self.now
        self.fake.updates = [self.msg(text, mid, **extra)]
        self.bot.poll(timeout=0)
        self.ingest()
        self.bot.tick()

    def typing(self):
        return [b for b in self.fake.sent("sendChatAction") if b.get("action") == "typing"]

    def test_short_turn_is_one_message_and_typing_while_it_runs(self):
        self.clocked()
        self.j.append("turn.started", "hub", {"marker": "m3"})
        self.bot.tick()
        self.assertEqual([(b["chat_id"], b["action"]) for b in self.typing()], [(OWNER, "typing")])
        self.now += 3
        self.bot.tick()
        self.assertEqual(len(self.typing()), 1)          # no more than once in 5 s
        self.now += 3
        self.bot.tick()
        self.assertEqual(len(self.typing()), 2)
        self.j.append("session.said", "hub", {"text": "accepted"})
        self.now += 20
        self.j.append("turn.result", "hub", {"text": "Done"})
        self.j.append("turn.stopped", "hub", {})
        self.bot.tick()
        n = len(self.typing())
        for _ in range(3):
            self.now += 60
            self.bot.tick()
        self.assertEqual(len(self.typing()), n)          # the turn is over — no more "typing"
        self.assertEqual([(s["text"], s["reply_parameters"]["message_id"]) for s in self.fake.sent()], [("Done", 5)])

    def test_long_turn_sends_its_first_line_then_the_result(self):
        self.clocked()
        self.j.append("turn.started", "hub", {"marker": "m3"})
        self.j.append("session.said", "hub", {"text": "Got it, handing over to demo2"})
        self.j.append("session.said", "hub", {"text": "intermediate"})
        self.bot.tick()
        self.now += 30
        self.bot.tick()
        self.assertEqual(self.fake.sent(), [])           # up to 45 s — nothing, only "typing"
        self.now += 16
        self.bot.tick()
        self.now += 60
        self.bot.tick()
        self.assertEqual([(s["text"], s["reply_parameters"]["message_id"]) for s in self.fake.sent()],
                         [("Got it, handing over to demo2", 5)])  # once, as a reply to his message
        self.j.append("turn.result", "hub", {"text": "Done: demo2 took it"})
        self.bot.tick()
        self.assertEqual([s["text"] for s in self.fake.sent()], ["Got it, handing over to demo2", "Done: demo2 took it"])
        self.assertEqual(self.fake.sent()[-1]["reply_parameters"]["message_id"], 5)

    def test_long_turn_of_an_addressed_session_and_no_duplicate_result(self):
        # the owner replied to session w: its first line carries a signature; a result equal to the first line is not duplicated
        self.j.append("session.requested", "w", {"cwd": "/w", "session_id": "w"})
        self.j.append("session.started", "w", {"session_id": "w"})
        self.bot.tick()
        self.bot.st["routes"]["40"] = {"session": "w"}
        self.clocked(reply_to=40)
        mid = next(f"m{e['seq']}" for e in journal.read(self.j.path) if e["type"] == "message.received")
        self.j.append("turn.started", "w", {"marker": mid})
        self.j.append("session.said", "w", {"text": "Looking"})
        self.bot.tick()
        self.now += 46
        self.bot.tick()
        self.j.append("turn.result", "w", {"text": "Looking"})
        self.bot.tick()
        self.assertEqual([s["text"] for s in self.fake.sent()], ["<b>w</b>:\nLooking"])
        self.assertEqual([r["reaction"][0]["emoji"] for r in self.fake.sent("setMessageReaction")], ["👀", "👍"])

    def test_late_notice_once_when_nothing_was_sent(self):
        self.j.append("turn.started", "hub", {"marker": None})         # the hub is busy with something else — the message is queued
        self.j.append("session.recap", "hub", {"text": "reading the build logs", "by": "watch"})
        self.clocked()
        self.now += 599
        self.bot.tick()
        self.assertEqual(self.fake.sent(), [])
        self.now += 2
        self.bot.tick()
        self.now += 600
        self.bot.tick()
        (late,) = self.fake.sent()
        # who is busy and with what, in one sentence: not "the reply is delayed: hub — <its line>" glued together
        self.assertEqual(late["text"], "⏳ The hub is still busy, your message is in the queue. Now: reading the build logs")
        self.assertEqual(late["reply_parameters"]["message_id"], 5)

    def test_in_his_private_chat_a_mention_is_still_words_for_the_hub(self):
        # only in a group do people talk to each other; here "@name …" is his message to the hub, with live feedback
        self.clocked(text="@julia asked for the totals", entities=[{"type": "mention", "offset": 0, "length": 6}])
        (ev,) = [e for e in journal.read(self.j.path) if e["type"] == "message.received"]
        self.assertNotIn("fyi", ev["data"])
        self.assertIn(f"m{ev['seq']}", self.bot.st["live"])

    def test_the_late_notice_of_a_session_on_its_turn_names_it_and_its_work(self):
        self.j.append("session.requested", "w", {"cwd": "/w", "session_id": "w"})
        self.j.append("session.started", "w", {"session_id": "w"})
        self.bot.tick()
        self.bot.st["routes"]["40"] = {"session": "w"}
        self.clocked(reply_to=40)
        mid = next(f"m{e['seq']}" for e in journal.read(self.j.path) if e["type"] == "message.received")
        self.j.append("turn.started", "w", {"marker": mid})
        self.j.append("session.recap", "w", {"text": "rendering the second scene", "by": "watch"})
        self.bot.tick()
        self.now += 601
        self.bot.tick()
        (late,) = [b for b in self.fake.sent() if "⏳" in b["text"]]
        self.assertEqual(late["text"], "⏳ <code>w</code> is still busy: rendering the second scene")

    def test_a_session_that_runs_no_turn_is_not_called_busy(self):
        self.j.append("session.requested", "w", {"cwd": "/w", "session_id": "w"})
        self.j.append("session.started", "w", {"session_id": "w"})
        self.bot.tick()
        self.bot.st["routes"]["40"] = {"session": "w"}
        self.clocked(reply_to=40)   # its turn has not started (the core has not typed it yet)
        self.now += 601
        self.bot.tick()
        (late,) = [b for b in self.fake.sent() if "⏳" in b["text"]]
        self.assertEqual(late["text"], "⏳ No answer from <code>w</code> yet: free")

    def test_the_late_notice_does_not_pass_the_last_turns_line_for_the_current_one(self):
        # the model of the "what it is working on" line hit its limit: there is no new line — do not show the previous turn's line
        self.j.append("turn.started", "hub", {"marker": None})
        self.j.append("session.recap", "hub", {"text": "reading the build logs", "by": "watch"})
        self.j.append("turn.result", "hub", {"text": "done"})
        self.j.append("turn.started", "hub", {"marker": None})         # a new turn — no line about it yet
        self.clocked()
        self.now += 601
        self.bot.tick()
        self.now += 600
        self.bot.tick()
        (late,) = self.fake.sent()
        self.assertNotIn("reading the build logs", late["text"])
        self.assertIn("working", late["text"])
        self.j.append("session.recap", "hub", {"text": "fixing the build", "by": "watch"})   # this turn's line is shown
        self.bot.tick()
        self.assertEqual(self.bot.doing(self.bot.reader.state["sessions"]["hub"]), "fixing the build")
        self.j.append("turn.result", "hub", {"text": "done"})        # the turn is over — the line is no longer "now"
        self.bot.tick()
        self.assertNotIn("fixing", self.bot.doing(self.bot.reader.state["sessions"]["hub"]))

    def test_no_late_notice_when_answered_or_a_card_came(self):
        self.clocked()
        self.j.append("turn.started", "hub", {"marker": "m3"})
        self.j.append("turn.result", "hub", {"text": "Done"})
        self.j.append("turn.stopped", "hub", {})
        self.bot.tick()
        self.now += 700
        self.bot.tick()
        self.assertEqual([s["text"] for s in self.fake.sent()], ["Done"])
        # the session he replied to asked with a card — that is an answer, no "delayed" notice needed
        self.j.append("session.requested", "w", {"cwd": "/w", "session_id": "w"})
        self.j.append("session.started", "w", {"session_id": "w"})
        self.bot.tick()
        self.bot.st["routes"]["40"] = {"session": "w"}
        self.fake.calls.clear()
        self.clocked(mid=6, text="what about the tests?", reply_to=40)
        mid = [f"m{e['seq']}" for e in journal.read(self.j.path) if e["type"] == "message.received"][-1]
        self.j.append("turn.started", "w", {"marker": mid})
        self.j.append("permission.requested", "w", {"request_id": "r1", "tool": "AskUserQuestion", "input": {
            "questions": [{"question": "Run all?", "options": [{"label": "Yes"}]}]}})
        self.bot.tick()
        self.now += 700
        self.bot.tick()
        self.assertFalse([s for s in self.fake.sent() if "⏳" in s["text"]])

    def test_idle_ticks_do_not_rewrite_the_state_file(self):
        # 15.10: the bot rewrote its state file (1.5 MB) on every loop pass — ~60 MB of writes per minute to the
        # server's disk with nothing new; we write only when the state has changed
        import os
        self.bot.tick()
        path = self.bot.path
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns - 10**9))
        before = os.stat(path).st_mtime_ns
        for _ in range(3):
            self.bot.tick()
        self.assertEqual(os.stat(path).st_mtime_ns, before)
        self.j.append("session.said", "hub", {"text": "New"})
        self.bot.tick()
        self.assertNotEqual(os.stat(path).st_mtime_ns, before)

    def test_state_file_does_not_grow_without_end(self):
        # 15.10: 8 thousand buttons of long-answered questions and 5.6 thousand reply addresses — 1.5 MB on every write
        st = self.bot.st
        st["asks"] = {f"a{i}": {"session": "w", "request_id": f"r{i}", "done": True} for i in range(300)}
        st["asks"]["open"] = {"session": "w", "request_id": "ro", "questions": ["?"], "answers": {}, "msgs": []}
        st["keys"] = {f"k{i}": {"do": "answer", "ask": f"a{i}", "q": 0, "label": "Yes"} for i in range(300)}
        st["keys"]["live"] = {"do": "answer", "ask": "open", "q": 0, "label": "Yes"}
        st["keys"].update({f"t{i}": {"do": "tool", "session": "w", "tool": "x", "yes": True} for i in range(500)})
        st["routes"] = {str(i): {"session": "w"} for i in range(5000)}
        self.bot.save()
        self.assertEqual(len([a for a in st["asks"].values() if a.get("done")]), tg.KEEP_ASKS)
        self.assertIn("open", st["asks"])
        self.assertEqual([k for k, v in st["keys"].items() if v["do"] == "answer"], ["live"])
        self.assertEqual(len([k for k, v in st["keys"].items() if v["do"] == "tool"]), tg.KEEP_TOOL_KEYS)
        self.assertIn("t499", st["keys"])
        self.assertEqual(len(st["routes"]), tg.KEEP_ROUTES)
        self.assertIn("4999", st["routes"])

    def test_no_stale_feedback_after_a_restart(self):
        import time
        # 1) the turn ended while the bot was down: after startup — the result, as before, but no "typing", no first line, no ⏳
        self.clocked()
        self.bot.save()
        self.j.append("turn.started", "hub", {"marker": "m3"})
        self.j.append("session.said", "hub", {"text": "Got it"})
        self.j.append("turn.result", "hub", {"text": "Done"})
        self.j.append("turn.stopped", "hub", {})
        later = self.now + 1500
        bot = tg.Bot(self.state, tg.Api("1:x", base=self.fake.base), clock=lambda: later)
        bot.catch_up()
        for later in (later, later + 60, later + 700):
            bot.tick()
        self.assertEqual([s["text"] for s in self.fake.sent()], ["Done"])
        self.assertEqual(self.typing(), [])
        # 2) a message two hours old, the turn for it still "running" in the journal — we stay silent
        self.fake.calls.clear()
        old = time.time() - 7200
        ev = self.j.append("message.received", None, {"text": "old", "tg": {"chat": OWNER, "id": 9},
                                                      "channel": "telegram", "owner": True}, src="telegram", ts=old)
        self.j.append("turn.started", "hub", {"marker": f"m{ev['seq']}"}, ts=old + 1)
        self.j.append("session.said", "hub", {"text": "Got it"}, ts=old + 2)
        now = time.time()
        bot = tg.Bot(self.state, tg.Api("1:x", base=self.fake.base), clock=lambda: now)
        bot.catch_up()
        for now in (now, now + 60, now + 700):
            bot.tick()
        self.assertEqual([m for m, _ in self.fake.calls if m in ("sendMessage", "sendChatAction")], [])

    def test_executor_reply_and_owner_answers_it(self):
        self.fake.updates = [self.msg("calculate the revenue", 5)]
        self.bot.poll(timeout=0)
        self.ingest()  # m3
        self.j.append("session.requested", "w", {"cwd": "/w", "session_id": "w"})
        self.j.append("session.started", "w", {"session_id": "w"})
        self.j.append("delegation.created", None, {"session": "w", "for": "m3", "channel": "session", "owner": False,
                                                   "sender": "hub"})  # d6, task m6
        self.j.append("turn.started", "w", {"marker": "m6"})
        self.j.append("turn.result", "w", {"text": "42 000 $"})
        self.bot.tick()
        sent = self.fake.sent()[-1]
        self.assertEqual((sent["text"], sent["reply_parameters"]["message_id"]), ("<b>w</b> replies:\n42 000 $", 5))
        self.fake.updates = [self.msg("and for August?", 9, reply_to=self.fake.next_id)]
        self.bot.poll(timeout=0)
        got = self.spooled()[0]["data"]
        self.assertEqual((got["to"], got["reply_to"], got["text"]), ("w", "d6", "and for August?"))

    def test_an_intermediate_result_of_a_minihub_reaches_telegram_like_any_result(self):
        # the mini-hub gave the owner's task to an executor: its turn result ("delegated …") goes to the owner like any
        # result, into the same thread (the bot's own filter also muted a card answer forwarded by the hub; review 15.10)
        self.fake.updates = [self.msg("fix the CI", 5)]
        self.bot.poll(timeout=0)
        self.ingest()  # m3
        self.j.append("session.requested", "w", {"cwd": "/w", "session_id": "w", "role": "agent", "about": "W"})
        self.j.append("session.started", "w", {"session_id": "w"})
        self.j.append("owner.settings", None, {"timezone": "UTC", "parallel": 3, "channel": "core"})
        self.j.append("session.folder", "w", {"channel": "core", "git": "ok", "clean": True})   # mini-hub: the folder is clean
        self.j.append("session.started", "w", {"session_id": "w"})
        hub ={"channel": "session", "owner": False, "sender": "hub"}
        first = self.j.append("delegation.created", None, {"session": "w", "for": "m3", **hub})
        self.j.append("turn.started", "w", {"marker": f"m{first['seq']}"})
        self.j.append("turn.result", "w", {"text": "looked: an executor is needed"})
        self.j.append("turn.stopped", "w", {})
        again = self.j.append("delegation.created", None, {"session": "w", "for": "m3", **hub})
        self.j.append("turn.started", "w", {"marker": f"m{again['seq']}"})
        self.j.append("task.run_requested", "w-ci", {"task": "fix the CI", "channel": "session", "owner": False,
                                                     "sender": "w"})
        self.j.append("turn.result", "w", {"text": "delegated to w-ci"})
        self.j.append("turn.stopped", "w", {})
        self.bot.tick()
        texts = [s["text"] for s in self.fake.sent()]
        self.assertIn("<b>w</b> replies:\nlooked: an executor is needed", texts)
        self.assertTrue([t for t in texts if "delegated to w-ci" in t], texts)

    def test_an_executor_of_a_minihub_never_reaches_telegram(self):
        # the bus (owner decision 2026-10-15): the owner talks only to the mini-hub — the mini-hub executor's result,
        # interruption and form do not go to Telegram, even when its task comes from the owner's task from Telegram
        self.fake.updates = [self.msg("fix the CI", 5)]
        self.bot.poll(timeout=0)
        self.ingest()  # m3
        self.j.append("session.requested", "w", {"cwd": "/w", "session_id": "w", "role": "agent", "about": "W"})
        self.j.append("session.started", "w", {"session_id": "w"})
        self.j.append("owner.settings", None, {"timezone": "UTC", "parallel": 3, "channel": "core"})
        self.j.append("session.folder", "w", {"channel": "core", "git": "ok", "clean": True})   # mini-hub: the folder is clean
        self.j.append("session.started", "w", {"session_id": "w"})
        w ={"channel": "session", "owner": False, "sender": "w"}
        task = self.j.append("delegation.created", None, {"session": "w", "for": "m3", "channel": "session",
                                                           "owner": False, "sender": "hub"})
        self.j.append("turn.started", "w", {"marker": f"m{task['seq']}"})
        self.j.append("task.run_requested", "w-ci", {"task": "fix the CI", **w})
        self.j.append("turn.result", "w", {"text": "delegated to w-ci"})
        self.j.append("turn.stopped", "w", {})
        self.j.append("session.requested", "w-ci", {"cwd": "/w2", "session_id": "w2", "executor": True,
                                                    "raised_by": "w"})
        sub = self.j.append("delegation.created", None, {"session": "w-ci", "for_session": "w", "context": "CI", **w})
        self.j.append("session.started", "w-ci", {"session_id": "w2"})
        self.j.append("turn.started", "w-ci", {"marker": f"m{sub['seq']}"})
        self.j.append("session.said", "w-ci", {"text": "looking at the CI"})
        self.j.append("permission.requested", "w-ci", {"request_id": "r1", "tool": "AskUserQuestion", "interactive": True,
                                                       "input": {"questions": [{"question": "Merge?", "options": [
                                                           {"label": "Yes"}, {"label": "No"}]}]}})
        self.j.append("turn.result", "w-ci", {"text": "CI is green, MR !7"})
        self.j.append("turn.stopped", "w-ci", {})
        self.j.append("turn.started", "w-ci", {"marker": None})
        self.j.append("turn.result", "w-ci", {"text": "You've hit your session limit · resets 5pm", "is_error": True})
        self.bot.tick()
        texts = [s["text"] for s in self.fake.sent()]
        self.assertFalse([t for t in texts if "w-ci</b>" in t or "MR !7" in t or "Merge?" in t or "limit" in t.lower()],
                         texts)
        # the owner's own command to an executor (an action on the session, not words to it) — the answer goes to him, as usual
        cmd = self.j.append("message.received", None, {"to": "w-ci", "text": "/context", "channel": "telegram",
                                                        "owner": True, "tg": {"id": 11, "chat": OWNER}})
        self.j.append("turn.started", "w-ci", {"marker": f"m{cmd['seq']}"})
        self.j.append("turn.result", "w-ci", {"text": "context: 12%"})
        self.bot.tick()
        sent = self.fake.sent()[-1]
        self.assertEqual((sent["text"], sent["reply_parameters"]["message_id"]), ("<b>w-ci</b>:\ncontext: 12%", 11))

    def test_an_answer_sharing_the_turn_with_a_delegated_task_reaches_telegram(self):
        # the mini-hub gives a task to a session, mid-turn the hub urgently (--now) inserted the owner's task from
        # Telegram: the turn result is intermediate for the first and an answer to the second — the answer goes to him
        self.j.append("session.requested", "w", {"cwd": "/w", "session_id": "w", "role": "agent", "about": "W"})
        self.j.append("session.started", "w", {"session_id": "w"})
        self.j.append("session.requested", "x", {"cwd": "/x", "session_id": "x"})
        self.j.append("session.started", "x", {"session_id": "x"})
        self.j.append("owner.settings", None, {"timezone": "UTC", "parallel": 3, "channel": "core"})
        self.j.append("session.folder", "w", {"channel": "core", "git": "ok", "clean": True})   # mini-hub: the folder is clean
        self.j.append("session.started", "w", {"session_id": "w"})
        task = self.j.append("delegation.created", None, {"session": "w", "for_session": "x", "context": "T",
                                                           "channel": "session", "owner": False, "sender": "x"})
        self.j.append("turn.started", "w", {"marker": f"m{task['seq']}"})
        self.fake.updates = [self.msg("and collect the log", 5)]
        self.bot.poll(timeout=0)
        (said,) = self.spooled()   # like hubd: his message goes to the journal
        mid = f"m{self.j.append(said['type'], said.get('entity'), said.get('data'), src=said.get('src'))['seq']}"
        now = self.j.append("delegation.created", None, {"session": "w", "for": mid, "now": True, "channel": "session",
                                                          "owner": False, "sender": "hub"})
        self.j.append("turn.started", "w", {"marker": f"m{now['seq']}"})
        self.j.append("task.run_requested", "w-ci", {"task": "T", "channel": "session", "owner": False, "sender": "w"})
        self.j.append("turn.result", "w", {"text": "delegated to w-ci; log collected"})
        self.j.append("turn.stopped", "w", {})
        self.bot.tick()
        self.assertIn("<b>w</b> replies:\ndelegated to w-ci; log collected", [s["text"] for s in self.fake.sent()])

    def test_executor_followup_reaches_owner(self):
        """30.09: after "waiting for the agent" Claude Code wakes the session itself — its result goes to him too
        (since 25.10 without a quote)."""
        self.fake.updates = [self.msg("fix the site map", 5)]
        self.bot.poll(timeout=0)
        self.ingest()  # m3
        self.j.append("session.requested", "w", {"cwd": "/w", "session_id": "w"})
        self.j.append("session.started", "w", {"session_id": "w"})
        self.j.append("delegation.created", None, {"session": "w", "for": "m3", "channel": "session", "owner": False,
                                                   "sender": "hub"})  # d6, task m6
        self.j.append("turn.started", "w", {"marker": "m6"})
        self.j.append("turn.result", "w", {"text": "waiting for the agent"})
        self.j.append("turn.stopped", "w", {})
        self.j.append("turn.started", "w", {"marker": None})
        self.j.append("turn.result", "w", {"text": "done: MR !31"})
        self.bot.tick()
        texts = [(s["text"], s.get("reply_parameters", {}).get("message_id")) for s in self.fake.sent()]
        self.assertIn(("<b>w</b> replies:\nwaiting for the agent", 5), texts)
        # the result after it is the same session message but without a quote: one quote, for the first reply (25.10)
        self.assertIn(("<b>w</b> · on the task “fix the site map”:\ndone: MR !31", None), texts)

    def task_for_owner_message(self, text="shall we finish the rest on sqlalchemy?"):
        """His message (m3, Telegram 5) -> the hub created a task for session w with --for m3 (like a mini-hub's on 25.10)."""
        self.fake.updates = [self.msg(text, 5)]
        self.bot.poll(timeout=0)
        self.ingest()  # m3
        self.j.append("session.requested", "w", {"cwd": "/w", "session_id": "w"})
        self.j.append("session.started", "w", {"session_id": "w"})
        self.j.append("delegation.created", None, {"session": "w", "for": "m3", "channel": "session", "owner": False,
                                                   "sender": "hub", "words": text})  # d6, task m6
        self.j.append("turn.started", "w", {"marker": "m6"})

    def test_only_the_first_result_of_a_task_quotes_the_owners_message(self):
        # 25.10: every mini-hub turn on a task "for m3" went out as "w replies:" with a quote of his question —
        # dozens of "replies"
        self.task_for_owner_message()
        self.j.append("turn.result", "w", {"text": "started: stages 2 and 3"})
        self.j.append("turn.stopped", "w", {})
        for n in range(3):   # executors' reports wake the session: turns without a label, each again a result of the same task
            self.j.append("turn.started", "w", {"marker": None})
            self.j.append("turn.result", "w", {"text": f"report {n}"})
            self.j.append("turn.stopped", "w", {})
        self.bot.tick()
        sent = [(s["text"], s.get("reply_parameters", {}).get("message_id")) for s in self.fake.sent()]
        self.assertEqual(sent[0], ("<b>w</b> replies:\nstarted: stages 2 and 3", 5))
        # the rest all arrive, as ordinary session messages — with the task title, without a quote
        self.assertEqual([t.split("\n")[1] for t, _ in sent[1:]], ["report 0", "report 1", "report 2"])
        for text, quoted in sent[1:]:
            self.assertEqual(quoted, None, text)
            self.assertTrue(text.startswith("<b>w</b> · on the task “shall we finish the rest on sqlalchemy?”:\n"), text)
        reacted = [b for m, b in self.fake.calls if m == "setMessageReaction"]
        self.assertEqual([b["message_id"] for b in reacted], [5])   # 👍 once, on the first reply
        # the owner's reply to any of them goes by the task, as before
        self.fake.updates = [self.msg("is there an MR already?", 9, reply_to=self.fake.next_id)]
        self.bot.poll(timeout=0)
        got = self.spooled()[0]["data"]
        self.assertEqual((got["to"], got["reply_to"]), ("w", "d6"))

    def test_the_quote_is_kept_for_a_first_result_that_failed_to_send_and_not_doubled_on_retry(self):
        self.task_for_owner_message()
        self.fake.fail["sendMessage"] = [502]   # the first send failed: the retry is the same reply with a quote
        self.j.append("turn.result", "w", {"text": "first"})
        self.j.append("turn.stopped", "w", {})
        self.bot.tick()
        self.assertEqual(self.fake.sent(), [])
        self.bot.hold_until = 0
        self.bot.tick()
        self.assertEqual([(s["text"], s["reply_parameters"]["message_id"]) for s in self.fake.sent()],
                         [("<b>w</b> replies:\nfirst", 5)])
        self.j.append("turn.started", "w", {"marker": None})
        self.j.append("turn.result", "w", {"text": "second"})
        self.j.append("turn.stopped", "w", {})
        self.fake.fail["sendMessage"] = [502]   # the second one and its retry — without a quote, once
        self.bot.tick()
        self.bot.hold_until = 0
        self.bot.tick()
        self.assertEqual([("reply_parameters" in s, s["text"].split("\n")[1]) for s in self.fake.sent()],
                         [(True, "first"), (False, "second")])

    def test_the_record_of_quoted_messages_forgets_the_silent_session_not_the_busy_one(self):
        self.bot.mark_quoted("busy", 1)
        for n in range(60):
            self.bot.mark_quoted(f"s{n}", n)
            self.bot.mark_quoted("busy", 100 + n)   # a busy session goes to the end every time
        self.assertEqual(len(self.bot.st["quoted"]), 50)
        self.assertIn("busy", self.bot.st["quoted"])
        self.assertNotIn("s0", self.bot.st["quoted"])

    def test_a_recheck_after_a_surrender_goes_to_the_same_thread(self):
        # the concession detector (06.10): a session answers anew to a core message (re is the owner's message) — as a reply to it
        self.fake.updates = [self.msg("are you sure the port is 8080?", 5)]
        self.bot.poll(timeout=0)
        self.ingest()  # m3
        ev = self.j.append("watch.surrender", "hub", {"message": "m3", "channel": "cli", "owner": True})
        self.j.append("turn.started", "hub", {"marker": f"m{ev['seq']}"})
        self.j.append("turn.result", "hub", {"text": "Rechecked the config, keeping 8080"})
        self.bot.tick()
        sent = self.fake.sent()[-1]
        self.assertEqual((sent["text"], sent["reply_parameters"]["message_id"]), ("Rechecked the config, keeping 8080", 5))

    def test_a_sessions_result_after_the_hub_relayed_the_owners_answer_goes_to_his_thread(self):
        # 07.10: the owner answered a session's card to the hub from Telegram ("roll it out"), the hub passed on `hub send
        # dev --for m3` — the session's result goes to him, as a reply to his message, not only to the session feed
        self.fake.updates = [self.msg("roll it out", 5)]
        self.bot.poll(timeout=0)
        self.ingest()  # m3 goes to the hub
        self.j.append("session.requested", "dev", {"cwd": "/w", "session_id": "dev"})
        self.j.append("session.started", "dev", {"session_id": "dev"})
        ev = self.j.append("message.received", None, {"to": "dev", "text": "Owner: roll it out", "for": "m3",
                                                      "channel": "session", "owner": False, "sender": "hub"})
        self.j.append("turn.started", "dev", {"marker": f"m{ev['seq']}"})
        self.j.append("turn.result", "dev", {"text": "Released"})
        self.bot.tick()
        sent = self.fake.sent()[-1]
        self.assertEqual((sent["text"], sent["reply_parameters"]["message_id"]), ("<b>dev</b>:\nReleased", 5))

    def relayed_turn(self, to="devops"):
        """The owner asked the hub from Telegram (his message 5), the hub passed on `hub send devops --for m3`,
        devops is mid-turn."""
        self.fake.updates = [self.msg("bring up the test", 5)]
        self.bot.poll(timeout=0)
        self.ingest()  # m3 goes to the hub
        for n in (to, "dev"):
            self.j.append("session.requested", n, {"cwd": "/w", "session_id": n})
            self.j.append("session.started", n, {"session_id": n})
        ev = self.j.append("message.received", None, {"to": to, "text": "Owner asks: bring up the test", "for": "m3",
                                                      "channel": "session", "owner": False, "sender": "hub"})
        self.j.append("turn.started", to, {"marker": f"m{ev['seq']}"})

    def test_a_message_injected_mid_turn_does_not_take_the_owners_thread(self):
        # 13.10: mid-turn of devops, on the owner's words, the core inserted a message from dev — the turn result went out
        # as "dev's answer", to the page only; the owner reported that this answer did not reach Telegram either
        self.relayed_turn()
        ev = self.j.append("message.received", None, {"to": "devops", "text": "MR merged", "channel": "session",
                                                      "owner": False, "sender": "dev"})
        self.j.append("message.inject", f"m{ev['seq']}", {"channel": "core", "owner": False})
        self.j.append("turn.started", "devops", {"marker": f"m{ev['seq']}"})
        self.j.append("turn.result", "devops", {"text": "The test is running, I will write the outcome"})
        self.bot.tick()
        sent = self.fake.sent()[-1]
        self.assertEqual((sent["text"], sent["reply_parameters"]["message_id"]),
                         ("<b>devops</b>:\nThe test is running, I will write the outcome", 5))

    def test_the_continuation_of_the_owners_chain_goes_to_his_thread(self):
        # 13.10: devops answered the owner and handed the MR to dev; dev's reply "MR is ready" started a new devops
        # turn — its result (the MR link) stayed on the page; the owner could not see the MR
        self.relayed_turn()
        ask = self.j.append("message.received", None, {"to": "dev", "text": "make an MR", "channel": "session",
                                                       "owner": False, "sender": "devops"})
        self.j.append("turn.result", "devops", {"text": "Handed the MR to dev"})
        self.j.append("turn.stopped", "devops", {})
        self.j.append("turn.started", "dev", {"marker": f"m{ask['seq']}"})
        back = self.j.append("message.received", None, {"to": "devops", "text": "MR is ready: !332", "channel": "session",
                                                        "owner": False, "sender": "dev"})
        self.j.append("turn.result", "dev", {"text": "Made MR !332"})
        self.j.append("turn.stopped", "dev", {})
        self.j.append("turn.started", "devops", {"marker": f"m{back['seq']}"})
        self.j.append("turn.result", "devops", {"text": "MR !332 can be merged"})
        self.j.append("turn.stopped", "devops", {})
        self.bot.tick()
        got = [(b["text"], b["reply_parameters"]["message_id"]) for b in self.fake.sent() if b.get("reply_parameters")]
        self.assertEqual(got, [("<b>devops</b>:\nHanded the MR to dev", 5), ("<b>devops</b>:\nMR !332 can be merged", 5)])

    def test_a_turn_without_a_message_does_not_continue_the_thread(self):
        # a watcher, a Claude Code schedule (/loop) — a turn without a label: "MR not merged yet" every 2 min is not for the owner
        self.relayed_turn()
        self.j.append("turn.result", "devops", {"text": "Handed the MR to dev"})
        self.j.append("turn.stopped", "devops", {})
        self.j.append("turn.started", "devops", {"marker": None})
        self.j.append("turn.result", "devops", {"text": "MR not merged yet"})
        self.bot.tick()
        self.assertNotIn("<b>devops</b>:\nMR not merged yet", [b["text"] for b in self.fake.sent()])

    def test_the_continuation_through_a_task_goes_to_his_thread(self):
        # review 13.10: the work was handed over as a task (hub task new) — the core delivers the task result as its own
        # message, without a sender; devops' next turn ("MR can be merged") also goes into his thread, as with hub send
        self.relayed_turn()
        task = self.j.append("delegation.created", None, {"session": "dev", "for": None, "for_session": "devops",
                                                          "context": "make an MR", "channel": "session", "owner": False,
                                                          "sender": "devops"})
        self.j.append("turn.result", "devops", {"text": "Handed the MR to dev"})
        self.j.append("turn.stopped", "devops", {})
        st = journal.load_state(self.state)
        d = next(x for x in st["delegations"].values() if x["session"] == "dev")
        self.j.append("turn.started", "dev", {"marker": d["message"]})
        self.j.append("turn.result", "dev", {"text": "Made MR !332"})
        self.j.append("turn.stopped", "dev", {})
        st =journal.load_state(self.state)
        back = next(m for m in st["messages"].values() if m.get("session") == "devops" and m.get("task") == d["id"])
        self.j.append("turn.started", "devops", {"marker": back["id"]})
        self.j.append("turn.result", "devops", {"text": "MR !332 can be merged"})
        self.bot.tick()
        got = [(b["text"], b["reply_parameters"]["message_id"]) for b in self.fake.sent() if b.get("reply_parameters")]
        self.assertIn(("<b>devops</b>:\nMR !332 can be merged", 5), got)
        self.assertIsNotNone(task)

    def test_a_wake_up_mid_turn_keeps_the_owners_thread(self):
        # review 13.10: mid-turn on his words a background command finished — Claude Code starts a turn without a label;
        # the turn result still goes to him
        self.relayed_turn()
        self.j.append("turn.started", "devops", {"marker": None})
        self.j.append("turn.result", "devops", {"text": "The test passed"})
        self.bot.tick()
        sent = self.fake.sent()[-1]
        self.assertEqual((sent["text"], sent["reply_parameters"]["message_id"]), ("<b>devops</b>:\nThe test passed", 5))

    def test_a_schedule_set_in_the_owners_chain_does_not_continue_his_thread(self):
        # review 13.10: devops answered him and in the same turn set up a scheduled check (its start is in his chain):
        # every run of "MR not merged yet" would go to him as a reply — a continuation only on another session's message
        self.relayed_turn()
        self.j.append("schedule.set", "chk", {"session": "devops", "every": "5m", "prompt": "check the MR",
                                              "channel": "session", "owner": False, "sender": "devops"})
        self.j.append("turn.result", "devops", {"text": "Handed the MR to dev"})
        self.j.append("turn.stopped", "devops", {})
        due = self.j.append("schedule.due", "chk", {"slot": time.time()})
        self.j.append("turn.started", "devops", {"marker": f"m{due['seq']}"})
        self.j.append("turn.result", "devops", {"text": "MR not merged yet"})
        self.bot.tick()
        from core import chain
        self.assertEqual(chain.root(self.bot.reader.state, f"m{due['seq']}"), "m3")   # the check is in his chain
        self.assertNotIn("<b>devops</b>:\nMR not merged yet", [b["text"] for b in self.fake.sent()])

    def test_an_injected_message_of_another_chain_does_not_take_over_the_owners_thread(self):
        # review 13.10: mid-turn of devops, on his words from Telegram, a message from dev was inserted from a chain started
        # on the page — the turn result goes to him (his request), but the page's chain does not become his thread
        self.relayed_turn()
        self.j.append("message.received", None, {"to": "dev", "text": "take a look at the review", "channel": "web", "owner": True})
        page = f"m{self.j.seq}"
        self.j.append("turn.started", "dev", {"marker": page})
        ev = self.j.append("message.received", None, {"to": "devops", "text": "review is ready", "channel": "session",
                                                      "owner": False, "sender": "dev"})
        self.j.append("message.inject", f"m{ev['seq']}", {"channel": "core", "owner": False})
        self.j.append("turn.started", "devops", {"marker": f"m{ev['seq']}"})
        self.j.append("turn.result", "devops", {"text": "The test is up"})
        self.j.append("turn.stopped", "devops", {})
        again = self.j.append("message.received", None, {"to": "devops", "text": "one more fix", "channel": "session",
                                                         "owner": False, "sender": "dev"})
        self.j.append("turn.started", "devops", {"marker": f"m{again['seq']}"})
        self.j.append("turn.result", "devops", {"text": "Saw the fix"})
        self.bot.tick()
        sent = [b["text"] for b in self.fake.sent()]
        self.assertIn("<b>devops</b>:\nThe test is up", sent)
        self.assertNotIn("<b>devops</b>:\nSaw the fix", sent)

    def test_quiet_result_of_the_hub_is_not_sent(self):
        # "—" is a quiet turn result (a schedule job with nothing new, a break in the feed): we do not send it to the owner
        self.j.append("turn.started", "hub", {"marker": None})
        self.j.append("turn.result", "hub", {"text": "—"})
        self.bot.tick()
        self.assertFalse([b for b in self.fake.sent() if b.get("text", "").strip().endswith("—")])

    # --- a failure is not an answer (07.10: 22 results "You've hit your session limit · resets …" went out as answers) ---
    LIMIT = "You've hit your session limit · resets 6am (Europe/Paris)"

    def failed(self, who, text=LIMIT):
        """As in the journal on 07.10: a line with the failure text, then a result with subtype success + is_error."""
        self.j.append("session.said", who, {"text": text})
        self.j.append("turn.result", who, {"text": text, "subtype": "success", "is_error": True, "duration_ms": 796,
                                            "turns": 1, "cost_usd": 1.2, "tokens_in": 0, "tokens_out": 0})
        self.j.append("turn.stopped", who, {})

    def emojis(self):
        return [r["reaction"][0]["emoji"] for r in self.fake.sent("setMessageReaction")]

    def test_a_limit_hit_on_his_message_is_one_status_line_not_an_answer(self):
        self.fake.updates = [self.msg("hello", 5)]
        self.bot.poll(timeout=0)
        self.ingest()  # m3
        self.j.append("turn.started", "hub", {"marker": "m3"})
        self.failed("hub")
        self.bot.tick()
        (line,) = self.fake.sent()
        self.assertTrue(line["text"].startswith("⚠️ hub: Claude account limit"), line["text"])
        self.assertIn("6am", line["text"])
        self.assertNotIn("You've hit", line["text"])
        self.assertEqual(line["reply_parameters"]["message_id"], 5)
        self.assertEqual(self.emojis(), ["👀"])                       # 👍 is only for an answer
        self.j.append("turn.started", "hub", {"marker": "m3"})       # once more for the same message — do not repeat
        self.failed("hub")
        self.bot.tick()
        self.assertEqual(len(self.fake.sent()), 1)

    API = "API Error: 529 Overloaded. This is a server-side issue, usually temporary — try again"

    def unasked(self, who, text=LIMIT, later=0):
        """A turn of no one's message fails (a schedule, another session); `later` seconds after now."""
        if who not in self.bot.reader.state["sessions"]:
            self.j.append("session.requested", who, {"cwd": "/w", "session_id": who})
            self.j.append("session.started", who, {"session_id": who})
        ts = time.time() + later
        self.j.append("turn.started", who, {"marker": None}, ts=ts)
        self.j.append("turn.result", who, {"text": text, "subtype": "success", "is_error": True, "turns": 1}, ts=ts)
        self.j.append("turn.stopped", who, {}, ts=ts)
        self.bot.tick()

    def edits(self):
        return [b["text"] for b in self.fake.sent("editMessageText")]

    def test_failures_of_turns_without_his_message_are_one_message_for_the_hub_edited_for_an_hour(self):
        self.unasked("hub")
        (line,) = self.fake.sent()
        self.assertEqual(line["text"], "⚠️ Turns that were not from your message (schedules, other sessions) failed: 1. "
                                       "Sessions: hub. Why: Claude account limit (reset 6am (Europe/Paris)).")
        self.assertNotIn("reply_parameters", line)
        self.assertNotIn("You've hit", line["text"])
        self.unasked("hub", self.API)                                # the same session, another reason: edited, not sent
        self.unasked("w1", self.API)
        self.unasked("w2", self.API)
        self.assertEqual(len(self.fake.sent()), 1)
        self.assertEqual(self.edits()[-1], "⚠️ Turns that were not from your message (schedules, other sessions) failed: 4. "
                                           "Sessions: hub, <code>w1</code>, <code>w2</code>. "
                                           "Why: Claude account limit (reset 6am (Europe/Paris)); Claude API is overloaded.")
        for n in ("w3", "w4", "w5", "w6", "w7"):
            self.unasked(n, self.API)
        self.assertEqual(len(self.fake.sent()), 1)                   # dozens of sessions — still one message
        self.assertIn("failed: 9. Sessions: hub, <code>w1</code>, <code>w2</code>, <code>w3</code>, <code>w4</code> +3.",
                      self.edits()[-1])
        self.unasked("w8", self.API, later=3700)                     # the hour is over: a new message, a new count
        self.assertEqual(len(self.fake.sent()), 2)
        self.assertIn("failed: 1. Sessions: <code>w8</code>.", self.fake.sent()[1]["text"])

    def test_a_failure_of_a_turn_on_his_own_page_message_is_not_reported_as_not_from_his_message(self):
        # his word from the page (no Telegram thread): the answer is on the page, as before - Telegram stays silent
        self.j.append("message.received", None, {"to": "hub", "text": "hello", "channel": "web", "owner": True})
        self.j.append("turn.started", "hub", {"marker": "m3"})
        self.failed("hub", self.API)
        self.bot.tick()
        self.assertEqual((self.fake.sent(), self.bot.st["unasked"]), ([], {}))

    def test_old_failures_after_the_bot_was_down_are_not_reported_as_news(self):
        self.unasked("hub", later=-2 * tg.LIVE_FOR)
        self.assertEqual((self.fake.sent(), self.bot.st["unasked"]), ([], {}))

    def test_with_no_chat_to_write_to_nothing_is_counted(self):
        self.bot.cfg = {**self.bot.cfg, "owners": []}
        self.unasked("hub", self.API)
        self.assertEqual(self.bot.st["unasked"], {})
        self.bot.cfg = {**self.bot.cfg, "owners": [OWNER]}
        self.unasked("w1", self.API)
        self.assertIn("failed: 1.", self.fake.sent()[0]["text"])      # the count starts when it is said

    def test_the_reasons_beyond_three_are_a_number(self):
        for i, text in enumerate((self.API, self.LIMIT, "Invalid API key · Please run /login", "boom: something else")):
            self.unasked(f"j{i}", text)
        self.assertEqual(len(self.fake.sent()), 1)
        self.assertTrue(self.edits()[-1].endswith(" +1."), self.edits()[-1])

    def test_other_failures_are_named(self):
        self.fake.updates = [self.msg("once more", 5)]
        self.bot.poll(timeout=0)
        self.ingest()
        mid = [f"m{e['seq']}" for e in journal.read(self.j.path) if e["type"] == "message.received"][-1]
        self.j.append("turn.started", "hub", {"marker": mid})
        self.failed("hub", "API Error: 529 Overloaded. This is a server-side issue, usually temporary — try again")
        self.bot.tick()
        (line,) = self.fake.sent()
        self.assertTrue(line["text"].startswith("⚠️ hub: Claude API is overloaded"), line["text"])

    def test_an_executors_failure_on_his_task_is_a_status_line_under_his_message(self):
        self.fake.updates = [self.msg("calculate the revenue", 5)]
        self.bot.poll(timeout=0)
        self.ingest()  # m3
        self.j.append("session.requested", "w", {"cwd": "/w", "session_id": "w"})
        self.j.append("session.started", "w", {"session_id": "w"})
        self.j.append("delegation.created", None, {"session": "w", "for": "m3", "channel": "session", "owner": False,
                                                   "sender": "hub"})  # d6, task m6
        self.j.append("turn.started", "w", {"marker": "m6"})
        self.failed("w", "You've hit your weekly limit · resets Oct 8 at 5pm (Europe/Paris)")
        self.bot.tick()
        (line,) = self.fake.sent()
        self.assertIn("w", line["text"])
        self.assertIn("Claude account limit", line["text"])
        self.assertEqual(line["reply_parameters"]["message_id"], 5)
        self.assertNotIn("👍", self.emojis())

    def test_a_failure_line_is_never_the_first_line_of_a_long_turn(self):
        self.clocked()
        self.j.append("turn.started", "hub", {"marker": "m3"})
        self.j.append("session.said", "hub", {"text": self.LIMIT})
        self.bot.tick()
        self.now += 46
        self.bot.tick()
        self.assertEqual(self.fake.sent(), [])

    def test_a_helper_agents_words_are_not_the_first_line_of_a_long_turn(self):
        """The text of a helper agent (Agent) arrives in the session stream marked parent — it is not the session's answer."""
        self.clocked()
        self.j.append("turn.started", "hub", {"marker": "m3"})
        self.j.append("session.said", "hub", {"text": "agent: looking at the files", "parent": "toolu_a"})
        self.bot.tick()
        self.now += 46
        self.bot.tick()
        self.assertEqual(self.fake.sent(), [])

    def pool_on(self):
        """Claude accounts are on: the core is on the main account, the hub is started on it."""
        self.j.append("account.switched", None, {"to": "main", "restart": True, "channel": "core"})
        self.j.append("session.exited", "hub", {})
        self.j.append("session.started", "hub", {"session_id": "h"})

    def test_with_the_account_pool_the_switch_continues_and_the_answer_comes_to_his_message(self):
        self.pool_on()
        self.fake.updates = [self.msg("hello", 5)]
        self.bot.poll(timeout=0)
        self.ingest()
        mid = [f"m{e['seq']}" for e in journal.read(self.j.path) if e["type"] == "message.received"][-1]
        self.j.append("turn.started", "hub", {"marker": mid})
        self.failed("hub")
        self.bot.tick()
        self.assertEqual(self.fake.sent(), [])                       # the core will switch and continue — send nothing
        self.j.append("account.switched", None, {"to": "work", "from": "main", "restart": True, "channel": "core",
                                                 "blocked": {"main": 9e9}})
        self.j.append("session.exited", "hub", {})
        ev = self.j.append("session.started", "hub", {"session_id": "h"})   # a continuation — m<seq>x5 from the core
        self.j.append("turn.started", "hub", {"marker": f"m{ev['seq']}x5"})
        self.j.append("turn.result", "hub", {"text": "Hello! Done"})
        self.bot.tick()
        (answer,) = self.fake.sent()
        self.assertEqual((answer["text"], answer["reply_parameters"]["message_id"]), ("Hello! Done", 5))
        self.assertEqual(self.emojis().count("👍"), 1)

    def test_with_the_account_pool_a_limit_on_a_turn_without_his_message_is_silent_until_the_core_switches(self):
        self.pool_on()
        self.j.append("turn.started", "hub", {"marker": None})
        self.failed("hub")
        self.bot.tick()
        self.assertEqual(self.fake.sent(), [])                       # the core switches the account and continues — nothing to say
        self.assertEqual(self.bot.st["unasked"], {})                 # and the quiet hour is not spent

    def test_after_a_restart_the_continuation_answers_in_his_thread_and_nothing_to_continue_stays_quiet(self):
        # a turn on his message was cut off by a hub restart (the wait ceiling) — the continuation answers him in the
        # same thread, as after a limit; we do not send him "nothing to continue" in reply to the core's "continue"
        for text, mid in (("hello", 5), ("more", 6)):
            self.fake.updates = [self.msg(text, mid)]
            self.bot.poll(timeout=0)
            self.ingest()
            m = [f"m{e['seq']}" for e in journal.read(self.j.path) if e["type"] == "message.received"][-1]
            self.j.append("turn.started", "hub", {"marker": m})
            self.j.append("session.exited", "hub", {"why": "reconcile"})
            ev = self.j.append("session.started", "hub", {"session_id": "h"})   # a continuation — m<seq>x3 from the core
            self.j.append("turn.started", "hub", {"marker": f"m{ev['seq']}x3"})
            self.j.append("turn.result", "hub", {"text": "Hello! Done" if mid == 5 else "Nothing to continue."})
            self.bot.tick()
        (answer,) = self.fake.sent()
        self.assertEqual((answer["text"], answer["reply_parameters"]["message_id"]), ("Hello! Done", 5))

    def test_with_every_account_busy_he_learns_when_it_continues(self):
        self.pool_on()
        self.fake.updates = [self.msg("hello", 5)]
        self.bot.poll(timeout=0)
        self.ingest()
        mid = [f"m{e['seq']}" for e in journal.read(self.j.path) if e["type"] == "message.received"][-1]
        self.j.append("turn.started", "hub", {"marker": mid})
        self.failed("hub")
        self.bot.tick()
        self.assertEqual(self.fake.sent(), [])
        self.j.append("account.exhausted", None, {"blocked": {"main": 9e9}, "until": 9e9, "why": "main: limit",
                                                  "channel": "core"})
        self.j.append("account.exhausted", None, {"blocked": {"main": 9e9}, "until": 9e9, "channel": "core"})
        self.bot.tick()
        (line,) = self.fake.sent()
        self.assertTrue(line["text"].startswith("⚠️ hub: Claude account limit"), line["text"])
        self.assertIn("will continue", line["text"])
        self.assertEqual(line["reply_parameters"]["message_id"], 5)

    def test_while_the_core_waits_for_the_reset_he_learns_when_it_continues(self):
        # PR #207 (hub accounts wait): the core waits for the active account's near reset — the line "will continue after
        # the reset", as with "all busy": both when the wait is recorded before the turn result (limits arrive earlier) and after
        self.pool_on()
        for mid, first in ((5, True), (6, False)):
            self.fake.calls.clear()
            self.fake.updates = [self.msg("hello", mid)]
            self.bot.poll(timeout=0)
            self.ingest()
            m = [f"m{e['seq']}" for e in journal.read(self.j.path) if e["type"] == "message.received"][-1]
            self.j.append("turn.started", "hub", {"marker": m})
            wait = {"account": "main", "until": 9e9 + mid, "channel": "core"}
            if first:
                self.j.append("account.waiting", None, wait)
            self.failed("hub")
            if not first:
                self.j.append("account.waiting", None, wait)
            self.bot.tick()
            (line,) = self.fake.sent()
            self.assertTrue(line["text"].startswith("⚠️ hub: "), line["text"])
            self.assertIn("waiting for this account to reset — will continue after the reset", line["text"])
            self.assertEqual(line["reply_parameters"]["message_id"], mid)
            self.j.append("account.switched", None, {"to": "main", "from": "main", "free": True, "reset": True,
                                                     "channel": "core"})

    # --- bot commands: it answers itself from the core's state, without a model — also when the hub is busy or down ---
    def command(self, text, mid=50):
        self.fake.updates = [self.msg(text, mid)]
        self.bot.poll(timeout=0)

    def test_status_answers_from_the_core_while_the_hub_is_busy(self):
        self.j.append("turn.started", "hub", {"marker": None, "prompt_head": "busy"})
        self.j.append("session.recap", "hub", {"text": "sorting the mail", "by": "watch"})
        self.j.append("session.requested", "w", {"cwd": "/w", "session_id": "w"})
        self.j.append("session.started", "w", {"session_id": "w"})
        self.j.append("turn.started", "w", {"marker": None, "prompt_head": "work"})
        self.j.append("session.recap", "w", {"text": "fixing the build", "by": "watch"})
        self.j.append("permission.requested", "w", {"request_id": "r1", "tool": "AskUserQuestion", "interactive": True,
                                                    "input": {"questions": [{"question": "Merge?", "options": [
                                                        {"label": "Yes"}]}]}})
        self.j.append("session.requested", "idle1", {"cwd": "/w", "session_id": "i1"})
        self.j.append("session.started", "idle1", {"session_id": "i1"})
        self.j.append("message.received", None, {"text": "and more", "channel": "web", "owner": True})   # the hub is busy — it waits
        self.bot.tick()
        self.fake.calls.clear()
        self.command("/status")
        self.assertEqual(self.spooled(), [])                     # nothing went to the hub
        (reply,) = self.fake.sent()
        text = reply["text"]
        self.assertEqual(reply["reply_parameters"]["message_id"], 50)
        self.assertIn("sorting the mail", text)
        self.assertIn("fixing the build", text)
        self.assertIn("Questions for you: 1", text)
        self.assertIn("queued: 1", text)
        self.assertNotIn("idle1", text)                          # free ones as a number, not a list
        self.assertIn("free 1", text)

    # --- /stop: the list of working executors as buttons, a press is the same stop as the page's button -----------
    def executor(self, name, turn=True):
        self.j.append("session.requested", name, {"cwd": "/w", "session_id": name, "executor": True, "raised_by": "hub"})
        self.j.append("session.started", name, {"session_id": name})
        if turn:
            self.j.append("turn.started", name, {"marker": None, "prompt_head": "work"})

    def press_stop(self, button, user=OWNER, uid=60):
        self.fake.updates = [{"update_id": uid, "callback_query": {"id": f"c{uid}", "from": {"id": user},
                                                                  "data": button["callback_data"]}}]
        self.bot.poll(timeout=0)

    def test_stop_with_nobody_working_says_so(self):
        self.executor("ex-idle", turn=False)
        self.bot.tick()
        self.fake.calls.clear()
        self.command("/stop", mid=60)
        self.assertEqual(self.spooled(), [])
        (reply,) = self.fake.sent()
        self.assertEqual(reply["text"], "No one is working right now.")
        self.assertEqual(reply["reply_parameters"]["message_id"], 60)
        self.assertNotIn("reply_markup", reply)

    def test_stop_lists_only_working_executors_as_buttons(self):
        self.executor("ex-a")
        self.executor("ex-b")
        self.executor("ex-idle", turn=False)
        self.executor("ex-gone")
        self.j.append("session.stop_requested", "ex-gone", {"channel": "cli", "owner": True})   # asleep: nothing to stop
        self.j.append("session.requested", "w", {"cwd": "/w", "session_id": "w"})
        self.j.append("session.started", "w", {"session_id": "w"})
        self.j.append("turn.started", "w", {"marker": None, "prompt_head": "not an executor"})
        self.j.append("turn.started", "hub", {"marker": None, "prompt_head": "busy"})
        self.bot.tick()
        self.fake.calls.clear()
        self.command("/stop", mid=61)
        self.assertEqual(self.spooled(), [])                     # the list is the bot's own answer: nothing goes to the hub
        (reply,) = self.fake.sent()
        rows = reply["reply_markup"]["inline_keyboard"]
        self.assertEqual([r[0]["text"] for r in rows], ["ex-a", "ex-b"])
        self.assertTrue(all(len(r) == 1 for r in rows))
        self.assertEqual(reply["reply_parameters"]["message_id"], 61)

    def test_a_press_is_the_owners_stop_fact_and_the_answer_names_the_executor(self):
        self.executor("ex-a")
        self.executor("ex-b")
        self.bot.tick()
        self.fake.calls.clear()
        self.command("/stop", mid=62)
        rows = self.fake.sent()[-1]["reply_markup"]["inline_keyboard"]
        self.fake.calls.clear()
        self.press_stop(rows[1][0])
        (ev,) = self.spooled()
        self.assertEqual((ev["type"], ev["entity"], ev["src"]), ("session.stop_requested", "ex-b", "telegram"))
        self.assertEqual((ev["data"]["channel"], ev["data"]["owner"]), ("telegram", True))
        self.assertEqual(len(self.fake.sent("answerCallbackQuery")), 1)
        self.assertEqual(self.fake.sent(), [])                   # "stopped" is said when the core has recorded it, not at the press
        self.j.append(ev["type"], ev["entity"], ev["data"], src=ev["src"])
        self.bot.tick()
        (done,) = self.fake.sent()
        self.assertEqual(done["text"], "🛑 Stopped <code>ex-b</code>.")
        self.assertEqual(done["reply_parameters"]["message_id"], 62)

    def test_a_stranger_or_another_chat_cannot_stop(self):
        self.executor("ex-a")
        self.bot.tick()
        self.command("/stop", mid=63)
        button = self.fake.sent()[-1]["reply_markup"]["inline_keyboard"][0][0]
        self.press_stop(button, user=1)
        self.assertEqual(self.spooled(), [])
        self.fake.updates = [self.msg("/stop", 64, user=1)]      # a stranger's command is not answered at all
        self.fake.calls.clear()
        self.bot.poll(timeout=0)
        self.assertEqual(self.fake.sent(), [])

    def test_a_press_on_an_executor_that_has_stopped_working_sends_nothing(self):
        self.executor("ex-a")
        self.bot.tick()
        self.command("/stop", mid=65)
        button = self.fake.sent()[-1]["reply_markup"]["inline_keyboard"][0][0]
        self.j.append("turn.result", "ex-a", {"text": "done", "turns": 1})
        self.j.append("turn.stopped", "ex-a", {})
        self.bot.tick()
        self.fake.calls.clear()
        self.press_stop(button)
        self.assertEqual(self.spooled(), [])
        (answer,) = self.fake.sent("answerCallbackQuery")
        self.assertEqual(answer["text"], "Not working anymore")

    def test_a_second_press_on_the_same_button_puts_no_second_stop(self):
        self.executor("ex-a")
        self.bot.tick()
        self.command("/stop", mid=67)
        button = self.fake.sent()[-1]["reply_markup"]["inline_keyboard"][0][0]
        self.press_stop(button, uid=70)
        self.assertEqual(len(self.spooled()), 1)
        self.fake.calls.clear()
        self.press_stop(button, uid=71)                          # the reader has not seen the first stop yet
        self.assertEqual(self.spooled(), [])
        self.assertEqual([a["text"] for a in self.fake.sent("answerCallbackQuery")], ["Stopping"])

    def test_an_old_list_cannot_stop_a_later_run_of_the_executor(self):
        self.executor("ex-a")
        self.bot.tick()
        now = time.time()
        self.bot.clock = lambda: now
        self.command("/stop", mid=68)
        button = self.fake.sent()[-1]["reply_markup"]["inline_keyboard"][0][0]
        self.bot.clock = lambda: now + tg.STOP_LIST_TTL + 1       # it finished, was woken for another task: busy again
        self.fake.calls.clear()
        self.press_stop(button)
        self.assertEqual(self.spooled(), [])
        self.assertEqual([a["text"] for a in self.fake.sent("answerCallbackQuery")], ["This list is out of date — send /stop again."])

    def test_a_stop_the_core_refuses_is_said_with_the_reason(self):
        self.executor("ex-a")
        self.bot.tick()
        self.command("/stop", mid=66)
        button = self.fake.sent()[-1]["reply_markup"]["inline_keyboard"][0][0]
        self.press_stop(button)
        (ev,) = self.spooled()
        self.j.append("session.stop_requested", "ex-a", {"channel": "cli", "owner": True})   # stopped from elsewhere first
        self.bot.tick()
        self.fake.calls.clear()
        self.j.append(ev["type"], ev["entity"], ev["data"], src=ev["src"])   # the core rejects the second one: it sleeps
        self.bot.tick()
        (line,) = self.fake.sent()
        self.assertEqual(line["text"], "Not stopped <code>ex-a</code>: the session is already asleep — nothing to stop; to "
                                       "take it off the list — hub session remove ex-a.")
        self.assertNotIn("🛑", line["text"])

    def test_away_and_back_are_core_facts_like_the_cli(self):
        self.command("/away 2", mid=51)
        (ev,) = self.spooled()
        self.assertEqual((ev["type"], ev["data"]["hours"], ev["data"]["channel"], ev["data"]["owner"]),
                         ("owner.away", 2.0, "telegram", True))
        self.j.append(ev["type"], ev.get("entity"), ev["data"], src=ev["src"])
        self.bot.tick()
        (ok,) = self.fake.sent()
        self.assertTrue(ok["text"].startswith("🌙 Noted: you are away until "), ok["text"])
        self.assertEqual(ok["reply_parameters"]["message_id"], 51)
        self.fake.calls.clear()
        self.command("/away", mid=50)   # again while away: the core keeps the start of the absence, the bot still confirms
        (ev,) = self.spooled()
        self.j.append(ev["type"], ev.get("entity"), ev["data"], src=ev["src"])
        self.bot.tick()
        (ok,) = self.fake.sent()
        self.assertTrue(ok["text"].startswith("🌙 Noted: you are away until /back"), ok["text"])
        self.assertEqual(ok["reply_parameters"]["message_id"], 50)
        self.fake.calls.clear()
        self.command("/back", mid=52)
        (ev,) = self.spooled()
        self.assertEqual(ev["type"], "owner.back")
        self.j.append(ev["type"], ev.get("entity"), ev["data"], src=ev["src"])
        self.bot.tick()
        self.assertTrue(self.fake.sent()[-1]["text"].startswith("Welcome back"))   # the summary is the answer itself
        self.fake.calls.clear()
        self.command("/back", mid=53)                            # already back — the core has nothing to write
        self.assertEqual(self.spooled(), [])
        (reply,) = self.fake.sent()
        self.assertIn("already here", reply["text"])

    def test_away_with_the_deputies_names_them_in_the_confirmation(self):
        # spec d2679763 A5: /away deputies [<person> …] — the questions of the deputies' areas go to them
        self.j.append("contact.deputy_set", None, {"person": {"id": "c0000a0b1", "name": "Anna Lee"}, "names": ["Anna"],
                                                   "zone": "map tiles", "path": {"kind": "via", "session": "hub",
                                                                                 "address": {"teams_chat": "19:a"}},
                                                   "channel": "cli", "owner": True})
        self.command("/away 2 deputies Anna", mid=51)
        (ev,) = self.spooled()
        self.assertEqual((ev["data"]["hours"], ev["data"]["deputies"]), (2.0, ["Anna"]))
        self.j.append(ev["type"], ev.get("entity"), ev["data"], src=ev["src"])
        self.bot.tick()
        (ok,) = self.fake.sent()
        self.assertIn("Questions in the deputies' areas go to them: Anna Lee («map tiles»)", ok["text"])
        self.fake.calls.clear()
        self.command("/away deputies", mid=52)
        (ev,) = self.spooled()
        self.assertIs(ev["data"]["deputies"], True)
        self.command("/away soon deputies", mid=53)   # a word that is not hours: the usage, nothing put
        self.assertEqual(self.spooled(), [])

    def test_a_superseded_away_is_not_confirmed_late(self):
        # the confirmation failed to send; meanwhile he came back and went away again from the page (also with no end):
        # the retry must not confirm the old /away as if it held now
        self.command("/away", mid=51)
        (ev,) = self.spooled()
        self.fake.fail["sendMessage"] = [502]
        self.j.append(ev["type"], ev.get("entity"), ev["data"], src=ev["src"])
        self.bot.tick()
        self.assertEqual(self.fake.sent(), [])
        self.j.append("owner.back", None, {"channel": "web", "owner": True})
        self.j.append("owner.away", None, {"hours": None, "channel": "web", "owner": True})
        self.bot.tick()   # still holding after the failure: read, the queue waits
        self.bot.hold_until = 0
        self.bot.tick()
        self.assertEqual([s for s in self.fake.sent() if s["text"].startswith("🌙")], [])

    # --- the summary on return is off (telegram.back_summary, PR #297) --------------------------------------------
    def back(self, channel, on):
        self.bot.hub_config = lambda: {} if on else {"telegram": {"back_summary": False}}
        self.j.append("owner.away", None, {"channel": "cli", "owner": True})
        self.bot.tick()
        self.fake.calls.clear()
        self.j.append("owner.back", None, {"channel": channel, "owner": True})
        self.bot.tick()
        return [s["text"] for s in self.fake.sent()]

    def test_the_back_summary_comes_by_default(self):
        (text,) = self.back("web", on=True)
        self.assertTrue(text.startswith("Welcome back. While you were away:"), text)

    def test_no_back_summary_when_the_option_is_off(self):
        self.assertEqual(self.back("web", on=False), [])

    def test_back_from_telegram_is_still_acknowledged_without_the_summary(self):
        self.assertEqual(self.back("telegram", on=False), ["Welcome back."])

    def test_no_back_summary_when_he_writes_while_away_and_the_option_is_off(self):
        # wrote from the page in the "away" mode — the core marks the return with the same message (a summary with its
        # seq): when off, no summary here either
        for on in (True, False):
            self.bot.hub_config = lambda: {} if on else {"telegram": {"back_summary": False}}
            self.j.append("owner.away", None, {"channel": "cli", "owner": True})
            self.bot.tick()
            self.fake.calls.clear()
            self.j.append("message.received", None, {"text": "hello", "channel": "web", "owner": True})
            self.bot.tick()
            back = [s["text"] for s in self.fake.sent() if s["text"].startswith("Welcome back")]
            self.assertEqual(len(back), 1 if on else 0, (on, back))
        # from Telegram — one line, no summary
        self.j.append("owner.away", None, {"channel": "cli", "owner": True})
        self.bot.tick()
        self.fake.calls.clear()
        self.j.append("message.received", None, {"text": "hello", "channel": "telegram", "owner": True})
        self.bot.tick()
        self.assertEqual([s["text"] for s in self.fake.sent() if s["text"].startswith("Welcome back")], ["Welcome back."])

    def test_without_the_summary_away_does_not_promise_it_and_its_end_is_still_told(self):
        # off: /away does not promise a summary; the "away" term ran out by itself — one line (otherwise the cards held
        # back during the absence would flow into the chat without a word that the mode is off); /back — as a reply to the command
        self.bot.hub_config = lambda: {"telegram": {"back_summary": False}}
        self.command("/away 2", mid=51)
        (ev,) = self.spooled()
        self.j.append(ev["type"], ev.get("entity"), ev["data"], src=ev["src"])
        self.bot.tick()
        (ok,) = self.fake.sent()
        self.assertTrue(ok["text"].endswith("when you are back — /back."), ok["text"])
        self.fake.calls.clear()
        self.j.append("timer.fired", "owner", {})
        self.bot.tick()
        self.assertEqual([s["text"] for s in self.fake.sent()], ["Welcome back."])
        self.j.append("owner.away", None, {"channel": "cli", "owner": True})
        self.bot.tick()
        self.fake.calls.clear()
        self.command("/back", mid=52)
        (ev,) = self.spooled()
        self.j.append(ev["type"], ev.get("entity"), ev["data"], src=ev["src"])
        self.bot.tick()
        (ok,) = self.fake.sent()
        self.assertEqual((ok["text"], ok["reply_parameters"]["message_id"]), ("Welcome back.", 52))

    def test_bad_hours_and_unknown_commands(self):
        self.command("/away tomorrow")
        self.assertEqual(self.spooled(), [])
        self.assertIn("/away", self.fake.sent()[-1]["text"])
        self.command("/status@test_bot", mid=54)
        self.assertEqual(self.spooled(), [])
        self.command("/deploy prod", mid=55)                     # not a bot command — to the hub, as before
        (ev,) = self.spooled()
        self.assertEqual((ev["type"], ev["data"]["text"]), ("message.received", "/deploy prod"))

    def test_commands_are_registered_in_telegram(self):
        self.bot.register_commands()
        (cmds,) = self.fake.sent("setMyCommands")
        self.assertEqual([c["command"] for c in cmds["commands"]], ["status", "away", "back", "stop", "spec", "page",
                                                                         "link"])
        self.assertTrue(all(c["description"] for c in cmds["commands"]))   # /stop's description is from the catalog

    def test_monitor_notify_goes_to_owner(self):
        self.j.append("owner.notify", None, {"text": "The gate is **closed** for 7 h", "title": "Shop", "urgent": True,
                                             "source": "gate", "channel": "cli", "owner": True})
        self.bot.tick()
        self.assertEqual(self.fake.sent()[-1]["text"], "⛔ <b>Shop</b>\nThe gate is <b>closed</b> for 7 h")

    def test_a_consent_to_new_plugin_rights_is_told_to_the_owner(self):
        # the consent on the page could also be clicked by the page plugin's code (full access): the owner sees it in Telegram
        self.j.append("plugins.consented", None, {"key": "alice:owl", "sha": "a" * 40, "via": "web",
                                                  "wider": ["a process on the machine with your rights: sh -c <x>"],
                                                  "channel": "web", "owner": True})
        self.bot.tick()
        text = self.fake.sent()[-1]["text"]
        self.assertIn("alice:owl", text)
        self.assertIn("sh -c &lt;x&gt;", text)
        self.assertIn("web", text)

    def test_notify_with_a_file_sends_it(self):
        # 02.10: the videos a session posted did not open — the video now arrives right in the chat
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "demo.mp4")
        with open(path, "wb") as f:
            f.write(b"\x00\x00\x00\x18ftypmp42" + b"x" * 1000)
        self.j.append("owner.notify", None, {"text": "Demo is ready", "title": "Video", "file": path,
                                             "channel": "cli", "owner": True})
        self.bot.tick()
        (method, body), = [(m, b) for m, b in self.fake.calls if m in ("sendVideo", "sendDocument")]
        self.assertEqual(method, "sendVideo")
        self.assertTrue(body["multipart"])
        self.assertIn(b'name="video"; filename="demo.mp4"', body["raw"])
        self.assertIn(b'name="chat_id"', body["raw"])
        # the session sends a file itself: hub send-file
        self.fake.calls.clear()
        self.j.append("owner.file", None, {"path": path, "caption": "demo", "channel": "session", "owner": False,
                                           "sender": "demo"})
        self.bot.tick()
        (method, body), = [(m, b) for m, b in self.fake.calls if m in ("sendVideo", "sendDocument")]
        self.assertIn("demo: demo".encode(), body["raw"])

    def test_a_screenshot_comes_as_a_photo_and_as_a_document_if_telegram_refuses_it(self):
        # 06.10: check that screenshots can be forwarded — the picture went as a document, a file, not a photo in the chat
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "shot.png")
        with open(path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"x" * 100)
        self.j.append("owner.file", None, {"path": path, "caption": "looks like this", "channel": "session",
                                           "owner": False, "sender": "maps"})
        self.bot.tick()
        sent = [(m, b) for m, b in self.fake.calls if m in ("sendPhoto", "sendDocument")]
        self.assertEqual([m for m, _ in sent], ["sendPhoto"])
        self.assertIn(b'name="photo"; filename="shot.png"', sent[0][1]["raw"])
        self.assertIn("maps: looks like this".encode(), sent[0][1]["raw"])
        self.fake.calls.clear()
        self.fake.fail["sendPhoto"] = [400]    # a too large or odd picture — Telegram does not accept it as a photo
        self.j.append("owner.file", None, {"path": path, "caption": "more", "channel": "session", "owner": False,
                                           "sender": "maps"})
        self.bot.tick()
        self.assertEqual([m for m, _ in self.fake.calls if m in ("sendPhoto", "sendDocument")], ["sendDocument"])

    def test_several_files_of_one_command_all_reach_the_chat(self):
        # 2026-10-30: hub send-file a b c — three owner.file events in a row, one per file, none merged or skipped
        import tempfile
        d = tempfile.mkdtemp()
        paths = []
        for n in ("a.png", "b.png", "c.png"):
            paths.append(os.path.join(d, n))
            with open(paths[-1], "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n" + n.encode() * 20)
        for i, p in enumerate(paths):
            self.j.append("owner.file", None, {"path": p, "caption": "result" if i == 2 else None, "channel": "session",
                                               "owner": False, "sender": "maps"})
        self.bot.tick()
        sent = [b for m, b in self.fake.calls if m == "sendPhoto"]
        self.assertEqual(len(sent), 3)
        for b, n in zip(sent, ("a.png", "b.png", "c.png")):
            self.assertIn(f'filename="{n}"'.encode(), b["raw"])
        self.assertIn("maps: result".encode(), sent[2]["raw"])

    def test_voice_is_transcribed_when_configured(self):
        """The "Voice" module (owner decision 2026-09-30): a voice message -> 👀, the transcript -> a message "🎤 voice: …",
        🤔; when not configured — a request to write text, as before."""
        self.fake.updates = [self.msg(None, 5, voice={"file_id": "v1", "duration": 3})]
        self.bot.hub_config = lambda: {}
        self.bot.poll(timeout=0)
        self.assertIn("Voice is off", self.fake.sent()[-1]["text"])
        self.assertEqual(self.spooled(), [])

        d = tempfile.mkdtemp(prefix="tg-voice-")
        ffmpeg = exe.script(os.path.join(d, "ffmpeg"), "import sys, shutil\na = sys.argv\nshutil.copy(a[a.index('-i') + 1], a[-1])\n")
        asr = exe.script(os.path.join(d, "asr"),
                         "import sys, json\nprint(json.dumps({'text': 'hello ' + open(sys.argv[1]).read()}))\n")
        self.bot.hub_config = lambda: {"voice": {"cmd": f"{asr} {{wav}}", "ffmpeg": ffmpeg}}
        self.bot.voice_sync = True
        self.fake.updates = [self.msg(None, 6, voice={"file_id": "v2", "duration": 3})]
        self.bot.poll(timeout=0)
        self.bot.drain_voice()
        (ev,) = self.spooled()
        self.assertEqual((ev["type"], ev["data"]["text"]), ("message.received", VOICE + "hello JPEGDATA"))
        emojis = [c["reaction"][0]["emoji"] for c in self.fake.sent("setMessageReaction") if c["message_id"] == 6]
        self.assertEqual(emojis, ["👀", "🤔"])

    def test_a_voice_survives_a_bot_restart_during_transcription(self):
        # the bot was restarted (hub update, hub restart) while a voice message was being transcribed: the update offset
        # has already moved — Telegram will not return it, and the journal has nothing; the owner saw only 👀 forever
        d = tempfile.mkdtemp(prefix="tg-voice-")
        ffmpeg = exe.script(os.path.join(d, "ffmpeg"), "import sys, shutil\na = sys.argv\nshutil.copy(a[a.index('-i') + 1], a[-1])\n")
        asr = exe.script(os.path.join(d, "asr"),
                         "import sys, json\nprint(json.dumps({'text': 'hello ' + open(sys.argv[1]).read()}))\n")
        cfg ={"voice": {"cmd": f"{asr} {{wav}}", "ffmpeg": ffmpeg}}
        hang = threading.Event()
        self.addCleanup(hang.set)
        self.bot.hub_config = lambda: cfg
        self.bot.transcribe = lambda media, cfg: hang.wait() and (None, "stopped", False)   # the process dies midway
        self.fake.updates = [self.msg(None, 6, voice={"file_id": "v2", "duration": 3})]
        self.bot.poll(timeout=0)
        self.bot.drain_voice()
        self.assertEqual(self.spooled(), [])
        again = tg.Bot(self.state, tg.Api("1:x", base=self.fake.base))   # a new process — like serve()
        again.hub_config, again.voice_sync = (lambda: cfg), True
        self.assertEqual(again.st["offset"], 7)
        again.catch_up()
        again.poll(timeout=0)
        again.drain_voice()
        self.ingest()
        got = [e["data"]["text"] for e in journal.read(self.j.path) if e["type"] == "message.received"]
        self.assertEqual(got, [VOICE + "hello JPEGDATA"])
        third = tg.Bot(self.state, tg.Api("1:x", base=self.fake.base))   # it went through — the next startup does not repeat it
        third.hub_config, third.voice_sync = (lambda: cfg), True
        third.catch_up()
        third.drain_voice()
        self.assertEqual(self.spooled(), [])

    def test_a_transcribed_voice_is_not_lost_when_the_bot_fails_on_it(self):
        # an exception while parsing already transcribed text: from drain_voice it crashed the bot loop and the text was lost
        def boom(m):
            raise ValueError("bad")
        self.bot.transcribe = lambda media, cfg: ("buy bread", None, False)
        self.bot.hub_config = lambda: {"voice": {"cmd": "asr {wav}"}}
        self.bot.voice_sync = True
        self.fake.updates = [self.msg(None, 9, voice={"file_id": "v9", "duration": 3})]
        self.bot.poll(timeout=0)
        self.bot.attachments = boom
        self.bot.drain_voice()
        (ev,) = self.spooled()
        self.assertTrue(ev["data"]["text"].startswith(VOICE + "buy bread"), ev["data"]["text"])

    def net_down(self, times):
        """getFile fails with a DNS error (socket.gaierror, as on 16.11: the VPN dropped name resolution) `times` times — then passes."""
        import socket
        left = [times]
        real = self.bot.api.keep_alive

        def flaky(verb, url, *a, **kw):
            if "/getFile" in url and left[0] > 0:
                left[0] -= 1
                raise socket.gaierror(8, "nodename nor servname provided, or not known")
            return real(verb, url, *a, **kw)
        self.bot.api.keep_alive = flaky
        return left

    def voice_cfg(self):
        d = tempfile.mkdtemp(prefix="tg-voice-")
        ffmpeg = exe.script(os.path.join(d, "ffmpeg"), "import sys, shutil\na = sys.argv\nshutil.copy(a[a.index('-i') + 1], a[-1])\n")
        asr = exe.script(os.path.join(d, "asr"),
                         "import sys, json\nprint(json.dumps({'text': 'hello ' + open(sys.argv[1]).read()}))\n")
        return {"voice": {"cmd": f"{asr} {{wav}}", "ffmpeg": ffmpeg}}

    def test_a_voice_waits_out_a_network_blink_instead_of_failing(self):
        """16.11: the VPN dropped DNS for a minute — four voice messages in a row got "could not make out: getFile: [Errno 8]
        nodename nor servname…" and were lost (the transcription queue was cleared). A network failure while downloading keeps
        the voice in the queue and retries it on the clock; the owner hears once that it waits for the network, then gets the text."""
        cfg = self.voice_cfg()
        self.bot.hub_config = lambda: cfg
        self.bot.voice_sync = True
        now = [1000.0]
        self.bot.clock = lambda: now[0]
        self.net_down(3)
        self.fake.updates = [self.msg(None, 6, voice={"file_id": "v2", "duration": 3})]
        self.bot.poll(timeout=0)
        replies = lambda: [c["text"] for c in self.fake.sent() if (c.get("reply_parameters") or {}).get("message_id") == 6]  # noqa: E731
        for _ in range(3):
            self.bot.drain_voice()
            self.assertEqual(self.spooled(), [])
            self.bot.drain_voice()   # not due yet — no attempt, nothing said again
            now[0] += 120
        self.bot.drain_voice()       # due: this attempt passes
        self.bot.drain_voice()
        (ev,) = self.spooled()
        self.assertEqual(ev["data"]["text"], VOICE + "hello JPEGDATA")
        self.assertEqual(ev["data"]["tg"], {"chat": OWNER, "id": 6})
        self.assertEqual(replies(), [i18n.t("telegram.voice_net_wait")])   # one line, no "could not make out"
        self.assertEqual(self.bot.st["voice_wait"], {})
        self.assertEqual(self.bot.st["transcribe"], {})

    def test_a_voice_waiting_for_the_network_survives_a_bot_restart(self):
        cfg = self.voice_cfg()
        self.bot.hub_config = lambda: cfg
        self.bot.voice_sync = True
        now = [1000.0]
        self.bot.clock = lambda: now[0]
        self.net_down(1)
        self.fake.updates = [self.msg(None, 6, voice={"file_id": "v2", "duration": 3})]
        self.bot.poll(timeout=0)
        self.bot.drain_voice()
        self.assertEqual(self.spooled(), [])
        again = tg.Bot(self.state, tg.Api("1:x", base=self.fake.base), clock=lambda: now[0] + 600)   # a new process — like serve()
        again.hub_config, again.voice_sync = (lambda: cfg), True
        again.catch_up()
        again.drain_voice()
        again.drain_voice()
        (ev,) = self.spooled()
        self.assertEqual(ev["data"]["text"], VOICE + "hello JPEGDATA")
        self.assertEqual(again.st["voice_wait"], {})

    def test_a_voice_without_network_for_a_quarter_of_an_hour_asks_to_send_it_again(self):
        cfg = self.voice_cfg()
        self.bot.hub_config = lambda: cfg
        self.bot.voice_sync = True
        now = [1000.0]
        self.bot.clock = lambda: now[0]
        self.net_down(10 ** 6)
        self.fake.updates = [self.msg(None, 6, voice={"file_id": "v2", "duration": 3})]
        self.bot.poll(timeout=0)
        for _ in range(60):          # 30 min of the bot loop
            self.bot.drain_voice()
            now[0] += 30
        replies = [c["text"] for c in self.fake.sent() if (c.get("reply_parameters") or {}).get("message_id") == 6]
        self.assertEqual(len(replies), 2, replies)
        self.assertEqual(replies[0], i18n.t("telegram.voice_net_wait"))
        self.assertIn("nodename", replies[1])
        self.assertTrue(replies[1].startswith(i18n.t("telegram.voice_net_failed", why="", count=15).split("(")[0][:20]), replies[1])
        self.assertEqual(self.spooled(), [])
        self.assertEqual(self.bot.st["voice_wait"], {})

    def test_a_voice_the_telegram_refuses_fails_at_once(self):
        """Not the network: "file is too big" will not pass on a retry — as before, at once."""
        cfg = self.voice_cfg()
        self.bot.hub_config = lambda: cfg
        self.bot.voice_sync = True
        self.fake.fail["getFile"] = [400]
        self.fake.updates = [self.msg(None, 6, voice={"file_id": "v2", "duration": 3})]
        self.bot.poll(timeout=0)
        self.bot.drain_voice()
        replies = [c["text"] for c in self.fake.sent() if (c.get("reply_parameters") or {}).get("message_id") == 6]
        self.assertEqual(len(replies), 1)
        self.assertTrue(replies[0].startswith("Could not make out the voice message: getFile"), replies[0])
        self.assertEqual(self.bot.st["voice_wait"], {})

    def voice_waiting(self, times=10 ** 6):
        """A voice message whose download failed on the network and now waits in voice_wait."""
        cfg = self.voice_cfg()
        self.bot.hub_config = lambda: cfg
        self.bot.voice_sync = True
        now = [1000.0]
        self.bot.clock = lambda: now[0]
        left = self.net_down(times)
        self.fake.updates = [self.msg(None, 6, voice={"file_id": "v2", "duration": 3})]
        self.bot.poll(timeout=0)
        self.bot.drain_voice()
        self.assertIn("6", " ".join(self.bot.st["voice_wait"]))
        return now, left

    def replies_to(self, mid):
        return [c["text"] for c in self.fake.sent() if (c.get("reply_parameters") or {}).get("message_id") == mid]

    def reactions(self):
        return [r["emoji"] for c in self.fake.sent("setMessageReaction") for r in c["reaction"]]

    def test_a_voice_whose_file_request_blinks_waits_for_the_network_too(self):
        """getFile passed, but the file itself (GET /file/bot…) got a 502 or a dropped connection: that is the network too,
        the voice waits and is transcribed later instead of "could not make out"."""
        cfg = self.voice_cfg()
        self.bot.hub_config = lambda: cfg
        self.bot.voice_sync = True
        now = [1000.0]
        self.bot.clock = lambda: now[0]
        self.fake.fail["file"] = [502, "drop", "drop", 503]
        self.fake.updates = [self.msg(None, 6, voice={"file_id": "v2", "duration": 3})]
        self.bot.poll(timeout=0)
        for _ in range(20):
            self.bot.drain_voice()
            got = self.spooled()
            if got:
                break
            now[0] += 120
        (ev,) = got
        self.assertEqual(ev["data"]["text"], VOICE + "hello JPEGDATA")
        self.assertEqual(self.replies_to(6), [i18n.t("telegram.voice_net_wait")])
        self.assertNotIn("👎", self.reactions())
        self.assertEqual(self.bot.st["voice_wait"], {})

    def test_a_voice_whose_file_is_refused_fails_at_once(self):
        """A 404 on the file is not the network: as before, at once."""
        cfg = self.voice_cfg()
        self.bot.hub_config = lambda: cfg
        self.bot.voice_sync = True
        self.fake.fail["file"] = [404]
        self.fake.updates = [self.msg(None, 6, voice={"file_id": "v2", "duration": 3})]
        self.bot.poll(timeout=0)
        self.bot.drain_voice()
        (line,) = self.replies_to(6)
        self.assertTrue(line.startswith("Could not make out the voice message: getFile: HTTP 404"), line)
        self.assertEqual(self.bot.st["voice_wait"], {})

    def test_a_voice_whose_file_answer_is_cut_waits_for_the_network_too(self):
        """getFile's answer broke off in the middle (http.client.IncompleteRead, an HTTPException, not an OSError): that is
        the network too, the voice waits and is transcribed later instead of "could not make out"."""
        cfg = self.voice_cfg()
        self.bot.hub_config = lambda: cfg
        self.bot.voice_sync = True
        now = [1000.0]
        self.bot.clock = lambda: now[0]
        self.fake.fail["getFile"] = ["cut", "cut"]
        self.fake.updates = [self.msg(None, 6, voice={"file_id": "v2", "duration": 3})]
        self.bot.poll(timeout=0)
        for _ in range(20):
            self.bot.drain_voice()
            got = self.spooled()
            if got:
                break
            now[0] += 120
        (ev,) = got
        self.assertEqual(ev["data"]["text"], VOICE + "hello JPEGDATA")
        self.assertEqual(self.replies_to(6), [i18n.t("telegram.voice_net_wait")])
        self.assertNotIn("👎", self.reactions())
        self.assertEqual(self.bot.st["voice_wait"], {})

    def test_a_retry_longer_than_the_last_pause_sends_the_text_once(self):
        """A retry in the background took longer than VOICE_NET_PAUSES[-1] (the cloud transcriber backs off 10+30+90+270 s):
        when it ended, the next drain_voice saw the entry out of `voicing` with `next` in the past and started a second
        download and transcription before it read the first result — the text went to the session twice."""
        now, left = self.voice_waiting()
        left[0] = 0   # the network is back
        started = []

        class Later:   # the background thread of start_voice, run by the test when it wants
            def __init__(self, target, daemon=None):
                started.append(target)

            def start(self):
                pass
        real = self.bot.transcribe

        def slow(media, cfg):
            now[0] += tg.VOICE_NET_PAUSES[-1] + 60
            return real(media, cfg)
        self.bot.transcribe = slow
        self.bot.voice_sync = False
        only_bot = type(threading)("threading")   # the fake server's own threads stay real
        only_bot.__dict__.update(vars(threading), Thread=Later)
        with mock.patch.object(tg, "threading", only_bot):
            now[0] += 600
            self.bot.drain_voice()
            self.assertEqual(len(started), 1)
            started.pop()()          # the attempt runs longer than the last pause and ends
            self.bot.drain_voice()
            self.bot.drain_voice()
        self.assertEqual(started, [])   # no second download
        (ev,) = self.spooled()
        self.assertEqual(ev["data"]["text"], VOICE + "hello JPEGDATA")
        self.assertEqual(self.bot.st["voice_wait"], {})

    def test_a_waiting_voice_is_dropped_when_voice_is_turned_off(self):
        """Voice was turned off while the voice message waited for the network: no more downloads, the owner hears that
        voice is off (as for a new voice message), the entry is gone."""
        now, left = self.voice_waiting()
        tries = left[0]
        self.bot.hub_config = lambda: {}
        now[0] += 600
        self.bot.drain_voice()
        self.bot.drain_voice()
        self.assertEqual(left[0], tries)   # no download
        self.assertEqual(self.bot.st["voice_wait"], {})
        self.assertEqual(self.replies_to(6)[-1], i18n.t("telegram.voice_off"))
        self.assertEqual(self.spooled(), [])

    def test_no_thumbs_down_when_the_give_up_line_did_not_go(self):
        """Past the quarter of an hour the "send it again" line could not be sent: no 👎 alone without words; both go
        together once sending works."""
        now, _ = self.voice_waiting()
        self.fake.fail["sendMessage"] = [502] * 10 ** 4
        for _ in range(40):
            self.bot.drain_voice()
            now[0] += 30
        self.assertNotIn("👎", self.reactions())
        self.assertTrue(self.bot.st["voice_wait"])
        self.fake.fail["sendMessage"] = []
        for _ in range(20):
            self.bot.drain_voice()
            now[0] += 30
        self.assertEqual(self.reactions().count("👎"), 1)
        self.assertEqual(len(self.replies_to(6)), 2, self.replies_to(6))
        self.assertEqual(self.bot.st["voice_wait"], {})

    def test_transcribe_always_returns_text_why_and_net(self):
        cfg = self.voice_cfg()
        media = {"file_id": "v2", "duration": 3}
        self.assertEqual(self.bot.transcribe(media, cfg), ("hello JPEGDATA", None, False))
        self.fake.fail["getFile"] = [400]
        text, why, net = self.bot.transcribe(media, cfg)
        self.assertEqual((text, net), (None, False))
        self.assertIn("getFile", why)
        self.assertEqual(self.bot.transcribe({"file_id": "x", "mime_type": "application/zip"}, cfg)[0::2], (None, False))   # a type it does not accept

    def test_the_give_up_line_counts_minutes_in_the_language(self):
        key = "telegram.voice_net_failed"
        self.assertNotEqual(i18n.tr("en", key, why="x", count=1), i18n.tr("en", key, why="x", count=2))
        self.assertNotEqual(i18n.tr("ru", key, why="x", count=2), i18n.tr("ru", key, why="x", count=5))
        self.assertIn(" 15 ", i18n.tr("en", key, why="x", count=15))

    def test_an_unexpected_transcription_failure_still_answers(self):
        # a transcription failure outside known errors (a malformed recognition reply) — "could not make it out", not 👀 forever
        def boom(media, cfg):
            raise ValueError("bad json")
        self.bot.transcribe = boom
        self.bot.hub_config = lambda: {"voice": {"cmd": "asr {wav}"}}
        self.bot.voice_sync = True
        self.fake.updates = [self.msg(None, 9, voice={"file_id": "v9", "duration": 3})]
        self.bot.poll(timeout=0)
        self.bot.drain_voice()
        self.assertIn("Could not make out the voice message: ValueError", self.fake.sent()[-1]["text"])
        self.assertEqual(self.bot.voicing, {})

    def test_a_slow_transcription_tells_the_owner_once(self):
        """Transcription took minutes (retries, a slow model) — the owner saw only 👀 and thought voice was broken. Past
        VOICE_SLOW_AFTER — one line "still transcribing", without repeats; the text then arrives as usual."""
        d = tempfile.mkdtemp(prefix="tg-voice-")
        go = os.path.join(d, "go")
        ffmpeg = exe.script(os.path.join(d, "ffmpeg"), "import sys, shutil\na = sys.argv\nshutil.copy(a[a.index('-i') + 1], a[-1])\n")
        asr = exe.script(os.path.join(d, "asr"), f"import os, time, json\nwhile not os.path.exists({go!r}): time.sleep(0.05)\n"
                         "print(json.dumps({'text': 'slow'}))\n")
        self.bot.hub_config = lambda: {"voice": {"cmd": f"{asr} {{wav}}", "ffmpeg": ffmpeg}}
        now = [1000.0]
        self.bot.clock = lambda: now[0]
        self.fake.updates = [self.msg(None, 8, voice={"file_id": "v8", "duration": 3})]
        self.bot.poll(timeout=0)
        replies = lambda: [c for c in self.fake.sent() if (c.get("reply_parameters") or {}).get("message_id") == 8]  # noqa: E731
        try:
            self.bot.drain_voice()
            self.assertEqual(replies(), [])                 # a minute has not passed — we stay silent
            now[0] += tg.VOICE_SLOW_AFTER + 1
            self.bot.drain_voice()
            self.bot.drain_voice()
            (note,) = replies()
            self.assertIn("🎤", note["text"])
        finally:
            open(go, "w").close()
        for _ in range(200):
            if not self.bot.voicing and not self.bot.voice_done.empty():
                break
            time.sleep(0.05)
        self.bot.drain_voice()
        self.assertEqual(self.spooled()[-1]["data"]["text"], VOICE + "slow")
        self.assertEqual(len(replies()), 1)

    def test_text_paths_become_links(self):
        """30.09: a path to a plan in a session's question — "open in the browser"; we do not touch a/code/pre."""
        cwd = tempfile.mkdtemp(prefix="tg-plan-")
        os.makedirs(os.path.join(cwd, ".harness", "plans", "active"))
        plan = os.path.join(cwd, ".harness", "plans", "active", "2026-09-30-ab-252-screens.md")
        open(plan, "w").close()
        base = "http://100.9.9.9:8788"
        out = fmt.link_text("Plan is ready: .harness/plans/active/2026-09-30-ab-252-screens.md, 7 tasks. "
                            "Missing: docs/nope.md. <code>a/b.md</code>", base, cwd)
        self.assertIn(f'<a href="{base}/open?path={quote(plan)}">.harness/plans/active/2026-09-30-ab-252-screens.md</a>,',
                      out)
        self.assertIn("Missing: docs/nope.md.", out)
        self.assertIn("<code>a/b.md</code>", out)
        self.assertEqual(fmt.link_text("x a/b.md", None, cwd), "x a/b.md")

    @unittest.skipIf(os.name == "nt", "links to a path with spaces in the text — POSIX paths only (/…); paths with a drive letter in the text are not searched for")
    def test_a_path_with_spaces_in_folder_names_is_one_link(self):
        root = tempfile.mkdtemp(prefix="tg-spaces-")
        doc = os.path.join(root, "My Projects", "Where Android", "docs", "README.md")
        os.makedirs(os.path.dirname(doc))
        open(doc, "w").close()
        base = "http://100.9.9.9:8788"
        href = f'<a href="{base}/open?path={quote(doc)}">'
        self.assertIn(f"{href}{doc}</a>.", fmt.link_text(f"Done: {doc}.", base, root))
        self.assertIn(href, fmt.link_paths(f"<code>{doc}</code>", base, root))

    def test_bare_file_name_in_a_question_is_a_link(self):
        # 03.10: a file was not highlighted, so the spec could not be viewed (a name without a folder, in parentheses)
        cwd = tempfile.mkdtemp(prefix="tg-bare-")
        spec = os.path.join(cwd, "inventory.md")
        open(spec, "w").close()
        base = "http://100.9.9.9:8788"
        out = fmt.link_text("Collected (inventory.md). As in Node.js and in notes.md, which does not exist?", base, cwd)
        self.assertIn(f'(<a href="{base}/open?path={quote(spec)}">inventory.md</a>).', out)
        self.assertIn("Node.js", out)
        self.assertNotIn(">Node.js<", out)
        self.assertIn("notes.md, which", out)          # no such file — not a link

    def test_reply_carries_quote(self):
        """30.09: the recipient sees what was replied to — the text without the session's signature; a selected fragment wins."""
        self.fake.updates = [self.msg("yes", 5, reply_to=40, quote=None)]
        self.fake.updates[0]["message"]["reply_to_message"]["text"] = lang.legacy("reply_head").format(name="w") + "\nOption 1 or 2?"
        self.bot.st["routes"]["40"] = {"session": "w"}
        self.bot.poll(timeout=0)
        self.assertEqual(self.spooled()[0]["data"]["quote"], "Option 1 or 2?")
        self.fake.updates = [self.msg("this one", 6, reply_to=41, quote={"text": "Option 2"})]
        self.fake.updates[0]["message"]["reply_to_message"]["text"] = "Hub: Option 1 or Option 2"
        self.bot.poll(timeout=0)
        self.assertEqual(self.spooled()[0]["data"]["quote"], "Option 2")
        self.fake.updates = [self.msg("just because", 7)]
        self.bot.poll(timeout=0)
        self.assertNotIn("quote", self.spooled()[0]["data"])

    def test_question_buttons_and_answer_from_page_closes_it(self):
        self.j.append("permission.requested", "hub", {"request_id": "r1", "tool": "AskUserQuestion", "input": {
            "questions": [{"question": "Which option?", "options": [{"label": "A"}, {"label": "B"}]}]}})
        self.bot.tick()
        card = self.fake.sent()[-1]
        self.assertIn("Which option?", card["text"])
        buttons = card["reply_markup"]["inline_keyboard"]
        # the standard button reads in the bot's language; pressed, it sends that label, which the core knows (owner.asks_more)
        self.assertEqual([b[0]["text"] for b in buttons], ["A", "B", i18n.t("core.owner.more_context")])
        self.fake.updates = [{"update_id": 20, "callback_query": {"id": "c1", "from": {"id": OWNER},
                                                                  "data": buttons[1][0]["callback_data"]}}]
        self.bot.poll(timeout=0)
        got = self.spooled()[0]
        self.assertEqual((got["type"], got["entity"], got["data"]["answers"]), ("permission.answered", "hub",
                                                                                 {"Which option?": "B"}))
        # a stranger cannot press; a repeated press gives "already answered"
        self.fake.updates = [{"update_id": 21, "callback_query": {"id": "c2", "from": {"id": 1},
                                                                  "data": buttons[0][0]["callback_data"]}}]
        self.bot.poll(timeout=0)
        self.assertEqual(self.spooled(), [])
        self.j.append("permission.answered", "hub", {"request_id": "r1", "behavior": "allow", "answers": {"Which option?": "B"},
                                                     "channel": "telegram", "owner": True})
        self.bot.tick()
        closed = self.fake.sent("editMessageText")[-1]   # the result goes into the card itself, the buttons removed by the same edit
        self.assertEqual(closed["reply_markup"], {"inline_keyboard": []})
        self.assertIn("✅ Answer: B", closed["text"])

    def test_the_turn_after_an_answer_in_telegram_comes_back_to_telegram(self):
        # 06.10: the owner answered a session's card with "Need more context" — the session wrote the context as its turn
        # result and a new card "context is in the message below", but there was nothing below: a turn result after a
        # button answer did not go to Telegram
        self.j.append("session.requested", "w", {"cwd": "/w", "session_id": "w"})
        self.j.append("session.started", "w", {"session_id": "w"})
        self.j.append("permission.requested", "w", {"request_id": "r1", "tool": "AskUserQuestion", "input": {
            "questions": [{"question": "Push the runbook?", "options": [{"label": "Yes"}, {"label": "Need more context"}]}]}})
        self.bot.tick()
        card = self.fake.next_id
        seq = self.j.append("permission.answered", "w", {"request_id": "r1", "behavior": "allow",
                                                        "answers": {"Push the runbook?": "Need more context"},
                                                        "channel": "telegram", "owner": True})["seq"]
        self.j.append("turn.started", "w", {"marker": f"m{seq}"})
        self.j.append("turn.result", "w", {"text": "Runbook — instructions for moving the users"})
        self.bot.tick()
        got = [m for m in self.fake.sent() if "Runbook — instructions" in m["text"]]
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].get("reply_to_message_id") or (got[0].get("reply_parameters") or {}).get("message_id"), card)
        self.assertIn("w", got[0]["text"].split("\n")[0])
        # answered on the page — the result is there too, we do not duplicate it in Telegram
        self.j.append("permission.requested", "w", {"request_id": "r2", "tool": "AskUserQuestion", "input": {
            "questions": [{"question": "And the tests?", "options": [{"label": "Yes"}]}]}})
        self.bot.tick()
        seq = self.j.append("permission.answered", "w", {"request_id": "r2", "behavior": "allow",
                                                        "answers": {"And the tests?": "Yes"}, "channel": "web", "owner": True})["seq"]
        self.j.append("turn.started", "w", {"marker": f"m{seq}"})
        self.j.append("turn.result", "w", {"text": "Ran the tests"})
        self.bot.tick()
        self.assertFalse([m for m in self.fake.sent() if "Ran the tests" in m["text"]])

    def test_option_previews_go_before_the_question_as_text_in_pre_and_as_photos(self):
        # a variant is judged by sight. An option's `preview` is an ASCII sketch (a
        # monospace block, spaces kept) or a path to a screenshot (a photo); both come right before the question card, so
        # the buttons stay the last thing in the chat
        self.j.append("session.requested", "w", {"cwd": "/w", "session_id": "w"})
        self.j.append("session.started", "w", {"session_id": "w"})
        shot = os.path.join(self.state, "attachments", "ab12", "shot.png")
        os.makedirs(os.path.dirname(shot))
        with open(shot, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n")
        sketch = "┌────────┐\n│ <Button> │\n└────────┘"
        self.j.append("permission.requested", "w", {"request_id": "r1", "tool": "AskUserQuestion", "interactive": True, "input": {
            "questions": [{"question": "Which button?", "options": [
                {"label": "Left", "description": "at the edge", "preview": sketch},
                {"label": "Right", "preview": shot},
                {"label": "No look", "preview": ""},
                {"label": "No file", "preview": "/nope/none.png"}]}]}})
        self.bot.tick()
        texts = self.fake.texts()
        pre = [i for i, x in enumerate(texts) if "<pre>" in x and "Left" in x]
        self.assertEqual(len(pre), 1, texts)
        self.assertIn("│ &lt;Button&gt; │", texts[pre[0]])        # escaped, the line structure is kept
        ask = [i for i, x in enumerate(texts) if "Which button?" in x]
        self.assertTrue(ask and ask[-1] > pre[0])                  # the question is after the sketch: the buttons are last
        photos = self.fake.sent("sendPhoto")
        self.assertEqual(len(photos), 1)                           # only the screenshot that exists and is shown
        self.assertIn("Right".encode(), photos[0]["raw"])         # the photo is signed with the option's name
        self.assertTrue([x for x in texts if MISSING in x and "/nope/none.png" in x])   # not shown / gone: a line
        self.assertFalse([x for x in texts if "No look" in x and "<pre>" in x])

    def test_an_option_preview_is_cut_for_the_limit_stays_text_and_a_picture_goes_only_by_the_file_rule(self):
        # a preview is the session's text: a huge one never makes a message Telegram refuses, markup in it is shown, not
        # run, and a file on disk that no session showed is not sent as a photo (the page's /api/file rule)
        self.j.append("session.requested", "w", {"cwd": "/w", "session_id": "w"})
        self.j.append("session.started", "w", {"session_id": "w"})
        loose = os.path.join(self.state, "loose", "x.png")        # on disk, but never shown by a session
        os.makedirs(os.path.dirname(loose))
        with open(loose, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n")
        tall = "\n".join(f"{n:04d} <b>&</b> " + "#" * 60 for n in range(400))   # ~28 000 characters
        self.j.append("permission.requested", "w", {"request_id": "r1", "tool": "AskUserQuestion", "interactive": True, "input": {
            "questions": [{"question": "Which look?", "options": [
                {"label": "Long " + "y" * 3000, "preview": tall},
                {"label": "Unshown file", "preview": loose}]}]}})
        self.bot.tick()
        visible = lambda t: len(html.unescape(re.sub(r"<[^>]+>", "", t)))   # noqa: E731
        texts = self.fake.texts()
        self.assertTrue(all(visible(t) <= 4096 for t in texts), [visible(t) for t in texts])
        sketch = [t for t in texts if "<pre>" in t and "0000 " in t]
        self.assertEqual(len(sketch), 1, texts)
        self.assertIn("0000 &lt;b&gt;&amp;&lt;/b&gt; ", sketch[0])   # markup is text
        self.assertNotIn("<b>&", sketch[0])
        self.assertTrue(sketch[0].endswith("…</pre>") and "0399 " not in sketch[0])   # cut at a line end, and it says so
        self.assertEqual(self.fake.sent("sendPhoto"), [])             # not shown by a session: no photo
        self.assertTrue([t for t in texts if MISSING in t and loose.replace("\\", "/") in t.replace("\\", "/")])

    def test_an_answer_the_core_rejected_keeps_the_card(self):
        # review: the core rejects another session's `hub allow`, while the bot closed the card with "✅ Answer: allowed" —
        # the session waits and the owner has no buttons
        self.j.append("session.requested", "w", {"cwd": "/w", "session_id": "w"})
        self.j.append("session.started", "w", {"session_id": "w"})
        self.j.append("permission.requested", "w", {"request_id": "r1", "tool": "Bash", "input": {"command": "ls"}})
        self.bot.tick()
        (ask,) = self.bot.st["asks"].values()
        ask["done"] = True                                              # the owner pressed — waiting for the core's decision
        seq = self.j.append("permission.answered", "w", {"request_id": "r1", "behavior": "allow", "channel": "session",
                                                        "owner": False, "sender": "x"})["seq"]
        self.j.append("core.rejected", "w", {"of": seq, "type": "permission.answered", "reason": "no"})
        self.bot.tick()
        self.assertFalse([t for t in self.fake.texts() if "✅ Answer" in t])
        self.assertEqual((ask["request_id"], ask["done"]), ("r1", True))  # another's rejection does not affect his press
        seq = self.j.append("permission.answered", "w", {"request_id": "r1", "behavior": "allow", "channel": "telegram",
                                                        "owner": True})["seq"]
        self.j.append("core.rejected", "w", {"of": seq, "type": "permission.answered", "reason": "in busy"})
        self.bot.tick()
        self.assertFalse([t for t in self.fake.texts() if "✅ Answer" in t])
        self.assertFalse(ask["done"])                                   # his press was rejected — the buttons respond again
        self.j.append("permission.answered", "w", {"request_id": "r1", "behavior": "allow", "channel": "web", "owner": True})
        self.bot.tick()
        self.assertIn("✅ Answer: allowed (on the page)", self.fake.last_text())
        self.assertEqual(self.bot.st["asks"], {})

    def test_every_question_has_a_more_context_button(self):
        # 06.10: "need more context" is a built-in answer to a card, not an option a session might forget
        from core import owner
        self.j.append("permission.requested", "hub", {"request_id": "r1", "tool": "AskUserQuestion", "input": {
            "questions": [{"question": "Which option?", "options": [{"label": "A"}, {"label": "B"}]}]}})
        self.j.append("permission.requested", "hub", {"request_id": "r2", "tool": "AskUserQuestion", "input": {
            "questions": [{"question": "What do we take?", "multiSelect": True, "options": [{"label": "A"}]}]}})
        self.j.append("permission.requested", "hub", {"request_id": "r3", "tool": "AskUserQuestion", "input": {
            "questions": [{"question": "Already there?", "options": [{"label": "Yes"}, {"label": owner.MORE_CONTEXT}]}]}})
        self.bot.tick()
        cards = [m for m in self.fake.sent() if m.get("reply_markup")]
        texts = [[b[0]["text"] for b in c["reply_markup"]["inline_keyboard"]] for c in cards]
        more = i18n.t("core.owner.more_context")   # the button reads in the bot's language; a session's own option as is
        self.assertEqual(texts, [["A", "B", more], ["☐ A", i18n.t("telegram.done"), more],
                                 ["Yes", owner.MORE_CONTEXT]])
        self.fake.updates = [{"update_id": 30, "callback_query": {"id": "c1", "from": {"id": OWNER},
                              "data": cards[1]["reply_markup"]["inline_keyboard"][2][0]["callback_data"]}}]
        self.bot.poll(timeout=0)
        got = self.spooled()[0]
        # the answer is the label in the owner's language, as he chose it; the core knows every form (owner.asks_more)
        self.assertEqual((got["type"], got["data"]["answers"]), ("permission.answered", {"What do we take?": more}))
        self.assertTrue(owner.asks_more(got["data"]["answers"]))

    def test_a_mermaid_diagram_in_a_reply_comes_as_a_picture(self):
        # 06.10: in Telegram mermaid diagrams go as pictures; if one fails to render — the source, as before
        import tempfile
        pics = []

        def fake_render(code):
            pics.append(code)
            if "broken" in code:
                return None
            path = os.path.join(tempfile.mkdtemp(), "schema.png")
            with open(path, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n" + b"x" * 50)
            return path
        self.bot.render_mermaid = fake_render
        self.bot.send("Diagram:\n```mermaid\ngraph TD\n  A-->B\n```\nand a broken one:\n```mermaid\nbroken\n```\nthe end")
        self.assertEqual(pics, ["graph TD\n  A-->B", "broken"])
        text = [m for m in self.fake.sent() if "the end" in m["text"]][0]["text"]
        self.assertNotIn("A--&gt;B", text)
        self.assertIn("diagram 1 — as a picture below", text)
        self.assertIn("broken", text)                                    # it failed to render — the source stays
        self.assertEqual([m for m, _ in self.fake.calls if m == "sendPhoto"], ["sendPhoto"])

    def test_risky_tool_card(self):
        self.j.append("catalog.loaded", None, {"tools": {"browser:mac": {"kind": "mcp", "risk": ["owner-data"], "server": {}}}})
        self.j.append("session.requested", "w", {"cwd": "/w", "session_id": "w"})
        self.j.append("session.started", "w", {"session_id": "w"})
        self.j.append("tools.granted", "w", {"tool": "browser:mac", "channel": "session", "owner": False, "sender": "hub"})
        self.bot.tick()
        card = self.fake.sent()[-1]
        self.assertIn("Connect <b>browser:mac</b> for <b>w</b>? Risk: owner-data.", card["text"])
        self.fake.updates = [{"update_id": 30, "callback_query": {"id": "c", "from": {"id": OWNER},
                                                                  "data": card["reply_markup"]["inline_keyboard"][0][0]["callback_data"]}}]
        self.bot.poll(timeout=0)
        got = self.spooled()[0]
        self.assertEqual((got["type"], got["entity"], got["data"]["tool"], got["data"]["channel"]),
                         ("tools.approved", "w", "browser:mac", "telegram"))

    def test_quiet_holds_questions_and_tool_requests_until_it_is_turned_off(self):
        # 14.10, feedback from colleagues and the owner: add a setting to the Telegram module — a button to turn the
        # notifications off. When on — session questions, permissions and tool connections do not go to Telegram; when
        # switched off — the open ones arrive at once, the ones answered on the page do not. Replies to his messages — as before
        self.bot.hub_config = lambda: {"telegram": {"quiet": True}}
        self.j.append("catalog.loaded", None, {"tools": {"browser:mac": {"kind": "mcp", "risk": ["owner-data"], "server": {}}}})
        self.j.append("session.requested", "w", {"cwd": "/w", "session_id": "w"})
        self.j.append("session.started", "w", {"session_id": "w"})
        self.j.append("permission.requested", "w", {"request_id": "r1", "tool": "AskUserQuestion", "input": {
            "questions": [{"question": "Run all the tests?", "options": [{"label": "Yes"}]}]}})
        self.j.append("permission.answered", "w", {"request_id": "r1", "answers": {"Run all the tests?": "Yes"},
                                                   "channel": "web", "owner": True})
        self.j.append("permission.requested", "w", {"request_id": "r2", "tool": "AskUserQuestion", "input": {
            "questions": [{"question": "Delete the branch?", "options": [{"label": "Yes"}]}]}})
        self.j.append("permission.requested", "w", {"request_id": "r3", "tool": "Bash", "input": {"command": "rm -r build"}})
        self.j.append("tools.granted", "w", {"tool": "browser:mac", "channel": "session", "owner": False, "sender": "hub"})
        self.bot.tick()
        self.assertEqual(self.fake.sent(), [])
        self.bot.save()
        bot = tg.Bot(self.state, tg.Api("1:x", base=self.fake.base))     # the held items survive a bot restart
        bot.catch_up()
        bot.hub_config = lambda: {"telegram": {"quiet": False}}
        bot.tick()
        texts = [m["text"] for m in self.fake.sent()]
        self.assertFalse([t for t in texts if "Run all the tests" in t], texts)
        self.assertEqual(len([t for t in texts if "Delete the branch" in t]), 1, texts)
        self.assertEqual(len([t for t in texts if "rm -r build" in t]), 1, texts)
        self.assertEqual(len([t for t in texts if "Connect <b>browser:mac</b>" in t]), 1, texts)
        bot.tick()
        self.assertEqual(len(self.fake.sent()), len(texts))                 # it does not send a second time

    def test_quiet_releases_the_hubs_question_drops_answered_and_sends_no_reminders(self):
        # review 14.10: the core turns the hub's question into a card at once (asks, not pending) — it was lost on
        # switching off; ones answered on the page piled up in held; "⏰ waiting for an answer" reminders went out although
        # questions were asked not to be sent
        self.bot.hub_config = lambda: {"telegram": {"quiet": True}}
        self.j.append("permission.requested", "hub", {"request_id": "h1", "tool": "AskUserQuestion", "input": {
            "questions": [{"question": "Release today?", "options": [{"label": "Yes"}]}]}})
        self.j.append("session.requested", "w", {"cwd": "/w", "session_id": "w"})
        self.j.append("session.started", "w", {"session_id": "w"})
        self.j.append("permission.requested", "w", {"request_id": "r9", "tool": "Bash", "input": {"command": "ls"}})
        self.j.append("permission.answered", "w", {"request_id": "r9", "behavior": "allow", "channel": "web", "owner": True})
        self.bot.tick()
        self.assertEqual(list(self.bot.st["held"]), ["ask:h1"])          # the answered one does not pile up
        from core import owner
        (qid,) = [q[0] for q in owner.questions(self.bot.reader.state)]
        self.j.append("timer.fired", qid, {"n": 1})                         # the reminder is due
        self.bot.tick()
        self.assertEqual(self.bot.reader.state["owner"]["reminders"].get(qid), 1)
        self.assertEqual(self.fake.sent(), [])
        self.bot.hub_config, self.bot._quiet = (lambda: {"telegram": {"quiet": False}}), (float("-inf"), False)
        self.bot.tick()
        self.assertEqual(len([m for m in self.fake.sent() if "Release today" in m["text"]]), 1)
        self.assertEqual(self.bot.st["held"], {})

    @tz.need
    def test_non_working_hours_pile_up_and_come_as_one_batch_in_the_morning(self):
        # hub.json quiet_review on the weekend and after 19:00 cards do not go to Telegram; at
        # 10:00 of the working day — one line "piled up: N", then the cards; what was answered on the page is not in it
        from tests.test_quiet_review import TZ, setting, ts as at
        now = [at(7, 12)]
        self.bot.clock = lambda: now[0]
        self.j.append("owner.settings", None, {"timezone": TZ, "quiet_review": setting()}, ts=now[0])
        for n in ("w", "v"):
            self.j.append("session.requested", n, {"cwd": "/w", "session_id": n}, ts=now[0])
            self.j.append("session.started", n, {"session_id": n}, ts=now[0])
        for rid, n, q, t in (("r1", "w", "Delete the branch?", at(7, 15)), ("r2", "v", "Paint it blue?", at(8, 11)),
                             ("r3", "w", "Run all tests?", at(8, 20))):
            self.j.append("turn.started", n, {"marker": None}, ts=t)
            self.j.append("permission.requested", n, {"request_id": rid, "tool": "AskUserQuestion", "interactive": True,
                                                      "input": {"questions": [{"question": q, "options": [{"label": "Yes"}]}]}},
                          ts=t)
            now[0] = t
            self.bot.tick()
        self.j.append("permission.answered", "w", {"request_id": "r3", "answers": {"Run all tests?": "Yes"},
                                                   "channel": "web", "owner": True}, ts=at(8, 21))
        now[0] = at(9, 9, 59)
        self.bot.tick()
        self.assertEqual(self.fake.sent(), [])
        now[0] = at(9, 10)
        self.bot.tick()
        texts = [m["text"] for m in self.fake.sent()]
        self.assertEqual(texts[0], i18n.t("telegram.review_batch", count=2))
        self.assertEqual([len([t for t in texts if q in t]) for q in ("Delete the branch", "Paint it blue", "Run all tests")],
                         [1, 1, 0], texts)
        self.assertEqual(len(texts), 3, texts)
        self.assertEqual(self.bot.st["held"], {})
        now[0] = at(9, 10, 1)
        self.bot.tick()
        self.assertEqual(len(self.fake.sent()), 3)                          # neither the line nor the cards a second time

    def test_a_rejected_denial_keeps_the_held_tool_card(self):
        # review 14.10: a refusal the core did not accept ("waits for the owner") removed the held card — it never arrived
        self.bot.hub_config = lambda: {"telegram": {"quiet": True}}
        self.j.append("catalog.loaded", None, {"tools": {"browser:mac": {"kind": "mcp", "risk": ["owner-data"], "server": {}}}})
        self.j.append("session.requested", "w", {"cwd": "/w", "session_id": "w"})
        self.j.append("session.started", "w", {"session_id": "w"})
        self.j.append("tools.granted", "w", {"tool": "browser:mac", "channel": "session", "owner": False, "sender": "hub"})
        self.j.append("tools.denied", "w", {"tool": "browser:mac", "channel": "session", "owner": False, "sender": "hub"})
        self.bot.tick()
        self.bot.hub_config, self.bot._quiet = (lambda: {"telegram": {"quiet": False}}), (float("-inf"), False)
        self.bot.tick()
        self.assertEqual(len([m for m in self.fake.sent() if "Connect <b>browser:mac</b>" in m["text"]]), 1)

    def test_released_card_waits_out_a_network_error(self):
        # review 14.10: a network failure while sending the held items crashed the bot, and what was already sent
        # went out a second time
        self.bot.hub_config = lambda: {"telegram": {"quiet": True}}
        self.j.append("session.requested", "w", {"cwd": "/w", "session_id": "w"})
        self.j.append("session.started", "w", {"session_id": "w"})
        for rid in ("a1", "a2"):
            self.j.append("permission.requested", "w", {"request_id": rid, "tool": "Bash", "input": {"command": f"echo {rid}"}})
        self.bot.tick()
        self.bot.hub_config, self.bot._quiet = (lambda: {"telegram": {"quiet": False}}), (float("-inf"), False)
        sent, real = [], self.bot.on_ask

        def flaky(name, data):
            if data["request_id"] == "a2" and not sent.count("a2-fail"):
                sent.append("a2-fail")
                raise tg.TgError("network", transient=True)
            sent.append(data["request_id"])
            return real(name, data)
        self.bot.on_ask = flaky
        self.bot.tick()                                                   # does not crash
        self.assertEqual(list(self.bot.st["held"]), ["ask:a2"])
        self.assertGreater(self.bot.hold_until, self.bot.clock())         # the retry comes after a pause, like flush
        self.bot.hold_until = 0
        self.bot.tick()
        self.assertEqual((sent, self.bot.st["held"]), (["a1", "a2-fail", "a2"], {}))

    def test_permission_card_says_why_it_asks(self):
        self.j.append("session.requested", "w", {"cwd": "/w", "session_id": "w"})
        self.j.append("session.started", "w", {"session_id": "w"})
        self.j.append("permission.requested", "w", {"request_id": "r1", "tool": "Bash", "input": {"command": "git push -f"},
                                                    "why": "the ask rule Bash(git push -f *)"})
        self.bot.tick()
        self.assertIn("Why it asks: the ask rule Bash(git push -f *)", self.fake.sent()[-1]["text"])

    def test_a_request_the_core_allowed_itself_is_not_sent(self):
        # permissions.auto_allow (14.10): the core answered itself — there is no card in Telegram
        self.j.append("session.requested", "w", {"cwd": "/w", "session_id": "w"})
        self.j.append("session.started", "w", {"session_id": "w"})
        self.j.append("permission.requested", "w", {"request_id": "r1", "tool": "Bash", "input": {"command": "cd /w; make > l"},
                                                    "auto": "allow"}, src="host")
        self.bot.tick()
        self.assertEqual(self.fake.sent(), [])
        # another mark — not "allowed by the core": the core holds the request for the owner, the card arrives
        self.j.append("permission.requested", "w", {"request_id": "r2", "tool": "Bash", "input": {"command": "ls"},
                                                    "auto": "cd"}, src="host")
        self.bot.tick()
        self.assertEqual(len(self.fake.sent()), 1)

    def test_pairing_code(self):
        tg.save_config({"token": "1:x"})
        self.bot.cfg = tg.load_config()
        self.fake.updates = [self.msg("/start", 1, user=555)]
        self.bot.poll(timeout=0)
        code = next(iter(tg.load_config()["pending"]))
        self.assertIn(code, self.fake.sent()[-1]["text"])
        os.environ["HUB_TELEGRAM_API"] = self.fake.base
        try:
            import importlib
            importlib.reload(tg)
            tg.main(["pair", code])
        finally:
            os.environ.pop("HUB_TELEGRAM_API")
            importlib.reload(tg)
        self.assertEqual(tg.load_config()["owners"], [555])

    def test_photo_becomes_attachment(self):
        self.fake.updates = [{"update_id": 40, "message": {"message_id": 40, "from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"},
                                                          "caption": "what is this?", "photo": [{"file_id": "f1"}]}}]
        self.bot.poll(timeout=0)
        got = self.spooled()[0]["data"]
        self.assertEqual(got["text"], "what is this?")
        with open(got["attachments"][0]["path"], "rb") as f:
            self.assertEqual(f.read(), b"JPEGDATA")

    # 03.10: what the bot cannot parse, it does not lose (a message that did not arrive was reported as a bug);
    # 14.10 (a feed review: service notes in the text looked like the owner wrote them to the agents): the reason goes
    # in the bot's reply in Telegram and in the attach_failed event field, while text holds only the owner's words
    def undownloaded(self, mid, message, download=None):
        calls = []

        def spy(file_id):
            calls.append(file_id)
            return download(file_id) if download else b"JPEGDATA"
        self.bot.api.download = spy
        self.fake.updates = [{"update_id": mid, "message": {"message_id": mid, "from": {"id": OWNER},
                                                           "chat": {"id": OWNER, "type": "private"}, **message}}]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.bot.poll(timeout=0)
        got = [e["data"] for e in self.spooled() if e["type"] == "message.received"]
        replies = [(s["text"], (s.get("reply_parameters") or {}).get("message_id")) for s in self.fake.sent()]
        return (got[0] if got else None), calls, err.getvalue(), replies

    def test_only_an_attachment_that_did_not_download_is_answered_in_telegram_not_sent_to_the_hub(self):
        def fail(_file_id):
            raise tg.TgError("file is too big")
        got, _, _, replies = self.undownloaded(41, {"photo": [{"file_id": "f1"}]}, download=fail)
        self.assertIsNone(got)   # no words, no file — the session has nothing to read
        ((reply, to),) = replies
        self.assertEqual(to, 41)
        self.assertIn("Did not download", reply)
        self.assertIn("photo-41.jpg", reply)

    def test_too_big_attachment_names_its_size_and_the_limit_without_downloading(self):
        got, calls, err, replies = self.undownloaded(43, {"caption": "estimate", "document": {
            "file_id": "big1", "file_name": "plan.pdf", "file_size": 26_500_000}})
        self.assertEqual(calls, [])   # the Bot API will not serve such a file — do not ask in vain
        self.assertEqual(got["text"], "estimate")   # the owner's words — without a service note
        self.assertEqual(got["attachments"], [])
        (failed,) = got["attach_failed"]
        self.assertEqual((failed["name"], failed["size"]), ("plan.pdf", 26_500_000))
        for part in ("plan.pdf", "25.3 MB", "20 MB"):
            self.assertIn(part, failed["reason"])
        ((reply, to),) = replies
        self.assertEqual(to, 43)
        for part in ("Did not download", "plan.pdf", "25.3 MB", "20 MB"):
            self.assertIn(part, reply)
        self.assertIn("big1", err)
        self.assertIn("too_big", err)

    def test_telegram_refusal_is_named_with_its_reason(self):
        def refuse(_file_id):
            raise tg.TgError("getFile: Bad Request: file is too big")
        got, _, err, replies = self.undownloaded(44, {"caption": "here", "photo": [{"file_id": "p0", "file_size": 900},
                                                                                {"file_id": "p1"}]}, download=refuse)
        self.assertEqual(got["text"], "here")
        self.assertIn("larger than the", got["attach_failed"][0]["reason"])
        self.assertIn("larger than the", replies[0][0])

    def test_telegram_error_is_named_with_its_text(self):
        def broken(_file_id):
            raise tg.TgError("getFile: HTTP 404 b'Not Found'")
        got, _, err, replies = self.undownloaded(
            45, {"caption": "photo", "photo": [{"file_id": "p2", "file_size": 1_200_000}]}, download=broken)
        reason = got["attach_failed"][0]["reason"]
        for part in ("Telegram error", "HTTP 404", "1.1 MB"):
            self.assertIn(part, reason)
            self.assertIn(part, replies[0][0])
        self.assertNotIn("HTTP 404", got["text"])
        self.assertIn("p2", err)
        self.assertIn("telegram", err)

    def test_network_failure_is_named_as_such(self):
        def slow(_file_id):
            raise TimeoutError("timed out")
        got, _, err, replies = self.undownloaded(
            46, {"caption": "text", "document": {"file_id": "d1", "file_name": "a.txt"}}, download=slow)
        self.assertEqual(got["text"], "text")
        self.assertIn("timeout", got["attach_failed"][0]["reason"])
        self.assertIn("a.txt", replies[0][0])
        self.assertIn("d1", err)
        self.assertIn("network", err)

    def test_one_of_two_attachments_failed_the_other_reaches_the_hub(self):
        def half(file_id):
            if file_id == "p3":
                return b"JPEGDATA"
            raise TimeoutError("timed out")
        got, _, _, replies = self.undownloaded(47, {"photo": [{"file_id": "p3"}],
                                                    "document": {"file_id": "d2", "file_name": "b.txt"}}, download=half)
        self.assertEqual(got["text"], "(attachments)")
        self.assertEqual([a["name"] for a in got["attachments"]], ["photo-47.jpg"])
        self.assertEqual([f["name"] for f in got["attach_failed"]], ["b.txt"])
        self.assertIn("b.txt", replies[0][0])

    def test_a_failed_reply_does_not_lose_the_owner_words(self):
        def down(*_a, **_k):
            raise tg.TgError("sendMessage: HTTP 502")
        self.bot.send = down

        def slow(_file_id):
            raise TimeoutError("timed out")
        got, _, err, _ = self.undownloaded(49, {"caption": "take it", "document": {"file_id": "d3", "file_name": "c.txt"}},
                                           download=slow)
        self.assertEqual(got["text"], "take it")
        self.assertEqual([f["name"] for f in got["attach_failed"]], ["c.txt"])
        self.assertIn("HTTP 502", err)

    def test_an_answer_to_a_card_with_a_failed_attachment_still_names_the_reason(self):
        # a text answer to a question card goes as a form — the bot still reports the file that did not arrive
        self.bot.st["routes"]["900"] = {"ask": "a1", "q": 0}
        self.bot.answer = lambda *_a, **_k: True
        got, _, _, replies = self.undownloaded(50, {"caption": "see the file", "reply_to_message": {"message_id": 900},
                                                    "document": {"file_id": "d5", "file_name": "big.zip",
                                                                 "file_size": 30_000_000}})
        self.assertIsNone(got)   # the answer went as a form, not a message
        self.assertIn("big.zip", [r for r, to in replies if to == 50][0])

    def test_a_download_cut_midway_is_a_network_failure(self):
        def cut(_file_id):
            raise http.client.IncompleteRead(b"xx", 100)
        got, _, err, replies = self.undownloaded(51, {"caption": "take it", "document": {"file_id": "d6",
                                                                                     "file_name": "e.txt"}}, download=cut)
        self.assertEqual(got["text"], "take it")
        self.assertEqual([f["name"] for f in got["attach_failed"]], ["e.txt"])
        self.assertIn("network", err)

    def test_a_cut_answer_to_a_send_keeps_the_message_and_repeats_it(self):
        # http.client raises IncompleteRead (not an OSError) on a cut answer: it is a network failure like a dropped
        # connection — the message waits for a repeat, it is not skipped as a bot error
        real, cut = self.bot.api.keep_alive, [True]

        def keep_alive(verb, url, *a, **k):
            if cut and url.endswith("/sendMessage"):
                raise http.client.IncompleteRead(b"{", 50)
            return real(verb, url, *a, **k)
        self.bot.api.keep_alive = keep_alive
        self.task_for_owner_message()
        self.j.append("turn.result", "w", {"text": "first"})
        self.j.append("turn.stopped", "w", {})
        with contextlib.redirect_stderr(io.StringIO()):
            self.bot.tick()
        self.assertEqual(self.fake.sent(), [])
        self.assertTrue(self.bot.outbox)
        self.assertEqual(self.bot.fails, 1)
        cut.clear()
        self.bot.hold_until = 0
        self.bot.tick()
        self.assertEqual(len(self.fake.sent()), 1)
        self.assertEqual(self.bot.outbox, [])

    def test_a_reply_that_breaks_otherwise_does_not_duplicate_the_message(self):
        def broken(*_a, **_k):
            raise ValueError("fmt")
        self.bot.send = broken
        self.fake.updates = [{"update_id": 52, "message": {
            "message_id": 52, "from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"}, "caption": "one",
            "document": {"file_id": "d7", "file_name": "f.txt", "file_size": 30_000_000}}}]
        with contextlib.redirect_stderr(io.StringIO()):
            self.bot.poll(timeout=0)
        self.assertEqual([e["data"]["text"] for e in self.spooled() if e["type"] == "message.received"], ["one"])

    def test_a_broken_reply_to_an_attachment_only_message_still_reaches_the_hub(self):
        # with no words and no file no event is written — the bot's reply broke: the message is not lost (lost())
        def broken(*_a, **_k):
            raise ValueError("fmt")
        self.bot.send = broken
        self.fake.updates = [{"update_id": 53, "message": {
            "message_id": 53, "from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"},
            "document": {"file_id": "d8", "file_name": "g.txt", "file_size": 30_000_000}}}]
        with contextlib.redirect_stderr(io.StringIO()):
            self.bot.poll(timeout=0)
        self.assertEqual(len([e for e in self.spooled() if e["type"] == "message.received"]), 1)

    def test_an_attachment_only_message_survives_a_network_failure_of_the_reply(self):
        # the attachment was not downloaded and the bot's reply failed on the network (the Bot API drops the
        # connection) — nothing is recorded: lost() keeps it
        self.fake.fail["sendMessage"] = ["drop"] * 10
        self.fake.updates = [{"update_id": 54, "message": {
            "message_id": 54, "from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"},
            "document": {"file_id": "d9", "file_name": "h.txt", "file_size": 30_000_000}}}]
        with contextlib.redirect_stderr(io.StringIO()):
            self.bot.poll(timeout=0)
        got = [e["data"] for e in self.spooled() if e["type"] == "message.received"]
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["tg"]["id"], 54)

    def test_an_owner_message_does_not_name_a_client(self):
        # the label of a Telegram message stays as it was: the bot adds no app to the fact
        self.fake.updates = [{"update_id": 55, "message": {
            "message_id": 55, "from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"}, "text": "hello"}}]
        self.bot.poll(timeout=0)
        got = [e["data"] for e in self.spooled() if e["type"] == "message.received"]
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["tg"]["id"], 55)
        self.assertNotIn("app", got[0])
        self.assertNotIn("app_version", got[0])

    def test_a_message_whose_attachments_all_downloaded_gets_no_extra_reply(self):
        got, _, _, replies = self.undownloaded(48, {"caption": "ok", "photo": [{"file_id": "p4"}]})
        self.assertNotIn("attach_failed", got)
        self.assertEqual(replies, [])

    def test_sticker_gets_an_answer_not_silence(self):
        self.fake.updates = [{"update_id": 42, "message": {"message_id": 42, "from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"},
                                                          "sticker": {"file_id": "s1", "emoji": "👍"}}}]
        self.bot.poll(timeout=0)
        self.assertEqual(self.spooled(), [])
        self.assertIn("don't understand", self.fake.sent()[-1]["text"])

    def test_a_failure_while_reading_still_hands_the_text_to_the_hub(self):
        def boom(*_a):
            raise KeyError("routes")
        self.bot.quote = boom
        self.fake.updates = [self.msg("important", 43)]
        self.bot.poll(timeout=0)
        (got,) = [e["data"] for e in self.spooled() if e["type"] == "message.received"]
        self.assertTrue(got["text"].startswith("important"))
        self.assertNotIn("to", got)                                    # to the hub: it will sort it out
        self.assertEqual(self.bot.st["offset"], 44)                     # the bot does not get stuck further on

    # --- a long result: the gist outside, the rest folded (telegram.fold_long, owner decision 2026-10-20) --
    LONG = "Done: updated `docs/a.md`, tests are green.\n\n" + "\n\n".join(f"Paragraph {i}: " + "words " * 30 for i in range(3)) + "\n\n- one\n- two"

    def long_result(self, text, on=True):
        self.bot.hub_config = lambda: {"telegram": {"fold_long": True}} if on else {}
        self.clocked()
        self.j.append("turn.started", "hub", {"marker": "m3"})
        self.j.append("turn.result", "hub", {"text": text})
        self.bot.tick()
        return [s["text"] for s in self.fake.sent()]

    def test_a_long_result_is_folded_behind_its_first_line(self):
        (text,) = self.long_result(self.LONG)
        first, rest = text.split("\n", 1)
        self.assertEqual(first, "Done: updated <code>docs/a.md</code>, tests are green.")
        self.assertTrue(rest.startswith("<blockquote expandable>") and rest.endswith("</blockquote>"), rest)
        self.assertIn("Paragraph 2", rest)
        self.assertEqual(rest.count("<blockquote"), 1)

    def test_a_short_result_is_not_folded(self):
        self.assertEqual(self.long_result("Done.\nSecond line, third.\nOne more."), ["Done.\nSecond line, third.\nOne more."])

    def test_a_long_result_is_as_before_when_the_option_is_off(self):
        (text,) = self.long_result(self.LONG, on=False)
        self.assertNotIn("<blockquote", text)

    def test_a_long_result_in_pieces_folds_every_piece(self):
        big = "Gist.\n\n" + "\n\n".join("Paragraph %d: %s" % (i, "words " * 150) for i in range(12))
        parts = self.long_result(big)
        self.assertGreater(len(parts), 1)
        for i, t in enumerate(parts):
            self.assertLessEqual(len(t), 4096)
            self.assertEqual((t.count("<blockquote expandable>"), t.count("</blockquote>")), (1, 1), t[:80])
            self.assertTrue(t.endswith("</blockquote>"))
            self.assertEqual(t.startswith("<blockquote"), i > 0)   # the gist only in the first one


class BrokenReplies(unittest.TestCase):
    """A server whose answer is cut or is not HTTP at all: http.client raises HTTPException (IncompleteRead, BadStatusLine),
    which is not an OSError — Api turns it into a network TgError, so the bot's loop does not crash."""

    def serve(self, reply):
        import socket
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(8)
        self.addCleanup(srv.close)

        def run():
            while True:
                try:
                    c, _ = srv.accept()
                except OSError:
                    return
                with c:
                    c.settimeout(5)
                    try:
                        c.recv(65536)
                        c.sendall(reply)
                    except OSError:
                        pass   # the client has already gone

        threading.Thread(target=run, daemon=True).start()
        return tg.Api("1:x", base=f"http://127.0.0.1:{srv.getsockname()[1]}")

    CUT = b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{\"ok\": tr"   # the body ends before Content-Length
    NOT_HTTP = b"garbage from a proxy\r\n\r\n"

    def test_a_cut_answer_is_a_network_failure_in_call(self):
        for reply in (self.CUT, self.NOT_HTTP):
            with self.subTest(reply=reply[:12]):
                with self.assertRaises(tg.TgError) as cm:
                    self.serve(reply).call("sendMessage", chat_id=1, text="x")
                self.assertTrue(cm.exception.transient)
                self.assertFalse(cm.exception.unsent)   # it may have reached Telegram: the caller must not call it unsent

    def test_a_cut_answer_to_a_once_call_is_a_network_failure(self):
        with self.assertRaises(tg.TgError) as cm:
            self.serve(self.CUT).call("sendMessage", _once=True, chat_id=1, text="x")
        self.assertTrue(cm.exception.transient)

    def test_a_cut_answer_is_a_network_failure_in_an_upload(self):
        d = self.enterContext(tempfile.TemporaryDirectory(prefix="hubtg-"))
        path = os.path.join(d, "a.txt")
        with open(path, "w") as f:
            f.write("data")
        with self.assertRaises(tg.TgError) as cm:
            self.serve(self.CUT).upload("sendDocument", "document", path, chat_id=1)
        self.assertTrue(cm.exception.transient)

    def test_a_broken_file_download_is_a_network_failure(self):
        api = self.serve(self.NOT_HTTP)
        calls = []

        def call(method, **kw):
            calls.append(method)
            return {"file_path": "photos/p.jpg"}
        api.call = call   # getFile itself answers; the file's own GET is cut
        with self.assertRaises(tg.TgError) as cm:
            api.download("f1")
        self.assertTrue(cm.exception.transient)
        self.assertEqual(calls, ["getFile"])

    def test_a_file_cut_short_of_its_content_length_is_not_taken_for_the_whole_file(self):
        # read(limit) returns what came before the break without an error: the length is checked against Content-Length
        api = self.serve(self.CUT)
        api.call = lambda method, **kw: {"file_path": "photos/p.jpg"}
        with self.assertRaises(tg.TgError) as cm:
            api.download("f1")
        self.assertTrue(cm.exception.transient)

    def test_a_chunked_file_cut_midway_is_a_network_failure(self):
        api = self.serve(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n64\r\n" + b"x" * 20)
        api.call = lambda method, **kw: {"file_path": "photos/p.jpg"}
        with self.assertRaises(tg.TgError) as cm:
            api.download("f1")
        self.assertTrue(cm.exception.transient)

    def test_a_whole_file_under_the_limit_is_returned_as_it_is(self):
        body = b"x" * 50
        api = self.serve(b"HTTP/1.1 200 OK\r\nContent-Length: 50\r\n\r\n" + body)
        api.call = lambda method, **kw: {"file_path": "photos/p.jpg"}
        self.assertEqual(api.download("f1"), body)

    def test_a_cut_getfile_answer_is_a_network_failure(self):
        with self.assertRaises(tg.TgError) as cm:
            self.serve(self.CUT).download("f1")
        self.assertTrue(cm.exception.transient)


class Format(unittest.TestCase):
    def test_bold_around_inline_code(self):
        # 04.10: "**The folder `lab`** was not touched" went out with asterisks — the code cut the line before the bold span
        out = fmt.render_text("- **The folder `maps-lab`** was not touched; **`demo`** warned; *italic `x`*")
        self.assertIn("<b>The folder <code>maps-lab</code></b>", out)
        self.assertIn("<b><code>demo</code></b>", out)
        self.assertIn("<i>italic <code>x</code></i>", out)
        self.assertNotIn("*", out)
        self.assertEqual(fmt.render_text("`a**b**c` and **d**"), "<code>a**b**c</code> and <b>d</b>")  # inside code it stays as is

    def test_markdown_to_telegram_html(self):
        out = fmt.to_messages("## Summary\n**bold** and `code` <tag>\n\n| a | b |\n|---|---|\n| 1 | 2 |")
        self.assertEqual(out, ["<b>Summary</b>\n<b>bold</b> and <code>code</code> &lt;tag&gt;\n\n<pre>a │ b\n1 │ 2</pre>"])
        self.assertEqual(sum(len(x) for x in fmt.to_messages("a" * 9000)), 9000)

    def test_file_paths_become_links(self):
        """30.09: a session wrote docs/decisions.md and the owner could not open it — a path in `…` leads to "Files"."""
        cwd = tempfile.mkdtemp(prefix="tg-cwd-")
        os.makedirs(os.path.join(cwd, "docs"))
        open(os.path.join(cwd, "docs", "decisions.md"), "w").close()
        base = "http://100.9.9.9:8788"
        html_in = fmt.to_messages("Wrote to `docs/decisions.md`, line `docs/decisions.md:12`; `no/such.md` and `ls -la`")[0]
        out = fmt.link_paths(html_in, base, cwd)
        target = "http://100.9.9.9:8788/open?path=" + quote(os.path.join(cwd, "docs", "decisions.md"))
        self.assertIn(f'<a href="{target}">docs/decisions.md</a>', out)
        self.assertIn(f'<a href="{target}&amp;line=12">docs/decisions.md:12</a>', out)  # to the line
        self.assertIn("<code>no/such.md</code>", out)  # no such file — no link
        self.assertIn("<code>ls -la</code>", out)
        self.assertEqual(fmt.link_paths(html_in, None, cwd), html_in)  # tailnet is not working — unchanged
        # a path from a neighbouring folder (`<session folder>/docs/…`) — do not glue it to the session folder a second time
        rel = os.path.basename(cwd) + "/docs/decisions.md"
        self.assertIn(f'<a href="{target}">{rel}</a>', fmt.link_paths(fmt.to_messages(f"`{rel}`")[0], base, cwd))

    def test_session_names_become_links(self):
        """06.10: a session name in `…` is a link to its tab; foreign code and nonexistent names stay as they were."""
        html_in = fmt.to_messages("Woke `maps`, no `maps-2`, `ls -la` — code")[0]
        out = fmt.link_sessions(html_in, "http://100.9.9.9:8787", {"maps", "docs"})
        self.assertIn('<a href="http://100.9.9.9:8787/#/s/maps"><code>maps</code></a>', out)
        self.assertIn("<code>maps-2</code>", out)
        self.assertIn("<code>ls -la</code>", out)
        self.assertEqual(fmt.link_sessions(html_in, None, {"maps"}), html_in)  # tailnet is not working — unchanged

    def test_fold_keeps_the_gist_outside_and_the_rest_in_one_expandable_quote(self):
        md = "Gist **here**\n\n## Section\ntext `code` and [link](https://x.io)\n\n> quote\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n" + "yet " * 80
        (out,) = fmt.to_messages(md, head="<b>s</b>:", fold=True)
        first, rest = out.split("\n", 2)[:2], out.split("\n", 2)[2]
        self.assertEqual(first, ["<b>s</b>:", "Gist <b>here</b>"])
        self.assertTrue(rest.startswith("<blockquote expandable><b>Section</b>\n"))
        self.assertIn("<pre>a │ b\n1 │ 2</pre>", rest)
        self.assertEqual((rest.count("<blockquote"), rest.count("</blockquote>")), (1, 1))  # the quote is not nested

    def test_fold_off_or_short_is_the_old_output(self):
        long = "Gist\n\n" + "yet " * 100
        self.assertEqual(fmt.to_messages(long), fmt.to_messages(long, fold=False))
        self.assertNotIn("<blockquote", fmt.to_messages(long)[0])
        short = "Gist\na\nb\nc"
        self.assertEqual(fmt.to_messages(short, fold=True), fmt.to_messages(short))
        self.assertEqual(fmt.to_messages("```\ncode\n```\n" + "a" * 400, fold=True), fmt.to_messages("```\ncode\n```\n" + "a" * 400))

    def test_fold_keeps_the_rest_when_the_gist_grows_on_escaping(self):
        # review: a gist of up to FOLD_GIST characters grew from escaping (`<` → `&lt;`) — the body did not fit and was lost
        md = "<" * 990 + "\n" + "lines\n" * 200
        out = "\n".join(fmt.to_messages(md, head="<b>s</b>:", fold=True))
        self.assertEqual(out.count("lines"), 200)
        self.assertNotIn("<blockquote", out)

    def test_fold_limits(self):
        # more than 3 non-empty lines after the first, or longer than 300 characters — we fold; exactly at the
        # threshold — we do not
        self.assertNotIn("<blockquote", fmt.to_messages("S\n1\n2\n3", fold=True)[0])
        self.assertIn("<blockquote", fmt.to_messages("S\n1\n2\n3\n4", fold=True)[0])
        self.assertNotIn("<blockquote", fmt.to_messages("S\n" + "a" * 300, fold=True)[0])
        self.assertIn("<blockquote", fmt.to_messages("S\n" + "a" * 301, fold=True)[0])

    def test_fold_pieces_are_balanced_html(self):
        md = "Gist\n\n" + "\n\n".join(f"**Block {i}** " + "texts " * 120 for i in range(15))
        out = fmt.to_messages(md, fold=True)
        self.assertGreater(len(out), 1)
        for i, t in enumerate(out):
            self.assertLessEqual(len(t), 4096)
            tags = re.findall(r"<(/?)(\w+)", t)
            depth = []
            for close, name in tags:
                if close:
                    self.assertEqual(depth.pop(), name)
                else:
                    depth.append(name)
            self.assertEqual(depth, [])
            self.assertEqual(t.count("<blockquote expandable>"), 1)
            self.assertEqual(t.startswith("<blockquote"), i > 0)

if __name__ == "__main__":
    unittest.main()
