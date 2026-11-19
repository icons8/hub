"""The "page" module: the owner's channel in the browser. Optional; the core does not know about it.

Reads the core journal (like `hub status`) and writes actions as facts into the spool (like `hub send`) with the `web`
channel. Live updates use server-sent events. Stdlib only. Listens on localhost: whoever can reach it is the owner
(owner decision); from a phone it is also served on the Mac's tailnet IP (the tailnet module), admitting only the Mac's owner.
"""
import contextlib
import copy
import gzip
import hashlib
import json
import os
import re
import stat
import struct
import sys
import threading
import time
import uuid
import zlib
from urllib.parse import parse_qs, quote, unquote, urlparse, urlsplit
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if __name__ == "__main__":   # before the imports: output and files are UTF-8 under any locale
    import platforms
    platforms.current.utf8_mode([os.path.realpath(__file__)] + sys.argv[1:])
from core import board, catalog, coretext, cutout, forms, httpd, i18n, journal, limitresume, minihub, modelcall, modstate, owner, parking, pluginstyle, shown, spool, tools, views  # noqa: E402
import platforms  # noqa: E402
from modules.voice import transcribe as voice  # noqa: E402
from modules.jira import links as jira  # noqa: E402
from core.tail import Tail  # noqa: E402
from modules.scheduler.clock import read_status  # noqa: E402
from core.daemon import default_state_dir  # noqa: E402
from modules.tailnet import access  # noqa: E402
from modules.web import attach  # noqa: E402
from modules.web import pagecache, preload, stream_feed  # noqa: E402
from modules.web import stream_delta  # noqa: E402
from modules.web.settings import reactions_on  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STREAM_GAP = 1.0  # a snapshot goes into the page stream no more often than this, s
STREAM_WAIT = 15  # with no journal events the snapshot is re-checked after this many seconds (ping, module readiness)
STREAM_RECHECK = 60  # an open stream asks again who may read it (a remote token expired, a person removed), s
# a request waits for the journal reader lock (held by the poll) no longer than this, s: then 503 with Retry-After; if
# the client left, the request is dropped at once (core/httpd.py gone) and the socket does not hang until the lock;
# the first journal pass (Tail.ready) is not waited for at all
READ_WAIT, READ_STEP = 15.0, 0.5
FEED_POSTS_AT_ONCE = 20   # posts by number in one /api/feed request


class ReaderBusy(Exception):
    """The journal reader lock was not acquired within READ_WAIT (Handler.reading)."""


STATIC = os.path.join(HERE, "static")  # the page: index.html, css/, js/ (ES modules, no build), icons.svg
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml", ".json": "application/json",
                ".png": "image/png", ".ico": "image/x-icon", ".woff2": "font/woff2"}
FAVICON = os.path.join(HERE, "favicon.svg")
APP_ICONS = {f"/static/icons/{n}.png" for n in ("icon-192", "icon-512", "icon-maskable-512", "apple-touch-icon")}
APP_ICON_MAX = 2 * 1024 * 1024   # an app icon is tens of KB; a bigger file is not one
APP_ICON_SIDE = 192
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PNG_END = b"IEND\xaeB`\x82"   # the last chunk of every PNG: its type and CRC
# a page plugin's own media in its folder (/ext/<name>/… and a core module's /mod/<name>/web/…, API 1.32): served whole or by Range, never compressed, never
# sniffed, never a page (FILE_HEADERS) — pictures, sound and video for <img>/<audio>/<video>, wasm and 3D models for fetch
PLUGIN_MEDIA = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif",
                ".mp4": "video/mp4", ".webm": "video/webm", ".mp3": "audio/mpeg", ".ogg": "audio/ogg",
                ".wav": "audio/wav", ".wasm": "application/wasm", ".glb": "model/gltf-binary"}
THEME_LIGHT = "#f4f4f5"   # shell color of the light theme; the dark one (#161619) is in index.html, <meta name="theme-color">
# response compression (hub on another machine, page over a tunnel: a stream snapshot is 1.3 MB, ~2 MB of patches a
# minute; gzip is 7 times smaller): text only, no smaller than GZIP_MIN, only if the browser accepts gzip
GZIP_MIN = 1024
GZIP_TYPES = ("text/", "application/json", "image/svg+xml")
ART_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}  # theme art
# icons, avatars and the theme font (a theme can replace the profile icon, background and emoji set); svg only as an image
KIT_TYPES = {**ART_TYPES, ".svg": "image/svg+xml", ".gif": "image/gif", ".woff2": "font/woff2", ".woff": "font/woff",
             ".ttf": "font/ttf", ".otf": "font/otf"}
ART_MAX = 8 * 1024 * 1024
# Markdown in session replies: marked (MIT) + DOMPurify (Apache-2.0/MPL-2.0), versions pinned and shipped in the module —
# the page works without the internet. Only these files are served.
VENDOR = {"marked.min.js": "marked 15.0.7", "purify.min.js": "DOMPurify 3.4.16",
          "highlight.min.js": "highlight.js 11.11.1 (common)",
          "mermaid.min.js": "mermaid 11.17.2"}   # diagrams in markdown
VENDOR_DIR = os.path.join(HERE, "vendor")
# attachments and theme files: shown or downloaded, but never a page at the hub's address
FILE_HEADERS = {"Content-Security-Policy": "default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'; sandbox",
                "X-Content-Type-Options": "nosniff"}
MAX_TEXT = 20000
LAYOUT_MAX = 64 << 10   # a fleet layout of dozens of sessions is a few KB; anything bigger does not go into the journal
PATHS_MAX = 500  # paths per /api/paths request
# core commands via "/" — the same as on the page (js/commands.js, a test cross-checks them): the client calls POST path,
# session is the name of the open feed, the argument goes in the arg field; choices is the name of the list next to it
# in the snapshot (commands.models, commands.efforts)
# hints and arguments are catalog keys web.cmd.<name>_hint / _args (in the snapshot's language, commands_list)
COMMANDS = [{"name": n, "args": a, "hint": h, "path": p, "session": s, "arg": arg, **({"choices": c} if c else {})}
            for n, a, h, p, s, arg, c in (
    ("compact", "web.cmd.compact_args", "web.cmd.compact_hint", "/api/session/compact", True, "text", None),
    ("reload", "", "web.cmd.reload_hint", "/api/session/reload", True, None, None),
    ("model", "web.cmd.model_args", "web.cmd.model_hint", "/api/session/model", True, "model", "models"),
    ("effort", "web.cmd.effort_args", "web.cmd.effort_hint", "/api/session/effort", True, "level", "efforts"),
    ("stop", "", "web.cmd.stop_hint", "/api/interrupt", True, None, None),
    ("spec", "web.cmd.spec_args", "web.cmd.spec_hint", "/api/session/spec", True, "on", None),
    ("away", "web.cmd.away_args", "web.cmd.away_hint", "/api/away", False, "hours", None),
    ("back", "", "web.cmd.back_hint", "/api/owner/back", False, None, None),
    ("restart", "web.cmd.restart_args", "web.cmd.restart_hint", "/api/restart", False, "parts", None))]


def commands_list(lang):
    """Core commands via "/" for clients (the snapshot's commands.list): hints and arguments in the language lang."""
    return [{**c, "args": i18n.tr(lang, c["args"]) if c["args"] else "", "hint": i18n.tr(lang, c["hint"])}
            for c in COMMANDS]


MAX_FILE = 20 * 1024 * 1024  # base64 attachment (/api/upload): up to 20 MB (model images up to 5 MB, see hosts/streamjson.py)
RAW_MAX_MB = attach.RAW_MAX_MB   # raw-stream attachment (/api/upload/raw), MB; hub.json web.upload_max_mb


from core.version import PLUGIN_API  # noqa: E402 — the plugin API version (core/version.py)
from core.coretext import T  # noqa: E402
# "Fix" of a plugin: the task for Hub Developer (plugin_fix)
PLUGIN_FIX = T("core.web.plugin_fix",
               "Fix the plugin {name} ({source}{at}) and send a PR to its author. What is wrong with it: {why}. The hub's plugin API is {api} (EXTENDING.md, \"Page plugins\", \"Versions\"). Steps: fork the "
               "repo (gh repo fork {source} --clone), adapt it to the current API and the field hub = ^{api} in "
               "hub-plugin.json, check on a copy of the hub, a PR to the author describing what was changed and why. "
               "The hub installs the plugin from the fork, until the PR is accepted, on the owner's word — offer this in "
               "the result. The result goes to me: a link to the PR.")
EXT_NAME = r"[a-z0-9][a-z0-9-]*(?::[a-z0-9][a-z0-9-]*)?"  # an own extension or <author>:<name> from a marketplace
# catalog plugins that became part of the core (1.36): the core no longer loads them or their themes (two copies of the
# same code would change the page twice); "Plugins" says they can be removed. The page moves the choice made with them
IN_CORE = {"kalimullin:clean", "clean"}   # the Clean theme (modules/web/static/clean, js/themes.js); "clean" — an own copy
# Bundled plugins: live in the hub repo, enabled by default, updated together with the hub; in settings they only have "disabled".
BUNDLED = os.path.join(HERE, "bundled")


def live_extension(name, ext):
    """An extensions entry from settings that is not an enabled/disabled flag of a bundled plugin the hub no longer has
    (e.g. the threads view was removed, but hubs still have {"enabled": true} in hub.json): such an entry is not a plugin, skip it silently."""
    if not isinstance(ext, dict):
        return False
    return not set(ext) <= {"enabled", "disabled"} or os.path.isfile(os.path.join(BUNDLED, str(name), "hub-plugin.json"))


def query_number(q, key, default, kind=int):
    """A number from the URL (parse_qs); unreadable or infinite gives default: a malformed link gets a response, not a
    dropped connection."""
    try:
        value = kind((q.get(key) or [""])[0] or default)
    except (ValueError, OverflowError):
        return default
    return value if value == value and abs(value) != float("inf") else default


def lean_code(code):
    """The code card for the snapshot without what only "About the hub" / "Updates" read: the CHANGES.md entries (hundreds
    of KB) and the unpublished commit list (kept as an empty list: the page tests for its presence; the count stays).
    The full card is in /api/about."""
    out = {k: v for k, v in code.items() if k != "changes"}
    if "unpublished" in out:
        out["unpublished"] = []
    return out


