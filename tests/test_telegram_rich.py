"""A report as one rich Telegram message (hub send-rich, telegram.rich): parsing the file and sending by the bot."""
import contextlib
import io
import json
import os
import tempfile
import unittest

import platforms
from core import journal
from modules.telegram import bot as tg
from modules.telegram import rich
from tests.test_telegram import OWNER, FakeBotApi

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 100


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb" if isinstance(data, bytes) else "w", **({} if isinstance(data, bytes) else {"encoding": "utf-8"})) as f:
        f.write(data)
    return path


class Build(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hubrich-")
        self.addCleanup(platforms.current.rmtree, self.dir)
        write(os.path.join(self.dir, "img", "a.png"), PNG)
        write(os.path.join(self.dir, "b.jpg"), PNG)

    def test_markdown_local_pictures_become_media_links_and_web_ones_stay(self):
        md = write(os.path.join(self.dir, "r.md"),
                   "## Total\n\n![](img/a.png \"Cover\")\n\n![x](b.jpg)\n\n![](img/a.png)\n\n"
                   "![](https://example.com/c.png)\n\n![](missing.png)\n")
        r = rich.build(md, title="Week")
        self.assertEqual(r.field, "markdown")
        self.assertTrue(r.text.startswith("# Week\n"))
        self.assertIn('![](tg://photo?id=m1 "Cover")', r.text)
        self.assertIn("![x](tg://photo?id=m2)", r.text)
        self.assertEqual(r.text.count("tg://photo?id=m1"), 2)   # one picture twice: one upload
        self.assertIn("https://example.com/c.png", r.text)
        self.assertNotIn("missing.png)", r.text)                    # a missing file is a line, not a broken link
        self.assertIn("missing.png", r.text)
        self.assertEqual([(m, os.path.basename(p)) for m, p, _ in r.files], [("m1", "a.png"), ("m2", "b.jpg")])
        self.assertEqual(r.media(), [{"id": "m1", "media": {"type": "photo", "media": "attach://m1"}},
                                     {"id": "m2", "media": {"type": "photo", "media": "attach://m2"}}])
        self.assertIsNone(r.too_big())

    def test_html_keeps_the_body_and_drops_styles_and_scripts(self):
        page = write(os.path.join(self.dir, "r.html"),
                     "<!doctype html><html><head><title>t</title><style>h1{color:red}</style></head>"
                     "<body><h1>Report</h1><script>x()</script><img src='img/a.png'/>"
                     "<figure><img src=\"b.jpg\"><figcaption>B</figcaption></figure>"
                     "<details><summary>More</summary><p>details body</p></details></body></html>")
        r = rich.build(page)
        self.assertEqual(r.field, "html")
        self.assertNotIn("<style", r.text)
        self.assertNotIn("<script", r.text)
        self.assertNotIn("<head", r.text)
        self.assertIn("<img src='tg://photo?id=m1'/>", r.text)
        self.assertIn('<img src="tg://photo?id=m2">', r.text)
        self.assertIn("<details><summary>More</summary>", r.text)

    def test_html_data_src_is_not_the_picture(self):
        # a lazy-loading <img data-src=… src=…>: the picture is src; data-src is left as it is
        page = write(os.path.join(self.dir, "lazy.html"),
                     '<body><img data-src="b.jpg" src="img/a.png"></body>')
        r = rich.build(page)
        self.assertEqual([os.path.basename(p) for _, p, _ in r.files], ["a.png"])
        self.assertIn('data-src="b.jpg" src="tg://photo?id=m1"', r.text)

    def test_limits_are_named(self):
        md = write(os.path.join(self.dir, "long.md"), "a" * (rich.MAX_CHARS + 1))
        self.assertIn("32768", rich.build(md).too_big())
        many = "".join(f"![](p{i}.png)\n\n" for i in range(rich.MAX_MEDIA + 1))
        for i in range(rich.MAX_MEDIA + 1):
            write(os.path.join(self.dir, f"p{i}.png"), PNG)
        self.assertIn("50", rich.build(write(os.path.join(self.dir, "many.md"), many)).too_big())

    def test_video_and_other_files_get_their_own_media_type(self):
        write(os.path.join(self.dir, "v.mp4"), b"x")
        write(os.path.join(self.dir, "d.zip"), b"x")
        r = rich.build(write(os.path.join(self.dir, "r.md"), "![](v.mp4)\n\n![](d.zip)\n"))
        self.assertIn("tg://video?id=m1", r.text)
        self.assertIn("tg://document?id=m2", r.text)
        self.assertEqual([m["media"]["type"] for m in r.media()], ["video", "document"])

    def test_names_with_parentheses_are_substituted_whole(self):
        write(os.path.join(self.dir, "map(1).png"), PNG)
        write(os.path.join(self.dir, "my map (2).png"), PNG)
        r = rich.build(write(os.path.join(self.dir, "p.md"), "![](map(1).png)\n\n![x](<my map (2).png> \"T\")\n"))
        self.assertEqual([os.path.basename(p) for _, p, _ in r.files], ["map(1).png", "my map (2).png"])
        self.assertEqual(r.text, '![](tg://photo?id=m1)\n\n![x](tg://photo?id=m2 "T")\n')

    def test_fenced_code_is_left_as_it_is(self):
        md = write(os.path.join(self.dir, "c.md"), "```md\n![](b.jpg)\n```\n\n~~~\n![](img/a.png)\n~~~\n\n![](img/a.png)\n")
        r = rich.build(md)
        self.assertEqual([m for m, _, _ in r.files], ["m1"])
        self.assertIn("```md\n![](b.jpg)\n```", r.text)
        self.assertIn("~~~\n![](img/a.png)\n~~~", r.text)
        self.assertTrue(r.text.endswith("![](tg://photo?id=m1)\n"))

    def test_the_media_map_and_base_replace_the_source_files(self):
        copy = write(os.path.join(self.dir, "kept", "a.png"), PNG + b"copy")
        orig = os.path.join(self.dir, "img", "a.png")
        r = rich.build(write(os.path.join(self.dir, "kept", "r.md"), "![](img/a.png)\n"), base=self.dir, media={orig: copy})
        self.assertEqual([p for _, p, _ in r.files], [copy])

    def test_only_reports_are_read_as_reports(self):
        for ok in ("a.md", "a.MARKDOWN", "a.html", "a.htm"):
            self.assertTrue(rich.is_report(ok), ok)
        for bad in ("a.pdf", "a.txt", "a.png", "a"):
            self.assertFalse(rich.is_report(bad), bad)

    def test_the_sender_stays_in_the_message(self):
        md = write(os.path.join(self.dir, "s.md"), "text\n")
        self.assertTrue(rich.build(md, "Week", sender="analyst").text.startswith("# analyst: Week"))
        self.assertTrue(rich.build(md, sender="analyst").text.startswith("analyst\n\ntext"))
        page = write(os.path.join(self.dir, "s.html"), "<body><p>x</p></body>")
        self.assertTrue(rich.build(page, sender="a<b").text.startswith("<p>a&lt;b</p>"))

    def test_fallback_is_plain_markdown_and_the_pictures(self):
        md = write(os.path.join(self.dir, "r.md"), "## Total\n\n![](img/a.png \"Cover\")\n\n<details><summary>More</summary>\n\ndetails body\n\n</details>\n")
        text, pics = rich.fallback(rich.build(md, title="Week"))
        self.assertNotIn("tg://", text)
        self.assertNotIn("<details>", text)
        self.assertIn("Week", text)
        self.assertIn("details body", text)
        self.assertEqual([os.path.basename(p) for p in pics], ["a.png"])


class Send(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hubtgrich-")
        self.state = os.path.join(self.dir, "state")
        self.old_cfg = os.environ.get("HUB_TELEGRAM_CONFIG")
        os.environ["HUB_TELEGRAM_CONFIG"] = os.path.join(self.dir, "telegram.json")
        tg.save_config({"token": "1:x", "owners": [OWNER]})
        self.fake = FakeBotApi()
        self.j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        self.j.append("session.requested", "hub", {"cwd": "/w", "session_id": "h", "kind": "hub"})
        self.j.append("session.started", "hub", {"session_id": "h"})
        self.j.append("session.requested", "analyst", {"cwd": "/w", "session_id": "a"})
        self.j.append("session.started", "analyst", {"session_id": "a"})
        self.bot = tg.Bot(self.state, tg.Api("1:x", base=self.fake.base))
        self.bot.album_sync = True
        self.bot.ffmpeg = ""
        self.bot.hub_config = lambda: {"telegram": {"rich": True}}
        self.bot.catch_up()
        write(os.path.join(self.dir, "rep", "a.png"), PNG)
        write(os.path.join(self.dir, "rep", "b.png"), PNG)
        self.report = write(os.path.join(self.dir, "rep", "week.md"),
                            "## Total\n\n![](a.png)\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n![](b.png \"Back\")\n")

    def tearDown(self):
        self.fake.server.shutdown()
        self.fake.server.server_close()
        os.environ["HUB_TELEGRAM_CONFIG"] = self.old_cfg
        platforms.current.rmtree(self.dir)

    def send_rich(self, title="Week"):
        err = io.StringIO()
        self.j.append("owner.file", None, {"path": self.report, "source": self.report, "rich": True, "title": title,
                                           "channel": "session", "owner": False, "sender": "analyst"})
        with contextlib.redirect_stderr(err):
            self.bot.tick()
        return err.getvalue()

    def fields(self, raw):
        """multipart → {field: value} (files as bytes)."""
        out = {}
        for part in raw.split(b"--" + raw.split(b"\r\n", 1)[0][2:])[1:-1]:
            head, _, body = part.partition(b"\r\n\r\n")
            name = head.split(b'name="', 1)[1].split(b'"', 1)[0].decode()
            out[name] = body[:-2]
        return out

    def test_a_report_goes_as_one_rich_message_with_its_pictures(self):
        self.send_rich()
        (body,) = self.fake.sent("sendRichMessage")
        self.assertEqual([m for m, _ in self.fake.calls if m.startswith("send")], ["sendRichMessage"])
        f = self.fields(body["raw"])
        msg = json.loads(f["rich_message"])
        self.assertEqual(sorted(msg), ["markdown", "media"])
        self.assertTrue(msg["markdown"].startswith("# analyst: Week"))
        self.assertIn("tg://photo?id=m1", msg["markdown"])
        self.assertEqual([m["id"] for m in msg["media"]], ["m1", "m2"])
        self.assertEqual((f["m1"], f["m2"]), (PNG, PNG))
        self.assertEqual(f["chat_id"], str(OWNER).encode())

    def test_without_the_option_a_report_is_an_ordinary_file(self):
        self.bot.hub_config = lambda: {}
        self.send_rich()
        self.assertEqual(self.fake.sent("sendRichMessage"), [])
        self.assertEqual(len(self.fake.sent("sendDocument")), 1)

    def test_api_refusal_falls_back_to_text_and_album_and_says_why(self):
        self.fake.fail["sendRichMessage"] = [400]
        err = self.send_rich()
        self.assertIn("rich", err)
        self.assertIn("fail 400", err)
        texts = " ".join(self.fake.texts())
        self.assertIn("Week", texts)
        self.assertIn("Total", texts)
        self.assertEqual(len(self.fake.sent("sendMediaGroup")), 1)

    def test_the_sender_is_named_in_the_rich_message(self):
        self.send_rich()
        (body,) = self.fake.sent("sendRichMessage")
        self.assertTrue(json.loads(self.fields(body["raw"])["rich_message"])["markdown"].startswith("# analyst: Week"))

    def test_a_report_whose_source_folder_is_gone_still_has_its_pictures(self):
        kept = os.path.join(self.dir, "state", "att", "x")
        with open(self.report, encoding="utf-8") as f:
            copy = write(os.path.join(kept, "week.md"), f.read())
        pics = {os.path.join(self.dir, "rep", n): write(os.path.join(kept, "p_" + n), PNG + n.encode()) for n in ("a.png", "b.png")}
        src = self.report
        platforms.current.rmtree(os.path.dirname(src))
        self.j.append("owner.file", None, {"path": copy, "source": src, "rich": True, "title": "Week", "media": pics,
                                           "channel": "session", "owner": False, "sender": "analyst"})
        self.bot.tick()
        (body,) = self.fake.sent("sendRichMessage")
        f = self.fields(body["raw"])
        self.assertEqual((f["m1"], f["m2"]), (PNG + b"a.png", PNG + b"b.png"))

    def test_not_a_report_goes_as_a_file(self):
        pdf = write(os.path.join(self.dir, "rep", "x.pdf"), b"%PDF text")
        self.j.append("owner.file", None, {"path": pdf, "rich": True, "title": "X", "channel": "session", "owner": False,
                                           "sender": "analyst"})
        self.bot.tick()
        self.assertEqual(self.fake.sent("sendRichMessage"), [])
        self.assertEqual(len(self.fake.sent("sendDocument")), 1)

    def test_the_fallback_album_goes_through_bot_upload_many(self):
        self.fake.fail["sendRichMessage"] = [400]
        seen = []
        real = self.bot.upload_many
        self.bot.upload_many = lambda method, files, **kw: seen.append(method) or real(method, files, **kw)
        self.send_rich()
        self.assertEqual(seen.count("sendMediaGroup"), 1)

    def test_a_refused_album_goes_one_picture_at_a_time(self):
        self.fake.fail["sendRichMessage"] = [400]
        self.fake.fail["sendMediaGroup"] = [400]
        self.send_rich()
        self.assertEqual(len(self.fake.sent("sendPhoto")), 2)

    def test_pictures_that_never_get_through_are_named_not_lost(self):
        # the upload fails UPLOAD_TRIES times with 5xx: the rich message is given up on, the text goes, and each picture
        # is named in a line with its path (the album does not raise the given-up error up)
        self.fake.fail["sendRichMessage"] = [502] * 6
        self.send_rich()
        for _ in range(6):
            self.bot.hold_until = 0
            with contextlib.redirect_stderr(io.StringIO()):
                self.bot.tick()
        texts = self.fake.texts()
        self.assertEqual(sum("Total" in t for t in texts), 1)
        self.assertEqual(sum("did not get through" in t for t in texts), 2)

    def test_too_long_falls_back_without_trying(self):
        write(self.report, "## Total\n\n" + "word " * 7000 + "\n\n![](a.png)\n")
        err = self.send_rich()
        self.assertIn("32768", err)
        self.assertEqual(self.fake.sent("sendRichMessage"), [])
        self.assertTrue(self.fake.texts())
        self.assertEqual(len(self.fake.sent("sendPhoto")) + len(self.fake.sent("sendMediaGroup")), 1)

    def test_network_failure_retries_the_same_rich_message_once(self):
        self.fake.fail["sendRichMessage"] = ["drop"]
        self.send_rich()                     # the network dropped: the event waits for a retry, no fallback
        self.assertEqual(self.fake.sent("sendRichMessage"), [])
        self.bot.hold_until = 0             # the pause before the retry has passed
        with contextlib.redirect_stderr(io.StringIO()):
            self.bot.tick()
        self.assertEqual(len(self.fake.sent("sendRichMessage")), 1)
        self.assertEqual(self.fake.texts(), [])

if __name__ == "__main__":
    unittest.main()
