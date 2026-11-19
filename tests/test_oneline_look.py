"""What the one-line install shows in a terminal (deploy/oneline/README.md, "The look"): install.sh draws steps 1 to 3 and the
sign, setup.py (look.py) steps 4 and 5 and the closing screen. The screen is replayed on a small terminal
(tools/dev/termshot.py's Screen) and read as a person would: what is left on it, not what was written. Not a terminal,
NO_COLOR, TERM=dumb, a locale that is not UTF-8: no colour, no sign, no escapes — or the lines as they were."""
import tests  # noqa: F401 - keeps the machine's settings out
import contextlib
import importlib.util
import io
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import platforms

from tests.fakes import cut
from tests.test_oneline import Stand
from tests.test_oneline_setup import Base

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ONELINE = os.path.join(ROOT, "deploy", "oneline")
BASH = shutil.which("bash")


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SHOT = module("hub_test_termshot", os.path.join(ROOT, "tools", "dev", "termshot.py"))
LOOK = module("hub_test_look", os.path.join(ONELINE, "look.py"))
SGR = re.compile(r"\x1b\[[0-9;]*m")


def screen_of(text, cols=80, rows=30):
    screen = SHOT.Screen(cols, rows)
    screen.feed(text.replace("\n", "\r\n"))
    return screen


def lines_of(screen):
    return ["".join(ch for ch, _ in row).rstrip() for row in screen.cells]


def run_on_terminal(cmd, env, cols=80, rows=24):
    """cmd on a terminal of its own (a pty) -> what it wrote."""
    import fcntl
    import pty
    import termios
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=slave, stderr=slave, env=env, close_fds=True)
    os.close(slave)
    got = b""
    while True:
        try:
            chunk = os.read(master, 65536)
        except OSError:
            break
        if not chunk:
            break
        got += chunk
    os.close(master)
    p.wait()
    return got.decode("utf-8", "replace"), p.returncode


def redraws(text):
    """How many times the live part of 9 lines was drawn again in place (not taken off: that is `ESC[9A CR ESC[J`)."""
    return len(re.findall(r"\x1b\[9A(?!\r\x1b\[J)", text))


class Tty(io.StringIO):
    def isatty(self):
        return True


class Picture(unittest.TestCase):
    def test_the_pictures_in_the_scripts_are_the_ones_the_generator_draws(self):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "dev", "logo.py"), "--check"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_the_sign_is_a_wave_of_frames_a_finished_sign_and_a_failed_one(self):
        self.assertEqual(len(LOOK.LOGO_FRAMES), 8)
        self.assertEqual(len({*LOOK.LOGO_FRAMES}), 8)   # each frame is another picture: the wave moves
        for pic in (*LOOK.LOGO_FRAMES, LOOK.LOGO_DONE, LOOK.LOGO_FAIL):
            self.assertEqual(len(pic), LOOK.LOGO_W * LOOK.LOGO_H)
            self.assertTrue(set(pic) <= set(LOOK.LOGO_PALETTE) | {"."}, set(pic) - set(LOOK.LOGO_PALETTE))
        self.assertLessEqual(LOOK.LOGO_H // 2, 7)    # lines of text
        self.assertLessEqual(LOOK.LOGO_W, 16)        # columns
        self.assertIn("K", LOOK.LOGO_FAIL)           # the failed sign has a dark ray, on a tile dimmer than the sign's
        self.assertIn("X", LOOK.LOGO_FAIL)
        self.assertNotIn("K", "".join(LOOK.LOGO_FRAMES) + LOOK.LOGO_DONE)
        self.assertNotIn("X", "".join(LOOK.LOGO_FRAMES) + LOOK.LOGO_DONE)

    def test_the_sign_is_the_tab_icon_s_tile_and_white(self):
        with open(os.path.join(ROOT, "modules", "web", "static", "favicon", "tab.svg"), encoding="utf-8") as f:
            svg = f.read().lower()
        self.assertIn("#%02x%02x%02x" % LOOK.LOGO_PALETTE["T"][0], svg)   # the tile is the svg's orange
        self.assertEqual(LOOK.LOGO_PALETTE["W"][0], (255, 255, 255))      # the dots are its white

    def test_the_generator_draws_from_any_svg_of_the_same_make(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mark.svg")
            with open(path, "w", encoding="utf-8") as f:
                f.write('<svg viewBox="0 0 32 32"><rect width="32" height="32" rx="8" fill="#102030"/><g fill="#fff">'
                        '<circle cx="19" cy="16" r="2"/><circle cx="23" cy="16" r="1.4"/><circle cx="27" cy="16" r="1"/>'
                        '<circle cx="16" cy="19" r="2"/><circle cx="16" cy="23" r="1.4"/><circle cx="16" cy="27" r="1"/></g></svg>')
            r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "dev", "logo.py"), "--preview", "--source", path],
                               capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("38;2;16;32;48", r.stdout)   # the tile of this svg


class Termshot(unittest.TestCase):
    def test_a_recording_cut_anywhere_by_its_timing_is_the_same_screen(self):
        # script writes chunks as the pty gave them: a letter of 3 bytes or a colour escape can end one chunk and begin the next
        body = "ab\x1b[38;2;224;80;16m▀▄\x1b[0m é\r\n\x1b[1A\x1b[2Kdone ✓\n".encode("utf-8")
        data = b"Script started on 2026-01-01 [COMMAND=\"x\"]\n" + body
        whole = SHOT.replay(data, "", None, None, 20, 4)
        timing = "\n".join(f"0.01 {1}" for _ in body)   # every byte a chunk of its own
        cut = SHOT.replay(data, timing, None, None, 20, 4)
        self.assertEqual(cut.cells, whole.cells)
        self.assertEqual(lines_of(cut)[0], "done ✓")
        self.assertEqual(lines_of(SHOT.replay(data, timing, 0.035, None, 20, 4))[0], "ab")   # cut by the time: 3 bytes
        # an escape that never finishes holds nothing back, and the end of the recording says what was held
        for body in (b"x\x1b[\nyz", b"ab\x1b[3", "x é".encode("utf-8")[:-1]):
            timing = "\n".join("0.01 1" for _ in body)
            self.assertEqual(SHOT.replay(body, timing, None, None, 20, 4).cells, SHOT.replay(body, "", None, None, 20, 4).cells,
                             body)


