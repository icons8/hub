"""A terminal recording into a picture: what a person sees on a screen at one moment of an install.

  script -qfec "stty cols 80 rows 24; <command>" -T timing.txt typescript.txt          (util-linux `script`, the classic timing file)
  python3 tools/dev/termshot.py typescript.txt out.png [--timing timing.txt] [--at SECONDS | --until TEXT]
        [--cols 80] [--rows 24] [--theme dark|light] [--title TEXT]

The recording is replayed on a small screen of --cols x --rows (cursor moves, colours of 16, 256 and 24 bits, bold, dim,
the carriage return of a progress line, the scroll), cut at a time (--at, needs --timing) or when a text first appears on the screen
(--until, a regular expression), drawn as HTML (a half block is a cell painted in two colours, so the pixels of a character picture touch) and
shot by Chrome (CHROME, or google-chrome). The stdlib only. Not part of the product: a tool for looking at output."""
import argparse
import codecs
import html
import os
import re
import subprocess
import sys
import tempfile

CSI = re.compile(r"\x1b\[([0-9;?]*)([ -/]*)([@-~])")
UNFINISHED = re.compile(r"\x1b(\[[0-9;?]*[ -/]*)?")   # the start of an escape, its last byte not there yet

THEMES = {   # background, text, the 16 colours
    "dark": ("#1e1e1e", "#d4d4d4", "#1e1e1e #cd3131 #0dbc79 #e5e510 #2472c8 #bc3fbc #11a8cd #e5e5e5 "
                                    "#666666 #f14c4c #23d18b #f5f543 #3b8eea #d670d6 #29b8db #ffffff"),
    "light": ("#ffffff", "#1f1f1f", "#000000 #cd3131 #00bc00 #949800 #0451a5 #bc05bc #0598bc #555555 "
                                     "#666666 #cd3131 #14ce14 #b5ba00 #0451a5 #bc05bc #0598bc #a5a5a5"),
}


