"""Browsers for sessions: an agent Chrome (its own profile, CDP only on localhost, never the owner's main Chrome);
there can be several and of any kind (local and remote).

hub.json settings:
  "browsers": {
    "vm":  {"kind": "local", "port": 9230, "description": "…", "risk": []},           # the core starts and keeps it itself
    "own": {"kind": "local", "port": 9223, "headless": false, "start": "login"},      # an on-screen window: start, open
    "mac": {"kind": "remote", "url": "http://127.0.0.1:9223", "risk": ["owner-data"]}  # someone else's CDP: a tunnel, a cloud
  },
  "browser": {"mcp": ["chrome-devtools-mcp"], "chrome": "<path>", "env": {"PATH": "…"},  # what to connect and launch with
              "workspace": ["/tmp"],   # where else MCP allows shots and downloads, besides the session folder and its TMPDIR
              "usage_statistics": false}  # true: let chrome-devtools-mcp send its usage statistics to Google (off by default)
A local browser's own "chrome" (a path, like browser.chrome) launches it with another Chromium build, for example another
Chromium-based browser with a copy of its profile; the rest keep browser.chrome.

Every browser is a catalog tool `browser:<name>` (MCP chrome-devtools-mcp to its address): the hub gives it to a
session like any other; a browser with the owner's logins is marked with the risk `owner-data`, and then the owner's "yes" is needed.

start: "always" (the default): the supervisor keeps the browser running; "demand": a tool call by a
session starts it, and one closed by the owner stays closed until the next call; "login": like demand, plus once per machine
boot the supervisor starts it. open is the page the window opens with (about:blank by default): for example, the
hub page when the on-screen window is your working one (06.10: an empty window at Mac login, once closed, opened again).
idle_minutes (start: demand and login, 0 by default — never): the supervisor closes a headless Chrome that no session has
called a tool of for that long (sessions keep their MCP connected for life, so a connected client is no sign of use); the
next tool call starts it again. A window on the screen (headless: false) may hold the owner's sign-in in progress and is
never closed for idleness.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
import platforms  # noqa: E402
from core import i18n  # noqa: E402
from core import net  # noqa: E402,F401 — to itself (CDP on 127.0.0.1) directly, bypassing the machine's proxy
from core import redact  # noqa: E402
from core.catalog import expand_self_ref, write_private  # noqa: E402

PYTHON = platforms.current.python()  # the MCP server is the hub's own process in the session: on Windows it sets UTF-8 itself
DEFAULT_MCP = ["chrome-devtools-mcp"]
VIEWPORT = "1440x900"
# chrome-devtools-mcp writes and reads files only in the client's roots (for Claude Code, the session folder) and in its own TMPDIR;
# sessions put shots in /tmp: without this, "not within any of the configured workspace roots"
WORKSPACE = ["/tmp"]
CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
]
CHROME_NAMES = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome-headless-shell"]


class Problem(Exception):
    pass


def screenless(cfg):
    """A Linux machine where the environment Chrome is launched with names no display (DISPLAY, WAYLAND_DISPLAY): a window
    cannot open there, Chrome dies at start with "Missing X server or $DISPLAY". The display of another system is its own."""
    if not sys.platform.startswith("linux"):
        return False
    launch_env = env(cfg)
    return not (launch_env.get("DISPLAY") or launch_env.get("WAYLAND_DISPLAY"))


def mcp_missing(cfg):
    """The program of browser.mcp when it is not there — not on the PATH Chrome and MCP get (browser.env included), or a
    path to nothing: the MCP launcher would die in exec and the session see only "connection closed". None if it is, or
    if it cannot be told here: a relative path is counted from each session's folder; a value that is not a list of
    words is not checked (the supervisor calls this every step and must not fall on a bad setting)."""
    base = (cfg.get("browser") or {}).get("mcp") or DEFAULT_MCP
    if not isinstance(base, list) or not isinstance(base[0], str) or not base[0]:
        return None
    program = base[0]
    if os.path.dirname(program) and not os.path.isabs(os.path.expanduser(program)):
        return None
    return None if shutil.which(os.path.expanduser(program), path=env(cfg).get("PATH")) else program


def configured(cfg):
    """{name: browser} with defaults; wrong entries carry a problem field, so they are visible rather than silently missing.
    A window (headless: false) where the machine has no screen is a headless Chrome: the other would not start at all."""
    out = {}
    no_screen = None
    for name, b in (cfg.get("browsers") or {}).items():
        b = dict(b or {})
        kind = b.get("kind")
        if kind == "local":
            b.setdefault("port", 9230)
            b.setdefault("headless", True)
            if not b["headless"]:
                no_screen = screenless(cfg) if no_screen is None else no_screen
                if no_screen:
                    b["headless"], b["no_screen"] = True, True
            b.setdefault("start", "always")
            b.setdefault("open", "about:blank")
            b.setdefault("idle_minutes", 0)
            b["url"] = f"http://127.0.0.1:{int(b['port'])}"
            if b["start"] not in ("always", "demand", "login"):
                b["problem"] = i18n.t("cli.browser.bad_start")
            if "chrome" in b and not (isinstance(b["chrome"], str) and b["chrome"].strip()):
                b["problem"] = i18n.t("cli.browser.bad_chrome")   # find_chrome would fail the whole supervisor
            idle = b["idle_minutes"]
            if isinstance(idle, bool) or not isinstance(idle, (int, float)) or not 0 <= idle < float("inf"):
                b["problem"] = i18n.t("cli.browser.bad_idle_minutes")
        elif kind == "remote":
            if not str(b.get("url") or "").startswith(("http://", "https://", "ws://", "wss://")):
                b["problem"] = i18n.t("cli.browser.bad_remote_url")
        else:
            b["problem"] = i18n.t("cli.browser.bad_kind")
        b.setdefault("risk", [])
        b.setdefault("description", i18n.t("cli.browser.local_description", name=name) if kind == "local"
                     else i18n.t("cli.browser.remote_description", name=name))
        out[name] = b
    return out


def catalog_entries(cfg):
    """Catalog tools: browser:<name> is an MCP server through our launcher (it waits for the browser)."""
    tools = {}
    for name, b in configured(cfg).items():
        if b.get("problem"):
            continue
        tools[f"browser:{name}"] = {
            "kind": "mcp", "source": "browser", "description": b["description"], "risk": sorted(b["risk"]),
            "server": {"command": PYTHON[0], "args": PYTHON[1:] + [os.path.join(REPO, "bin", "hub-browser"), "mcp", name]}}
    return tools


def profile_dir(state_dir, name, b):
    return os.path.expanduser(b.get("profile") or os.path.join(state_dir, "browsers", name))


# One Chrome.app has one icon in the dock: a window of the agents' profile is told from the person's own by its colour
THEME_COLOR = 0xFF2E7D32 - (1 << 32)   # ARGB as Chrome keeps it in Preferences: a signed 32-bit number


SEEDED = "hub-seeded"   # a file in the folder of a profile that this module made: the sign of "continue where you left off"


def seeded(profile):
    return os.path.isfile(os.path.join(profile, SEEDED))


def restoring(profile):
    """Chrome of this profile will restore the previous session when it starts: the module made the profile, Chrome kept a
    session there and did not crash (after a crash it restores nothing, the flag or not, and with no start page it opens a
    New Tab page). The tabs come back; the empty start page next to them would be one more tab at every start. A start page
    set in `open` is asked for, so it stays."""
    default = os.path.join(profile, "Default")
    try:
        if not (seeded(profile) and any(os.path.isfile(os.path.join(default, "Sessions", n))
                                        for n in os.listdir(os.path.join(default, "Sessions")))):
            return False
        with open(os.path.join(default, "Preferences"), encoding="utf-8") as f:
            return ((json.load(f).get("profile") or {}).get("exit_type")) != "Crashed"
    except (OSError, ValueError, AttributeError):
        return False


def seed_profile(profile, window=True):
    """A profile that is new (no folder, or an empty one) is marked as made by the module (SEEDED: chrome_command then starts
    its Chrome with --restore-last-session, without which it keeps neither the tabs nor the session cookies, and every
    sign-in without "remember me" is gone at the next start) and, for a window, gets a colour theme (the keys Chrome writes
    itself when a colour is picked in Customize Chrome). The setting "continue where you left off" is not written to
    Preferences: Chrome protects it with a hash and resets it on macOS and Windows. A folder with anything in it, a person's
    own Chrome profile included, is not touched. True if the profile was seeded."""
    prefs = os.path.join(profile, "Default", "Preferences")
    try:
        if os.path.isdir(profile) and os.listdir(profile):
            return False
        os.makedirs(profile, exist_ok=True)
        with open(os.path.join(profile, SEEDED), "w", encoding="utf-8") as f:   # first: a failure later must not lose it
            f.write("The agents' browser made this profile: its Chrome continues where it left off.\n")
        if window:
            os.makedirs(os.path.dirname(prefs), exist_ok=True)
            with open(prefs + ".tmp", "w", encoding="utf-8") as f:
                json.dump({"browser": {"theme": {"user_color2": THEME_COLOR}},
                           "extensions": {"theme": {"id": "user_color_theme_id"}}}, f)
            os.replace(prefs + ".tmp", prefs)   # a half-written file would be taken for Chrome's own at the next start
    except OSError:
        return False
    return True


def _open_shared(path):
    """Windows: open the file for reading as any program would (sharing read and write) and close it; OSError with
    winerror when it cannot. Python's open() goes through the C runtime and leaves winerror unset (only errno 13)."""
    import _winapi
    _winapi.CloseHandle(_winapi.CreateFile(path, 0x80000000, 3, 0, 3, 0x80, 0))   # GENERIC_READ, OPEN_EXISTING


