"""Showing a result (modules/proof): hub shot is a screenshot with markup, before/after, on every hub; hub video is a
scenario video up to 60 s, the module is enabled manually. Its own CDP WebSocket client, command and scenario parsing
run without a browser; shots and video run in a real headless Chrome if there is one (like the mermaid render)."""
import tests  # noqa: F401 - ignore the machine's settings
import base64
import contextlib
import functools
import http.server
import io
import json
import os
import shutil
import sys
import socket
import struct
import tempfile
import threading
import time
import unittest

import platforms
from core import i18n
from modules.proof import cdp, shot, steps, video
from tests.fakes import exe
from modules.testchrome import chrome

CHROME = chrome.find()
FFMPEG = shutil.which("ffmpeg")
PAGE = """<!doctype html><meta charset="utf-8"><body style="font:16px sans-serif;padding:40px">
<h1>Plan</h1><button id="go" onclick="document.getElementById('r').textContent='Saved'">Save</button>
<p id="r"></p><input id="q"><p id="mail">someone@example.com</p><p id="hidden" style="display:none">x</p>"""


def server_frame(payload, opcode=1, fin=True):
    """A server frame: unmasked."""
    n = len(payload)
    head = bytes([(0x80 if fin else 0) | opcode])
    head += bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n) if n < 1 << 16 else \
        bytes([127]) + struct.pack(">Q", n)
    return head + payload


def png_size(path):
    with open(path, "rb") as f:
        head = f.read(24)
    assert head[:8] == b"\x89PNG\r\n\x1a\n", path
    return struct.unpack(">II", head[16:24])


def run(fn, *a):
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()) as out:
        code = fn(*a)
    return code, out.getvalue(), err.getvalue()


class WebSocket(unittest.TestCase):
    def setUp(self):
        self.a, self.b = socket.socketpair()
        self.addCleanup(self.a.close)
        self.addCleanup(self.b.close)
        self.ws = cdp.Socket.__new__(cdp.Socket)
        self.ws.sock, self.ws.buf = self.a, b""

    def test_messages_of_every_length_kind_come_whole(self):
        for n in (5, 300, 70000):   # length of 7 bits, 16 bits, 64 bits (a screencast frame is hundreds of KB)
            text = "é" * n
            t = threading.Thread(target=self.b.sendall, args=(server_frame(text.encode()),))
            t.start()   # the socket pair's buffer is under 70 KB: write and read at the same time
            self.assertEqual(self.ws.recv(), text)
            t.join()

    def test_fragments_are_joined_and_ping_gets_a_pong(self):
        self.b.sendall(server_frame(b'{"id":', fin=False) + server_frame(b"x", opcode=9)
                       + server_frame(b"1}", opcode=0))
        self.assertEqual(self.ws.recv(), '{"id":1}')
        pong = self.b.recv(100)
        self.assertEqual(pong[0], 0x8A)
        self.assertEqual(cdp.unmask(pong[6:7], pong[2:6]), b"x")

    def test_client_frames_are_masked(self):
        for n in (5, 300, 70000):
            f = cdp.frame(b"a" * n)
            self.assertTrue(f[1] & 0x80)
            off = 2 + {126: 2, 127: 8}.get(f[1] & 0x7F, 0)
            self.assertEqual(cdp.unmask(f[off + 4:], f[off:off + 4]), b"a" * n)

    def test_closed_socket_is_an_error_not_a_hang(self):
        self.b.close()
        with self.assertRaises(cdp.Error):
            self.ws.recv()


