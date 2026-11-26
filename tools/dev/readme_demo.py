"""Demo stand for the README shots: the hub page with an invented fleet (a shop and four agents) and shooting of `media/`.
The state folder is temporary, HOME is empty, hub.json is empty: no live hub and none of its names on the shots.

    python3 tools/dev/readme_demo.py serve [port]      # the page on 127.0.0.1:<port> (default 8793)
    python3 tools/dev/readme_demo.py shots [folder]    # shoot into media/, check the page text, write manifest.json

`shots` shoots the pages of this stand (2x) and composes the README frames over them with the templates of tools/dev/readme_media
(wash, window, iPhone, the mark media/brand/hub-logo.png, Lora): hero, social-preview, feed, now, telegram.
`shots` puts a mark next to every picture in manifest.json: the page text under the shot (visible text and attributes)
was checked against the forbidden forms of publication and the names of this machine (user, host, name of the live hub);
if anything is found, the shots are not written. A folder whose manifest.json names another tool (frames composed by
hand over these pages) is refused: shoot into another one. Needs node and headless Chrome (modules/testchrome)."""
import functools
import getpass
import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES = os.path.join(HERE, "readme_media")
CDP = os.path.join(REPO, "tests", "e2e", "cdp.mjs")
sys.path.insert(0, REPO)


def seed(state):
    """The journal of an invented fleet: a hub, four agents, a conversation about slow checkout; one agent asks about prod."""
    from core import journal
    j = journal.Journal(os.path.join(state, "journal.jsonl"))

    def ev(t, e=None, **d):
        return j.append(t, e, d)
    ev("owner.settings", None, timezone="UTC", parallel=3, channel="core")
    ev("session.requested", "hub", session_id="s-hub", cwd="/work/hub", kind="hub", channel="core")
    ev("session.started", "hub", session_id="s-hub")
    ev("session.info", "hub", model="claude-opus-5-5", effort="high", mode="bypassPermissions")
    agents = [("api", "Backend of the shop: REST API, migrations, deploys", "/work/shop-api"),
              ("web", "Storefront: React app, design system", "/work/shop-web"),
              ("infra", "Terraform, CI pipelines, on-call", "/work/infra"),
              ("docs", "Docs site and changelog", "/work/docs")]
    for n, about, cwd in agents:
        ev("session.requested", n, session_id=f"s-{n}", cwd=cwd, role="agent", about=about, channel="core", owner=True)
        ev("session.started", n, session_id=f"s-{n}")
        ev("session.info", n, model="claude-opus-5-5", effort="high", mode="bypassPermissions")
        ev("session.folder", n, channel="core", git="ok", dirty=0)
    m = ev("message.received", None, to="hub", channel="web", owner=True,
           text="The checkout page is slow on mobile. Find out why and fix it, but ask me before touching prod.")["seq"]
    ev("turn.started", "hub", marker=f"m{m}", prompt_head="The checkout page is slow on mobile…")
    ev("session.said", "hub", text="Got it. I'll hand the profiling to **web** and ask **api** whether the cart endpoint "
       "is the bottleneck. I'll bring you the result.")
    ev("turn.tool", "hub", id="t1", name="Bash",
       input={"command": "hub send web \"Profile /checkout on a throttled phone, report the top 3 causes\""})
    ev("turn.tool", "hub", id="t2", name="Bash",
       input={"command": "hub send api \"Is GET /cart slower than 300 ms p95? Check the last 7 days\""})
    ev("turn.tool_result", "hub", id="t1", text="queued for web", error=False)
    ev("turn.tool_result", "hub", id="t2", text="queued for api", error=False)
    ev("turn.result", "hub", context=18000,
       text="Started: **web** is profiling the checkout page, **api** is checking the cart endpoint. "
            "Nothing touches prod without your yes.")
    ev("session.said", "hub", text="While they work: you will see their questions here and in Telegram.")
    w = ev("message.received", None, to="web", channel="session", owner=False, sender="hub",
           text="Profile /checkout on a throttled phone, report the top 3 causes")["seq"]
    ev("turn.started", "web", marker=f"m{w}", prompt_head="Profile /checkout on a throttled phone…")
    ev("turn.tool", "web", id="w1", name="Bash",
       input={"command": "npx lighthouse https://staging.example.com/checkout --preset=perf"})
    ev("session.said", "web", text="Found it: the order summary re-renders on every keystroke and loads a 1.4 MB country "
       "list. Two small fixes, about 40 lines.")
    ev("turn.tool", "web", id="w2", name="Edit", input={"file_path": "src/checkout/Summary.tsx"})
    a = ev("message.received", None, to="api", channel="session", owner=False, sender="hub",
           text="Is GET /cart slower than 300 ms p95? Check the last 7 days")["seq"]
    ev("turn.started", "api", marker=f"m{a}", prompt_head="Is GET /cart slower than 300 ms p95?")
    ev("turn.tool", "api", id="a1", name="Bash", input={
        "command": "psql -c \"select percentile_cont(0.95) within group (order by ms) from cart_timing\""})
    ev("session.said", "api", text="`GET /cart` p95 is **212 ms** - not the bottleneck. One slow query on "
       "`/cart/shipping` (640 ms): an index fixes it.")
    ev("permission.requested", "api", request_id="r1", tool="AskUserQuestion", interactive=True, input={
        "questions": [{"question": "Run the index migration on production now?", "header": "Prod", "options": [
            {"label": "Yes, now", "description": "Takes ~2 s, no downtime"}, {"label": "Tonight at 03:00"},
            {"label": "No, staging first"}]}]})
    i = ev("message.received", None, to="infra", channel="web", owner=True, text="Rotate the staging certificates")["seq"]
    ev("turn.started", "infra", marker=f"m{i}", prompt_head="Rotate the staging certificates")
    ev("turn.tool", "infra", id="i1", name="Bash", input={"command": "terraform plan -target=module.staging_certs"})
    d = ev("message.received", None, to="docs", channel="web", owner=True, text="Draft the 2.4 changelog from merged PRs")["seq"]
    ev("turn.started", "docs", marker=f"m{d}", prompt_head="Draft the 2.4 changelog…")
    ev("turn.result", "docs", context=9000,
       text="Changelog for 2.4 is drafted: 11 entries grouped by area, two breaking changes called out at the top.")
    ev("session.gist", "web", text="Fixing re-renders on the checkout page")
    ev("session.gist", "infra", text="Rotating staging certificates")
    ev("session.gist", "api", text="Waiting for you: index migration")