def profile_held(profile):
    """Does a live Chrome hold this profile? Asked of the profile itself, not of a process listing: `ps` does not exist on
    Windows and hangs on a loaded Mac, and a launcher that took "no answer" for "not running" started a second Chrome,
    which handed its start to the live one: one more window on every slow CDP probe. Windows: Chrome keeps
    <profile>/lockfile open with no sharing, so opening it fails with a sharing violation. Elsewhere:
    <profile>/SingletonLock is a link to "host-pid" of this machine with a live pid, and <profile>/SingletonSocket
    accepts a connection (a live Chrome, even a stopped one, does; a lock left by a dead Chrome whose pid went to
    another process does not). False when the profile tells nothing."""
    if os.name == "nt":
        lockfile = os.path.join(profile, "lockfile")
        if not os.path.exists(lockfile):
            return False
        try:
            _open_shared(lockfile)
            return False
        except OSError as e:   # only a sharing violation is Chrome; read-only or an ACL is not
            return getattr(e, "winerror", None) == 32
    try:
        host, _, pid = os.readlink(os.path.join(profile, "SingletonLock")).rpartition("-")
    except OSError:
        return False
    if host != socket.gethostname() or not pid.isdigit():
        return False
    if not platforms.current.alive(int(pid)):
        return False
    return singleton_listens(profile)


