"""Scenario steps of the browser: one code for `hub video` (modules/proof/video.py) and `hub shot --steps`
(modules/proof/shot.py). A step is a JSON object with one action (plus an optional "caption"):
  {"goto": "https://…"}, {"click": "<selector>"}, {"hover": "<selector>"}, {"type": ["<selector>", "text"]},
  {"press": "Enter"}, {"scroll": "<selector>" | 600}, {"wait": 800}, {"wait_for": "<selector>"}, {"caption": "Done"}
wait is milliseconds; wait_for waits (up to WAIT_FOR s) until the element is on the page and has a size, then goes on.
hub shot adds the action {"shot": "<name>"}: it is not run here, the caller takes the picture."""
import json
import time
import types

from core import i18n
from modules.proof import cdp

ACTIONS = ("goto", "click", "hover", "type", "press", "scroll", "wait", "wait_for", "caption")
SHOT = "shot"   # an action of hub shot only: video says so instead of ignoring it
WAIT_FOR = 10   # seconds wait_for waits for its element before the step fails
KEYS = {"Enter": (13, "\r"), "Tab": (9, ""), "Escape": (27, ""), "Backspace": (8, ""), "ArrowDown": (40, ""),
        "ArrowUp": (38, ""), "ArrowLeft": (37, ""), "ArrowRight": (39, ""), "Space": (32, " ")}

# step caption, and the cursor with the click wave (cursor=true): installed into every scenario document
OVERLAY_FN = """(cursor => {
  if (window.__hubProof) return; window.__hubProof = true;
  const css = (e, s) => (e.style.cssText = s, e);
  const add = () => {
    const cap = css(document.createElement('div'), 'position:fixed;z-index:2147483647;left:50%;bottom:24px;' +
      'transform:translateX(-50%);max-width:90vw;background:rgba(20,20,20,.85);color:#fff;' +
      'font:600 20px/1.3 system-ui,sans-serif;padding:8px 16px;border-radius:8px;pointer-events:none;display:none');
    if (cursor) {
      const dot = css(document.createElement('div'), 'position:fixed;z-index:2147483647;width:18px;height:18px;' +
        'margin:-9px 0 0 -9px;border-radius:50%;background:rgba(255,214,0,.85);box-shadow:0 0 0 2px #333;' +
        'pointer-events:none;left:-40px;top:-40px;transition:left .04s linear,top .04s linear');
      document.documentElement.append(dot);
      addEventListener('mousemove', e => { dot.style.left = e.clientX + 'px'; dot.style.top = e.clientY + 'px'; }, true);
      addEventListener('mousedown', e => {
        const r = css(document.createElement('div'), `position:fixed;z-index:2147483647;left:${e.clientX}px;` +
          `top:${e.clientY}px;width:16px;height:16px;margin:-8px 0 0 -8px;border:3px solid #e5341d;border-radius:50%;` +
          'pointer-events:none;transition:transform .45s ease-out,opacity .45s ease-out');
        document.documentElement.append(r);
        requestAnimationFrame(() => { r.style.transform = 'scale(4)'; r.style.opacity = '0'; });
        setTimeout(() => r.remove(), 600);
      }, true);
    }
    document.documentElement.append(cap);
    window.__hubCaption = t => { cap.textContent = t || ''; cap.style.display = t ? 'block' : 'none'; };
  };
  document.documentElement ? add() : addEventListener('DOMContentLoaded', add);
})"""
OVERLAY = OVERLAY_FN + "(true)"
CAPTIONS = OVERLAY_FN + "(false)"   # a picture shows the state, not where the mouse was


class Usage(Exception):
    pass


def state():
    """What run_step keeps between steps: the current caption (a recorder object with a caption field does as well)."""
    return types.SimpleNamespace(caption=None)


def check(steps, extra=()):
    """The steps of a scenario are right: one action each (ACTIONS and extra), the first one opens a page."""
    actions = ACTIONS + tuple(extra)
    for i, s in enumerate(steps, 1):
        acts = [k for k in s if k in ACTIONS + (SHOT,) and k != "caption"] if isinstance(s, dict) else None
        if acts is None or len(acts) > 1 or (not acts and "caption" not in s) or set(acts) - set(actions):
            raise Usage(i18n.t("cli.proof.step_one_action", n=i, actions=", ".join(actions)))
        if "type" in s and not (isinstance(s["type"], list) and len(s["type"]) == 2):
            raise Usage(i18n.t("cli.proof.step_type", n=i))
        if "wait_for" in s and not (isinstance(s["wait_for"], str) and s["wait_for"].strip()):
            raise Usage(i18n.t("cli.proof.step_wait_for", n=i))
    if "goto" not in steps[0]:
        raise Usage(i18n.t("cli.proof.first_goto"))