FLEET_WORDS = {"hub", "api", "web", "infra", "docs", "shop", "you", "root", "user", "localhost"}   # words of the demo itself and common ones


def live_names():
    """What must not appear in the shots but this machine has: the OS user, the host, the live hub's name, the git name.
    Read from the machine's own environment — not from a list in the code. Words of the demo itself (its agents' names) do not count:
    a user called `docs` would make any shooting impossible."""
    found = set()
    try:
        found.add(getpass.getuser())
    except Exception:  # noqa: BLE001 — no user in the environment (a container): the other sources
        pass
    host = socket.gethostname()
    found.update({host, host.split(".")[0]})
    found.add(os.path.basename(os.path.expanduser("~").rstrip("/\\")))
    paths = [os.environ.get("HUB_CONFIG"), os.path.expanduser("~/.config/hub-core/hub.json")]
    if os.environ.get("APPDATA"):
        paths.append(os.path.join(os.environ["APPDATA"], "hub-core", "hub.json"))
    for path in paths:   # all that can be read: a name in one empty file does not cancel the others
        try:
            with open(path, encoding="utf-8") as f:
                found.add(str((json.load(f).get("hub") or {}).get("name") or ""))
        except (OSError, ValueError, AttributeError, TypeError):
            continue
    try:
        who = subprocess.run(["git", "config", "--global", "user.name"], capture_output=True, text=True, timeout=5).stdout
        found.update(re.findall(r"[A-Za-z][A-Za-z0-9._-]{3,}", who))
    except (OSError, subprocess.SubprocessError):
        pass
    return sorted({n.lower() for n in found if len(n) >= 3 and n.lower() not in FLEET_WORDS})


