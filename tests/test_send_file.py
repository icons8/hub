"""`hub send-file` — a file to the owner (a "looks like this" screenshot, a video, a document). Owner decision
2026-10-06: check that screenshots can be forwarded over the buses — to Telegram, to plugins and to the page.
Telegram sent the picture as a document, while the page feed and plugins did not see it at all — only the command
line. Now the file is a feed entry of whoever sent it."""
import tests  # noqa: F401 — hide the machine's settings
import json
import os
import shutil
import subprocess
import sys
import unittest
from types import SimpleNamespace

from core import reducer, spool, views
from modules.web import attach

HUB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")

OWNER = {"channel": "web", "owner": True}


class SentFileInTheFeed(unittest.TestCase):
    def setUp(self):
        self.r, self.seq = views.Reader(), 0
        for n, kind in (("hub", "hub"), ("maps", None)):
            self.ev("session.requested", n, cwd="/w", session_id=f"s-{n}", **({"kind": kind} if kind else {}), **OWNER)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.r.feed({"seq": self.seq, "ts": 1000 + self.seq, "type": type, "entity": entity, "data": data})

    def test_a_screenshot_from_a_session_is_an_item_of_its_feed(self):
        self.ev("owner.file", None, path="/w/shots/map.png", caption="the map — looks like this", channel="session",
                owner=False, sender="maps")
        (it,) = [x for x in self.r.timelines.get("maps") or [] if x["kind"] == "file"]
        self.assertEqual((it["path"], it["caption"], it["name"], it["image"]),
                         ("/w/shots/map.png", "the map — looks like this", "map.png", True))
        self.assertFalse([x for x in self.r.timelines.get("hub") or [] if x["kind"] == "file"])

    def test_a_file_from_a_schedule_job_is_also_a_row_of_the_hub_feed(self):
        # 20.11: a command job of a sleeping session sent a file: it landed only in that session's feed — no turn, no
        # mark, nobody to read it. A job's file is the owner's news like `hub notify` from a job: the hub feed has it
        # too, marked whose it is (the "sent you by" line)
        self.ev("owner.file", None, path="/w/invoice.xlsx", caption="ready", channel="session", owner=False,
                sender="maps", schedule_job="monthly")
        (own,) = [x for x in self.r.timelines.get("maps") or [] if x["kind"] == "file"]
        (row,) = [x for x in self.r.timelines.get("hub") or [] if x["kind"] == "file"]
        self.assertEqual((own["path"], own.get("by")), ("/w/invoice.xlsx", None))
        self.assertEqual((row["path"], row["caption"], row["by"], row["name"]), ("/w/invoice.xlsx", "ready", "maps", "invoice.xlsx"))

    def test_a_file_of_the_hubs_own_job_is_one_row(self):
        self.ev("owner.file", None, path="/w/a.pdf", channel="session", owner=False, sender="hub", schedule_job="tidy")
        self.assertEqual(len([x for x in self.r.timelines.get("hub") or [] if x["kind"] == "file"]), 1)

    def test_a_file_from_outside_a_session_goes_to_the_hub_feed(self):
        self.ev("owner.file", None, path="/w/report.pdf", caption=None, **OWNER)
        (it,) = [x for x in self.r.timelines.get("hub") or [] if x["kind"] == "file"]
        self.assertEqual((it["name"], it["image"]), ("report.pdf", False))

    def test_a_windows_absolute_path_is_accepted(self):
        # 2026-10-09: on Windows hub send-file was always rejected — the check was path.startswith("/")
        self.ev("owner.file", None, path="C:" + chr(92) + "w" + chr(92) + "report.pdf", caption=None, **OWNER)
        (it,) = [x for x in self.r.timelines.get("hub") or [] if x["kind"] == "file"]
        self.assertEqual(it["name"], "report.pdf")   # the name is what follows the last separator, not the whole path

    def test_absolute_paths_are_judged_by_text_not_by_the_python_version(self):
        from core import reducer
        for ok in ("/w/a.pdf", "C:" + chr(92) + "w", "c:/w", chr(92) + "x" + chr(92) + "a.pdf", chr(92) * 2 + "srv" + chr(92) + "a"):
            self.assertTrue(reducer.is_abs_path(ok), ok)
        for bad in ("", "shots/map.png", "C:map.png", "map.png"):
            self.assertFalse(reducer.is_abs_path(bad), bad)

    def test_a_rejected_file_is_not_shown(self):
        self.ev("owner.file", None, path="shots/map.png", channel="session", owner=False, sender="maps")
        self.assertFalse([x for x in self.r.timelines.get("maps") or [] if x["kind"] == "file"])