class Words(unittest.TestCase):
    def test_what_install_sh_hands_over_is_read_back(self):
        self.assertEqual(LOOK.parse("3.80.24.4.0"), (3, 80, 24, 4, 0))
        self.assertEqual(LOOK.parse("1.120.50.5.9"), (1, 80, 50, 5, 9))   # at most 80 columns
        self.assertEqual(LOOK.parse("2.60.20.1.0"), (2, 60, 20, 1, 0))
        for bad in ("", None, "0.80.24.4.0", "4.80.24.4.0", "3.80.24.6.0", "3.59.24.4.0", "3.80.24.4", "3.80.4.0", "c.3.80.24.4.0"):
            self.assertIsNone(LOOK.parse(bad), bad)

    def test_nothing_is_drawn_unless_the_output_is_a_terminal(self):
        names = {4: "Set up", 5: "Start"}
        self.assertIsNone(LOOK.Look.from_env({"HUB_INSTALL_UI": "3.80.24.4.0"}, io.StringIO(), names))
        self.assertIsNone(LOOK.Look.from_env({}, Tty(), names))
        self.assertIsNotNone(LOOK.Look.from_env({"HUB_INSTALL_UI": "3.80.24.4.0"}, Tty(), names))

    def test_a_stream_that_cannot_write_the_marks_gets_the_lines_as_they_were(self):
        class Ascii(Tty):
            encoding = "ascii"
        self.assertIsNone(LOOK.Look.from_env({"HUB_INSTALL_UI": "3.80.24.4.0"}, Ascii(), {4: "Set up", 5: "Start"}))


