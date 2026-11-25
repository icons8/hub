"""The "Telegram" module: the owner's channel in his bot, through the core.

Incoming: an owner message is a `message.received` fact (channel telegram) in the core's spool: without a reply — to
the hub; as a reply to a session's message — to that session (a reply to an executor's answer goes to it, marked
"a reply to your answer"). Photos and files go as attachments. Buttons are answers to sessions' questions and "yes" to
risky tools. The bot runs /status, /away, /back itself, without the model (COMMANDS); a turn failure (limit, API) is not
an answer but a status line (on_failure).
Outgoing comes from the core's journal, like "Brief" on the page: replies of the hub and executors go as a reply to the
owner's message, where they asked (only if the chain started with their message from Telegram, core/chain.py, or they
are away), sessions' questions as buttons, tool requests, reminders, the "I'm back" summary, the line "The hub is back
online" after core downtime. Receipts are reactions:
👀 delivered, 👍 answered, 👎 not delivered. While a turn on their message runs — "typing…"; a turn longer than 45 s — its
first line at once, the result later; nothing within 10 min — one line "⏳ The reply is delayed". The "Voice reply"
module (voice_reply in hub.json) adds a short voice message after the reply to the owner's message.

The bot listens only to the owner: the number is paired by a code (`/start` in a private chat → a code for 10 min →
`hub-telegram pair <code>`; the command names whom it paired). Or it works in one group instead (spec d2513429): a code
from `/start` of the group's admin, `hub-telegram pair` in the owner's terminal shows the group and its admins and asks
(once the bot has an owner, the code goes to nobody: a card in the owner's chat, his press binds); the group's admins are
the owner (tg_by names who), its other members only ask (owner false, member) — the rights checked fresh with Telegram on
every word and press; `/here <session>` gives a topic of the group to a session; in a group there is no private chat and
no chats with people. The token and the number are in `telegram.json` in the
OS settings folder (mode 600), not in the repo. The module's own state (offset, which Telegram message belongs to which
session) is `<state>/telegram/state.json`; it does not write the hub's state, only facts.
"""
import getpass
import hashlib
import html
import http.client
import json
import os
import queue
import re
import secrets
import shutil
import threading
import sys
import time
import traceback
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from core import busstate, catalog, chain, coretext, delegations, forms, i18n, identity, minihub, net, news, owner, reducer, shown, specmode, spool, tools, views  # noqa: E402
from core.daemon import default_state_dir  # noqa: E402
from modules.jira import links as jira  # noqa: E402
from modules.tailnet import access  # noqa: E402
from modules.telegram import business, digest, fmt, hubs, mermaid, rich  # noqa: E402
from modules.voice import speech  # noqa: E402
from modules.web import attach  # noqa: E402
from modules.voice import transcribe as voice  # noqa: E402

API = os.environ.get("HUB_TELEGRAM_API", "https://api.telegram.org")
MAX_FILE = 20 * 1024 * 1024   # the Bot API gives bots no more than this
DECIMAL_COMMA = {"ru", "uk", "de", "fr", "es", "pt", "tr"}   # catalog languages with a decimal comma: "25,3 MB"
KEEP_IDLE = 30     # a connection to the Bot API idle longer than this is not reused: a proxy may have silently dropped it
UPLOAD_TRIES = 3   # this many times one file upload fails while the Bot API responds — the file is given up on, the chat goes on
# live feedback on the owner's message from Telegram (without extra messages: a short turn gets one result, as before)
TYPING_EVERY = 5         # Telegram clears "typing…" itself after ~5 s — refresh it while a turn for his message runs
FIRST_LINE_AFTER = 45    # a turn longer than this — its first line ("accepted, passing on …") goes at once, the result later
VOICE_SLOW_AFTER = 60    # a voice message is transcribed longer than this — one line "still transcribing" (otherwise a single 👀 for minutes)
VOICE_NET_PAUSES = (10, 30, 60, 120, 300)   # s between downloads of a voice message while the network to Telegram is down (the last repeats)
VOICE_NET_GIVE_UP = 15 * 60   # s without the network since the first failure — then "send it again" (the file is still on Telegram's side)
LATE_AFTER = 600         # nothing for him within 10 min — one line "⏳ <who> is still busy: <what it does>"
# Telegram's answer to a send into a topic that was deleted or closed: the message goes to "General" instead
THREAD_GONE = re.compile(r"thread not found|topic_(?:closed|deleted|id_invalid)|topic (?:is )?closed", re.I)
PAIR_TTL = 600           # the pairing code lives 10 minutes
PAIR_MAX = 5             # open codes at once; for strangers beyond that — nothing
PREVIEW_CHARS = 600      # how much of a permission request input its card shows
OPTION_LABEL = 200       # how much of an option's name goes into the heading of its preview
OPTION_PREVIEW = 3000    # how much of an option's preview sketch goes to the chat (a Telegram message holds 4096)
LIVE_FOR = 3600          # a message older than an hour — no live feedback (a bot restart, a long downtime: do not send old things)
DOWN_QUIET = 120         # the core was absent longer than this — a "Hub is back" line (a quick restart — without it)
UNASKED_QUIET = 3600     # failed turns of no human's message (a schedule, another session): one message per hub per hour, edited
UNASKED_NAMES = 5        # sessions named in it (the rest — "+N")
UNASKED_WHY = 3          # reasons named in it (the rest — "+N")
UNASKED_KEEP = 50        # sessions and reasons kept in the state for the hour
STATE_WORDS = {"busy": "telegram.state_busy", "dialog": "telegram.state_dialog", "idle": "telegram.state_idle",
               "asleep": "telegram.state_asleep", "starting": "telegram.state_starting"}   # catalog keys
# bot commands — it answers by itself from the core state, within seconds, even when the hub is busy or down; other /commands go to the hub
COMMANDS = {"/status": None,   # the descriptions are in the catalog (CATALOG_DESC): register_commands
            "/away": None,
            "/back": None,
            "/stop": None,
            "/spec": None,   # specs on|off for a session (core/specmode.py): a person's command, as in the page's input field
            "/page": None,   # the address of the page on the computer
            "/link": None}   # link this hub with another one (modules/telegram/hubs.py)


# --- transport: urllib cannot do a proxy with the https:// scheme (HTTPS_PROXY=https://…, curl works, urllib
# gives "Connection reset by peer"). urllib.request.ProxyHandler can only proxy through http://: it sends a plain proxy
# a bare CONNECT and TLS only up to the real host. A proxy that itself listens on TLS does not see such a CONNECT and
# drops the connection. curl and browsers in this case wrap CONNECT in TLS to the proxy themselves, and inside — one more
# TLS to the target; we do the same by hand (only http.client/ssl/socket, no new dependencies). ------

# The proxy for a host (as in urllib, NO_PROXY honoured — an improvement made when accepting a PR) and the connection
# through it now live in core/net, shared with the Claude Code downloads (core/claudeinstall); plain http (a local fake
# server in tests) goes directly
_proxy_url = net.proxy_for
_connection = net.connection


def read_body(resp, max_bytes=None):
    """The body, or up to max_bytes of it. read(limit) returns what came before a break without an error (read() raises
    IncompleteRead): a body shorter than Content-Length and than the limit is a cut one — IncompleteRead, so that a cut
    file is not taken for the whole one."""
    if max_bytes is None:
        return resp.read()
    body = resp.read(max_bytes)
    if len(body) < max_bytes and resp.length:   # length: what Content-Length still expects (None — chunked or unknown)
        raise http.client.IncompleteRead(body, resp.length)
    return body


def request(method, url, data=None, headers=None, timeout=30, max_bytes=None):
    """(code, body) without urllib — see _connection: our own CONNECT tunnel if the proxy itself listens on TLS."""
    parts = urllib.parse.urlsplit(url)
    conn = _connection(parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), timeout)
    try:
        path = parts.path + (("?" + parts.query) if parts.query else "")
        conn.request(method, path, body=data, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, read_body(resp, max_bytes)
    finally:
        conn.close()


# core.bus_* facts that put a ready card of the hub bus on a session (core/busstate._card)
BUS_CARDS = ("core.bus_send_held", "core.bus_received", "core.bus_intro", "core.bus_offer")


class TgError(Exception):
    """transient — a network failure, 429 or 5xx: retry later (retry_after — how long to wait by Telegram's word).
    unsent — the request never left (no connection to Telegram or the proxy): certainly not delivered."""
    def __init__(self, text, transient=False, retry_after=None, unsent=False):
        super().__init__(text)
        self.transient, self.retry_after, self.unsent = transient, retry_after, unsent


# a network failure: http.client raises its own HTTPException (IncompleteRead on a cut answer, BadStatusLine from a proxy),
# not an OSError; ValueError — an answer that is not JSON
NET_ERRORS = (OSError, ValueError, http.client.HTTPException)


class NotSent(OSError):
    """No connection could be made: the request did not leave (Api.keep_alive with once)."""


def gave_up(text):
    """The file was given up on after UPLOAD_TRIES: send_file writes a line about it instead of the file."""
    e = TgError(text)
    e.gave_up = True
    return e


class Api:
    def __init__(self, token, base=API):
        self.token, self.base = token, base.rstrip("/")
        self.local = threading.local()   # a connection of its own per thread
        self.clock = time.monotonic

    def keep_alive(self, verb, url, data, headers, wait, max_bytes=None, once=False):
        """(code, body) over one connection per thread: a new one for each call is TCP, TLS to the proxy, CONNECT and TLS
        to Telegram anew (several seconds behind a slow proxy), while getUpdates runs every ~2 s and the answer to a press comes
        right after it. The connection is made by the same _connection: the same proxy as without reuse. If the server
        or proxy closed an idle one — one retry on a new one; idle longer than KEEP_IDLE — close and open a new one.
        once: a call that must not reach Telegram twice (an answer to a person in the owner's name) — a new connection
        and no retry: a dropped one is the caller's "unknown"."""
        parts = urllib.parse.urlsplit(url)
        path = parts.path + (("?" + parts.query) if parts.query else "")
        if once:   # a connection of its own, made here: the kept one stays for getUpdates
            conn = None
            try:   # an https:// proxy makes the socket in _connection itself
                conn = _connection(parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), wait)
                conn.timeout = wait
                if conn.sock is None:
                    conn.connect()
            except OSError as e:
                if conn is not None:
                    conn.close()
                raise NotSent(str(e)) from None
            try:
                conn.request(verb, path, body=data, headers=headers)
                resp = conn.getresponse()
                return resp.status, read_body(resp, max_bytes)
            finally:
                conn.close()
        for fresh in (False, True):
            conn = None if fresh else getattr(self.local, "conn", None)
            if conn is not None and self.clock() - getattr(self.local, "used", 0) > KEEP_IDLE:
                conn.close()
                conn = None
            reused = conn is not None and conn.sock is not None
            if not reused:
                conn = _connection(parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), wait)
            conn.timeout = wait
            if conn.sock is not None:
                conn.sock.settimeout(wait)
            self.local.conn = None
            try:
                conn.request(verb, path, body=data, headers=headers)
                resp = conn.getresponse()
                body = read_body(resp, max_bytes)
            except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError):
                conn.close()
                if reused:
                    continue
                raise
            except BaseException:
                conn.close()
                raise
            if resp.will_close or not resp.isclosed():   # the server closes or not everything is read (max_bytes)
                conn.close()
            else:
                self.local.conn, self.local.used = conn, self.clock()
            return resp.status, body

    def upload(self, method, field, path, **params):
        """Sending a file (multipart/form-data): video, document."""
        return self.upload_many(method, [(field, path)], **params)

    def upload_many(self, method, files, **params):
        """Several files in one request: [(field, path)] (a rich report with its pictures)."""
        import uuid
        boundary = uuid.uuid4().hex
        parts = []   # joined once: a report with 50 pictures does not copy the growing body at every part
        for k, v in params.items():
            if v is None:
                continue
            v = json.dumps(v) if isinstance(v, (dict, list, bool)) else str(v)
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        for field, path in files:
            with open(path, "rb") as f:
                data = f.read()
            name = os.path.basename(path).replace('"', "")
            parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{name}"\r\n'
                          f"Content-Type: application/octet-stream\r\n\r\n").encode() + data + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        try:
            _, resp = request("POST", f"{self.base}/bot{self.token}/{method}", data=body,
                              headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, timeout=300)
            out = json.loads(resp)
        except NET_ERRORS as e:
            raise TgError(f"{method}: {e}", transient=True) from None   # network: retry, not lose the file
        return self.result(method, out)

    @staticmethod
    def result(method, out):
        """A Bot API response → its result or TgError: 429 and 5xx are temporary (retry_after — the pause it asks for)."""
        if not out.get("ok"):
            code = out.get("error_code") or 0
            raise TgError(f"{method}: {out.get('description')}", transient=code == 429 or code >= 500,
                          retry_after=(out.get("parameters") or {}).get("retry_after"))
        return out.get("result")

    def call(self, method, _once=False, **params):
        wait = (params.get("timeout") or 0) + 15  # getUpdates holds the connection for timeout seconds
        data = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
        try:
            # Telegram answers with JSON {ok:false, description} for an erroneous HTTP status too — we always read the body
            _, resp = self.keep_alive("POST", f"{self.base}/bot{self.token}/{method}", data,
                                      {"Content-Type": "application/json"}, wait, once=_once)
            out = json.loads(resp)
        except NotSent as e:
            raise TgError(f"{method}: {e}", transient=True, unsent=True) from None
        except NET_ERRORS as e:
            raise TgError(f"{method}: {e}", transient=True) from None
        return self.result(method, out)

    def download(self, file_id):
        path = self.call("getFile", file_id=file_id)["file_path"]
        try:
            status, body = self.keep_alive("GET", f"{self.base}/file/bot{self.token}/{path}", None, {}, 60,
                                           max_bytes=MAX_FILE + 1)
        except NET_ERRORS as e:   # a break in the middle of a download is the same network failure as in call()
            raise TgError(f"download: {e}", transient=True) from None
        if status != 200:  # otherwise the error text would go on as the file content (an improvement made when accepting a PR)
            raise TgError(f"getFile: HTTP {status} {body[:200]!r}", transient=status == 429 or status >= 500)
        return body


# --- module settings and state ----------------------------------------------------------------------------------

def config_path():
    return os.environ.get("HUB_TELEGRAM_CONFIG") or os.path.join(catalog.config_dir(), "telegram.json")


def load_config():
    try:
        with open(config_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    path = config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"   # the file is written by the bot and the hub-telegram commands: a temporary name unique to each
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def live_codes(cfg, now):
    """Open pairing requests {code: {user, name, at}} younger than PAIR_TTL (older ones and the former form {code: id} have expired)."""
    return {c: v for c, v in (cfg.get("pending") or {}).items()
            if isinstance(v, dict) and 0 <= now - v.get("at", 0) < PAIR_TTL}


def drop_code(cfg, code, now):
    """The request is decided: its code goes, the others' live ones stay."""
    cfg["pending"] = {c: v for c, v in live_codes(cfg, now).items() if c != code}


def oldest_out(pending):
    """A new request when all PAIR_MAX places are taken: the oldest one goes. Before the owner is bound anyone who knows
    the bot's name could fill the places with /start from five accounts and hold them for PAIR_TTL; now his own /start
    always gets a code (the command names whom it binds, so a stranger's code is seen). One code per person: to push
    his code out in the minute he types `hub-telegram pair`, a stranger needs five accounts with no live code of their
    own — a few such windows per PAIR_TTL, not a lock."""
    while len(pending) >= PAIR_MAX:
        del pending[min(pending, key=lambda c: pending[c].get("at", 0))]


def group_of(cfg):
    """The bound Telegram group {id, title} (`hub-telegram pair` with a group's code, spec d2513429), or None."""
    g = (cfg or {}).get("group")
    return g if isinstance(g, dict) and isinstance(g.get("id"), int) and not isinstance(g.get("id"), bool) else None


def display_name(user):
    """How a person is named in Telegram — safe for a terminal (without control characters)."""
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)
    if user.get("username"):
        name += f" (@{user['username']})"
    return "".join(c for c in name if c.isprintable())[:80] or i18n.t("telegram.no_name")


SAY_GROUP_AGAIN = 600     # s: the bot says telegram.group again while the core's state still differs
KEEP_ASKS, KEEP_TOOL_KEYS, KEEP_ROUTES = 100, 200, 3000   # how much of the old the bot state file keeps
KEEP_STOP_KEYS = 50      # buttons of the /stop list: only the last lists can still be pressed
STOPPABLE = ("starting", "busy", "dialog")   # "working" executors: those /stop offers
STOP_LIST_TTL = 600      # a /stop list is good for 10 minutes: an old button must not stop the executor's later run
HERE_FACTS = ("session.role_set", "task.backlog")   # what a /here press puts: the topic is bound when the core takes it
HERE_OFFER_TTL = 600     # /here's "Make an agent / a direction" is good for 10 minutes, and for one press
HERE_LIST = 30           # /here lists who is who: no more sessions than fit a glance
KEEP_HERE = 50           # /here offers and presses waiting for the core that the state file keeps
# voice failure code (VoiceError.kind) -> catalog key of the reason, as on the page (Web.VOICE_WHY): its own copy, not an import —
# a process of the previous release holds the old modules.voice in memory while the new files are read from disk
VOICE_WHY = {"format": "web.srv.voice_why_format", "ffmpeg_start": "web.srv.voice_why_ffmpeg_start",
             "ffmpeg_timeout": "web.srv.voice_why_ffmpeg_timeout", "ffmpeg_failed": "web.srv.voice_why_ffmpeg_failed",
             "engine_start": "web.srv.voice_why_engine_start", "engine_timeout": "web.srv.voice_why_engine_timeout",
             "engine_failed": "web.srv.voice_why_engine_failed", "engine_output": "web.srv.voice_why_engine_output",
             "model_missing": "web.srv.voice_why_model_missing", "engine_busy": "web.srv.voice_why_engine_busy",
             "cloud_login": "web.srv.voice_why_cloud_login", "cloud_limit": "web.srv.voice_why_cloud_limit",
             "cloud_failed": "web.srv.voice_why_cloud_failed"}
CATALOG_DESC = {"/status": "telegram.cmd_status", "/away": "telegram.cmd_away", "/back": "telegram.cmd_back",
                "/stop": "telegram.cmd_stop", "/spec": "telegram.cmd_spec", "/page": "telegram.cmd_page",
                "/link": "telegram.cmd_link_hub"}   # command descriptions that come from the catalog
MEMBER_COMMANDS = ("/status", "/page")   # what a member of the group (not an admin) may ask the bot: it only reads
KEEP_VOICED = 200   # keys "a voice message for this reply has already gone": there is no resend further back than a few replies
KEEP_NOTICES = 40   # `hub notify --id` notices that still know their message (a repeat edits it, the removal appends the result)


