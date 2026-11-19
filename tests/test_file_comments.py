"""File comments in Files, as in Google Docs (owner decision 2026-10-01: select passages and comment on them; no "ask to
rewrite" buttons — a question is simply answered in the comments, and if a rewrite is needed the session asks there;
it is the same chat, merely mirrored onto the file's page).
A comment is a thread on a piece of the file: the owner's message to the session (an ordinary one, into its chat) and
its turn result to it — the reply under the comment. The thread is run by Files: its own threads file, the replies
come from the core's journal."""
import tests  # noqa: F401 — don't see the machine's settings
import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
from urllib.parse import quote

from core import spool
from modules.files.server import Files

OWNER = {"channel": "cli", "owner": True}


class Comments(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="fcom-"))
        self.proj = os.path.join(self.dir, "proj")
        os.makedirs(os.path.join(self.proj, "docs"))
        self.spec = os.path.join(self.proj, "docs", "spec.md")
        with open(self.spec, "w") as f:
            f.write("# Spec\n\nThe button is green.\n")
        self.seq = 0
        self.journal([("session.requested", "hub", {"cwd": "/h", "session_id": "s-h", "kind": "hub", **OWNER}),
                      ("session.requested", "spec-writer", {"cwd": self.proj, "session_id": "s1", **OWNER}),
                      ("session.requested", "other", {"cwd": "/elsewhere", "session_id": "s2", **OWNER}),
                      ("session.started", "spec-writer", {})])
        self.files = Files(config=lambda: {"projects": {"proj": {"path": self.proj}}}, state_dir=self.dir)
        self.server = self.files.serve(port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def journal(self, events):
        with open(os.path.join(self.dir, "journal.jsonl"), "a") as f:
            for t, name, d in events:
                self.seq += 1
                f.write(json.dumps({"seq": self.seq, "ts": 100 + self.seq, "type": t, "entity": name, "data": d,
                                    "src": "t"}, ensure_ascii=False) + "\n")

    def deliver(self):
        """Like the core: mailbox → journal (message.received with a number), the session takes the message and replies with a result."""
        out = []
        for path, ev in spool.take(os.path.join(self.dir, "spool")):
            os.unlink(path)
            self.journal([(ev["type"], ev["entity"], ev["data"])])
            out.append((f"m{self.seq}", ev))
        return out

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": f"127.0.0.1:{self.port}", "X-Hub": "1", "Content-Type": "application/json", **(headers or {})}
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        data = r.read()
        c.close()
        loc = r.getheader("Location")
        return r.status, (json.loads(data) if r.getheader("Content-Type", "").startswith("application/json") else loc)

    def threads(self):
        return self.req("GET", "/api/comments?root=proj&path=docs/spec.md")[1]

    def test_open_carries_the_session(self):
        code, loc = self.req("GET", "/open?path=" + quote(self.spec) + "&session=spec-writer&line=3")
        self.assertEqual((code, loc), (302, "/#/proj/docs/spec.md?line=3&session=spec-writer"))

    def test_sessions_working_on_the_file_come_first(self):
        code, ss = self.req("GET", "/api/sessions?path=" + quote(self.spec))
        self.assertEqual([s["name"] for s in ss][:2], ["spec-writer", "hub"])
        self.assertTrue(ss[0]["here"])

    def test_bad_fields_get_an_error_not_a_dropped_connection(self):
        # a JSON list reached body.get(), and int() on nth and line ("x", "²" passes isdigit) was unguarded:
        # the handler crashed and the connection dropped without a reply
        for body in ([1], 5, "text"):
            code, out = self.req("POST", "/api/comment", body)
            self.assertEqual(code, 400, body)
            self.assertIn("error", out)
        base = {"session": "spec-writer", "root": "proj", "path": "docs/spec.md", "quote": "green", "text": "why?"}
        for extra in ({"nth": "x"}, {"nth": [1]}, {"line": "²"}, {"line": "3.5"}, {"line": 3}, {"nth": True}):
            code, t = self.req("POST", "/api/comment", {**base, **extra})
            self.assertEqual(code, 202, extra)
            self.assertIs(type(t["nth"]), int)
            self.assertIn(t["line"], (None, 3))

    def test_comment_is_a_thread_and_the_sessions_turn_is_the_reply(self):
        code, t = self.req("POST", "/api/comment", {"session": "spec-writer", "root": "proj", "path": "docs/spec.md",
                                                    "quote": "The button is green.", "line": 3, "nth": 0,
                                                    "text": "why is it green?"})
        self.assertEqual(code, 202)
        ((mid, ev),) = self.deliver()
        self.assertEqual((ev["type"], ev["data"]["to"], ev["data"]["owner"], ev["data"]["comment"]),
                         ("message.received", "spec-writer", True, t["id"]))
        text = ev["data"]["text"]
        self.assertIn(self.spec, text)
        self.assertIn('"The button is green." (line 3)', text)
        self.assertIn("why is it green?", text)
        self.assertIn("ask here", text)                     # not sure it is a command — ask in the thread
        # decision of 2026-10-10: file comments are just comments: answer or fix this file (a clear command — at once),
        # don't start work wider than the file from a thread — that is set in the session's chat
        for rule in ("edit this file at once", "wider than this file", "in the session's chat"):
            self.assertIn(rule, text)
        (th,) = self.threads()
        self.assertEqual((th["quote"], th["session"], [m["who"] for m in th["messages"]]),
                         ("The button is green.", "spec-writer", ["owner"]))
        # the session took the message and replied with a turn result — the reply goes under the comment
        self.journal([("turn.started", "spec-writer", {"marker": mid, "prompt_head": "x"}),
                      ("session.said", "spec-writer", {"text": "in between"}),
                      ("turn.result", "spec-writer", {"text": "By the guide it is blue. Rewrite?"})])
        (th,) = self.threads()
        self.assertEqual([(m["who"], m["text"]) for m in th["messages"]],
                         [("owner", "why is it green?"), ("spec-writer", "By the guide it is blue. Rewrite?")])
        # the owner's reply in the thread goes to the same place, the same session
        self.req("POST", "/api/comment", {"thread": th["id"], "root": "proj", "path": "docs/spec.md", "text": "yes"})
        ((mid2, ev2),) = self.deliver()
        self.assertEqual((ev2["data"]["to"], ev2["data"]["comment"]), ("spec-writer", th["id"]))
        self.assertIn("wider than this file", ev2["data"]["text"])   # and the reply in the thread carries the same rule
        self.journal([("turn.started", "spec-writer", {"marker": mid2, "prompt_head": "x"}),
                      ("turn.result", "spec-writer", {"text": "Rewrote line 3."})])
        (th,) = self.threads()
        self.assertEqual([m["who"] for m in th["messages"]], ["owner", "spec-writer", "owner", "spec-writer"])
        # close the thread
        self.assertEqual(self.req("POST", "/api/comment/resolve", {"thread": th["id"], "root": "proj",
                                                                   "path": "docs/spec.md"})[0], 200)
        self.assertTrue(self.threads()[0]["resolved"])

    def test_the_reply_arrives_even_after_the_journal_was_rotated(self):
        """The journal was started anew (journal.rotate: a new file, tail and snapshot — it is shorter than before): Files
        reads it from the start, not from the old position — otherwise session replies into threads vanished silently
        (2026-10-12: none after a rotation)."""
        self.journal([("session.said", "other", {"text": "x" * 200})] * 200)   # a long old journal
        self.req("POST", "/api/comment", {"session": "spec-writer", "root": "proj", "path": "docs/spec.md",
                                          "quote": "The button is green.", "line": 3, "nth": 0, "text": "why is it green?"})
        ((mid, ev),) = self.deliver()
        self.assertEqual(len(self.threads()), 1)               # Files read the journal to the end
        path = os.path.join(self.dir, "journal.jsonl")
        with open(path) as f:
            tail = [line for line in f if '"message.received"' in line]
        os.replace(path, path + ".old")                         # rotation: the old one goes to the archive, the new one is a different file
        with open(path, "w") as f:
            f.writelines(tail)
        self.assertLess(os.path.getsize(path), os.path.getsize(path + ".old") // 10)
        self.journal([("turn.started", "spec-writer", {"marker": mid, "prompt_head": "x"}),
                      ("turn.result", "spec-writer", {"text": "By the guide it is blue."})])
        (th,) = self.threads()
        self.assertEqual([(m["who"], m["text"]) for m in th["messages"]],
                         [("owner", "why is it green?"), ("spec-writer", "By the guide it is blue.")])

    def test_a_question_by_form_in_a_thread_turn_shows_in_the_thread(self):
        # 2026-10-10: a session did ask via a form in the middle of a thread's turn — the question is visible in the thread itself, not only as a card
        self.req("POST", "/api/comment", {"session": "spec-writer", "root": "proj", "path": "docs/spec.md",
                                          "quote": "x", "text": "redo it?"})
        ((mid, _),) = self.deliver()
        q = {"questions": [{"question": "Rewrite the button in blue?", "options": [{"label": "Yes"}, {"label": "No"}]}]}
        self.journal([("turn.started", "spec-writer", {"marker": mid, "prompt_head": "x"}),
                      ("permission.requested", "spec-writer", {"request_id": "r1", "tool": "AskUserQuestion",
                                                               "interactive": True, "input": q})])
        msgs = self.threads()[0]["messages"]
        self.assertEqual([m["who"] for m in msgs], ["owner", "spec-writer"])
        self.assertIn("Rewrite the button in blue?", msgs[-1]["text"])
        self.assertIn("Yes", msgs[-1]["text"])

    def test_other_turns_of_the_session_are_not_replies(self):
        self.req("POST", "/api/comment", {"session": "spec-writer", "root": "proj", "path": "docs/spec.md",
                                          "quote": "x", "text": "?"})
        self.deliver()
        self.journal([("message.received", None, {"to": "spec-writer", "text": "another", **OWNER}),
                      ("turn.started", "spec-writer", {"marker": f"m{self.seq + 1}", "prompt_head": "x"}),
                      ("turn.result", "spec-writer", {"text": "a reply to another"})])
        self.assertEqual([m["who"] for m in self.threads()[0]["messages"]], ["owner"])

    def test_validation(self):
        ok = {"session": "spec-writer", "root": "proj", "path": "docs/spec.md", "quote": "x", "text": "y"}
        for bad in ({**ok, "session": "nope"}, {**ok, "text": " "}, {**ok, "path": "../x"},
                    {"thread": "nope", "root": "proj", "path": "docs/spec.md", "text": "y"}):
            self.assertEqual(self.req("POST", "/api/comment", bad)[0], 400, bad)
        self.assertEqual(self.req("POST", "/api/comment", ok, headers={"X-Hub": "0"})[0], 403)
        self.assertEqual(self.deliver(), [])

    def test_stat_tells_the_file_changed(self):
        _, s1 = self.req("GET", "/api/stat?root=proj&path=docs/spec.md")
        with open(self.spec, "a") as f:
            f.write("One more line.\n")
        _, s2 = self.req("GET", "/api/stat?root=proj&path=docs/spec.md")
        self.assertNotEqual(s1, s2)


if __name__ == "__main__":
    unittest.main()
