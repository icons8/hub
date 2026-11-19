"""What the one-line install shows in a terminal (deploy/oneline/README.md, "The look"): the five steps as a list, the
step in progress as a live last line, the hub's sign beside the words, and the closing screen. install.sh draws
the first three steps and hands the screen over to setup.py in HUB_INSTALL_UI; this module draws the rest, the same way.

HUB_INSTALL_UI = <colours>.<columns>.<rows>.<step>.<tail>
  colours   1 of 16, 2 of 256, 3 of 24 bits (install.sh draws nothing for NO_COLOR, a TERM that is dumb or none, a locale
            that is not UTF-8, a window under 60 columns: the lines as they were); the hub's sign, its dots glowing in a
            wave while the work goes on, is drawn from 256 colours up (again 8 times a second by a thread of this module)
  columns   the width the lines are made for (60 to 80)
  rows      the height of the window: the sign needs 20
  step      the step in progress when the script handed over (1..5)
  tail      the lines of the screen's live part (the sign with its words, the step line) — they are taken off
            and drawn again each time something is said above them
Without the variable, when the output is not a terminal or cannot write the marks, nothing is drawn here: setup.py says
what it always said (the tests read those words); and a drawing that fails in the middle turns itself off there, the
install goes on (setup.py `_ui`). The stdlib only; the sign is drawn by tools/dev/logo.py."""
import os
import re
import sys
import threading
import time
import unicodedata

STEPS = 5
UI = re.compile(r"([1-3])\.(\d{2,3})\.(\d{1,3})\.([1-5])\.(\d{1,2})\Z")

# logo:begin (tools/dev/logo.py writes this; a test holds it equal to install.sh's)
LOGO_W, LOGO_H = 14, 14
LOGO_PALETTE = {
    'T': ((224, 80, 16), 166),
    'a': ((229, 109, 56), 167),
    'b': ((234, 138, 96), 173),
    'c': ((240, 168, 136), 216),
    'd': ((245, 197, 175), 223),
    'e': ((250, 226, 215), 224),
    'W': ((255, 255, 255), 231),
    'X': ((164, 92, 60), 131),
    'g': ((191, 141, 118), 138),
    'K': ((40, 32, 36), 235),
}
LOGO_FRAMES = (
    '.TTTTTTTTTTTT.TTTTTbTTbTTTTTTTTTTTTTTTTTTTTTTbTTccTTbTTTTTTTcTddTcTTTTTbTTTdTTdTTTbTTTTcdTTTTdcTTTTTTcdTTTTdcTTTTbTTTdTTdTTTbTTTTTcTddTcTTTTTTTbTTccTTbTTTTTTTTTTTTTTTTTTTTTTbTTbTTTTT.TTTTTTTTTTTT.',
    '.TTTTTTTTTTTT.TTTTTbTTbTTTTTTTTTTTTTTTTTTTTTTbTTccTTbTTTTTTTcTeeTcTTTTTbTTTeTTeTTTbTTTTceTTTTecTTTTTTceTTTTecTTTTbTTTeTTeTTTbTTTTTcTeeTcTTTTTTTbTTccTTbTTTTTTTTTTTTTTTTTTTTTTbTTbTTTTT.TTTTTTTTTTTT.',
    '.TTTTTTTTTTTT.TTTTTbTTbTTTTTTTTTTTTTTTTTTTTTTbTTccTTbTTTTTTTcTWWTcTTTTTbTTTWTTWTTTbTTTTcWTTTTWcTTTTTTcWTTTTWcTTTTbTTTWTTWTTTbTTTTTcTWWTcTTTTTTTbTTccTTbTTTTTTTTTTTTTTTTTTTTTTbTTbTTTTT.TTTTTTTTTTTT.',
    '.TTTTTTTTTTTT.TTTTTbTTbTTTTTTTTTTTTTTTTTTTTTTbTTddTTbTTTTTTTdTeeTdTTTTTbTTTeTTeTTTbTTTTdeTTTTedTTTTTTdeTTTTedTTTTbTTTeTTeTTTbTTTTTdTeeTdTTTTTTTbTTddTTbTTTTTTTTTTTTTTTTTTTTTTbTTbTTTTT.TTTTTTTTTTTT.',
    '.TTTTTTTTTTTT.TTTTTcTTcTTTTTTTTTTTTTTTTTTTTTTcTTeeTTcTTTTTTTeTddTeTTTTTcTTTdTTdTTTcTTTTedTTTTdeTTTTTTedTTTTdeTTTTcTTTdTTdTTTcTTTTTeTddTeTTTTTTTcTTeeTTcTTTTTTTTTTTTTTTTTTTTTTcTTcTTTTT.TTTTTTTTTTTT.',
    '.TTTTTTTTTTTT.TTTTTdTTdTTTTTTTTTTTTTTTTTTTTTTdTTeeTTdTTTTTTTeTddTeTTTTTdTTTdTTdTTTdTTTTedTTTTdeTTTTTTedTTTTdeTTTTdTTTdTTdTTTdTTTTTeTddTeTTTTTTTdTTeeTTdTTTTTTTTTTTTTTTTTTTTTTdTTdTTTTT.TTTTTTTTTTTT.',
    '.TTTTTTTTTTTT.TTTTTeTTeTTTTTTTTTTTTTTTTTTTTTTeTTddTTeTTTTTTTdTddTdTTTTTeTTTdTTdTTTeTTTTddTTTTddTTTTTTddTTTTddTTTTeTTTdTTdTTTeTTTTTdTddTdTTTTTTTeTTddTTeTTTTTTTTTTTTTTTTTTTTTTeTTeTTTTT.TTTTTTTTTTTT.',
    '.TTTTTTTTTTTT.TTTTTdTTdTTTTTTTTTTTTTTTTTTTTTTdTTccTTdTTTTTTTcTddTcTTTTTdTTTdTTdTTTdTTTTcdTTTTdcTTTTTTcdTTTTdcTTTTdTTTdTTdTTTdTTTTTcTddTcTTTTTTTdTTccTTdTTTTTTTTTTTTTTTTTTTTTTdTTdTTTTT.TTTTTTTTTTTT.',
)
LOGO_DONE = '.TTTTTTTTTTTT.TTTTTeTTeTTTTTTTTTTTTTTTTTTTTTTeTTWWTTeTTTTTTTWTWWTWTTTTTeTTTWTTWTTTeTTTTWWTTTTWWTTTTTTWWTTTTWWTTTTeTTTWTTWTTTeTTTTTWTWWTWTTTTTTTeTTWWTTeTTTTTTTTTTTTTTTTTTTTTTeTTeTTTTT.TTTTTTTTTTTT.'
LOGO_FAIL = '.XXXXXXXXXXXX.XXXXXKXXKXXXXXXXXXXXXXXXXXXXXXXgXXKKXXgXXXXXXXgXKKXgXXXXXgXXXgXXgXXXgXXXXggXXXXggXXXXXXggXXXXggXXXXgXXXgXXgXXXgXXXXXgXggXgXXXXXXXgXXggXXgXXXXXXXXXXXXXXXXXXXXXXgXXgXXXXX.XXXXXXXXXXXX.'
# logo:end