def singleton_listens(profile):
    """Does something listen on <profile>/SingletonSocket? It is a link into a short temporary folder (a socket path
    is limited to ~100 bytes), so the link's target is connected to. A dead Chrome leaves the link: ECONNREFUSED or
    no file."""
    path = os.path.join(profile, "SingletonSocket")
    try:
        target = os.path.join(profile, os.readlink(path)) if os.path.islink(path) else path
    except OSError:
        return False
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1)
    try:
        s.connect(target)
    except TimeoutError:   # the backlog is full: someone listens
        return True
    except OSError:
        return False
    finally:
        s.close()
    return True


def clean_metrics(profile):
    """Remove the metrics files (4 MB each) that Chrome writes at the very start, before it reads any flag (none turns
    them off), and leaves when it dies there: a Chrome that failed at every start filled the disk with them. The caller
    makes sure no Chrome of the profile runs: a live one holds its own; a file a moment old may be that of a Chrome started
    just now by someone else, and stays. Returns how many files went."""
    gone, now = 0, time.time()
    for folder, pattern in ((os.path.join(profile, "BrowserMetrics"), r"BrowserMetrics.*\.pma"),
                            (profile, r"BrowserMetrics-spare\.pma")):
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for n in names:
            if re.fullmatch(pattern, n):
                try:
                    if now - os.path.getmtime(os.path.join(folder, n)) < 2:
                        continue
                    os.unlink(os.path.join(folder, n))
                    gone += 1
                except OSError:
                    pass
    return gone


def active_mark(state_dir, name):
    return os.path.join(state_dir, "browsers", f"{name}.active")


def touch_active(state_dir, name):
    """A session called a tool of the browser (or got a reply): the mtime of this file is when it was last used."""
    path = active_mark(state_dir, name)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a"):
            os.utime(path)
    except OSError:
        pass