class ShotArgs(unittest.TestCase):
    def test_marks_and_outputs(self):
        self.assertEqual(shot.marks_from([".plan = New plan", "#x"], ["#mail"]), [
            {"type": "box", "selector": ".plan", "text": "New plan"},
            {"type": "box", "selector": "#x", "text": None}, {"type": "redact", "selector": "#mail"}])
        self.assertEqual(shot.outputs("/t/a.png", [1440]), [(1440, "/t/a.png")])
        self.assertEqual(shot.outputs("/t/a.png", [390, 1440]), [(390, "/t/a-390.png"), (1440, "/t/a-1440.png")])

    def test_box_splits_at_the_first_equals_outside_brackets_and_quotes(self):
        box = lambda b: shot.marks_from([b], [])[0]
        self.assertEqual(box('[data-x="a=b"]=Caption'),
                         {"type": "box", "selector": '[data-x="a=b"]', "text": "Caption"})
        self.assertEqual(box('a[href^="/x"]'), {"type": "box", "selector": 'a[href^="/x"]', "text": None})
        self.assertEqual(box(".a=Caption"), {"type": "box", "selector": ".a", "text": "Caption"})
        self.assertEqual(box(".a = 2+2=4"), {"type": "box", "selector": ".a", "text": "2+2=4"})
        self.assertEqual(box("[title='x=y'] .b = Plan"),
                         {"type": "box", "selector": "[title='x=y'] .b", "text": "Plan"})
        self.assertEqual(box('[data-q="]="]=Quotes'), {"type": "box", "selector": '[data-q="]="]', "text": "Quotes"})
        self.assertEqual(box(r'[data-q="a\"=b"]=Screen'), {"type": "box", "selector": r'[data-q="a\"=b"]', "text": "Screen"})
        self.assertEqual(shot.marks_from([], ['input[name="a=b"]']), [{"type": "redact", "selector": 'input[name="a=b"]'}])

    def test_bad_arguments_say_what_is_wrong(self):
        for argv, word in (([], "a URL"), (["u", "--widths", "50"], "--widths"), (["u", "--box"], "--box"),
                           (["u", "--what"], "--what")):
            code, _, err = run(shot.main, argv, tempfile.mkdtemp())
            self.assertEqual(code, 2)
            self.assertIn(word, err)

    def test_compare_of_a_missing_file_fails_before_chrome(self):
        code, _, err = run(shot.main, ["--compare", "/nope/a.png", "/nope/b.png"], tempfile.mkdtemp())
        self.assertEqual(code, 2)
        self.assertIn("no file", err)

    def test_compare_captions_are_the_owners_even_when_a_session_runs_it(self):
        # the captions are burned into the picture the owner looks at: his language, not the session's English output
        from unittest import mock
        folder = tempfile.mkdtemp()
        import platforms
        self.addCleanup(platforms.current.rmtree, folder)
        a, b = os.path.join(folder, "a.png"), os.path.join(folder, "b.png")
        for p in (a, b):
            open(p, "wb").close()

        class Page:   # no Chrome: only the page compare() writes
            def __init__(self, *args):
                self.dir = folder

            def viewport(self, *args):
                pass

            def goto(self, *args):
                pass

            def js(self, expr):
                return [10, 10]

            def call(self, method, params=None):
                return {"data": ""}

            def close(self):
                pass
        for owner, other in (("ru", "en"), ("en", "ru")):
            with mock.patch.dict(os.environ, {"HUB_SESSION": "worker", "HUB_LANG": owner}), \
                    mock.patch.object(shot.cdp, "Page", Page), mock.patch.object(i18n, "_owner", None), \
                    mock.patch.object(i18n, "_current", None):
                shot.compare(a, b, os.path.join(folder, "c.png"))
                self.assertEqual(i18n.current(), "en")   # the session's output is English...
            with open(os.path.join(folder, "compare.html"), encoding="utf-8") as f:
                html = f.read()
            for key in ("cli.proof.before", "cli.proof.after"):   # ...the picture is in the owner's language
                self.assertIn(f"<figcaption>{i18n.tr(owner, key)}</figcaption>", html, owner)
                self.assertNotIn(i18n.tr(other, key), html, owner)


class Scenario(unittest.TestCase):
    def write(self, sc):
        path = os.path.join(tempfile.mkdtemp(), "sc.json")
        with open(path, "w") as f:
            f.write(sc if isinstance(sc, str) else json.dumps(sc))
        return path

    def test_scenario_is_checked_before_the_browser(self):
        for sc, word in (("{", "JSON"), ({}, "steps"), ({"steps": [{"click": "#a"}]}, "goto"),
                         ({"steps": [{"goto": "u"}, {"click": "#a", "hover": "#b"}]}, "step 2"),
                         ({"steps": [{"goto": "u"}, {"type": "#a"}]}, "step 2"),
                         ({"steps": [{"goto": "u"}, {"fly": 1}]}, "step 2")):
            with self.assertRaises(video.Usage) as e:
                video.load(self.write(sc))
            self.assertIn(word, str(e.exception))
        self.assertEqual(len(video.load(self.write({"steps": [{"goto": "u"}, {"caption": "Done"}]}))["steps"]), 2)

    def test_each_frame_lasts_until_the_next_and_the_last_until_the_end(self):
        lst = video.concat_list([("/f/1.jpg", 0.0), ("/f/2.jpg", 1.5)], 4.0)
        self.assertEqual(lst.splitlines(), ["ffconcat version 1.0", "file '/f/1.jpg'", "duration 1.500",
                                            "file '/f/2.jpg'", "duration 2.500", "file '/f/2.jpg'"])

    def test_off_by_default_and_says_how_to_turn_on(self):
        code, _, err = run(video.main, [self.write({"steps": [{"goto": "u"}]})], {}, tempfile.mkdtemp())
        self.assertEqual(code, 2)
        self.assertIn('"video": {"enabled": true}', err)
        self.assertIn("hub shot", err)

    def test_without_ffmpeg_says_so(self):
        cfg = {"video": {"enabled": True, "ffmpeg": "/nope/ffmpeg"}}
        old = os.environ.get("PATH")
        os.environ["PATH"] = tempfile.mkdtemp()
        try:
            code, _, err = run(video.main, ["x.json"], cfg, tempfile.mkdtemp())
        finally:
            os.environ["PATH"] = old
        self.assertEqual(code, 2)
        self.assertIn("ffmpeg", err)

    def fake_ffmpeg(self, listed):
        """A program that answers -encoders with the given names, like an ffmpeg build without some encoders."""
        lines = "".join(f" V....D {n:<18} {n} encoder\\n" for n in listed)
        return exe.script(os.path.join(tempfile.mkdtemp(), "ffmpeg"),
                          f"import sys\nsys.stdout.write(' Encoders:\\n ------\\n{lines}')\n")

    def test_encoder_is_the_best_one_the_ffmpeg_has(self):
        for listed, name in ((["libx264", "mpeg4"], "libx264"), (["mpeg4", "libopenh264"], "libopenh264"),
                             (["mpeg4", "h264_videotoolbox", "libopenh264"], "h264_videotoolbox"),
                             (["mpeg4"], "mpeg4")):
            self.assertEqual(video.encoder(self.fake_ffmpeg(listed))[:2], ["-c:v", name])

    def test_ffmpeg_without_libx264_is_not_asked_for_it(self):
        enc = video.encoder(self.fake_ffmpeg(["mpeg4", "libopenh264"]))
        self.assertNotIn("libx264", enc)
        self.assertNotIn("-crf", enc)

    def test_ffmpeg_without_any_encoder_says_so_before_the_browser(self):
        ff = self.fake_ffmpeg(["aac"])
        with self.assertRaises(video.Usage) as e:
            video.encoder(ff)
        self.assertIn("encoder", str(e.exception))
        with self.assertRaises(video.Usage):   # record() checks it first: no Chrome is started
            video.record({"steps": [{"goto": "u"}]}, "/nope/out.mp4", ff, chrome="/nope/chrome")

    def test_usage_shows_the_scenario_format(self):
        code, _, err = run(video.main, [], {"video": {"enabled": True, "ffmpeg": sys.executable}},
                           tempfile.mkdtemp())
        self.assertEqual(code, 2)
        self.assertIn(i18n.t("cli.proof.video_usage"), err)   # the scenario format, from the catalog
        self.assertIn('"goto"', err)