class Bot:
    def __init__(self, state_dir, api, clock=time.time):
        self.state_dir, self.api, self.clock = state_dir, api, clock
        self.render_mermaid = mermaid.render   # a diagram → PNG (headless Chrome); tests replace it
        self.spool = os.path.join(state_dir, "spool")
        self.dir = os.path.join(state_dir, "telegram")
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, "state.json")
        try:
            with open(self.path, encoding="utf-8") as f:
                self.st = json.load(f)
        except (OSError, ValueError):
            self.st = {}
        # outgoing that has not gone yet (a network failure — retry in order, do not lose)
        self.outbox, self.hold_until, self.fails, self.poll_fails = [], 0, 0, 0
        for k, v in (("offset", 0), ("seq", None), ("routes", {}), ("owner_msgs", {}), ("asks", {}), ("held", {}), ("keys", {}),
                     ("cards", {}), ("turn", {}), ("failed", []), ("live", {}), ("retry", {}), ("told", []), ("unasked", {}),
                     ("threads", {}), ("posted", {}), ("card_html", {}), ("quoted", {}), ("transcribe", {}), ("voice_wait", {}),
                     ("voiced", {}), ("notices", {}), ("digest", {}), ("topics", {}), ("topic_of", {}),
                     ("here_offers", {}), ("binding", {})):
            self.st.setdefault(k, v)
        self.sending, self.attempt = None, {}   # the event being sent and its parts sent in this attempt (once)
        # the group: the person of the update being handled ({role, tg, name}: what put() names), the session the event being
        # sent is about (its topic), whether a turn result went to the chat, the bot's own getMe
        self.local = threading.local()   # speaker: the person of the update this thread handles (a worker thread has none)
        self.ev_session, self.result_sent, self._me, self.gone_threads = None, False, None, set()
        self._group_said = (None, 0)   # what say_group last told the core (telegram.group) in this process, and when
        self.typing_at = {}   # mN → when "typing…" was sent (after a restart — anew, if the turn is still running)
        self.reader = views.Reader()
        self.journal = os.path.join(state_dir, "journal.jsonl")
        self.pos = 0
        self.cfg = load_config()
        self._quiet = (float("-inf"), False)   # (when hub.json was read, whether "quiet" is on)
        self._digest = (float("-inf"), False, 0)   # (when hub.json was read, telegram.digest, telegram.digest_minutes)
        self.digest_retry = 0                      # after a Telegram failure - not before this moment
        self.digest_next = 0                       # the next build of the digest text: not more than once a minute
        self.hub_config = lambda: catalog.config_or_last(state_dir)  # hub.json: the "Voice" (voice) and "Jira" (jira) modules
        self.voice_done = queue.Queue()  # (message, text, why it failed) — from background transcriptions
        self.voice_sync = False
        self.voicing = {}   # (chat, id) → (message, when started, whether "longer than usual" was said) — transcriptions in progress
        self.voicing_mu = threading.Lock()
        self.voice_flight = set()   # "chat:id" of transcriptions started and not yet taken by drain_voice (the bot loop's own)
        # reply → voice message: the builtin voice (Codex realtime) or a module with the speech capability; tests replace speak
        self.speech = speech.Speech(state_dir, records=lambda: self.reader.state.get("modules") or {},
                                    providers=lambda: self.reader.state.get("providers") or {})
        self.speak = self.speech.reply
        self.speak_lock = threading.Lock()   # one at a time: each has its own Chrome and codex app-server
        self.voice_out = queue.Queue()   # (what the reply is to, route, (ogg, seconds, said) | None, why it failed)
        self.hubs = hubs.Hubs(self)   # linking hubs: /link, a pasted link, the owners' chat (modules/telegram/hubs.py)
        self.business = business.Business(self)   # the owner's chats with people (modules/telegram/business.py)

    # --- general ----------------------------------------------------------------------------------------------------
    def prune(self):
        """Old things — from the state file: it grew without limit (1.5 MB, 8 thousand buttons, 5.6 thousand reply addresses) and
        is rewritten on every piece of news. Answer buttons — only for unanswered questions (a press on an old one — "the question is
        no longer relevant"), of answered questions — the last KEEP_ASKS, of tool buttons and reply addresses — the last ones."""
        asks = self.st["asks"]
        done = [k for k, a in asks.items() if a.get("done")]
        for k in done[:-KEEP_ASKS]:
            del asks[k]
        tools = [k for k, v in self.st["keys"].items() if v.get("do") == "tool"]
        drop = set(tools[:-KEEP_TOOL_KEYS])
        stops = [k for k, v in self.st["keys"].items() if v.get("do") == "stop"]
        drop |= set(stops[:-KEEP_STOP_KEYS])
        heres = [k for k, v in self.st["keys"].items() if v.get("do") == "here"]   # three buttons an offer
        drop |= set(heres[:-3 * KEEP_HERE])
        self.st["keys"] = {k: v for k, v in self.st["keys"].items()
                           if k not in drop and ("ask" not in v or not (asks.get(v["ask"]) or {"done": True}).get("done"))}
        if hasattr(self, "hubs"):
            self.hubs.prune()
        routes = self.st["routes"]
        for k in list(routes)[:-KEEP_ROUTES]:
            del routes[k]
        topic_of = self.st.get("topic_of") or {}
        for k in list(topic_of)[:-KEEP_ROUTES]:
            del topic_of[k]
        for kept in (self.st["here_offers"], self.st["binding"]):
            for k in list(kept)[:-KEEP_HERE]:
                del kept[k]
        voiced = self.st["voiced"]
        for k in list(voiced)[:-KEEP_VOICED]:
            del voiced[k]
        # the text of cards — while the bot knows them (a question in asks, a tool card in cards): a closed one returns it itself
        # (edit_done), forgotten ones do not accumulate in a file that is rewritten on every piece of news
        notices = self.st["notices"]
        for k in list(notices)[:-KEEP_NOTICES]:
            del notices[k]
        for k in [k for k, n in notices.items() if self.clock() - n["at"] >= 2 * owner.NOTICE_TTL]:   # long past its life (see notice())
            del notices[k]
        known = {str(m) for a in asks.values() for m in a.get("msgs") or []} | {str(v) for v in self.st["cards"].values()}
        known |= {str(n["mid"]) for n in notices.values()}
        self.st["card_html"] = {k: v for k, v in self.st["card_html"].items() if k in known}

    def save(self):
        self.prune()
        # no changes — do not write: the loop calls save on every pass, and the file is megabytes (about 60 MB/min to disk)
        data = json.dumps(self.st, ensure_ascii=False)
        if data == getattr(self, "_saved", None):
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, self.path)
        self._saved = data

    @property
    def speaker(self):
        """The person of the update this thread handles ({role, tg, name}; a worker thread has none): what put() names."""
        return getattr(self.local, "speaker", None)

    @speaker.setter
    def speaker(self, value):
        self.local.speaker = value

    def owners(self):
        return [int(x) for x in self.cfg.get("owners") or []]

    def group(self):
        """The id of the bound group, or None: the bot then talks to its owner in a private chat, as always."""
        g = group_of(self.cfg)
        return g["id"] if g else None

    def chat(self):
        if self.group() is not None:
            return self.group()
        o = self.owners()
        return o[0] if o else None

    def topic_for(self, reply_to=None, session=None):
        """The topic (message_thread_id) of a message to the group: a reply — the topic of the message it answers; the
        words of a session — its topic (/here), else the topic of its mini-hub; the rest — "General" (None). Never in a
        private chat."""
        if self.group() is None:
            return None
        if reply_to and str(reply_to) in self.st["topic_of"]:
            return self.st["topic_of"][str(reply_to)]
        name = session or self.ev_session
        if not name:
            return None
        bound = {v: int(k) for k, v in self.st["topics"].items()}
        if name in bound:
            return bound[name]
        up = minihub.hub_of(self.reader.state, self.reader.state["sessions"].get(name))
        return bound.get(up) if up else None

    def where(self, reply_to=None, session=None):
        """chat_id of a new message, and its topic in the group (topic_for)."""
        out = {"chat_id": self.chat()}
        t = self.topic_for(reply_to, session)
        if t:
            out["message_thread_id"] = t
        return out

    def topic_param(self, reply_to):
        """{message_thread_id} of the topic a message to reply_to goes to, or {} (a private chat, "General")."""
        t = self.topic_for(reply_to)
        return {"message_thread_id": t} if t else {}

    @staticmethod
    def thread_of(m):
        """{message_thread_id} of an incoming message written in a topic, or {}: an answer to it goes there."""
        return {"message_thread_id": m["message_thread_id"]} if m.get("is_topic_message") and m.get("message_thread_id") else {}

    def to_general(self, send, params):
        """send(**params) of a new message; its topic is gone (deleted, closed) — the same again to "General", the topic
        unbound and that said once."""
        try:
            return send(**params)
        except TgError as e:
            if "message_thread_id" not in params or e.transient or not THREAD_GONE.search(str(e)):
                raise
            self.topic_gone(params.pop("message_thread_id"))
            return send(**params)

    def call(self, method, **params):
        """api.call of a new message (to_general)."""
        return self.to_general(lambda **p: self.api.call(method, **p), params)

    def topic_gone(self, thread):
        self.gone_threads.add(thread)   # the rest of a message already on its way to it goes to General too (send)
        name = self.st["topics"].pop(str(thread), None)
        for k in [k for k, v in self.st["topic_of"].items() if v == thread]:
            del self.st["topic_of"][k]
        if name:
            try:
                self.api.call("sendMessage", chat_id=self.chat(), text=i18n.t("telegram.group.topic_gone", name=name))
            except TgError as e:
                sys.stderr.write(f"telegram: the topic of {name} is gone, the line about it did not go: {e}\n")

    def say_group(self):
        """The core learns whether a group is bound (the fact telegram.group): on a hub nobody sits at (bus mode remote) a
        form then goes to the group's admins as a card instead of being refused. Said when the core's state and the
        settings differ, and again after SAY_GROUP_AGAIN while they still do (the fact may still wait in the mailbox; one
        the core did not take — an older core rejects the type — would be lost for the whole process). A bot that is
        down says nothing: the cards made meanwhile reach the group when it is back (catch_up goes on from its number).
        After the journal is read: against the core's state of now."""
        on = self.group() is not None
        said, at = self._group_said
        if on == bool(self.reader.state.get("telegram_group")) or said == on and self.clock() - at < SAY_GROUP_AGAIN:
            return
        self._group_said = (on, self.clock())
        self.put("telegram.group", None, {"on": on})

    def me(self):
        """The bot's own getMe (its id, can_read_all_group_messages) — once per process."""
        if self._me is None:
            self._me = self.api.call("getMe")
        return self._me

    def owner_chat(self, m):
        """A message from the owner AND in his private chat: one sender is not enough (a group, a channel with the bot is not his chat)."""
        user, chat = (m.get("from") or {}).get("id"), m.get("chat") or {}
        return user in self.owners() and chat.get("type") == "private" and chat.get("id") in self.owners()

    def put(self, type, entity=None, data=None):
        """A fact from the owner's Telegram. In the group it names the person: an admin's words are the owner's
        (tg_by — who), a member's are not (member, owner false) — and a member puts nothing but words."""
        who, extra = self.speaker or {}, {"channel": "telegram", "owner": True}
        if who.get("role") == "member":
            if type != "message.received":
                return
            extra = {"channel": "telegram", "owner": False, "member": {"tg": who["tg"], "name": who["name"]}}
        elif who.get("role") == "admin":
            extra["tg_by"] = {"tg": who["tg"], "name": who["name"]}
        spool.put(self.spool, type, entity, {**(data or {}), **extra}, src="telegram")

    def files_url(self):
        """The address of the "Files" module in the tailnet (written by the tailnet module) — for links to paths; no tailnet — None."""
        st = access.status(self.state_dir)
        return (st.get("urls") or {}).get("files") if st.get("ok") else None

    def web_url(self):
        """The address of the hub page in the tailnet — for links to session tabs; no tailnet — None."""
        st = access.status(self.state_dir)
        return (st.get("urls") or {}).get("web") if st.get("ok") else None

    def send_file(self, path, caption=None):
        """A file — straight into the chat: a video as a player, an image as a photo, the rest as a document (up to 50 MB — the bot limit). A file
        that does not go through at all — a line into the chat with the path. -> the number of its message, or None."""
        try:
            return self.send_file_once(path, caption)
        except TgError as e:
            if not getattr(e, "gave_up", False):
                raise
            self.send(i18n.t("telegram.file_failed", file=caption or os.path.basename(path), error=e, path=path))

    def send_file_once(self, path, caption):
        if not os.path.isfile(path) or self.chat() is None:
            return
        if os.path.getsize(path) > 50 * 1024 * 1024:
            self.send(i18n.t("telegram.file_too_big", path=path))
            return
        video = path.lower().endswith((".mp4", ".mov", ".m4v"))
        caption = (caption or "")[:1000] or None
        if path.lower().endswith((".png", ".jpg", ".jpeg", ".webp")) and os.path.getsize(path) <= 10 * 1024 * 1024:
            # a screenshot — a photo right in the chat, not a file; if not accepted (size, format) — as a document
            # once(): a repeat of the event after a failure does not upload a file that has already gone
            try:
                return self.once(("sendPhoto", path, caption), lambda: self.upload(
                    "sendPhoto", "photo", path, **self.where(), caption=caption))
            except TgError as e:
                if e.transient or getattr(e, "gave_up", False):
                    raise   # network: the photo will be repeated, not sent a second time as a document
        return self.once(("upload", path, caption), lambda: self.upload(
            "sendVideo" if video else "sendDocument", "video" if video else "document", path,
            **self.where(), caption=caption, supports_streaming=True if video else None))

    def send_rich(self, data):
        """A report (.md, .html) with pictures as one rich message (sendRichMessage, telegram.rich): pictures by disk paths
        are uploaded in the same request. Too big or refused — the fallback: text (telegram.fold_long) and the pictures as
        albums, the reason goes to the log. A network failure repeats the event."""
        who = data.get("sender") or i18n.t("telegram.hub")
        src = data["path"]   # the copy in the attachments (send-rich keeps it): the session may have deleted the source
        name = os.path.basename(data.get("source") or src)
        if not rich.is_report(name):   # only .md/.markdown/.html/.htm are read as a report, the rest is a file
            return self.send_file(data["path"], caption=f"{who}: {data.get('caption') or name}")
        try:
            r = rich.build(src, data.get("title"), media=data.get("media"), sender=who,
                           base=os.path.dirname(data["source"]) if data.get("source") else None)
        except (OSError, ValueError) as e:
            sys.stderr.write(f"telegram: rich {name}: not read ({e}), sending as a file\n")  # i18n-ok: operator log
            return self.send_file(data["path"], caption=f"{who}: {data.get('title') or name}")
        why = r.too_big()
        if not why and self.chat() is not None:
            try:
                return self.once(("sendRichMessage", src, data.get("title"), r.text), lambda: self.upload_many(
                    "sendRichMessage", [(mid, p) for mid, p, _ in r.files], **self.where(), rich_message=r.message()))
            except TgError as e:
                if (e.transient or e.retry_after) and not getattr(e, "gave_up", False):
                    raise
                why = str(e)
        if why:
            sys.stderr.write(f"telegram: rich {name}: {why}, fallback: text and pictures as albums\n")  # i18n-ok: operator log
        self.rich_fallback(r)

    def rich_fallback(self, r):
        """Not accepted as one rich message: text as messages, pictures as albums of 10, other files one by one."""
        text, files = rich.fallback(r)
        if text:
            self.send(text, cwd=r.base, fold=True)
        photos = [p for p in files if p.lower().endswith(rich.PHOTOS) and os.path.getsize(p) <= rich.PHOTO_MAX]
        for i in range(0, len(photos), 10):
            part = photos[i:i + 10]
            if len(part) == 1:
                self.send_file(part[0], None)
                continue
            media = [{"type": "photo", "media": f"attach://f{j}"} for j in range(len(part))]
            try:
                self.once(("richAlbum", part), lambda part=part, media=media: self.upload_many(
                    "sendMediaGroup", [(f"f{j}", p) for j, p in enumerate(part)], **self.where(), media=media))
            except TgError as e:
                if (e.transient or e.retry_after) and not getattr(e, "gave_up", False):
                    raise   # the network: the event repeats, what has gone is not sent again (once)
                sys.stderr.write(f"telegram: rich album refused ({e}), one picture at a time\n")  # i18n-ok: operator log
                for p in part:
                    self.send_file(p, None)
        for p in files:
            if p not in photos:
                self.send_file(p, None)

    def rich_on(self):
        """telegram.rich: a `hub send-rich` report as one rich message; off by default (otherwise a file, as send-file)."""
        try:
            return ((self.hub_config() or {}).get("telegram") or {}).get("rich") is True
        except Exception:   # noqa: BLE001 — hub.json is unreadable: as before
            return False

    def upload(self, method, field, path, **params):
        return self.upload_many(method, [(field, path)], **params)

    def upload_many(self, method, files, **params):
        """The file is sent → the number of its message (stored by once()). An upload that fails over the network or with 5xx while the Bot
        API answers a simple call is the file's own trouble (a big file over a slow channel, a proxy cutting big
        bodies): it would fail on every repeat and hold up everything after it, so after UPLOAD_TRIES within one event the file
        is given up on. While the network itself is down — the file just waits."""
        posted = self.st["posted"] if self.sending is not None else {}
        if posted.get("upload_fails", 0) >= UPLOAD_TRIES:   # already given up; the event repeats for the sake of the line about the file
            raise gave_up(posted.get("upload_error") or method)
        try:
            got = self.to_general(lambda **p: self.api.upload_many(method, files, **p), params)
        except TgError as e:
            if not e.transient or e.retry_after or self.sending is None:
                raise
            self.api.call("getMe")   # if that is down too — it is the network, not the file: it raises here, the event will wait
            posted["upload_fails"], posted["upload_error"] = posted.get("upload_fails", 0) + 1, str(e)
            if posted["upload_fails"] >= UPLOAD_TRIES:
                raise gave_up(str(e)) from None
            raise
        return got.get("message_id") if isinstance(got, dict) else None

    def back_summary(self):
        """telegram.back_summary: the summary on return ("while you were away…"); off — do not send."""
        try:
            return ((self.hub_config() or {}).get("telegram") or {}).get("back_summary") is not False
        except Exception:   # noqa: BLE001 — hub.json is unreadable: as before
            return True

    def page_results(self):
        """telegram.page_results: what the core marks for the owner (a file from `hub send-file`, a result with owner_reply) goes to Telegram
        whatever the chain began with, also by his message from the page; off by default (the rule "answer where he asked")."""
        try:
            return ((self.hub_config() or {}).get("telegram") or {}).get("page_results") is True
        except Exception:   # noqa: BLE001 — hub.json is unreadable: as before
            return False

    def fold_long(self):
        """telegram.fold_long: long session words — as a gist and the folded rest (blockquote expandable); off by default."""
        try:
            return ((self.hub_config() or {}).get("telegram") or {}).get("fold_long") is True
        except Exception:   # noqa: BLE001 — hub.json is unreadable: as before
            return False

    def send(self, md, head="", reply_to=None, buttons=None, route=None, cwd=None, fold=False, quiet=False):
        """Send to the owner (Markdown → HTML, long ones in several). route — where a reply to this message goes;
        cwd — the session folder: file paths in its text become links into "Files". fold — session words: a long one with
        telegram.fold_long goes as a gist and the folded rest (never for cards with buttons). quiet — without a sound
        (a notice the owner did not wait for)."""
        chat = self.chat()
        if chat is None:
            return []
        ids = []
        pics = []
        if "```mermaid" in md and not buttons:   # diagrams — as pictures right after; if not drawn — the source
            parts, last = [], 0
            for start, end, code in mermaid.split(md):
                png = self.render_mermaid(code)
                if png:
                    pics.append(png)
                    parts += [md[last:start], i18n.t("telegram.diagram_below", n=len(pics))]
                    last = end
            md = "".join(parts) + md[last:]
        where = self.where(reply_to, (route or {}).get("session"))   # the chat, and in the group its topic
        link = self.linker(cwd, (route or {}).get("session"))   # with cwd: paths into "Files" (a plan, a spec)
        rich_id = None if buttons or not fold else self.rich_result(md, head, where, reply_to, link, quiet)
        # the session's words went as one rich message (telegram.rich_results): no HTML parts
        msgs = [] if rich_id is not None else fmt.to_messages(md, head=head, fold=fold and not buttons and self.fold_long())
        msgs = [link(m) for m in msgs]
        for i, html_text in enumerate(msgs):
            params = {**where, "text": html_text, "parse_mode": "HTML", "disable_web_page_preview": True}
            if quiet:
                params["disable_notification"] = True
            if reply_to and i == 0:
                params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
            if buttons and i == len(msgs) - 1:
                params["reply_markup"] = {"inline_keyboard": buttons}

            def post(params=params, plain=md if len(msgs) == 1 else html_text):
                try:
                    return self.call("sendMessage", **params)["message_id"]
                except TgError as e:
                    if "parse" in str(e).lower() or "entities" in str(e).lower():  # HTML did not pass — as text
                        plain_params = {k: v for k, v in params.items() if k != "parse_mode"}
                        return self.call("sendMessage", **{**plain_params, "text": plain})["message_id"]
                    raise
            # a repeat of the event does not resend parts that have gone (the key without the topic: it may have fallen to General)
            message_id = self.once(("sendMessage", {k: v for k, v in params.items() if k != "message_thread_id"}), post)
            ids.append(message_id)
            if where.get("message_thread_id") in self.gone_threads:   # it went to General: so do the rest and the pictures
                where.pop("message_thread_id")
            if self.group() is not None:   # a reply to it goes into the same topic
                self.st["topic_of"][str(message_id)] = where.get("message_thread_id")
            if buttons and i == len(msgs) - 1:
                # closes — the result will be appended to it (edit_done); if it went as plain text — an HTML edit will not pass, the result goes as a line
                self.st["card_html"][str(message_id)] = html_text
            if route:
                # the heading above the reply (head) — as a line: a reply to it quotes without it (quote)
                self.st["routes"][str(message_id)] = {**route, "head": fmt.plain(head)} if head and not i else route
        if rich_id is not None:   # the same for the one rich message
            ids.append(rich_id)
            if where.get("message_thread_id") in self.gone_threads:
                where.pop("message_thread_id")
            if self.group() is not None:
                self.st["topic_of"][str(rich_id)] = where.get("message_thread_id")
            if route:
                self.st["routes"][str(rich_id)] = {**route, "head": fmt.plain(head)} if head else route
        if ids and fold and self.sending is not None:
            # fold marks a session's own words (on_result sends every result with it; a failure line goes without)
            self.result_sent = True   # joined
        for live in self.st["live"].values() if reply_to and ids else ():
            if live["tg"] == reply_to:
                live["answered"] = True   # he has already been answered on this message — "⏳ delayed" is not needed
        for i, png in enumerate(pics, 1):
            try:
                self.to_general(lambda **p: self.api.upload("sendPhoto", "photo", png, **p), {
                    **where,
                    "caption": i18n.t("telegram.diagram_caption", n=i) if len(pics) > 1 else None,
                    "reply_parameters": {"message_id": ids[-1], "allow_sending_without_reply": True} if ids else None})
            except TgError:
                pass
            finally:
                shutil.rmtree(os.path.dirname(png), ignore_errors=True)
        return ids

    def rich_results_on(self):
        """telegram.rich_results: a session's words (a result) as a rich message — real tables, a gist and the folded rest;
        off by default."""
        try:
            return ((self.hub_config() or {}).get("telegram") or {}).get("rich_results") is True
        except Exception:   # noqa: BLE001 — hub.json is unreadable: as before
            return False

    def rich_result(self, md, head, where, reply_to, link, quiet):
        """The session's words as one sendRichMessage (rich.result: the same chat, topic, reply and links — link — as the
        HTML parts; a long text with telegram.fold_long — the gist and the rest under <details>) → the message number, or
        None: the ordinary HTML path (option off, over the Bot API limits, or Telegram refused it). A network failure or
        a flood wait repeats the event, as for any message."""
        if not self.rich_results_on():
            return None
        try:
            r, why = rich.result(md, head, link, i18n.t("web.updates.more") if self.fold_long() else None)
        except Exception:  # noqa: BLE001 — a text the conversion did not expect: the words go as HTML, not lost
            r, why = None, f"not converted\n{traceback.format_exc()}"
        if why:
            sys.stderr.write(f"telegram: rich result: {why}, sending as HTML\n")  # i18n-ok: operator log
            return None
        params = {**where, "rich_message": r.message()}
        if quiet:
            params["disable_notification"] = True
        if reply_to:
            params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        try:   # a repeat of the event does not send it again (the key without the topic, as for the HTML parts)
            return self.once(("sendRichMessage", {k: v for k, v in params.items() if k != "message_thread_id"}),
                             lambda: self.call("sendRichMessage", **params)["message_id"])
        except TgError as e:
            if e.transient or e.retry_after:
                raise
            sys.stderr.write(f"telegram: rich result: {e}, sending as HTML\n")  # i18n-ok: operator log
            return None

    def linker(self, cwd=None, who=None):
        """html → html with the links of a message to the owner: with cwd (the session folder) paths in `code` and in plain
        text become links into "Files"; a session name in `code` — to its tab; a Jira task number — to the task."""
        files = self.files_url() if cwd else None
        web, names, cfg = self.web_url(), set(self.reader.state["sessions"]), self.hub_config()

        def link(h):
            if cwd:
                h = fmt.link_text(fmt.link_paths(h, files, cwd, session=who), files, cwd, session=who)
            return jira.link_html(fmt.link_sessions(h, web, names), cfg)
        return link

    def linked(self, msgs):
        """The ready HTML parts of a message: a session name in `code` is a link to its tab, a Jira task number is a link."""
        link = self.linker()
        return [link(m) for m in msgs]

    def auto_receipts(self):
        """telegram.receipts: false — no 👀/👍 receipts of the core on the owner's messages (failures and the voice progress stay)."""
        try:
            return ((self.hub_config() or {}).get("telegram") or {}).get("receipts") is not False
        except Exception:   # noqa: BLE001 — hub.json is unreadable: as before
            return True

    def react(self, message_id, emoji, auto=False):
        if auto and not self.auto_receipts():
            return
        if message_id and self.chat() is not None:
            try:
                self.api.call("setMessageReaction", chat_id=self.chat(), message_id=message_id,
                              reaction=[{"type": "emoji", "emoji": emoji}])
            except TgError:
                pass  # a reaction is a receipt; if it did not pass — no trouble

    def token(self):
        """A random id of a card or button. While sending an event — the same on every attempt of that event: a repeat
        after a failure in the middle of the event sends the same buttons, and once() recognizes the card that has already gone."""
        if self.sending is None:
            return secrets.token_hex(4)
        n = self.attempt["#tokens"] = self.attempt.get("#tokens", 0) + 1
        salt = self.st.setdefault("salt", secrets.token_hex(16))   # ids are still impossible to guess
        return hashlib.sha256(f"{salt}:{self.sending}:{n}".encode()).hexdigest()[:8]

    def key(self, payload):
        k = self.token()
        self.st["keys"][k] = payload
        return f"k:{k}"

    # --- incoming ----------------------------------------------------------------------------------------------
    def poll(self, timeout=20):
        try:
            updates = self.api.call("getUpdates", offset=self.st["offset"], timeout=timeout,
                                    allowed_updates=["message", "callback_query", "business_connection",
                                                     "business_message"])
        except TgError as e:
            # do not stay silent and do not spin without a pause (an error was swallowed, the network dropped — polling ran idle)
            self.poll_fails += 1
            pause = e.retry_after or min(30, 2 ** min(self.poll_fails, 5))
            if self.poll_fails == 1 or self.poll_fails % 20 == 0:
                sys.stderr.write(f"telegram: not receiving messages ({self.poll_fails}): {e} — pausing {pause} s\n")
            time.sleep(pause)
            return 0
        if self.poll_fails:
            sys.stderr.write(f"telegram: receiving works again (after {self.poll_fails} failures)\n")
            self.poll_fails = 0
        for u in updates:
            self.st["offset"] = u["update_id"] + 1
            self.follow_chat()   # a new chat (pair, group off) — before an update meets the old chat's numbers
            try:
                if "message" in u:
                    try:
                        self.on_message(u["message"])
                    except Exception as err:  # noqa: BLE001 — the owner's message is not lost on any error
                        self.lost(u["message"], err)
                elif "callback_query" in u:
                    try:
                        self.on_callback(u["callback_query"])
                    except Exception as err:  # noqa: BLE001 — serve() catches nothing: one press must not stop the bot
                        sys.stderr.write(f"telegram: a button press failed: {err!r}\n")
                elif ("business_connection" in u or "business_message" in u) and self.group() is None:
                    try:   # the owner's chats with people (modules/telegram/business.py)
                        if "business_connection" in u:
                            self.business.on_connection(u["business_connection"])
                        else:
                            self.business.on_message(u["business_message"])
                    except Exception as err:  # noqa: BLE001 — one message must not stop the bot
                        sys.stderr.write(f"telegram: a business update failed: {type(err).__name__}: {err}\n")
            finally:
                self.save()
        return len(updates)

    # --- the group: who is who (spec d2513429) ----------------------------------------------------------------------
    @staticmethod
    def content(m):
        """Words or a file of a person — not a service message of the group (joined, left, the title, a pin)."""
        return any(m.get(k) for k in ("text", "caption", "photo", "document", "voice", "audio", "video_note", "video"))

    def admin_rows(self, chat):
        """(the human admins {user id: name}, whether the bot itself may appoint admins, whether the bot is an admin)
        of a group now, or None if Telegram did not answer. The creator is an admin too; other bots are not people."""
        try:
            got = self.api.call("getChatAdministrators", chat_id=chat)
            me = self.me().get("id")
        except TgError as e:
            sys.stderr.write(f"telegram: the admins of the group are unknown: {e}\n")
            return None
        people, promote, bot_admin = {}, False, False
        for row in got if isinstance(got, list) else []:
            u = row.get("user") or {}
            if me is not None and u.get("id") == me:
                bot_admin, promote = True, row.get("can_promote_members") is True
            elif not u.get("is_bot") and row.get("status") in ("creator", "administrator") and isinstance(u.get("id"), int):
                people[u["id"]] = display_name(u)
        return people, promote, bot_admin

    def admins(self):
        """The human admins of the bound group {id: name} — fresh on every word and press, no cache: one who lost the
        right loses it with his next action. None — Telegram did not answer: nobody is an admin then. The bot that may
        appoint admins makes nobody an admin either (a session could read its token and appoint anyone)."""
        rows = self.admin_rows(self.group())
        if rows is None:
            return None
        people, promote, _ = rows
        if promote:
            if not self.st.get("promote_told"):
                self.st["promote_told"] = True
                self.tell_group(i18n.t("telegram.group.bot_promotes"))
            return {}
        self.st.pop("promote_told", None)
        now = {str(k): v for k, v in people.items()}
        was = self.st.get("admins")
        self.st["admins"] = now
        if was is not None and set(was) != set(now):   # who runs the hub changed: everyone in the group sees it
            added = [now[k] for k in now if k not in was]
            gone = [was[k] for k in was if k not in now]
            self.tell_group(i18n.t("telegram.group.admins_now", names=", ".join(now.values()) or "—")
                            + (" " + i18n.t("telegram.group.admins_added", names=", ".join(added)) if added else "")
                            + (" " + i18n.t("telegram.group.admins_gone", names=", ".join(gone)) if gone else ""))
        return people

    def tell_group(self, text):
        """A line of the bot itself into "General" of the group (no event: said at once, a failure only logged)."""
        try:
            self.api.call("sendMessage", chat_id=self.chat(), text=text)
        except TgError as e:
            sys.stderr.write(f"telegram: a line to the group did not go: {e}\n")

    def person(self, m):
        """Who wrote m, for the bot: {role: owner | admin | member, tg, name}, or None — the bot does not listen (a
        stranger, another chat, a bot, a channel). In the group the role is checked fresh; an anonymous admin and a check
        that failed are told in the chat."""
        if self.group() is None:
            return {"role": "owner"} if self.owner_chat(m) else None
        frm, chat = m.get("from") or {}, m.get("chat") or {}
        if chat.get("id") != self.group():
            return None
        if m.get("sender_chat"):   # on behalf of a chat: the group itself — an anonymous admin; a channel — nobody
            if m["sender_chat"].get("id") == self.group():
                self.say_quietly(i18n.t("telegram.group.anonymous"), m)
            return None
        if frm.get("is_bot") or m.get("is_automatic_forward") or not isinstance(frm.get("id"), int):
            return None
        who = {"tg": frm["id"], "name": display_name(frm)}
        admins = self.admins()
        if admins is None:   # not the old list: words confirm cards too
            self.say_quietly(i18n.t("telegram.group.check_failed"), m)
            return {**who, "role": "member"}
        return {**who, "role": "admin" if frm["id"] in admins else "member"}

    def say_quietly(self, text, m):
        """A line of the bot under m; its failure is only logged — the words of m must still reach the hub."""
        try:
            self.send(text, reply_to=m.get("message_id"))
        except TgError as e:
            sys.stderr.write(f"telegram: a line under message {m.get('message_id')} did not go: {e}\n")

    def addressed(self, text):
        """/start or /start@<this bot> — not a command to another bot of the group."""
        head = text.split()[0] if text.split() else ""
        if "@" not in head:
            return True
        try:
            return head.split("@", 1)[1].lower() == str(self.me().get("username") or "").lower()
        except TgError:
            return False

    def offer_group(self, m):
        """/start in a group from its admin: a code to bind the bot to this group (`hub-telegram pair <code>` in the hub's
        terminal). Only to an admin and only where the bot can work: it sees every message (an admin of the group, or its
        privacy mode is off) and it may not appoint admins — otherwise a line on what to fix. The limits of a private
        code (PAIR_MAX, PAIR_TTL); one code per person and group. Once the bot has an owner (a private chat or a group),
        the admin of another group gets no code — its admins would get the owner's rights by a code and a command a
        session could run: a card goes to the owner's chat instead, and his press binds (group_press). A stranger's
        request then waits for a free place — no more than PAIR_MAX cards in PAIR_TTL; the owner's own (or a bound
        group admin's) takes the oldest place (oldest_out)."""
        chat, who = m["chat"], m.get("from") or {}
        if chat["id"] == self.group() or not isinstance(who.get("id"), int):
            return
        rows = self.admin_rows(chat["id"])
        if rows is None or who["id"] not in rows[0]:
            return
        _, promote, bot_admin = rows
        say = lambda text: self.api.call("sendMessage", chat_id=chat["id"], text=text, **self.thread_of(m))  # noqa: E731
        if promote:
            return say(i18n.t("telegram.group.fix_promote"))
        if not (bot_admin or self.me().get("can_read_all_group_messages")):
            return say(i18n.t("telegram.group.fix_privacy"))
        cfg, now = load_config(), self.clock()   # not into self.cfg: the serve loop reloads it (and follow_chat follows)
        pending = live_codes(cfg, now)
        bound = bool(cfg.get("owners")) or group_of(cfg) is not None
        code = next((c for c, v in pending.items() if v.get("user") == who["id"] and v.get("chat") == chat["id"]), None)
        new = code is None
        if new:
            if len(pending) >= PAIR_MAX and bound and not self.trusted(who["id"]):
                return
            oldest_out(pending)
            while code is None or code in pending:
                code = f"{secrets.randbelow(10**6):06d}"
            title = "".join(c for c in str(chat.get("title") or "") if c.isprintable())[:80]
            pending[code] = {"user": who["id"], "name": display_name(who), "at": now, "chat": chat["id"], "title": title}
        cfg["pending"] = pending
        save_config(cfg)
        if not bound:
            return say(i18n.t("telegram.group.code", code=code, minutes=PAIR_TTL // 60))
        if new:
            try:
                self.ask_group(code, pending[code], rows[0])
            except TgError as e:
                sys.stderr.write(f"telegram: a group's request did not reach the owner: {e}\n")
                if e.unsent or not e.transient:   # certainly not delivered: the place is free, the next /start asks again
                    cfg = load_config()   # read again: the terminal could have written it during the call
                    drop_code(cfg, code, self.clock())
                    save_config(cfg)
                return say(i18n.t("telegram.group.ask_failed"))
        say(i18n.t("telegram.group.asked_owner"))

    def trusted(self, user):
        """The owner of a private chat or an admin of the bound group: his request for a group may take a place."""
        if user in self.owners():
            return True
        admins = self.admins() if self.group() is not None else None
        return bool(admins) and user in admins

    def ask_group(self, code, entry, people):
        """The card in the owner's chat: who asks to move the bot into which group and who gets his rights there. Plain
        text, no markup: the group's title and the names are the strangers' words."""
        rows = [[{"text": i18n.t("telegram.group.bind_yes"),
                  "callback_data": self.key({"do": "group_bind", "code": code, "yes": True})},
                 {"text": i18n.t("telegram.group.bind_no"),
                  "callback_data": self.key({"do": "group_bind", "code": code, "yes": False})}]]
        text = i18n.t("telegram.group.bind_ask", name=entry.get("name") or "—", title=entry.get("title") or entry["chat"],
                      admins=", ".join(people.values()) or "—", minutes=PAIR_TTL // 60)
        self.api.call("sendMessage", chat_id=self.chat(), text=text, reply_markup={"inline_keyboard": rows})

    def unbutton(self, msg):
        """The buttons off a message of the bot (pressed, or too old to press)."""
        try:
            self.api.call("editMessageReplyMarkup", chat_id=self.chat(), message_id=msg, reply_markup={"inline_keyboard": []})
        except TgError:
            pass

    def group_press(self, payload, cq):
        """"Move the bot there" / "Keep it here" on ask_group's card: the owner's word (on_callback let only him or the
        bound group's admin press). Yes binds as `hub-telegram pair` does after its question (bind_group: the admins are
        checked again, nothing is written without them); either way the code is gone and the card loses its buttons."""
        msg = (cq.get("message") or {}).get("message_id")
        cfg = load_config()
        entry = live_codes(cfg, self.clock()).get(str(payload.get("code") or ""))
        self.unbutton(msg)   # before the move: the card is in this chat
        if not entry or not entry.get("chat"):
            return i18n.t("telegram.cb_stale")
        if not payload.get("yes"):
            drop_code(cfg, payload["code"], self.clock())
            save_config(cfg)
            return i18n.t("telegram.group.bind_kept")
        try:
            facts = group_facts(self.api, entry["chat"])
        except NoBind as e:
            return str(e)[:190]
        bind_group(self.api, payload["code"], entry, facts)
        self.cfg = load_config()
        return i18n.t("telegram.group.pair_done", title=entry.get("title") or entry["chat"])[:190]

    def here_cmd(self, arg, m):
        """/here <session> in a topic of the group: the topic's words go to that session, its results, questions and
        pictures come here; /here — what it is bound to and who is who; /here off — unbound. "General" is always the
        hub. A topic is a long-lived session's: an agent or a direction (and the hub) is bound at once; any other one
        (an executor sleeps when it hands in, a plain session) gets "Make an agent" / "Make a direction" / "Cancel" —
        an admin's press makes it one through the core and binds the topic when the core has taken it (here_press)."""
        thread = m.get("message_thread_id") if m.get("is_topic_message") else None
        say = lambda text, **kw: self.send(text, reply_to=m["message_id"], **kw)  # noqa: E731
        topics, key = self.st["topics"], str(thread)
        if not arg:
            if not thread:
                head = i18n.t("telegram.group.here_general")
            elif key in topics:
                head = i18n.t("telegram.group.here_is_role", name=topics[key], role=self.role_word(topics[key]))
            else:
                head = i18n.t("telegram.group.here_none")
            return say("\n\n".join(x for x in (head, self.who_is_who()) if x))
        if not thread:
            return say(i18n.t("telegram.group.here_general"))
        if arg == "off":
            topics.pop(key, None)
            return say(i18n.t("telegram.group.here_off"))
        st = self.reader.state
        name = identity.address(st, arg)
        sess = st["sessions"].get(name or "")
        if not sess or sess["state"] in ("removed", "closed", "failed"):
            return say(i18n.t("telegram.group.here_unknown", name=arg))
        if not self.long_lived(sess):
            offer = secrets.token_hex(4)
            self.st["here_offers"][offer] = {"session": name, "thread": thread, "ts": self.clock()}
            rows = [[{"text": i18n.t(label), "callback_data": self.key({"do": "here", "offer": offer, "as": role})}]
                    for label, role in (("telegram.group.here_make_agent", "agent"),
                                        ("telegram.group.here_make_direction", "direction"),
                                        ("telegram.group.here_cancel", None))]
            return say(i18n.t("telegram.group.here_offer", name=name, role=self.role_word(name)), buttons=rows)
        self.bind_topic(key, name)
        say(i18n.t("telegram.group.here_set", name=name))

    @staticmethod
    def long_lived(sess):
        """A topic's session: a specialist or a direction (minihub.lead), the hub, the liaison — not an executor or a
        plain session."""
        return minihub.lead(sess) or sess.get("kind") == "hub" or bool(sess.get("liaison"))

    def bind_topic(self, key, name):
        topics = self.st["topics"]
        for k in [k for k, v in topics.items() if v == name]:   # one topic per session
            del topics[k]
        topics[key] = name

    def role_word(self, name):
        """Who the session is, in a word: the hub, an agent, a direction, an executor, a session (+ its icon)."""
        sess = self.reader.state["sessions"].get(name) or {}
        if sess.get("kind") == "hub":
            return i18n.t("telegram.hub")
        key = "telegram.group.role_direction" if sess.get("backlog") else "telegram.group.role_agent" \
            if sess.get("role") == "agent" else "telegram.group.role_executor" if minihub.runner(sess) \
            else "telegram.group.role_session"
        icon = sess.get("own_icon") or sess.get("icon")
        return i18n.t(key) + (f" {icon}" if icon else "")

    def topic_link(self, thread):
        """A link to a topic of the bound supergroup (t.me/c/<id>/<topic>: its members open it), or None."""
        chat = str(self.group() or "")
        return f"https://t.me/c/{chat[4:]}/{thread}" if chat.startswith("-100") and chat[4:].isdigit() else None

    def who_is_who(self):
        """/here: the sessions a topic may take, who each is, and which topic each has — the asleep executors aside."""
        st, bound = self.reader.state, {v: k for k, v in self.st["topics"].items()}
        names = [n for n, s in sorted(st["sessions"].items()) if s.get("kind") != "hub" and (n in bound or (
            s["state"] not in ("removed", "closed", "failed") and not (minihub.runner(s) and views.shown(s["state"]) == "asleep")))]
        if not names:
            return ""
        lines = []
        for n in names[:HERE_LIST]:
            line = f"- `{n}` — {self.role_word(n)}"
            if n in bound:
                link = self.topic_link(bound[n])
                topic = i18n.t("telegram.group.here_topic")
                line += f" · [{topic}]({link})" if link else f" · {topic}"
            lines.append(line)
        if len(names) > HERE_LIST:
            lines.append(i18n.t("telegram.group.here_more", count=len(names) - HERE_LIST))
        return i18n.t("telegram.group.here_list") + "\n" + "\n".join(lines)

    def here_press(self, payload, cq):
        """A press on /here's offer: make the session an agent or a direction — the same facts as `hub session role
        <name> agent` and `hub direction new <name> --session <name>`, so a running turn goes on and the session becomes
        a mini-hub after it — and bind the topic once the core has taken the fact (here_done). The card loses its buttons."""
        offer = self.st["here_offers"].get(payload.get("offer") or "")
        msg = (cq.get("message") or {}).get("message_id")
        if not offer or offer.get("used") or self.clock() - offer.get("ts", 0) > HERE_OFFER_TTL:
            self.unbutton(msg)   # an old card: no buttons that do nothing
            return i18n.t("telegram.cb_stale")
        name, role = offer["session"], payload.get("as")
        st = self.reader.state
        sess = st["sessions"].get(name)
        if role is not None and (not sess or sess["state"] in ("removed", "closed", "failed")):
            return i18n.t("telegram.group.here_gone", name=name)
        slug = None
        if role == "direction" and not self.long_lived(sess):
            slug, why = self.make_direction(name, sess)
            if why:   # the buttons stay: "Make an agent" may still do
                self.send(why, reply_to=msg)
                return i18n.t("telegram.group.here_not_made")
        offer["used"] = True
        self.unbutton(msg)
        if role is None:
            return i18n.t("telegram.group.here_cancelled")
        if self.long_lived(sess):   # made an agent or a direction meanwhile (the hub, the page): only the topic is left
            self.bind_topic(str(offer["thread"]), name)
            self.send(i18n.t("telegram.group.here_made", name=name, role=self.role_word(name)), route={"session": name})
            return i18n.t("telegram.cb_done")
        if role == "agent":
            last = max((d for d in st.get("delegations", {}).values() if d.get("session") == name),
                       key=delegations.born, default=None)
            about = sess.get("about") or (sess.get("gist") or {}).get("text") or (last or {}).get("words") or name
            fact = ("session.role_set", name, {"role": "agent", "about": reducer.one_line(about, 120, word=True)})
        else:
            fact = ("task.backlog", None, {"slug": slug, "session": name})
        req = secrets.token_hex(6)
        self.st["binding"][req] = {"session": name, "thread": offer["thread"], "msg": msg, "as": role}
        self.put(fact[0], fact[1], {**fact[2], "req": req})
        return i18n.t("telegram.cb_accepted")

    def unbutton(self, msg):
        """The buttons off a message of the bot (pressed, or too old to press)."""
        try:
            self.api.call("editMessageReplyMarkup", chat_id=self.chat(), message_id=msg, reply_markup={"inline_keyboard": []})
        except TgError:
            pass

    def make_direction(self, name, sess):
        """The direction's backlog named like the session, in the repo it works in (or a git folder of its own) — what
        `hub direction new` does before its fact; one that is there already is taken unless another session leads it
        or waits for it. (its name, None) or (None, why not — in the owner's words)."""
        from modules.directions import direction
        try:
            path = direction.path_of(direction.check_slug(name))
            if not os.path.isdir(os.path.join(path, "backlog")):
                direction.new(name, direction.repo_of(sess))
                return name, None
            slugs = direction.names_of(name)
        except direction.Fail as e:
            return None, str(e)
        except (OSError, ValueError) as e:   # the disk, an unreadable hub.json (direction.root): a line, not a broken press
            return None, f"{type(e).__name__}: {e}"[:300]
        st = self.reader.state
        lead = next((n for n, s in st["sessions"].items()
                     if n != name and s.get("backlog") in slugs and s["state"] != "removed"), None)
        lead = lead or next((n for n, v in (st.get("pending_backlogs") or {}).items()   # the hub gave it to one not up yet
                             if n != name and (v.get("slug") if isinstance(v, dict) else n) in slugs), None)
        if lead:
            return None, i18n.t("telegram.group.here_direction_taken", slug=slugs[0], name=lead)
        return os.path.basename(path), None   # a renamed direction: the name its folder has now, as the CLI sends

    def here_done(self, ev, why):
        """The core took (or refused) the fact of a /here press: the topic is bound — one line in it — or the reason."""
        req = (ev.get("data") or {}).get("req") or ""
        bind = self.st["binding"].get(req)
        if bind is None:
            return False
        if why:
            self.send(i18n.t("telegram.group.here_refused", name=bind["session"], why=coretext.human(why)),
                      reply_to=bind["msg"])
        else:
            self.bind_topic(str(bind["thread"]), bind["session"])
            self.send(i18n.t("telegram.group.here_made", name=bind["session"], role=self.role_word(bind["session"])),
                      route={"session": bind["session"]})
        self.st["binding"].pop(req)   # after the line went: a failed send repeats the event, and the line with it
        return True

    def migrated(self, new):
        """The group became a supergroup (topics turned on): a new chat id; the bot follows it (follow_chat)."""
        cfg = load_config()
        g = group_of(cfg)
        if not g or g["id"] != self.group() or not isinstance(new, int) or isinstance(new, bool):
            return
        cfg["group"] = {**g, "id": new}
        save_config(cfg)
        self.cfg = cfg
        self.st["migrated"] = new

    def follow_chat(self):
        """The chat changed (pair into a group, group off, a supergroup, a direct edit of the settings file): the old
        chat's message numbers mean nothing in the new one — what the bot kept by them goes, the open cards are sent to
        the new chat again, and the old chat is told where the bot went (the bot does it, not `pair`: a direct edit of the
        file is seen too). Only when a group is involved — a private chat stays as it was."""
        now, was = self.chat(), self.st.get("chat")
        if now == was:
            return
        self.st["chat"] = now
        if was is None or not ((was or 0) < 0 or (now or 0) < 0):
            return
        migrated = self.st.pop("migrated", None) == now
        if not migrated:
            title = (group_of(self.cfg) or {}).get("title") or ""
            try:
                self.api.call("sendMessage", chat_id=was, text=i18n.t("telegram.group.moved", title=title)
                              if self.group() is not None else i18n.t("telegram.group.unbound"))
            except TgError as e:
                sys.stderr.write(f"telegram: the old chat was not told where the bot went: {e}\n")
        for k in ("routes", "owner_msgs", "cards", "threads", "quoted", "live", "notices", "card_html", "topic_of",
                  "topics", "keys", "asks", "bus_msgs", "here_offers", "binding"):
            if k in self.st:
                self.st[k] = {}
        self.st["told"] = []
        self.st.pop("admins", None)
        self.typing_at = {}
        if now is None:
            return
        if migrated:
            self.tell_group(i18n.t("telegram.group.migrated"))
        if self.quiet():
            return
        st = self.reader.state
        for name, sess in st["sessions"].items():   # the cards the core still waits on — in the new chat
            if sess.get("state") == "removed" or minihub.boss(st, sess):
                continue
            for req in (sess.get("pending") or []) + owner.open_asks(sess, self.clock()):
                if req.get("tool"):
                    try:
                        self.on_ask(name, req)
                    except TgError as e:
                        sys.stderr.write(f"telegram: a card of {name} did not go to the new chat: {e}\n")

    def joined(self, st, ev, name):
        """In the group one turn may carry several people's words (the core typed them into the running turn —
        injected); its one result went under the message the turn began with — the others get 👍 and a line that the
        answer is above."""
        marker = self.st["turn"].get(name)
        for key, live in self.st["live"].items():
            msg = st["messages"].get(key) or {}
            if key == marker or live.get("answered") or not msg.get("injected") or msg.get("session") != name \
                    or (msg.get("delivered_at") or 0) > ev["ts"]:
                continue
            live["answered"] = True
            self.react(live["tg"], "👍", auto=True)
            self.send(i18n.t("telegram.group.answer_above"), reply_to=live["tg"], route={"session": name}, quiet=True)

    def offer_pairing(self, m):
        """/start in a private chat while the owner is not bound: a code for `hub-telegram pair`. One code per person (a repeated /start
        — the same one), not overwritten, no more than PAIR_MAX open ones, each lives PAIR_TTL. The file is read fresh and
        written whole: it is written by the hub-telegram commands too."""
        cfg, now, who = load_config(), self.clock(), m["from"]
        self.cfg = cfg
        if cfg.get("owners"):
            return
        pending = live_codes(cfg, now)
        code = next((c for c, v in pending.items() if v.get("user") == who["id"] and not v.get("chat")), None)
        if code is None:
            oldest_out(pending)   # five strangers' /start do not lock the owner out for ten minutes
            while code is None or code in pending:
                code = f"{secrets.randbelow(10**6):06d}"
            pending[code] = {"user": who["id"], "name": display_name(who), "at": now}
        cfg["pending"] = pending
        save_config(cfg)
        self.api.call("sendMessage", chat_id=m["chat"]["id"],
                      text=i18n.t("telegram.pair_code_sent", code=code, minutes=PAIR_TTL // 60))

    def lost(self, m, err):
        """The bot could not parse the owner's message — hand the text to the hub anyway: the update number
        is already advanced, without this the message would be lost forever."""
        print(f"telegram: could not parse message {m.get('message_id')}: {type(err).__name__}", file=sys.stderr, flush=True)
        seen = getattr(self.local, "seen", None)   # who wrote it, as on_message found (a group: no second check)
        mine = bool(seen) and seen[:2] == ((m.get("chat") or {}).get("id"), m.get("message_id"))
        who = seen[2] if mine and seen[2] else ({"role": "owner"} if self.owner_chat(m) else None)
        frm = m.get("from") or {}
        if who is None and mine and self.group() is not None and (m.get("chat") or {}).get("id") == self.group() \
                and isinstance(frm.get("id"), int) and not frm.get("is_bot"):
            who = {"role": "member", "tg": frm["id"], "name": display_name(frm)}   # the check itself broke: no rights
        if who is None:
            return
        self.speaker = who
        try:
            self.lost_words(m, err, who)
        finally:
            self.speaker = None

    def lost_words(self, m, err, who):
        text = (m.get("text") or m.get("caption") or "").strip() or "(message without text)"
        if hubs.secret(text):   # a link of the hub bus is a secret: never to the hub as words, he pastes it again
            self.hubs.send(hubs.esc(i18n.t("telegram.bus.lost", error=type(err).__name__)), reply_to=m.get("message_id"))
            return
        if (self.st["routes"].get(str((m.get("reply_to_message") or {}).get("message_id"))) or {}).get("bus") \
                and not (m.get("voice") or m.get("audio") or m.get("video_note")):
            # words for the other hub's owner: not the hub's to read
            self.hubs.send(hubs.esc(i18n.t("telegram.bus.not_sent", why=type(err).__name__)), reply_to=m.get("message_id"))
            return
        self.put("message.received", None, {"text": f"{text}\n\n(the Telegram bot could not parse it fully: {type(err).__name__})",
                                            "tg": {"chat": m["chat"]["id"], "id": m["message_id"]}})

    def on_message(self, m):
        text = (m.get("text") or m.get("caption") or "").strip()
        chat = m.get("chat") or {}
        if self.group() is not None and chat.get("id") == self.group():
            if m.get("migrate_to_chat_id"):
                return self.migrated(m["migrate_to_chat_id"])
            if not self.content(m):
                return   # a service message of the group (someone joined, the title, a pin): no words for the hub
            # the topic it was written in: answers to it go there
            self.st["topic_of"][str(m["message_id"])] = m.get("message_thread_id") if m.get("is_topic_message") else None
        self.local.seen = (chat.get("id"), m.get("message_id"), None)   # who: not known yet (lost)
        who = self.person(m)
        if who is None:
            if text.startswith("/start") and chat.get("type") in ("group", "supergroup") and self.addressed(text):
                self.offer_group(m)
            elif not self.owners() and text.startswith("/start") and chat.get("type") == "private":
                self.offer_pairing(m)
            return  # the bot does not answer strangers
        self.local.seen = ((m.get("chat") or {}).get("id"), m.get("message_id"), who)
        self.speaker = who
        try:
            return self.words(m, text, who)
        finally:
            self.speaker = None

    def words(self, m, text, who):
        member = who["role"] == "member"
        if text.startswith("/start"):
            if not member:
                self.api.call("sendMessage", chat_id=m["chat"]["id"], text=i18n.t("telegram.start_hello"), **self.thread_of(m))
            return
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        if self.group() is not None and (cmd in COMMANDS or cmd == "/here") and not (m.get("photo") or m.get("document")):
            if member and cmd not in MEMBER_COMMANDS:
                return self.send(i18n.t("telegram.group.admins_only"), reply_to=m["message_id"])
            if cmd == "/here":
                return self.here_cmd(arg.strip(), m)
            if cmd == "/link":   # a link of the bus is a secret: every member of the group would read it
                return self.send(i18n.t("telegram.group.no_link"), reply_to=m["message_id"])
        if cmd in COMMANDS and not (m.get("photo") or m.get("document")):   # the bot answers by itself, without a model
            return self.command(cmd, arg.strip(), m)
        route = self.st["routes"].get(str((m.get("reply_to_message") or {}).get("message_id")))
        if route and route.get("bus") and not (m.get("voice") or m.get("audio") or m.get("video_note")):
            if member:   # words to the other hub's owner are the owner's words
                return self.send(i18n.t("telegram.group.admins_only"), reply_to=m["message_id"])
            return self.hubs.reply(m, route, text)   # a reply to the other hub's owner: to him, not to the hub (no turn)
        if member and hubs.secret(text):   # a link of the bus is the owner's to use, and never words for the hub
            return self.send(i18n.t("telegram.group.no_member_link"), reply_to=m["message_id"])
        if self.hubs.pasted(m, text):   # a link of the hub bus: a preview card, never the hub
            return
        media =m.get("voice") or m.get("audio") or m.get("video_note")
        if media:
            cfg = self.hub_config()
            if not voice.enabled(cfg, state=self.state_dir):
                self.send(i18n.t("telegram.voice_off"), reply_to=m["message_id"])
                return
            # the "Voice" module: 👀 at once, transcription in the background (usually seconds, with retries — minutes), the text goes to the queue
            self.react(m["message_id"], "👀")
            # and to the state file — one entry with the update number (save in poll): a bot restart in the middle of transcription
            # does not lose it, catch_up transcribes anew (Telegram will not give the message again, it is not yet in the journal)
            self.st["transcribe"][f"{m['chat']['id']}:{m['message_id']}"] = m
            self.start_voice(m, media, cfg)
            return
        files, failed = self.attachments(m)
        if failed and not text and not files:   # neither words nor a file — the session has nothing to read: the reason goes only to him
            return self.attach_failed_reply(m, failed, recorded=False)
        if not text and not files:   # a sticker, a location, a poll… — answer, do not stay silent
            self.send(i18n.t("telegram.not_understood"), reply_to=m["message_id"])
            return
        route = self.st["routes"].get(str((m.get("reply_to_message") or {}).get("message_id")))
        remark = self.to_a_person(m)
        if route and route.get("ask") and not member and not remark:  # a reply to a session question — one's own answer option ("Other")
            if text and self.answer(route["ask"], route.get("q", 0), text, words=True):
                self.react(m["message_id"], "👌")
                if failed:   # the answer went in words — still tell about the file that did not arrive
                    self.attach_failed_reply(m, failed)
                return
            # the question is already closed (answered by a button or on the page) — this is just a message to that session: before, it silently
            # disappeared (a message did not reach either the journal or the session)
        data = {"text": text or "(attachments)", "attachments": files, "tg": {"chat": m["chat"]["id"], "id": m["message_id"]}}
        if remark:   # words to a person of the group: the hub reads them with its next turn, they start none
            data["fyi"] = True
        if failed:   # not downloaded — to the hub as a field, not into the owner's words
            data["attach_failed"] = failed
        quote = self.quote(m, route)
        if quote:
            data["quote"] = quote
        if route and route.get("session"):
            data["to"] = route["session"]
            if route.get("delegation"):
                data["reply_to"] = route["delegation"]
            if route.get("spec"):   # a reply to a spec's card (spec_card)
                data["spec_card"] = route["spec"]
            up = minihub.hub_of(self.reader.state, self.reader.state["sessions"].get(route["session"])) if member else None
            if up:   # an executor of a mini-hub hears the owner only through the mini-hub: a member's words go there too
                data["to"] = up
                data.pop("reply_to", None)
        elif self.group() is not None and m.get("is_topic_message"):
            bound = self.st["topics"].get(str(m.get("message_thread_id")))
            if bound:   # the topic's session (/here)
                data["to"] = bound
        target = self.reader.state["sessions"].get(data.get("to") or "")
        if member and data.get("to") and (target is None or target["state"] in minihub.GONE):
            # the core passes the owner's words for a session that is gone to the hub, not a member's: the bot does
            data.pop("to")
            data.pop("reply_to", None)
        self.put("message.received", None, data)
        if failed:
            self.attach_failed_reply(m, failed)

    def to_a_person(self, m):
        """Words in the group that begin with a mention of a person (@name, or a name picked from the list) and not of
        this bot: people talk to each other there, and that is no task for the hub — no turn, no "typing", no "⏳"."""
        if self.group() is None:
            return False
        text = m.get("text") is not None
        raw = str((m.get("text") if text else m.get("caption")) or "")
        start = len(raw) - len(raw.lstrip())   # whitespace is one UTF-16 unit, as Telegram counts the offset
        for e in (m.get("entities") if text else m.get("caption_entities")) or []:
            if not isinstance(e, dict) or e.get("offset") != start:
                continue
            if e.get("type") == "text_mention":
                return not (e.get("user") or {}).get("is_bot")
            if e.get("type") == "mention" and isinstance(e.get("length"), int):
                try:
                    me = str(self.me().get("username") or "")
                except TgError:   # unknown: a mention of the bot is rare, a turn the hub takes for a remark is not
                    me = ""
                return raw[start + 1:start + e["length"]].lower() != me.lower()
        return False

    def attach_failed_reply(self, m, failed, recorded=True):
        """Why an attachment was not downloaded (size, Telegram error, network) — to the owner as a reply to his message. After
        the event is recorded: a failure of the reply does not lose his words."""
        try:
            self.send(i18n.t("telegram.attach_failed", reasons="; ".join(f["reason"] for f in failed)),
                      reply_to=m["message_id"])
        except Exception as e:   # noqa: BLE001 — the words are already recorded: a failure of the reply must not lead to lost() and a duplicate
            if not recorded:
                raise   # nothing is recorded — let lost() keep the message
            print(f"telegram: reply about attachment of message {m.get('message_id')} not sent: {e!r}",
                  file=sys.stderr, flush=True)
            if not isinstance(e, (TgError, OSError)):
                traceback.print_exc()

    @staticmethod
    def quote(m, route):
        """What the owner replied to: the selected fragment, otherwise the message text without the "<session>:" signature."""
        if (m.get("quote") or {}).get("text"):
            return hubs.mask(m["quote"]["text"])
        if (m.get("quote") or {}).get("rich_texts"):   # a fragment of a rich message (telegram.rich_results)
            return hubs.mask(rich.plain(m["quote"]["rich_texts"]))
        orig = m.get("reply_to_message") or {}
        # a bus link quoted goes to the hub hidden; a rich message has no text, only its blocks
        text = hubs.mask(orig.get("text") or orig.get("caption") or rich.plain(orig.get("rich_message")))
        name = (route or {}).get("session")
        first, _, rest = text.partition("\n")
        # the heading the bot wrote itself (the route's head field), without it — its forms (core/forms.py)
        heads = {route["head"]} if isinstance((route or {}).get("head"), str) and route["head"] else \
            {h.format(name=name) for h in forms.REPLY_HEADS}
        if name and first.strip() in heads:
            text = rest
        return text.strip()

    def register_commands(self):
        """The command menu in Telegram (setMyCommands) — at startup; if it fails, the commands work anyway."""
        try:
            self.api.call("setMyCommands", commands=[
                {"command": c[1:], "description": d or i18n.t(CATALOG_DESC[c])} for c, d in COMMANDS.items()])
        except TgError as e:
            sys.stderr.write(f"telegram: the command menu was not saved: {e}\n")

    def command(self, cmd, arg, m):
        """/status, /away [hours], /back — without a model: the answer from the core state; "away" and "back" are the same facts as
        `hub away` / `hub back` (the core decides, the bot only sends an event; the confirmation comes when the core has recorded it)."""
        st, tg = self.reader.state, {"chat": m["chat"]["id"], "id": m["message_id"]}
        if cmd == "/status":
            return self.send(self.status_text(), reply_to=m["message_id"])
        if cmd == "/stop":
            return self.stop_list(m)
        if cmd == "/spec":
            return self.spec_cmd(arg, m)
        if cmd == "/link":
            return self.hubs.link_cmd(m, arg)
        if cmd == "/page":
            return self.send(self.page_text(), reply_to=m["message_id"])
        if cmd == "/back":
            if (st.get("owner") or {}).get("state") != "away":
                return self.send(i18n.t("telegram.back_not_away"), reply_to=m["message_id"])
            return self.put("owner.back", None, {"tg": tg})
        words = (arg or "").split()
        # /away deputies [<person> …]: the questions of the deputies' areas go to them (core/deputies.py, d2679763)
        at = next((i for i, w in enumerate(words) if w.casefold() == "deputies"), None)
        flag = (words[at + 1:] or True) if at is not None else None
        words = words[:at] if at is not None else words
        try:
            hours = float(words[0].replace(",", ".")) if words else None
        except ValueError:
            hours = -1
        if len(words) > 1 or hours is not None and not 0 < hours <= 24 * 30:
            return self.send(i18n.t("telegram.away_usage"), reply_to=m["message_id"])
        self.put("owner.away", None, {"hours": hours, "tg": tg, **({"deputies": flag} if flag else {})})

    def page_text(self):
        """/page: the address of the hub page on the computer itself (with a password, the page asks for it)."""
        port = os.environ.get("HUB_WEB_PORT", "")
        port = int(port) if port.isascii() and port.isdigit() else 8787
        return i18n.t("telegram.page_open", url=f"http://localhost:{port}/")

    def stop_list(self, m):
        """/stop: the working executors as buttons (without a model). A press puts the owner's session.stop_requested — the same
        fact as the page's "Stop executor": the core stops, the bot only asks and later says what the core recorded."""
        st = self.reader.state
        names = sorted(n for n, s in st["sessions"].items() if minihub.runner(s) and s["state"] in STOPPABLE)
        if not names:
            return self.send(i18n.t("telegram.stop_none"), reply_to=m["message_id"])
        tg = {"chat": m["chat"]["id"], "id": m["message_id"]}
        rows = [[{"text": n[:60], "callback_data": self.key({"do": "stop", "session": n, "tg": tg, "ts": self.clock()})}]
                for n in names]
        self.send(i18n.t("telegram.stop_pick"), reply_to=m["message_id"], buttons=rows)

    def spec_cmd(self, arg, m):
        """/spec — the sessions that raise executors, a button each to turn specs on or off; /spec <session> on|off — at
        once. The same fact as the page's /spec (session.spec_set from the owner's Telegram): the core decides."""
        st, words = self.reader.state, (arg or "").split()
        if len(words) == 2 and words[1] in ("on", "off") and words[0] in st["sessions"]:
            return self.send(self.spec_set(words[0], words[1] == "on"), reply_to=m["message_id"])
        names = sorted(n for n, s in st["sessions"].items() if not minihub.runner(s) and not s.get("liaison")
                       and s["state"] not in ("removed", "closed", "failed"))
        if not names:
            return self.send(i18n.t("telegram.spec_none"), reply_to=m["message_id"])
        rows = [[{"text": i18n.t("telegram.spec_row_on" if st["sessions"][n].get("spec") else "telegram.spec_row_off",
                                 name=n[:50]),
                  "callback_data": self.key({"do": "spec_set", "session": n, "on": not st["sessions"][n].get("spec")})}]
                for n in names]
        self.send(i18n.t("telegram.spec_pick"), reply_to=m["message_id"], buttons=rows)

    spec_wait = 5.0   # how long a press waits for the core's decision (tests without hubd: 0)

    def spec_set(self, name, on):
        """session.spec_set from his Telegram, waiting for the core: the answer is what it decided (a refusal in words:
        an executor, a removed session) -> the line to show."""
        import uuid
        from core import journal   # the module is the bot's own attribute name (self.journal): the core's under its name here
        path, req = self.journal, uuid.uuid4().hex[:12]
        start = journal.mark(path)
        self.put("session.spec_set", name, {"on": on, "req": req})
        seq, why = journal.decided(path, req, self.spec_wait, 1 << 20, start=start)
        if why:
            return str(coretext.human(why))[:300]
        if seq is None:   # not recorded in time (hubd busy or down): sent, not "on"
            return i18n.t("telegram.spec_queued", name=name)
        return i18n.t("telegram.spec_on" if on else "telegram.spec_off", name=name)

    def spec_card(self, d):
        """An executor's spec waits for the owner (core/specmode.py): the file as a document and "Accept" / "Drop";
        "Accept" takes the file as it is at the press."""
        info = specmode.card(self.state_dir, d)
        if info["sha256"] is None or self.quiet():   # "no cards in Telegram" (telegram.quiet): it waits on the page
            return
        old = self.st["cards"].get(f"spec:{d['id']}")
        if old:   # the earlier card's text is stale: its buttons go, the new one comes below
            self.edit_done(old, i18n.t("telegram.spec_replaced"))
        head = i18n.t("telegram.spec_ready", id=d["id"], name=d["session"])
        # words in reply to the card or to its file go to the executor naming its task (spec_card): the raiser may cite
        # them (--for mN)
        route = {"session": d["session"], "spec": d["id"]}
        if not info["empty"]:   # Telegram takes no empty file: the card alone
            sent = self.send_file(info["path"], caption=head)
            if sent:   # its caption is the bot's heading: a reply quotes without it (quote)
                self.st["routes"][str(sent)] = {**route, "head": fmt.plain(head)}
        rows = [[{"text": i18n.t("telegram.spec_accept"),
                  "callback_data": self.key({"do": "spec", "did": d["id"], "action": "accept"})},
                 {"text": i18n.t("telegram.spec_drop"),
                  "callback_data": self.key({"do": "spec", "did": d["id"], "action": "drop"})}]]
        ids = self.send(i18n.t("telegram.spec_ask", id=d["id"]), buttons=rows, route=route)
        if ids:
            self.st["cards"][f"spec:{d['id']}"] = ids[-1]

    def spec_press(self, payload):
        """A press on a spec card: the same steps as the page (specmode.request) -> the answer's note."""
        st = self.reader.state
        code, out = specmode.request(self.state_dir, st, payload["did"], payload["action"],
                                     channel="telegram", wait=self.spec_wait)
        if out.get("error"):
            return i18n.t("telegram.spec_closed") if out["error"] == "closed" else str(coretext.human(out["error"]))[:190]
        return i18n.t("telegram.cb_done")

    def doing(self, sess):
        """What a session is doing — in the core's words: a fresh note (recap, "away", "waiting: …"), otherwise its state. The watcher's
        "what it works on" line — only for a running turn: for a free session it is about the past one."""
        now = tools.waiting_now(sess)   # a stale "waiting" (it was written to after that) — do not show
        recap = sess.get("recap") if sess.get("state") in chain.BUSY else None   # the core removes it at the start of a turn
        notes = [n for n in (recap, sess.get("away"), now) if n and n.get("text")]
        note = max(notes, key=lambda n: n.get("ts") or 0) if notes else None
        if note:
            text = " ".join(note["text"].split())[:200]
            return i18n.t("telegram.doing_waits", text=text) if note is now else text
        state = STATE_WORDS.get(views.shown(sess.get("state")))
        return i18n.t(state) if state else sess.get("state") or i18n.t("telegram.state_none")

    def status_text(self):
        """/status: who works and on what, how many questions wait for the owner, how many messages are queued."""
        st, now = self.reader.state, self.clock()
        live = [s for s in st["sessions"].values() if s["state"] not in ("removed", "closed", "failed")]
        busy = [s for s in live if s["state"] in ("busy", "dialog", "starting")]
        # as on the page (core/views.shown): a parked one is free, an executor that has finished is asleep
        asleep = sum(views.shown(s["state"]) == "asleep" for s in live)
        idle = sum(views.shown(s["state"]) == "idle" for s in live)
        lines = [i18n.t("telegram.status_head_asleep" if asleep else "telegram.status_head", busy=len(busy), idle=idle,
                        asleep=asleep)]
        for s in sorted(busy, key=lambda s: (s.get("kind") != "hub", s["name"])):
            lines.append(f"- {self.who(s['name'])} — {self.doing(s)}")
        queued = sum(1 for x in st["messages"].values() if x["state"] == "queued")
        lines.append(i18n.t("telegram.status_questions", questions=len(owner.questions(st, now)), queued=queued))
        o, acc = st.get("owner") or {}, st.get("account") or {}
        tz = o.get("tz") or "UTC"
        if o.get("state") == "away":
            lines.append(i18n.t("telegram.status_away_until", time=owner._local(o["until"], tz).strftime("%H:%M")) if o.get("until")
                         else i18n.t("telegram.status_away_back"))
        if ((acc.get("exhausted") or {}).get("until") or 0) > now:
            lines.append(i18n.t("telegram.status_accounts_busy",
                                at=owner._local(acc["exhausted"]["until"], tz).strftime("%d.%m %H:%M")))
        return "\n".join(lines)

    def start_voice(self, m, media, cfg, told=False):
        """Transcription of one voice message in the background; the result (m, text, why, net) goes to voice_done for drain_voice."""
        key = (m["chat"]["id"], m["message_id"])
        with self.voicing_mu:
            self.voicing[key] = [m, self.clock(), told]
        self.voice_flight.add(f"{key[0]}:{key[1]}")   # until drain_voice takes its result: retry_voice does not start a second one

        def work():
            try:
                got = self.transcribe(media, cfg)
            except Exception as e:  # noqa: BLE001 — an unforeseen failure: the owner gets "could not parse", not a lone 👀
                traceback.print_exc()
                got = None, type(e).__name__, False
            finally:   # release before the queue: otherwise "longer than usual" could follow the ready text
                with self.voicing_mu:
                    self.voicing.pop(key, None)
            self.voice_done.put((m, *got))
        work() if self.voice_sync else threading.Thread(target=work, daemon=True).start()

    def transcribe(self, media, cfg):
        """(text, None, False) or (None, why, net) — in the background; does not write to the bot state. net — the file did
        not download because of the network (DNS, a timeout, 5xx/429): drain_voice keeps the voice message and retries later."""
        try:
            suffix = voice.suffix_for(media)
            if not suffix:
                return None, i18n.t("telegram.audio_unsupported", mime=media.get("mime_type")), False
            raw = self.api.download(media["file_id"])
            return voice.transcribe_retry(raw, cfg, suffix=suffix, state=getattr(self, "state_dir", None)), None, False
        except voice.Empty:
            return None, i18n.t("web.srv.no_speech").rstrip(".。"), False   # "{why}." follows in the line to the owner
        except voice.VoiceError as e:   # the reason in the owner's words from the failure code, as on the page
            return None, i18n.t(VOICE_WHY.get(e.kind, "web.srv.voice_why_other")), False
        except TgError as e:
            return None, str(e)[:200], e.transient
        except OSError as e:
            return None, str(e)[:200], False

    def drain_voice(self):
        """Ready transcriptions — as ordinary owner messages: "🎤 voice: …" to the same addressee (reply, quote)."""
        self.retry_voice()
        with self.voicing_mu:   # it takes longer than a minute (retries, a slow model) — say once that the voice did not get lost
            slow = [v for v in self.voicing.values() if not v[2] and self.clock() - v[1] >= VOICE_SLOW_AFTER]
            for v in slow:
                v[2] = True
        for m, _, _ in slow:
            self.send(i18n.t("telegram.voice_still"), reply_to=m["message_id"])
        while not self.voice_done.empty():
            m, text, why, net = self.voice_done.get()
            key = f"{m['chat']['id']}:{m['message_id']}"
            self.voice_flight.discard(key)
            self.st["transcribe"].pop(key, None)   # to the file — save below, after the record
            if text is None and net:
                self.voice_net_wait(key, m, why)
                continue
            self.st["voice_wait"].pop(key, None)
            if text is None:
                self.react(m["message_id"], "👎")
                self.send(i18n.t("telegram.voice_failed", why=why), reply_to=m["message_id"])
                continue
            self.react(m["message_id"], "🤔")  # transcribed and passed on; 👍 — with an answer
            plain = {k: v for k, v in m.items() if k not in ("voice", "audio", "video_note")}
            # the head in the owner's language, as they see their message; the core parses any word after the mic (forms.HEADS)
            heard = {**plain, "text": f"🎤 {i18n.t('telegram.voice_head')}: {text}"}
            try:
                self.on_message(heard)
            except Exception as err:  # noqa: BLE001 — as in poll: the transcribed is not lost on any error
                self.lost(heard, err)
        while not self.voice_out.empty():
            self.send_voice(*self.voice_out.get())
        self.save()

    def voice_net_wait(self, key, m, why):
        """The voice file did not download because of the network: keep it in voice_wait (the state file — a restart keeps it)
        and try again on the clock (retry_voice), the owner hears once that it waits. After VOICE_NET_GIVE_UP without the network —
        one line asking to send it again (with 👎 only once that line went) and the entry is gone."""
        now = self.clock()
        w = self.st["voice_wait"].setdefault(key, {"m": m, "since": now, "tries": 0, "told": False})
        w["tries"] += 1
        if now - w["since"] >= VOICE_NET_GIVE_UP:
            try:
                self.send(i18n.t("telegram.voice_net_failed", why=why, count=VOICE_NET_GIVE_UP // 60), reply_to=m["message_id"])
            except TgError:   # still no network: one more download attempt later, the line (and 👎) go when it can
                w["next"] = now + VOICE_NET_PAUSES[-1]
                return
            self.react(m["message_id"], "👎")
            self.st["voice_wait"].pop(key, None)
            return
        w["next"] = min(now + VOICE_NET_PAUSES[min(w["tries"], len(VOICE_NET_PAUSES)) - 1], w["since"] + VOICE_NET_GIVE_UP)
        if not w["told"]:
            try:
                self.send(i18n.t("telegram.voice_net_wait"), reply_to=m["message_id"], quiet=True)
                w["told"] = True
            except TgError:
                pass

    def retry_voice(self):
        """Voice messages waiting for the network whose time has come — download and transcribe again (by the clock, the bot loop
        does not sleep)."""
        now = self.clock()
        for key, w in list(self.st["voice_wait"].items()):
            m = w["m"]
            if key in self.voice_flight or w.get("next", 0) > now:   # an attempt longer than the last pause is still in flight
                continue
            w["next"] = now + VOICE_NET_PAUSES[-1]   # until the attempt reports back (a crash in it — retried, not forgotten)
            cfg = self.hub_config()
            if not voice.enabled(cfg, state=self.state_dir):   # turned off while it waited — as on_message: say so, no download
                try:
                    self.send(i18n.t("telegram.voice_off"), reply_to=m["message_id"])
                except TgError:   # no network still: the line goes on the next round
                    continue
                self.st["voice_wait"].pop(key, None)
                continue
            media = m.get("voice") or m.get("audio") or m.get("video_note")
            self.start_voice(m, media, cfg, told=True)

    def voice_reply(self, text, reply_to, route):
        """The "Voice reply" module: after the text of a reply to the owner's message — a voice message (in the background: compression and voice
        take up to a minute; sent from drain_voice)."""
        cfg = self.hub_config()
        if not reply_to or not speech.enabled(cfg):
            return

        # one voice message per reply — the key of this reply (the owner's message and the text): one message is answered by both the hub and a
        # session, and by the following turns of the same thread — each has its own voice
        key = f"{reply_to}:{hashlib.sha1(text.encode('utf-8', 'surrogatepass')).hexdigest()[:12]}"

        def work():
            with self.speak_lock:
                self.speech.by, self.speech.silent = None, False
                try:
                    # the bot is not the watcher: its POOL is empty; the account — when the turn in the queue came (replies wait for the lock up to a minute)
                    account = (self.reader.state.get("account") or {}).get("active")
                    got = self.speak(text, cfg, account=account, session=(route or {}).get("session"))
                    if got is not None:   # the voice module stayed silent — the reply remains text, without a failure line
                        self.voice_out.put((reply_to, route, got, None, self.speech.by, key))
                    elif self.speech.silent:   # did not answer; {} — "no voice message" — is not a failure
                        self.said(self.speech.by, reply_to, False, "the voice module did not answer")   # a journal fact
                except Exception as e:  # noqa: BLE001 — a voice failure does not crash the bot; the text is already with the owner
                    why = str(e)[:200] or type(e).__name__
                    self.said(self.speech.by, reply_to, False, why)
                    self.voice_out.put((reply_to, route, None, why, None, key))
        work() if self.voice_sync else threading.Thread(target=work, daemon=True).start()

    def said(self, by, reply_to, ok, why=None):
        """Voicing by the voice module (by; None — the builtin voice: not recorded) — the fact speech.said to the core: failures
        of the module in a row give the voice back to the builtin one (core/providers.py). ok None — the voice message exists, Telegram did not accept it."""
        if not by:
            return
        spool.put(self.spool, "speech.said", None, {"module": by, "key": str(reply_to), "ok": ok,
                                                    **({"why": str(why)[:200]} if why else {}),
                                                    "channel": "telegram", "owner": False}, src="telegram")

    def send_voice(self, reply_to, route, got, why, by=None, key=None):
        reply = {"message_id": reply_to, "allow_sending_without_reply": True}
        try:
            if got is None:
                self.call("sendMessage", **self.where(reply_to), text=i18n.t("telegram.speech_failed", why=why),
                              reply_parameters=reply, disable_notification=True)
                return
            # one voice message per reply: the reply key is written before sending (interrupted in the middle — the reply stays text,
            # there will be no second voice message)
            if key is not None:
                if key in self.st["voiced"]:
                    return
                self.st["voiced"][key] = time.time()
                self.save()
            res = self.to_general(lambda **p: self.api.upload("sendVoice", "voice", got[0], **p), {
                **self.where(reply_to), "duration": got[1], "reply_parameters": reply})
            self.said(by, reply_to, True)
            if route and isinstance(res, dict) and res.get("message_id"):
                self.st["routes"][str(res["message_id"])] = route   # a reply to the voice message — to the same addressee
        except TgError as e:
            self.said(by, reply_to, None, str(e))
            sys.stderr.write(f"telegram: the voice message did not go: {e}\n")
        finally:
            if got:
                shutil.rmtree(os.path.dirname(got[0]), ignore_errors=True)

    def attachments(self, m):
        """(downloaded files, failed — [{name, size?, reason}], reason — as text for a person). Each one is also
        a line in stderr."""
        items = []
        if m.get("photo"):
            p = m["photo"][-1]
            items.append((p["file_id"], f"photo-{m['message_id']}.jpg", "image/jpeg", p.get("file_size")))
        if m.get("document"):
            d = m["document"]
            items.append((d["file_id"], d.get("file_name") or f"file-{m['message_id']}", d.get("mime_type") or "",
                          d.get("file_size")))
        out, failed = [], []
        for file_id, name, mime, size in items:
            raw, why = None, None
            if size and size > MAX_FILE:   # the Bot API gives bots files up to 20 MB — do not ask for a bigger one in vain
                why = ("too_big", None)
            else:
                try:
                    raw = self.api.download(file_id)
                except TgError as e:
                    big = "too big" in str(e).lower()
                    why = ("too_big" if big else "network" if e.transient else "telegram", str(e))
                except (OSError, http.client.HTTPException) as e:   # a timeout, a break in the middle of a download, DNS
                    why = ("network", f"{type(e).__name__}: {e}")
                if raw is not None and len(raw) > MAX_FILE:   # read up to the limit: the real size is unknown
                    why, size = ("too_big", None), None
            if why:
                failed.append({"name": name, **({"size": size} if size else {}),
                               "reason": self.attach_reason(name, size, *why)})
                print(f"telegram: attachment {file_id} of message {m.get('message_id')} not downloaded: "
                      f"{why[0]}{': ' + why[1] if why[1] else ''}", file=sys.stderr, flush=True)
                continue
            path = attach.new_path(self.state_dir, name)
            name = os.path.basename(path)
            with open(path, "wb") as f:
                f.write(raw)
            out.append({"path": path, "name": name, "mime": mime or "application/octet-stream", "size": len(raw)})
        return out, failed

    @staticmethod
    def attach_reason(name, size, kind, detail):
        lang = i18n.current()
        comma = lang.split("-")[0] in DECIMAL_COMMA
        mb = lambda n: i18n.tr(lang, "telegram.size_mb", n=f"{n / 2**20:.1f}".replace(".", "," if comma else "."))  # noqa: E731
        what = i18n.tr(lang, "telegram.attach_file_size", name=name, size=mb(size)) if size else name
        if kind == "too_big":
            return i18n.tr(lang, "telegram.attach_too_big", file=what, limit=MAX_FILE >> 20)
        key = "telegram.attach_network" if kind == "network" else "telegram.attach_error"
        return i18n.tr(lang, key, file=what, error=(detail or "")[:200])

    def on_callback(self, cq):
        user, where = (cq.get("from") or {}).get("id"), (cq.get("message") or {}).get("chat")
        if self.group() is None:
            # a press by the owner — in his private chat too (a message button without a chat — inline mode — has no chat)
            ours = user in self.owners() and (where is None or (where.get("type") == "private" and where.get("id") in self.owners()))
            who = {"role": "owner"}
        else:   # the group: only its admin confirms (spec d2513429), checked fresh
            ours = where is not None and where.get("id") == self.group()
            admins = self.admins() if ours and isinstance(user, int) else {}
            if ours and (admins is None or user not in admins):
                try:
                    self.api.call("answerCallbackQuery", callback_query_id=cq["id"], text=i18n.t(
                        "telegram.group.press_unchecked" if admins is None else "telegram.group.press_admins"))
                except TgError:
                    pass
                return
            who = {"role": "admin", "tg": user, "name": display_name(cq.get("from") or {})}
        self.speaker = who
        try:
            return self.press(cq, ours)
        finally:
            self.speaker = None

    def press(self, cq, ours):
        payload = self.st["keys"].get((cq.get("data") or "")[2:]) if ours else None
        note = i18n.t("telegram.cb_done")
        if not payload:
            note = i18n.t("telegram.cb_stale")
        elif payload["do"] == "answer":
            note = i18n.t("telegram.cb_accepted" if self.answer(payload["ask"], payload["q"], payload["label"])
                          else "telegram.cb_answered")
        elif payload["do"] == "pick":   # multiSelect: mark / unmark an option
            ask = self.st["asks"].get(payload["ask"])
            if not ask or ask.get("done"):
                note = i18n.t("telegram.cb_answered")
            else:
                got, lb, qi = ask["picked"][str(payload["q"])], payload["label"], payload["q"]
                got.remove(lb) if lb in got else got.append(lb)
                note = i18n.t("telegram.cb_picked", count=len(got))
                try:
                    self.api.call("editMessageReplyMarkup", chat_id=self.chat(), message_id=ask["qmsg"][str(qi)],
                                  reply_markup={"inline_keyboard": self.ask_rows(payload["ask"], qi, ask)})
                except TgError:
                    pass
        elif payload["do"] == "picked":   # multiSelect: "Done" — the chosen ones in the order of the options
            ask = self.st["asks"].get(payload["ask"])
            qi = payload["q"]
            got = [lb for lb in (ask or {}).get("labels", {}).get(str(qi), []) if lb in (ask or {}).get("picked", {}).get(str(qi), [])]
            if not got:
                note = i18n.t("telegram.cb_pick_one" if ask and not ask.get("done") else "telegram.cb_answered")
            else:
                note = i18n.t("telegram.cb_accepted" if self.answer(payload["ask"], qi, ", ".join(got)) else "telegram.cb_answered")
        elif payload["do"] == "perm":
            ask = self.st["asks"].get(payload["ask"])
            if ask and not ask.get("done"):
                self.put("permission.answered", ask["session"], {"request_id": ask["request_id"],
                                                                 "behavior": payload["behavior"]})
                ask["done"] = True
            else:
                note = i18n.t("telegram.cb_answered")
        elif payload["do"] == "stop":
            sess = self.reader.state["sessions"].get(payload["session"]) or {}
            if self.clock() - payload.get("ts", 0) > STOP_LIST_TTL:
                note = i18n.t("telegram.stop_old")
            elif payload.get("used"):   # one press, one fact: a second tap would put a stop the core refuses ("already asleep")
                note = i18n.t("telegram.stop_pressed")
            elif minihub.runner(sess) and sess["state"] in STOPPABLE:
                payload["used"] = True
                self.put("session.stop_requested", payload["session"], {"tg": payload["tg"]})
                note = i18n.t("telegram.stop_pressed")
            else:
                note = i18n.t("telegram.stop_gone")
        elif payload["do"] == "spec":   # "Accept" / "Drop" on a spec card (core/specmode.py)
            note = self.spec_press(payload)
        elif payload["do"] == "here":   # /here's "Make an agent" / "Make a direction" / "Cancel"
            note = self.here_press(payload, cq)
        elif payload["do"] == "group_bind":   # "Move the bot there" / "Keep it here" on another group's request
            note = self.group_press(payload, cq)
        elif payload["do"] == "spec_set":   # /spec: specs on or off for a session
            note = self.spec_set(payload["session"], payload["on"] is True)[:190]
        elif payload["do"] == "tool":
            self.put("tools.approved" if payload["yes"] else "tools.denied", payload["session"], {"tool": payload["tool"]})
        elif payload["do"] == "update":   # the app's "Update" (app spec §4.3): the same flag as the page's button
            note = self.update_press(payload)
        elif payload["do"] == "restart":   # "Restart" under an installed update waiting for it (ready_buttons)
            note = self.restart_press(payload)
        elif str(payload["do"]).startswith("bus_"):   # linking hubs (modules/telegram/hubs.py)
            note = self.hubs.press(payload, cq)
        try:
            self.api.call("answerCallbackQuery", callback_query_id=cq["id"], text=note)
        except TgError:
            pass

    def offer_due(self, data):
        """update_offer would send for this check: a stable version of an app on a layer, not offered yet."""
        version = str(data.get("version") or "")
        return bool(data.get("result") == "available" and data.get("app") is True and data.get("layer") is True
                    and data.get("update_channel") != "main" and version
                    and self.st.get("update_offered") != version)

    def update_offer(self, ev, data):
        """A new version of the app on a code layer (modules/update/layer.py, app spec §4.3): one message per version
        with the "Update" button, on the stable channel only (nightly comes every night — "About the hub" shows it).
        Not again for the same version; a newer one is a new message."""
        version = str(data.get("version") or "")
        if not self.offer_due(data):
            return
        notes = news.bullets(data.get("notes") or [], total=2800)   # with the button: one message
        rows = [[{"text": i18n.t("telegram.update_button"),
                  "callback_data": self.key({"do": "update", "version": version, "ts": ev.get("ts")})}]]
        self.send(i18n.t("telegram.update_available", version=version) + (f"\n\n{notes}" if notes else ""),
                  head=f"🔔 <b>{html.escape(i18n.t('core.update.title'))}</b>", buttons=rows)
        self.st["update_offered"] = version   # after the send: a network failure repeats the event and the message

    MIN_EVERY = 24 * 3600   # the message about the minimum version again after this, while the hub is below it

    def update_minimum(self, ev, data):
        """Below the distribution server's minimum version (app spec §7.2): "The hub needs an update", why, what stops
        working below it, and "Update" — once a day while the hub is below (a newer minimum says so at once). Not while
        an install waits for the restart (the owner pressed already). -> whether it was sent."""
        low = data.get("min") if data.get("layer") is True else None
        if not (isinstance(low, dict) and low.get("below") is True and isinstance(low.get("version"), str)) \
                or data.get("result") in ("applied", "restarting"):
            return False
        told = self.st.get("update_min_told") if isinstance(self.st.get("update_min_told"), dict) else {}
        ts = ev.get("ts") if isinstance(ev.get("ts"), (int, float)) else time.time()
        if told.get("version") == low["version"] and told.get("ahead") == bool(low.get("ahead")) \
                and ts - (told.get("ts") or 0) < self.MIN_EVERY:
            return False
        reason = low.get("reason") if isinstance(low.get("reason"), dict) else {}
        why = reason.get(i18n.current()) or reason.get("en") or next((v for v in reason.values() if isinstance(v, str)), "")
        stops = [i18n.t(f"web.updates.min_stop_{x.replace('-', '_')}") for x in low.get("stops") or []
                 if x in ("claude", "update-format", "bus", "stats")]
        text = i18n.t("telegram.update_min_ahead", version=low["version"], since=str(low.get("since") or "")) \
            if low.get("ahead") else i18n.t("telegram.update_min", version=low["version"])
        text += (f"\n\n{why}" if why else "") + ("\n\n" + i18n.t("telegram.update_min_stops", list="; ".join(stops))
                                                  if stops else "")
        rows = [[{"text": i18n.t("telegram.update_button"),
                  "callback_data": self.key({"do": "update", "version": low["version"], "ts": ev.get("ts")})}]]
        self.send(text, head=f"⚠️ <b>{html.escape(i18n.t('core.update.title'))}</b>", buttons=rows)
        self.st["update_min_told"] = {"version": low["version"], "ts": ts, "ahead": bool(low.get("ahead"))}
        return True

    def ready_buttons(self, ev, data):
        """"Restart" under the update's notice of an installed release that waits for the restart (modules/update/layer.py
        _restart_or_ask), only from the update process itself."""
        ready = data.get("ready")
        if ev.get("src") != "update" or not isinstance(ready, dict) or not isinstance(ready.get("version"), str):
            return None
        return [[{"text": i18n.t("telegram.restart_button"),
                  "callback_data": self.key({"do": "restart", "version": ready["version"][:80]})}]]

    def restart_waits(self):
        """An installed release waits for the restart: a layer's switch (layers/next.json) or a new environment laid out
        (layers/shell-update.json "ready"). After the restart or "Return" before it — no."""
        from core import appcode, shellnext
        return isinstance((appcode.pending(self.state_dir) or {}).get("name"), str) \
            or shellnext.read(self.state_dir).get("status") == "ready"

    def restart_press(self, payload):
        """"Restart" under "updated to X — restart the hub": the whole hub, when sessions are free (as `hub restart`);
        one press, one request; an old button after the restart (or after "Return") asks nothing."""
        if not self.restart_waits():
            return i18n.t("telegram.restart_not_needed")
        if not payload.get("used"):
            self.put("hub.restart_requested", None, {"parts": [], "now": False, "why": i18n.t(
                "update.restart_layer", version=payload.get("version") or "?")})
            payload["used"] = True
        return i18n.t("telegram.restart_pressed")

    def update_press(self, payload):
        """"Update" pressed: the update process installs at its next pass (the flag the page writes, modules/update
        INSTALL_FLAG); one press is one install."""
        if payload.get("used"):
            return i18n.t("telegram.update_pressed", version=payload.get("version") or "?")
        from modules.update.update import INSTALL_FLAG
        try:
            open(os.path.join(self.state_dir, INSTALL_FLAG), "w").close()
        except OSError as e:
            return i18n.t("telegram.update_failed", error=str(e)[:120])
        payload["used"] = True
        return i18n.t("telegram.update_pressed", version=payload.get("version") or "?")

    def answer(self, ask_id, qi, label, words=False):
        """The answer to an AskUserQuestion; when all questions are answered — one fact to the core. An answer in words (a reply to
        a card) closes the whole form: the owner said what he wanted, there is nobody to wait for buttons on the rest (a voice
        reply to the first of three cards — 👌, and the session got nothing)."""
        ask = self.st["asks"].get(ask_id)
        if not ask or ask.get("done") or not self.open_in_core(ask):
            # the core has already closed the card (answered through the hub, removed), and the bot has not removed the buttons yet: the core would reject the answer and
            # the words would be lost — let them go to the session as a message (sweep_asks will remove the buttons)
            return False
        ask["answers"][ask["questions"][qi]] = label
        if words:
            for q in ask["questions"]:
                ask["answers"].setdefault(q, "— (the owner answered in words in another question of this form)")
        if len(ask["answers"]) == len(ask["questions"]):
            self.put("permission.answered", ask["session"], {"request_id": ask["request_id"], "behavior": "allow",
                                                             "answers": ask["answers"]})
            ask["done"] = True
        return True

    # --- outgoing: the core journal → the owner ---------------------------------------------------------------------
    def read_journal(self):
        from core import journal
        ino = journal.identity(self.journal)
        out = []
        if ino != getattr(self, "ino", ino):  # the journal was started anew (journal.rotate)
            # rotation of the file we were reading: read the rest of the archive from our place and stand on the "rotate" snapshot (like the page,
            # core/tail) — do not fold again what was carried over on the snapshot before it; if that fails — from the start, do not send the old
            got = journal.go_on(self.journal, self.ino, self.pos, ino)
            if got is not None:
                out, self.pos = got
            else:
                self.pos, self.reader = 0, views.Reader()
        self.ino = ino
        try:
            with open(self.journal, "rb") as f:
                if os.fstat(f.fileno()).st_ino != ino:   # while deciding there was another rotation: this file — next time
                    return out
                f.seek(self.pos)
                chunk = f.read()
        except FileNotFoundError:
            return out
        end = chunk.rfind(b"\n")
        if end < 0:
            return out
        self.pos += end + 1
        for line in chunk[:end].split(b"\n"):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    def catch_up(self):
        """First start — from the end of the journal (do not send history to the owner); restart — from where we stopped."""
        self.business.recover()   # an answer in the middle of its call when the bot stopped: unknown, not repeated
        events = self.read_journal()
        start = self.st["seq"]
        for ev, why in self.verdicts(events):
            if start is not None and ev["seq"] > start:
                self.queue(ev, why)
        if events and start is None:
            self.st["seq"] = events[-1]["seq"]
        for m in list(self.st["transcribe"].values()):   # were in progress at the last start — anew; on_message will record again
            del self.st["transcribe"][f"{m['chat']['id']}:{m['message_id']}"]
            try:
                self.on_message(m)
            except Exception as err:  # noqa: BLE001 — as in poll: the owner's words are not lost on any error
                self.lost(m, err)
        self.save()

    def flush(self):
        """Outgoing in order; a network failure, 429 or 5xx — wait and repeat the same (we do not advance the number: after a
        restart the bot continues from it too), a permanent error — record it and move on, so that the queue does not stall."""
        while self.outbox and self.clock() >= self.hold_until:
            ev = self.outbox[0]
            if self.st["posted"].get("seq") != ev["seq"]:
                self.st["posted"] = {"seq": ev["seq"], "refs": {}}
            self.sending, self.attempt = ev["seq"], {}
            try:
                self.outgoing(ev)
            except TgError as e:
                if e.transient:
                    self.fails += 1
                    delay = e.retry_after or min(60, 2 ** min(self.fails, 6))
                    self.hold_until = self.clock() + delay
                    if self.fails == 1 or self.fails % 10 == 0:
                        sys.stderr.write(f"telegram: {ev['type']} #{ev['seq']}: {e} — will retry in {delay} s\n")
                    return
                sys.stderr.write(f"telegram: {ev['type']} #{ev['seq']}: {e} — skipping\n")
            except Exception:  # noqa: BLE001
                # an error in the bot's own code on this event: flying out of tick(), it crashed the bot, and after every
                # restart the same event came first — log it with the trace and skip, like a permanent error
                sys.stderr.write(f"telegram: {ev['type']} #{ev['seq']}: bot error — skipping\n{traceback.format_exc()}")
            finally:
                self.sending = None
            self.outbox.pop(0)
            self.fails = 0
            self.st["seq"], self.st["posted"] = ev["seq"], {}

    def once(self, key, post):
        """One message of the event being sent: post() sends it and returns the result. A failure in the middle of the event repeats
        the event as a whole — a part that has already gone (the same key, the same number among equals) is not sent a second time, its
        earlier result is returned. Stored in state.json: a restart before the repeat does not repeat it either. Outside sending an event
        (live feedback, a button press) — it just sends."""
        if self.sending is None:
            return post()
        k = hashlib.sha256(json.dumps(key, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:16]
        n = self.attempt[k] = self.attempt.get(k, 0) + 1
        done = self.st["posted"].setdefault("refs", {}).setdefault(k, [])
        if n <= len(done):
            return done[n - 1]
        ref = post()
        done.append(ref)
        return ref

    def verdicts(self, events):
        """Journal events — for the reader → (event, why the core rejected it, or None). hubd writes core.rejected
        as a line right after, so the last line may not have a verdict yet: the fold gives the same one on the same state
        (on a copy, before feed)."""
        for i, (ev, known) in enumerate(reducer.verdicts(events)):
            why = None
            if known == "rejected":
                rej = events[i + 1].get("data") or {}   # the reason and its code (core/forms.py)
                why = coretext.why_of(rej, i18n.t("telegram.rejected"))
            elif known is None and ev["type"] not in ("core.snapshot", "core.rejected"):
                why = reducer.reduce(self.reader.state, ev)[2]
                known = "rejected" if why else "accepted"
            self.reader.feed(ev, known)
            if ev["type"] == "core.snapshot" and (ev.get("data") or {}).get("why") == "rotate":
                self.reader.drop_removed()   # the reader survives rotation (read_journal): do not accumulate feeds of removed ones
            yield ev, why

    def queue(self, ev, why):
        """An event — into the send queue in order. One rejected by the core changed nothing and does not go to the chat as if it
        happened (`hub notify` from a session is not "🔔 Monitor"). Except: an answer to a card (rejected — the card stays,
        close_ask), a turn result and a file rejected by a core code failure (the answer to the owner matters more), and his own
        message from here — he gets 👎 and the reason."""
        if why:
            data, code = ev.get("data") or {}, forms.code_of(why)
            if ev["type"] == "permission.answered":
                ev = {**ev, "rejected": True}
            elif ev["type"] in ("turn.result", "owner.file") and forms.is_reason(forms.CORE_FAILURE, code, why):
                pass
            elif ev["type"] == "message.received" and data.get("channel") == "telegram" and data.get("tg"):
                ev = {**ev, "refused": why}
            elif ev["type"] == "session.stop_requested" and data.get("channel") == "telegram" and data.get("tg"):
                ev = {**ev, "stop_refused": why}   # /stop: pressed, and the core refused (it slept already) — say why
            elif ev["type"] in HERE_FACTS and data.get("req") in self.st["binding"]:
                ev = {**ev, "bind_refused": why}   # /here's "Make an agent": the core refused — say why, bind nothing
            else:
                return
        if ev["type"] == "turn.result":
            # the turn chain and whether another session tells it (one task — one message) — as of the result: sending may be
            # late (a network failure), and the session may start the next turn or fold another result
            sess = self.reader.state["sessions"].get(ev.get("entity")) or {}
            ev = {**ev, "_root": sess.get("turn_root"), "_merged": (sess.get("merged") or {}).get("seq") == ev["seq"],
                  "_owner_reply": sess.get("owner_reply") == ev["seq"],
                  "_own_talk": self.own_talk(self.reader.state, ev.get("entity"), sess.get("turn_root"))}
        if not any(o["seq"] == ev["seq"] for o in self.outbox):
            # the core verdict — with the event: sending may be late (a network failure), and the reader state will move ahead
            self.outbox.append(ev)
            self.flush()   # at once, while the reader state is as at the moment of the event; on failure — waits in the queue

    def tick(self):
        self.follow_chat()
        for ev, why in self.verdicts(self.read_journal()):
            if self.st["seq"] is not None and ev["seq"] <= self.st["seq"]:
                continue  # already sent (a new journal repeats the recent tail of the old one)
            if self.st["seq"] is None:
                self.st["seq"] = ev["seq"]
            self.queue(ev, why)
        self.say_group()
        self.flush()
        self.hubs.tick()
        self.release_held()
        self.digest_tick()
        self.live_tick()
        self.sweep_asks()
        self.watch_failed()
        self.save()

    def digest_settings(self):
        """(on, minutes) of the task digest: telegram.digest (off by default) and telegram.digest_minutes (5-1440, 30). hub.json is
        read no more than once per 5 s, like quiet()."""
        now = self.clock()
        if now - self._digest[0] >= 5:
            try:
                tg = (self.hub_config() or {}).get("telegram") or {}
                try:
                    minutes = max(5, min(1440, int(tg.get("digest_minutes") or 30)))
                except (TypeError, ValueError):
                    minutes = 30
                self._digest = (now, tg.get("digest") is True, minutes)
            except Exception:   # noqa: BLE001 - hub.json is unreadable: as it was
                self._digest = (now, self._digest[1], self._digest[2] or 30)
        return self._digest[1], self._digest[2]

    def digest_tick(self):
        """telegram.digest: every telegram.digest_minutes one message with the owner's open tasks (modules/telegram/digest.py), the
        previous one erased. Nothing open or the setting off - the last one is erased and none comes. The last message, when it was
        sent and the ids still to erase are in state.json: a restart does not resend it. The text is built at most once a minute.
        Not a status: brief, focus and quiet do not mute it, the owner turned it on."""
        mine = self.st["digest"]
        if self.outbox or self.cfg.get("receive") is False or self.chat() is None or self.clock() < self.digest_retry:
            return
        now = self.clock()
        self.digest_erase(mine, now)
        on, minutes = self.digest_settings()
        old = mine.get("ids") or []
        if on and (now < self.digest_next or now - mine.get("sent", mine.get("at", 0)) < minutes * 60 and not old):
            return   # not time to look yet; or the interval since the last digest is not over and there is nothing to erase
        text = ""
        if on:
            self.digest_next = now + 60
            try:
                text = digest.build(self.reader.state, now=now)
            except Exception as e:   # noqa: BLE001 - a fault in the digest must not stop the bot loop
                sys.stderr.write(f"telegram: digest: {e!r}\n")
                return
        if text and old and now - mine.get("sent", mine.get("at", 0)) < minutes * 60:
            return
        if not text and not old:
            return
        try:
            ids = self.send(text, quiet=True) if text else []   # a periodic notice: no sound, like the bot's other ones
        except TgError as e:   # a digest is not a result: do not crash the bot, try again in a minute (a refusal - in an hour)
            if e.transient:
                self.digest_retry = now + max(60, e.retry_after or 0)
            else:
                self.digest_next = now + 3600   # only the next build waits: erasing and turning it off do not
            sys.stderr.write(f"telegram: digest: {e}\n")
            return
        if text and not ids:
            return   # did not go - the previous one stays
        mine["ids"], mine["at"] = ids, now
        if text:
            mine["sent"] = now
        mine["erase"] = (mine.get("erase") or []) + old
        self.digest_erase(mine, now)

    def digest_erase(self, mine, now):
        """Erase the previous digests; a temporary failure (network, 429, 5xx) keeps the id for the next minute, any other
        (already erased by the owner, older than 48 h for Telegram) drops it."""
        left = []
        for mid in mine.get("erase") or []:
            try:
                self.api.call("deleteMessage", chat_id=self.chat(), message_id=mid)
            except TgError as e:
                if e.transient:
                    left.append(mid)
        mine["erase"] = left
        if left:
            self.digest_retry = now + 60

    def live_tick(self):
        """While a turn for the owner's message from Telegram runs (of the hub or of the session he wrote to) — "typing…"; a turn
        longer than FIRST_LINE_AFTER — its first line as a reply; nothing for him within LATE_AFTER — one "⏳" line. The bot's clock,
        the count — from the event time in the journal; what is running — from the core state."""
        if self.outbox:   # outgoing has not gone yet: the core state is ahead of what was sent — do not get ahead of the result
            return
        st, now = self.reader.state, self.clock()
        for key, live in list(self.st["live"].items()):
            if now - live["ts"] >= LIVE_FOR:
                self.st["live"].pop(key)
                self.typing_at.pop(key, None)
                continue
            turn = live.get("turn") or {}
            name = turn.get("session")
            sess = st["sessions"].get(name) or {}
            running = (bool(name) and not live.get("done") and self.st["turn"].get(name) == key
                       and sess.get("state") == "busy")
            try:
                if not running:
                    self.typing_at.pop(key, None)
                elif now - self.typing_at.get(key, 0) >= TYPING_EVERY:
                    self.typing_at[key] = now
                    self.api.call("sendChatAction", chat_id=live["chat"], action="typing", **self.topic_param(live["tg"]))
                if (running and live.get("said") and not live.get("first")
                        and FIRST_LINE_AFTER <= now - turn["ts"] < LATE_AFTER):
                    # we try once; counted as sent only when it went — otherwise the result will decide it already existed
                    live["first"] = "trying"
                    self.send(live["said"], head="" if sess.get("kind") == "hub" else f"<b>{html.escape(name)}</b>:",
                              reply_to=live["tg"], route={"session": name}, cwd=sess.get("cwd"), fold=True)
                    live["first"] = True
                if not live.get("answered") and not live.get("late") and LATE_AFTER <= now - live["ts"] < 2 * LATE_AFTER:
                    live["late"] = True
                    text, who = self.late_text(key)
                    self.send(text, reply_to=live["tg"], route={"session": who} if who else None)
            except TgError as e:   # feedback is not a result: if it failed — do not repeat and do not crash the bot
                sys.stderr.write(f"telegram: live feedback for {key}: {e}\n")

    def holders(self, key):
        """Whose is the owner's message mN: the addressee, the session, which took it as a turn, the executors of assignments for it."""
        st, live = self.reader.state, self.st["live"][key]
        who = {(st["messages"].get(key) or {}).get("session"), (live.get("turn") or {}).get("session")}
        who |= {d["session"] for d in st.get("delegations", {}).values() if d.get("for") == key}
        return who - {None}

    def late_text(self, key):
        """"⏳ <who> is still busy: <what it is doing>" — whose the message is (recap, "waiting", state — from the core);
        one still in the queue says so; a session that runs no turn (asleep, waiting) "has not answered yet"."""
        st, live = self.reader.state, self.st["live"][key]
        msg = st["messages"].get(key) or {}
        who = msg.get("session") or (live.get("turn") or {}).get("session")
        for d in st.get("delegations", {}).values():
            if d.get("for") == key:
                who = d["session"]   # the hub passed it to an executor — we wait for it
        sess = st["sessions"].get(who) or {}
        doing = self.doing(sess)
        label = i18n.t("telegram.late_hub") if sess.get("kind") == "hub" or not who else f"`{who}`"
        # "still busy" only while it runs a turn: an asleep or waiting session has not answered yet
        line = "telegram.late_waits" if sess.get("state") not in chain.BUSY else \
            "telegram.late_busy_queued" if msg.get("state") == "queued" else "telegram.late_busy"
        return i18n.t(line, label=label, doing=doing), who

    def outgoing(self, ev):
        """An event of the journal → the chat. In the group, what it says goes to the topic of the session it is about."""
        data = ev.get("data") or {}
        name = ev.get("entity") if ev.get("entity") in self.reader.state["sessions"] else data.get("sender")
        self.ev_session = name if isinstance(name, str) else None
        try:
            return self.say(ev)
        finally:
            self.ev_session = None

    def say(self, ev):
        st, kind, name, data = self.reader.state, ev["type"], ev.get("entity"), ev.get("data") or {}
        self.business.outgoing(ev, st)   # an answer to a person a session asked to send: it goes in his name, once
        if ev.get("refused"):   # the core rejected his message from here: not delivered — and why
            self.react(data["tg"]["id"], "👎")
            self.send(i18n.t("telegram.not_delivered", why=coretext.human(ev["refused"])), reply_to=data["tg"]["id"])
            return
        if kind in HERE_FACTS and data.get("channel") == "telegram" and self.here_done(ev, ev.get("bind_refused")):
            return
        if kind == "session.stop_requested" and data.get("channel") == "telegram" and data.get("tg"):   # /stop: the core's word
            if ev.get("stop_refused"):
                self.send(i18n.t("telegram.stop_refused", name=name, why=re.sub(r" \(\S+ in [^)]+\)$", "", str(coretext.human(ev["stop_refused"])))),
                          reply_to=data["tg"]["id"])
            else:
                self.send(i18n.t("telegram.stop_done", name=name), reply_to=data["tg"]["id"])
            return
        sm = st["owner"].get("summary")
        # wrote in the "away" mode — returned: a summary, and without it (telegram.back_summary) — a line, if he wrote from here
        if kind == "message.received" and sm and sm.get("seq") == ev["seq"]:
            if self.back_summary():
                self.send(summary_text(sm))
            elif data.get("channel") == "telegram":
                self.send(i18n.t("web.cmd.back_ok") + ".")
        if kind == "message.received" and data.get("tg") and data.get("channel") == "telegram":
            self.st["owner_msgs"][f"m{ev['seq']}"] = data["tg"]["id"]
            # old (the bot was down) — without live feedback; a remark to a person of the group (fyi) starts no turn to wait for
            if self.clock() - ev["ts"] < LIVE_FOR and not data.get("fyi"):
                self.st["live"][f"m{ev['seq']}"] = {"tg": data["tg"]["id"], "chat": data["tg"]["chat"], "ts": ev["ts"]}
            return
        if kind == "session.said" and name and not data.get("parent"):   # the first line of a turn (not the words of his agent)
            live = self.st["live"].get(self.st["turn"].get(name) or "")
            said = reducer.shown_text(data.get("text")).strip()   # without HANDOFF-DONE, as in on_result
            if live and (live.get("turn") or {}).get("session") == name and not live.get("said") \
                    and not reducer.FAILURE_LINE.match(said):   # "You've hit your session limit…" — a failure, not an answer
                live["said"] = said
            return
        if kind in ("account.exhausted", "account.waiting"):
            # all accounts are busy or the core waits for a close reset of the active one (accounts: wait_reset): a turn interrupted
            # by the limit will continue after the reset
            for who, wait in list(self.st["retry"].items()):
                until = owner._local(data.get("until") or ev["ts"], st["owner"].get("tz") or "UTC").strftime("%d.%m %H:%M")
                self.tell(wait["tg"], wait["route"], self.limit_line(who, i18n.t("telegram.limit_hit"), kind, until))
                self.st["retry"].pop(who)   # after the line: a network failure — the repeat of the event will say it
            return
        if kind == "owner.away" and data.get("channel") == "telegram" and data.get("tg"):   # /away: the core recorded it
            o = st["owner"]
            # still away by this /away (a later /back or "away" supersedes it; a repeat keeps away_since, so not that)
            if o.get("state") == "away" and o.get("away_seq") == ev["seq"]:
                until = (i18n.t("telegram.away_until_time", time=owner._local(o["until"], o.get("tz") or "UTC").strftime("%H:%M"))
                         if o.get("until") else i18n.t("telegram.away_until_back"))
                from core import deputycli
                self.send("\n".join([i18n.t("telegram.away_ack" if self.back_summary() else "telegram.away_ack_plain",
                                             until=until), *deputycli.away_lines(st)]), reply_to=data["tg"]["id"])
            return
        if kind == "core.started":
            return self.back_online(st, ev, data)
        if kind == "update.checked":   # the app on a code layer: a new stable version — one message with "Update"
            due = self.offer_due(data)
            if self.update_minimum(ev, data) and due:
                # its "Update" installs the version on offer: no second message with the same button
                self.st["update_offered"] = str(data.get("version") or "")
                return
            return self.update_offer(ev, data)
        if kind == "plugins.consented":   # consent to new plugin rights: a forgery by the page code is visible too (§3.6)
            self.send(i18n.t("telegram.plugins_consented", plugin=data.get("key") or "?", via=data.get("via") or "?",
                             wider="; ".join(str(w) for w in data.get("wider") or [])),
                      head=f"🔔 <b>{html.escape(i18n.t('telegram.plugins_title'))}</b>")
            return
        if kind == "owner.notify" and data.get("bus") and ev.get("src") == "core" and self.hubs.notice(data):
            return   # linking hubs: «Linked» with the pair code, the other owner's words with «Reply»
        if kind == "owner.notify":  # `hub notify` (schedule, update, stats, watch): urgent — ⛔, the rest — 🔔
            return self.notice(ev, data)
        if kind == "owner.notify_resolved":   # the cause is gone: the notice's message gets the result
            return self.notice_resolved(ev, data)
        if kind == "owner.file":  # a session sent a file (hub send-file): the signature — from whom
            root = chain.turn_root(st, data.get("sender"))
            if root and not self.in_telegram(st, root)[0] and not self.away(st) and not self.page_results():
                return   # for his message from the page — the file is on the page (the answer where he asked; telegram.page_results — here too)
            who = data.get("sender") or i18n.t("telegram.hub")
            if data.get("rich") and self.rich_on():   # a report with pictures: one rich message (hub send-rich)
                self.send_rich(data)
                return
            self.send_file(data["path"], caption=f"{who}: {data.get('caption') or os.path.basename(data.get('source') or data['path'])}")
            return
        if kind == "turn.started" and name:
            # a message inserted in the middle of a turn (the watcher, --now) does not take the result out of the owner's thread unless it is itself from him (a
            # message from one session in the middle of another session's turn, by his words — the result went as a reply of the first one, only to the page)
            # and in the middle of a turn without a label (a background command ended — Claude Code starts a turn itself): the turn is the same
            msg = st["messages"].get(data.get("marker") or "") or {}
            keep = self.thread(st, self.st["turn"].get(name)) and (
                not data.get("marker") or msg.get("injected") and not self.thread(st, data.get("marker")))
            if not keep:
                self.st["turn"][name] = data.get("marker")
            live = self.st["live"].get(data.get("marker") or "")
            if live:
                live["turn"] = {"session": name, "ts": ev["ts"]}
            self.react(self.st["owner_msgs"].get(data.get("marker") or ""), "👀", auto=True)
            return
        if kind == "turn.result" and name in st["sessions"]:
            self.result_sent = False
            self.on_result(st, ev, name, data)
            if self.result_sent and self.group() is not None:
                self.joined(st, ev, name)
            # the turn label — only when the result went: a network failure repeats the event, and the repeat must know whose answer this is
            self.st["turn"].pop(name, None)
            # an executor's spec waits for the owner (core/specmode.awaiting: the same rule as the page's card — not in the
            # middle of a discussion): its card with the file, one per spec; a result that changes the file gets no new one
            d = specmode.open_of(st, name)
            if d and specmode.awaiting(d, st["sessions"].get(name)) and f"spec:{d['id']}" not in self.st["cards"]:
                self.spec_card(d)
            return
        if kind in BUS_CARDS:   # a ready card of the hub bus (core/buscards): shown like a form, answered by its buttons
            card = self.bus_card(st, kind, data, ev)
            if card is None:
                return
            kind, name, data = "permission.requested", card[0], card[1]
        if kind == "permission.requested" and name in st["sessions"]:
            if data.get("auto") == "allow" and ev.get("src") == "host":   # the core allowed it itself (permissions.auto_allow)
                return
            if minihub.asks_blocked(st, name, data):   # the form of a mini-hub executor — the core rejected it (bus)
                return
            if data.get("tool") == "AskUserQuestion" and self.core_ask(name, data.get("request_id")) is None \
                    and busstate.remote_form(st, st["sessions"][name]):
                return   # nobody sits at this hub's page (bus mode remote): the core refused the form, nobody waits on it
            card = self.core_ask(name, data.get("request_id"))
            if self.quiet() or card and (card.get("away") or owner.held(card, self.clock())):
                # quietly — the card waits on the page; set while the owner was away — at his return (the summary, then
                # buttons); in non-working hours (hub.json quiet_review) — in the working morning, one batch
                self.st["held"][f"ask:{data.get('request_id')}"] = {"session": name, "data": data}
                return
            if data.get("interactive") and not card and not self.open_in_core({"session": name, **data}):
                return   # the core has already answered itself (the owner is away: "question saved" — in the summary) — no buttons needed
            return self.on_ask(name, data)
        if kind == "permission.answered":
            return self.close_ask(name, data, ev["seq"], rejected=ev.get("rejected"))
        if kind == "tools.granted" and name in st["sessions"]:
            tool = tools.resolve(st, st["sessions"][name], data.get("tool"))   # a short name — as the core understood it
            req = next((r for r in reversed(st["sessions"][name].get("requests") or []) if r["tool"] == tool), None)
            if req and req["state"] == "needs-owner":
                if self.quiet() or owner.held(req, self.clock()):
                    self.st["held"][f"tool:{name}:{req['tool']}"] = {"session": name, "tool": req["tool"]}
                    return
                return self.tool_card(st, name, req)
        if kind in ("delegation.spec_accepted", "delegation.spec_dropped"):   # closed here or elsewhere: the card says so
            card = self.st["cards"].get(f"spec:{name}")
            if card:
                self.edit_done(card, i18n.t("telegram.spec_accepted_done" if kind.endswith("accepted")
                                            else "telegram.spec_dropped_done", id=name))
                self.st["cards"].pop(f"spec:{name}", None)   # after: a network failure — the repeat finds the card
            return
        if kind in ("tools.approved", "tools.denied"):
            key = f"{name}:{tools.resolve(st, st['sessions'].get(name), data.get('tool'))}"   # the card — by request id
            card = self.st["cards"].get(key)
            if card:
                self.edit_done(card, i18n.t("telegram.tool_approved" if kind == "tools.approved" else "telegram.tool_denied"),
                               route={"session": name})
                self.st["cards"].pop(key, None)   # after: a network failure — the repeat will find the card
            return
        if kind == "timer.fired" and str(ev.get("entity", "")).startswith("q:"):
            q = next((q for q in owner.questions(st) if q[0] == ev["entity"]), None)
            if q and st["owner"]["reminders"].get(q[0]) == data.get("n") and not self.quiet():   # quiet — and no reminders
                key, ask = next(((k, a) for k, a in self.st["asks"].items()
                                 if f"q:{a['session']}:{a['request_id']}" == q[0]), (None, None))
                # a reply to a reminder — as to the card itself: "own option" of the answer (a question) or a message to the session
                route = ({"ask": key, "q": 0, "session": q[1]} if ask and ask.get("questions")
                         else {"session": q[1]})
                # a paragraph per question of the card; a file:// address does not open on the phone: its name only
                body = re.sub(r"\[([^\]]+)\]\(file://[^)]*\)", r"`\1`", owner.reminder_md(st, q[0]) or coretext.human(q[3]))
                self.send(i18n.t("telegram.reminder_head", session=q[1]) + "\n\n" + body, reply_to=(ask or {}).get("msg"),
                          route=route)
            return
        if kind in ("owner.back", "timer.fired") and ev.get("entity") in (None, "owner"):
            sm = st["owner"].get("summary")
            if sm and sm.get("seq") == ev["seq"]:
                if self.back_summary():
                    self.send(summary_text(sm))
                elif data.get("channel") == "telegram" or kind == "timer.fired":   # /back from here — a reply to the command; the term
                    tg = data.get("tg") or {}                                         # "away" ended — a line that it is removed
                    self.send(i18n.t("web.cmd.back_ok") + ".", reply_to=tg.get("id"))

    def back_online(self, st, ev, data):
        """The hub was down longer than DOWN_QUIET (a failure, a reboot, a long restart) — one line when it came up: from
        what time to what time it was absent and how many messages have not yet reached the sessions (in the core queue and in
        the mailbox). A quick restart and a restart of the bot alone — without a line."""
        since = data.get("down_since")
        if not since or ev["ts"] - since <= DOWN_QUIET:
            return
        tz = (st.get("owner") or {}).get("tz") or "UTC"
        a, b = owner._local(since, tz), owner._local(ev["ts"], tz)
        form = "%H:%M" if a.date() == b.date() else "%d.%m %H:%M"
        n = sum(1 for m in st["messages"].values() if m["state"] in ("queued", "typing")) + (data.get("spooled") or 0)
        self.send(i18n.t("telegram.back_online", since=a.strftime(form), until=b.strftime(form))
                  + (i18n.t("telegram.back_waiting", count=n) if n else ""))

    def on_result(self, st, ev, name, data):
        text = reducer.shown_text(data.get("text")).strip()   # HANDOFF-DONE is for the core (it puts the session to sleep)
        sess = st["sessions"][name]
        root = ev["_root"] if "_root" in ev else sess.get("turn_root")   # the turn chain as of the result (queue)
        marker = self.st["turn"].get(name)
        asked = self.thread(st, marker)
        live = self.st["live"].get(marker or "")
        if live:
            live["done"] = True
        if minihub.boss(st, sess) and not asked:   # a result and an interruption of a mini-hub executor — only to the mini-hub (bus); a turn
            return                                 # by the owner's own word (a wake-up, a command) — to him, as usual
        if reducer.hook_refused(data):
            return   # the session hook refused the prompt — not an answer: watch_failed will say "Not delivered" with the reason
        fail = reducer.failure(data)
        if fail:
            return self.on_failure(st, ev, name, fail, asked, root)
        self.st["retry"].pop(name, None)   # the turn went through — the continuation after the limit happened
        cont = (marker or "").endswith(("x3", "x5"))   # a turn by the core's "continue" (an interruption by a restart or limit)
        if data.get("is_error") or not text or text in ("—", "-") or cont and delegations.nothing(text):
            return   # "—" is a quiet result (schedule, break), "nothing to continue" on the core's "continue": not to the owner
        # decided when the result was queued; an outbox of an older core has no `_merged`: the session's live mark then
        merged = ev.get("_merged", (sess.get("merged") or {}).get("seq") == ev["seq"])
        if merged:
            return   # a result on a task the owner gave to another session (the hub's retelling, an executor's report): that session tells him
        cwd = sess.get("cwd")
        # telegram.page_results: the core counted this result to the owner (owner_reply) — to Telegram whatever the chain began with,
        # except his own page talk with this session (own_talk); both as of the queue
        mine = ev.get("_own_talk", self.own_talk(st, name, root))
        for_owner = ev.get("_owner_reply", sess.get("owner_reply") == ev["seq"]) and not mine and self.page_results()
        if live and live.get("first") is True and text == live.get("said"):   # the result is the same line that went first: do not repeat
            self.result_sent = True   # it is in the chat already (joined)
            self.react(asked, "👍", auto=True)
            self.voice_reply(text, asked, {"session": name})
            return
        if sess.get("kind") == "hub":
            # a reply to where he asked: the turn chain started with his message from Telegram — as a reply to
            # it; from the page, from a session, from the core or the schedule without it — only on the page; away — everything here
            here, tg = self.in_telegram(st, root)
            if not (here or asked or self.away(st)):
                return
            to = asked or tg
            self.send(text, reply_to=to, route={"session": name}, cwd=cwd, fold=True)
            self.react(to, "👍", auto=True)
            self.voice_reply(text, to, {"session": name})
            return
        for d in st.get("delegations", {}).values():
            if d.get("replied_seq") == ev["seq"] and d["for"]:
                here, tg = self.in_telegram(st, chain.task_root(d))
                if not (here or self.away(st) or for_owner):
                    return   # a task for his message from the page — the answer is on the page (telegram.page_results — here too)
                to = self.st["owner_msgs"].get(d["for"]) or tg
                self.remember(name, root, to, st)
                route = {"session": name, "delegation": d["id"]}
                # a page task (telegram.page_results) has no message of his to reply to — nothing to quote: every result of it is
                # the session line with the task's words (otherwise each one would be "the first reply")
                if for_owner and not to or not self.first_reply(name, to):   # the next result of the task — the session line, not another "answer" to the question
                    words = " ".join((d.get("words") or "").split())
                    who = f"<b>{html.escape(name)}</b>"
                    if words:
                        who = i18n.t("telegram.by_task", who=who, task=html.escape(words[:40]) + ("…" if len(words) > 40 else ""))
                    return self.send(text, head=f"{who}:", route=route, cwd=cwd, fold=True)
                self.send(text, head=i18n.t("telegram.replies", name=f"<b>{html.escape(name)}</b>"), reply_to=to, route=route, cwd=cwd, fold=True)
                self.mark_quoted(name, to)
                self.react(to, "👍", auto=True)
                self.voice_reply(text, to, route)
                return
        if asked:  # the owner wrote to this session himself (a reply) or the hub passed his words (--for) — its answer to him
            self.remember(name, root, asked, st)
            self.send(text, head=f"<b>{html.escape(name)}</b>:", reply_to=asked, route={"session": name}, cwd=cwd, fold=True)
            self.mark_quoted(name, asked)
            self.react(asked, "👍", auto=True)
            self.voice_reply(text, asked, {"session": name})
            return
        # a continuation of his chain (core/chain.py): a turn for another session's message (a reply "MR is ready") of the one that has already
        # answered him in this chain — into the same thread (a link to an MR stayed on the page — "I do not see the MR").
        # A turn without a label (a watcher, /loop, a background command ended) and a schedule or `hub wait` launch in his chain — no: "the MR is not
        # merged yet" every 2 minutes is not needed by him; a session's answer and a task result (hub task new) — yes
        tg = (self.st["threads"].get(name) or {}).get(root or "")
        if tg and marker and not ((st.get("messages") or {}).get(marker) or {}).get("schedule"):
            self.send(text, head=f"<b>{html.escape(name)}</b>:", reply_to=tg, route={"session": name}, cwd=cwd, fold=True)
            self.mark_quoted(name, tg)
            self.voice_reply(text, tg, {"session": name})
            return
        if for_owner:   # a result for him from a chain that began on the page (or nobody took): a line of the session, once
            to = self.st["owner_msgs"].get(root or "")
            self.remember(name, root, to, st)
            self.send(text, head=f"<b>{html.escape(name)}</b>:", reply_to=to, route={"session": name}, cwd=cwd, fold=True)
            self.mark_quoted(name, to)
            self.voice_reply(text, to, {"session": name})

    @staticmethod
    def own_talk(st, name, root):
        """telegram.page_results: did the chain begin with his own message to this session not from Telegram (his page talk with it — he
        reads its answers there, its turns without a marker too)? A chain whose start is unknown (pruned from the state) — yes: do not guess."""
        start = (st.get("messages") or {}).get(root or "")
        return not start or bool(start.get("from_owner") and start.get("channel") != "telegram"
                                 and start.get("session", start.get("to")) == name)

    def first_reply(self, name, tg):
        """Is it the session's first reply to this message of his? It goes as a quote ("replies to your message"), its following turns
        for it (intermediate results of a mini-hub, wake-ups) — as ordinary session lines: otherwise one question of his
        gets dozens of "replies". The decision is remembered per event: a network failure repeats it with the same
        parameters (otherwise a part that has already gone would go a second time)."""
        decided = self.st["posted"].setdefault("quote", {})
        key = f"{name}\n{tg}"
        if key not in decided:
            decided[key] = tg not in (self.st["quoted"].get(name) or [])
        return decided[key]

    def mark_quoted(self, name, tg):
        """Mark after sending a reply: a network failure repeats the event with the same decision (and the same parameters)."""
        if not tg:
            return
        mine = self.st["quoted"].pop(name, [])   # moved to the end again: a session silent for long is removed, not a busy one
        if tg not in mine:
            mine.append(tg)
        self.st["quoted"][name] = mine[-20:]
        for old in list(self.st["quoted"])[:-50]:
            self.st["quoted"].pop(old)

    def thread(self, st, mid):
        """The number of his Telegram message that the reply of a turn with the label mid belongs to: his message itself or his words
        passed on (msg["re"]: `hub send --for`, a recheck) — or None."""
        return self.st["owner_msgs"].get(mid or "") or self.st["owner_msgs"].get(
            ((st.get("messages") or {}).get(mid or "") or {}).get("re") or "")

    def remember(self, name, root, tg, st=None):
        """The session replied to him in the chain root: its following turns in this chain — to the same place (the last 20 chains).
        Only his chain from this thread: a message of another chain (from the page) inserted in the middle of a turn is made by the core
        the start of a turn, while the answer went to his thread — do not link that chain to the thread."""
        if root and (st is None or self.thread(st, root) in (None, tg) and self.in_telegram(st, root)[0]):
            mine = self.st["threads"].setdefault(name, {})
            mine.pop(root, None)
            mine[root] = tg
            for old in list(mine)[:-20]:
                mine.pop(old)

    def limit_line(self, name, what, kind, at):
        """A turn interrupted by a limit stands until the reset: all accounts are busy (account.exhausted) or the core waits for the reset of the
        active one (account.waiting)."""
        why = i18n.t("telegram.limit_waiting") if kind == "account.waiting" else i18n.t("telegram.limit_all_taken")
        return i18n.t("telegram.limit_continues", who=self.who(name), what=what, why=why, at=at)

    def on_failure(self, st, ev, name, fail, asked, root=None):
        """A turn result is a failure (limit, API, login), not an answer (22 "You've hit your session limit" went as answers):
        there is no answer and no 👍. If the turn was for his message (itself or by an assignment of the hub) — one status line under it, once per
        message. A limit with Claude accounts enabled — we stay silent: the core will switch the account and continue the turn, the answer will come
        into the same thread (re / for_delegation); all busy or the core waits for the reset of the active one — we will say when it continues"
        (account.exhausted, account.waiting)."""
        to, route = asked, {"session": name}
        if not to:   # an executor by the owner's assignment: the assignment has just stopped (STOPPED) with this result
            d = next((d for d in st.get("delegations", {}).values() if d["session"] == name and d["state"] == "stopped"
                      and d.get("since") == ev["ts"] and d.get("for")), None)
            if d:
                to, route = self.st["owner_msgs"].get(d["for"]), {"session": name, "delegation": d["id"]}
        label, when = fail
        acc = st.get("account") or {}
        if st["sessions"][name].get("limit_cut") and acc.get("active"):
            waiting, cut = acc.get("waiting") or {}, st["sessions"][name]["limit_cut"]
            until, why = max(((acc.get("exhausted") or {}).get("until") or 0, "account.exhausted"),
                             ((waiting.get("account") == acc["active"] == cut.get("account") and waiting.get("until"))
                              or 0, "account.waiting"))
            if not to:   # no one's message: the core continues it by itself — nothing is lost, nothing to say
                return
            if until <= ev["ts"]:
                self.st["retry"][name] = {"tg": to, "route": route, "ts": ev["ts"]}
                return
            at = owner._local(until, st["owner"].get("tz") or "UTC").strftime("%d.%m %H:%M")
            return self.tell(to, route, self.limit_line(name, label, why, at))
        if not to:
            # the owner's own word on the page or in the CLI (root) answers on the page, as before; only a chain that began
            # with no message of his (a schedule, a session's own task) is reported here
            if not root:
                self.unasked_failure(ev, name, label, when)
            return
        if label == i18n.t(reducer.FAILURES[0][1]):
            text = i18n.t("telegram.turn_cut_reset" if when else "telegram.turn_cut_limit", who=self.who(name), label=label, when=when)
        else:
            text = i18n.t("telegram.turn_cut", who=self.who(name), label=label)
        self.tell(to, route, text)

    def in_telegram(self, st, root):
        """Did the chain start with his message from Telegram (core/chain.py)? (yes/no, the number of that message in Telegram).
        The number is from the bot's records: an answer by a button on a card leads into the card's thread."""
        tg = self.st["owner_msgs"].get(root or "")
        return bool(tg) or chain.channel(st, root) == "telegram", tg

    @staticmethod
    def away(st):
        return (st.get("owner") or {}).get("state") == "away"

    def who(self, name):
        return i18n.t("telegram.hub") if (self.reader.state["sessions"].get(name) or {}).get("kind") == "hub" else f"`{name}`"

    def unasked_failure(self, ev, name, label, when):
        """A failed turn of no human's message (a schedule, another session): ONE message for the whole hub per UNASKED_QUIET — how
        many turns failed, up to UNASKED_NAMES sessions (and "+N"), why. A common limit or an API outage fails dozens of sessions at
        once: the next failure of the hour edits the same message (an edit does not ring), the hour after it a new one starts.
        The record is written after sending: a network failure repeats the event, and a repeat is counted once (by seq)."""
        was = self.st["unasked"]
        if was.get("seq", 0) >= ev["seq"] or self.clock() - ev["ts"] > LIVE_FOR:   # a repeat, or old (the bot was down)
            return
        fresh = not was.get("mid") or ev["ts"] - was["at"] >= UNASKED_QUIET
        now = {"at": ev["ts"], "n": 1, "sessions": [name], "reasons": []} if fresh else {
            **was, "n": was["n"] + 1, "sessions": was["sessions"] + ([name] if name not in was["sessions"] else [])}
        why = i18n.t("telegram.turn_failed_why_reset", label=label, when=when) if when else label
        now["reasons"] = [*(() if fresh else was["reasons"]), *([why] if fresh or why not in was["reasons"] else [])][:UNASKED_KEEP]
        now["sessions"] = now["sessions"][:UNASKED_KEEP]
        shown = [self.who(n) for n in now["sessions"][:UNASKED_NAMES]]
        more = len(now["sessions"]) - UNASKED_NAMES
        text = i18n.t("telegram.turn_failed", count=now["n"], who=", ".join(shown) + (f" +{more}" if more > 0 else ""),
                      why="; ".join(now["reasons"][:UNASKED_WHY]) + (
                          f" +{len(now['reasons']) - UNASKED_WHY}" if len(now["reasons"]) > UNASKED_WHY else ""))
        mid = None if fresh else was["mid"]
        if mid:
            try:
                self.api.call("editMessageText", chat_id=self.chat(), message_id=mid, parse_mode="HTML", disable_web_page_preview=True,
                              text=self.linked(fmt.to_messages(text))[0])
            except TgError as e:
                if e.transient:
                    raise
                if "not modified" not in str(e).lower():   # the message is gone or too old: a new one with the same count
                    mid = None
        if not mid:
            ids = self.send(text)
            if not ids:   # no chat to write to: nothing was said, so nothing is counted
                return
            mid = ids[0]
        self.st["unasked"] = {**now, "mid": mid, "seq": ev["seq"]}

    def tell(self, to, route, text):
        """A status line under his message — no more than one per message."""
        if to in self.st["told"]:
            return
        self.send(text, reply_to=to, route=route)
        self.st["told"] = self.st["told"][-199:] + [to]   # after sending: a network failure — the repeat will say it

    @staticmethod
    def bus_card(st, kind, data, ev):
        """(session, form data) of the bus card a core.bus_* fact put on a session (the liaison), or None."""
        rid = {"core.bus_send_held": "bus-" + str(data.get("out")), "core.bus_received": f"bus-b{ev.get('seq')}",
               "core.bus_intro": "bus-i" + str(data.get("fp") or "")[4:16],
               "core.bus_offer": "bus-f" + str(data.get("id") or "")[:16]}.get(kind)
        for sname, sess in st["sessions"].items():
            for a in sess.get("asks") or []:
                if a.get("request_id") == rid and a.get("hubbus"):
                    return sname, {"request_id": rid, "tool": "AskUserQuestion", "input": a.get("input") or {},
                                   "interactive": True}
        return None

    def on_ask(self, name, data):
        rid, tool, inp = data.get("request_id"), data.get("tool"), data.get("input") or {}
        ask_id = self.token()
        for key, live in self.st["live"].items():   # a card from the one who has his message — is also an answer to him
            if name in self.holders(key):
                live["answered"] = True
        if tool == "AskUserQuestion":
            qs = inp.get("questions") or []
            ask = self.st["asks"][ask_id] = {"session": name, "request_id": rid, "questions": [q.get("question", "") for q in qs],
                                             "answers": {}, "msgs": [], "labels": {}, "multi": {}, "picked": {}}
            # paths to plans and specs in the question and options — links into "Files"
            cwd = (self.reader.state["sessions"].get(name) or {}).get("cwd")
            linked = lambda s: fmt.link_text(fmt._inline(s), self.files_url(), cwd, session=name)  # noqa: E731
            for qi, q in enumerate(qs):
                ask["labels"][str(qi)] = [o.get("label", "") for o in q.get("options") or []]
                self.option_previews(name, q)   # before the question: the buttons stay the last thing in the chat
                if q.get("multiSelect"):  # several options: buttons mark, "Done" answers
                    ask["multi"][str(qi)], ask["picked"][str(qi)] = True, []
                rows = self.ask_rows(ask_id, qi, ask)
                opts = "\n".join(f"• <b>{fmt._inline(o.get('label', ''))}</b> — {linked(o.get('description', ''))}"
                                 for o in q.get("options") or [] if o.get("description"))
                ids = self.send(q.get("question", "") + ("\n\n<i>" + i18n.t("telegram.ask_multi_hint") + "</i>"
                                                         if q.get("multiSelect") else ""),
                                head=i18n.t("telegram.asks", name=f"<b>{html.escape(name)}</b>"), buttons=rows,
                                route={"ask": ask_id, "q": qi, "session": name}, cwd=cwd)
                ask.setdefault("qmsg", {})[str(qi)] = ids[-1] if ids else None
                ask["msgs"] += ids
                if opts and ids:
                    text = (i18n.t("telegram.asks", name=f"<b>{html.escape(name)}</b>") + f"\n{linked(q.get('question', ''))}\n\n{opts}\n\n"
                            + ("<i>" + i18n.t("telegram.ask_multi_hint") + " </i>" if q.get("multiSelect") else "")
                            + "<i>" + i18n.t("telegram.ask_own") + "</i>")
                    try:   # option descriptions; the card works without them too
                        self.api.call("editMessageText", chat_id=self.chat(), message_id=ids[-1], parse_mode="HTML",
                                      text=text, reply_markup={"inline_keyboard": rows})
                        self.st["card_html"][str(ids[-1])] = text   # the result of the closed one will be appended to this text
                    except TgError as e:
                        if e.transient:
                            raise   # the event will repeat as a whole; once() will not send the cards that have gone again
                        sys.stderr.write(f"telegram: the option descriptions of the question {name} were not shown: {e}\n")
            ask["msg"] = ask["msgs"][0] if ask["msgs"] else None
            return
        preview = json.dumps(inp, ensure_ascii=False)
        if len(preview) > PREVIEW_CHARS:   # the dangerous part may stand after the cut: say that there is more
            preview = preview[:PREVIEW_CHARS] + i18n.t("telegram.perm_cut", count=len(preview) - PREVIEW_CHARS)
        ask = self.st["asks"][ask_id] = {"session": name, "request_id": rid, "questions": [], "answers": {}, "msgs": []}
        rows = [[{"text": i18n.t("telegram.perm_allow"), "callback_data": self.key({"do": "perm", "ask": ask_id, "behavior": "allow"})},
                 {"text": i18n.t("telegram.perm_deny"), "callback_data": self.key({"do": "perm", "ask": ask_id, "behavior": "deny"})}]]
        why = "\n<i>" + i18n.t("telegram.perm_why", why=html.escape(data["why"])) + "</i>" if data.get("why") else ""
        head = i18n.t("telegram.perm_head", name=f"<b>{html.escape(name)}</b>", tool=f"<code>{html.escape(str(tool))}</code>")
        ask["msgs"] = self.send(f"```\n{preview}\n```", head=head + why, buttons=rows, route={"session": name})
        ask["msg"] = ask["msgs"][0] if ask["msgs"] else None

    def option_previews(self, name, q):
        """Options with a `preview` (a variant is judged by sight): an ASCII sketch — a monospace block, a path
        to a screenshot — a photo with the option's name; one the session did not show or that is gone — a line."""
        for o in q.get("options") or []:
            raw = o.get("preview")
            if not isinstance(raw, str) or not raw.strip():
                continue
            label = str(o.get("label", ""))
            head = f"🖼 <b>{html.escape(label[:OPTION_LABEL])}</b>"   # a photo caption holds 1024 characters
            path = shown.preview_image(raw)
            if path is None:
                text = raw.strip("\n").replace("```", "ʼʼʼ")   # a fence inside would close ours
                if len(text) > OPTION_PREVIEW:   # never past a message: cut at a line end and say so
                    text = text[:OPTION_PREVIEW]
                    text = (text[:text.rfind("\n")] if "\n" in text else text) + "\n…"
                # Markdown only: the sketch is one code block, so Telegram shows it literally; the page does the same with textContent
                self.send(f"```\n{text}\n```", head=head, route={"session": name})
                continue
            real = self.shown_index().servable(path)
            if real and real.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
                self.send_file(real, caption=fmt.plain(head))
            else:
                self.send(i18n.t("telegram.preview_missing", path=path), head=head, route={"session": name})

    def shown_index(self):
        """What the owner may be shown from disk: the same rule as the page's /api/file."""
        if getattr(self, "_shown", None) is None:
            self._shown = shown.Index(self.state_dir)
        return self._shown

    def ask_rows(self, ask_id, qi, ask):
        """Question buttons: an option is the answer; with multiSelect — the mark ☐/☑ and "Done"."""
        labels = ask["labels"][str(qi)]
        # "Need more context" — at every question, answers at once
        more = [] if owner.more_forms() & set(labels) else [[{"text": i18n.t("core.owner.more_context"), "callback_data": self.key(
            {"do": "answer", "ask": ask_id, "q": qi, "label": i18n.t("core.owner.more_context")})}]]
        if str(qi) not in ask["multi"]:
            return [[{"text": lb[:60], "callback_data": self.key({"do": "answer", "ask": ask_id, "q": qi, "label": lb})}]
                    for lb in labels] + more
        picked = ask["picked"][str(qi)]
        rows = [[{"text": f"{'☑' if lb in picked else '☐'} {lb}"[:62],
                  "callback_data": self.key({"do": "pick", "ask": ask_id, "q": qi, "label": lb})}] for lb in labels]
        return rows + [[{"text": i18n.t("telegram.done_count", count=len(picked)) if picked else i18n.t("telegram.done"),
                         "callback_data": self.key({"do": "picked", "ask": ask_id, "q": qi})}]] + more

    def sweep_asks(self, now=None):
        """A hub question — a card with a deadline: it is removed both by a text answer to the hub and by the deadline — without an answer event for the
        form. The question is gone but the buttons hang alive — a press would go nowhere: remove them."""
        if self.outbox:   # an event with the card may wait for a repeat: remove the half-sent one — the repeat would recreate it
            return
        for ask in self.st["asks"].values():
            if not ask.get("done") and not self.open_in_core(ask, now):
                try:
                    self.close_card(ask, i18n.t("telegram.ask_closed"))
                except TgError as e:   # only network ones: close_card swallows the rest itself
                    sys.stderr.write(f"telegram: removing the question {ask['request_id']}: {e} — will finish later\n")
                    return
                ask["done"] = True

    def core_ask(self, name, rid):
        """The question card in the core (sessions[*].asks) by request_id, or None."""
        sess = self.reader.state["sessions"].get(name) or {}
        return next((a for a in sess.get("asks") or [] if a.get("request_id") == rid), None)

    def open_in_core(self, ask, now=None):
        """The card's question is still open in the core (a session waits for the answer or it hangs as a card)."""
        sess = self.reader.state["sessions"].get(ask["session"]) or {}
        live = (sess.get("pending") or []) + owner.open_asks(sess, time.time() if now is None else now)
        return any(r["request_id"] == ask["request_id"] for r in live)

    def close_ask(self, name, data, seq=None, rejected=False):
        for ask_id, ask in list(self.st["asks"].items()):
            if ask["session"] == name and ask["request_id"] == data.get("request_id"):
                if rejected:
                    # the core rejected the answer (not by the one entitled) — the card stays; if a press here is rejected too —
                    # the buttons answer again (a press sets done before the core decides)
                    if data.get("channel") == "telegram":
                        ask["done"] = False
                    continue
                if data.get("channel") == "telegram" and seq and ask["msgs"]:
                    # the core will deliver the answer to the session as a message m<seq>: the result of its turn — here, as a reply to the card
                    # ("need more context" — the context went in the turn result, but did not reach Telegram)
                    self.st["owner_msgs"][f"m{seq}"] = ask["msgs"][-1]
                ans = data.get("answers") or {}
                what = ", ".join(ans.values()) if ans else i18n.t("telegram.answer_allowed" if data.get("behavior") == "allow"
                                                                  else "telegram.answer_denied")
                via = {"web": i18n.t("telegram.via_web"), "cli": i18n.t("telegram.via_cli")}.get(data.get("channel"), "")
                if isinstance(data.get("tg_by"), dict) and data["tg_by"].get("name"):   # the group: which admin answered
                    via = i18n.t("telegram.group.via", name=str(data["tg_by"]["name"])[:80])
                says = lambda a: i18n.t("telegram.answer", answer=a, via=via)  # noqa: E731
                own = {}   # a form: each question's card gets its own answer, not the whole form's (it read as "one answer on both cards")
                if len(ask["questions"]) > 1 and ans:
                    for qi, q in enumerate(ask["questions"]):
                        mid = (ask.get("qmsg") or {}).get(str(qi))
                        if mid and q in ans:
                            own[mid] = says(ans[q])
                self.close_card(ask, says(what), own)
                ask["done"] = True
                self.st["asks"].pop(ask_id, None)

    def quiet(self):
        """The setting "do not send questions and tool requests" (telegram.quiet in hub.json): the owner works on the page —
        cards wait there, none in Telegram."""
        now = self.clock()
        if now - self._quiet[0] >= 5:   # hub.json — no more than once per 5 s: the bot ticks about every 2 s
            try:
                on = ((self.hub_config() or {}).get("telegram") or {}).get("quiet") is True
            except Exception:   # noqa: BLE001 — hub.json is unreadable: we send, as before
                on = False
            self._quiet = (now, on)
        return self._quiet[1]

    def release_held(self):
        """Deferred things that are already closed in the core — remove; the setting turned off or the owner returned (a question asked without
        him — a card after the summary) — the open ones come now. A network failure —
        the record stays and goes with the next tick (like flush), what was sent is no longer in held."""
        if not self.st["held"] or self.outbox or self.clock() < self.hold_until:
            return
        st, quiet, now = self.reader.state, self.quiet(), self.clock()
        due = []
        for key, h in list(self.st["held"].items()):
            sess = st["sessions"].get(h["session"]) or {}
            ask = key.startswith("ask:")
            if ask:
                item = self.core_ask(h["session"], h["data"].get("request_id")) or {}
                live = self.open_in_core({"session": h["session"], "request_id": h["data"].get("request_id")}, now)
            else:
                item = next((r for r in reversed(sess.get("requests") or []) if r["tool"] == h["tool"]), None) or {}
                live = item.get("state") == "needs-owner"
            if not live:
                self.st["held"].pop(key)
                continue
            if quiet or ask and owner.waits_return(item) or owner.held(item, now):
                continue   # asked without the owner — will come on his return, after the summary; non-working hours — in the morning
            due.append((key, h, ask, item))
        morning = [item["held_until"] for _, _, _, item in due if item.get("held_until")]
        if morning and self.st.get("review_told") != max(morning):
            # what piled up in non-working hours (quiet_review) comes as one batch: first a line how many, then the cards
            try:
                self.send(i18n.t("telegram.review_batch", count=len(morning)))
            except TgError as e:
                if e.transient:
                    self.fails += 1
                    self.hold_until = self.clock() + (e.retry_after or min(60, 2 ** min(self.fails, 6)))
                    return
            self.st["review_told"] = max(morning)
        for key, h, ask, item in due:
            req = item
            try:
                self.on_ask(h["session"], h["data"]) if ask else self.tool_card(st, h["session"], req)
            except TgError as e:
                if e.transient:   # like flush: wait and repeat
                    self.fails += 1
                    self.hold_until = self.clock() + (e.retry_after or min(60, 2 ** min(self.fails, 6)))
                    sys.stderr.write(f"telegram: held {key}: {e} — will retry later\n")
                    return
                sys.stderr.write(f"telegram: held {key}: {e}\n")
            except Exception:  # noqa: BLE001 — as in flush: a bot error on one record does not hold the rest
                sys.stderr.write(f"telegram: held {key}: bot error — skipping\n{traceback.format_exc()}")
            self.fails = 0
            self.st["held"].pop(key)

    def tool_card(self, st, name, req):
        info = st["catalog"].get(req["tool"]) or {}
        risk = ", ".join(info.get("risk") or [])
        runs = "; ".join([*(info.get("runs") or []), *([f"+{info['runs_more']}"] if info.get("runs_more") else [])])
        rows = [[{"text": i18n.t("telegram.tool_connect"), "callback_data": self.key({"do": "tool", "session": name, "tool": req["tool"], "yes": True})},
                 {"text": i18n.t("telegram.tool_refuse"), "callback_data": self.key({"do": "tool", "session": name, "tool": req["tool"], "yes": False})}]]
        head = i18n.t("telegram.tool_head", tool=f"<b>{html.escape(req['tool'])}</b>", name=f"<b>{html.escape(name)}</b>")
        ids = self.send(req.get("why") or "", head=head + (i18n.t("telegram.tool_risk", risk=risk) if risk else "")
                        + (i18n.t("telegram.tool_runs", list=html.escape(runs)) if runs else ""),
                        buttons=rows, route={"session": name})
        if ids:
            self.st["cards"][f"{name}:{req['tool']}"] = ids[-1]   # a message with buttons: the result goes there too

    @staticmethod
    def button_msgs(ask):
        """The messages of a card with buttons — where its result is written: for a form — the message of each question (qmsg), for a
        permission — the last one (a long card is split, the buttons are on the last part)."""
        got = [m for m in (ask.get("qmsg") or {}).values() if m]
        return got or [m for m in ask.get("msgs") or [] if m][-1:]

    def close_card(self, ask, note, own=None):
        """The question is closed: the result goes into the messages with buttons (the start of a long card does not carry them), the buttons are removed. With
        telegram.drop_answered in hub.json the card is erased entirely: the result line only repeats the choice made by
        the owner, while the chat accumulates closed questions. Telegram did not allow erasing (older than 48 h) — the result in the card, as without
        the setting. A network failure — outward; what is left (closing) is finished by the repeat: what is done is not touched a second time."""
        drop = self.drop_answered()
        buttons = self.button_msgs(ask)
        left = ask.setdefault("closing", [m for m in ask.get("msgs") or [] if m] if drop else buttons)
        while left:
            mid = left[0]
            if drop:
                try:
                    self.api.call("deleteMessage", chat_id=self.chat(), message_id=mid)
                    mid = None
                except TgError as e:
                    if e.transient:
                        raise
            if mid is not None and mid in buttons:
                self.edit_done(mid, (own or {}).get(mid, note), route={"session": ask["session"]})
            left.pop(0)
        ask.pop("closing")

    def drop_answered(self):
        """The setting "erase answered questions" (telegram.drop_answered in hub.json, off by default)."""
        try:
            return ((self.hub_config() or {}).get("telegram") or {}).get("drop_answered") is True
        except Exception:   # noqa: BLE001 — hub.json is unreadable: as before
            return False

    def notice(self, ev, data):
        """`hub notify`. A repeat with the same --id inside the notice's life (the core keeps it a day from its last update) edits the
        message of the earlier one - nine "3 of 9" lines are one message that changes. The occurrence is judged by the events the bot
        reads itself, in order (not by the reader state, which may have moved ahead of a late send): the removal (notice_resolved)
        and a pause of NOTICE_TTL close the binding, a repeat after that is a new message. The binding is the bot's own state (st["posted"]
        lives for one event). Not edited, a new message instead: nothing bound, a file with the notice, a text that is not one
        message, an edit Telegram refused (the message was deleted or is too old). A network failure goes out: the event repeats."""
        title = html.escape(data.get("title") or data.get("source") or i18n.t("telegram.monitor"))
        head = f"{'⛔' if data.get('urgent') else '🔔'} <b>{title}</b>"
        md = data.get("text") or ""
        if data.get("session"):   # whom it is about (the name in `code` is a link to its tab)
            md += "\n\n" + i18n.t("telegram.notice_from", name=str(data["session"]))
        key, notices, urgent = owner.notice_key(ev, data), self.st["notices"], bool(data.get("urgent"))
        bound = notices.get(key) if key else None   # replaced only when the event is done: a network failure repeats it
        if bound and (ev["ts"] - bound["at"] >= owner.NOTICE_TTL
                      or urgent and not bound.get("urgent")):   # an escalation to ⛔ must ring: an edit gives no sound
            bound = None
        parts = self.linked(fmt.to_messages(md, head=head))
        one = parts[0] if len(parts) == 1 and "```mermaid" not in md and self.fits(parts[0]) and not data.get("file") else None
        rows = self.ready_buttons(ev, data)
        if bound and one and self.chat() is not None:
            try:
                self.api.call("editMessageText", chat_id=self.chat(), message_id=bound["mid"], text=one,
                              parse_mode="HTML", disable_web_page_preview=True,
                              **({"reply_markup": json.dumps({"inline_keyboard": rows})} if rows else {}))
                done = True
            except TgError as e:
                if e.transient:
                    raise
                done = "not modified" in str(e).lower()   # a repeat of the event or the same text: already so
            if done:
                notices.pop(key, None)   # back to the end: the oldest is dropped first
                notices[key] = {"mid": bound["mid"], "at": ev["ts"], "urgent": urgent}
                self.st["card_html"][str(bound["mid"])] = one
                return
        ids = self.send(md, head=head, buttons=rows)
        if key:
            notices.pop(key, None)
        if key and one and ids:
            notices[key] = {"mid": ids[0], "at": ev["ts"], "urgent": urgent}
            self.st["card_html"][str(ids[0])] = one   # the result of the removal is appended to this text (edit_done)
        if data.get("file"):
            self.send_file(data["file"])

    def notice_resolved(self, ev, data):
        """The notice is withdrawn: the result goes into its message ("✅" or the sender's text); the binding is closed. The bot
        never showed it (or a day passed) - nothing to say. The edit did not pass - a line as a reply (edit_done)."""
        key = owner.notice_key(ev, data)
        bound = self.st["notices"].get(key) if key else None
        if not bound:
            return
        if ev["ts"] - bound["at"] < owner.NOTICE_TTL:
            said = " ".join(str(data.get("text") or "").split())[:500]
            self.edit_done(bound["mid"], f"✅ {said}" if said else i18n.t("telegram.notice_resolved"))
        self.st["notices"].pop(key, None)

    @staticmethod
    def fits(html_text):
        """One Telegram message holds 4096 characters of visible text (after markup parsing), not HTML with links."""
        return len(html.unescape(re.sub(r"<[^>]+>", "", html_text))) <= 4096

    def edit_done(self, message_id, note, route=None):
        """The question is closed (the answer may have come from the page or CLI): the result goes into the card itself, the buttons are removed by the same
        edit, without a new message (~130 a day of "✅ Answer" and "⌛ Question removed" as separate lines). The card text
        is missing (sent by an earlier version), the edit did not pass or is longer than the Telegram limit — as before: remove the buttons and
        the result line as a reply; route — where a reply to it goes: to the session that asked, not the hub. A network failure —
        outward: the event will repeat (the same edit — "message is not modified" means already done; once will not send the
        line twice; the removal of questions will finish on the next pass), other errors are no trouble."""
        card = self.st["card_html"].get(str(message_id))
        text = f"{card}\n\n<b>{html.escape(note)}</b>" if card else None
        # the Telegram limit is 4096 characters of visible text (after markup parsing), not HTML with links
        if text and self.fits(text):
            try:
                self.api.call("editMessageText", chat_id=self.chat(), message_id=message_id, text=text,
                              parse_mode="HTML", disable_web_page_preview=True, reply_markup={"inline_keyboard": []})
                return   # we do not erase the text: a repeat of the event (a failure on another question of the form) edits the same way; prune will remove
            except TgError as e:
                if e.transient:
                    raise
                if "not modified" in str(e).lower():   # a repeat of the event: the card is already closed by this edit
                    return
        try:
            self.api.call("editMessageReplyMarkup", chat_id=self.chat(), message_id=message_id,
                          reply_markup={"inline_keyboard": []})
        except TgError as e:   # a repeat edits buttons that are already removed — "message is not modified": no trouble
            if e.transient:
                raise
        params = {**self.where(message_id), "text": note, "disable_notification": True,
                  "reply_parameters": {"message_id": message_id, "allow_sending_without_reply": True}}
        try:
            res = self.once(("sendMessage", params), lambda: self.call("sendMessage", **params))
        except TgError as e:
            if e.transient:
                raise
            return
        if route and isinstance(res, dict) and res.get("message_id"):
            self.st["routes"][str(res["message_id"])] = route

    def watch_failed(self):
        msgs = self.reader.state["messages"]
        for mid, tg_id in self.st["owner_msgs"].items():
            m = msgs.get(mid)
            if m and m["state"] == "failed" and mid not in self.st["failed"]:
                try:
                    if m.get("reason") == "removed by owner":  # removed by the owner himself — not a failure, a quiet mark
                        self.call("sendMessage", **self.where(tg_id), text=i18n.t("telegram.dequeued"), disable_notification=True,
                                      reply_parameters={"message_id": tg_id, "allow_sending_without_reply": True})
                    else:
                        self.react(tg_id, "👎")
                        why = (m.get("reason") or i18n.t("telegram.no_session")) + (f": {m['hook']}" if m.get("hook") else "")
                        self.send(i18n.t("telegram.not_delivered", why=why), reply_to=tg_id)
                except TgError as e:
                    # a network failure flew out of tick() and crashed the bot; the "done" mark — only when it went
                    sys.stderr.write(f"telegram: \"not delivered\" {mid}: {e}\n")
                    if e.transient:
                        return   # the rest — on the next pass
                self.st["failed"].append(mid)


def status(cfg):
    """`hub-telegram status`: the token, the owners, open codes; with a group — the group, its admins as the bot last saw
    them, the topics bound to sessions and what is off in the group."""
    out = {"token": bool(cfg.get("token")), "owners": cfg.get("owners") or [], "pending": len(live_codes(cfg, time.time()))}
    g = group_of(cfg)
    if g:
        try:
            with open(os.path.join(default_state_dir(), "telegram", "state.json"), encoding="utf-8") as f:
                st = json.load(f)
        except (OSError, ValueError):
            st = {}
        out["group"] = {**g, "admins": list((st.get("admins") or {}).values()), "topics": st.get("topics") or {},
                        "off": [i18n.t("telegram.group.off_private"), i18n.t("telegram.group.off_business")]}
        if st.get("promote_told"):
            out["group"]["warning"] = i18n.t("telegram.group.bot_promotes")
    return out


def summary_text(sm):
    def sec(title, rows):
        # a question with the core's words ("allow X?") keeps their marks (owner.came_back): in the bot's language
        return f"**{title}**\n" + ("\n".join(f"- {r.get('session')}: {coretext.human(coretext.got(r, 'text'))}"
                                            + (f" · {r['card']}" if r.get("card") else "")
                                            for r in rows) if rows else i18n.t("telegram.summary_none"))
    # the hub's answers to cards for him (spec d2549241): first, and only when there are some (older summaries have none)
    hub = [sec(i18n.t("telegram.summary_hub"), sm["hub"])] if sm.get("hub") else []
    # the deputies (spec d2679763 A10): what they decided, and the cards at them with no answer — when there are some
    hub += [sec(i18n.t("telegram.summary_deputies"), sm["deputies"])] if sm.get("deputies") else []
    hub += [sec(i18n.t("telegram.summary_deputies_waiting"), sm["deputies_waiting"])] \
        if sm.get("deputies_waiting") else []
    return "\n\n".join([i18n.t("telegram.summary_head"), *hub,
                        sec(i18n.t("telegram.summary_waiting"), sm.get("waiting") or []),
                        sec(i18n.t("telegram.summary_decided"), sm.get("decided") or []),
                        sec(i18n.t("telegram.summary_done"), sm.get("done") or [])])


# --- commands ---------------------------------------------------------------------------------------------------------

def serve():
    from core import lock
    state_dir = default_state_dir()
    if not lock.hold(os.path.join(state_dir, "telegram.lock")):
        sys.exit(i18n.t("telegram.already_running"))
    cfg = load_config()
    if not cfg.get("token"):
        sys.stderr.write(i18n.t("telegram.waiting_token") + "\n")
    while not cfg.get("token"):  # the service stands quiet until there is a token, instead of crashing in a loop
        time.sleep(10)
        cfg = load_config()
    bot = Bot(state_dir, Api(cfg["token"]))
    bot.register_commands()
    bot.catch_up()
    while True:
        bot.cfg = load_config()  # the owner binding — without a restart
        bot.poll(timeout=2)
        bot.drain_voice()
        bot.tick()


def pair(code, yes=False):
    """Bind as the owner the one who asked for the code, and name him (name and id): a stranger's code is visible at once.
    A group's code binds the bot to that group (pair_group)."""
    cfg = load_config()
    entry = live_codes(cfg, time.time()).get(code)
    if entry is None:
        sys.exit(i18n.t("telegram.pair_unknown"))
    if entry.get("chat"):
        return pair_group(cfg, code, entry, yes)
    cfg["owners"] = sorted(set(cfg.get("owners") or []) | {entry["user"]})
    cfg["pending"] = {}
    save_config(cfg)
    Api(cfg["token"]).call("sendMessage", chat_id=entry["user"], text=i18n.t("telegram.paired_message"))
    print(i18n.t("telegram.pair_done", name=entry.get("name") or i18n.t("telegram.no_name"), id=entry["user"]))


class NoBind(Exception):
    """The group cannot be bound now: the line says why (no admins' list, the bot may appoint admins, it is blind)."""


def group_facts(api, chat):
    """Who is who in a group about to be bound: {creator, admins, appoint} (names), or NoBind."""
    try:
        rows = api.call("getChatAdministrators", chat_id=chat)
        me = api.call("getMe")
    except TgError as e:
        raise NoBind(i18n.t("telegram.group.pair_no_admins", error=e)) from None
    people = [r for r in rows if isinstance(r, dict) and not (r.get("user") or {}).get("is_bot")] if isinstance(rows, list) else []
    mine = next((r for r in rows or [] if isinstance(r, dict) and (r.get("user") or {}).get("id") == me.get("id")), None)
    if mine and mine.get("can_promote_members") is True:
        raise NoBind(i18n.t("telegram.group.fix_promote"))
    if not (mine or me.get("can_read_all_group_messages")):
        raise NoBind(i18n.t("telegram.group.fix_privacy"))
    name = lambda r: display_name(r.get("user") or {})  # noqa: E731
    return {"creator": next((name(r) for r in people if r.get("status") == "creator"), "—"),
            "admins": [name(r) for r in people if r.get("status") in ("creator", "administrator")],
            "appoint": [name(r) for r in people if r.get("status") == "creator" or r.get("can_promote_members") is True]}


def bind_group(api, code, entry, facts):
    """Write the group into the settings and tell it; the bot itself tells the old chat where it went (Bot.follow_chat).
    A line on stdout if the group was not told (the terminal's; the bot's log)."""
    chat, admins = entry["chat"], ", ".join(facts["admins"]) or "—"
    cfg = load_config()   # while we asked, the bot could have written the file (a code) — do not overwrite
    cfg["group"] = {"id": chat, "title": entry.get("title") or ""}
    drop_code(cfg, code, time.time())   # the others' codes stay
    save_config(cfg)
    try:
        api.call("sendMessage", chat_id=chat, text=i18n.t("telegram.group.bound", admins=admins))
    except TgError as e:
        print(i18n.t("telegram.group.bound_unsaid", error=e))


def pair_group(cfg, code, entry, yes):
    """Bind the bot to a group (spec d2513429): first show — the group, its creator, its admins (they get the owner's
    rights to the hub), who may appoint admins, what turns off — and ask; then write. Without the admins' list nothing
    is written."""
    api, chat = Api(cfg["token"]), entry["chat"]
    try:
        facts = group_facts(api, chat)
    except NoBind as e:
        sys.exit(str(e))
    print(i18n.t("telegram.group.pair_show", title=entry.get("title") or chat, creator=facts["creator"],
                 admins=", ".join(facts["admins"]) or "—", appoint=", ".join(facts["appoint"]) or "—"))
    if not yes:
        if not sys.stdin.isatty():
            sys.exit(i18n.t("telegram.group.pair_terminal"))
        if input(i18n.t("telegram.group.pair_ask")).strip().lower() not in ("y", "yes", i18n.t("telegram.group.yes_word")):
            sys.exit(i18n.t("telegram.group.pair_kept"))
    bind_group(api, code, entry, facts)
    print(i18n.t("telegram.group.pair_done", title=entry.get("title") or chat))


def group_off():
    """Back to the owner's private chat: the group is unbound (the bot tells it so itself)."""
    cfg = load_config()
    if not group_of(cfg):
        sys.exit(i18n.t("telegram.group.none"))
    cfg.pop("group")
    save_config(cfg)
    print(i18n.t("telegram.group.off_done"))


def owner_only(what):
    """pair and token decide whom the bot obeys: the owner's word in a terminal, not a session's or a plugin's (HUB_SESSION is
    in the environment of everything a session starts; HUB_EXTENSION and HUB_MODULE — of plugin and core module processes)."""
    if any(os.environ.get(v) for v in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE")):
        sys.exit(i18n.t("telegram.owner_only", what=what))


def main(argv):
    if argv[:1] in (["token"], ["pair"], ["group"]):
        owner_only("hub-telegram " + argv[0])
    if argv[:1] == ["pair"] and os.environ.get("HUB_SCHEDULE_JOB"):   # a job runs later, with nobody to see whom it binds
        sys.exit(i18n.t("telegram.group.pair_terminal"))
    cfg = load_config()
    if argv[:1] == ["serve"]:
        serve()
    elif argv[:1] == ["token"]:
        token = (getpass.getpass(i18n.t("telegram.token_prompt")) if sys.stdin.isatty()   # in a terminal — without echo, not `< file`
                 else sys.stdin.readline()).strip()
        if ":" not in token:
            sys.exit(i18n.t("telegram.not_a_token"))
        from core import instance
        holder = instance.bot_holder(token, config_path())   # several hubs on one machine: one bot — one hub
        if holder:
            sys.exit(i18n.t("telegram.token_taken", hub=holder))
        me = Api(token).call("getMe")
        cfg = load_config()   # while the request ran, the bot could have written the file (a pairing code) — do not overwrite
        cfg["token"] = token
        save_config(cfg)
        print(i18n.t("telegram.bot_saved", username=me.get("username")))
    elif argv[:1] == ["pair"] and len(argv) == 2:
        pair(argv[1])
    elif argv[:1] == ["pair"] and len(argv) == 3 and "--yes" in argv[1:]:
        # without the question only in the owner's terminal: not a session's step (owner_only), not a schedule job's
        # command (above), not a pipe — whom the bot obeys is his word
        if not sys.stdin.isatty():
            sys.exit(i18n.t("telegram.group.pair_terminal"))
        pair(next(a for a in argv[1:] if a != "--yes"), yes=True)
    elif argv[:2] == ["group", "off"]:
        group_off()
    elif argv[:1] == ["status"]:
        print(json.dumps(status(cfg), ensure_ascii=False))
    else:
        sys.exit(__doc__.split("\n\n")[0] + "\n\n  hub-telegram token < file | pair <code> [--yes] | group off | status | serve")


if __name__ == "__main__":
    main(sys.argv[1:])