def last_active(state_dir, name):
    """When a session last used the browser (s since the epoch); None if never."""
    try:
        return os.path.getmtime(active_mark(state_dir, name))
    except OSError:
        return None


def find_chrome(cfg, b=None):
    path = (b or {}).get("chrome") or (cfg.get("browser") or {}).get("chrome") or os.environ.get("HUB_CHROME")
    if path:
        import glob
        found = sorted(glob.glob(os.path.expanduser(path)))  # ".../chromium_headless_shell-*/...": the version changes
        return found[-1] if found else path
    for p in CHROME_CANDIDATES:
        if os.path.exists(p):
            return p
    import glob
    for p in CHROME_CANDIDATES:
        # A DMG dragged into /Applications whole (not just the .app) puts the .app one level deeper
        # (12.10: CONNECTION_CLOSED at browser:mac, Chrome in "/Applications/Google Chrome/Google Chrome.app/...")
        parts = p.split(os.sep)
        app = next((i for i, x in enumerate(parts) if x.endswith(".app")), None)
        if app is None:
            continue
        nested = sorted(glob.glob(os.path.join(os.sep.join(parts[:app]), "*", os.sep.join(parts[app:]))))
        if nested:
            return nested[0]
    for n in CHROME_NAMES:
        if shutil.which(n):
            return shutil.which(n)
    from core import runtime   # the Chrome for Testing «Setup» downloaded (core/runtime): the machine has none of its own
    got = runtime.chrome()
    if got:
        return got
    raise Problem(i18n.t("cli.browser.no_chrome"))


def _version(text):
    m = re.search(r"\d+(?:\.\d+){1,3}", text or "")
    return m.group(0) if m else None


