"""What /api/file serves across the whole journal history (core/shown.py): archives by their parses beside them
(<archive>.shown.json), the current journal by reading on by bytes; the same rule for modules (hubmod.servable). 2026-10-17:
after rotation, what had been sent earlier answered 404."""
import tests  # noqa: F401 — hide the machine's settings
import json
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from core import journal, reducer, shown
from tests import winsafe


class PreviewPath(unittest.TestCase):
    def test_an_options_preview_is_a_picture_only_when_it_is_one_path_to_a_picture(self):
        # `preview` of a form option is an ASCII sketch or a path to a screenshot: the card and Telegram
        # tell them apart by this
        home = os.path.expanduser("~")
        for text, want in (("/tmp/a b.png", "/tmp/a b.png"), ("  /tmp/a.JPG\n", "/tmp/a.JPG"),
                           ("file:///tmp/a%20b.webp", "/tmp/a b.webp"), ("~/a.png", home + "/a.png"),
                           ("file:///C:/x/a.png", "C:/x/a.png"), ("/tmp/s.svg", "/tmp/s.svg")):
            self.assertEqual(shown.preview_image(text), want, text)
        for text in ("", "   ", None, 5, "a.png", "┌─┐\n│ │ /tmp/a.png\n└─┘", "/tmp/a.png\n/tmp/b.png", "/tmp/a.txt",
                     "https://x.test/a.png", "/" + "x" * 1100 + ".png"):
            self.assertIsNone(shown.preview_image(text), text)