@functools.lru_cache(maxsize=1)
def deny_forms():
    """The shared publish-deny forms — by the same loader as publish.sh (tools/publish_allow.py)."""
    spec = importlib.util.spec_from_file_location("publish_allow", os.path.join(REPO, "tools", "publish_allow.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return tuple(mod.patterns(os.path.join(REPO, "tools", "publish-deny.txt"), False))


def forbidden(text, names):
    """Private things in a text: the forms publishing does not allow, and this machine's names (case-insensitive)."""
    found = []
    for line in text.splitlines():
        for f in deny_forms():
            m = f.search(line)
            if m:
                found.append(f"form {f.pattern[:40]!r}: {m.group(0)[:60]!r}")
    for name in names:
        if re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", text.lower()):
            found.append(f"this machine's name: {name!r}")
    return found


def clean_env(env, home):
    """The shooting environment: another hub and its settings are invisible, HOME is an empty folder."""
    out = {k: v for k, v in env.items()
           if not k.startswith(("HUB_", "CLAUDE", "ANTHROPIC", "XDG_")) and k not in ("HOME", "USERPROFILE", "APPDATA")}
    out["HOME"] = home
    out["USERPROFILE"] = home
    return out


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port, proc, seconds=60):
    end = time.time() + seconds
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), 0.3).close()
            return
        except OSError:
            if proc.poll() is not None:
                raise SystemExit(f"the process exited (code {proc.returncode}) without opening port {port}")
            time.sleep(0.2)
    raise SystemExit(f"port {port} did not open within {seconds} s")


def serve(port=8793, state=None):
    state = state or tempfile.mkdtemp(prefix="readme-demo-")
    os.environ["HUB_STATE_DIR"] = state
    os.environ["HUB_CONFIG"] = os.path.join(state, "hub.json")
    os.environ["HUB_CONFIG_DIR"] = os.path.join(state, "cfg")
    os.environ["HUB_WEB_PORT"] = str(port)
    with open(os.environ["HUB_CONFIG"], "w", encoding="utf-8") as f:
        f.write("{}\n")
    os.makedirs(os.environ["HUB_CONFIG_DIR"], exist_ok=True)
    seed(state)
    import threading
    from modules.web.server import Web
    web = Web(state)
    srv = web.serve(port=port)
    threading.Thread(target=web.tail.run, args=(web.stop,), daemon=True).start()
    print(f"http://127.0.0.1:{port}/  ({state})", flush=True)
    srv.serve_forever()


def steps(cdp, *items):
    r = subprocess.run(["node", CDP, str(cdp), json.dumps(list(items))], capture_output=True, text=True, timeout=120)
    if r.returncode:
        raise SystemExit(f"the step failed: {r.stdout}\n{r.stderr[:1500]}")
    return json.loads(r.stdout)


# visible text and attributes of the page the shot was taken from
SCAN = ("(() => { const a = [...document.querySelectorAll('[title],[aria-label],[alt],[placeholder],[href],[value]')]"
        ".map((n) => ['title', 'aria-label', 'alt', 'placeholder', 'href', 'value'].map((k) => n.getAttribute(k) || '').join(' '));"
        " return document.body.innerText + '\\n' + a.join('\\n'); })()")