class Web:
    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.spool = os.path.join(state_dir, "spool")
        self.model_limits = modelcall.Limits(state_dir)   # model calls made by modules: how many are running and how many happened in the last hour
        self.voice_lock, self.voice_answers, self.voice_meta, self.voice_clock = threading.Lock(), {}, {}, time.time   # raw recordings (voice_raw)
        self.started = time.time()   # the page has been running since — "About the hub"
        self.healed = None   # the update commit for which the page has already asked for a restart (heal)
        # core labels in feeds and the snapshot (views) are in the hub's language; if it changes, the feeds are re-read (core/tail.py)
        self.tail = Tail(os.path.join(state_dir, "journal.jsonl"), lang=self.state_lang, checkpoint=True,
                         on_rotate=lambda: self.history.build_in_background(appeared=True))   # the new archive is parsed without waiting for a scroll
        # send receipt: (recipient, client_id) -> the spool file of this page's latest send
        self.boxed, self.boxed_lock = {}, threading.Lock()
        self.view_lock, self._view = threading.Lock(), None   # shared snapshot build for event streams (stream_view)
        self.view_stalled = False   # its builder was not awaited within READ_WAIT: until it returns, streams build their own
        self.shown = shown.Index(state_dir)   # what /api/file serves — across the whole journal history (core/shown.py)
        from core.history import History
        self.history = History(state_dir)  # the feed's past, loaded when scrolling up
        self.history.build_in_background()
        from core import uses
        self.uses = uses.Reader(state_dir)   # "Skills and MCP": a session's uses; the archives are digested on first ask
        self.stop = threading.Event()
        self.sent_actions = {}   # button action key -> when it went to a session (once): a repeated request does not send it twice
        self.sent_lock = threading.Lock()
        self.req = threading.local()   # this thread's request: its language (Handler.lang) for response texts (t)
        from modules.web import push
        self.push = push.Push(state_dir, self.push_source, lambda: self.config(), lang=self.hub_lang)
        self.config = lambda: catalog.config_or_last(state_dir)
        from modules.web import modproxy
        self.modproxy = modproxy.Proxy(state_dir, self.mod_records)   # module endpoints /api/mod/<name>/… (Module API)
        from core import profiles
        # session sign-in (core/profiles.py): the "Accounts" snapshot, /api/accounts/use; the sign-in module's reply lives 5 s
        self.profiles = profiles.Profiles(state_dir, mods=self.modproxy.mods, config=lambda: self.config(), ttl=5)
        from modules.web.busweb import BusWeb
        self.bus = BusWeb(self)   # Settings -> Hubs and a contact's page: the hub bus for the owner (spec hubbus/ux)
        self.setup_lock, self._setup = threading.Lock(), None   # Settings -> Setup (modules/setup), made on first use
        self.bundle_lock = threading.Lock()   # «A file for support»: one build at a time
        self.probe_lock = threading.Lock()   # Settings -> Accounts "Refresh": one limits measurement at a time

    came_told = False
    came_lock = threading.Lock()

    def came(self, lang):
        """The owner's page opened: while the core has not met them (a fresh hub), it learns they came and the language
        their browser speaks (reducer owner.came) — the hub greets them in it, not in the machine's (owner 2026-11-19:
        a Russian browser on an English system was greeted in English). Once per process; the fold keeps the first."""
        with self.came_lock:   # two tabs at once: one fact
            if self.came_told or ((self.tail.lang_state or {}).get("owner") or {}).get("met"):
                return
            try:
                self.put("owner.came", None, {"lang": lang})
                self.came_told = True
            except OSError as e:   # the page matters more: the next page load tries again
                print(f"web: owner.came not written: {e}", file=sys.stderr, flush=True)

    def push_source(self):
        """For the push module: open alerts (only it counts them), the snapshot's sessions (the question text for the
        summarizer) and the pool account (the summarizer calls the model on it)."""
        snap = self.tail.snapshot
        return {"alerts": self.tail.alerts_now(), "sessions": snap.get("sessions") or [], "account": snap.get("account")}

    def instance_name(self):
        """This hub's name among the hubs of the machine (core/instance.py), None for the main hub: the tab and the app
        of another hub are told apart by it. Read from its anchor (cached by mtime there)."""
        from core import instance
        return instance.name(self.state_dir)

    def hub_lang(self, cfg=None):
        """The hub's language without a browser (core/i18n.py human): texts outside a page request (phone notifications)."""
        try:
            cfg = self.config() if cfg is None else cfg
        except (OSError, ValueError):
            cfg = {}
        return i18n.human(cfg, self.tail.lang_state)   # without the journal lock: re-reading a feed holds it for a long time

    def state_lang(self, st):
        """The hub's language from settings and this state (Tail calls it under its own lock)."""
        try:
            cfg = self.config()
        except (OSError, ValueError, AttributeError):
            cfg = {}
        return i18n.human(cfg, st)

    def lang_now(self):
        """The response text language: that of the request this thread is handling now (Handler.lang), otherwise the hub's."""
        ask = getattr(self.req, "lang", None)
        return ask() if callable(ask) else self.hub_lang()

    def t(self, key, **params):
        """Human-readable text in the response language (i18n/<language>.json)."""
        return i18n.tr(self.lang_now(), key, **params)

    def mod_records(self):
        """The journal reader's state["modules"]: whether modules are ready and not paused, as published by the latest
        journal poll (Tail.records), without the read lock: the module proxy looks at them on every request, while a
        poll under load holds the lock for seconds (module endpoints used to wait for it 2–30 s)."""
        return dict(self.tail.records)

    @staticmethod
    def mod_state(reader):
        """What the module rows (core/modules.rows) need from the journal reader's state, as a copy: called under its lock
        (Handler.reading); the disk read then happens without the lock."""
        st = reader.state
        return {"modules": copy.deepcopy(st.get("modules") or {}),
                "core_written": copy.deepcopy(st.get("core_written") or {}),
                **({"catalog": dict.fromkeys(st["catalog"])} if isinstance(st.get("catalog"), dict) else {})}

    def attachments_dir(self):
        return attach.root(self.state_dir)

    def save_upload(self, body):
        """A file from the page (base64) goes to attachments/<id>/<name>; return the description for the message."""
        import base64
        try:
            raw = base64.b64decode(body.get("data") or "", validate=True)
        except ValueError:
            return None, self.t("web.srv.file_unreadable")
        if not raw or len(raw) > MAX_FILE:
            return None, self.t("web.srv.file_size", mb=MAX_FILE // 1024 // 1024)
        path = attach.new_path(self.state_dir, body.get("name"))
        with open(path, "wb") as f:
            f.write(raw)
        return {"path": path, "name": os.path.basename(path),
                "mime": str(body.get("mime") or "application/octet-stream")[:100], "size": len(raw)}, None

    def upload_max(self, cfg=None):
        """The raw-stream attachment limit, bytes: hub.json web.upload_max_mb, RAW_MAX_MB by default."""
        return attach.max_bytes(self.config() if cfg is None else cfg)

    def upload_raw(self, h):
        """POST /api/upload/raw?name=…&mime=…: the body is the file itself (large attachments from the page and clients).
        Written to disk in chunks (modproxy.body), so only one chunk is in memory; the limit is upload_max; if the body
        did not arrive in full (the client left or went silent) or was not written, there is no file: it is written to
        <name>.part and renamed when complete (if the hub dies midway, a .part remains, not a truncated file under the
        real name). The response is the same description as for /api/upload."""
        from modules.web import modproxy as mp
        length = mp.content_length(h, True)
        if length is None:
            return
        limit = self.upload_max()
        if length > limit:
            mp.drain(h, length)
            return mp.refuse(h, 413, self.t("web.srv.file_too_big", size=mp.size_text(limit, self.lang_now())),
                             close=True)
        if not length:
            return mp.refuse(h, 400, self.t("web.srv.file_empty"))
        q = parse_qs(urlparse(h.path).query)
        mime = (q.get("mime") or [""])[0] or h.headers.get("Content-Type") or "application/octet-stream"
        path, got, failed = None, 0, None
        try:
            h.connection.settimeout(mp.IDLE)
            path = attach.new_path(self.state_dir, (q.get("name") or [""])[0])
            with open(path + ".part", "wb") as f:
                for chunk in mp.body(h, length):
                    f.write(chunk)
                    got += len(chunk)
            if got == length:
                os.replace(path + ".part", path)
        except OSError as e:   # disk full, no permission
            failed = self.t("web.srv.file_not_written", error=e.strerror or type(e).__name__)
        finally:
            try:   # the rest of the connection follows the server's setting (core/httpd.py conn_timeout)
                h.connection.settimeout(getattr(h.server, "conn_timeout", None))
            except OSError:
                pass
        if got == length and not failed:
            return h.send(200, json.dumps({"path": path, "name": os.path.basename(path), "mime": mime[:100],
                                           "size": got}, ensure_ascii=False))
        if path:
            for drop in (lambda: os.unlink(path + ".part"), lambda: os.rmdir(os.path.dirname(path))):
                try:
                    drop()
                except OSError:
                    pass
        if failed:
            return mp.refuse(h, 500, failed, close=True)
        # the client left mid-body: nobody to answer (body already closes the connection)

    def resolve_paths(self, items, local):
        """Paths from session texts -> where they lead (the page links only what can be opened):
        {"open": "#/<root>/<path>"} is a file in "Files", {"web": "https://…"} is a domain without http, {} is neither."""
        from modules.files import paths
        from modules.files.server import Files
        cfg = self.config()
        files = Files(config=lambda: cfg, state_dir=self.state_dir)
        projects = [p.get("path") for p in (cfg.get("projects") or {}).values() if isinstance(p, dict)]
        extra = [os.path.dirname(catalog.config_path(self.state_dir))]
        out, seen = [], {}
        for it in items[:PATHS_MAX]:
            it = it if isinstance(it, dict) else {}
            key = tuple(str(it.get(k) or "") for k in ("path", "cwd", "hint"))
            if key not in seen:
                path, cwd, hint = key
                found = paths.resolve(path, cwd or None, hint or None, extra, projects) if path else None
                hit = files.locate(found, local) if found else None
                seen[key] = {"open": f"#/{quote(hit[0])}/{quote(hit[1])}"} if hit else \
                    ({"web": paths.web_address(path)} if not found and paths.web_address(path) else {})
                if not hit and found and self.shown_file(found):
                    # an image the session showed (or a file it sent) outside the project folders: "Files" cannot
                    # open it over the tailnet, so link to it from the page itself, otherwise the path would turn
                    # into plain text and the thumbnail would vanish; a non-image is downloaded (/api/file)
                    seen[key] = {"file": "/api/file?path=" + quote(found, safe="")}
            out.append(seen[key])
        return out

    def files_target(self, path, local):
        """The file "Files" would open for this client by its absolute path (its /open: the same lookup and the same rights,
        the whole disk from the machine itself, project folders from the tailnet), or None."""
        from modules.files import paths
        from modules.files.server import Files
        cfg = self.config()
        found = paths.find(path, None, extra=[os.path.dirname(catalog.config_path(self.state_dir))])
        return found if found and Files(config=lambda: cfg, state_dir=self.state_dir).locate(found, local) else None

    def secret_fill(self, body):
        """A session's request to fill a secret into a form field: checks, confirmation and filling are in the engine (modules/secrets)."""
        from modules.secrets import engine
        item, field, who = body.get("item"), body.get("field"), body.get("session") or "unknown"
        if not (isinstance(item, str) and engine.NAME.match(item) and field in engine.FIELDS
                and isinstance(who, str) and engine.NAME.match(who)):
            return 400, json.dumps({"error": self.t("web.srv.secret_fill_args")}, ensure_ascii=False)
        if getattr(self, "secrets", None) is None:
            self.secrets = engine.Engine(self.config, lambda kind, entity, data: spool.put(
                self.spool, kind, entity, {**data, "channel": "web", "owner": False}, src="web"), state_dir=self.state_dir)
        code, out = self.secrets.fill(who, item, field)
        return code, json.dumps(out, ensure_ascii=False)

    def own_file(self, path):
        """An owner attachment: a file uploaded from the page (attachments/)."""
        return attach.inside(self.state_dir, path)

    def shown_file(self, path):
        """What /api/file serves besides owner attachments: what a session showed to the owner — sent
        (hub send-file, any type) or, for an image, named by path in a step or text; exactly those paths, not the
        folder around them, across the whole journal history (core/shown.py). "Files" (:8788) serves remote tailnet
        clients only the project folders, while screenshots live in temporary ones. Images by name are raster only:
        svg and html served from the page's address would be a script with its privileges; a sent file that is not
        raster, audio or video is served by /api/file only as a download, in a sandbox."""
        return self.shown.shown(path)

    def put(self, type, entity, data):
        return spool.put(self.spool, type, entity, {**data, "channel": "web", "owner": True}, src="web")

    decide_wait = 3.0   # how long to wait for the core's decision on a page command (tests without hubd use 0)

    def decide(self, type, entity, data, wait=None):
        """Put a fact with a req tag and wait for the core's decision (core/journal.decided) -> (seq, reason, spool file):
        (seq, None) — accepted, (seq, Why) — rejected, (None, None) — not recorded within decide_wait. Waits from the
        journal-end mark taken before the spool write, so under load the fact is not lost behind a batch of tail events."""
        req, path = uuid.uuid4().hex[:12], os.path.join(self.state_dir, "journal.jsonl")
        start = journal.mark(path)
        box = self.put(type, entity, {**data, "req": req})
        return (*journal.decided(path, req, self.decide_wait if wait is None else wait, 1 << 20, start=start), box)

    def said(self, why):
        """A refusal of the core as the response says it: its core phrases in the response language (core/coretext.py),
        the code kept. Whatever decides by the reason (codes, REFUSED_IN) reads it before this."""
        return coretext.human(why, self.lang_now())

    def queued(self):
        """The fact is in the spool, the core has not recorded it within decide_wait: the outcome comes later."""
        return 202, {"ok": True, "queued": True, "text": self.t("web.srv.queued")}

    def put_decided(self, type, entity, data, seq_out=False, raw=False):
        """A core command from the page (/compact, /reload…): put the fact and wait for the core's decision, like hub in
        the terminal (core/journal.decided); return a refusal to the owner in words, not silence (a core command to a
        sleeping session may be refused). If the core did not record it in time (hubd busy or not running), say so, not "accepted"."""
        seq, why, _ = self.decide(type, entity, data)
        if why:
            return 409, why if raw else self.said(why)
        if seq is None:
            return self.queued()
        return 202, ({"ok": True, "seq": seq} if seq_out else "ok")

    REFUSED_IN = re.compile(r"\((?P<type>[a-z_.]+) in (?P<state>[a-z-]+)\)$")   # the core's refusal code by state

    def act_decided(self, type, name, data, said):
        """put_decided for an action on a session: the core decides; its "not allowed in this state" refusal (the code
        "(type in state)" at the end of the reason, core/machines.refusal) is replaced by the page's phrase in the
        owner's language: said {state | "*": i18n key}, for the same states where the page used to refuse itself. Other
        refusals (permissions, manual mode for /reload) keep the core's words."""
        code, why = self.put_decided(type, name, data, raw=True)
        m = self.REFUSED_IN.search(why) if code == 409 and isinstance(why, str) else None
        key = (said.get(m["state"]) or said.get("*")) if m and m["type"] == type else None
        return (409, self.t(key, name=name)) if key else (code, self.said(why) if code == 409 else why)

    def resume(self, name):
        """POST /api/session/resume {session} -> 202 | 409 {error, code: not_paused} | 409 {error, code: not_yet, until}
        | 409 a refusal by state. The page answers by the same rule as the core (limitresume.held) before writing the
        fact, so "not yet" comes with the time it continues by itself; the core decides again (the reset or a new turn
        may come between the two) and its codes get the same answers."""
        def no(code, until=None, st=None):
            if code == "not_paused":
                return 409, {"error": self.t("web.srv.resume_not_paused", name=name), "code": code}
            at = limitresume.at(st, until, time.time()) if until else None
            # one a person stopped after the cut does not continue by itself: it waits for this button again
            if limitresume.stopped(st["sessions"].get(name) or {}):
                said = self.t("web.srv.resume_not_yet_stopped", at=at) if at else self.t("web.srv.resume_soon_stopped")
            else:
                said = self.t("web.srv.resume_not_yet", at=at) if at else self.t("web.srv.resume_soon")
            return 409, {"error": said, "code": code, "until": until}
        st = self.tail.reader.state
        sess = st["sessions"].get(name)
        why = limitresume.held(st, sess, time.time())
        # not running: the core's refusal by state comes first ("continues at" would not come true)
        if why and (why[0] == "not_paused" or (sess or {}).get("state") in limitresume.CONTINUABLE):
            return no(*why, st=st)
        code, said = self.act_decided("session.resume_requested", name, {}, {"asleep": "web.srv.resume_not_running"})
        if code == 409 and forms.code_of(said) in ("not_paused", "not_yet"):
            st = self.tail.reader.state
            why = limitresume.held(st, st["sessions"].get(name), time.time())
            return no(forms.code_of(said), why[1] if why and why[0] == "not_yet" else None, st=st)
        return code, said

    # --- send receipt (/api/send): whether the message was accepted, under what number, or a refusal with a reason -----
    USAGE_WAIT = 10.0   # how long /api/usage waits for the first read of the journal archives (core/usage.facts); longer gives 503
    READ_WAIT = 1.0     # how long to wait for the journal reader lock (a re-read from the start holds it for tens of seconds)
    BOXED_KEEP = 1000   # own spool files with a client_id that the page remembers (the "still in the spool" receipt)

    def send_receipt(self, name, data):
        """An owner message from the page goes to the spool, and the reply follows the core's decision: 202 accepted
        {seq, id}; a repeat of an accepted one (same client_id, code duplicate) is 202 accepted already with the
        accepted number; a refusal is 409 refused {error, code}; if the core did not decide within decide_wait it is
        202 queued (the outcome comes later via /api/send/receipt by client_id)."""
        cid = data.get("client_id")
        tag = {} if cid is None else {"client_id": cid}
        seq, why, box = self.decide("message.received", None, data)
        if cid is not None:
            self.remember_box(name, cid, box)
        if seq is None:
            return 202, {"ok": True, "receipt": "queued", "queued": True, "text": self.t("web.srv.send_queued"), **tag}
        if why is not None and forms.code_of(why) == forms.DUPLICATE and cid is not None:
            # the first one is already accepted: the number is its (reader not caught up or busy — no number, but "accepted")
            got = self.accepted(name, cid, poll=True)
            return 202, got if isinstance(got, dict) else {"ok": True, "receipt": "accepted", "already": True,
                                                           "seq": None, "id": None, "client_id": cid}
        if why is not None:
            code = forms.code_of(why)
            return 409, {"ok": False, "receipt": "refused", "error": str(self.said(why)), **({"code": code} if code else {}),
                         "seq": seq, **tag}
        mid = self.tail.peek(lambda r: f"m{seq}" if f"m{seq}" in r.state["messages"] else None, self.READ_WAIT,
                             poll=True)
        return 202, {"ok": True, "receipt": "accepted", "seq": seq, "id": None if mid is Tail.BUSY else mid, **tag}

    def remember_box(self, name, cid, box):
        """The spool file of a send to name with a client_id (a message, a card reply): the outcome is looked up by it later, with no second fact."""
        with self.boxed_lock:   # requests run in their own threads
            self.boxed.pop((name, cid), None)
            self.boxed[(name, cid)] = box
            while len(self.boxed) > self.BOXED_KEEP:
                del self.boxed[next(iter(self.boxed))]

    def accepted(self, name, cid, window=False, poll=False):
        """The core accepted a message to name with this client_id (state.web_dedup) -> an "already accepted" reply, None
        if not, Tail.BUSY if the journal reader is busy. window — only if a repeat would still be rejected now (a day):
        otherwise the core would accept it as new."""
        if not isinstance(cid, str):
            return None
        # an accepted message is never revoked: first use what the latest journal poll published (Tail.dedup), without
        # the read lock — under load the poll holds it for seconds, and the receipt of an accepted one answered 503 for minutes on end
        rec = (self.tail.dedup.get(name) or {}).get(cid) or self.tail.peek(
            lambda r: ((r.state.get("web_dedup") or {}).get(name) or {}).get(cid), self.READ_WAIT, poll=poll)
        if rec is Tail.BUSY:
            return rec
        if not rec or (window and time.time() - rec.get("ts", 0) >= modstate.DEDUP_WINDOW):
            return None
        mid = rec.get("id")   # "event N" if the fact did not create a message — null goes out
        return {"ok": True, "receipt": "accepted", "already": True, "seq": rec.get("seq"),
                "id": mid if isinstance(mid, str) and re.fullmatch(r"m\d+", mid) else None, "client_id": cid}

    def in_spool(self, name, cid, card=False):
        """Whether a message from the page to name with this client_id is in the spool (the core has not taken it yet);
        card — a reply to session name from a card. The page remembers its own send by file name; a foreign one, or one
        from before its restart, it looks up in the spool."""
        with self.boxed_lock:
            box = self.boxed.get((name, cid))
        if box:
            return os.path.exists(os.path.join(self.spool, box + ".json"))
        try:
            names = [n for n in os.listdir(self.spool) if n.endswith(".json")]
        except FileNotFoundError:
            return False
        for n in names:   # read-only: hubd deletes the file after writing it to the journal, a corrupt one too
            try:
                with open(os.path.join(self.spool, n), encoding="utf-8") as f:
                    e = json.load(f)
            except (OSError, ValueError):
                continue
            if not isinstance(e, dict) or not isinstance(e.get("data"), dict):
                continue
            d = e["data"]
            if e.get("type") == ("permission.answered" if card else "message.received") and e.get("src") == "web" \
                    and d.get("channel") == "web" and (e.get("entity") if card else d.get("to")) == name \
                    and d.get("client_id") == cid:
                return True
        return False

    def receipt(self, name, cid):
        """GET /api/send/receipt: the outcome of a send to name with a client_id — accepted {seq, id} (the core accepted
        it, web_dedup), pending (still in the spool), refused {error, code?, seq} (a refusal remembered by the page's
        journal reader), unknown (the core never accepted it and it is not in the spool). The spool is checked before
        the journal: hubd deletes the file after writing to the journal. If the journal reader is busy re-reading — 503: ask later."""
        if not (name and isinstance(cid, str) and 0 < len(cid) <= modstate.DEDUP_KEY_MAX):
            return 400, {"error": self.t("web.srv.receipt_query", n=modstate.DEDUP_KEY_MAX)}
        waiting = self.in_spool(name, cid)
        done = self.accepted(name, cid, poll=True)
        lang = self.lang_now()
        refused = self.tail.peek(lambda r: r.refusal(name, cid, lang), self.READ_WAIT) if done is None else None
        if done is Tail.BUSY or refused is Tail.BUSY:
            return 503, {"error": self.t("web.srv.receipt_busy")}
        if done:
            return 200, {"receipt": "accepted", "seq": done["seq"], "id": done["id"]}
        if waiting:
            return 200, {"receipt": "pending"}
        return 200, ({"receipt": "refused", **refused} if refused else {"receipt": "unknown"})

    # --- actions: validate and put a fact into the spool ---------------------------------------------------------
    MCP_LINK_WAIT = 8   # s: the link comes in about a second; the old way (pty) — up to a minute: the page asks /api/mcp/link

    def setup(self):
        """«Setup» (modules/setup): one per page process — it caches probes of programs."""
        with self.setup_lock:
            if self._setup is None:
                from modules.setup.setup import Setup
                self._setup = Setup(self.state_dir, lambda: self.config())
            return self._setup

    WORKING = 7 * 86400   # s: a session result this fresh says the hub's sign-in works («Setup», the band)

    def working(self):
        """Whether a session of this hub answered within WORKING (the snapshot's out_head_ts)."""
        since = time.time() - self.WORKING
        return any((s.get("out_head_ts") or 0) > since for s in self.tail.snapshot.get("sessions") or [])

    def setup_bundle(self, handler):
        """POST /api/setup/bundle -> the zip (attachment). A POST from the page (X-Hub, checked by do_POST), not a link
        another site could open; one at a time (it runs `hub doctor`)."""
        from core import supportbundle
        if not self.bundle_lock.acquire(blocking=False):
            return handler.send(409, json.dumps({"error": self.t("web.srv.setup_busy")}, ensure_ascii=False))
        try:
            name, data = supportbundle.build(self.state_dir)
        finally:
            self.bundle_lock.release()
        handler.send_response(200)
        handler.send_header("Content-Type", "application/zip")
        handler.send_header("Content-Disposition", f'attachment; filename="{name}"')
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.end_headers()
        handler.wfile.write(data)
        return None

    def setup_act(self, what, body):
        """POST /api/setup/run {step} | /api/setup/input {step, text} | /api/setup/cancel {step}."""
        step = str(body.get("step") or "")
        if what == "run":
            code, out = self.setup().run(step, self.lang_now())
        elif what == "input":
            code, out = self.setup().give(step, body.get("text"))
        else:
            code, out = self.setup().cancel(step)
        return code, out if code < 300 else self.t("web.srv.setup_bad") if code == 400 else self.t("web.srv.setup_idle")

    def voice_local(self, body=None):
        """GET/POST /api/voice/local: the built-in voice (modules/voice/install.py) for «Settings -> Modules -> Voice».
        {on: true, model?} starts the setup step `voice` with that model of the hub language's rows (none — the
        recommended one; its own process: the download outlives a page restart) — with voice on, a switch: the model in
        use works until the new one passes its check; a step already running — 409 with its model. {cancel: true}
        stops a running step (a switch keeps the model in use); {on: false} stops it and removes the files. -> {on,
        model, name, lang, hears, size, models, why, cmd, job}."""
        from modules.voice import install
        started = False
        if body is not None:
            if body.get("on") is True:
                model = body.get("model")
                if model is not None and model not in [m["id"] for m in install.rows(self.lang_now(), self.state_dir)]:
                    return 400, self.t("web.srv.setup_bad")
                running = self.setup().job("voice") or {}
                if running.get("state") == "running":
                    return 409, {"error": self.t("web.voicehelp.busy"), "model": running.get("model")}
                install.clear_off(self.state_dir)
                code, _ = self.setup().run("voice", self.lang_now(), arg=model)
                if code >= 300:
                    return code, self.t("web.srv.setup_bad")
                started = model or True
            elif body.get("cancel") is True:
                install.mark_stop(self.state_dir)   # also a step spawned a moment ago that has not written its file yet
                self.setup().cancel("voice")
            elif body.get("on") is False:
                running = (self.setup().job("voice") or {}).get("state") == "running"
                install.mark_off(self.state_dir)   # also a step spawned a moment ago that has not written its file yet
                self.setup().cancel("voice")
                if not running:   # a running step stops and removes what came by itself (modules/setup/job.step_voice)
                    install.remove(self.state_dir)
            else:
                return 400, self.t("web.srv.setup_bad")
        cfg = self.config()
        out = install.status(self.state_dir, self.lang_now(), cfg=cfg)
        job = self.setup().job("voice")
        if started and (job or {}).get("state") != "running":   # the step's process has not written its file yet
            job = {"step": "voice", "state": "running", **({"model": started} if isinstance(started, str) else {})}
        out.update(cmd=bool(voice.command(cfg)), job=job)
        return 200, out

    def setup_host(self, body):
        """POST /api/setup/host {host: claude|codex} — the app's band (app spec §4a.1): the engine the hub's sessions run
        on, chosen before any sign-in; hub.json "host" (codex — the key, claude — none) and a restart of the whole hub on
        the owner's word: the core comes up on it, its steps start the sign-in. Codex CLI the hub installs itself (none,
        or its own older copy) is installed first: the step codex asks the restart once it is in place (job.CODEX_RESTART;
        a failed install — the band's error and «Retry», no restart). A hub from git chooses in «Settings» as before (400)."""
        from modules.setup import job
        host = body.get("host")
        if not getattr(self.setup(), "app", False) or host not in ("claude", "codex"):
            return 400, self.t("web.srv.setup_bad")
        with catalog.edit_settings(catalog.config_path(self.state_dir)) as cfg:
            if (cfg.get("host") == "codex") == (host == "codex"):
                return 200, {"ok": True, "host": host, "same": True}
            if host == "codex":
                cfg["host"] = "codex"
            else:
                cfg.pop("host", None)
        with self.setup().lock:
            self.setup().cache.clear()
        if host == "codex" and job.hub_installs_codex(self.setup().item_codex()):
            job.restart_after_codex(self.state_dir)
            self.setup().run("codex")   # one already running (auto.py saw the setting first) is that one
            return 202, {"ok": True, "host": host, "installing": True}
        if host == "claude" and job.codex_restart_waits(self.state_dir):   # hubd never left Claude: nothing to restart
            job.restart_after_codex(self.state_dir, on=False)
            return 200, {"ok": True, "host": host}
        job.restart_after_codex(self.state_dir, on=False)
        self.put("hub.restart_requested", None, {"parts": [], "now": True,
                                                 "why": self.t("web.srv.setup_host_restart", host=host)})
        return 202, {"ok": True, "host": host}

    def mcp_login(self, body):
        """POST /api/mcp/login {server, fp?, confirmed?}: a sign-in to an MCP server that waits for one — in hubd
        (core/mcpwatch), the owner's word. -> 200 {attempt, url} (open it in a new tab) | 200 {attempt, done} (it was
        signed in already) | 202 {attempt, pending} (no link yet: /api/mcp/link) | 409 {error} (refused, did not work) |
        202 queued (the core did not decide in time)."""
        server, fp = body.get("server"), body.get("fp")
        if not isinstance(server, str) or not server or len(server) > 200 or not (fp is None or isinstance(fp, str)):
            return 400, self.t("web.srv.mcp_bad")
        seq, why, _ = self.decide("mcp.login_requested", None, {
            "server": server, **({"fp": fp} if fp is not None else {}),
            **({"confirmed": True} if body.get("confirmed") is True else {})})
        if why:
            return 409, self.said(why)
        if seq is None:
            return self.queued()
        return self.mcp_link({"attempt": f"l{seq}"}, wait=self.MCP_LINK_WAIT)

    def mcp_dismiss(self, body):
        """POST /api/mcp/dismiss {server, fp?}: the owner closes a row of the "MCP waits for a login" band — in the core
        (core/mcpwatch.wait_dismissed), so every device sees it closed. -> 200 | 409 {error}."""
        server, fp = body.get("server"), body.get("fp")
        if not isinstance(server, str) or not server or len(server) > 200 or not (fp is None or isinstance(fp, str)):
            return 400, self.t("web.srv.mcp_bad")
        seq, why, _ = self.decide("mcp.wait_dismissed", None, {"server": server, **({"fp": fp} if fp is not None else {})})
        if why:
            return 409, {"error": self.t("web.srv.mcp_dismiss_failed"), "why": self.said(why)}
        if seq is None:
            return self.queued()
        return 200, {"ok": True}

    def mcp_link(self, body, wait=0):
        """POST /api/mcp/link {attempt}: the sign-in link from the owner's file (core/mcplogin; it is not in the
        journal), or how the attempt ended."""
        from core import mcplogin, mcpwatch
        attempt = body.get("attempt")
        if not isinstance(attempt, str) or not mcplogin.ATTEMPT.fullmatch(attempt):
            return 400, self.t("web.srv.mcp_bad")
        end = time.time() + wait
        while True:
            url = mcplogin.link_of(self.state_dir, attempt)
            if url:
                return 200, {"ok": True, "attempt": attempt, "url": url}
            a = self.tail.peek(lambda r: (r.state.get("mcp_logins") or {}).get(attempt), self.READ_WAIT)
            a = a if isinstance(a, dict) else None   # Tail.BUSY: the journal is being reread — the next round
            if a and a.get("state") in ("done", "sent"):   # sent without a link: signed in without a human
                return 200, {"ok": True, "attempt": attempt, "done": True}
            if a and a.get("state") == "failed":
                return 409, {"error": self.t("web.srv.mcp_failed", server=a.get("server") or ""),
                             "why": mcpwatch.error_text(a) or "", "attempt": attempt}
            if time.time() >= end:
                return 202, {"ok": True, "attempt": attempt, "pending": True}
            time.sleep(0.2)

    def mcp_callback(self, body):
        """POST /api/mcp/callback {attempt, url}: the address from the browser bar after the sign-in (a phone, another
        computer: the browser could not reach the hub's localhost). It carries a one-time code: into a file for the
        owner only, the journal gets only "the address is given" (mcp.callback_given)."""
        from core import mcplogin
        attempt, url = body.get("attempt"), body.get("url")
        url = url.strip() if isinstance(url, str) else ""
        if not isinstance(attempt, str) or not mcplogin.ATTEMPT.fullmatch(attempt) or not mcplogin.RETURN.fullmatch(url):
            return 400, self.t("web.srv.mcp_url_bad")
        mcplogin.put_callback(self.state_dir, attempt, url)
        seq, why, _ = self.decide("mcp.callback_given", None, {"attempt": attempt})
        if why:
            mcplogin.drop_callback(self.state_dir, attempt)
            return 409, self.said(why)
        if seq is None:
            return self.queued()
        return 202, {"ok": True, "attempt": attempt}

    def spec_act(self, snap, body):
        """POST /api/spec {delegation, action: peek|accept|drop}: peek gives the file's path (the card opens it); accept
        copies the file as it is now and waits for the core (specmode.request)."""
        from core import specmode
        ready = self.tail.ready   # before the journal's first catch-up the board is empty: "try again", not "closed"
        if not ready:
            raise ReaderBusy()
        snap = self.tail.snapshot   # taken after ready (act's one may be the empty board of the catch-up)
        did = str(body.get("delegation") or "")
        d = next((x for x in snap.get("delegations") or [] if x.get("id") == did), None)
        if not d or not specmode.is_open(d):
            return 409, self.t("web.srv.spec_closed", id=did)
        if body.get("action") == "peek":
            return 200, specmode.card(self.state_dir, d)
        code, out = specmode.request(self.state_dir, {"delegations": {did: d}}, did, body.get("action"),
                                     channel="web", wait=self.decide_wait)
        if out.get("error"):
            return 409, self.said(out["error"]) if out["error"] != "closed" else self.t("web.srv.spec_closed", id=did)
        return 202, ({"ok": True, "queued": True, "text": self.t("web.srv.queued")} if out.get("queued") else "ok")

    def act(self, path, body):
        ready = self.tail.ready   # before the snapshot: if the catch-up finishes between the two, the snapshot is still empty while "ready" is already true
        snap = self.tail.snapshot
        sessions = {s["name"]: s for s in snap["sessions"]}
        if path == "/api/bus/act":   # Settings -> Hubs, a contact's page: the owner's quick actions and views (busweb.py)
            return self.bus.act(body)
        if path == "/api/bus/start":   # "Link with a hub" / "Link" after a pasted link: a job with steps
            return self.bus.start(body)
        if path == "/api/bus/job":   # the job's steps and outcome (the made link only here)
            return self.bus.job_view(body)
        if path == "/api/push/register":  # phone client: the device token for notifications (modules/web/push.py)
            if not self.push.settings():
                return 409, self.t("web.srv.push_off")
            ok = self.push.register(body.get("token"), body.get("env") or "production", body.get("mode"),
                                    body.get("caps"))   # what the client can do (API 1.26): clear — removes the banner
            return (200, {"ok": True}) if ok else (400, self.t("web.srv.push_no_token"))
        if path == "/api/upload":
            meta, problem = self.save_upload(body)
            return (200, meta) if meta else (400, problem)
        if path == "/api/mcp/login":   # "Sign in" on the "MCP waits for a login" band (core/mcpwatch)
            return self.mcp_login(body)
        if path == "/api/mcp/dismiss":   # the cross on the band: close the row (core/mcpwatch.wait_dismissed)
            return self.mcp_dismiss(body)
        if path == "/api/mcp/link":    # the link of a sign-in that has begun (the page was reloaded, the link was slow)
            return self.mcp_link(body)
        if path == "/api/mcp/callback":   # the address from the browser bar after a sign-in on another device
            return self.mcp_callback(body)
        if path == "/api/plugins/fix":  # a button on a broken plugin: Hub Developer fixes it locally and sends a PR to the author
            return self.plugin_fix(str(body.get("name") or ""))
        if path == "/api/voice/local":   # Settings -> Modules -> Voice: turn the built-in model on (download) or off
            return self.voice_local(body)
        if path in ("/api/setup/run", "/api/setup/input", "/api/setup/cancel"):   # Settings -> Setup (modules/setup)
            return self.setup_act(path.rsplit("/", 1)[1], body)
        if path == "/api/setup/host":   # the app's band: Claude or Codex, before any sign-in
            return self.setup_host(body)
        if path == "/api/codex/login":   # experiment: "Settings -> Codex" — a device-code sign-in (the link and code in the answer)
            # the cloud row of the voice (modules/voice/chatgpt.py, the team flavor) signs a Claude hub in too: its folder
            if not (catalog.codex_host(self.config()) or body.get("for") == "voice" and body.get("account") is None
                    and cutout.present("voice_cloud")):
                return 400, self.t("web.srv.codex_off")
            from core import codexcli
            name = body.get("account")
            if name is None:
                got = codexcli.start_login(self.state_dir)
            else:   # a row of the Codex account pool (core/codexslot.py): its folder, signed out first
                row = codexcli.pool_rows(self.state_dir, self.tail.reader.state).get(str(name))
                if row is None:
                    return 404, self.t("web.srv.codex_no_account")
                lost = name in ((((self.tail.reader.state.get("codex") or {}).get("account") or {}).get("lost")) or {})
                got = self.codex_sign_in(name, row["home"], hub=bool(row.get("hub")), again=lost)
                if got is None:
                    return 409, self.t("web.srv.codex_not_ready")
            if got.get("code") and got.get("account") != name:   # one sign-in at a time: another account's is going on
                return 409, self.t("web.srv.codex_busy", name=got.get("account") or self.t("web.codex.row_hub"))
            if (got.get("result") or {}).get("ok") is False and not got.get("code"):
                return 502, str(got["result"].get("why") or "")
            return 202, {"ok": True, "url": got.get("url"), "code": got.get("code"), **({"account": name} if name else {})}
        if path == "/api/codex/add":   # experiment: "Settings -> Codex" — a new account of the Codex pool, its sign-in
            if not catalog.codex_host(self.config()):
                return 400, self.t("web.srv.codex_off")
            from core import codexslot
            if not codexslot.present():   # the open flavor: one sign-in, no pool (the page shows no "Add")
                return 404, self.t("web.srv.codex_add_failed", why=self.t("core.codexpool.no_provider"))
            name, home = codexslot.pool(self.state_dir, self.profiles.mods(), self.config, time.time).add()
            if not name:
                return 503, self.t("web.srv.codex_add_failed", why=home or "")
            got = self.codex_sign_in(name, home)
            if got is None:
                return 409, self.t("web.srv.codex_not_ready")
            if got.get("code") and got.get("account") != name:   # another account's sign-in is going on: its row stays
                return 409, self.t("web.srv.codex_busy", name=got.get("account") or self.t("web.codex.row_hub"))
            if (got.get("result") or {}).get("ok") is False and not got.get("code"):
                return 502, str(got["result"].get("why") or "")
            return 202, {"ok": True, "url": got.get("url"), "code": got.get("code"), "account": name}
        if path == "/api/codex/logout":
            if not catalog.codex_host(self.config()):
                return 400, self.t("web.srv.codex_off")
            from core import codexcli
            try:
                codexcli.logout_home(self.state_dir)
            except (OSError, RuntimeError) as e:
                return 502, str(e)
            self.put("codex.logout", None, {})
            return 202, {"ok": True}
        if path == "/api/accounts/use":  # Settings -> Accounts, clients: switch to an account now
            # {account|name, force?} -> 200 {ok, active} | 404 not in the pool | 409 paused or busy ({error, code, until, forceable, why}); without force
            # a busy account (limit, threshold) is refused, with force the switch happens and holds until a real API refusal
            name, force = str(body.get("account") or body.get("name") or ""), body.get("force") is True   # "false" is not force
            acc = self.tail.reader.state.get("account")
            bad = self.profiles.use(acc, name, force)   # session sign-in (core/profiles.py): the pool or the sign-in module
            if bad and bad["code"] == "none":
                return 503, self.t("web.srv.account_no_module")
            if bad and bad["code"] == "unknown":
                return 404, self.t("web.srv.account_unknown")
            if bad and bad["code"] == "paused":
                return 409, {"error": self.t("web.srv.account_none"), "code": "paused"}   # a client tells them by code
            if bad:
                return 409, {"error": self.t("web.srv.account_busy", name=name, why=bad.get("why") or ""), "code": bad["code"],
                             "until": bad.get("until"), "forceable": bool(bad.get("forceable")), "why": bad.get("why") or ""}
            self.put("account.switched", None, {"to": name, "from": (snap.get("account") or {}).get("active"),
                                                "restart": True, "manual": True,
                                                "why": i18n.tr(i18n.owner(), "web.srv.picked_manually"),
                                                **({"force": True} if force else {})})
            return 200, {"ok": True, "active": name, "text": self.t("web.srv.account_moving", name=name)}
        if path == "/api/accounts/probe":  # Settings -> Accounts "Refresh": a fresh limits measurement without a switch
            return self.accounts_probe(body)
        if path == "/api/modules/toggle":  # Settings -> Modules: the toggle of an optional module
            return self.module_toggle(str(body.get("id") or ""), bool(body.get("enabled")))
        if path == "/api/modules/enable":  # Modules -> a third-party module (Module API): modules.enabled in hub.json
            return self.module_enable(str(body.get("name") or ""), bool(body.get("enabled")),
                                      body.get("embed") if isinstance(body.get("embed"), bool) else None)
        if path == "/api/modules/model":  # Modules -> "Allow model": modules.model in hub.json (core/modelcall.py)
            return self.module_model_allow(str(body.get("name") or ""), bool(body.get("allowed")))
        if path in ("/api/modules/reset", "/api/modules/forget"):   # = hub modules reset|forget <name>
            from core import modules
            name = str(body.get("name") or "")
            if not modules.NAME.fullmatch(name):
                return 400, self.t("web.srv.module_bad_name")
            return self.put_decided("module." + path.rsplit("/", 1)[1], name, {"module": name})
        if path == "/api/plugins/toggle":  # Settings -> Plugins: disable / enable without removing
            return self.plugin_toggle(body.get("name"), bool(body.get("enabled")))
        if path == "/api/plugins/review":  # "Review and install": a clone at the commit and a task for Hub Developer
            from modules.market.market import repo_of
            return self.plugin_review(repo_of(body.get("repo")) or str(body.get("repo") or ""))
        accept = body.get("accept") is True   # "Allow and update": the new permissions the update asked about
        if path == "/api/plugins/install" and body.get("entry"):   # an entry of a connected catalog: <name>@<catalog>
            return self.plugin_entry("preview" if body.get("preview") is True else "install", str(body["entry"]), accept,
                                     str(body["at"]) if body.get("at") else None)
        entry = self.from_catalog(str(body.get("name") or "")) if path in ("/api/plugins/update", "/api/plugins/remove") else None
        if entry:
            if body.get("reviewed"):   # code review belongs to the previous path (the author's head), a catalog entry is exactly its sha
                return 400, self.t("web.srv.plugin_from_catalog", entry=entry)
            return self.plugin_entry(path.rsplit("/", 1)[1], str(body["name"]), accept,
                                     str(body["at"]) if body.get("at") and path.endswith("/update") else None)
        if path == "/api/plugins/install" and body.get("reviewed"):  # "Install the reviewed one" — exactly this clone
            return self.plugin_market("install_reviewed", str(body.get("repo") or ""), accept)
        if path == "/api/plugins/update" and body.get("reviewed"):  # "Update with the reviewed one"
            self.updates_cache = None
            return self.plugin_market("update_reviewed", str(body.get("name") or ""), accept)
        if path in ("/api/plugins/install", "/api/plugins/remove", "/api/plugins/update"):  # the owner from the page
            return self.plugin_market(path.rsplit("/", 1)[1], str(body.get("repo") or body.get("name") or ""), accept)
        if path == "/api/voice":  # the Voice module: a microphone recording -> text in the input field (the owner edits and sends it)
            return self.transcribe(body)
        if path == "/api/feed/post":  # the owner writes to a plugin channel from the page (API 1.2)
            from core.reducer import POST_MAX
            data = {"plugin": str(body.get("plugin") or ""), "text": str(body.get("text") or "")}
            if len(data["text"].strip()) > POST_MAX:   # do not truncate silently (API 1.22): clients put JSON here too
                return 400, self.t("web.srv.post_long", max=POST_MAX)
            if body.get("reply_to"):
                data["reply_to"] = str(body["reply_to"])
            self.put("ext.post", None, data)
            return 202, {"ok": True}
        if path == "/api/owner/back":   # the "away mode" strip -> "I'm back"; /back in the input field
            if not ready:   # the snapshot is still empty: "not in away mode" judged by it would be false
                raise ReaderBusy()
            if (snap.get("owner") or {}).get("state") != "away":
                return 409, self.t("web.srv.not_away")
            code, out = self.put_decided("owner.back", None, {})
            return (code, {"ok": True, "text": self.t("web.srv.welcome_back")}) if out == "ok" else (code, out)
        if path == "/api/update/check":  # About the hub -> "Check for updates": the update process checks now
            from modules.update.update import NOW_FLAG
            open(os.path.join(self.state_dir, NOW_FLAG), "w").close()
            return 202, {"ok": True, "text": self.t("web.srv.update_checking")}
        if path == "/api/update/fix":   # Settings -> the red band "own edits of the core": a task for Hub Developer to sort them out
            if not ready:
                raise ReaderBusy()
            return self.update_fix(snap.get("update_check") or {})
        if path == "/api/update/local-edits":   # the band: "Take off" / "Put back" of the hub's own edits (modules/update/takeoff.py)
            if not ready:
                raise ReaderBusy()
            return self.update_local_edits(str(body.get("to") or ""), snap.get("update_check") or {})
        if path in ("/api/update/install", "/api/update/rollback"):
            # the app (installer S3): "Update" and "Return <previous>" go straight to the update process, not through a
            # turn of the hub session (the hub may not be signed in yet, or busy); a hub from git keeps its route
            from core import packaged
            from modules.update.update import INSTALL_FLAG, ROLLBACK_FLAG
            if not packaged.is_app(packaged.info()):
                return 409, self.t("web.srv.update_app_only")
            if path == "/api/update/install":
                open(os.path.join(self.state_dir, INSTALL_FLAG), "w").close()
                return 202, {"ok": True, "text": self.t("web.srv.update_installing")}
            version = str(body.get("version") or "")
            # a release vX.Y.Z of the app from git; the code layer's version (1.9.3, 1.9.3-nightly.…; core/appcode.NAME)
            from core import appcode
            if not appcode.version_ok(version, (packaged.info() or {}).get("layout") == "layer"):
                return 400, self.t("web.srv.update_rollback_version")
            flag = os.path.join(self.state_dir, ROLLBACK_FLAG)
            with open(flag + ".part", "w") as f:   # whole or not at all: the update process reads it every 15 s
                f.write(version)
            os.replace(flag + ".part", flag)
            return 202, {"ok": True, "text": self.t("web.srv.update_rolling_back", version=version)}
        if path == "/api/app/restore":   # About the hub -> "Return the released": the app's edited code layer back to its release
            return self.app_restore()
        if path == "/api/app/undo":   # About the hub -> "Return to the clone": the move from a git clone undone (core/appmove.py)
            return self.app_undo()
        if path == "/api/settings/title":  # the hub's name — the browser tab and the panel title
            title = " ".join(str(body.get("title") or "").split())
            if not 1 <= len(title) <= 40:
                return 400, self.t("web.srv.title_length")
            with self.market().editing() as cfg:
                cfg["title"] = title
            self.put("settings.changed", None, {"what": "title"})   # the page gets a snapshot right away
            return 200, {"ok": True, "text": self.t("web.srv.title_set", title=title)}
        if path == "/api/settings/set":  # the Settings tab: only the allowlist in modules/web/settings.py
            return self.settings_set(str(body.get("key") or ""), body.get("value"))
        if path.startswith("/api/schedule/"):
            if not ready:   # the snapshot is still empty: "no such job" judged by it would be false
                raise ReaderBusy()
            return self.schedule_act(path, body, snap, sessions)
        # commands via "/" in the input field (js/commands.js): the same facts as hub away / back / restart in the terminal
        if path == "/api/away":
            hours = body.get("hours")
            if hours is not None and not (isinstance(hours, (int, float)) and 0 < hours <= 24 * 30):
                return 400, self.t("web.srv.hours_positive")
            return self.put_decided("owner.away", None, {"hours": hours})
        if path == "/api/hold":   # a tab holds off a hub restart while it has undelivered messages (core/restart.py, js/api.js)
            from core import restart
            holds = body.get("holds")
            if not isinstance(holds, list) or not isinstance(body.get("tab"), str) or not body["tab"]:
                return 400, "tab, holds"
            holds = list(dict.fromkeys(str(h)[:restart.HOLD_NAME] for h in holds[:64] if h))[:restart.HOLD_MAX]   # same as the core
            self.put("page.held", None, {"tab": body["tab"][:restart.HOLD_TAB], "holds": holds})
            return 200, {"ok": True}
        if path == "/api/restart":
            from core import restart
            parts = [str(p) for p in body.get("parts") or []]
            bad = [p for p in parts if p not in restart.PARTS and not p.startswith("ext:")]
            if bad:
                return 400, self.t("web.srv.no_part", bad=", ".join(bad), parts=", ".join(restart.PARTS))
            return self.put_decided("hub.restart_requested", None, {"parts": parts})
        if path == "/api/layout":   # the whole session-list layout (core/layout.py); the reply is its new number (seq)
            if len(json.dumps(body.get("layout"), ensure_ascii=False)) > LAYOUT_MAX:
                return 413, self.t("web.srv.layout_big")
            return self.put_decided("layout.set", None, {"layout": body.get("layout"), "base": body.get("base")},
                                    seq_out=True)
        if path == "/api/spec":   # "Accept" / "Drop" of an executor's spec (core/specmode.py); peek — what to accept
            return self.spec_act(snap, body)
        name = body.get("session")
        if path == "/api/send" and isinstance(name, str) and name not in sessions:
            # a repeat of an already accepted message (the reply was lost) when the session is gone from the snapshot:
            # answer "accepted" with the first one's number, not 404 (if the session exists, the core itself rejects
            # the repeat with the same reply)
            done = self.accepted(name, body.get("client_id"), window=True)
            if isinstance(done, dict):   # reader busy (BUSY) — same as without a key: the core itself rejects the repeat
                return 202, done
        if path == "/api/seen" and isinstance(name, str) and name.startswith(":hub:"):
            # the owner opened a linked hub's page: its words up to ts are read (the core keeps it: every device)
            if not ready:
                raise ReaderBusy()   # the contacts are not read yet: "try again", not "no such hub"
            if name[5:] not in ((self.tail.reader.state.get("bus") or {}).get("contacts") or {}):
                return 404, self.t("web.srv.no_session", name=name)
            ts = body.get("ts")   # a word's time, as the core takes it (a mark from the future would hide later answers)
            if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not 0 <= ts <= time.time() + 86400:
                return 400, self.t("web.srv.seq_number")
            self.put("owner.seen", name, {"ts": ts})
            return 202, "ok"
        if name not in sessions and not ready:   # before the journal's first catch-up the snapshot is empty: not "no such session"
            raise ReaderBusy()                     # (a refusal the client would not retry), but "try again"
        # a message from the owner to a session that is gone or never was is not refused: the core passes it to the hub
        # (or to whoever raised a removed executor) with the addressee named — a session-shaped name only, as the core takes it
        from core.reducer import NAME
        if name not in sessions and not (path == "/api/send" and isinstance(name, str) and NAME.fullmatch(name)):
            return 404, self.t("web.srv.no_session", name=name)
        if path == "/api/session/icon":   # the session's own icon (palette); empty clears it
            return self.put_decided("session.iconed", name, {"icon": body.get("icon") or None})
        if path == "/api/seen":  # the owner is viewing the session: its results up to seq are read (read/unread)
            try:
                seq = int(body.get("seq") or 0)
            except (TypeError, ValueError):
                return 400, self.t("web.srv.seq_number")
            self.put("owner.seen", name, {"seq": seq})
            return 202, "ok"
        if path == "/api/inject":  # the owner pushes a queued message through in the middle of a turn
            mid = str(body.get("id") or "")
            if not re.fullmatch(r"m[\w]+", mid):
                return 400, self.t("web.srv.message_id")
            spool.put(self.spool, "message.inject", mid, {"channel": "web", "owner": True}, src="web")
            return 202, "ok"
        if path == "/api/queue/cancel":  # the owner removes a message from the queue without waiting for the turn
            mid = str(body.get("id") or "")
            if not re.fullmatch(r"m[\w]+", mid):
                return 400, self.t("web.srv.message_id")
            self.put("message.cancel", mid, {})
            return 202, "ok"
        if path == "/api/dismiss":  # clear the watchdog's "reply?" mark on the session
            self.put("owner.dismissed", name, {})
            return 202, "ok"
        if path == "/api/send":
            text = (body.get("text") or "").strip()
            if (not text and not body.get("attachments")) or len(text) > MAX_TEXT:
                return 400, self.t("web.srv.message_size", max=MAX_TEXT)
            files = body.get("attachments") or []
            # not an owner upload (/api/upload) — refuse with a reason rather than silently dropping the file (API 1.22)
            bad = [f for f in files if not (isinstance(f, dict) and self.own_file(f.get("path")))] \
                if isinstance(files, list) else [files]
            if bad:
                what = ", ".join(str(f.get("name") or f.get("path")) if isinstance(f, dict) else str(f)
                                 for f in bad)[:300]
                return 400, self.t("web.srv.attachment_refused", what=what)
            # the mark of a pasted image, as in Claude Code: "[Image #1]" in the text <-> an attachment
            files = [{**{k: v for k, v in f.items() if k != "label"},
                      **({"label": f["label"]} if re.fullmatch(r"Image #\d{1,3}", str(f.get("label") or "")) else {})}
                     for f in files]
            if not text and files:
                text = "(attachments)"   # what the session reads as the owner's message text
            data = {"to": name, "text": text, "attachments": files}
            if body.get("wake") is True:   # the owner writes in the field of a worker that has given up — wake it
                data["wake"] = True
            if body.get("action") is True:   # a page button ("Install" in "About the hub") — not a reply to a card
                data["action"] = True
            if body.get("reply_to"):
                data["reply_to"] = str(body["reply_to"])
            if body.get("card") is not None:   # a reply to the named card qN, not the latest one (the core will check it)
                if not (isinstance(body["card"], str) and re.fullmatch(r"q\d+", body["card"])):
                    return 400, self.t("web.srv.card_id")
                data["card"] = body["card"]
            if isinstance(body.get("quote"), str) and body["quote"].strip():
                data["quote"] = body["quote"][:1000]  # what the owner is replying to; the core takes the beginning for the label
            tasks = board.clean_tasks(body.get("tasks"))   # the board cards the owner named; the core puts them into the label
            if tasks:
                data["tasks"] = tasks
            if body.get("client_id") is not None:   # a repeat after a drop with the same id gets 202 already; one message (dedup)
                cid = body["client_id"]
                if not (isinstance(cid, str) and 0 < len(cid) <= modstate.DEDUP_KEY_MAX):
                    return 400, self.t("web.srv.client_id_bad", n=modstate.DEDUP_KEY_MAX)
                data["client_id"] = cid
            if body.get("app") is not None or body.get("app_version") is not None:
                # the client that sent it (the label says "from the owner · Android 0.2.7"): a known client, a short plain
                # version/build, else 400 - the meaning is not changed silently
                app, ver = body.get("app"), body.get("app_version")
                if not (isinstance(app, str) and app in forms.APPS) or not (ver is None or isinstance(ver, str) and forms.APP_VERSION.fullmatch(ver)
                                                 and ver == ver.strip()):
                    return 400, self.t("web.srv.app_bad")
                data["app"] = app
                if ver is not None:
                    data["app_version"] = ver
            if body.get("todo") is not None:   # which item of the client's to-do list the free-text reply is for — the core does not interpret it
                item = body["todo"]
                if not (isinstance(item, str) and 0 < len(item) <= modstate.DEDUP_KEY_MAX):
                    return 400, self.t("web.srv.todo_bad", n=modstate.DEDUP_KEY_MAX)
                data["todo"] = item
            if body.get("ref") is not None:   # which reference or note of the client the reply is for (its own thread) — the core does not interpret it
                item = body["ref"]
                if not (isinstance(item, str) and 0 < len(item) <= modstate.DEDUP_KEY_MAX):
                    return 400, self.t("web.srv.ref_bad", n=modstate.DEDUP_KEY_MAX)
                data["ref"] = item
            return self.send_receipt(name, data)
        if path == "/api/react":   # the owner's emoji reaction: to their own message mN or to a session row (event number)
            if not reactions_on(self.config()):
                return 409, self.t("web.srv.reactions_off")
            target, emoji = str(body.get("target") or ""), str(body.get("emoji") or "").strip()
            if not target or len(emoji) > 16:
                return 400, self.t("web.srv.react_needed")
            # a core refusal (no such session or message) is shown in words on the page, not silently
            data = {"target": target, "emoji": emoji, "session": name, "quote": str(body.get("quote") or "")[:1000]}
            if body.get("part") is True:   # on a selected piece: the core keeps the fragment next to the icon
                data["part"] = True
            return self.put_decided("message.reacted", None, data)
        if path == "/api/session/train":   # "Train workers" on agent-dev (core/system.py)
            if not sessions[name].get("trains"):
                return 409, self.t("web.srv.no_training", name=name)
            from core import system
            self.put("message.received", None, {"to": name, "text": str(system.TRAIN), "action": True,
                                                "l10n": coretext.marks(system.TRAIN),
                                                "button": self.t("web.dialogs.train_ok")})
            return 202, "ok"
        if path in ("/api/session/close", "/api/session/fire", "/api/session/remove") and sessions[name].get("system"):
            return 409, self.t("web.srv.system_agent", name=name)
        if path == "/api/session/close":
            if sessions[name].get("kind") == "hub":
                return 409, self.t("web.srv.hub_no_sleep")
            if sessions[name].get("role") == "agent":
                return 409, self.t("web.srv.agent_fire_first", name=name)
            # wait for the core's decision, like /compact: a refusal ("a turn is running", "background work in the session", "already asleep") is shown in words on the page
            return self.put_decided("session.close_requested", name, {})
        if path == "/api/session/fire":  # demote the agent to a worker and put it to sleep; sleep comes after the role change
            if sessions[name].get("role") != "agent":
                return 409, self.t("web.srv.not_agent")
            self.put("session.role_set", name, {"role": "worker"})
            if sessions[name]["state"] not in ("asleep", "failed", "closed"):
                self.put("session.close_requested", name, {})
            return 202, "ok"
        if path in ("/api/session/reload", "/api/session/compact"):
            # free a session's memory from the page: restart the process with the same conversation (MCP servers and
            # children too), or compact the conversation. Both happen when the session is free, the core itself waits
            # for the turn to end; a sleeping session is compacted by the core on wake-up, and there is nothing to
            # restart. The core decides (the page used to refuse where the core accepts); a refusal by state is
            # replaced by the page's phrase: for a parked session — that its process was released
            if path.endswith("reload"):
                said = {"parked": "web.srv.parked",
                        **{s: "web.srv.asleep" for s in ("asleep", "retired", "closed", "failed", "removed")}}
                return self.act_decided("session.reload_requested", name, {}, said)
            text = str(body.get("text") or "").strip()   # /compact <what to keep>
            return self.act_decided("session.compact_requested", name, {"text": text} if text else {},
                                    {s: "web.srv.compact_gone" for s in ("closed", "failed", "removed")})
        if path == "/api/session/resume":
            # "Continue" on a session paused by a limit (the clients' menu item; a reload restarted the process and left
            # the cut turn standing): the core continues the cut turn now, as at the window's reset (core/limitresume.py)
            return self.resume(name)
        if path == "/api/session/effort":  # /effort in the input field: the same as hub session effort
            from core.catalog import EFFORTS
            level = body.get("level", body.get("effort"))   # effort — like the snapshot field (API 1.22)
            if level not in (*EFFORTS, "default"):   # default — back to the level from settings
                return 400, self.t("web.srv.effort_one_of", levels=", ".join((*EFFORTS, "default")))
            return self.put_decided("session.effort_set", name, {"level": level})
        if path == "/api/session/spec":  # /spec on|off in the input field: specs for this session (core/specmode.py)
            # on: true from the page, "on" | "off" from a client that passes the typed argument (commands.list)
            return self.put_decided("session.spec_set", name, {"on": body.get("on") in (True, "on")})
        if path == "/api/session/solo":  # the "solo" checkbox in the session menu: works on its own, without workers (minihub)
            return self.put_decided("session.solo_set", name, {"on": body.get("on") is True})
        if path == "/api/session/parallel":  # an own number of workers in the line under the input field (core/minihub.limit)
            ev = minihub.own_event(body.get("n"))   # 0 is "solo" via the old event (rollback), 1…20 and default are parallel_set
            if not ev:
                return 400, self.t("web.srv.parallel_range", max=minihub.OWN_MAX)
            return self.put_decided(ev[0], name, ev[1])
        if path == "/api/session/model":  # model choice under the input field (the model can be changed for any session)
            return self.put_decided("session.model_set", name, {"model": str(body.get("model") or "")})
        if path == "/api/interrupt":
            return self.act_decided("session.interrupt_requested", name, {}, {"*": "web.srv.session_idle"})
        if path == "/api/session/stop":   # "Stop executor": its process tree is killed now, it sleeps with the conversation
            if not minihub.runner(sessions[name]):
                return 409, self.t("web.srv.stop_executors_only")
            return self.act_decided("session.stop_requested", name, {}, {"*": "web.srv.stop_not_running"})
        if path == "/api/session/remove":   # a sleeping, parked or failed one; the core shuts down and removes a free worker
            return self.act_decided("session.remove_requested", name, {}, {s: "web.srv.remove_asleep_only"
                                                                           for s in ("starting", "idle", "busy", "dialog", "held")})
        if path == "/api/tools/grant":   # Settings -> Plugins "Give to a session…": the owner's word, as hub tools grant
            tool = body.get("tool")
            if not isinstance(tool, str) or tool not in (snap.get("catalog") or {}):
                return 409, self.t("web.srv.tool_not_in_catalog", tool=str(tool or ""))
            code, out = self.put_decided("tools.granted", name, {"tool": tool})
            return (code, {"ok": True, "text": self.t("web.srv.tool_granted", tool=tool, name=name)}) if out == "ok" \
                else (code, out)
        if path == "/api/tools":
            tool, action = body.get("tool"), body.get("action")
            waiting = [r for r in sessions[name].get("requests") or [] if r["tool"] == tool and r["state"] == "needs-owner"]
            if action not in ("approve", "deny") or not waiting:
                return 409, self.t("web.srv.request_decided")
            self.put("tools.approved" if action == "approve" else "tools.denied", name,
                     {"tool": tool, "reason": (body.get("reason") or "").strip() or None})
            return 202, "ok"
        if path in ("/api/question/dismiss", "/api/answer", "/api/permission"):
            return self.answer(path, body, name, sessions[name])
        return 404, self.t("web.srv.no_action")

    def answer(self, path, body, name, sess):
        """A reply to a card: /api/answer (a question), /api/permission (a permission), /api/question/dismiss (close
        without an answer: Esc, ×). The receipt is the number of the accepted fact (202 {ok, seq}); whether it was
        applied, the client sees in the snapshot: the session's answered[] with the same seq. A repeat (the client did
        not get the receipt and retried from its queue, a second client, a page restart in between) is judged by the
        request id: the card is already closed with the same decision or the same client_id — 200 {ok, already, seq},
        no second fact; closed by a different one — 409 answered_other; absent altogether — 409.
        If the core did not record it within decide_wait — 202 {ok, queued}; the page asks for the outcome with the same
        request and client_id: the fact is still in the spool — queued again with no second fact, the core refused
        (core.rejected) — 409 {error, reason: refused}, the journal reader is busy — 503 (it is unknown whether the
        reply was recorded: do not put a second fact)."""
        cid = body.get("client_id")
        if cid is not None and not (isinstance(cid, str) and 0 < len(cid) <= modstate.DEDUP_KEY_MAX):
            return 400, self.t("web.srv.client_id_bad", n=modstate.DEDUP_KEY_MAX)
        refused = None
        if cid is not None:
            if self.in_spool(name, cid, card=True):
                return self.queued()
            # the core may have already taken the fact from the spool — read the journal up to it before deciding. If
            # the reader is busy, it is unknown whether this reply was recorded or rejected: do not put a second fact
            # with the same client_id, ask later
            lang = self.lang_now()
            refused = self.tail.peek(lambda r: r.refusal(name, cid, lang), self.READ_WAIT, poll=True)
            if refused is Tail.BUSY:
                return 503, {"error": self.t("web.srv.receipt_busy")}
            sess = next((s for s in self.tail.snapshot["sessions"] if s["name"] == name), sess)
        rid = body.get("request_id")
        req = next((r for r in sess.get("pending") or [] if r["request_id"] == rid
                    and (path != "/api/question/dismiss" or r.get("tool") == "AskUserQuestion")), None)
        if path == "/api/question/dismiss":
            kind, data = "question.dismissed", {"request_id": rid}
        elif path == "/api/answer":
            answers = body.get("answers") or {}
            if not isinstance(answers, dict):
                return 400, self.t("web.srv.answer_each")
            if req:
                questions = [q["question"] for q in req["input"].get("questions", [])]
                if set(answers) != set(questions) or not all(str(v).strip() for v in answers.values()):
                    return 400, self.t("web.srv.answer_each")
            kind, data = "permission.answered", {"request_id": rid, "behavior": "allow", "answers": answers}
        else:
            behavior = body.get("behavior")
            if behavior not in ("allow", "deny"):
                return 400, self.t("web.srv.allow_or_deny")
            kind, data = "permission.answered", {"request_id": rid, "behavior": behavior,
                                                 "message": (body.get("message") or "").strip() or None}
        sig = owner.answer_sig(data.get("behavior", "dismissed"), data.get("answers"))
        if not req:
            return self.answered_before(sess, rid, sig, cid) or (409, self.t("web.srv.question_gone"))
        if refused:   # the question is open but the core rejected this reply: no point repeating it
            return 409, {"error": refused["error"], "reason": "refused", "seq": refused["seq"]}
        if cid is not None:
            data["client_id"] = cid
        seq, why, box = self.decide(kind, name, data)
        if cid is not None:
            self.remember_box(name, cid, box)
        if seq is None:
            return self.queued()
        if not why:
            return 202, {"ok": True, "seq": seq}
        # the card was closed while the request ran (a second client, Telegram): the decision on that reply is already in
        # the journal (read the tail, no longer than READ_WAIT); if the reader is busy, it is unknown whether it was
        # "already accepted": answer "retry" (same client_id)
        if self.tail.peek(lambda r: None, self.READ_WAIT, poll=True) is Tail.BUSY:
            raise ReaderBusy()
        now = next((s for s in self.tail.snapshot["sessions"] if s["name"] == name), sess)
        return self.answered_before(now, rid, sig, cid) or (409, self.said(why))

    def answered_before(self, sess, rid, sig, cid):
        """Card rid is already closed (the core applied a reply): the same reply gives 200 "already accepted" with that
        fact's number, a different one gives 409 answered_other. If the core did not close it, or it was withdrawn (no reply
        of his was applied) — None."""
        rec = next((r for r in sess.get("answered") or [] if r.get("request_id") == rid), None)
        if not rec or rec.get("how") == "withdrawn":   # a withdrawn card took no reply of his: it is gone, not answered
            return None
        if rec.get("sig") == sig or (cid is not None and rec.get("client_id") == cid):
            return 200, {"ok": True, "already": True, "seq": rec["seq"]}
        return 409, {"error": self.t("web.srv.answered_other"), "reason": "answered_other", "seq": rec["seq"]}

    def schedule_act(self, path, body, snap, sessions):
        job = next((j for j in snap.get("schedule", []) if j["id"] == body.get("id")), None)
        # an expired job (its term ran out) can only be removed: "not needed" (core/schedule.py)
        if not job or job["state"] not in ("active", "paused", "expired") or \
                (job["state"] == "expired" and path != "/api/schedule/remove"):
            return 409, self.t("web.srv.job_gone")
        if path == "/api/schedule/remove":
            self.put("schedule.remove_requested", job["id"], {})
            return 202, "ok"
        if path == "/api/schedule/retarget":
            target = body.get("session")
            if target not in sessions or sessions[target]["state"] in ("failed", "closed"):
                return 400, self.t("web.srv.pick_session")
            self.put("schedule.retarget", job["id"], {"session": target})
            return 202, "ok"
        return 404, self.t("web.srv.no_action")

    VOICE_BODY_MAX = (MAX_FILE + 2) // 3 * 4 + (64 << 10)   # the base64 of the longest recording plus the mime and session fields

    VOICE_DRAIN_MAX = 256 << 20   # a longer body is not read off (the page's answer waits for the end of the body): the connection is closed

    def voice_too_long(self):
        """A recording above the limit (MAX_FILE): 413 that names the limit, in the language of this request."""
        from modules.web import modproxy as mp
        return 413, self.t("web.srv.recording_too_long", size=mp.size_text(MAX_FILE, self.lang_now()))

    def transcribe(self, body):
        import base64
        if not voice.enabled(self.config(), state=self.state_dir):
            return 404, self.t("web.srv.voice_off")
        try:
            raw = base64.b64decode(body.get("data") or "", validate=True)
        except ValueError:
            return 400, self.t("web.srv.recording_unreadable")
        return self.transcribe_bytes(raw, body.get("mime"), body.get("session"))

    def transcribe_bytes(self, raw, mime, session):
        """The recording as bytes (from base64 /api/voice or a raw upload /api/voice/raw) -> (code, answer)."""
        cfg = self.config()
        if not raw:
            return 400, self.t("web.srv.recording_empty")
        if len(raw) > MAX_FILE:
            return self.voice_too_long()
        # the format comes from the recording header (Safari: mp4, while the type may arrive empty or wrong); if not
        # recognized — from the shared voice type table; with no type or an unknown one — a browser recording (webm), as before
        suffix = voice.sniff(raw) or voice.suffix_of_mime(mime) or ".webm"
        started = time.time()
        try:
            text = voice.transcribe(raw, cfg, suffix=suffix, state=self.state_dir)
        except voice.VoiceError as e:
            print(f"voice: {len(raw)} B {suffix}, {time.time() - started:.1f} s — not transcribed: {e}", file=sys.stderr, flush=True)
            if isinstance(e, voice.Empty):
                # no speech — an outcome of the recording, not a failure: replaying the same sound will hear the same.
                # 422, not 502 — a client that counts 5xx as lost connectivity does not retry endlessly; there is no
                # need to keep it (or to let it replace the last failed one)
                return 422, self.t("web.srv.no_speech")
            # the recording lived only in the request: do not lose it — keep every failed one, not just the last (failures
            # come in a row). The reply carries what is needed to decide from a phone: not a path on the hub's disk but
            # the address from which the client can play it, where the owner was speaking and what that is about, and
            # what the engine managed to transcribe
            name = self.keep_failed_voice(raw, suffix)
            # reason — a short reason in the hub's words from the failure code (e.kind); the path and command output go only to the log above
            return 502, self.voice_kept(name, self.VOICE_WHY.get(e.kind, "web.srv.voice_why_other"), session, e.partial)
        print(f"voice: {len(raw)} B {suffix}, {time.time() - started:.1f} s — {len(text)} characters", file=sys.stderr, flush=True)
        return 200, {"text": text}

    def voice_kept(self, name, why, session, partial=""):
        """The 502 answer about a recording kept as a failed one (name — its id, why — the reason's key)."""
        try:
            context = self.voice_context(session)
        except Exception:   # noqa: BLE001 — only a hint: the address of the kept recording matters more
            context = None
        return {"error": self.t("web.srv.voice_kept"), "id": name, "reason": self.t(why),
                "file": "/api/voice/failed?id=" + quote(name, safe=""),
                **({"context": context} if context else {}), **({"partial": partial} if partial else {})}

    VOICE_SESSION_TTL = 3600   # an unfinished upload session is dropped after this many seconds without a chunk
    VOICE_SESSIONS_MAX = 16    # open upload sessions in all (each up to MAX_FILE): a new one drops the oldest
    VOICE_ANSWER_TTL = 300     # the answer of a finished session is kept this long: a retried last chunk gets it, not a 404

    def voice_raw(self, h):
        """POST /api/voice/raw?mime=&session=&id=&offset=&done=: the recording as a raw body (no base64) in ONE upload
        session. No id: a new session (the whole recording in one request, or the first chunk with done=0). id + offset:
        the next chunk of the same session; offset is the size the hub already has (a chunk with id repeated adds
        nothing; a repeated first chunk opens another session, which expires; a gap — 409 with the size to resume from).
        done=0 answers {id, size}; done=1 (the default) transcribes and answers as /api/voice. A last chunk repeated
        while it is transcribed, or after the answer got lost, gets that same answer. mime and session are kept from the
        chunk that has them (the first one; a later one may change them). The decisions are made under voice_lock, and
        nothing is read from or written to the socket under it: a refusal is answered (and its body read off) after the
        lock is released."""
        from modules.web import modproxy as mp
        length = mp.content_length(h, True)
        if length is None:
            return
        q = {k: v[0] for k, v in parse_qs(urlparse(h.path).query).items()}

        def early(code, text, fields=None):   # a refusal with the body unread: read off a small one, answer, close
            mp.drain(h, length)
            return mp.refuse(h, code, text, close=True, fields=fields)

        if length > MAX_FILE:
            return early(*self.voice_too_long())
        if not voice.enabled(self.config(), state=self.state_dir):
            return early(404, self.t("web.srv.voice_off"))
        sid, off, done = q.get("id") or "", q.get("offset"), q.get("done", "1") != "0"
        if sid and not re.fullmatch(r"[0-9a-f]{16}", sid):
            return early(404, self.t("web.srv.voice_session_none"))
        if (off is not None or sid) and not (off and off.isascii() and off.isdigit() and len(off) <= 12):   # "²" passes str.isdigit()
            # a chunk of a session without offset would go at the end: its repeat would double the sound
            return early(400, self.t("web.srv.voice_offset_bad"))
        if not sid and not length:
            return early(400, self.t("web.srv.recording_empty"))
        folder = os.path.join(self.state_dir, "voice-upload")
        no = None
        with self.voice_lock:   # the quick refusals; the size is measured again below, after the body
            self.voice_sweep(folder)
            have = self.voice_size(folder, sid) if sid else 0
            if sid and have is None and (sid not in self.voice_answers or not done):   # an ended one takes only its last chunk again
                no = (404, self.t("web.srv.voice_session_none"))
            elif have is not None and int(off or 0) > have:   # a chunk of a session always has an offset
                no = (409, self.t("web.srv.voice_session_gap"), {"size": have})
            elif have is not None and int(off or 0) + length > MAX_FILE:   # not after 20 MB more
                self.voice_drop(folder, sid)
                self.voice_meta.pop(sid, None)
                no = self.voice_too_long()
        if no:
            return early(*no)
        got = b""
        try:
            h.connection.settimeout(mp.IDLE)
            got = b"".join(mp.body(h, length))
        finally:
            try:
                h.connection.settimeout(getattr(h.server, "conn_timeout", None))
            except OSError:
                pass
        if len(got) < length:
            return   # the client left mid-body: nobody to answer
        raw, known, reply = None, None, None
        with self.voice_lock:
            try:
                self.voice_sweep(folder)
                known = self.voice_answers.get(sid) if done else None   # the last chunk again: while it is transcribed wait for that, after it the same answer
                if not known:
                    have = self.voice_size(folder, sid) if sid else 0
                    offset = int(off or 0)
                    if have is None:
                        no = (404, self.t("web.srv.voice_session_none"))
                    elif offset > have:
                        no = (409, self.t("web.srv.voice_session_gap"), {"size": have})
                    elif offset + len(got) > MAX_FILE:
                        self.voice_drop(folder, sid)
                        self.voice_meta.pop(sid, None)
                        no = self.voice_too_long()
                    else:
                        if not sid:
                            sid = uuid.uuid4().hex[:16]
                            if not done:   # a session that stays open; a whole recording leaves under this same lock
                                self.voice_sweep(folder, room=1)
                        part = os.path.join(folder, sid + ".part")
                        platforms.current.makedirs_private(folder)
                        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                        with os.fdopen(fd, "ab") as f:   # the owner's speech: for the owner only, like the failed recordings
                            f.write(got[have - offset:])   # a chunk repeated: what is already written is skipped
                        os.utime(part)   # an empty chunk keeps the session alive too
                        meta = self.voice_meta.setdefault(sid, {})
                        meta.update({k: q[k] for k in ("mime", "session") if q.get(k)})
                        if done:
                            with open(part, "rb") as f:   # read under the lock: a sweep cannot take it away from us
                                raw = f.read()
                            self.voice_drop(folder, sid)
                            known = self.voice_answers[sid] = {"event": threading.Event(), "answer": None,
                                                               "at": self.voice_clock()}
                        else:
                            reply = (200, {"id": sid, "size": os.path.getsize(part)})
            except OSError as e:   # disk full, no permission
                no = (500, self.t("web.srv.file_not_written", error=e.strerror or type(e).__name__))
        if no:   # the body is read: no need to close the connection
            return mp.refuse(h, no[0], no[1], fields=no[2] if len(no) > 2 else None)
        if reply:
            return h.send(reply[0], json.dumps(reply[1]))
        if raw is not None:
            meta = self.voice_meta.get(sid) or {}
            answer = (500, self.t("web.srv.voice_why_other"))   # whatever happens below, the request and its retries get an answer
            try:
                answer = self.transcribe_bytes(raw, meta.get("mime"), meta.get("session"))
            except Exception as e:   # not a VoiceError: do not lose the recording, keep it as a failed one
                print(f"voice: {len(raw)} B — transcription raised {type(e).__name__}", file=sys.stderr, flush=True)
                try:
                    name = self.keep_failed_voice(raw, voice.sniff(raw) or voice.suffix_of_mime(meta.get("mime")) or ".webm")
                    answer = (502, self.voice_kept(name, "web.srv.voice_why_other", meta.get("session")))
                except OSError as e:   # disk full, no permission: the recording is lost, say why
                    answer = (500, self.t("web.srv.file_not_written", error=e.strerror or type(e).__name__))
            finally:   # an answer left at None would hold every retry for 300 s and then a 504, until a restart
                with self.voice_lock:
                    known["answer"], known["at"] = answer, self.voice_clock()
                    self.voice_meta.pop(sid, None)
                known["event"].set()
        elif not known["event"].wait(300):
            return mp.refuse(h, 504, self.t("web.srv.voice_why_engine_timeout"))
        code, out = known["answer"]
        return h.send(code, json.dumps(out if isinstance(out, dict) else {"error": out}, ensure_ascii=False))

    def voice_size(self, folder, sid):
        """The bytes a session holds (None: no such session)."""
        try:
            return os.path.getsize(os.path.join(folder, sid + ".part"))
        except OSError:
            return None

    def voice_drop(self, folder, sid):
        try:
            os.unlink(os.path.join(folder, sid + ".part"))
        except OSError:
            pass

    def voice_sweep(self, folder, room=0):
        """Under voice_lock: sessions nobody finished (VOICE_SESSION_TTL) go; with `room` — and the oldest ones, so that
        room sessions more fit under VOICE_SESSIONS_MAX; answers kept longer than VOICE_ANSWER_TTL go."""
        now = self.voice_clock()
        for sid, v in list(self.voice_answers.items()):
            if v["answer"] is not None and now - v["at"] > self.VOICE_ANSWER_TTL:
                del self.voice_answers[sid]
        try:
            files = [(os.path.getmtime(os.path.join(folder, n)), n) for n in os.listdir(folder) if n.endswith(".part")]
        except OSError:
            return
        files.sort()
        for mtime, n in list(files):
            if now - mtime > self.VOICE_SESSION_TTL:
                self.voice_drop(folder, n[:-5])
                files.remove((mtime, n))
        for mtime, n in files[:max(0, len(files) + room - self.VOICE_SESSIONS_MAX)]:
            self.voice_drop(folder, n[:-5])
            files.remove((mtime, n))
        alive = {n[:-5] for _, n in files}
        for sid in [k for k in self.voice_meta if k not in alive and k not in self.voice_answers]:
            del self.voice_meta[sid]

    VOICE_WHY = {"format": "web.srv.voice_why_format", "ffmpeg_start": "web.srv.voice_why_ffmpeg_start",
                 "ffmpeg_timeout": "web.srv.voice_why_ffmpeg_timeout", "ffmpeg_failed": "web.srv.voice_why_ffmpeg_failed",
                 "engine_start": "web.srv.voice_why_engine_start", "engine_timeout": "web.srv.voice_why_engine_timeout",
                 "engine_failed": "web.srv.voice_why_engine_failed", "engine_output": "web.srv.voice_why_engine_output",
                 "model_missing": "web.srv.voice_why_model_missing", "engine_busy": "web.srv.voice_why_engine_busy",
                 "cloud_login": "web.srv.voice_why_cloud_login", "cloud_limit": "web.srv.voice_why_cloud_limit",
                 "cloud_failed": "web.srv.voice_why_cloud_failed"}
    # transcription failure codes (voice.VoiceError.kind) -> the hub's words; any other code — web.srv.voice_why_other
    VOICE_FAILED_DAYS = 7                       # failed voice recordings are kept for a week…
    VOICE_FAILED_BYTES = 200 * 1024 * 1024      # …and no more than 200 MB in total: beyond that the oldest go
    VOICE_FAILED_NAME = re.compile(r"(\d{8}-\d{6}(-[0-9a-f]{6})?|[0-9a-f]{16})(" + "|".join(
        re.escape(x) for x in sorted(set(voice.MIME_SUFFIX.values()))) + ")")
    VOICE_TYPES = {".webm": "audio/webm", ".ogg": "audio/ogg", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
                   ".mp4": "audio/mp4", ".wav": "audio/wav", ".flac": "audio/flac"}

    def keep_failed_voice(self, raw, suffix):
        """A failed voice recording goes to state/voice-failed, owner-only (0600). The name (id) is the recording's
        fingerprint: a repeat of the same one ("Retry transcription", the client retries a 502) gets the same copy and the
        same id, not a new copy per retry (those would push other recordings out of the size ceiling); it is written to a
        temporary file and swapped in whole, so a parallel retry never sees half of it. Along the way, remove recordings
        older than VOICE_FAILED_DAYS and, if the total exceeds VOICE_FAILED_BYTES, the oldest ones; never this one.
        -> the recording's name (id)."""
        import hashlib
        kept = os.path.join(self.state_dir, "voice-failed")
        os.makedirs(kept, exist_ok=True)
        name = hashlib.sha256(raw).hexdigest()[:16] + suffix
        path = os.path.join(kept, name)
        try:
            os.utime(path)   # already exists — refresh it: age-based cleanup counts from the last retry
        except OSError:
            part = os.path.join(kept, f".{uuid.uuid4().hex}.part")
            try:
                with open(os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
                    f.write(raw)
                os.replace(part, path)
            except OSError:   # Windows: a parallel request has already stored and is serving the same recording — that is it
                with contextlib.suppress(OSError):
                    os.remove(part)   # and do not leave a partly written one behind (no space)
                if not os.path.isfile(path):
                    raise
        files = []
        for n in os.listdir(kept):
            try:
                st = os.stat(os.path.join(kept, n))
            except OSError:
                continue
            files.append((st.st_mtime, n, st.st_size))
        files.sort()
        # a partly written copy (.part — ours or a parallel request's) does not count toward the size and goes only by age
        total, now = sum(f[2] for f in files if not f[1].startswith(".")), time.time()
        for mtime, n, size in files:
            if n == name or (n.startswith(".") and now - mtime <= self.VOICE_FAILED_DAYS * 86400):
                continue
            if now - mtime > self.VOICE_FAILED_DAYS * 86400 or total > self.VOICE_FAILED_BYTES:
                try:
                    os.remove(os.path.join(kept, n))
                    total -= 0 if n.startswith(".") else size
                except OSError:
                    pass
        return name

    def voice_context(self, name):
        """Where a failed recording was headed and what the conversation there is about, in one line: the addressee (the
        field being dictated into) and its gist (hub gist, otherwise the start of the latest result for the owner).
        If no addressee is named, it is not a hub session, or the journal reader is busy — None."""
        if not isinstance(name, str) or not name:
            return None

        def look(r):
            s = (r.state.get("sessions") or {}).get(name)
            if not s or s.get("state") == "removed":
                return None
            return ((tools.gist_now(s) or {}).get("text") or s.get("out_head") or "").strip()

        topic = self.tail.peek(look, self.READ_WAIT)
        if topic is None or topic is Tail.BUSY:
            return None
        if not topic:
            return self.t("web.srv.voice_context_to", session=name)
        return self.t("web.srv.voice_context", session=name, topic=topic.splitlines()[0][:160])

    def failed_voice(self, name):
        """The real path of a failed voice recording by its id (a file name in state/voice-failed) or None: only names
        that keep_failed_voice produces, only this folder."""
        if not self.VOICE_FAILED_NAME.fullmatch(name or ""):
            return None
        kept = os.path.realpath(os.path.join(self.state_dir, "voice-failed"))
        real = os.path.realpath(os.path.join(kept, name))
        return real if real.startswith(kept + os.sep) and os.path.isfile(real) else None

    def directions_root(self):
        from modules.directions import direction
        return direction.root(self.config())

    def backlog(self, slug):
        """A direction's whole board — for the built-in view on the page, with no server of its own (a minimal built-in
        variant): columns are the statuses from backlog/config.yml, tasks come with descriptions and criteria. If there
        is no such direction — None."""
        import glob
        from modules.directions import direction, minibacklog
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug or ""):
            return None
        base = os.path.join(self.directions_root(), slug, "backlog")
        if not os.path.isdir(base):
            return None
        statuses = minibacklog.statuses_of(base)
        tasks = []
        for f in sorted(glob.glob(os.path.join(base, "tasks", "*.md"))):
            try:
                text = direction.read(f)
            except OSError:
                continue
            head, _, body = text.partition("\n---\n") if text.startswith("---\n") else ("", "", text)
            labels = direction.field(head, "labels") or ""
            body = re.sub(r"<!--.*?-->\n?", "", body, flags=re.S).strip()
            tasks.append({"id": direction.field(head, "id") or os.path.basename(f), "title": direction.field(head, "title")
                          or os.path.basename(f), "status": minibacklog.canon(direction.field(head, "status"), statuses) or "?",
                          "priority": direction.field(head, "priority"), "updated": direction.field(head, "updated_date"),
                          "labels": [x.strip().strip("'\"") for x in labels.strip("[]").split(",") if x.strip()],
                          "ordinal": float(direction.field(head, "ordinal") or 1e9),
                          "ac": [{"done": m.group(1) == "x", "text": m.group(2)}
                                 for m in re.finditer(r"^- \[([ x])\] (?:#\d+ )?(.+)$", body, re.M)],
                          "body": body[:20000]})
        tasks.sort(key=lambda t: (t["ordinal"], t["id"]))
        out = {"statuses": statuses, "tasks": tasks}
        facts = self.board_facts([slug]).get(slug)
        if facts is not None:   # who works on a card, what is handed in and not yet reviewed (core/board.py); if the reader is busy — without
            out["facts"] = facts
        return out

    def board_facts(self, slugs, wait=None):
        """{slug: the core's facts about the board} (core/board.py) — from the core state under the journal reader's lock
        (the calculation takes milliseconds); if the lock is busy for longer than READ_WAIT — {}: the board and counters
        go without facts until next time."""
        from core import board
        from modules.directions import direction
        paths = {slug: os.path.join(self.directions_root(), slug) for slug in slugs}
        cards = {slug: direction.cards(p) for slug, p in paths.items()}

        def calc(reader):
            common = board.shared(reader.state)
            return {slug: board.facts(reader.state, slug, cards[slug], time.time(), common) for slug in slugs}
        got = self.tail.peek(calc, self.READ_WAIT if wait is None else wait)
        return {} if got is Tail.BUSY else got

    def direction_tasks(self, slugs):
        """A summary of each direction's board: {slug: {open, doing: [titles in progress]}} (not Done) — the counter in
        the list and the "Board" row in a direction's session. Board files are read once per 30 s."""
        import glob
        import time
        from modules.directions import direction, minibacklog
        cache = getattr(self, "_dir_tasks", None)
        if cache and time.time() - cache[0] < 30 and cache[1] == slugs:
            return cache[2]
        out = {}
        for slug in slugs:
            info = {"open": 0, "doing": [], "tasks": []}
            known = direction.statuses(os.path.join(self.directions_root(), slug))
            for f in sorted(glob.glob(os.path.join(self.directions_root(), slug, "backlog", "tasks", "*.md"))):
                try:
                    text = direction.read(f)
                except OSError:
                    continue
                status = minibacklog.canon(direction.field(text, "status"), known) or "?"
                title = (direction.field(text, "title") or os.path.basename(f))[:120]
                # board tasks with criteria go into the snapshot for plugins (direction_tasks, Plugin API)
                info["tasks"].append({"id": direction.field(text, "id") or os.path.basename(f), "title": title,
                                      "status": status, "updated": direction.field(text, "updated_date"),
                                      "ac": [{"done": m.group(1) == "x", "text": m.group(2)[:200]} for m in
                                             re.finditer(r"^- \[([ x])\] (?:#\d+ )?(.+)$", text, re.M)][:15]})
                if status == "Done":
                    continue
                info["open"] += 1
                if status == "In Progress" and len(info["doing"]) < 3:
                    info["doing"].append(title)
            # done — the last 5, open — all
            done = sorted((t for t in info["tasks"] if t["status"] == "Done"), key=lambda t: t["updated"] or "")[-5:]
            info["tasks"] = [t for t in info["tasks"] if t["status"] != "Done"] + done
            out[slug] = info
        # the core's fact counters: how many cards workers are on, how many are handed in and not yet reviewed (core/board.py)
        for slug, facts in self.board_facts(list(slugs), wait=0.2).items():   # the snapshot does not wait for a busy reader
            out[slug].update(facts["counts"])
        self._dir_tasks = (time.time(), list(slugs), out)
        return out

    def own_app_icon(self, maskable=False):
        """The bytes of the hub's own app icon next to hub.json, None without one: app-icon.png for the "any" and the
        apple-touch icons. maskable: app-icon-maskable.png, taken only next to an own app-icon.png (alone it would give
        the app the standard icon on one screen and an own one on another); without it the own picture — an app
        installed before the manifest dropped its maskable entry still asks for that address."""
        own = self._own_png("app-icon.png")
        return (self._own_png("app-icon-maskable.png") or own) if maskable and own else own

    def _own_png(self, name):
        """A file next to hub.json if it is fit to be an app icon, else None. Only a regular file (not a link: the page
        would serve whatever it points to) that is a whole PNG (IEND near its end: a file still being copied is not one)
        and square, of at least APP_ICON_SIDE (the manifest declares 192 and 512; Chrome does not install a page whose
        icon is smaller or not square) and at most APP_ICON_MAX — anything else leaves the standard icons."""
        path = os.path.join(os.path.dirname(catalog.config_path(self.state_dir)), name)
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        except OSError:
            return None
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if os.path.islink(path) or not stat.S_ISREG(st.st_mode) or st.st_size > APP_ICON_MAX:
                return None
            data = f.read(APP_ICON_MAX + 1)
        # IEND is the last chunk, but some tools leave bytes after it (a newline, padding) and browsers show such a file
        if len(data) > APP_ICON_MAX or data[:8] != PNG_MAGIC or data[12:16] != b"IHDR" or PNG_END not in data[-1024:]:
            return None
        width, height = struct.unpack(">II", data[16:24])
        return data if width == height >= APP_ICON_SIDE else None

    def manifest(self, lang):
        """The "app" manifest: the name is the hub's from settings, a neutral one without it; icons are files in static/icons/.
        A server started before an update has nothing to serve — a page without a manifest stays an ordinary bookmark."""
        mine = self.instance_name()
        name = (self.config().get("title") or "").strip() or mine or i18n.tr(lang, "web.hub.default_title")
        icon = lambda f, size, purpose: {"src": "/static/icons/" + f, "sizes": f"{size}x{size}", "type": "image/png", "purpose": purpose}
        # another hub of this machine is another app by its origin (its own port) already, so no "id" of its own: one
        # would orphan an app already installed from it (its id is "/" by start_url) — only its name tells it apart
        icons = [icon("icon-192.png", 192, "any"), icon("icon-512.png", 512, "any")]
        # a maskable icon is cut to a shape (Android): the own picture promises no margin for it and the standard one is
        # someone else's — with an own icon the maskable one comes only from its own file, otherwise there is none
        if self._own_png("app-icon.png") is None or self._own_png("app-icon-maskable.png"):
            icons.append(icon("icon-maskable-512.png", 512, "maskable"))
        return json.dumps({"name": name, "short_name": name[:12].strip() or name, "start_url": "/", "scope": "/",
                           "display": "standalone", "theme_color": THEME_LIGHT, "background_color": THEME_LIGHT,
                           "icons": icons},
                          ensure_ascii=False, separators=(",", ":"))

    def stream_view(self, snap, records, lang, seen=None, alive=lambda: True, since=None, rev=0):
        """(freshness key, {snapshot key: its JSON} or None if the key is seen) for event streams — one build for all:
        the same snapshot, the same module records, the same language and no older than STREAM_GAP (a stream sends no
        more often anyway). With ~16 streams connected to the page and a 16 MB snapshot, each one built it itself on
        every event under the GIL (the first event of a new stream took 5.5 s). The JSON of the parts is built only when
        the key is new, as before: a ping with no news does not build it. The shared build is awaited in steps: if the
        client left (alive) — httpd.Gone; if the builder did not finish within READ_WAIT — build your own. The page
        fetches the feeds (timelines) itself (/api/timeline). since — when the stream connected (time.monotonic): a
        build older than that may not have seen settings and files that changed without a journal event, so the key of
        such a reply is marked, and the stream's next query (after STREAM_GAP, without waiting for an event) takes a fresh build.
        rev — how many times the reader took the core's state in place of its own (Tail.rev): that changes the snapshot
        with no new event, and a quiet hub's stream would not see it without this in the key."""
        # the builder was already not awaited — until it returns, do not wait for it on every event; if the lock is free
        # the mark is stale (set after its return), and the next shared build clears it
        if self.view_stalled and self.view_lock.locked():
            return self._stream_view(snap, records, lang, seen, None, since, rev)
        end = time.monotonic() + READ_WAIT
        while not self.view_lock.acquire(timeout=min(READ_STEP, max(0.0, end - time.monotonic()))):
            if not alive():
                raise httpd.Gone()
            if time.monotonic() >= end:   # the builder hung — stop waiting for it and build our own
                self.view_stalled = True
                return self._stream_view(snap, records, lang, seen, None, since, rev)
        try:
            return self._stream_view(snap, records, lang, seen, self, since, rev)
        finally:
            self.view_stalled = False
            self.view_lock.release()

    def _stream_view(self, snap, records, lang, seen, shared, since=None, rev=0):
        hit = shared and shared._view
        fresh = hit and hit[0] is snap and hit[1] is records and hit[2] == lang and time.monotonic() - hit[3] < STREAM_GAP
        if fresh:
            key, full, parts, started = hit[4], hit[5], hit[6], hit[7]
        else:
            started = time.monotonic()   # a build sees settings and files no earlier than its own start
            full = self.with_clock(snap, records, lang)
            # a stopped clock or one waiting for hubd is news too; as are module keys and features: a module's
            # readiness changes without an event (a pause ended, hubd removed the module)
            key, parts = (full["seq"], full["clock"]["state"], tuple(full["features"]), tuple(full), rev), None
        # a build older than the stream's connection: the key is marked (None in front — the stream does not wait for
        # an event with the same seq); equal timestamps (the Windows clock ticks in ~15.6 ms steps) count as older too
        mine = (None, *key) if fresh and since is not None and started <= since else key
        if mine == seen:
            out = None
        else:
            parts = parts if parts is not None else self.stream_parts(full)
            out = parts
        if shared is not None:
            shared._view = (snap, records, lang, hit[3] if fresh else time.monotonic(), key, full, parts, started)
        return mine, out

    @staticmethod
    def stream_parts(full):
        return {k: json.dumps(v, ensure_ascii=False) for k, v in full.items() if k != "timelines"}

    def with_clock(self, snap, records=None, lang=None):
        """The core snapshot plus what the page knows. records — module records from the same journal poll
        (Tail.records); texts (the default hub name, settings labels) are in the language lang (the request's), without it — in this thread's response language."""
        from modules.directions.boards import Boards
        cfg = self.config()
        lang = lang or self.lang_now()
        try:
            directions = Boards(self.directions_root(), state_dir=self.state_dir).slugs()  # their sessions get the "Board" row
        except OSError:
            directions = []
        # core modules (core/modstate.py): mods and mod_versions of the loaded ones, their plugins; no modules — nothing
        mods = modstate.view(snap.get("mods"), self.state_dir, self.tail.records if records is None else records)
        plugins = mods.pop("plugins", [])
        claims = mods.pop("claims", {})   # keys and features of ready modules (core/modclaims.py): the core takes precedence
        if "mods" in snap:
            snap = {k: v for k, v in snap.items() if k != "mods"}
        if snap.get("core_written"):   # whether the module is on now — per hub.json (core/corewrite.py)
            from core import corewrite
            snap = {**snap, "core_written": {n: {**r, "on": corewrite.on_now(cfg, n)}
                                             for n, r in snap["core_written"].items()}}
        voice_on = voice.enabled(cfg, state=self.state_dir)   # it can read the built-in model's record and the cloud choice
        out = {**snap, "clock": read_status(self.state_dir), "voice": voice_on, "jira": jira.settings(cfg),
                "files_port": files_port(self.state_dir),   # a second hub has its own "Files" port
                # the hub's name is in settings; another hub of this machine without one goes by its instance name
                "hub_title": cfg.get("title") or self.instance_name() or i18n.tr(lang, "web.hub.default_title"),
                "hub_name": cfg.get("title") or self.instance_name() or "",   # the hub row's label: "Hub" + name
                # the Reactions module: when off there are no icons; up to which event it is answered (taken) is always
                # present: collapsing the answered is a page checkbox, not the module
                "reactions": (snap.get("reactions") or {}) if reactions_on(cfg)
                else {"taken": (snap.get("reactions") or {}).get("taken") or {}},
                "directions": directions, "direction_tasks": self.direction_tasks(directions),
                "version": self.version(),
                # optional for clients outside the page: notifications are sent only when a key is set (modules/web/push.py)
                "features": (["push", "push.alerts"] if self.push.settings() else []) + (["reactions"] if reactions_on(cfg) else [])
                + (["modules"] if mods else []) + ["upload.raw", "accounts.use", "send.tasks", "feed.post_by_id", "bus", "about.addresses", "about.lite", "send.app", "setup", "feed.on_behalf", "session.resume", "stream.delta2", "stream.feed", "page.password", "file.thumb"]
                + (["voice.raw"] if voice_on else [])
                # the app (app spec §4a): the steps run by themselves, the band is the only view of them
                + (["setup.app"] if getattr(self.setup(), "app", False) else []),
                # raw-stream attachment (the body is the file itself) and its limit; max_inline is the /api/upload (base64) limit
                "upload": {"raw": "/api/upload/raw", "max_bytes": self.upload_max(cfg), "max_inline": MAX_FILE},
                # the threshold after which an idle conversation is compacted (core/parking): the page shows progress toward it
                "context_limit": (cfg.get("compact_idle") or {}).get("min_tokens") or parking.COMPACT_MIN_TOKENS,
                "extensions": [{"name": e["name"], "main": e["main"], **({"css": e["css"]} if e["css"] else {})}
                               for e in self.extensions() if e["main"]] + plugins,
                "plugins": self.plugins(lang), "plugin_api": PLUGIN_API, **self.accounts(), **self.codex_view(cfg),
                "settings": self.settings_view(cfg, lang),
                # commands via "/" (js/commands.js): what the core accepts in /model and /effort; list — the core
                # commands themselves, for clients outside the page (the "/" menu and the session menu)
                "commands": {"list": commands_list(lang), **self.model_lists(cfg)},
                "code": lean_code(self.code()), "bus": self.bus_list(), **mods}
        if self.push.settings():   # alerts with push keys and their number (modules/web/push.py): the client reconciles banners
            out.update(self.push.view())
        out["features"] += [f for f in claims.get("features") or [] if f not in out["features"]]
        out.update({k: v for k, v in (claims.get("keys") or {}).items() if k not in out})
        return out

    def bus_list(self):
        """The session list's «Hubs» group (snapshot key bus): the contacts of the hub bus, None without them."""
        from modules.web.busweb import snapshot_key
        if not self.tail.ready:   # a reread from the start: the reader has no contacts yet — the last list, not «none»
            return getattr(self, "_bus_list", None)
        for _ in range(3):   # read without the reader's lock (as admit.mode above): a fold in between changes the dicts
            try:
                self._bus_list = snapshot_key(self.tail.reader.state)
                break
            except RuntimeError:
                continue
        return getattr(self, "_bus_list", None)

    def settings_view(self, cfg, lang):
        from core import modelrule, packaged
        from modules.web import settings
        from modules.watch import watch
        from core import admit
        # the mailbox check mode now (core/admit.py) — from the core state the page reads, not a snapshot key
        state = self.tail.reader.state
        # the hub and its agents each get a pair row for their executors (executors.agents.<name>)
        for _ in range(3):   # read without the reader's lock (as bus_list above): a fold in between changes the dicts
            try:
                self._agents = sorted(n for n, x in (state.get("sessions") or {}).items()
                                      if (x.get("role") == "agent" or x.get("kind") == "hub") and x.get("state") != "removed")
                break
            except RuntimeError:
                continue
        agents = getattr(self, "_agents", [])   # busy three times in a row: the last list, the rows do not vanish
        listed = self.codex_models() if modelrule.codex_only(cfg) else None   # experiment, slice E
        rows = settings.view(cfg, watch.config(), lang, admit.mode(state), agents, [m["id"] for m in listed or []])
        data = packaged.info()
        if data is not None:   # a packaged hub: updates come as a new package, the app's — by its own button; not from here
            for r in rows:
                if r["key"] == "update.enabled":
                    r.update(value=packaged.is_app(data), locked=packaged.how(data))
        if (data or {}).get("layout") != "layer":   # only a hub on a code layer has a background install to hold back
            rows = [r for r in rows if r["key"] != "update.manual"]
        return rows

    def code(self):
        """The version of this hub's code: the commit it updates from; for a shared hub — what is not yet published (the
        "Updates" page)."""
        if time.time() - getattr(self, "_code_at", 0) < 30:
            return self._code
        import subprocess
        from core import appcode, appwatch
        repo = os.path.dirname(os.path.dirname(HERE))
        layered = bool(appwatch.layer(repo))   # a code layer has no git: git would read a repository it sits in
        git = lambda *a: "" if layered else subprocess.run(  # noqa: E731
            ["git", *a], cwd=repo, capture_output=True, text=True, timeout=10).stdout.strip()
        head = git("rev-parse", "--short", "HEAD")
        if not hasattr(self, "boot_head"):
            self.boot_head = head  # the code the page started with (restarted together with the hub)
        from core import version
        # the release (tag vX.Y.Z) that is already in the code, and the update channel (a hub from git: main = nightly)
        out = {"head": head, "running": self.boot_head, "date": git("log", "-1", "--format=%cI"),
               "upstream": "origin" in git("remote").split(), "changes": [], "version": None if layered else version.release(repo, self.state_dir),
               "update_channel": version.channel(self.config())}
        if layered:   # the app's code layer: its manifest names the release and the commit
            man = appwatch.manifest(repo)
            out.update(version=man.get("version"), head=str(man.get("commit") or "")[:9], date=man.get("built"))
            own = appcode.own_of(man)
            if own:   # a build of the person's edits (`hub app apply`): "<release> + your edits (N)"
                out["own"] = {"release": own["release"], "edits": own["edits"]}
            if not getattr(self, "boot_layer", None):
                self.boot_layer = (out["head"], os.path.basename(appwatch.layer(repo) or ""))
            # the layer the page came up on: the update's watchdog (modules/update/trial.py) waits for it by name
            out["running"], out["layer"] = self.boot_layer
        try:  # what is new — CHANGES.md entries (collapsed in settings, the latest two in "Updates")
            from core import news
            with open(os.path.join(repo, "CHANGES.md"), encoding="utf-8") as f:
                out["changes"] = news.changes(f.read())   # without the items under the sessions-only heading
        except OSError:
            pass
        try:
            with open(os.path.join(self.state_dir, "published.json")) as f:
                pub = json.load(f)
            out["published_at"] = pub.get("at")
            out["unpublished"] = git("log", "--format=%h %s", f"{pub['sha']}..HEAD").splitlines()[:40]
            # the list is cut to 40 lines, the count is the full one
            out["unpublished_count"] = int(git("rev-list", "--count", f"{pub['sha']}..HEAD") or len(out["unpublished"]))
        except (OSError, ValueError, KeyError):
            pass
        try:   # a shared hub: the latest stable release (tools/release.py)
            with open(os.path.join(self.state_dir, "published-stable.json")) as f:
                rel = json.load(f)
            out["release"] = {"tag": str(rel["tag"]), "at": rel.get("at")}
        except (OSError, ValueError, KeyError, TypeError):
            pass
        self._code, self._code_at = out, time.time()
        return out

    def version(self):
        """The page code version: the content of the static/ files and the extensions' code (js, css, html, manifest). When
        it changes, open tabs reload themselves. An extension's data (its process writes json next to it) and .git are
        not code: tabs once reloaded in a loop because of a plugin's writes."""
        if time.time() - getattr(self, "_ver_at", 0) < 5:
            return self._ver
        import hashlib
        h = hashlib.sha1()
        # the content, not the time: a reinstall of the same code (a market update, a checkout) must not reload tabs.
        # A file is read again only when its size, times or inode changed
        sums, seen = getattr(self, "_sums", {}), {}
        # and the page strings (i18n/*.json, /i18n.json): if they changed on a server restart, tabs pick up the new ones
        for top in [STATIC, i18n.ROOT] + [e["dir"] for e in self.extensions()]:
            for base, dirs, files in os.walk(top):   # no sorted() here: otherwise the walk goes through everything before the folders are pruned
                dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x != "__pycache__")
                for f in sorted(files):
                    if top == i18n.ROOT:
                        if not i18n.CATALOG.fullmatch(f):
                            continue
                    elif top != STATIC and not (f.endswith((".js", ".mjs", ".css", ".html")) or f == "hub-plugin.json"):
                        continue
                    path = os.path.join(base, f)
                    try:
                        st = os.stat(path)
                        key = (st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino)
                        got = sums.get(path)
                        if not got or got[0] != key:
                            with open(path, "rb") as fh:
                                got = (key, hashlib.sha1(fh.read()).hexdigest())
                        seen[path] = got
                    except OSError:
                        continue
                    h.update(f"{base}/{f}:{got[1]}".encode())
        self._sums = seen
        self._ver, self._ver_at = h.hexdigest()[:12], time.time()
        return self._ver

    def model_lists(self, cfg):
        """What the core accepts in /model and /effort (commands.models, commands.efforts). Codex (experimental): a hub entirely on Codex lists its Codex models
        as codex/<id> (core/codexcli.models: model/list, no model call), with their names (titles) and the efforts each one knows (model_efforts; "default" — those of the
        hub's model); until the list is read — the hub's model from the settings; codex — the page knows the hub is on
        Codex before any model is listed. Any other hub — as on main."""
        from core import modelrule
        if not modelrule.codex_only(cfg):
            return {"models": [*catalog.MODELS, "default"], "efforts": list(catalog.EFFORTS)}
        from core import codexcli
        listed = self.codex_models() or []
        own = (cfg.get("codex") or {}).get("model") if isinstance(cfg.get("codex"), dict) else None
        own = own if isinstance(own, str) and codexcli.MODEL_ID.match(own) else None
        ids = [m["id"] for m in listed] + ([own] if own and own not in {m["id"] for m in listed} else [])
        efforts = {}
        for m in listed:
            known = [e for e in m["efforts"] if e in catalog.EFFORTS]
            if known:   # none the core knows: every level, as for an unknown model
                efforts[f"codex/{m['id']}"] = known
        # sessions on "default" run codex.model: its levels; without one — Codex's default model (isDefault)
        hub = f"codex/{own}" if own else next((f"codex/{m['id']}" for m in listed if m["default"]), None)
        if hub in efforts:
            efforts["default"] = efforts[hub]
        return {"codex": True, "models": [f"codex/{i}" for i in ids] + ["default"], "efforts": list(catalog.EFFORTS),
                "titles": {f"codex/{m['id']}": m["title"] for m in listed}, "model_efforts": efforts}

    def codex_models(self):
        """The models of the hub's Codex (core/codexcli.models) or None: read anew in the background when missing or
        old. The parsed file is kept by its mtime — a snapshot reads it once, not per section."""
        from core import codexcli
        try:
            st = os.stat(codexcli.models_path(self.state_dir))
            mtime = (st.st_mtime_ns, st.st_size, st.st_ino)   # coarse clocks: two writes in one tick
        except OSError:
            mtime = None
        held = getattr(self, "codex_models_held", None)
        listed, ts = held[1:] if held and held[0] == mtime else codexcli.models(self.state_dir)
        self.codex_models_held = (mtime, listed, ts)
        if listed is None or time.time() - ts > codexcli.MODELS_MAX_AGE:
            self.read_codex_models()
        return listed

    CODEX_MODELS_RETRY = 300   # s; a failed model/list (no codex, a broken install) waits this long, then twice as long…
    _codex_models = threading.Lock()   # one reading at a time for the process (several Web objects in tests)

    def read_codex_models(self):
        """model/list of the hub's Codex in a thread of its own: a snapshot never waits for codex; one at a time. A
        failure (or an empty answer) waits CODEX_MODELS_RETRY, doubling up to MODELS_MAX_AGE — not a codex app-server
        every 5 min for good."""
        with self._codex_models:
            if time.time() < getattr(self, "codex_models_next", 0):
                return
            self.codex_models_next = float("inf")   # this reading is under way: no second one
        threading.Thread(target=self.read_codex_models_now, daemon=True).start()

    def read_codex_models_now(self):
        """One model/list reading; a failure or an empty answer sets when the next one may start."""
        from core import codexcli
        got = None
        try:
            got = codexcli.fetch_models(self.state_dir)
        except Exception as e:   # noqa: BLE001 — a thread of its own: whatever codex answers, the page lives on
            print(f"web: Codex models were not read: {e!r}", file=sys.stderr, flush=True)
        with self._codex_models:
            fails = 0 if got else getattr(self, "codex_models_fails", 0) + 1
            self.codex_models_fails = fails
            # read: the file's age decides the next reading; failed: 5 min, 10, 20… up to MODELS_MAX_AGE
            self.codex_models_next = time.time() + (min(self.CODEX_MODELS_RETRY * 2 ** (fails - 1), codexcli.MODELS_MAX_AGE)
                                                    if fails else 0)

    def codex_sign_in(self, name, home, hub=False, again=False):
        """A sign-in by a code into a row of the Codex account pool: its folder ready (an account folder: the links to
        the hub folder, hosts/codex.prepare_account), then the background sign-in; None — the folder is not fit."""
        from core import codexcli, codexslot
        if not hub:
            from hosts.codex import prepare_account
            if codexslot.fit(self.state_dir, home) or prepare_account(self.state_dir, home):
                return None
        return codexcli.start_login(self.state_dir, account=name, home=None if hub else home, hub=hub, again=again)

    def codex_view(self, cfg):
        """Codex (experimental): a hub with the Codex host — "Settings -> Codex": the sign-in (email, plan, lost or
        not), a sign-in from the page if one is going on (the link, the code, the account), and the rows of the Codex
        account pool (core/codexslot.py): name, sign-in, whether its folder has one, the limits, busy, lost, active;
        cut: true — the tree has no pool's code (the open flavor): no "Add". A hub without the Codex host — nothing."""
        if not catalog.codex_host(cfg):
            return {}
        from core import codexcli, codexslot
        from modules.critic import critic
        known = self.tail.reader.state.get("codex") or {}
        page = codexcli.page_status(self.state_dir) or {}
        acc = known.get("account") or {}
        rows = codexcli.pool_view(self.tail.reader.state)
        return {"codex": {"login": known.get("login"), "lost": known.get("lost"), "since": known.get("since"),
                          "critic": critic.enabled(cfg),   # the row under the sign-in: the critic's switch (the Modules card's)
                          "page": {"url": (page.get("info") or {}).get("url"), "code": (page.get("info") or {}).get("code"),
                                   "account": (page.get("info") or page.get("result") or {}).get("account"),
                                   "result": page.get("result")} if page else None,
                          "pool": {"on": bool((known.get("pool") or {}).get("on")), "rows": rows,
                                   "exhausted": (acc.get("exhausted") or {}).get("until"),
                                   **({} if codexslot.present() else {"cut": True})}}}

    def accounts(self):
        """The pool of Claude accounts without secrets — for Settings -> Accounts and the limits row (the state is in the
        core snapshot); the pool is session sign-in (core/profiles.py). If the sign-in module did not answer — null: no tab."""
        from core import profiles
        got = self.profiles.listing()
        if got is None:
            return {"accounts": None}
        cfg = self.config()
        level, text = self.profiles.gateway_summary(self.tail.reader.state.get("gateway"), time.time(), cfg)
        return {"accounts": {"pool": [{"name": p["name"], **({"email": p["email"]} if p.get("email") else {}),
                                       # "Refresh" (/api/accounts/probe): an account with its own token (the built-in
                                       # pool or the login module's — then the module measures)
                                       **({"probe": True} if not (p.get("login") or p.get("renewable")) else {})}
                                      for p in got["profiles"]], "switch_at": got["switch_at"], "on": got["on"],
                             "gateway": {"enabled": profiles.gateway_enabled(cfg), "level": level, "text": text}}}

    def accounts_probe(self, body):
        """POST /api/accounts/probe {account?} = `hub accounts probe [<name>]`: a fresh limits measurement of the
        account (without a name — every one with its own token in the pool), one minimal request with its token
        (modules/gateway measure); the limits go to the core as account.limits with the account and its token generation,
        the snapshot redraws the card. While a login module leads session login, the module measures its pool and writes
        the readings (accounts.module_probe). -> 200 {results: [{name, ok, error?}]} in pool order | 400 a field other
        than account | 404 not in the pool | 409 a measurement is already running, nothing to measure, the module
        refused or cannot measure. On an idle account it opens its 5-hour window. No built-in pool in the tree (the open
        flavor, core/cutout.py) and no login module — nothing to measure."""
        from core import accountprobe
        if set(body) - {"account"} or not isinstance(body.get("account", ""), str):
            return 400, self.t("web.srv.account_probe_field")   # another key is not "all accounts": each reading spends a limit
        name = body.get("account") or ""
        module = self.profiles.sign_in()
        if module is None and not cutout.present("profile"):
            return 409, self.t("web.srv.account_probe_none")
        if module is None:
            from modules.accounts import accounts
            from modules.gateway import gateway as gw
            pool = accounts.load()
            if name and name not in [a["name"] for a in pool["accounts"]]:
                return 404, self.t("web.srv.account_unknown")
        if not self.probe_lock.acquire(blocking=False):   # two clicks — one measurement (each spends the limit)
            return 409, self.t("web.srv.account_probe_busy")
        try:
            path = os.path.join(self.state_dir, "journal.jsonl")
            start, reqs = journal.mark(path), {}
            if module is None:
                env, facts = gw.sessions_env(), []
                got = accounts.probe([name] if name else [], self.tail.reader.state, pool,
                                     lambda token: gw.measure(token, env=env),
                                     lambda account, gen, lim: facts.append((account, gw.limits_fact(account, gen, lim))))
                # every reading into the mailbox at once, then one wait for the core's decisions (not decide_wait per account)
                for account, data in facts:
                    reqs[account] = uuid.uuid4().hex[:12]
                    self.put("account.limits", None, {**data, "req": reqs[account]})
            else:   # the module puts its readings into the mailbox itself, each with its req
                st = self.tail.reader.state
                got, refusal = accountprobe.module_probe(
                    self.profiles.mods(), module, [name] if name else [], {"owner": True},
                    ((st.get("modules") or {}).get(module) or {}).get("version"))
                if refusal:
                    return 409, self.t("web.srv.account_probe_module", module=module, why=refusal)
                if any(r["code"] == "unknown" for r in got):
                    return 404, self.t("web.srv.account_unknown")
                reqs = {r["name"]: r["req"] for r in got if r["code"] == "ok" and r.get("req")}
            decided = accountprobe.recorded(path, reqs, self.decide_wait, start)
        finally:
            self.probe_lock.release()
        if not got:
            return 409, self.t("web.srv.account_probe_none")
        out = []
        for r in got:
            seq, why = decided.get(r["name"], (None, None))
            error = None if r["code"] == "ok" and seq is not None and not why else \
                self.t("web.srv.account_probe_refused", why=str(self.said(why))) if r["code"] == "ok" and why else \
                self.t("web.srv.account_probe_queued") if r["code"] == "ok" else self.t(
                    "web.srv.account_probe_offline" if r["code"] == "offline" else
                    "web.srv.account_probe_no_limits" if r["code"] == "no_limits" else "web.srv.account_probe_skip",
                    error=str(r.get("error") or ""), status=str(r.get("status") or ""))
            out.append({"name": r["name"], "ok": error is None, **({"error": error} if error else {})})
        return 200, {"results": out}

    def ext_config(self):
        """Extensions: bundled (modules/web/bundled) and from settings. For a bundled one, only "disabled" or "enabled"
        is taken from settings — the path and code are always its own. With default_off in the manifest it is off until
        enabled. An entry with no folder and no source is an enabled/disabled flag of a bundled plugin the hub no longer
        has (e.g. the threads view was removed): not a plugin, silently skipped."""
        own = dict(self.config().get("extensions") or {})
        out = {}
        for name in sorted(os.listdir(BUNDLED)) if os.path.isdir(BUNDLED) else []:
            try:
                with open(os.path.join(BUNDLED, name, "hub-plugin.json"), encoding="utf-8") as f:
                    man = json.load(f)
            except (OSError, ValueError):
                continue
            out[name] = {"path": os.path.join(BUNDLED, name), "bundled": True, "version": str(man.get("version") or ""),
                         "hub": man.get("hub"), **{k: man[k] for k in ("web", "themes", "css") if man.get(k)},
                         **({"default_off": True} if man.get("default_off") else {})}
            mine = own.pop(name, None)
            mine = mine if isinstance(mine, dict) else {}
            if mine.get("disabled") or (man.get("default_off") and not mine.get("enabled")):
                out[name]["disabled"] = True
        return {**out, **{k: v for k, v in own.items() if live_extension(k, v)}}

    def plugins(self, lang=None):
        """All extensions and plugins for Settings -> Plugins: source, version, compatibility, themes, whether disabled."""
        from core.version import compat
        out = []
        for name, ext in self.ext_config().items():
            if not re.fullmatch(EXT_NAME, name):
                continue
            path = os.path.expanduser(str(ext.get("path") or ""))
            st, why = compat(ext.get("hub")) if ext.get("source") else ("ok", "")  # an own one — no version check
            man = {}
            try:  # what it does and why — from the plugin manifest in its folder
                with open(os.path.join(path, "hub-plugin.json"), encoding="utf-8") as f:
                    man = json.load(f) if path else {}
            except (OSError, ValueError):
                pass
            man = man if isinstance(man, dict) else {}
            icon = man.get("icon")
            icon = icon if isinstance(icon, str) and self.plugin_icon_path(name) else None
            missing = [k for k in ("icon", "description", "why") if not str(man.get(k) or "").strip()] \
                if ext.get("source") else []  # an own extension — no catalog requirements
            out.append({"name": name, "source": ext.get("source"), "bundled": bool(ext.get("bundled")),
                        # the version is the one in the folder (the manifest); the install record may lag behind
                        "version": str(man.get("version") or ext.get("version") or ""),
                        "icon": f"/api/plugins/icon?name={name}" if icon else None, "missing": missing,
                        "description": self.manifest_text(name, ext, man, "description", lang, 300),
                        "why": self.manifest_text(name, ext, man, "why", lang, 500),
                        "commit": (ext.get("commit") or "")[:7], "web": bool(ext.get("web")), "run": bool(ext.get("run")),
                        "themes": [t for t in ext.get("themes") or [] if isinstance(t, str) and ".." not in t.split("/")],
                        "disabled": bool(ext.get("disabled")), "missing": not os.path.isdir(path),
                        "compat": st, "warning": self.t("web.srv.plugin_in_core") if name in IN_CORE else why,
                        # page code has full access to the page whether page_access is declared or not (there is no isolation)
                        "page_access": "full" if ext.get("web") else None,
                        **({"market": ext["market"]} if isinstance(ext.get("market"), str) else {})})
        return out

    def manifest_text(self, name, ext, man, field, lang=None, most=300):
        """What a plugin does and why: for a bundled one — from the catalog in the page language (web.<name>.manifest_*),
        for the others — as in its manifest (its language is the author's business)."""
        key = f"web.{name}.manifest_{field}"
        if ext.get("bundled") and key in i18n.catalog(i18n.SOURCE):
            return i18n.tr(lang, key) if lang else self.t(key)   # our own text — in full
        return str(man.get(field) or "")[:most]   # a foreign manifest — no longer than most

    def plugin_icon_path(self, name):
        """The icon file of an installed plugin: from its hub-plugin.json, inside its folder, svg/png/webp, up to 256 KB."""
        from modules.market import market
        ext = self.ext_config().get(name) or {}
        root = os.path.realpath(os.path.expanduser(str(ext.get("path") or "")))
        try:
            with open(os.path.join(root, "hub-plugin.json"), encoding="utf-8") as f:
                icon = (json.load(f) or {}).get("icon")
        except (OSError, ValueError, AttributeError):
            return None
        if not ext.get("path") or not isinstance(icon, str) or not market._file(icon):
            return None
        path = os.path.realpath(os.path.join(root, icon))
        ok = path.startswith(root + os.sep) and os.path.splitext(path)[1].lower() in market.ICON_TYPES
        return path if ok and os.path.isfile(path) and os.path.getsize(path) <= market.ICON_MAX else None

    def about(self, core_started, sessions, updates=()):
        import platform
        import sys as _sys
        from core import pagepass
        journal_path = os.path.join(self.state_dir, "journal.jsonl")
        size = lambda p: os.path.getsize(p) if os.path.exists(p) else 0  # noqa: E731
        # the "Updates" screen takes the notes of past updates (updates: what the latest journal poll published) and the full
        # code card here, not from the snapshot
        return {"code": self.code(), "updates": list(updates),
                "core_started": core_started, "web_started": getattr(self, "started", None),
                "plugin_api": PLUGIN_API, "python": platform.python_version(), "os": f"{platform.system()} {platform.release()}",
                "repo": os.path.dirname(os.path.dirname(HERE)), "state": self.state_dir, "sessions": sessions,
                "journal": size(journal_path), "archive": sum(size(p) for p in journal.archives(self.state_dir)),
                "executable": _sys.executable,
                # the hub's own addresses for a client to keep as fallbacks (the tailnet IP changes on a Tailscale reinstall)
                "addresses": access.addresses(access.status(self.state_dir)),
                # how the page is signed in to (core/pagepass.py): open | password | key | broken
                "signin": pagepass.state()}

    @staticmethod
    def about_lite(about):
        """/api/about?lite=1 — the same keys, without the two heavy parts: the notes of past updates (`notes` is empty,
        `notes_count` says how many there are) and the CHANGES.md entries (`code.changes` is empty); the code card is the
        snapshot's lean one (the unpublished list is empty, the count stays). For a client that only needs the version and
        the addresses (hub discovery, signing in); the "About" screens ask for the full one."""
        return {**about, "lite": True, "code": {**lean_code(about["code"]), "changes": []},
                "updates": [{**{k: v for k, v in u.items() if k != "notes"}, "notes": [], "notes_count": len(u.get("notes") or [])}
                            for u in about["updates"]]}

    def market(self):
        from core import catalog
        from modules.market.market import Market
        return Market(catalog.config_path(self.state_dir), os.path.join(self.state_dir, "market"),
                      restart=lambda parts: self.put("hub.restart_requested", None, {"parts": parts}),
                      events=lambda kind, data: self.put(kind, None, data), via="web")

    def plugin_market(self, what, arg, accept=False):
        from modules.market.market import Error, NeedsConsent
        import subprocess
        m = self.market()
        self.updates_cache = None  # installed, updated, removed — recompute "newer available"
        try:
            text = {"install": lambda full: m.install(full, accept=accept), "remove": m.remove,
                    "update": lambda name: m.update(name, accept=accept),
                    "install_reviewed": lambda full: m.install(full, reviewed=True, accept=accept),
                    "update_reviewed": lambda name: m.update(name, reviewed=True, accept=accept)}[what](arg)
        except NeedsConsent as e:   # new permissions: the page shows them and asks "Allow and update"
            return 409, {"error": self.t("web.srv.plugin_consent", name=arg), "wider": e.shown}
        except (Error, subprocess.CalledProcessError, OSError) as e:
            return 400, str(getattr(e, "stderr", "") or e)[:300]
        self.plugins_changed(what, arg)
        return 200, {"ok": True, "text": text}

    def from_catalog(self, key):
        """Something installed from a connected catalog (the market field in hub.json), or, from before catalogs existed,
        from a source that the catalog pinned (marketplace spec §4) -> "<entry>@<catalog>" or None: it is updated and
        removed by core/mpinstall — at exactly the catalog's commit, into the new storage, not the old path (the author's head)."""
        from core import catalog, marketplaces
        return marketplaces.entry_for(self.state_dir, catalog.read_settings(catalog.config_path(self.state_dir)), key)

    def plugin_entry(self, what, arg, accept=False, at=None):
        """A catalog entry from the page: install <entry>@<catalog> — install or update to the catalog's version,
        preview — what it will ask for (permissions per the manifest at the catalog's commit, before installing),
        update <key> — update an installed one, remove <key> — remove it. at — the catalog commit at which the owner saw
        the entry and its permissions (the preview returns it): if the catalog has been updated since — a refusal. New
        permissions without accept — 409 with a list, nothing is installed; consent is the plugins.consented event
        (via web). A core module goes only via the command: it starts immediately, and `hub marketplace install` prints
        its permissions."""
        from core import catalog, mpinstall
        cfg_path = catalog.config_path(self.state_dir)
        restart = lambda parts: self.put("hub.restart_requested", None, {"parts": parts})  # noqa: E731
        events = lambda kind, data: self.put(kind, None, data)  # noqa: E731
        self.updates_cache = None
        try:
            if what in ("preview", "install"):
                _, m, _, e = mpinstall.find(self.state_dir, cfg_path, arg)
                if e["type"] == "module":
                    return 400, self.t("web.srv.module_by_command", entry=arg)
                if what == "preview":
                    return 200, {"ok": True, "at": m["commit"], "preview": mpinstall.install(
                        self.state_dir, cfg_path, arg, preview=True)}
            if what == "remove":
                lines = mpinstall.uninstall(self.state_dir, cfg_path, arg, events=events, restart=restart)
            else:
                lines = mpinstall.install(self.state_dir, cfg_path, self.from_catalog(arg) if what == "update" else arg,
                                          accept=accept, via="web", events=events, restart=restart, at=at)
        except mpinstall.NeedsConsent as e:
            return 409, {"error": self.t("web.srv.plugin_consent", name=arg), "wider": e.shown}
        except (RuntimeError, ValueError, OSError) as e:
            return 400, str(e)[:600]
        self._ver_at = 0   # plugin files changed — the page version is recomputed right away
        return 200, {"ok": True, "text": "\n".join(lines)}

    def plugins_changed(self, what, name):
        """Record a fact in the journal: plugin settings live in a file, and without an event the page got no snapshot
        while the fleet was quiet (enabling a plugin appeared to do nothing); with it a new snapshot arrives at once,
        the page reloads with the plugin, and the journal shows who installed what."""
        self._ver_at = 0   # the page version (plugin files) — recompute right away
        self.put("plugins.changed", None, {"what": what, "name": name})

    def plugin_updates(self, fresh=False):
        """Which installed plugins have a newer version published by the author — with the review of the new version, if
        one was installed. Cached for 10 minutes: there is no need to hit GitHub every time a tab opens; "Check" fetches fresh."""
        import time
        cache = getattr(self, "updates_cache", None)
        if fresh or not cache or time.time() - cache[0] > 600:
            m = self.market()
            try:
                found = m.updates()
            except Exception as e:  # noqa: BLE001 — network: empty, and why
                return {"updates": {}, "error": str(e)[:200]}
            self.updates_cache = cache = (time.time(), found)
        from core import catalog, marketplaces
        reviews = self.market().reviews()
        cfg = catalog.read_settings(catalog.config_path(self.state_dir))
        cats = marketplaces.readable(self.state_dir, cfg)
        out = {}
        for key, up in cache[1].items():
            # the fetch may predate an install from a catalog (or the catalog being connected): its version is in the catalog
            if marketplaces.entry_for(self.state_dir, cfg, key, cats):
                continue
            rv = reviews.get(up["repo"])
            out[key] = {**up, **({"review": {k: rv.get(k) for k in ("state", "summary", "commit")}}
                                 if rv and rv.get("commit") == up["commit"] else {})}
        return {"updates": out}

    def plugin_review(self, full):
        """The hub reviews a plugin's code itself and connects it only afterwards, with the user's consent. A clone at the
        commit goes to the review folder (nothing is connected), the task goes to Hub Developer: read the code and give
        a verdict; the owner installs with the "Install the reviewed one" button — exactly this clone."""
        from modules.market.market import Error
        import subprocess
        try:
            dev = (self.config().get("dev") or {}).get("name") or "hub-dev"
            rec = self.market().stage(full, to=dev)
        except (Error, subprocess.CalledProcessError, OSError) as e:
            return 400, str(getattr(e, "stderr", "") or e)[:300]
        from modules.market.market import review_task
        if self.once(("plugin.review", full, rec["commit"])):
            self.put("message.received", None, {"to": dev, "text": review_task(rec), "action": True,
                                                 "button": self.t("web.plugins.check_install")})
        return 202, {"ok": True, "text": self.t("web.srv.review_started", dev=dev, commit=rec["commit"][:7])}

    ACTION_ONCE = 30.0   # for this many seconds an identical button does not send the same thing to a session again

    def once(self, key):
        """The first press of an action button within ACTION_ONCE seconds is True, a repeat is False. A repeat comes
        from a double click and from the browser itself (it repeats a POST when the connection dropped: two requests
        9 ms apart); the lock is there because requests run in parallel. A refused press does not take the key: call
        this after the checks."""
        now = time.monotonic()   # a clock moved back must not hold the button for hours
        with self.sent_lock:
            self.sent_actions = {k: t for k, t in self.sent_actions.items() if now - t < self.ACTION_ONCE}
            if key in self.sent_actions:
                return False
            self.sent_actions[key] = now
            return True

    def plugin_fix(self, name):
        """Fix a plugin by itself and send a PR/merge request to its author: a task for Hub Developer — fork the
        plugin's repo, port it to the current plugin API, verify, send a PR to the author; until the PR is accepted the
        plugin is installed from the fork."""
        p = next((x for x in self.plugins() if x["name"] == name), None)
        if not p or not p.get("source"):
            return 404, self.t("web.srv.fix_no_plugin")
        dev = (self.config().get("dev") or {}).get("name") or "hub-dev"
        if not self.once(("plugin.fix", name)):   # the same press again: the task is already with the session
            return 202, {"ok": True, "text": self.t("web.srv.fix_started", dev=dev)}
        warning = p.get("warning") or ""
        missing = T("core.web.fix_missing", "the plugin folder is not on the disk") if p.get("missing") else ""
        why = warning + ("; " if warning and missing else "") + missing   # + keeps the marks of the phrase
        at = "@" + p["commit"] if p.get("commit") else ""
        # a task to the session (core/coretext.py): it reads the English, the owner the same in his language
        text = PLUGIN_FIX.format(name=name, source=p["source"], at=at, api=PLUGIN_API, why=why or T(
            "core.web.fix_page_errors", "errors on the page — \"Settings\" → \"Plugins\""))
        self.put("message.received", None, {"to": dev, "text": str(text), "action": True, "l10n": coretext.marks(text),
                                             "button": self.t("web.plugins.fix_pr")})
        return 202, {"ok": True, "text": self.t("web.srv.fix_started", dev=dev)}

    fix_sent = None   # the update check (its ts) that the last "Fix" task was sent for: one task per check, whatever tab or device presses

    def app_restore(self):
        """The shell's launcher puts the released files back into the layer and asks hub-up to restart (its own process,
        the shell's copy of launch.py: the code doing the restore is not the edited code). The core records the result
        (core/appwatch.py) — the page sees it in the snapshot."""
        from core import appwatch, packaged
        import subprocess
        import platforms
        if not (packaged.is_app(packaged.info()) and appwatch.layer(packaged.REPO)):
            return 409, self.t("web.srv.app_layer_only")
        try:
            with open(os.path.join(self.state_dir, appwatch.APP, "shell.json"), encoding="utf-8") as f:
                shell = json.load(f)
        except (OSError, ValueError):
            shell = None
        launch, python = (shell or {}).get("launch"), (shell or {}).get("python")
        if not (isinstance(launch, str) and os.path.basename(launch) == "launch.py" and os.path.isfile(launch)
                and isinstance(python, str) and os.path.isfile(python)):
            return 409, self.t("web.srv.app_no_shell")
        subprocess.Popen([python, "-I", "-B", launch, "--restore", self.state_dir], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **platforms.current.apart())
        return 202, {"ok": True, "text": self.t("web.srv.app_restoring")}

    def app_undo(self):
        """`hub app undo` apart from the page's process: it stops the app's hub, this server with it. Its words go to
        logs/app-move.log; the clone's hub comes up on the same port."""
        from core import appmove
        import subprocess
        import sys as _sys
        import platforms
        rec = appmove.record(self.state_dir) or {}
        if rec.get("status") != "moved" or not appmove.snapshot(self.state_dir):
            return 409, self.t("web.srv.app_no_move")
        hub = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "bin", "hub")
        os.makedirs(os.path.join(self.state_dir, "logs"), exist_ok=True)
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE")}
        env["HUB_STATE_DIR"] = self.state_dir
        path = os.path.join(self.state_dir, "logs", "app-move.log")
        cmd = appmove.undo_command(self.state_dir, _sys.executable, hub, path, env=env)
        with open(path, "a", encoding="utf-8") as log:
            platforms.current.popen_detached(cmd, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        return 202, {"ok": True, "text": self.t("web.srv.app_undoing")}

    def update_fix(self, check):
        """"Fix" on the band about the hub's own edits of the core (modules/update local_edits): not a reset from the page, a
        task for Hub Developer of this hub (the text is modules/update fix_task). Pressed again before a newer check has seen
        the result (another tab, a reload, a retry) — 409: two cleanups of one folder would race."""
        edits = check.get("local_edits")
        if not isinstance(edits, dict) or not edits.get("count"):
            return 409, self.t("web.srv.no_local_edits")
        if self.fix_sent is not None and self.fix_sent == check.get("ts"):
            return 409, self.t("web.srv.local_edits_again")
        from modules.update.update import fix_task
        dev = (self.config().get("dev") or {}).get("name") or "hub-dev"
        text = fix_task(self.state_dir, edits)   # the core's phrases: the owner reads them in their language (l10n)
        self.put("message.received", None, {"to": dev, "text": str(text), "action": True, "l10n": coretext.marks(text),
                                             "button": self.t("web.updates.local_edits_fix")})
        self.fix_sent = check.get("ts")
        return 202, {"ok": True, "text": self.t("web.srv.local_edits_started", dev=dev)}

    def update_local_edits(self, to, check):
        """"Take off" (to: off) or "Put back" (to: on) on the band about the hub's own edits of the core: the update process
        does it under the machine lock (modules/update EDITS_FLAG, read every 15 s), the result goes to the hub's feed.
        Nothing to take off or put back by the last check, or a press already on its way — 409."""
        from modules.update.update import EDITS_FLAG, NOW_FLAG, ask
        if to not in ("off", "on"):
            return 400, self.t("web.srv.edits_word")
        edits = check.get("local_edits") if isinstance(check.get("local_edits"), dict) else {}
        if not edits.get("count") if to == "off" else not edits.get("backup"):
            # the page saw an older check (another tab did it): a fresh one redraws the band within 15 s
            if not os.path.exists(os.path.join(self.state_dir, NOW_FLAG)):
                with contextlib.suppress(OSError):
                    ask(self.state_dir, NOW_FLAG, "")
            return 409, self.t("web.srv.no_local_edits") if to == "off" else self.t("web.srv.no_backup")
        if os.path.exists(os.path.join(self.state_dir, EDITS_FLAG)):
            return 409, self.t("web.srv.edits_pending")
        ask(self.state_dir, EDITS_FLAG, to)
        return 202, {"ok": True, "text": self.t("web.srv.edits_off_started") if to == "off" else self.t("web.srv.edits_on_started")}

    def settings_set(self, key, value):
        """A setting from the allowlist goes into hub.json (or the watchdog's watch.json), atomically; the change is an event in the journal."""
        from core import packaged
        from modules.web import settings
        from modules.watch import watch
        value, problem = settings.validate(key, value, self.lang_now(), self.config())
        if problem:
            return 400, problem
        data = packaged.info()
        if key == "update.enabled" and data is not None and bool(value) != packaged.is_app(data):   # core/packaged.apply decides
            return 400, packaged.how(data)
        row = settings.row_of(key)
        if row[1] == "watch":
            path = watch.config_path()
            cfg = watch.config()
            stored = (not value) if row[2] == "boolnot" else value
            old = settings.put(cfg, row[0], stored)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        else:
            with self.market().editing() as cfg:
                old = settings.put(cfg, row[0], value)
        self.put("settings.changed", None, {"what": key, "old": old, "new": value})   # the page gets a snapshot right away
        return 200, {"ok": True, "text": self.t("web.settings.saved", label=settings.label(row, self.lang_now()))}

    def module_toggle(self, mid, enabled):
        """A toggle on the "Modules" card — its key comes from the settings allowlist (hub.json or the watchdog's
        watch.json). Modules read settings themselves on every job — no restart needed."""
        from modules.web import modinfo
        key = modinfo.TOGGLES.get(mid)
        if not key:
            return 400, self.t("web.srv.no_switch")
        code, out = self.settings_set(key, bool(enabled))
        if code != 200:
            return code, out
        return 200, {"ok": True, "text": self.t("web.srv.module_on") if enabled else self.t("web.srv.module_off")}

    def module_model_allow(self, name, allowed):
        """Whether a module may use a model through the core: the name goes into modules.model under the hub.json lock —
        via the same modsrc.set_model as `hub module model`; allow only a discovered module with the model permission
        (a typo is not written)."""
        from core import catalog, modsrc, modules
        if not modules.NAME.fullmatch(name):
            return 400, self.t("web.srv.module_bad_name")
        changed = []
        try:
            modsrc.set_model(self.state_dir, catalog.config_path(self.state_dir), name, on=allowed, changed=changed)
        except modsrc.Unknown:
            return 404, self.t("web.srv.module_unknown", name=name)
        for was, new in changed:
            if new != was:
                self.put("settings.changed", None, {"what": "modules.model", "old": was, "new": new})
        return 200, {"ok": True, "text": self.t("web.srv.model_on") if allowed else self.t("web.srv.model_off")}

    def module_model(self, handler):
        """POST /api/module-model (modules/web/modmodel.py): a module is recognized by its launch secret, not by X-Hub —
        it does not need the owner's login. -> (code, JSON)."""
        from modules.web import modmodel
        try:
            length = int(handler.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= modmodel.MAX_BODY:
            handler.close_connection = True   # we do not read the body — close the connection
            return 400, json.dumps({"code": "bad", "error": f"the body is up to {modmodel.MAX_BODY} bytes"})
        raw = handler.rfile.read(length)
        account = (self.tail.snapshot.get("account") or {}).get("active")
        code, out = modmodel.answer(self.state_dir, self.config(), account, handler.headers, handler.client_address[0],
                                    raw, self.model_limits, lambda t, e, d: spool.put(self.spool, t, e, d, src="web"))
        return code, json.dumps(out, ensure_ascii=False)

    def module_enable(self, name, enabled, embed=None):
        """A third-party module is added to or removed from modules.enabled under the hub.json lock (like `hub module
        add`), an embed's module also to modules.off (core/modules.switch: it is on by its manifest); hubd sees the file
        edit and starts or stops the process. Only a module the hub discovered: a typo is not written to settings. The
        core's own built-in modules are not switched here. embed — whether the row pressed is an embed's module (False: a
        copy with its name in a folder or git — only modules.enabled); None (a page from before it sent this) — by the
        name."""
        from core import catalog, modules
        found = [f for f in modules.discover(self.config(), self.state_dir) if f["origin"] != "builtin" or f.get("embed")]
        if name not in {f["name"] for f in found}:
            return 404, self.t("web.srv.module_unknown", name=name)
        with catalog.edit_settings(catalog.config_path(self.state_dir)) as cfg:
            lists = modules.switch(cfg, name, enabled, embed is not False and any(f["name"] == name and f.get("embed")
                                                                                  for f in found))
        for what, was, now in lists:
            if what == "modules.enabled" or was != now:   # modules.off only when it changed
                self.put("settings.changed", None, {"what": what, "old": was, "new": now})
        return 200, {"ok": True, "text": self.t("web.srv.module_on") if enabled else self.t("web.srv.module_off")}

    def plugin_toggle(self, name, enabled):
        m = self.market()
        known = self.ext_config()
        if (name or "") not in known:
            return 404, self.t("web.srv.no_plugin")   # and a flag left from a removed bundled plugin is not a plugin
        bundled = known[name]

        class Missing(Exception):
            pass
        try:
            with m.editing() as cfg:   # under the lock: the page and `hub market` write hub.json concurrently
                if bundled.get("bundled"):  # bundled: settings keep only the difference from its default
                    cfg.setdefault("extensions", {})[name] = {}
                ext = (cfg.get("extensions") or {}).get(name or "")
                if ext is None:
                    raise Missing(name)   # an exception inside the block means the file is not written
                if enabled:
                    ext.pop("disabled", None)
                    if bundled.get("default_off"):
                        ext["enabled"] = True
                else:
                    ext["disabled"] = True
                if bundled.get("bundled") and bool(enabled) != bool(bundled.get("default_off")):  # back to the default — no entry
                    cfg["extensions"].pop(name)
        except Missing:
            return 404, self.t("web.srv.no_plugin")
        self.plugins_changed("toggle", name)
        runs = ext.get("run") or bundled.get("run")
        if enabled and runs:
            m.restart([f"ext:{name}"])
        if runs:
            text = self.t("web.srv.plugin_on_run", name=name) if enabled else self.t("web.srv.plugin_off_run", name=name)
        else:
            text = self.t("web.srv.plugin_on", name=name) if enabled else self.t("web.srv.plugin_off", name=name)
        return 200, {"ok": True, "text": text}

    def themes(self):
        """Page themes: from plugins (the manifest's themes field) and own files in themes/*.json of this hub's settings
        folder (`folder` in the answer, for the hint of the page). The page validates the shape and colors (js/themes.js);
        here we only check that the file reads as a JSON object."""
        found, errors = [], []
        for src in self.theme_sources():
            tid, origin, path, _ = src
            try:
                with open(path, encoding="utf-8") as f:
                    theme = json.load(f)
                if not isinstance(theme, dict):
                    raise ValueError(self.t("web.srv.theme_not_object"))
                item = {"id": tid, "origin": origin, "theme": theme}
                if isinstance(theme.get("art"), dict):
                    art = self.theme_art(tid, src)
                    if art:
                        theme["art"]["url"] = f"/api/themes/art?id={quote(tid)}&v={int(os.path.getmtime(art))}"
                    else:
                        item["warning"] = self.t("web.srv.theme_art_missing")
                # icons, avatars, font (1.3): URLs for the page; anything missing or of the wrong type gives a warning
                url = lambda rel: (lambda real: real and f"/api/themes/file?id={quote(tid)}&f={quote(rel)}&v={int(os.path.getmtime(real))}")(self.theme_file(tid, rel, src))  # noqa: E731
                bad = []
                for key in ("icons", "avatars"):
                    if isinstance(theme.get(key), dict):
                        got = {k: url(v) for k, v in theme[key].items() if isinstance(v, str)}
                        theme[f"{key}_url"] = {k: v for k, v in got.items() if v}
                        bad += [f"{key}.{k}" for k, v in got.items() if not v]
                if isinstance(theme.get("font"), str):
                    theme["font_url"] = url(theme["font"])
                    bad += [] if theme["font_url"] else ["font"]
                if bad:
                    item["warning"] = "; ".join(filter(None, [item.get("warning"),
                                                              self.t("web.srv.theme_files_bad", files=", ".join(bad))]))
                found.append(item)
            except (OSError, ValueError) as err:
                errors.append({"id": tid, "origin": origin, "error": str(err)[:200]})
        return {"themes": found, "errors": errors, "folder": self.theme_folder()}

    def theme_dir(self):
        from core import catalog
        return os.path.join(os.path.dirname(catalog.config_path(self.state_dir)), "themes")

    def theme_folder(self):
        """theme_dir for a person: ~/… when it is under the home folder (not on Windows, where ~ is no path)."""
        own, home = self.theme_dir(), os.path.expanduser("~")
        if os.name != "nt" and home != os.sep:
            rel = os.path.relpath(own, home)
            if rel != os.pardir and not rel.startswith(os.pardir + os.sep) and not os.path.isabs(rel):
                own = "~/" + rel
        return own.rstrip("/\\") + ("\\" if os.name == "nt" else "/")

    def theme_sources(self):
        """(id, origin, theme file, the folder its art must stay inside): own themes and themes of enabled plugins."""
        own = self.theme_dir()
        sources = [(f"own:{n}", self.t("web.srv.theme_own"), os.path.join(own, n), own) for n in
                   (sorted(x for x in os.listdir(own) if x.endswith(".json")) if os.path.isdir(own) else [])]
        known = self.ext_config()
        for e in self.extensions():
            ext = known.get(e["name"]) or {}
            for t in ext.get("themes") or []:
                real = os.path.realpath(os.path.join(e["dir"], t))
                if real.startswith(os.path.realpath(e["dir"]) + os.sep):
                    sources.append((f"{e['name']}:{t}", self.t("web.srv.theme_plugin", name=e["name"]), real, e["dir"]))
        return sources

    def theme_file(self, tid, rel, src=None):
        """A file declared in a theme (art, icon, avatar, font), or None: only what is named in its JSON, inside the
        plugin's folder or the own themes folder, of an allowed type and no larger than ART_MAX. src — its source from
        theme_sources, if already found (in the theme list: otherwise each icon would rebuild all the sources — hundreds
        of icons, ~1 s)."""
        src = src or next((s for s in self.theme_sources() if s[0] == tid), None)
        try:
            with open(src[2], encoding="utf-8") as f:
                theme = json.load(f)
            declared = {(theme.get("art") or {}).get("image"), theme.get("font"),
                        *((theme.get("icons") or {}).values()), *((theme.get("avatars") or {}).values())}
            if not isinstance(rel, str) or rel not in declared:
                return None
            root = os.path.realpath(src[3])
            real = os.path.realpath(os.path.join(os.path.dirname(src[2]), rel))
        except (TypeError, OSError, ValueError, AttributeError):
            return None
        if os.path.splitext(real)[1].lower() not in KIT_TYPES or not real.startswith(root + os.sep) \
                or not os.path.isfile(real) or os.path.getsize(real) > ART_MAX:
            return None
        return real

    def theme_art(self, tid, src=None):
        """A theme's art file, or None: an image next to the theme, inside the plugin's folder or the own themes folder,
        no larger than ART_MAX; src — as for theme_file."""
        src = src or next((s for s in self.theme_sources() if s[0] == tid), None)
        try:
            with open(src[2], encoding="utf-8") as f:
                image = json.load(f)["art"]["image"]
            root = os.path.realpath(src[3])
            real = os.path.realpath(os.path.join(os.path.dirname(src[2]), image))
        except (TypeError, OSError, ValueError, KeyError):
            return None
        if os.path.splitext(real)[1].lower() not in ART_TYPES or not real.startswith(root + os.sep) \
                or not os.path.isfile(real) or os.path.getsize(real) > ART_MAX:
            return None
        return real

    def extensions(self):
        """Page extensions from settings (EXTENDING.md): name, folder, main module. Disabled and incompatible ones
        (a different major plugin API version) are not loaded."""
        from core.version import compat
        out = []
        for name, ext in self.ext_config().items():
            path = os.path.expanduser(str(ext.get("path") or ""))
            if ext.get("disabled") or name in IN_CORE or (ext.get("source") and compat(ext.get("hub"))[0] == "incompatible"):
                continue
            if (ext.get("web") or ext.get("themes")) and os.path.isdir(path) and re.fullmatch(EXT_NAME, name):
                # css: only with a page module (a plugin of themes alone changes the look only while its theme is chosen)
                out.append({"name": name, "dir": path, "main": ext.get("web"), "css": pluginstyle.css_files(ext.get("css")) if ext.get("web") else []})
        return out

    def ext_style_links(self):
        """The <link> tags of the plugins' stylesheets (the css field): in the page's own <head>, so a plugin's tokens are
        there from the first frame, not after the page has loaded the plugin's code. The address is exactly the one
        hub.addStyle builds (/ext/<name>/<file>), so the page does not add the same sheet a second time."""
        import html
        q = lambda x: html.escape(x, quote=True)  # noqa: E731 — both values are already vetted (EXT_NAME, pluginstyle.css_files)
        return "".join(f'<link rel="stylesheet" href="/ext/{q(e["name"])}/{q(f)}" data-plugin="{q(e["name"])}">\n'
                       for e in self.extensions() if e["main"] for f in e["css"])

    # --- server --------------------------------------------------------------------------------------------------
    def handler(self):
        web = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def end_headers(self):
                # only the page itself may embed the page (plugin files under /ext/ live on it) — a foreign site cannot
                # slip it under its own click; on every response, refusals included
                self.send_header("Content-Security-Policy", "frame-ancestors 'self'")
                self.send_header("X-Frame-Options", "SAMEORIGIN")
                super().end_headers()

            def setup(self):
                super().setup()
                self.wfile = httpd.Writer(self.connection)   # the write deadline is per chunk, not for the whole response

            def handle_one_request(self):
                try:
                    super().handle_one_request()
                except ReaderBusy:   # the lock was not acquired within READ_WAIT — respond instead of holding the socket and thread any longer
                    from modules.web.modproxy import refuse
                    refuse(self, 503, self.t("web.srv.err_reader_busy"), close=True, extra={"Retry-After": "1"})
                    self.close_connection = True

            @contextlib.contextmanager
            def reading(self):
                """The journal reader lock (web.tail.reading) while the client waits for the reply: if the client left,
                httpd.Gone (the thread drops the request and the socket closes at once, not when the lock frees up —
                otherwise hundreds of sockets piled up in CLOSED/CLOSE_WAIT); if not acquired within READ_WAIT — 503."""
                if not web.tail.ready:   # the first journal pass holds the lock to the end — nothing to wait for
                    raise ReaderBusy()
                end = time.monotonic() + READ_WAIT
                while not web.tail.reading.acquire(timeout=min(READ_STEP, max(0.0, end - time.monotonic()))):
                    if httpd.gone(self.connection):
                        raise httpd.Gone()
                    if time.monotonic() >= end:
                        raise ReaderBusy()
                try:
                    yield web.tail.reader
                finally:
                    web.tail.reading.release()

            def lang(self):
                """The page language for this request: hub.json -> owner -> hub before languages existed -> browser (core/i18n.py human)."""
                try:
                    cfg = web.config()
                except (OSError, ValueError):
                    cfg = {}
                # without the journal lock (Tail.lang_state): re-reading the feeds after a language change holds it for tens of seconds
                return i18n.human(cfg, web.tail.lang_state, self.headers.get("Accept-Language"), page=True)

            def send_page(self, path, lang):
                """index.html in the page language (static text is rendered by the server, with no flash of a foreign
                language); the tag derives from the file, the catalogs and the language: a language change does not
                reuse an old copy from the browser cache."""
                st = os.stat(path)
                # the built-in icon set is inlined in the page (our own file only, not plugins'): icons show from the
                # first paint, with no second request; the separate /static/icons.svg stays for plugins and old pages.
                # It is read once and the tag follows what was read: if the file is missing (an update in the middle) the
                # page goes without icons, not a 500, and its tag will not match the tag of a page with icons
                try:
                    with open(os.path.join(STATIC, "icons.svg"), encoding="utf-8") as f:
                        sprite = f.read()
                except OSError:
                    sprite = ""
                sprite_tag = f"{zlib.crc32(sprite.encode()):x}-{len(sprite):x}"
                # the files' version (modules/web/pagecache.py) is in the tag: index.html does not change when a module
                # does, and a 304 would keep the old version's addresses (and the early hints, derived from the same files)
                ver, vendor = pagecache.walk(STATIC)[0], pagecache.walk(VENDOR_DIR)[0]
                links = web.ext_style_links()   # the plugins' stylesheets are part of the page: another set, another tag
                tag = f'W/"{st.st_mtime_ns:x}-{st.st_size:x}-{sprite_tag}-{i18n.stamp(lang)}-{ver}-{vendor}' \
                      + (f'-{zlib.crc32(links.encode()):x}"' if links else '"')
                if tag in [t.strip() for t in (self.headers.get("If-None-Match") or "").split(",")]:
                    self.send_response(304)
                    self.send_header("ETag", tag)
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Vary", "Accept-Encoding, Accept-Language")
                    self.renewed()
                    return self.end_headers()
                with open(path, encoding="utf-8") as f:
                    page = i18n.render_html(f.read(), lang)
                sprite = sprite.replace('<svg ', '<svg id="hub-icon-sprite" width="0" height="0" '
                                        'aria-hidden="true" style="position:absolute;pointer-events:none" ', 1)
                page = page.replace("<!-- hub-icon-sprite -->", sprite, 1)
                if sprite:
                    # in the file, icons point to /static/icons.svg — so a server started before the files were
                    # updated also finds them (it serves the new index.html without the inlining); with the inlining
                    # they point to the set in the page
                    page = page.replace('<use href="/static/icons.svg#', '<use href="#')
                # the early hints (modules/web/preload.py) go by the addresses the page itself uses
                hints = lambda prefix, html=page: preload.head_hints(STATIC, html, prefix)  # noqa: E731
                page = pagecache.page(page, ver, vendor, hints)
                if links:   # after the core's stylesheets: a plugin's token overrides the core's, as hub.addStyle did
                    page = page.replace("</head>", links + "</head>", 1)
                return self.send(200, page, "text/html; charset=utf-8", "no-cache", tag)

            def local_host(self):
                # localhost (with DNS rebinding protection) or the owner from the tailnet — modules/tailnet/access.py
                return access.allowed(web.state_dir, self.headers.get("Host"), self.client_address[0], headers=self.headers)

            def signin_needed(self):
                """Who came is known (this machine, the owner by a provider), only the page's password is missing
                (core/pagepass.py): 401 and the sign-in form, not "localhost only"."""
                from core import remote
                return remote.needs_signin(web.state_dir, self.headers.get("Host"), self.client_address[0], self.headers)

            def refuse_signin(self):
                return self.send(401, json.dumps({"error": self.t("web.pass.err_signin"), "code": "signin"},
                                                 ensure_ascii=False, separators=(",", ":")))

            def renewed(self):
                """The page load renews a sign-in cookie older than a day (core/pagepass.renewal): who uses the page
                does not type the password again."""
                from core import pagepass
                if pagepass.state() == "password":
                    cookie = pagepass.renewal(self.headers)
                    if cookie:   # due: only then whether it came over https (a whois for the tailnet)
                        from modules.web import signin
                        if signin.secure(web, self):
                            cookie = pagepass.renewal(self.headers, secure=True)
                        self.send_header("Set-Cookie", cookie)

            def not_local(self, key):
                """The refusal for a request local_host turned away: one that came through a proxy or tunnel says so;
                one whose only lack is the page's password — how to sign in."""
                if self.signin_needed():
                    return self.t("web.pass.err_signin")
                if access.via_proxy(self.client_address[0], self.headers) and not self.local_host():   # else: another reason
                    from core import remote
                    if remote.refused_by(web.state_dir, self.headers.get("Host"), self.client_address[0],
                                         self.headers) == "cloudflare":
                        return self.t("web.srv.err_cloudflare")
                    return self.t("web.srv.err_proxied")
                return self.t(key)

            def send_file(self, path, ctype, cache="no-store", extra=None, tag=None):
                """The whole file or a piece by Range (206): Safari plays video only this way (otherwise videos would not open).
                tag — the file's ETag: a Range with an If-Range of another version gets the whole file (200), not a
                piece of the new file stitched into the old one; with a weak tag (W/) every Range gets the whole file."""
                size = os.path.getsize(path)
                m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range") or "")
                if_range = self.headers.get("If-Range")
                if m and not (m.group(1) or m.group(2)) or m and tag and (tag.startswith("W/") or if_range not in (None, tag)):
                    m = None   # "bytes=-" is not a range; a stale If-Range, or a weak tag (bytes may differ) — the whole file
                start, end = 0, size - 1
                if m:
                    if m.group(1):
                        start, end = int(m.group(1)), int(m.group(2)) if m.group(2) else size - 1
                    else:
                        start = max(0, size - int(m.group(2)))
                    end = min(end, size - 1)
                    if start > end:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")   # keep-alive: without it the client waits for a body
                        self.end_headers()
                        return
                self.send_response(206 if m else 200)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(end - start + 1))
                if m:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Cache-Control", cache)
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                with open(path, "rb") as f:
                    f.seek(start)
                    left = end - start + 1
                    while left > 0:
                        chunk = f.read(min(1 << 20, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)

            def gzip_ok(self):
                for part in (self.headers.get("Accept-Encoding") or "").split(","):
                    name, _, q = part.partition(";")
                    if name.strip() == "gzip":
                        return not re.fullmatch(r"\s*q=0(\.0*)?\s*", q)
                return False

            def send(self, code, body, ctype="application/json; charset=utf-8", cache="no-store", etag=None):
                data = body if isinstance(body, bytes) else body.encode()
                packable = ctype.startswith(GZIP_TYPES)
                packed = packable and len(data) >= GZIP_MIN and self.gzip_ok()
                if packed:
                    data = gzip.compress(data, 6, mtime=0)
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", cache)
                if packed:
                    self.send_header("Content-Encoding", "gzip")
                if packable:
                    self.send_header("Vary", "Accept-Encoding")
                if etag:
                    self.send_header("ETag", etag)
                once, self.cookie_once = getattr(self, "cookie_once", None), None
                if once:
                    self.send_header("Set-Cookie", once)
                if ctype.startswith("text/html") and self.path.split("?")[0] in ("/", "/index.html"):
                    self.renewed()
                self.end_headers()
                self.wfile.write(data)

            def send_static(self, path, ctype):
                """A page file: the browser keeps it but revalidates every time (no-cache) — if unchanged, 304 with no body.
                An edit on disk is visible from the next load, as with no-store; a reload does not refetch everything
                (mermaid is 2.5 MB)."""
                st = os.stat(path)
                tag = f'W/"{st.st_mtime_ns:x}-{st.st_size:x}"'
                if tag in [t.strip() for t in (self.headers.get("If-None-Match") or "").split(",")]:
                    self.send_response(304)
                    self.send_header("ETag", tag)
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Vary", "Accept-Encoding")
                    return self.end_headers()
                with open(path, "rb") as f:
                    return self.send(200, f.read(), ctype, "no-cache", tag)

            def send_versioned(self, folder, path, ctype, ver):
                """A page file under /static/v/<ver>/ or /vendor/v/<ver>/ (modules/web/pagecache.py): cached for good
                when the bytes are that version's, otherwise revalidated as send_static. The tag is the content: a 304
                never promotes bytes the browser got under another version."""
                with open(path, "rb") as f:
                    data = f.read()
                try:
                    rel = os.path.relpath(path, os.path.realpath(folder)).replace(os.sep, "/")
                except ValueError:   # Windows: another drive — not that version's file, revalidated
                    rel = ""
                cache, sha = pagecache.cache_for(folder, ver, rel, data)
                tag = f'W/"{sha[:20]}"'   # weak: the same tag for the gzip and the plain body
                if tag in [t.strip() for t in (self.headers.get("If-None-Match") or "").split(",")]:
                    self.send_response(304)
                    self.send_header("ETag", tag)
                    self.send_header("Cache-Control", cache)
                    self.send_header("Vary", "Accept-Encoding")
                    return self.end_headers()
                return self.send(200, data, ctype, cache, tag)

            def send_plugin_file(self, path):
                """A file of a page plugin's folder (already checked to be inside it) by its type: code as send_static,
                media (PLUGIN_MEDIA) as send_media; another type — a 404."""
                ext = os.path.splitext(path)[1]
                if ext.lower() in PLUGIN_MEDIA:   # code types stay case-sensitive, as they were
                    return self.send_media(path, PLUGIN_MEDIA[ext.lower()])
                if ext in STATIC_TYPES:
                    return self.send_static(path, STATIC_TYPES[ext])
                return self.send(404, self.err(self.t("web.srv.err_no_ext_file")))

            def send_media(self, path, ctype, extra=None, tag_of=None, cache="no-cache", thumb=None):
                """A plugin's media file: revalidated by its tag (304 with no body), otherwise the whole file or a piece by
                Range (send_file) — a video seeks without loading all of it. A file's tag is strong: browsers send If-Range
                only with one, and a file replaced mid-playback then comes whole. A copy's (tag_of: a thumbnail, the
                source's stat, its width and, for a png, "png") is weak — made again it may differ in bytes — and it always
                comes whole; the tag ends in -w<px> whatever the format (clients read that tail). thumb: the X-Hub-Thumb
                header, on a 304 too — the width of the copy, 0 for the original (/api/file); None: no header."""
                st = tag_of[0] if tag_of else os.stat(path)   # a copy of a file (thumbnail): the tag is of its source's stat
                tag = f'"{st.st_ino:x}-{st.st_mtime_ns:x}-{st.st_size:x}' + (   # a file swapped in by rename is another inode
                    (f"-{tag_of[2]}" if len(tag_of) > 2 and tag_of[2] else "") + f'-w{tag_of[1]}"' if tag_of else '"')
                if tag_of:   # a copy made again (a week later, a newer sips) may differ in bytes: the same picture, not the same bytes
                    tag = "W/" + tag
                if not self.headers.get("Range") and tag.removeprefix("W/") in \
                        [t.strip().removeprefix("W/") for t in (self.headers.get("If-None-Match") or "").split(",")]:   # weak comparison (RFC 9110)
                    self.send_response(304)
                    self.send_header("ETag", tag)
                    self.send_header("Cache-Control", cache)
                    self.send_header("X-Content-Type-Options", "nosniff")
                    if thumb is not None:
                        self.send_header("X-Hub-Thumb", str(thumb))
                    return self.end_headers()
                return self.send_file(path, ctype, cache, extra={**FILE_HEADERS, **(extra or {}), "ETag": tag,
                                                                **({"X-Hub-Thumb": str(thumb)} if thumb is not None else {})}, tag=tag)

            def t(self, key, **params):
                """Text in the request's language."""
                return i18n.tr(self.lang(), key, **params)

            def err(self, text):
                """The body of a refusal {"error": text}."""
                return json.dumps({"error": text}, ensure_ascii=False, separators=(",", ":"))   # same as the former literals

            def do_GET(self):
                web.req.lang = self.lang   # response texts use this request's language (Web.t), only when needed
                if access.cross_site(self):
                    return self.send(403, self.err(self.t("web.srv.err_cross_site")))
                from modules.web import pagekeyweb   # the old link with a key of the state "key" (core/pagepass.py)
                if pagekeyweb.from_link(web, self):
                    return
                if web.modproxy.serve(self, "GET"):
                    return
                if not self.local_host():
                    if self.signin_needed():   # the page's password (core/pagepass.py), from anywhere
                        if self.path.split("?")[0] in ("/", "/index.html", "/signin"):   # /signin: a wrong try's address
                            from modules.web import signin
                            return signin.page(web, self)   # the form, nothing of the hub's data
                        if self.path.split("?")[0] == "/api/about" and access.local(
                                self.headers.get("Host"), self.client_address[0], headers=self.headers):
                            # the update's watchdog (modules/update/trial.py): is it up, on what code; a neighbour's page:
                            # how this hub is signed in to
                            return self.send(200, json.dumps(pagekeyweb.about_alive(web), ensure_ascii=False))
                        return self.refuse_signin()
                    return self.send(403, self.err(self.not_local("web.srv.err_localhost")))
                if self.path.split("?")[0] == "/signin":   # signed in (or no password): the page itself
                    self.send_response(303)
                    self.send_header("Location", "/")
                    self.send_header("Content-Length", "0")
                    return self.end_headers()
                if self.path == "/api/signin":   # how the page is signed in to (core/pagepass.py); a signed-out tab hears 401
                    from core import pagepass
                    return self.send(200, json.dumps({"signin": pagepass.state()}))
                if self.path == "/api/instances":   # Settings -> Main -> Hubs on this computer
                    from modules.web import instancesweb
                    return self.send(200, json.dumps(instancesweb.status(web), ensure_ascii=False))
                if self.path == "/api/secrets":   # Settings -> Secrets: the vault without values (modules/web/secretsweb.py)
                    from modules.web import secretsweb
                    return self.send(200, json.dumps(secretsweb.status(web, self), ensure_ascii=False))
                if self.path in ("/", "/index.html"):
                    if self.headers.get("Accept-Language"):   # a person's browser; a probe of the port (hub instance) sends none
                        web.came(i18n.negotiate(self.headers["Accept-Language"]))
                    return self.send_page(os.path.join(STATIC, "index.html"), self.lang())
                if self.path.split("?")[0] == "/i18n.json":   # page strings in its language (static/js/i18n.js; API 1.25)
                    # ?lang=<code> — strings of another ready language: the app (iOS/Mac) tells the Flow view its own
                    # language (hubFlow.setLang), and the canvas labels the dots in it; an unknown code — the page language
                    ask = (parse_qs(urlparse(self.path).query).get("lang") or [""])[0]
                    lang = (i18n.negotiate(ask) if ask else None) or self.lang()
                    tag = f'W/"{i18n.stamp(lang)}"'   # ~30 KB compressed: a page reload gets 304 with no body
                    if tag in [x.strip() for x in (self.headers.get("If-None-Match") or "").split(",")]:
                        self.send_response(304)
                        self.send_header("ETag", tag)
                        self.send_header("Cache-Control", "no-cache")
                        return self.end_headers()
                    return self.send(200, json.dumps(i18n.page_bundle(lang), ensure_ascii=False), cache="no-cache",
                                     etag=tag)
                if self.path.split("?")[0] in APP_ICONS:   # the hub's own app icon, if there is one next to hub.json, wins
                    own = web.own_app_icon(maskable=self.path.split("?")[0].endswith("/icon-maskable-512.png"))
                    if own:
                        return self.send(200, own, "image/png", "no-cache")
                if self.path.startswith("/static/"):
                    # /static/v/<version>/<file> — the same file, cached for good when it is that version's
                    versioned = pagecache.split(self.path.split("?")[0]) if self.path.startswith(pagecache.PREFIX) else None
                    if self.path.startswith(pagecache.PREFIX) and not versioned:
                        return self.send(404, self.err(self.t("web.srv.err_no_file")))
                    rest = versioned[1] if versioned else self.path[len("/static/"):].split("?")[0]
                    real = os.path.realpath(os.path.join(STATIC, rest))
                    kind = STATIC_TYPES.get(os.path.splitext(real)[1])
                    if not kind or not real.startswith(os.path.realpath(STATIC) + os.sep) or not os.path.isfile(real):
                        return self.send(404, self.err(self.t("web.srv.err_no_file")))
                    if versioned:
                        return self.send_versioned(STATIC, real, kind, versioned[0])
                    return self.send_static(real, kind)
                if self.path == "/favicon.svg":
                    return self.send_static(FAVICON, "image/svg+xml")
                if self.path == "/favicon.ico":   # what a browser asks without a <link>: the tab icon, not the large mark
                    return self.send_static(os.path.join(STATIC, "favicon", "favicon.ico"), "image/x-icon")
                if self.path.split("?")[0] == "/manifest.webmanifest":   # the page as an app on the phone home screen
                    return self.send(200, web.manifest(self.lang()), "application/manifest+json; charset=utf-8", "no-cache")
                if self.path.startswith("/vendor/") and self.path[8:] in VENDOR:
                    return self.send_static(os.path.join(VENDOR_DIR, self.path[8:]), "text/javascript; charset=utf-8")
                versioned = pagecache.split(self.path.split("?")[0]) if self.path.startswith("/vendor/v/") else None
                if versioned and versioned[1] in VENDOR:   # the page's vendor scripts under the folder's version
                    # resolved, as the static files: the Mac app runs the code through a symlink
                    real = os.path.realpath(os.path.join(VENDOR_DIR, versioned[1]))
                    if not os.path.isfile(real):   # mid-update
                        return self.send(404, self.err(self.t("web.srv.err_no_file")))
                    return self.send_versioned(VENDOR_DIR, real, "text/javascript; charset=utf-8", versioned[0])
                if self.path.startswith("/api/file?"):
                    asked = parse_qs(urlparse(self.path).query).get("path", [""])[0]
                    real = web.own_file(asked) or web.shown_file(asked)
                    if not real:
                        return self.send(404, self.err(self.t("web.srv.err_no_attachment")))
                    from modules.files import server as files_server
                    from modules.files.server import disposition, mime_of
                    kind = mime_of(real) or "application/octet-stream"   # our own table: on Windows webp does not come from the registry
                    # served from the page's address — without scripts, as "Files" serves a raw file; html, svg and
                    # everything else is a download, the browser displays only raster images, audio and video
                    inline = kind.startswith(("audio/", "video/")) or kind.startswith("image/") and kind != "image/svg+xml"
                    # ?w=<px>: a raster picture scaled by width, from the Files cache (feature file.thumb): a png when
                    # the picture has an alpha channel, else a jpeg; the original when it cannot be scaled. A repeat is
                    # a 304 (the tag is of the source, the width and, for a png, its format: a jpeg copy a browser kept
                    # from before the format followed the alpha channel is white where the picture was transparent).
                    # X-Hub-Thumb says what came: the copy's width, 0 for the original (a client need not read the tag)
                    w = files_server.thumb_width(parse_qs(urlparse(self.path).query).get("w", [""])[0]) if inline else None
                    made = files_server.thumbnail(web.state_dir, real, w) if w else None
                    name = os.path.basename(real)
                    if made:   # the tag is of the stat the copy was made from, not of a later one
                        png = made[0].endswith(".png")
                        return self.send_media(made[0], "image/png" if png else "image/jpeg", extra={"Content-Disposition":
                                               disposition("inline", os.path.splitext(name)[0] + (".png" if png else ".jpg"))},
                                               tag_of=(made[1], w, "png" if png else ""), cache="private, no-cache", thumb=w)
                    return self.send_media(real, kind, extra={"Content-Disposition": disposition(
                        "inline" if inline else "attachment", name)}, cache="private, no-cache", thumb=0)
                if urlparse(self.path).path == "/api/voice/failed":   # play back a failed voice recording (/api/voice)
                    real = web.failed_voice(parse_qs(urlparse(self.path).query).get("id", [""])[0])
                    if not real:
                        return self.send(404, self.err(self.t("web.srv.err_no_file")))
                    from modules.files.server import disposition
                    return self.send_file(real, web.VOICE_TYPES.get(os.path.splitext(real)[1], "application/octet-stream"), extra={
                        **FILE_HEADERS, "Content-Disposition": disposition("inline", os.path.basename(real))})
                if self.path.startswith("/api/themes/file?"):  # a theme's icon, avatar, font — only those declared in it
                    q = parse_qs(urlparse(self.path).query)
                    real = web.theme_file((q.get("id") or [""])[0], (q.get("f") or [""])[0])
                    if not real:
                        return self.send(404, self.err(self.t("web.srv.err_no_theme_file")))
                    return self.send_file(real, KIT_TYPES[os.path.splitext(real)[1].lower()], "max-age=86400",
                                          extra=FILE_HEADERS)
                if self.path.startswith("/api/themes/art?"):
                    real = web.theme_art(parse_qs(urlparse(self.path).query).get("id", [""])[0])
                    if not real:
                        return self.send(404, self.err(self.t("web.srv.err_no_theme_art")))
                    return self.send_file(real, ART_TYPES[os.path.splitext(real)[1].lower()], "max-age=86400",
                                          extra=FILE_HEADERS)
                if self.path.startswith("/ext/"):  # page extension files (EXTENDING.md)
                    parts = urlsplit(self.path).path.split("/", 3)   # urlsplit: a ";" stays in the file name
                    ext = next((e for e in web.extensions() if len(parts) == 4 and e["name"] == parts[2]), None)
                    rel = unquote(parts[3]) if ext else ""   # "my clip.mp4" comes as my%20clip.mp4
                    real = os.path.realpath(os.path.join(ext["dir"], rel)) if ext and "\0" not in rel else ""
                    if not real or not real.startswith(os.path.realpath(ext["dir"]) + os.sep) or not os.path.isfile(real):
                        return self.send(404, self.err(self.t("web.srv.err_no_ext_file")))
                    return self.send_plugin_file(real)
                parts = urlsplit(self.path).path.split("/", 4)
                if len(parts) == 5 and parts[1] == "mod" and parts[3] == "web":
                    # page plugin files from a core module (core/modstate.py); /mod/<name>/view/ is handled by modproxy above
                    rel = unquote(parts[4])
                    real = modstate.web_file(web.state_dir, parts[2], rel) if "\0" not in rel else None
                    if not real:
                        return self.send(404, self.err(self.t("web.srv.err_no_mod_file")))
                    return self.send_plugin_file(real)
                if self.path.startswith("/api/backlog?"):  # a direction's board — a built-in view (no server of its own)
                    board = web.backlog((parse_qs(urlparse(self.path).query).get("slug") or [""])[0])
                    if board is None:
                        return self.send(404, self.err(self.t("web.srv.err_no_direction")))
                    return self.send(200, json.dumps(board, ensure_ascii=False))
                if self.path.startswith("/api/feed?"):  # a plugin channel (API 1.2): posts after a number
                    q = parse_qs(urlparse(self.path).query)
                    plugin = (q.get("plugin") or [""])[0]
                    since = query_number(q, "since", 0)
                    if not web.tail.ready:
                        raise ReaderBusy()
                    # like the stream snapshot - the latest completed poll: a plugin feed read does not queue behind
                    # the next journal batch or a checkpoint write; the published posts are detached
                    asked = q.get("post")
                    if asked:   # posts by number, also the ones that left the window (the last FEED_MAX): from the journal
                        if len(asked) > FEED_POSTS_AT_ONCE or not all(re.fullmatch(r"p[1-9][0-9]{0,14}", a) for a in asked):
                            return self.send(400, self.err(self.t("web.srv.err_feed_post", count=FEED_POSTS_AT_ONCE)))
                        return self.send(200, json.dumps({"posts": views.old_posts(web.state_dir, plugin, asked,
                                                                                 web.tail.feed_posts.get(plugin, ()))},
                                                         ensure_ascii=False))
                    posts = views.posts_since(web.tail.feed_posts.get(plugin, ()), since)
                    return self.send(200, json.dumps({"posts": posts}, ensure_ascii=False))
                if self.path.startswith("/api/flow"):  # who wrote to whom — for the flow animation
                    q = parse_qs(urlparse(self.path).query)
                    if urlparse(self.path).path == "/api/flow/history":
                        from core import flow
                        hours = max(0.0, min(query_number(q, "hours", 1.0, float), flow.WINDOW / 3600))
                        return self.send(200, json.dumps({"flow": flow.history(web.state_dir, time.time() - hours * 3600)},
                                                         ensure_ascii=False))
                    since = query_number(q, "since", 0)
                    with self.reading():
                        items = web.tail.reader.flow_since(since)
                    return self.send(200, json.dumps({"flow": items[-500:]}, ensure_ascii=False))
                if urlparse(self.path).path == "/api/send/receipt":   # the outcome of a send by client_id (the receipt)
                    q = parse_qs(urlparse(self.path).query)
                    code, out = web.receipt((q.get("session") or [""])[0], (q.get("client_id") or [None])[0])
                    return self.send(code, json.dumps(out, ensure_ascii=False))
                if self.path.startswith("/api/subagent?"):  # the steps of an agent called by an Agent step
                    from core import subagents
                    q = parse_qs(urlparse(self.path).query)
                    with self.reading():
                        sess = (web.tail.reader.state.get("sessions") or {}).get((q.get("session") or [""])[0]) or {}
                    claude = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
                    got = sess and subagents.steps(claude, sess.get("cwd"), sess.get("session_id"), (q.get("tool") or [""])[0],
                                                   state_dir=web.state_dir)   # a Codex subagent's steps: this hub's folder
                    if not got:
                        return self.send(404, self.err(self.t("web.srv.err_no_steps")))
                    return self.send(200, json.dumps(got, ensure_ascii=False))
                if self.path.startswith("/api/history?"):  # the feed's past: the core's live feed, then the journal archives
                    q = parse_qs(urlparse(self.path).query)
                    name = (q.get("session") or [""])[0]
                    before = query_number(q, "before", 10 ** 12)
                    n = min(query_number(q, "n", 100), 500)
                    from core.history import names
                    with self.reading():
                        live = list(web.tail.reader.timelines.get(name) or [])
                        known = names(web.tail.reader.state, name)
                    if "after" in q:   # a window around a search hit: rows newer than after
                        page = web.history.newer(name, query_number(q, "after", 0), n, live, keep=views.shown_line,
                                                 names=known)
                    else:
                        page = web.history.older(name, before, n, live, build=False, keep=views.shown_line, names=known)
                    return self.send(200, json.dumps(page, ensure_ascii=False))
                if self.path.startswith("/api/history/search?"):  # text search in the feed's past (older than what is loaded)
                    q = parse_qs(urlparse(self.path).query)
                    name, text = (q.get("session") or [""])[0], (q.get("q") or [""])[0]
                    before, cursor = query_number(q, "before", 10 ** 12), (q.get("cursor") or [""])[0] or None
                    if len(text.strip()) < 2:
                        return self.send(400, self.err(self.t("web.srv.err_search_short")))
                    from core.history import hit, names
                    with self.reading():
                        known = names(web.tail.reader.state, name)
                        # without a cursor — first what is older than what is loaded, in the core's live feed (it is longer than the page's window)
                        live = [] if cursor else list(web.tail.reader.timelines.get(name) or [])
                    needle = views.fold(text)
                    near = [hit(it, s, needle) for it in live if it.get("seq", 0) < before and views.shown_line(it)
                            and (s := views.found(it, needle)) is not None]
                    got = web.history.search(known, text, before, cursor, tab=(q.get("tab") or [""])[0][:64] or None)
                    seen = {h["key"] for h in near}
                    got["hits"] = near + [h for h in got.get("hits") or [] if h["key"] not in seen]
                    return self.send(200, json.dumps(got, ensure_ascii=False))
                if urlparse(self.path).path == "/api/session/kit":
                    # "Skills and MCP" of a session (core/uses.py, spec d2547143): its set and what it really used
                    from core import uses
                    q = parse_qs(urlparse(self.path).query)
                    name = (q.get("session") or [""])[0]
                    days = uses.PERIODS.get((q.get("days") or ["7"])[0], 7)
                    with self.reading():
                        live = web.tail.reader.state
                        sess = live["sessions"].get(name)
                        # what the report reads, copied under the lock: names, who raised whom, the sets, MCP statuses
                        st = None if not sess or sess["state"] == "removed" else {"sessions": {
                            n: {k: x.get(k) for k in uses.STATE_KEYS} for n, x in live["sessions"].items()}}
                    if st is None:
                        return self.send(404, self.err(self.t("web.srv.no_session", name=name[:80])))
                    got = uses.report(web.uses, st, name, days, time.time())
                    return self.send(200, json.dumps(got, ensure_ascii=False))
                if self.path == "/api/usage":  # "Usage": session spending and limits from the journal
                    from core import usage
                    with self.reading():
                        tz = (web.tail.reader.state.get("owner") or {}).get("tz")
                        account = dict(web.tail.reader.state.get("account") or {})   # the limits row is the active account's
                    try:   # after an update the archives take minutes to read — do not hold the request, say "later"
                        got = usage.report(web.state_dir, tz=tz, account=account, wait=web.USAGE_WAIT)
                    except usage.Busy:
                        from modules.web.modproxy import refuse
                        return refuse(self, 503, self.t("web.srv.usage_busy"), extra={"Retry-After": "30"})
                    return self.send(200, json.dumps(got, ensure_ascii=False))
                if urlparse(self.path).path == "/api/voice/local":   # Settings -> Modules -> Voice: the built-in model
                    code, out = web.voice_local()
                    return self.send(code, json.dumps(out, ensure_ascii=False))
                if urlparse(self.path).path == "/api/setup":   # Settings -> Setup: the checklist of a new hub
                    full = (parse_qs(urlparse(self.path).query).get("full") or ["1"])[0] != "0"
                    local = access.local(self.headers.get("Host"), self.client_address[0], headers=self.headers)
                    return self.send(200, json.dumps(web.setup().status(local=local, full=full, working=web.working()),
                                                     ensure_ascii=False))
                if self.path.split("?")[0] == "/api/about":  # Settings -> About the hub: version, uptime, where things live
                    # what the latest journal poll published, without its lock: "About the hub" (and a client's hub
                    # discovery, which has a 4 s deadline) does not wait for the lock even after the first pass, while a
                    # poll under load holds it; during a re-read from the start — the previous snapshot; if no snapshot
                    # from the journal exists yet — no numbers from it (null)
                    with web.tail.changed:   # the snapshot and started come from one poll (they change as a pair)
                        snap, started, known, ups = web.tail.snapshot, web.tail.started, web.tail.published, web.tail.updates
                    about = web.about(started if known else None, len(snap["sessions"]) if known else None, ups)
                    if (parse_qs(urlparse(self.path).query).get("lite") or [""])[0] != "1":
                        return self.send(200, json.dumps(about, ensure_ascii=False))
                    body = json.dumps(web.about_lite(about), ensure_ascii=False)
                    # a client that polls it pays for headers only while the card stands still: the journal's size is in
                    # it, so on a working hub every event changes the tag
                    tag = 'W/"' + hashlib.sha1(body.encode()).hexdigest()[:20] + '"'
                    if tag in [t.strip() for t in (self.headers.get("If-None-Match") or "").split(",")]:
                        self.send_response(304)
                        self.send_header("ETag", tag)
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("Vary", "Accept-Encoding")
                        return self.end_headers()
                    return self.send(200, body, etag=tag)
                if self.path.startswith("/api/stats?") and cutout.present("stats"):  # Modules -> statistics: what will be sent
                    from modules.stats import stats   # (hub stats), what was sent (log); the open flavor has no report
                    what = (parse_qs(urlparse(self.path).query).get("what") or [""])[0]
                    if what not in ("next", "sent"):
                        return self.send(400, '{"error":"what=next|sent"}')
                    lang = self.lang()   # the page's language, as the rest of "Modules"
                    # the report is built by a child process: the page keeps no copy of the hub state after it
                    text = stats.preview(web.state_dir, web.config(), lang=lang, make=stats.page_report) \
                        if what == "next" else stats.show_log(web.state_dir, 5, lang=lang)
                    return self.send(200, json.dumps({"text": text}, ensure_ascii=False))
                if self.path == "/api/modules":  # Settings -> Modules: what exists, what it provides, whether it is on
                    from modules.web import modinfo
                    lang = self.lang()
                    with self.reading() as reader:   # the snapshot and module state come from one poll
                        snap, st = web.tail.snapshot, web.mod_state(reader)
                    cfg = web.config()
                    try:   # foreign module files must not take the core-part cards down with them
                        mine = modinfo.installed(web.state_dir, cfg, st)
                    except Exception as e:  # noqa: BLE001
                        print(f"web: /api/modules installed — {type(e).__name__}: {e}", flush=True)
                        mine = []
                    return self.send(200, json.dumps({"modules": modinfo.modules(web.state_dir, cfg, snap, lang),
                                                      "installed": mine}, ensure_ascii=False))
                if self.path == "/api/themes":
                    return self.send(200, json.dumps(web.themes(), ensure_ascii=False))
                if self.path.startswith("/api/plugins/icon?"):  # an installed plugin's icon — for <img> only
                    from modules.market import market
                    path = web.plugin_icon_path((parse_qs(urlparse(self.path).query).get("name") or [""])[0])
                    if not path:
                        return self.send(404, self.err(self.t("web.srv.err_no_icon")))
                    with open(path, "rb") as f:
                        data = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", market.ICON_TYPES[os.path.splitext(path)[1].lower()])
                    self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; sandbox")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    return self.wfile.write(data)
                if self.path == "/api/marketplaces":   # "Plugins": connected catalogs and their entries
                    from core import catalog, marketplaces
                    try:
                        got = marketplaces.page(web.state_dir, catalog.config_path(web.state_dir))
                    except (RuntimeError, ValueError, OSError) as err:   # hub.json has the wrong shape, git does not run
                        got = {"catalogs": [], "entries": [], "error": str(err)[:300]}
                    # "Claude and Codex plugins": what the hub installed from the engines' catalogs, with what is inside;
                    # grant — this server takes "Give to a session…" (/api/tools/grant)
                    got["grant"] = True
                    try:
                        from core import engineplugins
                        got["engine"] = engineplugins.installed(catalog.read_settings(catalog.config_path(web.state_dir)))
                    except (ImportError, ValueError, OSError):
                        pass   # the page keeps its own view of them (from the entries and the snapshot's catalog)
                    return self.send(200, json.dumps({**got, "official": marketplaces.OFFICIAL_NAME}, ensure_ascii=False))
                if self.path.startswith("/api/marketplaces/icon?"):  # an entry's icon — from the catalog clone, for <img> only
                    from core import catalog, marketplaces
                    from modules.market import market
                    q = parse_qs(urlparse(self.path).query)
                    try:
                        got = marketplaces.icon(web.state_dir, catalog.config_path(web.state_dir),
                                                (q.get("catalog") or [""])[0], (q.get("entry") or [""])[0])
                    except (RuntimeError, ValueError, OSError):
                        got = None
                    kind = market.ICON_TYPES.get(got[1]) if got else None
                    if not kind:
                        return self.send(404, self.err(self.t("web.srv.err_no_icon")))
                    self.send_response(200)
                    self.send_header("Content-Type", kind)
                    self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; sandbox")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Cache-Control", "private, max-age=300")
                    self.send_header("Content-Length", str(len(got[0])))
                    self.end_headers()
                    return self.wfile.write(got[0])
                if self.path.startswith("/api/plugins/updates"):  # "a new version is available" for installed plugins
                    fresh = "fresh=1" in self.path
                    return self.send(200, json.dumps(web.plugin_updates(fresh), ensure_ascii=False))
                if self.path.startswith("/api/plugins/search"):  # Settings -> Plugins: plugins under review by link
                    q = (parse_qs(urlparse(self.path).query).get("q") or [""])[0]
                    try:
                        m = web.market()
                        reviews = m.reviews()
                        found = [{**p, **({"review": {k: reviews[p["repo"]].get(k) for k in ("state", "summary", "commit")}}
                                          if p.get("repo") in reviews else {})} for p in m.search(q)]
                        return self.send(200, json.dumps({"plugins": found}, ensure_ascii=False))
                    except Exception as err:  # noqa: BLE001 — the network to GitHub: empty, and why
                        return self.send(200, json.dumps({"plugins": [], "error": str(err)[:200]}, ensure_ascii=False))
                if self.path.startswith("/api/timeline?"):  # the open session's feed: what changed since revision since
                    q = parse_qs(urlparse(self.path).query)
                    name = (q.get("session") or [""])[0]
                    since = query_number(q, "since", 0)
                    with self.reading():  # the reader is modified in the journal polling thread
                        out = web.tail.reader.timeline(name, since, (q.get("epoch") or [""])[0])
                    return self.send(200, json.dumps(out, ensure_ascii=False))
                if self.path == "/api/snapshot":
                    if httpd.gone(self.connection):   # waited for the GIL while the client left: a 6 MB snapshot with nobody to send it to
                        raise httpd.Gone()
                    with web.tail.changed:   # the snapshot and module records come from one journal poll (they change as a pair)
                        snap, records = web.tail.snapshot, web.tail.records
                    return self.send(200, json.dumps(web.with_clock(snap, records, self.lang()), ensure_ascii=False))
                # a query is not part of the path (?delta=2 — item deltas, stream(); an app's ?delta=1 changes nothing); split at "?" as modproxy does (urlparse would also take the path with ";x" after it)
                if self.path.split("?")[0] == "/api/stream":
                    return self.stream()
                if self.files_redirect():
                    return
                self.send(404, self.err(self.t("web.srv.err_no_page")))

            def files_redirect(self):
                """An absolute path as the address (a link in a text led here and got "no such page"): to "Files" when it
                shows this file to this client, otherwise False and the usual 404. Files decides again on its own port."""
                raw = unquote(urlsplit(self.path).path)
                if re.match(r"/[A-Za-z]:[/\\]", raw):   # Windows: /C:/Users/x
                    raw = raw[1:]
                if not os.path.isabs(raw) or raw.startswith("/api/") or "\0" in raw:
                    return False
                if not web.files_target(raw, access.local(self.headers.get("Host"), self.client_address[0], headers=self.headers)):
                    return False
                name = urlsplit("//" + (self.headers.get("Host") or "")).hostname or "127.0.0.1"
                if ":" in name:
                    name = f"[{name}]"
                self.send_response(302)
                # no scheme: the browser keeps the page's own, as the links of the page do (location.protocol)
                self.send_header("Location", f"//{name}:{files_port(web.state_dir)}/open?path={quote(raw, safe='')}")
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return True

            def stream(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Vary", "Accept-Encoding")
                # compressed stream: every event is flushed to a boundary (Z_SYNC_FLUSH) — the browser gets it at once
                z = zlib.compressobj(6, zlib.DEFLATED, 31) if self.gzip_ok() else None
                if z:
                    self.send_header("Content-Encoding", "gzip")
                self.end_headers()

                def put(data):
                    self.wfile.write(z.compress(data) + z.flush(zlib.Z_SYNC_FLUSH) if z else data)
                seen, sent, last = None, 0.0, None   # last — {key: its JSON} of the last sent snapshot
                # ?delta=2 (feature stream.delta2): patches carry item deltas and a frame number; any other query is
                # ignored, such a stream gets whole sections as before
                deltas, frame = parse_qs(urlsplit(self.path).query).get("delta") == ["2"], 0
                # ?feed=<plugin>[,…] (feature stream.feed): new posts of those channels as `feed` events (stream_feed.py)
                asked, since_post = stream_feed.parse(self.path)
                cursors = None   # taken with the first snapshot built from the journal: before it every channel is empty
                connected = time.monotonic()   # a shared build older than this is followed by a fresh one (Web.stream_view)
                lang = self.lang()   # the snapshot text language holds for the whole connection (a language change reloads the page)
                # let in at the start is not let in for ever: a remote token expires; the page's password is set or
                # changed (core/pagepass.py) — a tab signed in with the old one must not keep its stream
                checked = time.monotonic()
                try:
                    while not web.stop.is_set():
                        if time.monotonic() - checked >= STREAM_RECHECK:
                            if not self.local_host():
                                self.close_connection = True   # the stream has no length: only closing ends it
                                break
                            checked = time.monotonic()
                        with web.tail.changed:
                            # a marked key (the shared build is older than the stream) has None instead of seq: do not wait, re-check after STREAM_GAP
                            if seen and web.tail.snapshot["seq"] == seen[0]:
                                web.tail.changed.wait(STREAM_WAIT)
                        # no more often than STREAM_GAP: when busy, the fleet writes up to three events a second to the journal
                        gap = STREAM_GAP - (time.monotonic() - sent)   # the same clock as the shared build
                        if gap > 0 and web.stop.wait(gap):
                            break
                        with web.tail.changed:   # the snapshot and module records as a pair; build without the lock
                            snap, records, rev = web.tail.snapshot, web.tail.records, web.tail.rev
                            posts, published = web.tail.feed_posts, web.tail.published   # the channels of this snapshot
                        # one build for all streams (Web.stream_view); without feeds: the page fetches the open session's
                        # feed itself, only the changed part (/api/timeline) — 2.2 MB per event was 90% feeds of all
                        # sessions. The first send is whole, then only the changed parts (558 KB a second went out
                        # while only 1–108 KB changed)
                        key, parts = web.stream_view(snap, records, lang, seen, since=connected, rev=rev,
                                                     alive=lambda: not httpd.gone(self.connection))
                        news = []   # the asked channels' new posts of this snapshot (stream_feed.py)
                        if asked:
                            if cursors is None and published:
                                cursors = stream_feed.start(asked, posts, since_post)
                            news = [stream_feed.frame(e) for e in stream_feed.step(cursors or {}, posts)]
                        if last is not None:   # the posts go before the patch whose seq covers them: a client that drops
                            for event in news:   # in between and comes back with that seq (feed_since) misses none
                                put(event)
                            news = []
                        if key != seen:
                            seen, sent = key, time.monotonic()
                            if last is None:
                                body = "{" + ",".join(f"{json.dumps(k)}:{v}" for k, v in parts.items()) + "}"
                                put(f"event: snapshot\ndata: {body}\n\n".encode())
                            elif deltas:   # the changed items of a big section, not the section (stream_delta.py)
                                body = stream_delta.patch(last, parts, frame + 1)
                                if body:
                                    frame += 1
                                    put(f"event: patch\ndata: {body}\n\n".encode())
                                else:
                                    put(b": ping\n\n")
                            else:
                                changed = [f"{json.dumps(k)}:{v}" for k, v in parts.items() if last.get(k) != v]
                                gone = [k for k in last if k not in parts]
                                if gone:
                                    changed.append(f'"_gone":{json.dumps(gone)}')
                                if changed:
                                    put(f"event: patch\ndata: {{{','.join(changed)}}}\n\n".encode())
                                else:   # the key changed but the parts are the same (a re-check after a shared build older than the stream)
                                    put(b": ping\n\n")
                            last = parts
                        else:
                            put(b": ping\n\n")
                        for event in news:   # after the first snapshot: it is the stream's start
                            put(event)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, TimeoutError):   # the client left or has not read for conn_timeout
                    pass

            def do_POST(self):
                web.req.lang = self.lang
                if access.cross_site(self):   # we do not read the body — close the connection
                    from modules.web.modproxy import refuse
                    return refuse(self, 403, self.t("web.srv.err_cross_site"), close=True)
                if urlparse(self.path).path == "/api/module-model":   # a core module calls a model (hubmod.model)
                    return self.send(*web.module_model(self))
                if web.modproxy.serve(self, "POST"):
                    return
                raw = urlparse(self.path).path in ("/api/upload/raw", "/api/voice/raw")   # the body is the file itself, not JSON
                if urlparse(self.path).path == "/signin":   # the sign-in form (core/pagepass.py): a plain form, no X-Hub
                    from core import remote
                    from modules.web import signin
                    if remote.identify(web.state_dir, self.headers.get("Host"), self.client_address[0],
                                       self.headers) is None:   # who came is unknown: no form for him
                        self.close_connection = True
                        return self.send(403, self.err(self.not_local("web.srv.err_not_from_page")))
                    return signin.post(web, self)
                from core import pagepass
                # the hub's own `hub secret fill|add` from this machine: the token of this start of the page, those paths only
                own = (urlparse(self.path).path in pagepass.LOCAL_PATHS and access.local(
                    self.headers.get("Host"), self.client_address[0], headers=self.headers)
                    and pagepass.local_ok(self.headers, web.state_dir, urlparse(self.path).path))
                if not (own or self.local_host()) or self.headers.get("X-Hub") != "1":
                    if raw:   # we do not read the body (up to hundreds of MB) — close the connection, or it would become the "next request"
                        from modules.web.modproxy import refuse
                        return refuse(self, 401 if self.signin_needed() else 403,
                                      self.not_local("web.srv.err_not_from_page"), close=True)
                    if self.signin_needed():
                        self.close_connection = True   # the body was not read
                        return self.refuse_signin()
                    return self.send(403, self.err(self.not_local("web.srv.err_not_from_page")))
                if urlparse(self.path).path == "/api/voice/raw":
                    return web.voice_raw(self)
                if raw:
                    return web.upload_raw(self)
                if urlparse(self.path).path == "/api/voice":   # a recording over the limit: say so, do not read tens of MB to cut them in half
                    from modules.web import modproxy as mp
                    length = (self.headers.get("Content-Length") or "").strip()
                    if length.isascii() and length.isdigit() and len(length) < 20 and int(length) > web.VOICE_BODY_MAX:   # else: below
                        mp.drain(self, int(length), web.VOICE_DRAIN_MAX)   # the page sees the answer
                        return mp.refuse(self, *web.voice_too_long(), close=True)
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(min(length, MAX_FILE * 2)) or b"{}")
                except ValueError:
                    return self.send(400, self.err(self.t("web.srv.err_not_json")))
                if not isinstance(body, dict):   # every action reads the object's fields
                    return self.send(400, self.err(self.t("web.srv.err_not_object")))
                if self.path == "/api/paths":   # where paths from session texts lead (links only to what exists)
                    items = body.get("items") if isinstance(body.get("items"), list) else []
                    local = access.local(self.headers.get("Host"), self.client_address[0], headers=self.headers)
                    return self.send(200, json.dumps({"items": web.resolve_paths(items, local)}, ensure_ascii=False))
                if self.path == "/api/signout":   # "Sign out": this browser's sign-in cookie is gone
                    from modules.web import signin
                    return signin.signout(web, self)
                if self.path == "/api/instances":   # "Add a hub", "Remove", "Password": the install runs apart from this server
                    from modules.web import instancesweb
                    code, msg, *cookie = instancesweb.act(web, body, self)
                    if cookie and cookie[0]:   # a password set or changed for this hub: this tab stays signed in
                        self.cookie_once = cookie[0]
                    return self.send(code, json.dumps(msg if isinstance(msg, dict) else {"error": msg}, ensure_ascii=False))
                if self.path == "/api/setup/bundle":   # «A file for support» (core/supportbundle): masked, sent nowhere
                    return web.setup_bundle(self)
                if self.path == "/api/secrets/fill":   # `hub secret fill` (modules/secrets): the value goes into the agents' Chrome
                    return self.send(*web.secret_fill(body))
                if self.path.startswith("/api/secrets/"):   # Settings -> Secrets: the owner's vault (modules/web/secretsweb.py)
                    from modules.web import secretsweb
                    return self.send(*secretsweb.act(web, self, body))
                code, msg = web.act(self.path, body)
                out = msg if isinstance(msg, dict) else ({"ok": True} if code < 300 else {"error": msg})
                self.send(code, json.dumps(out, ensure_ascii=False))

            def other_method(self):
                web.req.lang = self.lang
                # other methods exist only for module endpoints; everything else behaves like http.server with no such method
                if not web.modproxy.serve(self, self.command):
                    self.send_error(501, "Unsupported method (%r)" % self.command)

            do_PUT = do_PATCH = do_DELETE = other_method

        return Handler

    def disk_head(self):
        import subprocess
        try:
            r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=os.path.dirname(os.path.dirname(HERE)),
                               capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout.strip() if r.returncode == 0 else None

    def heal(self, now):
        """The core stayed on the pre-update code (core/restart.behind) while the page is already on the new one — it
        certainly restarted: once per version, ask for an ordinary restart of the whole hub (it waits for busy sessions,
        as always). Not earlier than HEAL_AFTER after its own start and not while a restart of the whole hub is already pending."""
        from core import restart
        if now - self.started < restart.HEAL_AFTER:
            return None
        to = self.tail.peek(lambda r: restart.heal(r.state, now), self.READ_WAIT)
        # healed — our own request that the core has not recorded yet (the restart_heal mark will appear with it)
        if to is Tail.BUSY or not to or to == self.healed or self.disk_head() != to:
            return None
        self.healed = to
        self.put("hub.restart_requested", None, {"parts": [], "now": False, "why": restart.HEAL_WHY, "heal": to})
        return to

    def heal_loop(self):
        from core import restart
        while not self.stop.wait(restart.HEAL_EVERY):
            try:
                self.heal(time.time())
            except Exception as e:  # noqa: BLE001 — the check must not take the page down
                print(f"web: heal: {e!r}", flush=True)

    def serve(self, port=8787, host="127.0.0.1"):
        server = access.Server((host, port), self.handler())
        threading.Thread(target=self.tail.run, args=(self.stop,), daemon=True).start()
        threading.Thread(target=self.heal_loop, daemon=True).start()
        threading.Thread(target=self.shown.follow, args=(self.stop,), daemon=True).start()
        threading.Thread(target=self.push.run, args=(self.stop,), daemon=True).start()
        from core import pagepass
        pagepass.new_local(self.state_dir)   # `hub secret fill` of this start (core/pagepass.py)
        from modules.web import signin   # the notice of a hub with the old link with a key: set a password
        threading.Thread(target=signin.notice_loop, args=(self,), daemon=True).start()
        return server


def _arenas():
    """glibc keeps a separate malloc arena per thread (page requests run in threads), and what is freed in them stays
    in the process's memory: on a hub copy, 181 MB sat in 5 arenas. Two are enough (measured: RSS 315 -> 262 MB).
    Without glibc (macOS, Windows, musl without mallopt) do nothing."""
    try:
        import ctypes
        return ctypes.CDLL(None).mallopt(-8, 2) == 1   # M_ARENA_MAX
    except (OSError, AttributeError, TypeError):
        return False


def files_port(state_dir):
    """This hub's "Files" port: HUB_FILES_PORT, else the anchor of an instance (core/instance.py), else 8788."""
    from core import instance
    return instance.port("files", state_dir)


def main():
    _arenas()
    from core import instance
    port = instance.port("web", default_state_dir())
    web = Web(default_state_dir())
    server = web.serve(port)
    print(f"hub page: http://127.0.0.1:{port}/", flush=True)
    # and from a phone, over the tailnet (the first server reads the journal — the second makes do with its tail)
    access.serve_tailnet(lambda ip, p: access.Server((ip, p), web.handler()), port, web.state_dir, web.stop)
    try:
        server.serve_forever()
    finally:
        web.stop.set()


if __name__ == "__main__":
    main()