def xterm(n, palette):
    if n < 16:
        return palette[n]
    if n >= 232:
        v = 8 + (n - 232) * 10
        return f"#{v:02x}{v:02x}{v:02x}"
    n -= 16
    level = (0, 95, 135, 175, 215, 255)
    return "#%02x%02x%02x" % (level[n // 36], level[n // 6 % 6], level[n % 6])


class Screen:
    def __init__(self, cols, rows):
        self.cols, self.rows = cols, rows
        self.cells = [[(" ", ()) for _ in range(cols)] for _ in range(rows)]
        self.r = self.c = 0
        self.pen = {}
        self.pending = False

    def scroll(self):
        self.cells.pop(0)
        self.cells.append([(" ", ()) for _ in range(self.cols)])

    def put(self, ch):
        if self.c >= self.cols:
            self.c = 0
            self.lf()
        self.cells[self.r][self.c] = (ch, tuple(sorted(self.pen.items())))
        self.c += 1

    def lf(self):
        if self.r == self.rows - 1:
            self.scroll()
        else:
            self.r += 1

    def sgr(self, params):
        i = 0
        ps = params or [0]
        while i < len(ps):
            p = ps[i]
            if p == 0:
                self.pen = {}
            elif p in (1, 2, 3, 4, 7):
                self.pen[{1: "b", 2: "d", 3: "i", 4: "u", 7: "r"}[p]] = True
            elif p in (22, 23, 24, 27):
                self.pen.pop({22: "b", 23: "i", 24: "u", 27: "r"}[p], None)
                if p == 22:
                    self.pen.pop("d", None)
            elif 30 <= p <= 37:
                self.pen["fg"] = ("n", p - 30)
            elif 90 <= p <= 97:
                self.pen["fg"] = ("n", p - 90 + 8)
            elif 40 <= p <= 47:
                self.pen["bg"] = ("n", p - 40)
            elif 100 <= p <= 107:
                self.pen["bg"] = ("n", p - 100 + 8)
            elif p in (39, 49):
                self.pen.pop("fg" if p == 39 else "bg", None)
            elif p in (38, 48) and i + 1 < len(ps):
                key = "fg" if p == 38 else "bg"
                if ps[i + 1] == 5 and i + 2 < len(ps):
                    self.pen[key] = ("n", ps[i + 2])
                    i += 2
                elif ps[i + 1] == 2 and i + 4 < len(ps):
                    self.pen[key] = ("t", tuple(ps[i + 2:i + 5]))
                    i += 4
            i += 1

    def csi(self, params, final):
        n = params[0] if params and params[0] else 1
        if final == "m":
            self.sgr(params)
        elif final == "A":
            self.r = max(0, self.r - n)
        elif final == "B":
            self.r = min(self.rows - 1, self.r + n)
        elif final == "C":
            self.c = min(self.cols - 1, self.c + n)
        elif final == "D":
            self.c = max(0, self.c - n)
        elif final == "G":
            self.c = min(self.cols - 1, n - 1)
        elif final in "Hf":
            self.r = min(self.rows - 1, (params[0] or 1) - 1 if params else 0)
            self.c = min(self.cols - 1, (params[1] or 1) - 1 if len(params) > 1 else 0)
        elif final == "K":
            mode = params[0] if params else 0
            lo, hi = {0: (self.c, self.cols), 1: (0, self.c + 1), 2: (0, self.cols)}.get(mode, (self.c, self.cols))
            for k in range(lo, hi):
                self.cells[self.r][k] = (" ", ())
        elif final == "J":
            mode = params[0] if params else 0
            rows = range(self.r + 1, self.rows) if mode == 0 else range(0, self.r) if mode == 1 else range(self.rows)
            for k in rows:
                self.cells[k] = [(" ", ()) for _ in range(self.cols)]
            if mode in (0, 2):
                for k in range(self.c if mode == 0 else 0, self.cols):
                    self.cells[self.r][k] = (" ", ())

    def feed(self, text):
        i = 0
        n = len(text)
        while i < n:
            ch = text[i]
            if ch == "\x1b" and i + 1 < n and text[i + 1] == "[":
                m = CSI.match(text, i)
                if m:
                    ps = [int(x) if x.isdigit() else 0 for x in m.group(1).replace("?", "").split(";")] if m.group(1) else []
                    if not m.group(1).startswith("?"):
                        self.csi(ps, m.group(3))
                    i = m.end()
                    continue
            if ch == "\x1b":
                i += 2 if i + 1 < n and text[i + 1] in "78=>" else 1
                continue
            if ch == "\r":
                self.c = 0
            elif ch == "\n":
                self.lf()
            elif ch == "\b":
                self.c = max(0, self.c - 1)
            elif ch == "\t":
                self.c = min(self.cols - 1, (self.c // 8 + 1) * 8)
            elif ch >= " " and ch != "\x7f":
                self.put(ch)
            i += 1


def colour(spec, palette, default):
    if spec is None:
        return default
    kind, v = spec
    return xterm(v, palette) if kind == "n" else "#%02x%02x%02x" % v


def render(screen, theme, title):
    bg, fg, pal = THEMES[theme]
    palette = pal.split()
    rows = []
    for row in screen.cells:
        cells = []
        for ch, pen in row:
            p = dict(pen)
            f, b = colour(p.get("fg"), palette, fg), colour(p.get("bg"), palette, bg)
            if p.get("r"):
                f, b = b, f
            if p.get("b") and p.get("fg", ("n", 9))[0] == "n" and p.get("fg", ("n", 9))[1] < 8:
                f = palette[p["fg"][1] + 8]
            style = []
            if ch in "▀▄█":
                top, bottom = {"▀": (f, b), "▄": (b, f), "█": (f, f)}[ch]
                style.append(f"background:linear-gradient({top} 50%,{bottom} 50%)")
                cells.append(f'<i style="{";".join(style)}"></i>')
                continue
            if b != bg:
                style.append(f"background:{b}")
            style.append(f"color:{f}")
            if p.get("b"):
                style.append("font-weight:700")
            if p.get("d"):
                style.append("opacity:.6")
            if p.get("i"):
                style.append("font-style:italic")
            if p.get("u"):
                style.append("text-decoration:underline")
            cells.append(f'<i style="{";".join(style)}">{html.escape(ch)}</i>')
        rows.append("".join(cells))
    css = (f"body{{margin:0;background:#c8c8c8;padding:14px}}.w{{display:inline-block;background:{bg};border-radius:8px;"
           f"box-shadow:0 4px 18px rgba(0,0,0,.35);overflow:hidden}}.t{{height:22px;background:#3a3a3a;color:#bbb;"
           f"font:12px/22px sans-serif;text-align:center}}.s{{padding:10px 14px;font:14px/20px 'Liberation Mono','DejaVu Sans Mono',"
           f"monospace;color:{fg}}}.r{{display:grid;grid-template-columns:repeat({screen.cols},1ch);height:20px;white-space:pre}}"
           f".r i{{font-style:normal;display:block;height:20px}}")
    body = "".join(f'<div class="r">{r}</div>' for r in rows)
    return (f'<!doctype html><meta charset="utf-8"><style>{css}</style><div class="w"><div class="t">{html.escape(title)}</div>'
            f'<div class="s">{body}</div></div>')


def replay(data, timing, at, until, cols, rows):
    """The screen after `at` seconds of the recording (timing: script -T lines "delay count"), or just before the first
    chunk that makes `until` appear on it, or the end."""
    screen = Screen(cols, rows)
    raw = data
    if raw.startswith(b"Script started"):   # the first line is script's own header (the timing counts bytes after it)
        raw = raw[raw.find(b"\n") + 1:]
    if not timing:
        screen.feed(raw.decode("utf-8", "replace").replace("\x00", ""))
        return screen
    t = 0.0
    off = 0
    decoder, rest = codecs.getincrementaldecoder("utf-8")("replace"), ""   # a letter or an escape cut between two chunks
    for line in timing.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == "O":   # the advanced format; the classic one is "delay size"
            parts = parts[1:]
        if len(parts) != 2 or parts[0][:1].isalpha():
            continue
        t += float(parts[0])
        size = int(parts[1])
        chunk = rest + decoder.decode(raw[off:off + size]).replace("\x00", "")
        off += size
        if at is not None and t > at:
            break
        cut = chunk.rfind("\x1b")   # an escape the chunk ends in the middle of waits for the next one
        rest = chunk[cut:] if cut >= 0 and UNFINISHED.fullmatch(chunk[cut:]) else ""
        screen.feed(chunk[:len(chunk) - len(rest)])
        if until and re.search(until, "\n".join("".join(ch for ch, _ in row) for row in screen.cells)):
            break
    else:   # the end of the recording: what was held back, and a letter its last bytes did not finish
        screen.feed(rest + decoder.decode(b"", final=True))
    return screen


def main(argv=None):
    ap = argparse.ArgumentParser(prog="termshot.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("typescript")
    ap.add_argument("out")
    ap.add_argument("--timing")
    ap.add_argument("--at", type=float)
    ap.add_argument("--until")
    ap.add_argument("--cols", type=int, default=80)
    ap.add_argument("--rows", type=int, default=24)
    ap.add_argument("--theme", default="dark", choices=sorted(THEMES))
    ap.add_argument("--title", default="Terminal")
    ap.add_argument("--trim", action="store_true", help="the window is as tall as the lines written (and 3 more)")
    ap.add_argument("--html", action="store_true", help="write the page next to the picture and keep it")
    args = ap.parse_args(argv)
    with open(args.typescript, "rb") as f:
        data = f.read()
    timing = open(args.timing).read() if args.timing else None
    screen = replay(data, timing, args.at, args.until, args.cols, args.rows)
    if args.trim:
        used = max([i + 1 for i, row in enumerate(screen.cells) if any(ch != " " for ch, _ in row)] + [1])
        screen.cells = screen.cells[:min(args.rows, used + 1)]
        args.rows = len(screen.cells)
    page = render(screen, args.theme, args.title)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(page)
        if args.html:
            with open(os.path.splitext(args.out)[0] + ".html", "w", encoding="utf-8") as f:
                f.write(page)
        width = args.cols * 8 + 90   # 14 px of Liberation Mono is 8.4 px a column; the page is cut to the window
        height = args.rows * 20 + 22 + 20 + 28 + 14
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        from modules.testchrome import chrome as testchrome   # the tools' Chrome plays nothing on the machine's devices
        chrome = os.environ.get("CHROME", "google-chrome")
        subprocess.run([chrome, "--headless=new", *testchrome.NO_AUDIO, "--no-sandbox", "--hide-scrollbars", f"--window-size={width},{height}",
                        f"--user-data-dir={os.path.join(tmp, 'p')}", f"--screenshot={os.path.abspath(args.out)}",
                        "--force-device-scale-factor=2", "file://" + path], check=True, capture_output=True, timeout=120)
    return 0


if __name__ == "__main__":
    sys.exit(main())