class FakePage:
    """A tab without Chrome: remembers what the steps asked of it. present(selector, n) says whether a wait_for element is
    there on the n-th look."""
    PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 16).decode()

    def __init__(self, *args):
        self.dir = tempfile.mkdtemp()
        self.log, self.looks, self.on_event = [], {}, None
        FakePage.last = self

    present = staticmethod(lambda sel, n: True)

    def viewport(self, *args):
        self.log.append(("viewport",) + args)

    def goto(self, url, wait=0.4):
        self.log.append(("goto", url))

    def pump(self, seconds):
        time.sleep(0.005)

    def js(self, expr):
        if expr.startswith("(() => { const e = document.querySelector("):   # centre of an element, or a wait_for look
            sel = json.loads(expr.split("querySelector(", 1)[1].split(")", 1)[0])
            if "getBoundingClientRect" in expr and "return [" in expr:
                self.log.append(("center", sel))
                return [100, 50]
            self.looks[sel] = self.looks.get(sel, 0) + 1
            self.log.append(("look", sel))
            return self.present(sel, self.looks[sel])
        return {}

    def call(self, method, params=None, page=True):
        self.log.append((method, params))
        return {"data": self.PNG} if method == "Page.captureScreenshot" else {}

    def close(self):
        platforms.current.rmtree(self.dir)

    def kinds(self):
        return [e[0] for e in self.log]


