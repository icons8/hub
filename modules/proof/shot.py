"""hub shot: a screenshot of the result for the owner (the "milestones: show them" rule), for all hubs, no dependencies: its own
headless Chrome over CDP. Markup by selector (a frame with a caption, a blacked-out secret), phone and desktop in one command,
before/after as two pictures side by side.

  hub shot <url|file.html> [--out x.png] [--widths 390,1440] [--full] [--wait ms] [--eval "js"]
           [--box "<selector>[=caption]"]... [--redact "<selector>"]...
  --box splits at the first "=" outside [...] and quotes: --box '[data-x="a=b"]=caption' keeps the selector whole
  --full taller than 12000 px goes in parts: x.png, x-2.png, x-3.png... (each path on its own line)
  hub shot --compare before.png after.png [--out x.png] [--stack]
  hub shot --steps scenario.json [--out x.png]
  --steps is a scenario of `hub video` steps (modules/proof/steps.py: goto, click, hover, type, press, scroll, wait,
  wait_for, caption) and a {"shot": "<name>"[, "width": 390][, "height": 800]} step: it saves the page as it is then to
  x-<name>.png (width and height are for that shot only). A scenario shot is one screen of the viewport; the scenario's
  "mobile": true makes the whole run a phone, as in `hub video`.
"""
import base64
import json
import math
import os
import pathlib
import re
import struct
import sys
import time
import zlib
from html import escape

from core import i18n, perms
from modules.proof import cdp, steps

MOBILE = 500          # a width up to it is a phone: mobile emulation, touch, density 2
PHONE_HEIGHT = 844
DESKTOP_HEIGHT = 900
SETTLE_MAX = 10       # seconds the page gets to finish loading media before the shot; not done by then, shoot as is
MAX_HEIGHT = 12000    # one picture is no taller (a bigger one will not fit in Telegram): a longer page is shot in parts
# A frame of hub shot says in its own PNG text that it is a screenshot (tEXt Software, right after IHDR): a file index
# tells it from a picture drawn by a model or a script, which looks the same. A --steps shot is one too; --compare is
# made of two shots — no tag.
SOFTWARE = b"hub shot"

MARKS = """(marks) => {
  const layer = document.createElement('div');
  layer.dataset.hubProof = '1';
  layer.style.cssText = 'position:absolute;left:0;top:0;width:0;height:0;pointer-events:none;z-index:2147483647';
  document.body.appendChild(layer);
  for (const m of marks) {
    const el = document.querySelector(m.selector);
    if (!el) throw new Error('no element ' + m.selector);
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) throw new Error('element not visible: ' + m.selector);
    const pad = m.type === 'redact' ? 0 : 4, x = r.left + scrollX - pad, y = r.top + scrollY - pad;
    const d = document.createElement('div');
    d.style.cssText = `position:absolute;left:${x}px;top:${y}px;width:${r.width + 2 * pad}px;` +
      `height:${r.height + 2 * pad}px;box-sizing:border-box;border-radius:4px;` +
      (m.type === 'redact' ? 'background:#000' : 'border:3px solid #e5341d;box-shadow:0 0 0 2px #fff');
    layer.appendChild(d);
    if (m.text) {
      const t = document.createElement('div');
      t.textContent = m.text;
      t.style.cssText = 'position:absolute;background:#e5341d;color:#fff;font:600 14px/1.3 system-ui,sans-serif;' +
        'padding:3px 8px;border-radius:4px;white-space:nowrap;max-width:90vw;overflow:hidden;text-overflow:ellipsis;' +
        `left:${Math.max(scrollX + 2, x)}px;top:${y >= scrollY + 28 ? y - 26 : y + r.height + 2 * pad + 4}px`;
      layer.appendChild(t);
    }
  }
}"""


