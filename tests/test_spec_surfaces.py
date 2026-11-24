"""Spec mode where a person meets it (core/specmode.py): /spec on|off in the page's composer and in Telegram, the spec's
strip in "Files" with "Accept" / "Drop", the card "Spec dN is ready" in Telegram with the file. "Accept" sends the sum
takes the file as it is at the press, however it changed since the card was drawn. The model is not called."""
import tests  # noqa: F401 — hide the machine's settings
import hashlib
import http.client
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

import platforms
from core import journal, spool
from modules.files import server as files_server
from modules.files.server import Files
from modules.telegram import bot as tg
from tests.test_telegram import OWNER, FakeBotApi

WEB = {"channel": "web", "owner": True}


def by(name):
    return {"channel": "session", "owner": False, "sender": name}


def spec_journal(j, text="# Export\n\nThree slices.\n", state=None, background=False):
    """maps (specs on) raised maps-1 with --spec; its first result made the spec ready (background — it has a job of its
    own still running at the result). -> the task id."""
    j.append("session.requested", "maps", {"cwd": "/w", "session_id": "s-maps", "role": "agent", "about": "Maps",
                                           "channel": "cli", "owner": True})
    j.append("session.started", "maps", {"session_id": "s-maps"})
    j.append("session.spec_set", "maps", {"on": True, **WEB})
    j.append("task.run_requested", "maps-1", {"task": "design the export", "spec": True, **by("maps")})
    j.append("session.requested", "maps-1", {"session_id": "s1", "executor": True, "raised_by": "maps", "cwd": "/w/x",
                                             "about": "design the export"})
    seq = j.append("delegation.created", None, {"session": "maps-1", "for_session": "maps", "context": "design",
                                                **by("maps")})["seq"]
    did = f"d{seq}"
    j.append("session.started", "maps-1", {"session_id": "s1"})
    j.append("turn.started", "maps-1", {"marker": f"m{seq}"})
    if background:
        j.append("session.background", "maps-1", {"tasks": [{"id": "b1", "kind": "bash", "title": "render"}]})
    j.append("turn.result", "maps-1", {"text": "The spec is ready."})
    j.append("turn.stopped", "maps-1", {})
    if state:
        path = os.path.join(state, "specs", did, "spec.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:   # these bytes: the sum is over them (Windows: no CRLF)
            f.write(text)
    return did


class InFiles(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="spec-files-"))
        self.j = journal.Journal(os.path.join(self.dir, "journal.jsonl"))
        self.did = spec_journal(self.j, state=self.dir)
        self.files = Files(config=lambda: {}, state_dir=self.dir)
        self.files.spec_wait = 0
        patch = mock.patch.object(files_server, "STATE_TTL", 0)   # the steps below change the journal and read at once
        patch.start()
        self.addCleanup(patch.stop)
        self.server = self.files.serve(port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        platforms.current.rmtree(self.dir)

    def req(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, json.dumps(body) if body is not None else None,
                  {"Host": f"127.0.0.1:{self.port}", "X-Hub": "1", "Content-Type": "application/json"})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, json.loads(data or b"null")

    def test_the_specs_root_and_the_strip(self):
        code, roots = self.req("GET", "/api/roots")
        self.assertIn("specs", [r["name"] for r in roots])
        code, f = self.req("GET", f"/api/read?root=specs&path={self.did}/spec.md")
        self.assertEqual(code, 200)
        self.assertEqual((f["spec"]["task"], f["spec"]["executor"], f["spec"]["state"], f["spec"]["ready"]),
                         (self.did, "maps-1", "open", True))
        self.assertEqual(f["spec"]["sha256"], hashlib.sha256(b"# Export\n\nThree slices.\n").hexdigest())
        self.assertNotIn("spec", self.req("GET", "/api/roots")[1][0])

    def test_the_strip_waits_for_the_decision_only_between_the_executors_turns(self):
        """While the executor is in a turn (the discussion), the strip says it is writing and /api/stat says so too."""
        path = f"root=specs&path={self.did}/spec.md"
        self.assertTrue(self.req("GET", f"/api/stat?{path}")[1]["spec_ready"])
        self.j.append("message.received", None, {"to": "maps-1", "text": "why two slices?", **WEB})
        self.j.append("turn.started", "maps-1", {"marker": "m99"})
        self.assertFalse(self.req("GET", f"/api/stat?{path}")[1]["spec_ready"])
        self.assertFalse(self.req("GET", f"/api/read?{path}")[1]["spec"]["ready"])
        self.j.append("turn.result", "maps-1", {"text": "Because of the migration."})
        self.j.append("turn.stopped", "maps-1", {})
        self.assertTrue(self.req("GET", f"/api/read?{path}")[1]["spec"]["ready"])

    def test_comments_on_the_spec_go_to_its_executor_first(self):
        path = os.path.join(self.dir, "specs", self.did, "spec.md")
        code, ss = self.req("GET", "/api/sessions?path=" + path)
        self.assertEqual(ss[0]["name"], "maps-1")

    def test_accept_takes_the_file_as_it_is_at_the_press_even_if_it_changed_after_the_card(self):
        # owner 2026-11-10: a press is "I have read it" — no refusal because the file moved on since the card was drawn
        code, f = self.req("GET", f"/api/read?root=specs&path={self.did}/spec.md")
        with open(os.path.join(self.dir, "specs", self.did, "spec.md"), "a", encoding="utf-8", newline="\n") as out:
            out.write("one more line\n")
        code, got = self.req("POST", "/api/spec", {"delegation": self.did, "action": "accept", "sha256": f["spec"]["sha256"]})
        self.assertEqual(code, 202)
        self.assertNotIn("changed", got)
        ((_, ev),) = spool.take(os.path.join(self.dir, "spool"))
        self.assertEqual((ev["type"], ev["entity"], ev["data"]["owner"]), ("delegation.spec_accepted", self.did, True))
        with open(os.path.join(self.dir, *ev["data"]["pending"].split("/")), "rb") as copy:   # its bytes: no \r\n
            data = copy.read()
        self.assertTrue(data.endswith(b"one more line\n"))   # the copy is the new version
        self.assertEqual(ev["data"]["sha256"], hashlib.sha256(data).hexdigest())

    def test_accept_without_a_sum_works_too(self):
        code, got = self.req("POST", "/api/spec", {"delegation": self.did, "action": "accept"})
        self.assertEqual(code, 202)
        ((_, ev),) = spool.take(os.path.join(self.dir, "spool"))
        self.assertEqual(len(ev["data"]["sha256"]), 64)


class InTelegram(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="spec-tg-")
        self.state = os.path.join(self.dir, "state")
        self.old_cfg = os.environ.get("HUB_TELEGRAM_CONFIG")
        os.environ["HUB_TELEGRAM_CONFIG"] = os.path.join(self.dir, "telegram.json")
        tg.save_config({"token": "1:x", "owners": [OWNER]})
        self.fake = FakeBotApi()
        self.j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        self.j.append("session.requested", "hub", {"cwd": "/w", "session_id": "h", "kind": "hub"})
        self.j.append("session.started", "hub", {"session_id": "h"})
        self.bot = tg.Bot(self.state, tg.Api("1:x", base=self.fake.base))
        self.bot.spec_wait = 0
        self.bot.catch_up()

    def tearDown(self):
        self.fake.server.shutdown()
        self.fake.server.server_close()
        if self.old_cfg is None:
            os.environ.pop("HUB_TELEGRAM_CONFIG", None)
        else:
            os.environ["HUB_TELEGRAM_CONFIG"] = self.old_cfg
        platforms.current.rmtree(self.dir)

    def spooled(self):
        d = os.path.join(self.state, "spool")
        out = [e for _, e in spool.take(d)]
        for n in os.listdir(d) if os.path.isdir(d) else []:
            os.unlink(os.path.join(d, n))
        return out

    def poll(self, update):
        self.fake.updates = [update]
        self.bot.poll(timeout=0)

    def press(self, button, uid=70):
        self.poll({"update_id": uid, "callback_query": {"id": f"c{uid}", "from": {"id": OWNER},
                                                       "data": button["callback_data"]}})

    def test_spec_lists_sessions_and_a_press_turns_specs_on(self):
        self.j.append("session.requested", "maps", {"cwd": "/w", "session_id": "s-maps", "role": "agent"})
        self.j.append("session.started", "maps", {"session_id": "s-maps"})
        self.bot.tick()
        self.fake.calls.clear()
        self.poll({"update_id": 60, "message": {"message_id": 60, "from": {"id": OWNER},
                                                 "chat": {"id": OWNER, "type": "private"}, "text": "/spec"}})
        rows = self.fake.sent()[-1]["reply_markup"]["inline_keyboard"]
        (row,) = [r for r in rows if "maps" in r[0]["text"]]
        self.press(row[0])
        (ev,) = self.spooled()
        self.assertEqual((ev["type"], ev["entity"], ev["data"]["on"], ev["data"]["channel"]),
                         ("session.spec_set", "maps", True, "telegram"))

    def test_spec_on_by_name_says_what_the_core_decided(self):
        self.j.append("session.requested", "maps", {"cwd": "/w", "session_id": "s-maps", "role": "agent"})
        self.bot.tick()
        self.fake.calls.clear()
        self.poll({"update_id": 61, "message": {"message_id": 61, "from": {"id": OWNER},
                                                 "chat": {"id": OWNER, "type": "private"}, "text": "/spec maps on"}})
        (ev,) = self.spooled()
        self.assertEqual((ev["type"], ev["entity"], ev["data"]["on"]), ("session.spec_set", "maps", True))
        self.assertIn("not recorded it yet", self.fake.sent()[-1]["text"])   # no hubd here: sent, not "on"

    def test_a_ready_spec_comes_as_a_file_with_accept_and_drop(self):
        did = spec_journal(self.j, state=self.state)
        self.bot.tick()
        self.assertTrue(self.fake.sent("sendDocument"))
        card = self.fake.sent()[-1]
        self.assertIn(did, card["text"])
        accept, drop = card["reply_markup"]["inline_keyboard"][0]
        self.fake.calls.clear()
        self.press(accept)
        (ev,) = self.spooled()
        self.assertEqual((ev["type"], ev["entity"], ev["data"]["channel"]), ("delegation.spec_accepted", did, "telegram"))
        self.assertEqual(ev["data"]["sha256"], hashlib.sha256(b"# Export\n\nThree slices.\n").hexdigest())

    def test_words_in_reply_to_the_card_name_its_spec_and_whoever_raised_it_accepts_by_them(self):
        # a colleague's hub: he answered the card with words, not a button; they went to the executor, and the raiser's
        # hub task spec accept dN --for mN was refused — nothing tied the words to the card
        did = spec_journal(self.j, state=self.state)
        self.bot.tick()
        card = self.fake.sent()[-1]
        self.assertIn("reply_markup", card)
        self.poll({"update_id": 72, "message": {"message_id": 72, "from": {"id": OWNER},
                                                 "chat": {"id": OWNER, "type": "private"}, "text": "all yes, except X",
                                                 "reply_to_message": {"message_id": self.bot.st["cards"][f"spec:{did}"],
                                                                      "text": card["text"]}}})
        (ev,) = self.spooled()
        self.assertEqual((ev["type"], ev["data"]["to"], ev["data"]["spec_card"]), ("message.received", "maps-1", did))
        word = f"m{self.j.append(ev['type'], ev['entity'], ev['data'])['seq']}"
        self.j.append("delegation.spec_accepted", did, {"sha256": "ab" * 32, "for": word, **by("maps"),
                                                        "pending": f"specs/{did}/.accepted-abababababab-00000000.md"})
        self.bot.tick()
        self.assertEqual(self.bot.reader.state["messages"][word]["session"], "maps-1")
        self.assertEqual(self.bot.reader.state["delegations"][did]["spec"]["by"], "maps")

    def test_a_reply_to_the_spec_file_names_its_spec_too(self):
        self.fake.results["sendDocument"] = lambda body: {"message_id": 555}
        did = spec_journal(self.j, state=self.state)
        self.bot.tick()
        self.assertTrue(self.fake.sent("sendDocument"))
        self.poll({"update_id": 73, "message": {"message_id": 73, "from": {"id": OWNER},
                                                 "chat": {"id": OWNER, "type": "private"}, "text": "drop it",
                                                 "reply_to_message": {"message_id": 555, "caption": tg.i18n.t(
                                                     "telegram.spec_ready", id=did, name="maps-1")}}})
        (ev,) = self.spooled()
        self.assertEqual((ev["data"]["to"], ev["data"]["spec_card"]), ("maps-1", did))
        self.assertNotIn("quote", ev["data"])   # the bot's own heading is no quote

    def test_a_press_whose_answer_fails_on_the_network_does_not_stop_the_bot(self):
        # release review 2026-11-09: a transient TgError from on_callback left poll() and serve() (which catch nothing) —
        # the bot process died, the press already read
        from unittest import mock
        did = spec_journal(self.j, state=self.state)
        self.bot.tick()
        accept = self.fake.sent()[-1]["reply_markup"]["inline_keyboard"][0][0]
        with mock.patch.object(self.bot, "on_callback", side_effect=tg.TgError("network", transient=True)):
            self.press(accept, uid=71)
        self.assertEqual(self.bot.st["offset"], 72)
        self.assertTrue(did)

    def test_no_card_while_the_executor_is_busy_with_its_own_job_one_after_it_and_not_one_per_result(self):
        """Owner decision 2026-11-09 (core/specmode.awaiting): a result with a job of its own still running brings no card;
        the result after it brings one, and the next results of the discussion do not bring another."""
        did = spec_journal(self.j, state=self.state, background=True)
        self.bot.tick()
        self.assertFalse(self.fake.sent("sendDocument"))
        self.assertFalse([m for m in self.fake.sent() if "reply_markup" in m])
        self.j.append("turn.started", "maps-1", {"marker": "m99"})
        self.j.append("session.background", "maps-1", {"tasks": []})
        self.j.append("turn.result", "maps-1", {"text": "Rendered; the spec is ready."})
        self.j.append("turn.stopped", "maps-1", {})
        self.bot.tick()
        self.assertEqual(len(self.fake.sent("sendDocument")), 1)
        self.assertIn(did, self.fake.sent()[-1]["text"])
        self.fake.calls.clear()
        self.j.append("turn.started", "maps-1", {"marker": "m100"})
        self.j.append("turn.result", "maps-1", {"text": "Added the case."})
        self.j.append("turn.stopped", "maps-1", {})
        self.bot.tick()
        self.assertFalse(self.fake.sent("sendDocument"))

    def test_accepted_elsewhere_closes_the_card(self):
        did = spec_journal(self.j, state=self.state)
        self.bot.tick()
        self.fake.calls.clear()
        self.j.append("delegation.spec_accepted", did, {"sha256": "ab" * 32, "pending": f"specs/{did}/.accepted-abababababab-00000000.md",
                                                        **WEB})
        self.bot.tick()
        (edit,) = self.fake.sent("editMessageText")
        self.assertIn(f"Spec {did} accepted", edit["text"])

    def test_a_press_takes_a_spec_that_changed_after_the_card(self):
        did = spec_journal(self.j, state=self.state)
        self.bot.tick()
        accept = self.fake.sent()[-1]["reply_markup"]["inline_keyboard"][0][0]
        with open(os.path.join(self.state, "specs", did, "spec.md"), "a", encoding="utf-8") as f:
            f.write("edited\n")
        self.fake.calls.clear()
        self.press(accept)
        (ev,) = self.spooled()
        self.assertEqual((ev["type"], ev["entity"], ev["data"]["channel"]), ("delegation.spec_accepted", did, "telegram"))
        with open(os.path.join(self.state, *ev["data"]["pending"].split("/")), encoding="utf-8") as copy:
            self.assertTrue(copy.read().endswith("edited\n"))   # the current text
        self.assertFalse(self.fake.sent("sendDocument"))   # no second card


if __name__ == "__main__":
    unittest.main()