class ShotSteps(unittest.TestCase):
    """hub shot --steps: the steps of hub video (one code, modules/proof/steps.py), a {"shot": name} step saves a picture."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(platforms.current.rmtree, self.dir)

    def write(self, sc):
        path = os.path.join(self.dir, "sc.json")
        with open(path, "w") as f:
            f.write(sc if isinstance(sc, str) else json.dumps(sc))
        return path

    def run_steps(self, sc, present=None, **kw):
        from unittest import mock
        warned = []
        with mock.patch.object(shot.cdp, "Page", FakePage):
            if present:
                FakePage.present = staticmethod(present)
            try:
                paths = shot.run_steps(shot.load_steps(self.write(sc)), os.path.join(self.dir, "out.png"),
                                       warn=warned.append, **kw)
            finally:
                FakePage.present = staticmethod(lambda sel, n: True)
        return paths, warned

    def test_a_shot_step_saves_the_page_as_it_is_after_the_steps_before_it(self):
        paths, _ = self.run_steps({"steps": [{"goto": "http://x.test/"}, {"click": "#go"}, {"shot": "saved"},
                                             {"hover": "#tip"}, {"shot": "tip"}]})
        self.assertEqual(paths, [os.path.join(self.dir, "out-saved.png"), os.path.join(self.dir, "out-tip.png")])
        for p in paths:
            self.assertTrue(os.path.getsize(p) > 0)
        log = FakePage.last.log
        click = next(i for i, e in enumerate(log) if e[0] == "Input.dispatchMouseEvent" and e[1]["type"] == "mousePressed")
        grabs = [i for i, e in enumerate(log) if e[0] == "Page.captureScreenshot"]
        self.assertEqual(len(grabs), 2)
        self.assertLess(click, grabs[0])   # the click is in the first picture
        self.assertGreater(len([e for e in log[grabs[0]:grabs[1]] if e[0] == "center"]), 0)   # the hover is in the second

    def test_width_and_height_are_for_that_shot_only(self):
        self.run_steps({"steps": [{"goto": "http://x.test/"}, {"shot": "phone", "width": 390},
                                  {"shot": "tall", "height": 2000}, {"shot": "plain"}]})
        views = [e[1:] for e in FakePage.last.log if e[0] == "viewport"]
        self.assertEqual(views, [(1440, 900, 1, False),
                                 (390, 844, 2, True), (1440, 900, 1, False),
                                 (1440, 2000, 1, False), (1440, 900, 1, False)])

    def test_a_shot_at_a_phone_width_is_a_phone_not_a_narrow_desktop(self):
        # the same rule as hub shot --widths: up to 500 px - mobile emulation, touch, density 2
        self.run_steps({"steps": [{"goto": "http://x.test/"}, {"shot": "a", "width": 500}, {"shot": "b", "width": 501}]})
        views = [e[1:] for e in FakePage.last.log if e[0] == "viewport"]
        self.assertEqual([v[2:] for v in views[1:4:2]], [(2, True), (1, False)])

    def test_a_mobile_scenario_is_a_phone_for_the_whole_run(self):
        self.run_steps({"mobile": True, "steps": [{"goto": "http://x.test/"}, {"shot": "a"}, {"shot": "wide", "width": 800},
                                                  {"shot": "tall", "height": 1200}, {"shot": "b"}]})
        views = [e[1:] for e in FakePage.last.log if e[0] == "viewport"]
        phone = (390, 844, 2, True)
        self.assertEqual(views, [phone, (800, 900, 1, False), phone, (390, 1200, 2, True), phone])   # a height keeps the width

    def test_the_caption_of_a_shot_step_is_in_the_picture_and_there_is_no_cursor_dot(self):
        self.run_steps({"steps": [{"goto": "http://x.test/"}, {"shot": "a", "caption": "Step"}]})
        added = [e[1]["source"] for e in FakePage.last.log if e[0] == "Page.addScriptToEvaluateOnNewDocument"]
        self.assertEqual(len(added), 1)
        self.assertIn("__hubCaption", added[0])
        self.assertTrue(added[0].rstrip().endswith("(false)"))   # the caption layer alone: no yellow dot, no click wave

    def test_wait_for_goes_on_when_the_element_is_there(self):
        self.run_steps({"steps": [{"goto": "http://x.test/"}, {"wait_for": "#card"}, {"shot": "card"}]},
                       present=lambda sel, n: n >= 3)
        self.assertEqual(FakePage.last.looks["#card"], 3)
        self.assertLess(FakePage.last.kinds().index("look"), FakePage.last.kinds().index("Page.captureScreenshot"))

    def test_wait_for_that_never_comes_names_the_step_and_the_element(self):
        from unittest import mock
        with mock.patch.object(steps, "WAIT_FOR", 0.2), self.assertRaises(cdp.Error) as e:
            self.run_steps({"steps": [{"goto": "http://x.test/"}, {"click": "#go"}, {"wait_for": "#card"},
                                      {"shot": "card"}]}, present=lambda sel, n: False)
        self.assertIn(i18n.t("cli.proof.step_failed", n=3, error=i18n.t("cli.proof.wait_for_timeout",
                                                                       selector="#card", seconds="0.2")), str(e.exception))
        self.assertNotIn("Page.captureScreenshot", FakePage.last.kinds())
        self.assertEqual(os.listdir(self.dir), ["sc.json"])   # nothing half-made

    def test_a_missing_element_names_the_step(self):
        class Gone(FakePage):
            def js(self, expr):
                if "return [" in expr:
                    raise cdp.Error("no element #nope")
                return super().js(expr)
        from unittest import mock
        with mock.patch.object(shot.cdp, "Page", Gone), self.assertRaises(cdp.Error) as e:
            shot.run_steps(shot.load_steps(self.write({"steps": [{"goto": "http://x.test/"}, {"click": "#nope"},
                                                                 {"shot": "a"}]})), os.path.join(self.dir, "o.png"))
        self.assertIn(i18n.t("cli.proof.step_failed", n=2, error="no element #nope"), str(e.exception))

    def test_the_scenario_is_checked_before_the_browser(self):
        from unittest import mock
        first = {"goto": "u"}
        for sc, word in (({"steps": [first, {"click": "#a"}]}, "shot"),   # nothing to save
                         ({"steps": [first, {"shot": "a/b"}]}, "step 2"),
                         ({"steps": [first, {"shot": ""}]}, "step 2"),
                         ({"steps": [first, {"shot": 5}]}, "step 2"),
                         ({"steps": [first, {"shot": "a"}, {"shot": "a"}]}, "step 3"),
                         ({"steps": [first, {"shot": "Menu"}, {"shot": "menu"}]}, "step 3"),   # one file on macOS and Windows
                         ({"steps": [first, {"shot": "a", "click": "#b"}]}, "step 2"),
                         ({"steps": [first, {"shot": "a", "width": 50}]}, "step 2"),
                         ({"steps": [first, {"shot": "a", "height": "tall"}]}, "step 2"),
                         ({"steps": [first, {"click": "#a", "width": 390}, {"shot": "a"}]}, "step 2"),
                         ({"steps": [first, {"wait_for": ""}, {"shot": "a"}]}, "step 2"),
                         ({"steps": [first, {"wait_for": 5}, {"shot": "a"}]}, "step 2"),
                         ({"steps": [{"click": "#a"}, {"shot": "a"}]}, "goto")):
            with mock.patch.object(shot.cdp, "Page", side_effect=AssertionError("the browser started")), \
                    self.assertRaises(shot.Usage) as e:
                shot.run_steps(shot.load_steps(self.write(sc)), os.path.join(self.dir, "o.png"))
            self.assertIn(word, str(e.exception), sc)
        shot.load_steps(self.write({"steps": [first, {"wait_for": "#a"}, {"shot": "a.b-c_1", "width": 390, "height": 800,
                                                                       "caption": "ok"}]}))

    def test_video_takes_wait_for_but_no_shot_step(self):
        sc = lambda *s: self.write({"steps": [{"goto": "u"}, *s]})
        self.assertEqual(len(video.load(sc({"wait_for": "#a"}))["steps"]), 2)
        with self.assertRaises(video.Usage) as e:
            video.load(sc({"shot": "a"}))
        self.assertIn("step 2", str(e.exception))
        with self.assertRaises(video.Usage):
            video.load(sc({"shot": "a", "caption": "x"}))   # a caption does not make a shot step a video step
        with self.assertRaises(video.Usage):
            video.load(sc({"wait_for": ""}))

    def test_video_and_shot_run_the_same_step_code(self):
        self.assertIs(video.run_step, steps.run_step)
        self.assertIs(video.Cursor, steps.Cursor)
        self.assertIs(shot.steps, steps)

    def test_the_command_line(self):
        sc = self.write({"steps": [{"goto": "http://x.test/"}, {"shot": "a"}]})
        out = os.path.join(self.dir, "o.png")
        self.assertEqual(shot.parse(["--steps", sc, "--out", out])["steps"], sc)
        self.assertEqual(shot.parse(["--steps", sc, "--out", "--wait"])["out"], "--wait")   # a value is not a flag
        for argv in (["--steps"], ["--steps", sc, "http://x.test/"], ["--steps", sc, "--widths", "390"], ["--steps", sc, "--widths", "1440"],
                     ["--steps", sc, "--wait", "400"],
                     ["--steps", sc, "--full"], ["--steps", sc, "--box", "#a"], ["--steps", sc, "--eval", "1"],
                     ["--steps", sc, "--wait", "100"], ["--steps", sc, "--compare", "a.png", "b.png"]):
            code, _, err = run(shot.main, argv, self.dir)
            self.assertEqual(code, 2, argv)
            self.assertIn("--steps", err)
        code, _, err = run(shot.main, ["--steps", "/nope/sc.json"], self.dir)
        self.assertEqual((code, "no file" in err), (2, True))
        code, _, err = run(shot.main, ["--steps", self.write("{")], self.dir)
        self.assertEqual((code, "JSON" in err), (2, True))

    def test_main_prints_each_path_and_uses_the_hub_folder_without_out(self):
        from unittest import mock
        sc = self.write({"steps": [{"goto": "http://x.test/"}, {"shot": "a"}, {"shot": "b"}]})
        with mock.patch.object(shot.cdp, "Page", FakePage):
            code, out, err = run(shot.main, ["--steps", sc, "--out", os.path.join(self.dir, "pay.png")], self.dir)
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(out.split(), [os.path.join(self.dir, "pay-a.png"), os.path.join(self.dir, "pay-b.png")])
            home = os.path.join(self.dir, "hub")
            code, out, err = run(shot.main, ["--steps", sc], home)
        self.assertEqual(code, 0, err)
        names = sorted(os.path.basename(p) for p in out.split())
        self.assertEqual(len(names), 2)
        self.assertTrue(names[0].startswith("steps-") and names[0].endswith("-a.png"), names)
        self.assertEqual(sorted(os.listdir(home)), names)


class Modules(unittest.TestCase):
    def test_video_is_listed_off_until_turned_on(self):
        from modules.web import modinfo
        state = tempfile.mkdtemp()

        def entry(cfg):
            return next(m for m in modinfo.modules(state, cfg, {}) if m["id"] == "video")
        self.assertEqual(entry({})["state"], modinfo.OFF)
        self.assertEqual(entry({"video": {"enabled": True, "ffmpeg": "/nope"}})["state"], modinfo.PART)

    def test_shots_folder_is_a_files_root_once_it_exists(self):
        from modules.files import server
        state = tempfile.mkdtemp()
        files = server.Files(config=lambda: {}, state_dir=state)
        self.assertNotIn(server.PROOF, files.roots())
        os.makedirs(os.path.join(state, "proof"))
        self.assertEqual(files.roots()[server.PROOF], os.path.realpath(os.path.join(state, "proof")))


def tiny_png(path, rgb=(220, 40, 40), size=64):
    import zlib

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))
    raw = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def png_chunks(path):
    """[(type, data)] of a PNG; a chunk with a wrong CRC is an error - the file would not open."""
    import zlib
    with open(path, "rb") as f:
        raw = f.read()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", path
    out, i = [], 8
    while i < len(raw):
        n, kind = struct.unpack(">I4s", raw[i:i + 8])
        data = raw[i + 8:i + 8 + n]
        assert struct.unpack(">I", raw[i + 8 + n:i + 12 + n])[0] == zlib.crc32(kind + data), (path, kind)
        out.append((kind, data))
        i += 12 + n
    return out


def software(path):
    """tEXt Software values before the image data - where a file index looks for the hub shot mark."""
    texts = []
    for kind, data in png_chunks(path):
        if kind == b"IDAT":
            break
        key, _, value = data.partition(b"\0")
        if kind == b"tEXt" and key == b"Software":
            texts.append(value.decode("latin-1"))
    return texts


class FramePage:
    """cdp.Page without a browser: every shot is the same small PNG, the page is `height` px tall."""

    def __init__(self, png, height=900):
        with open(png, "rb") as f:
            self.png = f.read()
        self.height, self.dir = height, tempfile.mkdtemp()

    def call(self, method, params=None):
        if method == "Page.getLayoutMetrics":
            return {"cssContentSize": {"width": 64, "height": self.height}}
        if method == "Page.addScriptToEvaluateOnNewDocument":   # the captions of a --steps scenario
            return {}
        assert method == "Page.captureScreenshot", method
        return {"data": base64.b64encode(self.png).decode()}

    def js(self, code):
        return [64, 64] if "getElementById('c')" in code else {}   # the size of the before/after pair; settle - nothing

    def viewport(self, *a):
        pass

    def goto(self, *a):
        pass

    def pump(self, *a):
        pass

    def close(self):
        platforms.current.rmtree(self.dir)


class ShotLabel(unittest.TestCase):
    """A file index tells a screenshot from a picture drawn by a model or a script only by what the file says: a hub
    shot frame says so in a PNG tEXt Software chunk ("hub shot") before the image data. The picture itself is the
    browser's bytes as they were - the chunk is inserted, nothing is re-encoded."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(platforms.current.rmtree, self.dir)
        self.src = os.path.join(self.dir, "src.png")
        tiny_png(self.src)
        self.pages = []
        real = cdp.Page
        cdp.Page = lambda *a, **kw: self.pages.append(FramePage(self.src, self.height)) or self.pages[-1]
        self.addCleanup(setattr, cdp, "Page", real)
        self.height = 900

    def test_a_hub_shot_frame_says_it_is_a_hub_shot(self):
        out = os.path.join(self.dir, "o.png")
        self.assertEqual(shot.shot("page.html", out, wait=0), [out])
        self.assertEqual(software(out), ["hub shot"])
        self.assertEqual([k for k, _ in png_chunks(out)], [b"IHDR", b"tEXt", b"IDAT", b"IEND"])
        self.assertEqual([c for c in png_chunks(out) if c[0] != b"tEXt"], png_chunks(self.src))   # the same picture

    def test_every_width_and_every_part_of_a_long_page_says_so(self):
        self.height = shot.MAX_HEIGHT + 10
        paths = shot.shot("page.html", os.path.join(self.dir, "t.png"), widths=(390, 1440), full=True, wait=0)
        self.assertEqual(len(paths), 4)
        self.assertEqual([software(p) for p in paths], [["hub shot"]] * 4)

    def test_before_and_after_is_not_a_screenshot(self):
        # --compare is a picture made of two shots, not a capture of a screen
        a, b = (os.path.join(self.dir, n) for n in ("a.png", "b.png"))
        tiny_png(a)
        tiny_png(b)
        out = shot.compare(a, b, os.path.join(self.dir, "ab.png"))
        self.assertEqual(software(out), [])

    def test_a_shot_of_a_steps_scenario_says_so_too(self):
        # hub shot --steps saves the page at each shot step: a capture of the screen as well
        path = os.path.join(self.dir, "sc.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"steps": [{"goto": "http://x.test/"}, {"shot": "start"}, {"shot": "phone", "width": 390}]}, f)
        paths = shot.run_steps(shot.load_steps(path), os.path.join(self.dir, "s.png"))
        self.assertEqual([os.path.basename(p) for p in paths], ["s-start.png", "s-phone.png"])
        self.assertEqual([software(p) for p in paths], [["hub shot"]] * 2)


SETTLE_PAGE = """<!doctype html><meta charset="utf-8"><body style="margin:0">
<div style="height:3000px">top</div>
<img id="pic" loading="lazy" src="pic.png?slow" width="64" height="64">
<video id="vid" src="clip.mp4?slow" muted preload="none" width="64" height="64"></video>
%s"""


class Slow(http.server.SimpleHTTPRequestHandler):
    """Files with a delay (?slow) or with no answer at all (hang): media that did not make it to the shot."""

    def do_GET(self):
        if "hang" in self.path:
            time.sleep(4)
            return
        if "slow" in self.path:
            time.sleep(0.4)
        super().do_GET()

    def log_message(self, *a):
        pass


@unittest.skipUnless(CHROME, chrome.INSTALL)
class Settle(unittest.TestCase):
    """hub shot waits for media itself: lazy pictures, videos, fonts (a shot of a page with videos came out empty)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        tiny_png(os.path.join(self.dir, "pic.png"))
        if FFMPEG:
            import subprocess
            subprocess.run([FFMPEG, "-v", "quiet", "-f", "lavfi", "-i", "color=c=blue:s=64x64:d=0.5", "-c:v", "libx264",
                            "-pix_fmt", "yuv420p", os.path.join(self.dir, "clip.mp4")], check=True)
        handler = functools.partial(Slow, directory=self.dir)
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/page.html"

    def page(self, extra=""):
        with open(os.path.join(self.dir, "page.html"), "w", encoding="utf-8") as f:
            f.write(SETTLE_PAGE % extra)

    def at_capture(self, **kw):
        """What the page has at the moment of the shot (capture is replaced: it reads the state before the real one)."""
        seen, real, warns = [], shot.capture, []

        def capture(page, out, full=False, **kw):
            seen.append(page.js("({pic: !!(document.getElementById('pic').complete && "
                                "document.getElementById('pic').naturalWidth), "
                                "vid: document.getElementById('vid').readyState, "
                                "y: Math.round(scrollY)})"))
            return real(page, out, full, **kw)
        shot.capture = capture
        self.addCleanup(setattr, shot, "capture", real)
        shot.shot(self.url, os.path.join(self.dir, "o.png"), wait=0, warn=warns.append, **kw)
        return seen[0], warns

    @unittest.skipUnless(FFMPEG, "ffmpeg is needed")
    def test_full_page_has_lazy_image_and_video_frame_by_the_shot(self):
        self.page()
        state, warns = self.at_capture(full=True)
        self.assertTrue(state["pic"], state)
        self.assertGreaterEqual(state["vid"], 2, state)
        self.assertEqual(state["y"], 0)   # the scroll went back to the top
        self.assertEqual(warns, [])

    def test_what_did_not_load_is_named_and_the_shot_is_still_taken(self):
        self.page('<img id="stuck" loading="lazy" src="never.png?hang" width="10" height="10">')
        t = time.time()
        state, warns = self.at_capture(full=True, ceiling=1.5)
        self.assertLess(time.time() - t, 15)
        self.assertTrue(os.path.getsize(os.path.join(self.dir, "o.png")) > 0)
        self.assertTrue(any("never.png" in w for w in warns), warns)

    def test_below_the_fold_does_not_hold_a_viewport_shot(self):
        self.page('<img id="stuck" loading="lazy" src="never.png?hang" width="10" height="10">')
        t = time.time()
        state, warns = self.at_capture(ceiling=1.5)
        self.assertFalse(state["pic"])   # below the screen: not shot and not waited for
        self.assertFalse([w for w in warns if "never.png" in w], warns)

    def test_a_broken_image_is_reported_without_waiting(self):
        self.page('<img id="bad" loading="lazy" src="missing.png" width="10" height="10">')
        t = time.time()
        state, warns = self.at_capture(full=True, ceiling=8)
        self.assertLess(time.time() - t, 8)
        self.assertTrue(any("missing.png" in w for w in warns), warns)


@unittest.skipUnless(CHROME, chrome.INSTALL)
class InChrome(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.page = os.path.join(self.dir, "page.html")
        with open(self.page, "w", encoding="utf-8") as f:
            f.write(PAGE)

    def _chromes_of(self, profile):
        import subprocess
        out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
        return [line for line in out.splitlines() if f"--user-data-dir={profile}" in line]

    def _orphan(self, script):
        """A script on cdp.Page in a separate process; prints its Chrome's profile."""
        import subprocess
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        p = subprocess.Popen([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); from modules.proof "
                              "import cdp; p = cdp.Page(); print(p.dir, flush=True); " + script, root],
                             stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        return p, p.stdout.readline().strip()

    @unittest.skipIf(os.name == "nt", "ps is macOS and Linux only")
    def test_a_script_failing_before_close_leaves_no_chrome(self):
        # 2026-10-12: shot scripts that crashed before close() left three headless Chromes - dozens of processes with helpers
        p, profile = self._orphan("raise RuntimeError('failed')")
        p.wait(30)
        self.assertTrue(profile)
        self.assertEqual(self._chromes_of(profile), [])

    @unittest.skipIf(os.name == "nt", "ps is macOS and Linux only")
    def test_chrome_of_a_killed_script_is_stopped_by_the_next_page(self):
        import signal
        import time
        p, profile = self._orphan("import time; time.sleep(60)")
        os.kill(p.pid, signal.SIGKILL)
        p.wait(10)
        self.assertNotEqual(self._chromes_of(profile), [])   # the killed script never called close()
        cdp.Page().close()
        deadline = time.time() + 10
        while self._chromes_of(profile) and time.time() < deadline:
            time.sleep(0.2)
        self.assertEqual(self._chromes_of(profile), [])

    def test_two_widths_of_a_page_with_a_hash(self):
        # 2026-10-06, Windows test rig: `hub shot <address>#/s/x --widths 1440,390` did not wait for the second width -
        # navigating to the same address with a # does not reload the page, so there is no load event; each width alone worked
        import pathlib
        url = pathlib.Path(self.page).as_uri() + "#/s/x"
        code, out, err = run(shot.main, [url, "--out", os.path.join(self.dir, "h.png"), "--widths", "1440,390",
                                         "--wait", "100"], self.dir)
        self.assertEqual(code, 0, err)
        for w in (1440, 390):
            self.assertTrue(os.path.getsize(os.path.join(self.dir, f"h-{w}.png")) > 0)

    def test_phone_and_desktop_with_marks(self):
        code, out, err = run(shot.main, [self.page, "--out", os.path.join(self.dir, "s.png"), "--widths", "390,1440",
                                         "--box", "#go=New button", "--redact", "#mail"], self.dir)
        self.assertEqual(code, 0, err)
        phone, desk = out.split()
        self.assertEqual(png_size(phone), (780, 1688))   # 390x844 at density 2
        self.assertEqual(png_size(desk), (1440, 900))

    def test_a_page_taller_than_the_limit_is_shot_whole_in_parts(self):
        # --full used to crop a long page at 12000 px and lose the rest; now parts <name>.png, <name>-2.png...
        tall = os.path.join(self.dir, "tall.html")
        with open(tall, "w", encoding="utf-8") as f:
            f.write('<!doctype html><meta charset="utf-8"><body style="margin:0">'
                    '<div style="height:24900px;background:#eee">top</div><p id="end" style="height:100px;margin:0">end</p>'
                    '<p id="mid" style="position:absolute;top:13000px;left:100px;width:200px;height:50px">mid</p>')
        out = os.path.join(self.dir, "t.png")
        code, printed, err = run(shot.main, [tall, "--out", out, "--full", "--wait", "100", "--box", "#mid=Middle"],
                                 self.dir)
        self.assertEqual(code, 0, err)
        parts = [os.path.join(self.dir, n) for n in ("t.png", "t-2.png", "t-3.png")]
        self.assertEqual(printed.split(), parts)
        self.assertEqual([png_size(x) for x in parts], [(1440, 12000), (1440, 12000), (1440, 1000)])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "t-4.png")))
        # several widths: <base>-<width>.png, <base>-<width>-2.png...
        code, printed, err = run(shot.main, [tall, "--out", out, "--full", "--wait", "100", "--widths", "1440,1000"],
                                 self.dir)
        self.assertEqual(code, 0, err)
        names = [os.path.basename(x) for x in printed.split()]
        self.assertEqual(names, ["t-1440.png", "t-1440-2.png", "t-1440-3.png",
                                 "t-1000.png", "t-1000-2.png", "t-1000-3.png"])

    def test_a_page_within_the_limit_stays_one_picture(self):
        code, printed, err = run(shot.main, [self.page, "--out", os.path.join(self.dir, "o.png"), "--full",
                                             "--wait", "100"], self.dir)
        self.assertEqual(code, 0, err)
        self.assertEqual(printed.split(), [os.path.join(self.dir, "o.png")])

    def test_a_mark_on_a_missing_or_hidden_element_is_an_error(self):
        for sel, word in (("#nope", "no element #nope"), ("#hidden", "element not visible: #hidden")):
            code, _, err = run(shot.main, [self.page, "--box", sel], self.dir)
            self.assertEqual(code, 2)
            self.assertIn(word, err)
            self.assertNotIn("at <anonymous>", err)

    def test_without_out_it_goes_to_the_hub_folder(self):
        code, out, err = run(shot.main, [self.page], os.path.join(self.dir, "proof"))
        self.assertEqual(code, 0, err)
        self.assertTrue(out.strip().startswith(os.path.join(self.dir, "proof", "shot-")))

    def test_before_and_after_side_by_side_and_stacked(self):
        a, b = shot.shot(self.page, os.path.join(self.dir, "x.png"), [390, 1440])
        side = shot.compare(a, b, os.path.join(self.dir, "side.png"))
        stack = shot.compare(a, b, os.path.join(self.dir, "stack.png"), stack=True)
        self.assertEqual([software(p) for p in (a, b, side)], [["hub shot"], ["hub shot"], []])   # Chrome opened the tagged
        (sw, sh), (tw, th) = png_size(side), png_size(stack)
        self.assertGreater(sw, 780 + 1440)
        self.assertGreater(th, 1688 // 2 + 900)
        self.assertGreater(sw, tw)

    def test_shots_of_a_scenario_after_a_click_and_a_wait(self):
        # #r has no size until the click fills it: wait_for passes only if the click really happened in this Chrome
        sc = os.path.join(self.dir, "sc.json")
        with open(sc, "w") as f:
            json.dump({"steps": [{"goto": self.page}, {"shot": "before"}, {"click": "#go", "caption": "Save"},
                                 {"wait_for": "#r"}, {"shot": "after"}, {"shot": "phone", "width": 390},
                                 {"shot": "again"}]}, f)
        out = os.path.join(self.dir, "s.png")
        code, printed, err = run(shot.main, ["--steps", sc, "--out", out], self.dir)
        self.assertEqual(code, 0, err)
        names = ("before", "after", "phone", "again")
        self.assertEqual(printed.split(), [os.path.join(self.dir, f"s-{n}.png") for n in names])
        sizes = {n: png_size(os.path.join(self.dir, f"s-{n}.png")) for n in names}
        self.assertEqual(sizes["phone"][0], 390 * 2)
        self.assertEqual(sizes["before"], (1440, 900))
        self.assertEqual(sizes["again"], (1440, 900))   # the width was for the phone shot only

    def test_wait_for_that_never_comes_fails_with_its_step(self):
        from unittest import mock
        sc = os.path.join(self.dir, "sc.json")
        with open(sc, "w") as f:
            json.dump({"steps": [{"goto": self.page}, {"wait_for": "#r"}, {"shot": "a"}]}, f)
        with mock.patch.object(steps, "WAIT_FOR", 0.5):
            code, _, err = run(shot.main, ["--steps", sc, "--out", os.path.join(self.dir, "s.png")], self.dir)
        self.assertEqual(code, 2)
        self.assertIn(i18n.t("cli.proof.step_failed", n=2, error=i18n.t("cli.proof.wait_for_timeout", selector="#r",
                                                                       seconds="0.5")), err)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "s-a.png")))

    @unittest.skipUnless(FFMPEG, "ffmpeg is needed")
    def test_scenario_video_is_an_mp4(self):
        sc = os.path.join(self.dir, "sc.json")
        with open(sc, "w") as f:
            json.dump({"steps": [{"goto": self.page, "caption": "Opened"}, {"type": ["#q", "abc"]},
                                 {"click": "#go", "caption": "Save"}, {"wait_for": "#r"}, {"press": "Enter"}]}, f)
        out = os.path.join(self.dir, "run.mp4")
        code, printed, err = run(video.main, [sc, "--out", out], {"video": {"enabled": True}}, self.dir)
        self.assertEqual(code, 0, err)
        self.assertIn(out, printed)
        with open(out, "rb") as f:
            self.assertEqual(f.read(12)[4:8], b"ftyp")

    @unittest.skipUnless(FFMPEG, "ffmpeg is needed")
    def test_a_failed_step_names_its_number(self):
        sc = os.path.join(self.dir, "sc.json")
        with open(sc, "w") as f:
            json.dump({"steps": [{"goto": self.page}, {"click": "#nope"}]}, f)
        code, _, err = run(video.main, [sc], {"video": {"enabled": True}}, self.dir)
        self.assertEqual(code, 2)
        self.assertIn(i18n.t("cli.proof.step_failed", n=2, error="no element #nope"), err)


if __name__ == "__main__":
    unittest.main()
