"""The installer's mark: the hub's sign (modules/web/static/favicon/tab.svg - an orange rounded tile and rays of white dots)
drawn in a terminal with half blocks, and its animation while the install works: the dots glow in a wave that runs from the
centre outwards.

  python3 tools/dev/logo.py              write the pictures into deploy/oneline/install.sh and deploy/oneline/look.py
  python3 tools/dev/logo.py --check      exit 1 when the two files do not carry exactly these pictures (tests/test_oneline_look.py)
  python3 tools/dev/logo.py --preview    print the frames, the finished sign and the failed one in 24-bit colour (see them with
                                         tools/dev/termshot.py; the recording needs CR LF)
  --source <file.svg>                    draw from another svg of the same make (a square viewBox, a rect for the tile, circles
                                         for the dots); the default is the tab icon above

The pictures are made here, once, and written into the two scripts as plain text between the marks `# logo:begin` and
`# logo:end`: the person's machine has no Pillow and nothing is computed there. The colours of the tile and of the dots, and
the place and the size of each dot, are read out of the svg; the grid is 14 x 14 pixels (7 lines of text), so each dot is one
pixel and its size is how strongly it is white (a pixel cannot be smaller); the sign has twelve rays of dots in three rings. A letter is a colour: the table gives it in 24 bits and
in the 256 colours of xterm. Two pixel rows are one line of text. The stdlib only."""
import math
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SOURCE = os.path.join(ROOT, "modules", "web", "static", "favicon", "tab.svg")
SCRIPTS = (os.path.join("deploy", "oneline", "install.sh"), os.path.join("deploy", "oneline", "look.py"))
W = H = 14
FRAMES = 8

REST = (0.6, 0.5, 0.4)             # how strongly white a dot of each size (the biggest first) is while the wave is elsewhere
CREST = (1.0, 0.94, 0.86)          # ... and when the wave is on it
FRONT = (0.6, 5.9)                 # the wave runs from the first to the second radius (in pixels) in FRAMES frames
WIDTH = 1.7                        # how wide the wave is, in pixels
INNER = 2.4                        # the inner ring is at least this far from the centre: twelve dots closer would run together
SPREAD = 0.9                       # the dots keep this much of their distance from the centre: the outer ring off the edge
FAINT = 0.3                        # the failed sign: the dots dim ...
DIM_TILE = 0.5                     # ... on a tile that is this much of the way to a grey, and one ray dark
DARK = (40, 32, 36)
GREY = (104, 104, 104)
LEVELS = 6                         # the white of a dot is mixed with the tile in this many steps: a, b, c, d, e, W

TILE = DOT = None
RAYS, OFFSET, RINGS = 0, 0.0, []   # the rays, the angle of the first one, the distance of each ring (by size rank) in pixels


def hexrgb(text):
    text = text.lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))


def configure(path):
    """Read the sign out of an svg: the tile's and the dots' colours, each dot's place and size."""
    global TILE, DOT, RAYS, OFFSET, RINGS
    with open(path, encoding="utf-8") as f:
        text = f.read()
    side = float(re.search(r'viewBox="0 0 ([0-9.]+) ', text).group(1))
    TILE = hexrgb(re.search(r'<rect[^>]* fill="(#[0-9A-Fa-f]{3,6})"', text).group(1))
    DOT = hexrgb(re.search(r'<g fill="(#[0-9A-Fa-f]{3,6}|white)"', text).group(1).replace("white", "#fff"))
    circles = [tuple(float(v) for v in m) for m in re.findall(r'<circle cx="([0-9.]+)" cy="([0-9.]+)" r="([0-9.]+)"', text)]
    sizes = sorted({r for _, _, r in circles}, reverse=True)
    scale = W / side * SPREAD
    by_rank = [[(math.degrees(math.atan2(cy - side / 2, cx - side / 2)) % 360, math.hypot(cx - side / 2, cy - side / 2) * scale)
                for cx, cy, r in circles if r == size] for size in sizes]
    RAYS = len(by_rank[-1])   # the outermost ring has a dot on every ray; the inner ones have a dot on some of them,
    OFFSET = min(a for a, _ in by_rank[-1]) % (360 / RAYS)   # which a grid of 14 pixels cannot show: every ray has them all
    RINGS = [sum(d for _, d in ring) / len(ring) for ring in by_rank]
    RINGS[0] = max(RINGS[0], INNER)