class Shooter:
    def __init__(self, out):
        from modules.testchrome import chrome as testchrome
        self.out = os.path.abspath(out)
        self.chrome_bin = testchrome.find()
        if not self.chrome_bin or not shutil.which("node"):
            raise SystemExit(f"node and headless Chrome are needed. {testchrome.INSTALL}")
        self.work = tempfile.mkdtemp(prefix="readme-shots-")
        self.home = os.path.join(self.work, "home")
        self.stage = os.path.join(self.work, "media")
        os.makedirs(self.home)
        # HOME is empty, but the user's fonts (emoji of the agent icons) are needed: without them the icons turn into empty squares
        fonts = os.path.expanduser("~/.fonts")
        if os.path.isdir(fonts):
            shutil.copytree(fonts, os.path.join(self.home, ".fonts"))
        # template pages, page fonts and the shots lie side by side: that is how file:// finds them
        shutil.copytree(TEMPLATES, self.stage)
        shutil.copytree(os.path.join(REPO, "modules", "web", "static", "fonts"), os.path.join(self.stage, "fonts"))
        shutil.copy(os.path.join(REPO, "modules", "web", "static", "clean", "lora-700-latin.woff2"), os.path.join(self.stage, "fonts"))
        shutil.copy(os.path.join(REPO, "media", "brand", "hub-logo.png"), os.path.join(self.stage, "logo.png"))   # the mark the templates show
        self.names = live_names()
        self.scanned = {}   # page -> sha256 of its checked text
        self.procs = []

    def start(self):
        env = clean_env(os.environ, self.home)
        port = free_port()
        state = os.path.join(self.work, "state")
        os.makedirs(state)
        self.server_log = open(os.path.join(self.work, "server.log"), "w", encoding="utf-8")
        server = subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), "serve", str(port), state],
                                  env=env, stdout=self.server_log, stderr=self.server_log, start_new_session=True)
        self.procs.append(server)
        try:
            wait_port(port, server)
        except SystemExit as e:
            with open(os.path.join(self.work, "server.log"), encoding="utf-8") as f:
                raise SystemExit(f"{e}\n{f.read()[-1500:]}")
        self.base = f"http://127.0.0.1:{port}/"
        self.cdp = free_port()
        from modules.testchrome import chrome as testchrome
        root = ["--no-sandbox"] if testchrome.sandbox_blocked() or (hasattr(os, "geteuid") and os.geteuid() == 0) else []
        chrome = subprocess.Popen([self.chrome_bin, "--headless=new", *testchrome.NO_AUDIO, *root, f"--remote-debugging-port={self.cdp}",
                                   f"--user-data-dir={os.path.join(self.work, 'chrome')}", "--window-size=1440,900",
                                   "--lang=en", "--accept-lang=en", "--hide-scrollbars", "--force-color-profile=srgb",
                                   "--disable-component-update", "--disable-background-networking", "--no-first-run",
                                   "--no-default-browser-check", "about:blank"],
                                  env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.procs.append(chrome)
        wait_port(self.cdp, chrome)

    def stop(self):
        import signal
        if getattr(self, "server_log", None):
            self.server_log.close()
        for p in self.procs:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, AttributeError):
                p.terminate()
        shutil.rmtree(self.work, ignore_errors=True)

    def scan(self, label, got):
        """Check the text `got` of the page as it was shot (see `shoot`) and keep the hash of exactly that text."""
        if label in self.scanned:
            raise SystemExit(f"page \"{label}\" is already checked: each has its own label, otherwise a hash is lost in manifest.json")
        bad = forbidden(got, self.names)
        if bad:
            raise SystemExit(f"page \"{label}\" has personal data, not writing the frames:\n  " + "\n  ".join(bad))
        self.scanned[label] = hashlib.sha256(got.encode()).hexdigest()   # what exactly was checked — goes to manifest.json
        return got

    def shoot(self, label, shot, width, height):
        """A 2x picture of the window and the check of its text in ONE browser session: the window size of a shot ends with the
        session, so a later read would be of another size (a phone page has other visible text than a desktop one)."""
        got = steps(self.cdp, {"shot": os.path.join(self.stage, shot), "width": width, "height": height, "theme": "light",
                               "clip": [0, 0, width, height], "scale": 2}, {"eval": SCAN})[1]["eval"] or ""
        self.scan(label, got)
        return os.path.join(self.stage, shot)

    def page(self, label, url, shot, width, height, before=(), feed=None):
        """Open, wait for the page, shoot; check the text at the same window size it was shot at. The look is the hub's default
        (Clean, detailed list): `"look": None` keeps the step of the driver from putting the earlier Classic look into the browser."""
        steps(self.cdp, {"local": PAGE_OPTIONS}, {"go": url, "feed": feed, "look": None},
              {"until": "!!document.querySelector('#list li, .feed')", "ms": 15000}, {"sleep": 3500}, {"eval": NO_SETUP_BAR},
              *before, {"sleep": 900})
        return self.shoot(label, shot, width, height)

    def compose(self, template, shot, width, height, label, **inside):
        """A shot from its own template page (tools/dev/readme_media) and the already shot pictures beside it: `inside` names
        the page shots the template shows (desk=…, phone=…, as `desk.png` and `phone.png` there)."""
        for slot, src in inside.items():
            shutil.copy(os.path.join(self.stage, src), os.path.join(self.stage, slot + ".png"))
        steps(self.cdp, {"go": "file://" + os.path.join(self.stage, template), "feed": None}, {"sleep": 1200})
        return self.shoot(label, shot, width, height)


# the page's options of the look (Settings → Appearance of Clean): the detailed list, counts, no usage bars
PAGE_OPTIONS = {"hub.opt.core:clean": json.dumps({"detail": True, "counts": True, "usage": False})}
# a headless browser with an empty HOME has no Claude Code: the bar that offers to install it is not a part of the hub's look
NO_SETUP_BAR = "document.head.insertAdjacentHTML('beforeend', '<style>.setupbar{display:none!important}</style>'); 1"
# scroll the feed of the open chat to `top` pixels (0: its start; a big number: its end)
SCROLL = ("(() => { const f = document.querySelector('.feed'); const s = [f, ...document.querySelectorAll('main, .scroller')]"
          ".find((e) => e && e.scrollHeight > e.clientHeight + 50); if (s) s.scrollTop = %d; return !!s; })()")