SETTLE = """async (full, ceiling, limit) => {
  const end = performance.now() + ceiling * 1000, left = () => Math.max(0, end - performance.now());
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const seen = el => full || el.getBoundingClientRect().top < innerHeight;
  const name = el => el.tagName.toLowerCase() + ' ' + ((el.currentSrc || el.getAttribute('src') || '').split('/').pop()
    .slice(0, 60) || '?');
  const home = scrollY, at = y => scrollTo({top: y, behavior: 'instant'});
  if (full) {
    // lazy and on-appearance loading (loading=lazy, IntersectionObserver) fires only on scrolling: go to the bottom
    for (const i of document.querySelectorAll('img[loading=lazy]')) i.loading = 'eager';
    const step = Math.max(innerHeight * 0.8, 200);
    for (let y = 0; left() && y < Math.min(document.documentElement.scrollHeight, limit); y += step) {
      at(y);
      await sleep(60);
    }
    at(home);
    await sleep(60);
  }
  for (const v of document.querySelectorAll('video'))
    if (seen(v) && v.readyState < 2 && !v.error) { v.preload = 'auto'; v.load(); }
  const state = () => {
    const wait = [], broken = [];
    for (const m of document.querySelectorAll('img[src], video')) {
      if (!seen(m)) continue;
      const bad = m.tagName === 'IMG' ? m.complete && !m.naturalWidth : !!m.error;
      const done = m.tagName === 'IMG' ? m.complete : m.readyState >= 2;
      if (bad) broken.push(name(m)); else if (!done) wait.push(name(m));
    }
    return {wait, broken};
  };
  while (state().wait.length && left()) await sleep(50);
  const fonts = await Promise.race([document.fonts.ready.then(() => true), sleep(left()).then(() => false)]);
  await Promise.race([Promise.all([...document.images].filter(i => i.complete && seen(i))
    .map(i => i.decode().catch(() => 0))), sleep(left())]);
  at(home);
  await sleep(100);
  const s = state();
  if (!fonts) s.wait.push('fonts');   // a marker: settle() says it in the reader's language
  return s;
}"""


Usage = steps.Usage
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")   # a shot step's name goes into a file name


def split_box(box):
    """"<selector>=caption" -> (selector, caption): splits at the first "=" outside [...] and outside quotes, so an
    attribute selector ([data-x="a=b"], [href^="/x"]) stays whole; no such "=" - the whole string is the selector."""
    depth, quote, escaped = 0, None, False
    for i, ch in enumerate(box):
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(depth - 1, 0)
        elif ch == "=" and depth == 0:
            return box[:i], box[i + 1:]
    return box, ""


def marks_from(boxes, redacts):
    out = []
    for b in boxes:
        sel, text = split_box(b)
        out.append({"type": "box", "selector": sel.strip(), "text": text.strip() or None})
    return out + [{"type": "redact", "selector": r.strip()} for r in redacts]


def outputs(out, widths):
    """Where to write: one width gets out as is, several get out-<width>.png each."""
    if len(widths) == 1:
        return [(widths[0], out)]
    base, ext = os.path.splitext(out)
    return [(w, f"{base}-{w}{ext or '.png'}") for w in widths]


def settle(page, full=False, ceiling=SETTLE_MAX):
    """The page finishes its media by itself: scrolling (--full), loading of pictures and videos, fonts, but no longer
    than ceiling s. Returns what did not load and what is broken, as lines for the output."""
    r = page.js(f"({SETTLE})({json.dumps(bool(full))}, {ceiling}, {MAX_HEIGHT})") or {}
    notes = []
    if r.get("wait"):
        wait = [i18n.t("cli.proof.fonts") if w == "fonts" else w for w in r["wait"]]
        notes.append(i18n.t("cli.proof.not_loaded", seconds=f"{ceiling:g}", items=", ".join(wait[:6]))
                     + (i18n.t("cli.proof.and_more", n=len(wait) - 6) if len(wait) > 6 else ""))
    if r.get("broken"):
        notes.append(i18n.t("cli.proof.broken", items=", ".join(r["broken"][:6])))
    return notes