def mix(a, b, t):
    return tuple(x + (y - x) * t for x, y in zip(a, b))


def dim_tile():
    return mix(TILE, GREY, DIM_TILE)


def palette():
    """letter: (r, g, b) - the tile (T), the dots in steps of white (a is the faintest, W white), and for the failed sign
    the dimmed tile (X), its dim dot (g) and the dark ray (K)."""
    table = {"T": TILE}
    for i, letter in enumerate("abcdeW"):
        table[letter] = mix(TILE, DOT, (i + 1) / LEVELS)
    table["X"] = dim_tile()
    table["g"] = mix(dim_tile(), DOT, FAINT)
    table["K"] = DARK
    return {k: tuple(int(round(c)) for c in v) for k, v in table.items()}


def inside_tile(x, y):
    """The svg's rounded square: 14 wide, the corners of a fifth of the side (the svg has a quarter: it is a coarser grid)."""
    r = W * 0.2
    cx = min(max(x, r), W - r)
    cy = min(max(y, r), H - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def paint(dots, tile=None):
    """The grid of letters of a picture: dots is {(x, y): (strength 0..1, colour)}; the tile under them."""
    pal = palette()
    tile = tile or TILE
    rows = []
    for y in range(H):
        row = ""
        for x in range(W):
            if not inside_tile(x + 0.5, y + 0.5):
                row += "."
                continue
            base = tile
            if (x, y) in dots:
                strength, colour = dots[(x, y)]
                base = mix(base, colour, strength)
            row += min(pal, key=lambda k: sum((a - b) ** 2 for a, b in zip(pal[k], base)))
        rows.append(row)
    return "".join(rows)


def sign(strength_of, tile=None, dark_top=False):
    """strength_of(rank, distance) -> how white a dot glows; the dots of the rays at the top are dark when dark_top."""
    dots = {}
    for k in range(RAYS):
        angle = OFFSET + 360.0 * k / RAYS
        rad = math.radians(angle)
        dark = dark_top and abs(angle % 360 - 270) <= 16
        for rank, dist in enumerate(RINGS):
            pos = (int(W / 2 + dist * math.cos(rad)), int(H / 2 + dist * math.sin(rad)))
            strength, colour = (1.0, DARK) if dark else (strength_of(rank, dist), DOT)
            if strength > dots.get(pos, (0, 0))[0] or dark:
                dots[pos] = (strength, colour)
    return paint(dots, tile)


def build():
    frames = []
    for f in range(FRAMES):
        front = FRONT[0] + (FRONT[1] - FRONT[0]) * f / (FRAMES - 1)

        def strength_of(rank, dist, front=front):
            pulse = max(0.0, 1.0 - abs(dist - front) / WIDTH)
            return REST[rank] + (CREST[rank] - REST[rank]) * pulse
        frames.append(sign(strength_of))
    return frames, sign(lambda rank, dist: CREST[rank]), sign(lambda rank, dist: FAINT, tile=dim_tile(), dark_top=True)


def xterm256(rgb):
    """The nearest colour of the 256: the cube of 6 levels or the 24 greys."""
    level = (0, 95, 135, 175, 215, 255)
    best = None
    for n in range(16, 256):
        if n < 232:
            m = n - 16
            c = (level[m // 36], level[m // 6 % 6], level[m % 6])
        else:
            v = 8 + (n - 232) * 10
            c = (v, v, v)
        d = sum((a - b) ** 2 for a, b in zip(c, rgb))
        if best is None or d < best[0]:
            best = (d, n)
    return best[1]


def lines_of(pic, pal):
    out = []
    for y in range(0, H, 2):
        line = ""
        for x in range(W):
            top, bottom = pic[y * W + x], pic[(y + 1) * W + x]
            if top == "." and bottom == ".":
                line += "\x1b[0m "
            elif top != ".":
                line += "\x1b[38;2;%d;%d;%dm" % pal[top]
                line += ("\x1b[48;2;%d;%d;%dm" % pal[bottom]) if bottom != "." else "\x1b[49m"
                line += "▀"
            else:
                line += "\x1b[38;2;%d;%d;%dm\x1b[49m▄" % pal[bottom]
        out.append("  " + line + "\x1b[0m")
    return out


def preview():
    frames, done, fail = build()
    pal = palette()
    sets = [("frame %d" % i, p) for i, p in enumerate(frames)] + [("done", done), ("fail", fail)]
    for start in range(0, len(sets), 4):
        group = sets[start:start + 4]
        print("".join(name.ljust(W + 2) for name, _ in group))
        cols = [lines_of(p, pal) for _, p in group]
        for i in range(H // 2):
            print("".join(c[i] + "\x1b[0m" + " " for c in cols))
        print()


def shell_block():
    frames, done, fail = build()
    pal = palette()
    out = ["# logo:begin (tools/dev/logo.py writes this; a test holds it equal to look.py's)",
           "ui_logo_rgb() {   # a letter of the sign -> UI_RGB (24 bits) and UI_X256 (256 colours)",
           "  case $1 in"]
    for ch, rgb in pal.items():
        out.append("    %s) UI_RGB='%s'; UI_X256=%d ;;" % (ch, ";".join(map(str, rgb)), xterm256(rgb)))
    out += ["  esac", "}",
            "ui_logo() {   # the sign: UI_FRAMES (the wave of the work), UI_PIC_DONE and UI_PIC_FAIL, UI_W columns of UI_H rows of pixels",
            "  UI_W=%d; UI_H=%d; UI_NFRAMES=%d" % (W, H, FRAMES),
            "  UI_FRAMES=("]
    out += ["    %s" % f for f in frames]
    out += ["  )", "  UI_PIC_DONE=%s" % done, "  UI_PIC_FAIL=%s" % fail, "}", "# logo:end"]
    return "\n".join(out) + "\n"


def python_block():
    frames, done, fail = build()
    pal = palette()
    out = ["# logo:begin (tools/dev/logo.py writes this; a test holds it equal to install.sh's)",
           "LOGO_W, LOGO_H = %d, %d" % (W, H), "LOGO_PALETTE = {"]
    for ch, rgb in pal.items():
        out.append("    %r: ((%d, %d, %d), %d)," % (ch, *rgb, xterm256(rgb)))
    out += ["}", "LOGO_FRAMES = ("]
    out += ["    %r," % f for f in frames]
    out += [")", "LOGO_DONE = %r" % done, "LOGO_FAIL = %r" % fail, "# logo:end"]
    return "\n".join(out) + "\n"


BLOCK = re.compile(r"^# logo:begin.*?^# logo:end\n", re.S | re.M)


def rewritten(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        text = f.read()
    new = shell_block() if path.endswith(".sh") else python_block()
    if not BLOCK.search(text):
        raise SystemExit(f"{path}: no `# logo:begin` … `# logo:end` block to write into")
    return text, BLOCK.sub(lambda _: new, text, count=1)


def main(argv):
    configure(argv[argv.index("--source") + 1] if "--source" in argv else SOURCE)
    if "--preview" in argv:
        preview()
        return 0
    stale = 0
    for path in SCRIPTS:
        old, new = rewritten(path)
        if old == new:
            continue
        stale += 1
        if "--check" in argv:
            print(f"{path}: the sign is not as tools/dev/logo.py draws it")
        else:
            with open(os.path.join(ROOT, path), "w", encoding="utf-8", newline="") as f:
                f.write(new)
            print(f"{path}: written")
    return 1 if stale and "--check" in argv else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