class SentFileAfterRotation(unittest.TestCase):
    """2026-10-22: a session sent four screenshots, and 19 s later the journal went to the archive — the page rebuilt the
    feeds from the new journal and the screenshots landed in the hub's feed: the carried-over tail is laid on the
    snapshot before the tail, and a session born after it is not there. The owner was looking at the session feed
    and wrote "I don't see it" twice (the file did not go to Telegram — they were writing from the page)."""

    def test_a_file_of_a_session_born_after_the_base_stays_in_its_feed(self):
        import tempfile
        from core import journal
        d = tempfile.mkdtemp(prefix="send-rot-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        sess = lambda n, **k: ("session.requested", n, {"cwd": "/w", "session_id": f"s-{n}", **k, **OWNER})  # noqa: E731
        said = lambda n, t: ("session.said", n, {"text": t})  # noqa: E731
        events = [sess("hub", kind="hub"), sess("quiet"), sess("maps"), said("maps", "one"), said("maps", "two"),
                  ("owner.file", None, {"path": "/w/shots/map.png", "caption": None, "channel": "session",
                                        "owner": False, "sender": "maps"}),
                  said("hub", "done")]
        events = [{"seq": i, "ts": 1000 + i, "src": "x", "type": t, "entity": e, "data": data}
                  for i, (t, e, data) in enumerate(events, 1)]
        path = os.path.join(d, "journal.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
        # the tail: the last two events and one per session — the quiet one pulls the snapshot back to the very start
        journal.rotate(d, reducer.replay(events), keep=2, per_session=1)
        first = journal.read(path)[0]["data"]
        self.assertNotIn("maps", first["state"]["sessions"])   # the case's condition: the session is not in the snapshot before the tail
        r = views.Reader()
        r.feed_all(journal.read(path))
        self.assertEqual([x["path"] for x in r.timelines.get("maps") or [] if x["kind"] == "file"], ["/w/shots/map.png"])
        self.assertFalse([x for x in r.timelines.get("hub") or [] if x["kind"] == "file"])


class ResendKeepsQuote(unittest.TestCase):
    def test_feed_item_of_an_owner_message_keeps_quote_reply_and_wake(self):
        # PR #33: "Send again" took the quote and the reply from the snapshot, but they are not there — the resend
        # went out without them
        r = views.Reader()
        for i, (t, e, d) in enumerate([("session.requested", "maps", {"cwd": "/w", "session_id": "s", **OWNER}),
                                       ("message.received", None, {"to": "maps", "text": "yes, this one", "quote": "Option 2",
                                                                    "reply_to": "d7", "wake": True, **OWNER})], 1):
            r.feed({"seq": i, "ts": 1000 + i, "type": t, "entity": e, "data": d})
        (it,) = [x for x in r.timelines["maps"] if x["kind"] == "in"]
        self.assertEqual((it["quote"], it["reply_to"], it["wake"]), ("Option 2", "d7", True))


class SentFileIsKept(unittest.TestCase):
    """2026-10-19: a session sent a screenshot from /tmp and deleted it — a broken link on the page and in the feed: the
    event carried only the path. Now hub send-file puts a copy into attachments (attachments/, like the owner's
    files), and the event points to it."""

    def setUp(self):
        import json
        import tempfile
        self.state = tempfile.mkdtemp(prefix="send-file-")
        self.src_dir = tempfile.mkdtemp(prefix="send-src-")
        self.cfg = os.path.join(self.state, "hub.json")
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump({}, f)
        self.env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_CONFIG": self.cfg, "HUB_CLI_WAIT": "0",
                    "HUB_SESSION": "maps"}
        for k in ("HUB_EXTENSION", "HUB_MODULE", "HUB_WEB_PORT"):
            self.env.pop(k, None)
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.src_dir, ignore_errors=True)

    def send(self, path, *caption, cmd="send-file"):
        r = subprocess.run([sys.executable, HUB, cmd, path, *caption], env=self.env, capture_output=True,
                           text=True, encoding="utf-8", timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        (ev,) = [ev for p, ev in spool.take(os.path.join(self.state, "spool")) if not os.unlink(p)]
        self.assertEqual(ev["type"], "owner.file")
        return ev["data"], r

    def send_many(self, *args):
        """`hub send-file <args>` -> the owner.file events in the order they went into the spool."""
        r = subprocess.run([sys.executable, HUB, "send-file", *args], env=self.env, capture_output=True,
                           text=True, encoding="utf-8", timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        evs = sorted(spool.take(os.path.join(self.state, "spool")), key=lambda pe: os.path.basename(pe[0]))
        for p, _ in evs:
            os.unlink(p)
        self.assertEqual({ev["type"] for _, ev in evs}, {"owner.file"})
        return [ev["data"] for _, ev in evs], r

    def test_several_files_in_one_command_are_all_sent(self):
        # 2026-10-30: `hub send-file a.png b.png c.png` sent only a.png, the other names became its caption
        names = ["a.png", "b.png", "c.png"]
        got, _ = self.send_many(*[self.file(n) for n in names])
        self.assertEqual([d["source"].rsplit(os.sep, 1)[-1] for d in got], names)
        self.assertEqual([d["caption"] for d in got], [None, None, None])
        self.assertEqual(len({d["path"] for d in got}), 3)

    def test_the_text_after_the_files_is_the_caption_of_the_last_one(self):
        a, b = self.file("a.png"), self.file("b.png")
        got, _ = self.send_many(a, b, "both", "options")
        self.assertEqual([d["caption"] for d in got], [None, "both options"])

    def test_a_caption_that_looks_like_a_missing_file_stays_a_caption(self):
        a = self.file("a.png")
        got, _ = self.send_many(a, "missing.png")
        self.assertEqual([d["caption"] for d in got], ["missing.png"])
        got, _ = self.send_many(a, os.path.join(self.src_dir, "gone.png"), "caption")
        self.assertEqual([d["caption"] for d in got], [os.path.join(self.src_dir, "gone.png") + " caption"])

    def test_the_first_path_must_exist_even_with_several(self):
        r = subprocess.run([sys.executable, HUB, "send-file", os.path.join(self.src_dir, "no.png"), self.file("b.png")],
                           env=self.env, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(list(spool.take(os.path.join(self.state, "spool"))), [])

    def test_every_file_of_a_batch_reaches_the_feed_and_telegram(self):
        a, b = self.file("a.png"), self.file("b.png", b"\x89PNG\r\n\x1a\n" + b"1" * 64)
        got, _ = self.send_many(a, b)
        r = views.Reader()
        r.feed({"seq": 1, "ts": 1, "type": "session.requested", "entity": "maps",
                "data": {"cwd": "/w", "session_id": "s", **OWNER}})
        for i, d in enumerate(got, 2):
            r.feed({"seq": i, "ts": i, "type": "owner.file", "entity": None,
                    "data": {**d, "channel": "session", "owner": False, "sender": "maps"}})
        items = [x for x in r.timelines["maps"] if x["kind"] == "file"]
        self.assertEqual([x["name"] for x in items], ["a.png", "b.png"])

    def file(self, name, data=b"\x89PNG\r\n\x1a\n" + b"0" * 64):
        p = os.path.join(self.src_dir, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_send_rich_is_a_file_on_the_page_and_a_rich_report_for_telegram(self):
        # a report with pictures: the page gets the .md itself (a copy, as send-file), Telegram a rich message;
        # the pictures are taken by paths from the source, so it is in the event
        src = self.file("week.md", "## Total\n\n![](a.png)\n".encode())
        data, _ = self.send(src, "--title", "Week", cmd="send-rich")
        self.assertEqual((data["rich"], data["title"], data["caption"], data["source"]), (True, "Week", "Week", src))
        data, _ = self.send(src, cmd="send-rich")
        self.assertEqual((data["rich"], data["title"]), (True, None))

    def test_send_rich_keeps_copies_of_the_pictures_of_the_report(self):
        pic = self.file("a.png")
        src = self.file("week.md", b"## Total\n\n![](a.png)\n\n![](a.png)\n![](gone.png)\n")
        data, _ = self.send(src, "--title", "Week", cmd="send-rich")
        (orig, kept), = data["media"].items()
        self.assertEqual(orig, pic)
        self.assertTrue(attach.inside(self.state, kept))
        self.assertNotEqual(kept, pic)
        os.unlink(pic)
        self.assertTrue(os.path.isfile(kept))

    def test_send_rich_without_a_file_is_the_usage_not_a_traceback(self):
        r = subprocess.run([sys.executable, HUB, "send-rich", "--title", "Week"], env=self.env, capture_output=True,
                           text=True, encoding="utf-8", timeout=30)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("send-rich", r.stderr)
        self.assertEqual(list(spool.take(os.path.join(self.state, "spool"))), [])

    def test_send_rich_of_a_non_report_is_an_ordinary_file(self):
        src = self.file("data.pdf", b"%PDF-1")
        data, _ = self.send(src, cmd="send-rich")
        self.assertNotIn("rich", data)
        self.assertNotIn("media", data)

    def test_the_file_survives_the_source_being_deleted(self):
        src = self.file("map (1).png")
        data, _ = self.send(src, "map")
        os.unlink(src)
        root = os.path.realpath(attach.root(self.state))
        self.assertTrue(os.path.realpath(data["path"]).startswith(root + os.sep), data)
        with open(data["path"], "rb") as f:
            self.assertTrue(f.read().startswith(b"\x89PNG"))
        self.assertEqual((data["source"], data["caption"]), (src, "map"))
        # the page serves the copy (the attachment), the bot sends the same one — the path from the event
        from modules.web.server import Web
        self.assertEqual(Web.own_file(SimpleNamespace(state_dir=self.state), data["path"]),
                         os.path.realpath(data["path"]))
        # the feed shows the name the session gave it, not the sanitized name of the copy
        r = views.Reader()
        r.feed({"seq": 1, "ts": 1, "type": "session.requested", "entity": "maps",
                "data": {"cwd": "/w", "session_id": "s", **OWNER}})
        r.feed({"seq": 2, "ts": 2, "type": "owner.file", "entity": None,
                "data": {**data, "channel": "session", "owner": False, "sender": "maps"}})
        (it,) = [x for x in r.timelines["maps"] if x["kind"] == "file"]
        self.assertEqual((it["name"], it["path"], it["image"]), ("map (1).png", data["path"], True))
        self.assertEqual(it["source"], src)   # the page opens the document in "Files" as source, with comments

    def test_a_file_already_among_attachments_is_not_copied_again(self):
        own = attach.new_path(self.state, "plan.pdf")
        with open(own, "wb") as f:
            f.write(b"%PDF")
        data, _ = self.send(own)
        self.assertEqual(data["path"], own)
        self.assertNotIn("source", data)
        self.assertEqual(len(os.listdir(attach.root(self.state))), 1)

    def test_a_file_over_the_attachment_limit_stays_where_it_is(self):
        import json
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump({"web": {"upload_max_mb": 1}}, f)
        src = self.file("long.mp4", b"0" * (1 << 20) + b"1")
        data, r = self.send(src)
        self.assertEqual(data["path"], src)
        self.assertNotIn("source", data)
        self.assertFalse(os.path.isdir(attach.root(self.state)) and os.listdir(attach.root(self.state)))
        self.assertIn("do not delete it", r.stdout + r.stderr)

    def test_a_screen_capture_from_another_tool_is_marked_as_a_screenshot(self):
        # a screen capture made by a browser tool or a simulator, not by hub shot: the file itself says nothing, so the
        # sender says it — a file index then sorts it with the other screenshots, not with generated pictures
        a, b = self.file("login.png"), self.file("menu.png")
        got, _ = self.send_many("--screenshot", a, "login", "screen")
        self.assertEqual([(d.get("screenshot"), d["caption"]) for d in got], [(True, "login screen")])
        got, _ = self.send_many(a, b, "--screenshot")   # the flag after the files is a flag too, not the caption
        self.assertEqual([(d.get("screenshot"), d["caption"]) for d in got], [(True, None), (True, None)])
        got, _ = self.send_many(a, "chart")
        self.assertEqual([("screenshot" in d, d["caption"]) for d in got], [(False, "chart")])

    def test_the_screenshot_flag_alone_is_not_a_file(self):
        r = subprocess.run([sys.executable, HUB, "send-file", "--screenshot"], env=self.env, capture_output=True,
                           text=True, encoding="utf-8", timeout=30)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(list(spool.take(os.path.join(self.state, "spool"))), [])

    def test_a_report_is_not_a_screenshot(self):
        # send-rich sends a .md/.html report: it has no such flag, wherever it stands
        report = self.file("report.md")
        for args in (["--screenshot", report], [report, "--screenshot"]):
            r = subprocess.run([sys.executable, HUB, "send-rich", *args], env=self.env, capture_output=True,
                               text=True, encoding="utf-8", timeout=30)
            self.assertNotEqual(r.returncode, 0, args)
            self.assertIn("--screenshot", r.stdout + r.stderr, args)
        self.assertEqual(list(spool.take(os.path.join(self.state, "spool"))), [])


if __name__ == "__main__":
    unittest.main()