ACCENT = {3: "38;2;240;104;43", 2: "38;5;202", 1: "33"}
GOOD = {3: "38;2;40;167;90", 2: "38;5;35", 1: "32"}
BAD = {3: "38;2;232;76;76", 2: "38;5;167", 1: "31"}
DIM = {3: "2", 2: "2", 1: "2"}   # the terminal's own text colour, fainter: it reads on a dark and a light ground
MARKS = {"ok": "✓", "bad": "✗", "now": "›", "skip": "–", "dot": "·", "bar": "━", "rest": "─", "ell": "…"}


def parse(value):
    """The words of HUB_INSTALL_UI -> (colours, columns, rows, step, tail), or None."""
    m = UI.match(value or "")
    if not m or int(m.group(2)) < 60:
        return None
    colours, cols, rows, step, tail = m.groups()
    return int(colours), min(int(cols), 80), int(rows), int(step), int(tail)


def cells(text):
    """The columns a text takes on a terminal: a wide letter (Chinese, Japanese, Korean) is two, a mark that joins is none."""
    return sum(0 if unicodedata.combining(c) or unicodedata.category(c) in ("Mn", "Me", "Cf") else
               2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def padded(text, width):
    return text + " " * max(0, width - cells(text))


def fit(text, room, ell=""):
    """The text in room columns, cut short with `ell` when it does not fit."""
    if cells(text) <= room:
        return text
    while text and cells(text) + cells(ell) > room:
        text = text[:-1]
    return text + ell


def wrap(text, width, indent=""):
    """The lines of a text in width columns of a terminal, broken at spaces (the lines after the first begin with
    indent); a word wider than a line — wide letters have no spaces to break at — is cut where it fills the line, a mark
    that joins stays with its letter. [] for an empty text, as textwrap's."""
    lines, line = [], ""
    for word in text.split():
        sep = " " if line.strip() else ""
        if cells(line) + len(sep) + cells(word) <= width:
            line += sep + word
            continue
        if line.strip():
            lines.append(line)
            line = indent
        while cells(line) + cells(word) > width:
            used, cut = cells(line), 0
            while cut < len(word) and used + cells(word[cut]) <= width:
                used += cells(word[cut])
                cut += 1
            cut = max(cut, 1)
            lines.append(line + word[:cut])
            line, word = indent, word[cut:]
        line += word
    if line.strip():
        lines.append(line)
    return lines


def seconds(n):
    n = int(n + 0.5)
    return "<1 s" if n < 1 else f"{n} s" if n < 60 else f"{n // 60} min {n % 60} s"


FPS = 8


def locked(method):
    """The live part is drawn by the caller and by the thread of the sign: one at a time."""
    def inner(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    inner.__name__, inner.__doc__ = method.__name__, method.__doc__
    return inner


class Look:
    def __init__(self, colours, columns, rows, step, tail, out, names, clock=time.monotonic, ticker=False):
        self.colours, self.columns, self.rows = colours, columns, rows
        self.step, self.tail, self.out, self.names, self.clock = step, tail, out, names, clock
        self.mood = "work"
        self.lock = threading.RLock()
        self.frame = 0
        self.ticker, self.thread, self.stopped = ticker, None, threading.Event()
        self.started = clock()
        self.detail = ""
        self.paused = False
        self.over = False   # the closing screen or a failure is on the screen: no live part again
        self.running = False   # a step is in progress (begun, not done)
        # the sign needs colours of 256 at least and rows enough for it (install.sh decides the same)
        self.pic = colours >= 2 and rows >= 20
        self.mark = MARKS

    @classmethod
    def from_env(cls, environ, out, names, ticker=None):
        """The screen the install script handed over in HUB_INSTALL_UI, when this output is a terminal; else None. The sign
        moves (a thread draws it) when the output is a terminal of the system, not a stand-in with an isatty."""
        got = parse(environ.get("HUB_INSTALL_UI"))
        isatty = getattr(out, "isatty", None)
        if not got or not isatty or not isatty():
            return None
        try:   # a stream that cannot write the marks (an ASCII locale): the lines as they were
            "".join(MARKS.values()).encode(getattr(out, "encoding", None) or "utf-8")
        except (UnicodeEncodeError, LookupError):
            return None
        if ticker is None:
            try:
                ticker = os.isatty(out.fileno())
            except (AttributeError, OSError, ValueError):
                ticker = False
        return cls(*got, out, names, ticker=ticker)

    # --- pieces

    def sgr(self, *codes):
        return "\x1b[%sm" % ";".join(codes) if self.colours and codes else ""

    def paint(self, text, table=None, bold=False, underline=False):
        codes = ([table[self.colours]] if table and self.colours else []) + (["1"] if bold else []) + \
            (["4"] if underline else [])
        return f"\x1b[{';'.join(codes)}m{text}\x1b[0m" if self.colours and codes else text

    def write(self, text):
        self.out.write(text)
        self.out.flush()

    def picture(self, mood):
        """The sign's lines (each LOGO_W columns wide), drawn with half blocks: the upper pixel is the text colour, the lower
        one the background; a single pixel is a half block on the terminal's own background. A frame of the wave while it
        works, as it is when done or failed."""
        pic = {"done": LOGO_DONE, "fail": LOGO_FAIL}.get(mood) or LOGO_FRAMES[self.frame % len(LOGO_FRAMES)]
        width = LOGO_W
        lines = []
        for y in range(0, LOGO_H, 2):
            line, last = "", None

            for x in range(width):
                top, bottom = pic[y * width + x], pic[(y + 1) * width + x]
                if top == "." and bottom == ".":
                    cell, want = " ", ""
                elif top == ".":
                    cell, want = "▄", self.color(bottom, "38") + "\x1b[49m"
                else:
                    cell = "▀"
                    want = self.color(top, "38") + (self.color(bottom, "48") if bottom != "." else "\x1b[49m")
                if cell == " " and last is not None:
                    want = "\x1b[0m"
                if want != last and (want or last):
                    line += want
                    last = want
                line += cell
            lines.append(line + "\x1b[0m")
        return lines

    def color(self, ch, plane):
        rgb, x256 = LOGO_PALETTE[ch]
        if self.colours == 3:
            return "\x1b[%s;2;%d;%d;%dm" % (plane, *rgb)
        return "\x1b[%s;5;%dm" % (plane, x256)

    def face(self, words):
        """The sign with its words beside it (lines of text, the first one at the sign's second line), as lines."""
        pic = self.picture(self.mood)
        pad = " " * (2 + LOGO_W + 3)
        out = []
        for i, art in enumerate(pic):
            word = words[i - 1] if 1 <= i <= len(words) else ""
            out.append("  " + art + ("   " + word if word else ""))
        for extra in words[len(pic) - 1:]:
            out.append(pad + extra)
        return out

    def step_line(self, icon_table, icon, n, name, detail="", took=None):
        """The line of a step, as install.sh's ui_line makes it: the time ends 2 columns before the window's edge, in the
        column of the steps the script drew."""
        name = padded(name, 10)
        left = f"  {self.paint(icon, icon_table)} {self.paint(f'{n}/{STEPS}', DIM)}  {name} "
        head = 2 + 1 + 1 + len(f"{n}/{STEPS}") + 2 + cells(name) + 1
        width = self.columns - 2
        text = fit(detail, width - head - (len(took) + 2 if took else 0), self.mark["ell"])
        line = left + self.paint(text, DIM)
        if took:
            line += " " * max(1, width - head - cells(text) - len(took)) + self.paint(took, DIM)
        return line

    # --- the live part

    def tail_lines(self):
        n = self.step
        lines = []
        if self.pic:
            lines += self.face(self.head_words())
            lines.append("")
        lines.append(self.step_line(ACCENT, self.mark["now"], n, self.names[n], self.detail or self.mark["ell"]))
        return lines

    def head_words(self):
        return [self.paint("Hub by Icons8", ACCENT, bold=True), "", self.paint("Claude Code and Codex, in one place", DIM),
                self.paint("installing for this computer", DIM)]

    @locked
    def clear(self):
        if self.tail:
            self.write(f"\x1b[{self.tail}A\r\x1b[J")
            self.tail = 0

    @locked
    def draw(self):
        if self.paused or self.over or not self.running:   # (a step that is done leaves no live part until the next begins)
            return
        lines = self.tail_lines()
        self.write("\n".join(lines) + "\n")
        self.tail = len(lines)
        if self.ticker and self.pic and not self.thread and not self.stopped.is_set():
            self.thread = threading.Thread(target=self.run_ticker, daemon=True)
            self.thread.start()

    def run_ticker(self):
        """The wave of the sign: the next frame each 1/FPS second, while there is a live part and it is not paused."""
        while not self.stopped.wait(1.0 / FPS):
            with self.lock:
                if not self.tail or self.paused or self.stopped.is_set():
                    continue
                try:
                    self.next_frame()
                except (OSError, ValueError):   # the terminal went away: no more frames, the install goes on
                    self.ticker = False
                    return

    def next_frame(self):
        """The picture of the live part again, in place (the lines of the words and of the step are as they were)."""
        self.frame = (self.frame + 1) % len(LOGO_FRAMES)
        face = self.face(self.head_words())
        rest = self.tail - len(face)
        self.write(f"\x1b[{self.tail}A" + "".join(f"\r{line}\n" for line in face) + (f"\x1b[{rest}B" if rest > 0 else ""))

    def close(self):
        """No more frames (the thread ends by itself; it is a daemon: the install never waits for it)."""
        self.stopped.set()

    @locked
    def hand_over(self):
        """The words of HUB_INSTALL_UI for a program that goes on from here: the screen is its now (the live part is taken
        off, nothing more is drawn here)."""
        self.close()
        self.clear()
        self.over, self.running = True, False
        return f"{self.colours}.{self.columns}.{self.rows}.{self.step}.0"

    @locked
    def end(self):
        """The install ends here: no more frames, nothing live. A step still in progress — neither done nor failed (the
        person answered "later", the hub went back to the environment it had) — keeps its line, marked as not done."""
        self.close()
        self.clear()
        if self.running and not self.over:
            took = seconds(self.clock() - self.started)
            self.write(self.step_line(DIM, self.mark["skip"], self.step, self.names[self.step], "", took) + "\n")
        self.over, self.running = True, False

    @locked
    def pause(self):
        self.clear()
        self.paused = True

    @locked
    def resume(self):
        self.paused = False
        self.draw()

    # --- what the install says

    @locked
    def say(self, text, verbatim=()):
        """A line of the install's own words above the live part; verbatim are the lines that are typed (a command), as they are."""
        self.clear()
        rows = []
        for para in str(text).split("\n"):
            rows += wrap(para, self.columns - 6, "  ") or [""]
        self.write("".join(f"    {self.paint(r, DIM)}\n" for r in rows) + "".join(f"      {line}\n" for line in verbatim))
        self.draw()

    @locked
    def begin(self, n, detail=""):
        """The step n starts: it is the live line."""
        self.clear()
        self.step, self.detail, self.started = n, detail, self.clock()
        self.running = True
        self.mood = "work"
        self.draw()

    @locked
    def done(self, detail=""):
        """The step in progress is finished: its line stays; the live part is gone until the next step begins."""
        self.clear()
        took = seconds(self.clock() - self.started)
        self.running = False
        self.write(self.step_line(GOOD, self.mark["ok"], self.step, self.names[self.step], detail, took) + "\n")

    @locked
    def fail(self, message, log=""):
        """The step in progress failed: its line and the words of what happened, beside the sign."""
        self.close()
        self.clear()
        self.over = True
        if self.running:   # a step that is done keeps its line: it did not fail
            took = seconds(self.clock() - self.started)
            self.write(self.step_line(BAD, self.mark["bad"], self.step, self.names[self.step], "", took) + "\n")
        self.write("\n")
        words = [self.paint(self.names["fail"], BAD, bold=True), ""]
        width = self.columns - (2 + LOGO_W + 3 if self.pic else 4)
        for para in str(message).split("\n"):
            words += wrap(para, width) or [""]
        if log:
            words += ["", self.paint(f"{self.names['log']}  {log}", DIM)]
        self.mood = "fail"
        if self.pic:
            self.write("\n".join(self.face(words)) + "\n")
        else:
            self.write("".join(f"    {w}\n" for w in words))

    @locked
    def finish(self, headline, rows, notes, verbatim=()):
        """The closing screen: rows is [(label, text)], notes the lines of what to know (wrapped), verbatim the lines that
        are typed (a command), as they are."""
        self.close()
        self.clear()
        self.over = True
        self.write("\n")
        label = max([cells(a) for a, _ in rows] + [0])
        body = []
        for key, text in rows:
            shown = self.paint(text, ACCENT, bold=True, underline=True) if not body else text
            body.append(f"{self.paint(padded(key, label), DIM)}   {shown}")
        if self.pic:
            self.mood = "done"
            words = [self.paint(headline, GOOD, bold=True), ""] + body
            self.write("\n".join(self.face(words)) + "\n")
        else:
            width = self.columns - 4
            top = ("╭" + "─" * (width - 2) + "╮", "│", "╰" + "─" * (width - 2) + "╯")

            def row(text, plain):
                return "  " + top[1] + " " + text + " " * max(0, width - 4 - cells(plain)) + " " + top[1]
            lines = [row(self.paint(self.mark["ok"], GOOD) + " " + self.paint(headline, bold=True),
                         self.mark["ok"] + " " + headline), row("", "")]
            for (key, text), shown in zip(rows, body):
                plain = f"{padded(key, label)}   {text}"
                lines.append(row(shown, plain))
            self.write("  " + top[0] + "\n" + "\n".join(lines) + "\n  " + top[2] + "\n")
        if notes or verbatim:
            self.write("\n")
        for note in notes:
            self.write("\n".join("  " + ("  " if i else self.paint(self.mark["dot"], DIM) + " ") + self.paint(row, DIM)
                                 for i, row in enumerate(wrap(note, self.columns - 6) or [""])) + "\n")
        for line in verbatim:
            self.write("    " + line + "\n")


def demo(out=None, sleep=time.sleep):
    """The python half of the install, made up (deploy/oneline/demo.sh): the same calls setup.py makes."""
    out = out or sys.stderr
    if os.environ.get("HUB_DEMO_FAST"):
        sleep = lambda seconds: None   # noqa: E731 — the tests
    names = {4: "Set up", 5: "Start", "fail": "The install stopped", "log": "The log"}
    ui = Look.from_env(os.environ, out, names)
    if not ui:
        return 1
    ui.begin(4)
    sleep(1.2)
    ui.say("Downloading the hub's code 1.9.9 (15 MB)...")
    sleep(1.5)
    ui.done("the hub's code, its commands, the service")
    ui.begin(5)
    sleep(1.8)
    ui.done("the page answers on port 8787")
    ui.finish("The hub is running", [("Open", "http://localhost:8787/"), ("Command", "hub"),
                                     ("Next", "the page takes you through the first session")],
              ["The hub command works in a new terminal window (or now: source ~/.bashrc)"])
    return 0


if __name__ == "__main__":
    sys.exit(demo())