def load(path, extra=()):
    """The scenario from a file; errors come at once and are clear, before the browser starts."""
    try:
        with open(path, encoding="utf-8") as f:
            sc = json.load(f)
    except OSError:
        raise Usage(i18n.t("cli.proof.no_file", path=path))
    except ValueError as e:
        raise Usage(i18n.t("cli.proof.not_json", error=e))
    steps = sc.get("steps") if isinstance(sc, dict) else None
    if not steps or not isinstance(steps, list):
        raise Usage(i18n.t("cli.proof.no_steps"))
    check(steps, extra)
    return sc


def center(page, sel):
    r = page.js(f"""(() => {{ const e = document.querySelector({json.dumps(sel)});
      if (!e) throw new Error('no element ' + {json.dumps(sel)});
      e.scrollIntoView({{block: 'center', inline: 'center'}});
      const r = e.getBoundingClientRect();
      if (!r.width || !r.height) throw new Error('element not visible: ' + {json.dumps(sel)});
      return [r.left + r.width / 2, r.top + r.height / 2]; }})()""")
    page.pump(0.15)
    return r


class Cursor:
    def __init__(self, page):
        self.page, self.x, self.y = page, 40, 40

    def move(self, x, y, steps=12):
        for i in range(1, steps + 1):
            self.page.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": self.x + (x - self.x) * i / steps,
                                                        "y": self.y + (y - self.y) * i / steps})
            self.page.pump(0.025)
        self.x, self.y = x, y

    def click(self):
        for kind in ("mousePressed", "mouseReleased"):
            self.page.call("Input.dispatchMouseEvent", {"type": kind, "x": self.x, "y": self.y, "button": "left",
                                                        "clickCount": 1})


def key(page, name):
    code, text = KEYS.get(name, (0, name if len(name) == 1 else ""))
    base = {"key": "" if name == "Space" else name, "windowsVirtualKeyCode": code, "code": name}
    page.call("Input.dispatchKeyEvent", {"type": "keyDown", "text": text, **base} if text else
              {"type": "rawKeyDown", **base})
    page.call("Input.dispatchKeyEvent", {"type": "keyUp", **base})


def wait_for(page, sel):
    """Until the element is on the page and has a size (a shot or a click right after it is of the finished state)."""
    look = f"""(() => {{ const e = document.querySelector({json.dumps(sel)});
      if (!e) return false;
      const r = e.getBoundingClientRect();
      return !!(r.width && r.height); }})()"""
    end = time.time() + WAIT_FOR
    while not page.js(look):
        if time.time() > end:
            raise cdp.Error(i18n.t("cli.proof.wait_for_timeout", selector=sel, seconds=f"{WAIT_FOR:g}"))
        page.pump(0.1)


def show_caption(page, st):
    page.js(f"window.__hubCaption && window.__hubCaption({json.dumps(st.caption)})")


def run_step(page, cur, st, s):
    """One step; st (state()) holds the caption that stays on the page until another one."""
    if "caption" in s:
        st.caption = s["caption"]
    if "goto" in s:
        page.goto(cdp.url_of(s["goto"]), 0.3)
    elif "click" in s or "hover" in s:
        cur.move(*center(page, s.get("click") or s.get("hover")))
        if "click" in s:
            cur.click()
    elif "type" in s:
        cur.move(*center(page, s["type"][0]))
        cur.click()
        for ch in str(s["type"][1]):
            page.call("Input.insertText", {"text": ch})
            page.pump(0.04)
    elif "press" in s:
        key(page, s["press"])
    elif "scroll" in s:
        if isinstance(s["scroll"], (int, float)):
            page.js(f"scrollBy({{top: {s['scroll']}, behavior: 'smooth'}})")
        else:
            center(page, s["scroll"])
    elif "wait" in s:
        page.pump(s["wait"] / 1000)
    elif "wait_for" in s:
        wait_for(page, s["wait_for"])
    show_caption(page, st)
    page.pump(0.6)   # let the step's result be seen