class Index(unittest.TestCase):
    def setUp(self):
        self.state = os.path.realpath(tempfile.mkdtemp(prefix="hubshown-"))   # macOS: /var is a link to /private/var
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)
        self.j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        self.j.append("session.requested", "a", {"session_id": "s", "cwd": "/w"})

    def file(self, name, body=b"\x89PNG\r\n\x1a\n"):
        p = os.path.join(self.state, "files", name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(body)
        return p

    def sent(self, path):
        return self.j.append("owner.file", None, {"path": path, "caption": "", "channel": "session", "owner": False,
                                                  "sender": "a"})

    def rotate(self):
        journal.rotate(self.state, reducer.replay(journal.read(self.j.path)), keep=1, per_session=1)
        self.j = journal.Journal(os.path.join(self.state, "journal.jsonl"))

    def test_the_byte_scan_takes_what_the_rule_takes_and_skips_what_the_core_refused(self):
        pdf, png, refused = (self.file(n) for n in ("report «1».pdf", "a b.png", "no.pdf"))
        quoted = os.path.join(self.state, "files", 'x"y.webp')   # path only: on Windows a quote in a name is forbidden
        self.sent(pdf)
        # portable: only the text of a journal event; no file is opened through this URL
        self.j.append("session.said", "a", {"text": f"screenshot: file://{png.replace(' ', '%20')}. And {quoted}"})
        bad = self.sent(refused)
        self.j.append("core.rejected", None, {"of": bad["seq"], "reason": "x"})
        self.j.append("turn.tool", "a", {"id": "t", "name": "Read", "input": {"file_path": "/tmp/svg.svg"}})
        got = shown.of_file(self.j.path)
        want = {}
        for e, known in reducer.verdicts(journal.read(self.j.path)):
            if known != "rejected":
                shown.note(want, e["type"], e.get("data"))
        self.assertEqual(got, want)
        self.assertEqual(got[shown.key(pdf)], "file")
        self.assertEqual(got[shown.key(png)], "image")
        self.assertNotIn(shown.key(refused), got)

    def test_windows_paths_with_a_drive_are_paths(self):
        # 2026-10-19: on Windows an image path is C:\...; the rule knew only "/" and "~", so no session image was served
        self.assertEqual(shown.found("turn.tool", {"input": {"file_path": "C:\\Users\\a\\shot 1.png"}}),
                         [("C:\\Users\\a\\shot 1.png", "image")])
        said = "screenshot: C:\\Users\\a\\b.png, also file:///D:/x/y%20z.png and file://E:\\q\\r.jpg"
        self.assertEqual(shown.found("session.said", {"text": said}),
                         [("C:\\Users\\a\\b.png", "image"), ("D:/x/y z.png", "image"), ("E:\\q\\r.jpg", "image")])

    def test_a_drive_path_is_one_key_however_it_is_written_or_asked(self):
        # PR #180, review: file:///D:/x.png is "D:/x.png" in the index, but the page asks "/D:/x.png" (a URL path), so
        # it was not found
        self.j.append("session.said", "a", {"text": "screenshot file:///D:/x.png and E:\\q\\r.jpg"})
        idx = shown.Index(self.state)
        for asked in ("D:/x.png", "/D:/x.png", "D:\\x.png", "/D:\\x.png", "d:/x.png", "E:\\q\\r.jpg", "E:/q/r.jpg", "/E:/q/r.jpg"):
            self.assertEqual(idx.how(asked), "image", asked)
        for asked in ("D:/y.png", "/D:/x.png.bak", "F:/x.png"):
            self.assertIsNone(idx.how(asked), asked)

    def test_the_key_of_a_drive_path_is_one_form_whatever_the_session_wrote(self):
        # PR #180 review: a lowercase drive letter and mixed separators in session text: one key in the index,
        # "D:/a/b.png"
        self.j.append("session.said", "a", {"text": "file:///d:/x.png and D:\\a/b.png and e:\\q\\r.jpg"})
        idx = shown.Index(self.state)
        for asked in ("D:/x.png", "/d:/x.png", "d:\\x.png", "D:/a/b.png", "D:\\a\\b.png", "/D:/a/b.png", "E:/q/r.jpg"):
            self.assertEqual(idx.how(asked), "image", asked)
        self.assertEqual(sorted(idx.live), ["D:/a/b.png", "D:/x.png", "E:/q/r.jpg"])

    @unittest.skipUnless(os.name == "nt", "a drive letter — Windows")
    def test_a_picture_named_by_a_file_url_with_a_drive_is_served_for_the_pages_question(self):
        png = self.file("w.png")
        self.j.append("session.said", "a", {"text": "file:///" + png.replace(os.sep, "/")})
        idx = shown.Index(self.state)
        self.assertEqual(idx.servable("/" + png.replace(os.sep, "/")), png)   # this is how the page asks: a URL path
        self.assertEqual(idx.servable(png), png)

    def test_reading_in_pieces_changes_nothing_and_a_half_written_refusal_waits(self):
        a, b, c = (self.file(n) for n in ("a.pdf", "b.pdf", "c.pdf"))
        self.sent(a)
        bad = self.j.event("owner.file", None, {"path": b, "caption": "", "channel": "session", "owner": False,
                                                "sender": "a"})
        self.j.write(bad, self.j.event("core.rejected", None, {"of": bad["seq"], "reason": "x"}))
        self.sent(c)
        whole = shown.of_file(self.j.path)
        self.assertEqual(set(whole), {shown.key(a), shown.key(c)})
        for size in (1, 7, 64, 200):   # the chunk boundary mid-line, between an event and its rejection
            self.assertEqual(shown.of_file(self.j.path, chunk=size), whole, size)
        idx = shown.Index(self.state, background=False)
        idx.refresh()
        bad = self.j.event("owner.file", None, {"path": b, "caption": "", "channel": "session", "owner": False,
                                                "sender": "a"})
        tail = journal._line(bad) + journal._line(self.j.event("core.rejected", None, {"of": bad["seq"], "reason": "x"}))
        with open(self.j.path, "a", encoding="utf-8") as f:   # a record with a rejection is half visible
            f.write(tail[:len(journal._line(bad)) + 10])
        self.assertIsNone(idx.servable(b))
        with open(self.j.path, "a", encoding="utf-8") as f:
            f.write(tail[len(journal._line(bad)) + 10:])
        self.assertIsNone(idx.servable(b))
        self.assertEqual(idx.servable(c), c)

    def test_a_long_line_of_another_type_is_skipped_not_held_in_memory(self):
        import tracemalloc
        a, b, c = (self.file(n) for n in ("a.pdf", "b.pdf", "c.pdf"))
        self.sent(a)   # the last whole one before the snapshot: it has nothing to wait for
        snap = journal._line(self.j.event("core.snapshot", None, {"state": {"x": f"owner.file {b} /s/x.png " * 60000}}))
        before = os.path.getsize(self.j.path)
        with open(self.j.path, "a", encoding="utf-8") as f:   # a snapshot (~3 MB) is being written: half is visible
            f.write(snap[:len(snap) // 2])
        idx = shown.Index(self.state, background=False)
        current = idx._current
        idx._current = lambda: current(chunk=2 * shown.SKIP)   # the line is not finished, yet several chunks have been read
        peak = []

        def measured(path):
            tracemalloc.start()
            try:
                return idx.servable(path)
            finally:
                peak.append(tracemalloc.get_traced_memory()[1])
                tracemalloc.stop()
        self.assertEqual(measured(a), a)
        self.assertEqual(idx.offset, before)   # an unfinished line waits from its start
        with open(self.j.path, "a", encoding="utf-8") as f:
            f.write(snap[len(snap) // 2:])
        self.sent(c)
        self.assertEqual(measured(c), c)
        self.assertLess(max(peak), 1 << 20, peak)   # the line is 3 MB, the chunk is 128 KB
        self.assertIsNone(idx.servable(b))
        self.assertEqual(idx.offset, os.path.getsize(self.j.path))
        tracemalloc.start()
        try:
            got = shown.of_file(self.j.path, chunk=4096)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(set(got), {shown.key(a), shown.key(c)})
        self.assertEqual(got, shown.of_file(self.j.path, chunk=16 << 20))
        self.assertLess(peak, 1 << 20, peak)

    def test_a_long_refusal_written_halfway_still_holds_its_event(self):
        b = self.file("b.pdf")
        idx = shown.Index(self.state, background=False)
        idx.refresh()
        bad = self.j.event("owner.file", None, {"path": b, "caption": "", "channel": "session", "owner": False,
                                                "sender": "a"})
        no = journal._line(self.j.event("core.rejected", None, {"of": bad["seq"], "reason": "x" * (4 * shown.SKIP)}))
        with open(self.j.path, "a", encoding="utf-8") as f:   # a rejection longer than SKIP is half visible
            f.write(journal._line(bad) + no[:len(no) // 2])
        self.assertIsNone(idx.servable(b))
        with open(self.j.path, "a", encoding="utf-8") as f:
            f.write(no[len(no) // 2:])
        self.assertIsNone(idx.servable(b))
        self.assertEqual(idx.offset, os.path.getsize(self.j.path))

    def test_an_archive_is_read_once_and_what_was_read_before_the_rotation_stays_meanwhile(self):
        pdf = self.file("report.pdf")
        self.sent(pdf)
        for n in range(5):
            self.j.append("session.said", "a", {"text": f"line {n}"})
        live = shown.Index(self.state)
        self.assertEqual(live.servable(pdf), pdf)
        release, real = threading.Event(), shown.side_of
        def slow(path):   # the page's background did not manage to parse the new archive; parsing in this thread is as is
            if threading.current_thread() is not threading.main_thread():
                release.wait(10)
            return real(path)
        patch = mock.patch.object(shown, "side_of", slow)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(release.set)
        self.rotate()
        (archive,) = journal.archives(self.state)
        self.assertIsNone(shown.read_side(archive))   # rotation (hubd) does not parse the archive
        self.assertEqual(live.servable(pdf), pdf)   # what was read before rotation, until the archive is parsed
        self.assertTrue(any(k.startswith("journal@") for k in live.parts))
        idx = shown.Index(self.state, background=False)
        self.assertEqual(idx.servable(pdf), pdf)
        self.assertEqual(shown.read_side(archive), {shown.key(pdf): "file"})   # parsed and recorded: not read a second time
        with mock.patch.object(shown, "of_file", side_effect=AssertionError("parsed a second time")):
            self.assertEqual(shown.Index(self.state, background=False).servable(pdf), pdf)
        release.set()
        live.building.join(5)
        self.assertEqual(live.servable(pdf), pdf)
        self.assertEqual(live.servable(pdf), pdf)
        self.assertFalse([k for k in live.parts if k.startswith("journal@")])   # the archive is parsed: no copy needed

    def test_an_archive_parsed_by_the_old_rule_is_parsed_again(self):
        # PR #180, review: the path parsing rule changed (a Windows drive is a path) but the parse version stayed the
        # same,
        # so the old archive parse (no drive paths) was read as valid and the paths did not appear
        png = self.file("w.png")
        self.sent(png)
        self.rotate()
        (archive,) = journal.archives(self.state)
        with open(archive + shown.SIDE, "w", encoding="utf-8") as f:
            json.dump({"v": 1, "shown": {"/old/only.png": "image"}}, f)
        self.assertGreater(shown.VERSION, 1)
        self.assertIsNone(shown.read_side(archive))
        self.assertEqual(shown.side_of(archive), {shown.key(png): "file"})
        self.assertEqual(shown.read_side(archive), {shown.key(png): "file"})   # and recorded anew, now with the new version

    def test_an_archive_that_fails_is_not_read_again_on_every_question(self):
        pdf = self.file("report.pdf")
        self.sent(pdf)
        self.rotate()
        calls = []
        def broken(path):
            calls.append(path)
            raise UnicodeDecodeError("utf-8", b"", 0, 1, "x")
        with mock.patch.object(shown, "of_file", broken):
            idx = shown.Index(self.state, background=False)
            for _ in range(3):
                idx.servable(pdf)
                os.utime(os.path.join(self.state, journal.ARCHIVE))
        self.assertEqual(len(calls), 1)

    def test_newer_archives_are_not_pushed_out_by_older_ones(self):
        paths = [self.file(f"r{n}.pdf") for n in range(3)]
        for p in paths:
            self.sent(p)
            self.rotate()
        with mock.patch.object(shown, "LIMIT", 1):
            idx = shown.Index(self.state)
            idx.refresh()
            idx.building.join(5)
            for p in paths:
                self.assertEqual(idx.servable(p), p)

    def test_old_archives_are_read_in_the_background(self):
        pdf = self.file("report.pdf")
        self.sent(pdf)
        for n in range(5):
            self.j.append("session.said", "a", {"text": f"line {n}"})
        self.rotate()
        (archive,) = journal.archives(self.state)
        idx = shown.Index(self.state)
        idx.refresh()
        idx.building.join(5)
        self.assertEqual(idx.servable(pdf), os.path.realpath(pdf))

    def test_only_what_was_shown_and_is_a_file_on_disk(self):
        pdf, png, svg, other = (self.file(n) for n in ("a.pdf", "b.png", "c.svg", "d.png"))
        self.sent(pdf)
        self.j.append("session.said", "a", {"text": f"{png} and {svg} and {pdf.replace('a.pdf', 'named.pdf')}"})
        self.sent(os.path.dirname(pdf))   # a folder is not a file
        idx = shown.Index(self.state)
        self.assertEqual(idx.servable(pdf), pdf)
        self.assertEqual(idx.servable(png), png)
        for p in (svg, other, os.path.dirname(pdf), "/etc/hosts", "", None, "/x\0.png"):
            self.assertIsNone(idx.servable(p), p)
        link = os.path.join(self.state, "link.png")
        if not winsafe.try_symlink(other, link):   # Windows without the right to make links
            link = None
        if link:
            self.j.append("session.said", "a", {"text": f"{link}"})
            self.assertEqual(idx.servable(link), other)   # as before: the link's path was shown, what it points to is served
        own = os.path.join(self.state, "attachments", "u1", "x.bin")
        os.makedirs(os.path.dirname(own))
        with open(own, "wb") as f:
            f.write(b"1")
        self.assertEqual(idx.servable(own), own)   # the owner's attachment: always
        late = self.file("late.pdf")
        self.assertIsNone(idx.servable(late))
        self.sent(late)
        self.assertEqual(idx.servable(late), late)   # what was appended to the journal: at once, without a restart


if __name__ == "__main__":
    unittest.main()