def part_paths(out, count):
    """Names of the parts of a long page: out, then <base>-2<ext>, <base>-3<ext>..."""
    base, ext = os.path.splitext(out)
    return [out] + [f"{base}-{i}{ext or '.png'}" for i in range(2, count + 1)]


def capture(page, out, full=False, tag=False):
    """A shot into a file; --full taller than MAX_HEIGHT goes in parts of MAX_HEIGHT from top to bottom. Returns the paths
    (there may be several parts). Markup (--box/--redact) lives in the document, so it lands in the part with its element.
    tag: each file says it is a hub shot (tagged)."""
    if not full:
        return [_save(page, out, {"format": "png", "captureBeyondViewport": False}, tag)]
    size = page.call("Page.getLayoutMetrics").get("cssContentSize") or {}
    width, height = size.get("width") or 1, max(1, math.ceil(size.get("height") or 1))
    paths = part_paths(out, -(-height // MAX_HEIGHT))
    for i, path in enumerate(paths):
        top = i * MAX_HEIGHT
        clip = {"x": 0, "y": top, "width": width, "height": min(MAX_HEIGHT, height - top), "scale": 1}
        _save(page, path, {"format": "png", "captureBeyondViewport": True, "clip": clip}, tag)
    return paths


def tagged(png):
    """The PNG with a tEXt Software chunk after IHDR; the image chunks are the browser's bytes as they were (nothing is
    re-encoded). Not a PNG - as is."""
    if png[:8] != b"\x89PNG\r\n\x1a\n" or png[12:16] != b"IHDR":
        return png
    end = 8 + 12 + struct.unpack(">I", png[8:12])[0]
    body = b"tEXt" + b"Software\0" + SOFTWARE
    return png[:end] + struct.pack(">I", len(body) - 4) + body + struct.pack(">I", zlib.crc32(body)) + png[end:]


def _save(page, out, params, tag=False):
    data = base64.b64decode(page.call("Page.captureScreenshot", params)["data"])
    with open(out, "wb") as f:
        f.write(tagged(data) if tag else data)
    return out


def shot(target, out, widths=(1440,), full=False, wait=400, js=None, marks=(), chrome=None, timeout=60,
         ceiling=SETTLE_MAX, warn=None):
    """Page shots by width; a markup selector missing on the page is an error, not a frame in the wrong place. Before the
    shot the media finishes loading (settle); whatever did not make it goes to warn(line), the shot is taken anyway. wait
    is added on top."""
    done = []
    page = cdp.Page(chrome, timeout)
    try:
        for w, path in outputs(out, list(widths)):
            mobile = w <= MOBILE
            page.viewport(w, PHONE_HEIGHT if mobile else DESKTOP_HEIGHT, 2 if mobile else 1, mobile)
            page.goto(cdp.url_of(target), wait / 1000)
            if js:
                page.js(js)
                page.pump(0.2)
            for note in settle(page, full, ceiling):
                (warn or (lambda m: None))(f"{os.path.basename(path)}: {note}")
            if marks:
                if not full:
                    page.js(f"document.querySelector({json.dumps(marks[0]['selector'])})"
                            "?.scrollIntoView({block: 'center'})")
                page.js(f"({MARKS})({json.dumps(list(marks))})")
            done.extend(capture(page, path, full, tag=True))
    finally:
        page.close()
    return done


COMPARE = """<!doctype html><meta charset="utf-8"><style>
html,body{margin:0;background:#fff;font:600 22px/1.2 system-ui,sans-serif;color:#222}
#c{display:inline-flex;gap:24px;padding:20px;flex-direction:%s;align-items:flex-start}
figure{margin:0}figcaption{margin:0 0 10px}img{display:block;max-width:1440px;border:1px solid #ccc}
.a figcaption{color:#888}.b figcaption{color:#1a7f37}</style>
<div id="c"><figure class="a"><figcaption>%s</figcaption><img src="%s"></figure>
<figure class="b"><figcaption>%s</figcaption><img src="%s"></figure></div>"""


def compare(before, after, out, stack=False, chrome=None, timeout=60):
    """Before and after side by side (or one under the other, for a phone) as one picture."""
    for p in (before, after):
        if not os.path.isfile(p):
            raise Usage(i18n.t("cli.proof.no_file", path=p))
    # the captions are burned into the picture the owner looks at, even when a session runs the command: his language
    lang = i18n.owner()
    page = cdp.Page(chrome, timeout)
    try:
        html = os.path.join(page.dir, "compare.html")
        with open(html, "w", encoding="utf-8") as f:
            f.write(COMPARE % ("column" if stack else "row", escape(i18n.tr(lang, "cli.proof.before")),
                               pathlib.Path(os.path.abspath(before)).as_uri(),
                               escape(i18n.tr(lang, "cli.proof.after")),
                               pathlib.Path(os.path.abspath(after)).as_uri()))
        page.viewport(4000, 1000)
        page.goto(cdp.url_of(html), 0.2)
        w, h = page.js("(() => { const b = document.getElementById('c').getBoundingClientRect();"
                       " return [Math.ceil(b.width), Math.ceil(b.height)]; })()")
        page.viewport(min(w, 4000), min(h, MAX_HEIGHT))
        return capture(page, out)[0]
    finally:
        page.close()


def load_steps(path):
    """The scenario of hub shot --steps, checked before the browser starts: hub video's steps and the shot step."""
    sc = steps.load(path, ("shot",))
    names = set()   # lower case: on macOS and Windows "Menu" and "menu" are one file
    for i, s in enumerate(sc["steps"], 1):
        name = s.get("shot")
        if "shot" not in s:
            if "width" in s or "height" in s:
                raise Usage(i18n.t("cli.proof.step_size_alone", n=i))
            continue
        if not (isinstance(name, str) and NAME.fullmatch(name)) or name.lower() in names:
            raise Usage(i18n.t("cli.proof.step_shot_name", n=i))
        names.add(name.lower())
        for k in ("width", "height"):
            if k in s and not (isinstance(s[k], int) and not isinstance(s[k], bool) and 200 <= s[k] <= 4000):
                raise Usage(i18n.t("cli.proof.step_shot_size", n=i))
    if not names:
        raise Usage(i18n.t("cli.proof.no_shot_step"))
    return sc


def step_paths(sc, out):
    """Where the scenario's shots go: x-<name>.png for out x.png."""
    base, ext = os.path.splitext(out)
    return [f"{base}-{s['shot']}{ext or '.png'}" for s in sc["steps"] if "shot" in s]


def view(width=1440, height=None):
    """Arguments of Page.viewport for a width: a phone up to MOBILE, a desktop above it, as in shot()."""
    mobile = width <= MOBILE
    return width, height or (PHONE_HEIGHT if mobile else DESKTOP_HEIGHT), 2 if mobile else 1, mobile


def run_steps(sc, out, chrome=None, timeout=60, ceiling=SETTLE_MAX, warn=None):
    """Walk the scenario in its own Chrome and save a picture at each shot step; returns the paths. Like shot(), the media
    finishes loading before a picture (what did not goes to warn(line)). A failed step is an error with its number."""
    paths = iter(step_paths(sc, out))
    done = []
    home = view(390 if sc.get("mobile") else 1440)   # "mobile": true as in a hub video scenario: the whole run is a phone
    page = cdp.Page(chrome, timeout)
    try:
        page.viewport(*home)
        page.call("Page.addScriptToEvaluateOnNewDocument", {"source": steps.CAPTIONS})
        cur, st = steps.Cursor(page), steps.state()
        for i, s in enumerate(sc["steps"], 1):
            try:
                if "shot" not in s:
                    steps.run_step(page, cur, st, s)
                    continue
                path = next(paths)
                if "caption" in s:
                    st.caption = s["caption"]
                    steps.show_caption(page, st)
                size = "width" in s or "height" in s
                if size:   # for this picture only: the next steps see the page as before
                    page.viewport(*view(s.get("width", home[0]), s.get("height")))
                    page.pump(0.3)
                for note in settle(page, False, ceiling):
                    (warn or (lambda m: None))(f"{os.path.basename(path)}: {note}")
                done.extend(capture(page, path, tag=True))
                if size:
                    page.viewport(*home)
                    page.pump(0.3)
            except cdp.Error as e:
                raise cdp.Error(i18n.t("cli.proof.step_failed", n=i, error=e))
    finally:
        page.close()
    return done


def parse(argv):
    o = {"widths": [1440], "full": False, "wait": 400, "eval": None, "box": [], "redact": [], "out": None,
         "compare": None, "stack": False, "target": None, "steps": None}
    it, given = iter(argv), set()
    for a in it:
        given.add(a)
        try:
            if a == "--compare":
                o["compare"] = [next(it), next(it)]
            elif a in ("--out", "--eval", "--steps"):
                o[a[2:]] = next(it)
            elif a == "--wait":
                o["wait"] = int(next(it))
            elif a == "--widths":
                o["widths"] = [int(w) for w in next(it).split(",") if w.strip()]
            elif a in ("--box", "--redact"):
                o[a[2:]].append(next(it))
            elif a in ("--full", "--stack"):
                o[a[2:]] = True
            elif a.startswith("--") or o["target"]:
                raise Usage(i18n.t("cli.proof.unknown_arg", arg=a))
            else:
                o["target"] = a
        except (StopIteration, ValueError):
            raise Usage(i18n.t("cli.proof.needs_value", arg=a))
    if o["steps"] and (o["target"] or given & {"--compare", "--full", "--stack", "--box", "--redact", "--eval", "--wait",
                                               "--widths"}):   # the scenario says all of this itself
        raise Usage(i18n.t("cli.proof.steps_alone"))
    if not o["steps"] and not o["compare"] and not o["target"]:
        raise Usage(i18n.t("cli.proof.shot_usage"))
    if not o["widths"] or any(w < 200 or w > 4000 for w in o["widths"]):
        raise Usage(i18n.t("cli.proof.widths"))
    return o


def default_out(folder, name):
    """Without --out, into the hub folder, not the session's repo (an extra file in git); the name has a time, so shots do not overwrite each other."""
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, time.strftime(f"{name}-%Y%m%d-%H%M%S.png"))


def main(argv, folder):
    try:
        o = parse(argv)
        sc = load_steps(o["steps"]) if o["steps"] else None   # a wrong scenario is said before anything is made
        out = os.path.abspath(o["out"] or default_out(folder, "steps" if sc else "compare" if o["compare"] else "shot"))
        # --out is the user's place: new files there get the user's permissions, not the hub's closed ones; existing ones keep their own
        wanted = step_paths(sc, out) if sc else [out] if o["compare"] else [p for _, p in outputs(out, o["widths"])]
        made = [p for p in wanted if o["out"] and not os.path.exists(p)]
        try:
            if o["compare"]:
                print(compare(*o["compare"], out, o["stack"]))
                return 0
            warn = lambda m: print(f"hub shot: {m}", file=sys.stderr)
            if sc:
                paths = run_steps(sc, out, warn=warn)
            else:
                paths = shot(o["target"], out, o["widths"], o["full"], o["wait"], o["eval"],
                             marks_from(o["box"], o["redact"]), warn=warn)
            for path in paths:
                print(path)
            return 0
        finally:
            perms.give_to_user(made)
    except (Usage, cdp.Error, OSError) as e:
        print(f"hub shot: {e}", file=sys.stderr)
        return 2