def _ask(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def disk_version(exe, run=_ask):
    """The Chrome version on disk (not the running one): macOS, the app's Info.plist; Windows, the version folder next to
    chrome.exe (`--version` is silent there); others, `chrome --version`. None if unknown."""
    import plistlib
    from xml.parsers.expat import ExpatError
    plist = os.path.join(os.path.dirname(os.path.dirname(exe)), "Info.plist")
    if exe.endswith(".app/Contents/MacOS/" + os.path.basename(exe)) and os.path.exists(plist):
        try:
            with open(plist, "rb") as f:
                return _version(plistlib.load(f).get("CFBundleShortVersionString"))
        except (OSError, ValueError, plistlib.InvalidFileException, ExpatError):
            return None
    if exe.lower().endswith(".exe"):
        try:
            found = [n for n in os.listdir(os.path.dirname(exe)) if re.fullmatch(r"\d+(\.\d+){3}", n)]
        except OSError:
            found = []
        return max(found, key=lambda v: tuple(map(int, v.split(".")))) if found else None
    return _version(run([exe, "--version"]))


def newer(disk, running):
    """Is the one on disk newer than the running one ("HeadlessChrome/154.0.8037.93" from /json/version)?"""
    a, b = _version(disk), _version(running)
    if not a or not b:
        return False
    return tuple(map(int, a.split("."))) > tuple(map(int, b.split(".")))


def chrome_command(cfg, state_dir, name, b, headless=None):
    """A local browser: its own profile, CDP only on 127.0.0.1."""
    headless = b.get("headless", True) if headless is None else headless
    cmd = [find_chrome(cfg, b), f"--user-data-dir={profile_dir(state_dir, name, b)}",
           f"--remote-debugging-port={int(b['port'])}", "--remote-debugging-address=127.0.0.1",
           "--no-first-run", "--no-default-browser-check", "--window-size=1440,900"]
    if headless:
        cmd.append("--headless=new")
    profile = profile_dir(state_dir, name, b)
    if seeded(profile):
        cmd.append("--restore-last-session")   # a profile the module made; the setting in Preferences is reset by Chrome
    page = b.get("open") or "about:blank"
    return cmd + list(b.get("args") or []) + ([] if page == "about:blank" and restoring(profile) else [page])


# chrome-devtools-mcp sends usage statistics to Google by default and keeps a separate telemetry watchdog process
# per server. Data leaves only on the owner's word: browser.usage_statistics: true
NO_USAGE_STATISTICS = "CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS"


def env(cfg):
    """The environment for Chrome and MCP: PATH to node, libraries (on a VM everything is in /data), the browser.env
    settings; chrome-devtools-mcp usage statistics are off unless browser.usage_statistics is set. Neither needs the
    operator's or the session's keys: they are left out (redact.child_env); browser.env is set on purpose and stays, a
    reference to itself ("NPM_TOKEN": "$NPM_TOKEN") brings a left-out name back. A process of the previous release that
    loads this file anew keeps its own core.redact in memory: then the environment as before."""
    b = cfg.get("browser") or {}
    off = {} if b.get("usage_statistics") is True else {NO_USAGE_STATISTICS: "1"}
    base = redact.child_env(os.environ) if hasattr(redact, "child_env") else dict(os.environ)
    return {**base, **off, **expand_self_ref((b.get("env") or {}), os.environ)}


def probe(url, timeout=1.5):
    """The browser version if it answers over CDP; None if it does not. ws(s):// is checked via http(s)."""
    http = url.replace("wss://", "https://").replace("ws://", "http://")
    base = http.split("/devtools/")[0].rstrip("/")
    try:
        with urllib.request.urlopen(base + "/json/version", timeout=timeout) as r:
            return json.load(r).get("Browser") or "?"
    except (OSError, ValueError):
        return None


def identity(url, timeout=5):
    """Which Chrome answers: its CDP address (it carries an id made anew at every launch), "?" if the answer has none,
    None if nothing answers. A long timeout: a closed browser refuses at once, only a slow one waits."""
    http = url.replace("wss://", "https://").replace("ws://", "http://")
    base = http.split("/devtools/")[0].rstrip("/")
    try:
        with urllib.request.urlopen(base + "/json/version", timeout=timeout) as r:
            return json.load(r).get("webSocketDebuggerUrl") or "?"
    except (OSError, ValueError):
        return None


def _base(url):
    return url.replace("wss://", "https://").replace("ws://", "http://").split("/devtools/")[0].rstrip("/")


def tabs(url, timeout=3):
    """The browser's targets (/json/list)."""
    with urllib.request.urlopen(_base(url) + "/json/list", timeout=timeout) as r:
        return json.load(r)


def close_tab(url, tid, timeout=3):
    with urllib.request.urlopen(_base(url) + "/json/close/" + tid, timeout=timeout) as r:
        r.read()


def _ws_connect(ws_url, timeout):
    """A WebSocket to a CDP address (the stdlib: handshake only): (socket, what came after the handshake), None if it did
    not open."""
    import base64
    import socket
    from urllib.parse import urlparse
    try:
        u = urlparse(ws_url)
        s = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
    except (OSError, ValueError):
        return None
    try:
        s.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall((f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\n"
                   f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = s.recv(4096)
            if not chunk:
                raise OSError("connection closed")
            head += chunk
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise OSError("no upgrade")
    except (OSError, ValueError):
        s.close()
        return None
    return s, head.split(b"\r\n\r\n", 1)[1]


def _ws_send(s, message):
    """One masked text frame with the JSON of the message."""
    import struct
    payload = json.dumps(message).encode()
    n = len(payload)
    size = bytes([0x80 | n]) if n < 126 else b"\xfe" + struct.pack(">H", n) if n < 65536 else b"\xff" + struct.pack(">Q", n)
    mask = os.urandom(4)
    s.sendall(b"\x81" + size + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


def _ws_frames(s, buf):
    """The frames that come, as bytes; OSError when the connection ends or the wait runs out."""
    import struct

    def need(k):
        nonlocal buf
        while len(buf) < k:
            chunk = s.recv(65536)
            if not chunk:
                raise OSError("connection closed")
            buf += chunk

    while True:
        need(2)
        n, off = buf[1] & 0x7F, 2
        if n == 126:
            need(4)
            n, off = struct.unpack(">H", buf[2:4])[0], 4
        elif n == 127:
            need(10)
            n, off = struct.unpack(">Q", buf[2:10])[0], 10
        need(off + n)
        frame, buf = buf[off:off + n], buf[off + n:]
        yield frame


def tab_responds(ws_url, timeout=5.0):
    """Whether a tab answers Network.enable over CDP (what chrome-devtools-mcp hangs on): the reply with id 1."""
    got = _ws_connect(ws_url, timeout)
    if got is None:
        return False
    s, buf = got
    try:
        _ws_send(s, {"id": 1, "method": "Network.enable"})
        for frame in _ws_frames(s, buf):
            try:
                if json.loads(frame).get("id") == 1:
                    return True
            except ValueError:
                pass
    except (OSError, ValueError):
        return False
    finally:
        s.close()


def close_browser(url, timeout=3):
    """Ask a local Chrome to close itself (CDP Browser.close): it shuts down cleanly, writes its session and cookies and marks
    the profile's exit as normal; a signal leaves that to chance. True if the request was delivered (Chrome is going; wait
    for it), False if it could not be."""
    try:
        with urllib.request.urlopen(_base(url) + "/json/version", timeout=timeout) as r:
            ws = json.load(r).get("webSocketDebuggerUrl")
    except (OSError, ValueError, AttributeError):
        return False
    got = _ws_connect(ws, timeout) if isinstance(ws, str) else None
    if got is None:
        return False
    s, buf = got
    try:
        _ws_send(s, {"id": 1, "method": "Browser.close"})
        try:
            next(_ws_frames(s, buf), None)   # its reply, or the end of the connection: either way it heard
        except OSError:
            pass
        return True
    except OSError:
        return False
    finally:
        s.close()


def mcp_command(cfg, b, npm_dir=None, private_dir=None):
    """chrome-devtools-mcp to the browser's address. Never --autoConnect: we do not touch the owner's main Chrome.
    npm_dir is an empty folder for npx: without --prefix npx reads the package.json of the session folder and on a project with an
    unfamiliar npm (an npm: alias with a hash version, "Invalid comparator") fails before MCP starts. MCP's working folder stays the session
    folder: relative shot paths are counted from it.
    The headers of a remote browser (its key) go in a 0600 --config file in private_dir (chrome-devtools-mcp 1.10+), not on
    the command line, where `ps` shows them to every user of the machine; without private_dir they are refused. The tool
    checks that file on its own, so the endpoint is in it too; it also stays on the command line: an MCP before 1.10
    ignores --config, connects without headers and the remote refuses it, rather than it starting a Chrome of its own."""
    base = list((cfg.get("browser") or {}).get("mcp") or DEFAULT_MCP)
    npx = os.path.splitext(re.split(r"[\\/]", str(base[0]))[-1])[0].lower() == "npx"
    if npm_dir and npx and not any(str(a).split("=")[0] == "--prefix" for a in base[1:]):
        base[1:1] = ["--prefix", npm_dir]
    url = b["url"]
    ws = url.startswith(("ws://", "wss://"))
    target = ["--wsEndpoint", url] if ws else ["--browserUrl", url]
    if b.get("headers"):
        if not private_dir:
            raise ValueError("headers of a remote browser need a private folder for the MCP config file")
        platforms.current.makedirs_private(private_dir)
        path = os.path.join(private_dir, "mcp-config.json")
        write_private(path, {**({"wsEndpoint": url} if ws else {}), "wsHeaders": json.dumps(b["headers"])})
        target += ["--config", path]
    # --allow-unrestricted-paths (an earlier workaround for the same refusal) is not accepted by MCP together with --workspace, it would not start
    free = any(str(a).split("=")[0] == "--allow-unrestricted-paths" for a in base)
    return base + target + ([] if free else workspace_args(cfg)) + ["--viewport", VIEWPORT]


def workspace_args(cfg):
    """--workspace for every existing folder from browser.workspace (one that does not exist, like /tmp on Windows, is skipped: MCP
    would complain on every call). Absolute paths only: a relative one would mean something different in each session's folder. A string is
    one folder, not a list of letters (otherwise "/" from "/tmp" would open the whole disk). MCP versions without the flag (before 1.9) silently
    ignore it."""
    dirs = (cfg.get("browser") or {}).get("workspace")
    if dirs is None:
        dirs = WORKSPACE
    elif isinstance(dirs, str):
        dirs = [dirs]
    elif not isinstance(dirs, list):
        return []
    out = []
    for d in dirs:
        d = os.path.normpath(os.path.expanduser(str(d)))
        # on Windows "/tmp" is a path without a drive: for a session on another drive it would mean another folder
        if os.path.isabs(d) and (os.name != "nt" or os.path.splitdrive(d)[0]) and os.path.isdir(d) and d not in out:
            out.append(d)
    return [a for d in out for a in ("--workspace", d)]