# which pages stand behind each shot: checking the text of each of them is the condition for writing the shot
PICTURES = {"hero.png": ["hub: waiting question", "phone: waiting question", "composed: hero.png"],
            "social-preview.png": ["hub: waiting question", "phone: waiting question", "composed: social-preview.png"],
            "feed.png": ["hub: chat with the dispatcher", "composed: feed.png"], "now.png": ["hub: Now tab", "composed: now.png"],
            "telegram.png": ["composed: telegram.png"]}
# what manifest.json says made the frames; another word there — the frames were composed elsewhere (by hand, over page shots
# of another journal), and shooting over them would bring the older look back
TOOL = "tools/dev/readme_demo.py shots"


def made_elsewhere(out):
    """What made out's frames when it is not shots() (manifest.json's "tool", or what is wrong with the file), else None:
    a folder without manifest.json is new; a manifest that does not say shots() made it is not taken for its own."""
    path = os.path.join(out, "manifest.json")
    if not os.path.lexists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            tool = json.load(f).get("tool")
    except (OSError, ValueError, AttributeError) as e:
        return f"an unreadable manifest.json ({e})"
    return None if tool == TOOL else tool or "a manifest.json without \"tool\""


def shots(out):
    out = os.path.abspath(out)
    other = made_elsewhere(out)
    if other:
        raise SystemExit(f"the frames in {out} were not made by this tool (manifest.json: {other!r}): shooting here "
                         "would bring the older look back; shoot into another folder")
    s = Shooter(out)
    try:
        s.start()
        # the page shots (2x, the window of the card): the source of the pictures composed below
        s.page("hub: waiting question", s.base + "#/s/api", "desk-ask.png", 1180, 940)
        s.page("phone: waiting question", s.base + "#/s/api", "phone-ask.png", 390, 780)
        s.page("hub: chat with the dispatcher", s.base + "#/s/hub", "feed-page.png", 1168, 688, feed="full",
               # this machine's fonts have no "⎿" sign (on a Mac and most systems it exists) — a similar one from DejaVu
               before=({"eval": "document.querySelectorAll('.elbow').forEach((n) => { n.textContent = '\\u2514'; }); 1"},
                       {"eval": SCROLL % 0}))
        s.page("hub: Now tab", s.base + "#/s/api", "now-page.png", 1168, 688, before=({"click": "Now"}, {"eval": SCROLL % 9999999}))
        s.compose("hero.html", "hero.png", 1280, 800, "composed: hero.png", desk="desk-ask.png", phone="phone-ask.png")
        s.compose("social-preview.html", "social-preview.png", 1280, 640, "composed: social-preview.png", desk="desk-ask.png", phone="phone-ask.png")
        s.compose("window.html", "feed.png", 1280, 800, "composed: feed.png", desk="feed-page.png")
        s.compose("window.html", "now.png", 1280, 800, "composed: now.png", desk="now-page.png")
        s.compose("telegram.html", "telegram.png", 1280, 800, "composed: telegram.png")
        missing = {p for pages in PICTURES.values() for p in pages} - set(s.scanned)
        if missing:   # before writing: the folder is never left half new
            raise SystemExit(f"the frames list pages that were never shot or checked: {sorted(missing)}")
        os.makedirs(out, exist_ok=True)
        try:   # remove the shots of the previous shooting that are no longer in the list; other pictures in the folder are left alone
            with open(os.path.join(out, "manifest.json"), encoding="utf-8") as f:
                for old in set(json.load(f).get("pictures") or {}) - set(PICTURES):
                    if os.path.basename(old) == old:
                        os.remove(os.path.join(out, old))
        except (OSError, ValueError, AttributeError):
            pass
        pictures = {}
        for name, pages in PICTURES.items():
            shutil.copy(os.path.join(s.stage, name), os.path.join(out, name))
            with open(os.path.join(out, name), "rb") as f:
                pictures[name] = {"sha256": hashlib.sha256(f.read()).hexdigest(), "scan": "clean",
                                  "pages": {p: s.scanned[p] for p in pages}}
        with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"tool": TOOL, "pictures": pictures}, f, indent=1)
            f.write("\n")
        print(f"{len(pictures)} frames, pages checked: {len(s.scanned)}")
    finally:
        s.stop()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "serve":
        serve(int(sys.argv[2]) if len(sys.argv) > 2 else 8793, sys.argv[3] if len(sys.argv) > 3 else None)
    elif cmd == "shots":
        shots(sys.argv[2] if len(sys.argv) > 2 else os.path.join(REPO, "media"))
    else:
        sys.exit(__doc__)