class Screen(unittest.TestCase):
    NAMES = {1: "Check", 2: "Download", 3: "Unpack", 4: "Set up", 5: "Start", "fail": "The install stopped", "log": "The log"}

    def look(self, colours=3, cols=80, rows=24, ticker=False):
        out = Tty()
        ui = LOOK.Look(colours, cols, rows, 4, 0, out, self.NAMES, clock=iter(range(0, 1000, 3)).__next__, ticker=ticker)
        return ui, out

    def test_the_live_part_is_taken_off_and_drawn_again_under_what_is_said(self):
        ui, out = self.look()
        ui.begin(4, "putting the hub in place")
        ui.say("Downloading the hub's code 1.9.9 (15 MB)...")
        ui.say("a second line")
        rows = [r for r in lines_of(screen_of(out.getvalue())) if r]
        said = [i for i, r in enumerate(rows) if "Downloading" in r or "a second line" in r]
        self.assertEqual(said, [0, 1])   # above the live part, nothing of it left behind
        self.assertEqual(sum("Hub by Icons8" in r for r in rows), 1)   # one head, not one for each thing said
        self.assertEqual(sum("4/5" in r for r in rows), 1)
        self.assertIn("putting the hub in place", rows[-1])
        self.assertEqual(ui.tail, LOOK.LOGO_H // 2 + 2)

    def test_a_finished_step_stays_and_the_closing_screen_has_the_address(self):
        ui, out = self.look()
        ui.begin(4)
        ui.done("the hub's code")
        ui.begin(5)
        ui.done("the page answers on port 8787")
        ui.finish("The hub is running", [("Open", "http://localhost:8787/"), ("Command", "hub")], ["A note."],
                  verbatim=["ssh -N -L 8787:127.0.0.1:8787 me@host"])
        rows = lines_of(screen_of(out.getvalue()))
        text = "\n".join(rows)
        self.assertRegex(text, r"✓ 4/5 +Set up +the hub's code +\d+ s")
        self.assertRegex(text, r"✓ 5/5 +Start")
        self.assertIn("The hub is running", text)
        self.assertRegex(text, r"Open +http://localhost:8787/")
        self.assertIn("ssh -N -L 8787:127.0.0.1:8787 me@host", text)
        self.assertEqual(sum("Hub by Icons8" in r for r in rows), 0)   # the head went with the live part
        self.assertEqual(ui.tail, 0)
        for line in rows:
            self.assertLessEqual(len(line), 80)

    def test_a_failure_shows_the_step_and_the_words_and_leaves_nothing_live(self):
        ui, out = self.look()
        ui.begin(5)
        ui.fail("The hub did not answer in 90 s. Its log: /x/hub-up.log", log="/x/install.log")
        text = "\n".join(lines_of(screen_of(out.getvalue())))
        self.assertRegex(text, r"✗ 5/5 +Start")
        self.assertIn("The install stopped", text)
        self.assertIn("did not answer in 90 s", text)
        self.assertIn("The log  /x/install.log", text)
        self.assertNotIn("›", text)   # no step in progress any more
        self.assertEqual(ui.tail, 0)

    def test_sixteen_colours_have_the_steps_and_a_frame_and_not_the_sign(self):
        ui, out = self.look(colours=1)
        self.assertFalse(ui.pic)
        ui.begin(4)
        ui.done("the hub's code")
        ui.finish("The hub is running", [("Open", "http://localhost:8787/")], [])
        raw = out.getvalue()
        self.assertNotIn("▀", raw)
        self.assertNotIn("▄", raw)
        self.assertIn("\x1b[32m", raw)   # the 16 colours' green
        self.assertNotIn("38;5;", raw)
        text = "\n".join(lines_of(screen_of(raw)))
        self.assertIn("╭", text)
        self.assertIn("The hub is running", text)

    def test_after_a_failure_nothing_live_is_drawn_again_under_it(self):
        ui, out = self.look()
        ui.begin(5)
        ui.fail("The hub did not answer.")
        ui.say("a line of the undo")   # setup.py goes on after a failure: it says what it takes back
        ui.pause()
        ui.resume()
        rows = lines_of(screen_of(out.getvalue(), rows=40))
        text = "\n".join(rows)
        self.assertIn("a line of the undo", text)
        self.assertEqual(sum("Hub by Icons8" in r for r in rows), 0)   # no second sign, no step in progress
        self.assertNotIn("›", text)
        self.assertEqual(ui.tail, 0)

    def test_a_step_that_is_done_does_not_fail_again(self):
        ui, out = self.look()
        ui.begin(4)
        ui.done("the hub's code")
        ui.fail("The hub's service did not come up.")
        text = "\n".join(lines_of(screen_of(out.getvalue(), rows=40)))
        self.assertRegex(text, r"✓ 4/5 +Set up")
        self.assertNotIn("✗", text)
        self.assertIn("did not come up", text)

    def test_a_window_too_low_for_the_sign_has_the_steps_only(self):
        ui, out = self.look(rows=19)   # install.sh draws no sign under 20 rows: this half does the same
        self.assertFalse(ui.pic)
        ui.begin(4, "putting the hub in place")
        ui.done("x")
        self.assertNotIn("▀", out.getvalue())
        self.assertTrue(self.look(rows=20)[0].pic)

    def test_a_step_that_is_done_leaves_no_live_part_until_the_next_begins(self):
        ui, out = self.look()
        ui.begin(4)
        ui.done("the hub's code")
        ui.say("a line between the steps")
        ui.pause()
        ui.resume()
        text = "\n".join(lines_of(screen_of(out.getvalue(), rows=40)))
        self.assertEqual(text.count("4/5"), 1)   # the line of the finished step, not a second one as in progress
        self.assertNotIn("›", text)
        self.assertEqual(ui.tail, 0)
        ui.begin(5)
        self.assertEqual(ui.tail, LOOK.LOGO_H // 2 + 2)

    def test_wide_letters_take_two_columns_and_the_live_line_fits_the_window(self):
        self.assertEqual((LOOK.cells("abc"), LOOK.cells("設定"), LOOK.cells("é")), (3, 4, 1))
        ui, out = self.look(cols=60)
        ui.names = {**self.NAMES, 4: "セットアップ", 5: "起動"}
        ui.begin(4, "ハブのコードとコマンド、サービスを所定の場所に置いています。これには少し時間がかかります")
        line = [r for r in lines_of(screen_of(out.getvalue(), cols=100, rows=40)) if "4/5" in r][0]
        self.assertLessEqual(LOOK.cells(line), 60)
        ui.done("ハブのコード")
        ui.finish("ハブが動いています", [("開く", "http://localhost:8787/"), ("コマンド", "hub")], [])
        rows = lines_of(screen_of(out.getvalue(), cols=100, rows=40))
        self.assertTrue(all(LOOK.cells(r) <= 60 for r in rows), rows)
        ui.close()

    def test_words_of_wide_letters_are_wrapped_by_the_columns_they_take(self):
        # Japanese has no spaces to break at and each letter takes two columns: a failure, a line said above the steps and
        # a note of the closing screen stay in the window (a line wider than it would be wrapped by the terminal under the sign)
        long = "ハブのページが応答しませんでした。ログを確認してから、もう一度インストールの行を実行してください" * 2
        for colours, rows in ((3, 24), (1, 24)):
            ui, out = self.look(colours=colours, cols=60, rows=rows)
            ui.begin(4)
            ui.say(long)
            ui.fail(long, log="/home/x/install.log")
            shown = lines_of(screen_of(out.getvalue(), cols=120, rows=60))
            self.assertTrue(all(LOOK.cells(r) <= 60 for r in shown), [r for r in shown if LOOK.cells(r) > 60])
            self.assertIn(long[:10], "".join(shown))
        ui, out = self.look(cols=60)
        ui.begin(4)
        ui.done()
        ui.finish("ハブが動いています", [("開く", "http://localhost:8787/")], [long])
        shown = lines_of(screen_of(out.getvalue(), cols=120, rows=60))
        self.assertTrue(all(LOOK.cells(r) <= 60 for r in shown), [r for r in shown if LOOK.cells(r) > 60])
        self.assertEqual("".join(r.strip(" ·") for r in shown).count(long), 1)   # every letter said, none lost at a cut
        self.assertEqual(LOOK.wrap("", 10), [])
        # words of Latin letters among wide ones break at their spaces, not in the middle; a joining mark stays with its letter
        self.assertEqual(LOOK.wrap("エラー: connection refused by 203.0.113.5", 14, "  "),
                         ["エラー:", "  connection", "  refused by", "  203.0.113.5"])
        self.assertEqual(LOOK.wrap("ハブのページが応答しません", 10), ["ハブのペー", "ジが応答し", "ません"])
        self.assertEqual(LOOK.wrap("abcdéfg", 5), ["abcdé", "fg"])
        self.assertEqual(LOOK.wrap("one two  three", 80), ["one two three"])

    def test_a_line_that_is_typed_is_said_whole(self):
        ui, out = self.look()
        ui.begin(4)
        line = "curl -fsSL https://example.test/i/" + "k" * 90 + "/ | bash -s -- --move"
        ui.say("A hub from git is here: move it with", verbatim=[line])
        self.assertIn("\n      " + line + "\n", out.getvalue())   # not cut into lines, not indented in the middle

    def test_a_question_takes_the_live_part_off_while_the_person_types(self):
        ui, out = self.look()
        ui.begin(4)
        ui.pause()
        out.write("Move or stay? ")
        ui.say("said while paused")   # drawn again by resume, not by say
        before = out.getvalue()
        ui.resume()
        self.assertNotIn("4/5", before[len(before) - 40:])
        self.assertGreater(len(out.getvalue()), len(before))
        self.assertEqual(ui.tail, LOOK.LOGO_H // 2 + 2)


    # --- the sign, its dots glowing in a wave while the work goes on

    def test_a_frame_replaces_the_picture_in_place_and_nothing_else_moves(self):
        ui, out = self.look()
        ui.begin(4, "putting the hub in place")
        first = lines_of(screen_of(out.getvalue()))
        before = ui.picture("work")
        ui.next_frame()
        ui.next_frame()
        rows = lines_of(screen_of(out.getvalue()))
        self.assertEqual(ui.tail, LOOK.LOGO_H // 2 + 2)
        self.assertEqual(sum("Hub by Icons8" in r for r in rows), 1)   # drawn over, not under
        self.assertEqual(sum("4/5" in r for r in rows), 1)
        self.assertEqual(len([r for r in rows if r]), len([r for r in first if r]))   # the same lines on the screen
        self.assertIn("putting the hub in place", [r for r in rows if r][-1])
        self.assertNotEqual(before, ui.picture("work"))   # ... but the dots glow elsewhere

    def test_the_frames_of_a_cycle_come_back_to_the_first(self):
        ui, out = self.look()
        ui.begin(4)
        seen = [ui.picture("work")]
        for _ in range(len(LOOK.LOGO_FRAMES)):
            ui.next_frame()
            seen.append(ui.picture("work"))
        self.assertEqual(seen[0], seen[-1])
        self.assertEqual(len({tuple(p) for p in seen[:-1]}), len(LOOK.LOGO_FRAMES))

    def test_the_sign_is_done_or_failed_as_it_is_and_no_frame_moves_it(self):
        ui, out = self.look()
        ui.begin(5)
        ui.fail("The hub did not answer.")
        before = out.getvalue()
        self.assertIn("38;2;40;32;36", before)   # the dark ray
        self.assertEqual(ui.tail, 0)
        ui2, out2 = self.look()
        ui2.begin(5)
        ui2.done("x")
        ui2.finish("The hub is running", [("Open", "http://localhost:8787/")], [])
        self.assertEqual(ui2.tail, 0)
        self.assertNotIn("38;2;40;32;36", out2.getvalue())
        self.assertIn("▀", out2.getvalue())

    def test_a_thread_draws_the_frames_while_there_is_a_live_part_and_stops_when_it_is_over(self):
        import time
        ui, out = self.look(ticker=True)
        with mock.patch.object(LOOK, "FPS", 100):   # a frame each 10 ms: a tenth of a second is ten of them
            self.ticking(ui, out, time)

    def ticking(self, ui, out, time):
        ui.begin(4, "putting the hub in place")
        deadline = time.monotonic() + 5
        while redraws(out.getvalue()) < 3 and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertGreaterEqual(redraws(out.getvalue()), 3)   # the picture is drawn again, 7 lines and the 2 under it
        ui.pause()
        time.sleep(0.1)
        quiet = len(out.getvalue())
        time.sleep(0.1)
        self.assertEqual(len(out.getvalue()), quiet)   # a question is on the screen: the picture does not move
        ui.resume()
        ui.done("x")
        ui.finish("The hub is running", [("Open", "http://localhost:8787/")], [])
        time.sleep(0.1)
        end = len(out.getvalue())
        time.sleep(0.1)
        self.assertEqual(len(out.getvalue()), end)   # over: no frame after the closing screen
        rows = lines_of(screen_of(out.getvalue(), rows=60))
        self.assertEqual(sum("Hub by Icons8" in r for r in rows), 0)
        self.assertIn("The hub is running", "\n".join(rows))

    def test_a_terminal_that_goes_away_ends_the_frames_and_nothing_else(self):
        import time

        class Gone(Tty):
            def write(self, text):
                if self.dead:
                    raise BrokenPipeError("the terminal is gone")
                return super().write(text)
        out = Gone()
        out.dead = False
        ui = LOOK.Look(3, 80, 24, 4, 0, out, self.NAMES, ticker=True)
        ui.begin(4)
        out.dead = True
        deadline = time.monotonic() + 5
        while ui.ticker and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(ui.ticker)   # the thread is over (it did not raise into the install)
        ui.close()

    @unittest.skipUnless(os.name == "posix", "a pty")
    def test_from_env_moves_the_sign_only_on_a_terminal_of_the_system(self):
        names = {4: "Set up", 5: "Start"}
        self.assertFalse(LOOK.Look.from_env({"HUB_INSTALL_UI": "3.80.24.4.0"}, Tty(), names).ticker)   # a stand-in
        master, slave = __import__("pty").openpty()
        try:
            with os.fdopen(os.dup(slave), "w", encoding="utf-8") as tty:
                ui = LOOK.Look.from_env({"HUB_INSTALL_UI": "3.80.24.4.0"}, tty, names)
                self.assertTrue(ui.ticker)
        finally:
            os.close(master)
            os.close(slave)


@unittest.skipUnless(BASH and os.name == "posix", "a pty and bash")
class Shell(unittest.TestCase):
    def demo(self, *more, **env):
        base = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/"), "HUB_DEMO_FAST": "1", "TERM": "xterm-256color",
                "COLORTERM": "truecolor", "LANG": "en_US.UTF-8", **env}
        base = {k: v for k, v in base.items() if v is not None}
        return run_on_terminal([BASH, os.path.join(ONELINE, "demo.sh"), *more], base)

    def test_the_screen_ends_with_the_address_and_the_dots_glow_in_the_download(self):
        out, code = self.demo()
        self.assertEqual(code, 0, out[-400:])
        text = "\n".join(lines_of(screen_of(out, rows=30)))
        for n, name in ((1, "Check"), (2, "Download"), (3, "Unpack"), (4, "Set up"), (5, "Start")):
            self.assertRegex(text, rf"✓ {n}/5 +{name}")
        self.assertIn("The hub is running", text)
        self.assertNotIn("›", text)
        self.assertIn("\x1b[38;2;224;80;16", out)   # the orange of the tile, in 24 bits
        self.assertRegex(text, r"Open +http://localhost:8787/")
        self.assertGreaterEqual(redraws(out), 3)   # the live part drawn again in one write, the sign moving in the bar's time
        self.assertEqual(len(text.split("Hub by Icons8")), 1)
        faces = {"\n".join(chunk.split("\n")[:7]) for chunk in re.split(r"\x1b\[9A(?!\r\x1b\[J)", out)[1:]}
        self.assertGreaterEqual(len(faces), 3)   # the picture of each redraw is another frame of the wave

    def test_the_times_of_the_five_steps_end_in_one_column(self):
        # the steps 1-3 are install.sh's, 4-5 look.py's: one screen, one column of times, the same mark of a cut detail
        for cols in (60, 80, 100):
            out, code = run_on_terminal([BASH, os.path.join(ONELINE, "demo.sh")], {
                "PATH": os.environ["PATH"], "HUB_DEMO_FAST": "1", "TERM": "xterm-256color", "COLORTERM": "truecolor",
                "LANG": "en_US.UTF-8"}, cols=cols, rows=40)
            self.assertEqual(code, 0, out[-300:])
            steps = [r for r in lines_of(screen_of(out, cols=cols, rows=40)) if re.match(r"  ✓ [1-5]/5 ", r)]
            self.assertEqual(len(steps), 5, steps)
            self.assertEqual({len(r) for r in steps}, {min(cols, 80) - 2}, steps)
        plain = SGR.sub("", LOOK.Look(3, 60, 24, 4, 0, Tty(), {}).step_line(LOOK.GOOD, "✓", 4, "Set up", "x" * 80, "3 s"))
        self.assertEqual((LOOK.cells(plain), plain[-6:]), (58, "…  3 s"))
        shown = self.look_sh('UI_T0=$SECONDS; ui_line good "$UI_OK" 1 Check "$(printf "%080d" 0)" "3 s"; printf "%s" "$UI_S"',
                             cols=60)
        plain = SGR.sub("", shown)
        self.assertEqual((LOOK.cells(plain), plain[-6:]), (58, "…  3 s"))

    def test_a_frame_from_the_cache_is_the_frame_as_it_was_drawn(self):
        # the second turn of the wave, the finished and the failed sign come from the cache: line for line the same
        # (the bash of macOS is 3.2 - the Mac stand runs this with it)
        out = self.look_sh(
            'ok=1; f=0; while [ $f -lt $UI_NFRAMES ]; do UI_FRAME=$f; ui_art work; a[$f]="${UI_LINES[*]}"; f=$((f + 1)); done; '
            'ui_art done; d="${UI_LINES[*]}"; ui_art fail; x="${UI_LINES[*]}"; '
            'f=0; while [ $f -lt $UI_NFRAMES ]; do UI_FRAME=$f; ui_art work; '
            '[ "${#UI_LINES[@]}" = 7 ] && [ "${UI_LINES[*]}" = "${a[$f]}" ] || ok=; f=$((f + 1)); done; '
            'ui_art done; [ "${UI_LINES[*]}" = "$d" ] || ok=; ui_art fail; [ "${UI_LINES[*]}" = "$x" ] || ok=; '
            '[ "${#UI_LINES[@]}" = 7 ] || ok=; [ "$a" != "$d" ] || ok=; echo "cache:${ok:-differs}:$UI_NFRAMES"')
        self.assertIn("cache:1:8", out)

    def look_sh(self, script, cols=80):
        """A bash script run after ui_init of install.sh's look, on a terminal of cols columns -> what it wrote."""
        look = f"eval \"$(sed -n '/^# --- the look:/,/^# --- end of the look/p' {os.path.join(ONELINE, 'install.sh')})\"; ui_init; "
        out, code = run_on_terminal([BASH, "-c", look + script], {"PATH": os.environ["PATH"], "TERM": "xterm-256color",
                                                                   "COLORTERM": "truecolor", "LANG": "en_US.UTF-8"}, cols=cols)
        self.assertEqual(code, 0, out)
        return out

    def test_the_sign_in_256_colours_and_not_in_16(self):
        out, code = self.demo(COLORTERM=None)
        self.assertEqual(code, 0)
        self.assertIn("▀", out)
        self.assertNotIn("\x1b[38;2;", out)
        self.assertIn("\x1b[38;5;202m", out)
        out, code = self.demo(COLORTERM=None, TERM="xterm")
        self.assertEqual(code, 0)
        self.assertNotIn("▀", out)   # 16 colours: the steps only, the title above them and a frame at the end
        self.assertEqual(redraws(out), 0)
        text = "\n".join(lines_of(screen_of(out)))
        self.assertIn("Hub by Icons8", text)
        self.assertRegex(text, r"✓ 5/5 +Start")
        self.assertIn("╭", text)

    def test_a_failure_of_the_download_is_a_screen_of_its_own_with_the_failed_sign(self):
        out, code = self.demo("fail")
        self.assertEqual(code, 1)
        self.assertIn("\x1b[38;2;40;32;36", out)   # the dark ray
        text = "\n".join(lines_of(screen_of(out)))
        self.assertRegex(text, r"✗ 2/5 +Download")
        self.assertIn("The install stopped", text)
        self.assertIn("did not complete", text)
        self.assertNotIn("✓ 3/5", text)

    def nothing(self, **env):
        """The demo of the look on a terminal that gets the lines as they were: it says there is nothing to show."""
        out, code = self.demo(**env)
        self.assertEqual(code, 1, out[-300:])
        self.assertIn("nothing to show", out)
        self.assertNotIn("1/5", out)
        self.assertNotIn("\x1b[38", out)

    def test_every_reason_for_the_plain_lines_is_one(self):
        self.nothing(HUB_INSTALL_LOOK="plain")
        self.nothing(NO_COLOR="1")
        self.nothing(TERM="dumb")
        self.nothing(TERM=None)
        self.nothing(LANG="C")
        self.nothing(LANG=None)
        self.nothing(LC_ALL="C", LANG="en_US.UTF-8")

    def test_a_window_under_60_columns_gets_the_plain_lines_and_60_does_not(self):
        env = {"PATH": os.environ["PATH"], "HUB_DEMO_FAST": "1", "TERM": "xterm-256color", "COLORTERM": "truecolor",
               "LANG": "en_US.UTF-8"}
        out, code = run_on_terminal([BASH, os.path.join(ONELINE, "demo.sh")], env, cols=59)
        self.assertEqual(code, 1)
        self.assertIn("nothing to show", out)
        out, code = run_on_terminal([BASH, os.path.join(ONELINE, "demo.sh")], env, cols=60)
        self.assertEqual(code, 0, out[-300:])
        for line in lines_of(screen_of(out, cols=60)):
            self.assertLessEqual(len(line), 60)

    def test_the_download_line_with_its_speed_fits_a_window_of_60_columns(self):
        # the longest the line gets (a speed, 72 % of the file); a line that wrapped would leave its bar behind the next redraw
        with tempfile.TemporaryDirectory() as tmp:
            probe = os.path.join(tmp, "probe.sh")
            with open(probe, "w") as f:
                f.write('eval "$(sed -n \'/^# --- the look:/,/^# --- end of the look/p\' "$1")"\n'
                        'ui_init; SHELL_SIZE=208000000; n=0\n'
                        'kill() { n=$((n + 1)); [ "$n" -lt 12 ]; }; sleep() { SECONDS=$((SECONDS + 1)); }\n'   # time passes by itself
                        'dd if=/dev/null of="$2" bs=1 seek=150000000 2>/dev/null\n'
                        'ui_begin 2 Download "208 MB"; ui_download_bar 1 "$2" 0; ui_done "verified"\n')
            env = {"PATH": os.environ["PATH"], "TERM": "xterm-256color", "COLORTERM": "truecolor", "LANG": "en_US.UTF-8"}
            out, code = run_on_terminal([BASH, probe, os.path.join(ONELINE, "install.sh"), os.path.join(tmp, "part")], env, cols=60)
        text = "\n".join(lines_of(screen_of(out, cols=60)))
        self.assertIn("MB/s", out)
        self.assertRegex(text, r"✓ 2/5 +Download")
        self.assertEqual(len([r for r in text.split("\n") if r]), 1, text)   # nothing of the live part is left on the screen

    def test_ctrl_c_takes_the_live_part_off_and_gives_the_colours_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            probe = os.path.join(tmp, "probe.sh")
            with open(probe, "w") as f:
                f.write('eval "$(sed -n \'/^# --- the look:/,/^# --- end of the look/p\' "$1")"\n'
                        'ui_init; ui_begin 2 Download "208 MB"; ui_interrupted; echo "[prompt]"\n')
            env = {"PATH": os.environ["PATH"], "TERM": "xterm-256color", "COLORTERM": "truecolor", "LANG": "en_US.UTF-8"}
            out, code = run_on_terminal([BASH, probe, os.path.join(ONELINE, "install.sh")], env)
        text = "\n".join(lines_of(screen_of(out)))
        self.assertEqual(text.strip(), "[prompt]", text)   # the sign and the step line are gone

    def test_a_refusal_after_a_finished_step_does_not_fail_that_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            probe = os.path.join(tmp, "probe.sh")
            with open(probe, "w") as f:
                f.write('eval "$(sed -n \'/^# --- the look:/,/^# --- end of the look/p\' "$1")"\n'
                        'ui_init; ui_begin 1 Check "this computer"; ui_done "linux-x64"; ui_fail "no room to write in"\n')
            env = {"PATH": os.environ["PATH"], "TERM": "xterm-256color", "COLORTERM": "truecolor", "LANG": "en_US.UTF-8"}
            out, code = run_on_terminal([BASH, probe, os.path.join(ONELINE, "install.sh")], env)
        text = "\n".join(lines_of(screen_of(out)))
        self.assertRegex(text, r"✓ 1/5 +Check")
        self.assertNotIn("✗", text)
        self.assertIn("no room to write in", text)

    def test_a_drawing_that_does_not_work_here_is_the_plain_lines(self):
        # a tool the drawing needs that does not do what it is asked: ui_init tries the sign and a line once, quietly
        with tempfile.TemporaryDirectory() as tmp:
            probe = os.path.join(tmp, "probe.sh")
            with open(probe, "w") as f:
                f.write('eval "$(sed -n \'/^# --- the look:/,/^# --- end of the look/p\' "$1")"\n'
                        'ui_logo_rgb() { return 1; }; ui_art() { echo broken >&2; return 1; }\n'   # the picture cannot be made
                        'ui_init; echo "[${UI_ON:-off}]"\n')
            env = {"PATH": os.environ["PATH"], "TERM": "xterm-256color", "COLORTERM": "truecolor", "LANG": "en_US.UTF-8"}
            out, code = run_on_terminal([BASH, probe, os.path.join(ONELINE, "install.sh")], env)
        self.assertIn("[off]", out)
        self.assertNotIn("broken", out)   # quiet: no screen of errors

    def test_a_piped_script_stays_ascii_and_the_picture_is_in_it_as_text(self):
        with open(os.path.join(ONELINE, "install.sh"), "rb") as f:
            data = f.read()
        data.decode("ascii")
        self.assertIn(b"UI_PIC_DONE=", data)


@unittest.skipUnless(BASH and os.name == "posix", "a pty and bash")
@cut.needs_deliver()
class Terminal(Stand):
    """install.sh itself, the whole of it against the stand, on a terminal: the look where there is one to draw, and
    exactly the lines it always said, by the same path, where there is not."""

    PLAIN = ("Downloading the hub's environment for linux-x64", "Unpacking...")

    def setUp(self):
        super().setUp()
        self.fake("uname", 'case "$1" in -s) echo Linux ;; -m) echo x86_64 ;; esac\n')
        self.fake("getconf", 'echo "glibc 2.39"\n')
        self.fake("df", 'echo "Filesystem 1024-blocks Used Available Capacity Mounted"; echo "x 1 1 99999999 1% /"\n')
        if not shutil.which("git"):
            self.fake("git", "exit 0\n")
        self.terminal = {"TERM": "xterm-256color", "COLORTERM": "truecolor", "LANG": "en_US.UTF-8"}

    def on_terminal(self, *args, cols=80, rows=24, **extra):
        env = self.env(**{**self.terminal, **extra})
        out, code = run_on_terminal([BASH, os.path.join(ROOT, "deploy", "oneline", "install.sh"), "--no-open", *args], env,
                                    cols=cols, rows=rows)
        return out, code

    def plain(self, out, code, why):
        self.assertEqual(code, 0, f"{why}: {out[-300:]}")
        for words in self.PLAIN:
            self.assertIn(words, out, why)
        self.assertNotIn("\x1b", out, why)
        self.assertNotIn("1/5", out, why)
        self.assertFalse(self.got()["ui"], why)   # nothing is told to setup.py
        argv = self.got()["argv"]
        self.assertEqual(argv[:2], ["--contract", "1"], why)   # the same hand-over as ever
        self.assertEqual(argv[-2:], ["--no-open", "--no-input"], why)
        self.assertIn("linux-x64", argv, why)

    def test_a_terminal_that_can_draw_gets_the_steps_and_setup_py_is_told(self):
        out, code = self.on_terminal()
        self.assertEqual(code, 0, out[-300:])
        text = "\n".join(lines_of(screen_of(out, rows=30)))
        self.assertRegex(text, r"✓ 1/5 +Check")
        self.assertRegex(text, r"✓ 2/5 +Download +\d+ MB")
        self.assertRegex(text, r"✓ 3/5 +Unpack")
        self.assertNotIn("›", text)
        self.assertNotIn("Downloading the hub's environment", text)   # the live line says it
        self.assertRegex(self.got()["ui"], r"^3\.80\.24\.4\.0$")
        argv = self.got()["argv"]
        self.assertEqual(argv[:2], ["--contract", "1"])   # the same hand-over as ever
        self.assertEqual(argv[-2:], ["--no-open", "--no-input"])

    def test_a_low_window_has_no_sign_and_its_height_is_handed_over(self):
        out, code = self.on_terminal(rows=15)
        self.assertEqual(code, 0, out[-300:])
        text = "\n".join(lines_of(screen_of(out, rows=30)))
        self.assertRegex(text, r"✓ 3/5 +Unpack")
        self.assertNotIn("▀", out)   # the steps and no sign: install.sh does not draw it under 20 rows ...
        self.assertRegex(self.got()["ui"], r"^3\.80\.15\.4\.0$")   # ... and tells setup.py the height, which does the same

    def test_every_reason_for_the_plain_lines_gets_them_and_the_same_install(self):
        for why, env, cols in (("NO_COLOR", {"NO_COLOR": "1"}, 80), ("TERM=dumb", {"TERM": "dumb"}, 80),
                               ("no TERM", {"TERM": None}, 80), ("an ASCII locale", {"LANG": "C"}, 80),
                               ("a window of 59 columns", {}, 59), ("HUB_INSTALL_LOOK=plain", {"HUB_INSTALL_LOOK": "plain"}, 80)):
            with self.subTest(why):
                platforms.current.rmtree(self.root)   # a new machine each time: the environment is downloaded
                out, code = self.on_terminal(cols=cols, **env)
                self.plain(out, code, why)

    def test_a_pipe_gets_the_plain_lines_too(self):
        r = self.install("--no-open", **self.terminal)
        self.assertEqual(r.returncode, 0, r.stderr)
        for words in self.PLAIN:
            self.assertIn(words, r.stderr)
        self.assertNotIn("\x1b", r.stderr)
        self.assertFalse(self.got()["ui"])

    def test_a_refusal_is_a_screen_of_its_own_and_a_line_in_the_plain_way(self):
        out, code = self.on_terminal(HUB_DELIVER_URL="http://127.0.0.1:9")
        self.assertEqual(code, 1)
        text = "\n".join(lines_of(screen_of(out)))
        self.assertRegex(text, r"✗ 1/5 +Check")
        self.assertIn("The install stopped", text)
        self.assertIn("no answer from the delivery server", text)
        out, code = self.on_terminal(HUB_DELIVER_URL="http://127.0.0.1:9", NO_COLOR="1")
        self.assertEqual(code, 1)
        self.assertIn("Hub by Icons8 install: no answer from the delivery server", out)
        self.assertNotIn("\x1b", out)


class Setup(Base):
    """setup.py of the line, told by install.sh to draw the steps 4 and 5 (HUB_INSTALL_UI) on a terminal."""

    UI = {"HUB_INSTALL_UI": "3.80.24.4.0"}

    def test_the_last_two_steps_and_the_closing_screen(self):
        out = Tty()
        self.assertEqual(self.install(environ=self.UI, out=out), 0, out.getvalue())
        text = "\n".join(lines_of(screen_of(out.getvalue(), rows=40)))
        self.assertRegex(text, r"✓ 4/5 +Set up")
        self.assertRegex(text, r"✓ 5/5 +Start +the page answers on port 8787")
        self.assertIn("The hub is running", text)
        self.assertRegex(text, r"Open +http://127.0.0.1:8787/")
        self.assertRegex(text, r"Command +hub")
        self.assertNotIn("Starting the hub", text)
        self.assertNotIn("›", text)
        self.assertNotIn("in one place", text)   # the head of the live part is gone
        self.assertIn("▀", out.getvalue())

    def test_no_terminal_no_look_the_lines_stay_as_they_were(self):
        self.assertEqual(self.install(environ=self.UI), 0, self.out)   # a StringIO is no terminal
        self.assertIn("The hub is running: http://127.0.0.1:8787/", self.out)
        self.assertIn("Starting the hub...", self.out)
        self.assertNotIn("\x1b", self.out)

    def test_an_older_script_that_tells_nothing_gets_the_lines_too(self):
        out = Tty()
        self.assertEqual(self.install(out=out), 0, out.getvalue())
        self.assertIn("The hub is running: http://127.0.0.1:8787/", out.getvalue())
        self.assertNotIn("\x1b", out.getvalue())

    def test_a_page_that_does_not_answer_is_the_screen_of_a_failure(self):
        self.up[0] = False
        out = Tty()
        self.assertEqual(self.install(environ=self.UI, out=out), 4, out.getvalue())
        text = "\n".join(lines_of(screen_of(out.getvalue(), rows=40)))
        self.assertRegex(text, r"✗ 5/5 +Start")
        self.assertIn("The install stopped", text)
        self.assertIn("did not answer", text)

    def test_a_drawing_that_fails_in_the_middle_is_the_lines_as_they_were_and_the_install_goes_on(self):
        out = Tty()
        calls = []
        real = LOOK.Look.done

        def broken(look, *args, **kw):
            calls.append(args)
            raise ValueError("the picture does not work here")
        closed = []
        with mock.patch.object(LOOK.Look, "done", broken), mock.patch.object(self.setup, "_look_module", return_value=LOOK), \
                mock.patch.object(LOOK.Look, "close", lambda look: closed.append(1)):
            self.assertEqual(self.install(environ={"HUB_INSTALL_UI": "3.80.24.4.0"}, out=out), 0, out.getvalue())
        self.assertTrue(calls)
        self.assertTrue(closed)   # no more frames of the sign after the first failure
        text = out.getvalue()
        self.assertIn("The hub is running: http://127.0.0.1:8787/", text)   # the closing lines as they were
        with open(os.path.join(self.state, "..", ".config", "systemd", "user", "agents-hub.service"), encoding="utf-8") as f:
            self.assertIn("ExecStart=", f.read())   # the install went the whole way
        self.assertIs(real, LOOK.Look.done)

    def test_a_step_the_install_leaves_neither_done_nor_failed_keeps_its_line(self):
        # the hub went back to the environment it had (NOT_UP): the screen ends with 5/5 marked as not done, nothing live
        from tests import test_oneline_setup as S
        write = S.write
        self.assertEqual(self.install(), 0, self.out)   # a first run: the hub is up
        self.run_.calls.clear()
        self.run_.active = self.running = True
        write(os.path.join(self.env, ".agents-hub-shell.json"), "{}")
        was, self.env = self.env, S.EnvAgain.another(self, "1-open")
        self.up = [False]

        def back(job, env, real=self.setup.Install.back_to):
            self.up[0] = True
            return real(job, env)
        out = Tty()
        with mock.patch.object(self.setup.Install, "back_to", back):
            self.assertEqual(self.install(environ=self.UI, out=out), self.setup.NOT_UP, out.getvalue())
        text = "\n".join(lines_of(screen_of(out.getvalue(), rows=40)))
        self.assertRegex(text, r"✓ 4/5 +Set up")
        self.assertRegex(text, r"– 5/5 +Start")
        self.assertNotIn("›", text)
        self.assertNotIn("✓ 5/5", text)

    def test_a_look_that_failed_is_not_handed_to_the_newer_installer(self):
        S = sys.modules[Base.__module__]
        self.release("9.9.4")
        seen, real = [], S.Recorder.__call__

        def run(rec, cmd, **kw):
            if cmd[3:4] and cmd[3].endswith(os.path.join("deploy", "oneline", "setup.py")):
                seen.append(os.environ.get("HUB_INSTALL_UI"))
            return real(rec, cmd, **kw)

        def broken(look, *args, **kw):
            raise ValueError("the picture does not work here")
        for say, want in ((None, "3.80.24.4.0"), (broken, None)):
            seen.clear()
            with mock.patch.dict(os.environ, self.UI), mock.patch.object(S.Recorder, "__call__", run), \
                    mock.patch.object(self.setup, "_look_module", return_value=LOOK), \
                    contextlib.ExitStack() as stack:
                if say:
                    stack.enter_context(mock.patch.object(LOOK.Look, "say", say))
                self.assertEqual(self.install(environ=self.UI, out=Tty()), 0, self.out)
            self.assertEqual(seen, [want])   # drawn here: the newer one draws on; failed here: the plain lines there too

    def test_a_word_that_is_not_the_look_s_is_taken_for_none(self):
        out = Tty()
        with mock.patch.object(self.setup, "_look_module", side_effect=OSError("no look.py")):
            self.assertEqual(self.install(environ=self.UI, out=out), 0)
        self.assertIn("The hub is running: http://127.0.0.1:8787/", out.getvalue())


if __name__ == "__main__":
    unittest.main()
