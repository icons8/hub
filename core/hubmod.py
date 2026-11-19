"""Helper for a core module in Python (Module API, spec §2.1; EXTENDING.md, "Core modules"): a module is functions with
decorators, while the server, the secret check, hello from module.json, checking answers against the point schema and
facts into the core inbox live here. A module in another language does the same itself.

    from core import hubmod

    @hubmod.hook("doctor")                       # a core point: POST /hook/doctor
    def doctor(req):
        return [{"name": "database", "ok": True}]

    @hubmod.route("/items", methods=["GET", "POST"])   # a page route: /api/mod/<name>/items (capability http)
    def items(req):
        if req.method == "POST":
            hubmod.state.set("items", req.json())      # own state: the fact mod.<name>.set
        return {"items": hubmod.state.get("items", [])}

    @hubmod.view("/")                            # an HTML view: /mod/<name>/view/
    def page(req):
        return "<!doctype html>…" + hubmod.VIEW_JS

    if __name__ == "__main__":
        hubmod.run()

hub-up starts the module process with PYTHONPATH on the core folder, HUB_STATE_DIR, HUB_MODULE (the name), HUB_MODULE_PORT
and HUB_MODULE_SECRET (new on every launch); it must listen only on 127.0.0.1 and only with this secret — every process
on the machine sees the port. The old form — `hubmod.serve({point: function})` — works as before.
"""
import copy
import hmac
import json
import os
import queue
import re
import sys
import tempfile
import threading
import time
import traceback
import uuid
from collections.abc import Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, unquote, urlsplit

from core.journal import NOT_DECIDED  # noqa: F401 — the reply to the client when owner(wait=) returned (None, None)
from core import nolookup
from core import net  # noqa: F401 — hubmod.model calls the page: to itself bypassing the machine proxy (tests/test_net.py)

MAX_BODY = 16 << 20   # a core point's and a route's body in memory; more — a body="file" route
CHUNK = 64 << 10
PING = 20             # a stream with no events for this many seconds gets a ping (the page closes a silent one at 60 s)
MAX_WAIT = 30         # owner(wait=): longer, the route would hold the client (the page cuts a silent reply after 60 s)
READY_EVERY = (0.5, 5)   # @on_ready: check the core's "ready in this launch" record with this pause, growing up to the second
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")

_hooks, _routes, _ready = {}, [], []


def name():
    return os.environ.get("HUB_MODULE") or ""


def state_dir():
    return os.environ["HUB_STATE_DIR"]


def data_dir():
    """The module's data folder — <state>/moddata/<name>/ (0700, created on call): caches, indexes, its own files.
    Not the code folder (an edit there in developer mode would restart the module) and not the journal; removing the
    module does not touch it."""
    from core import modules
    mod = name()
    if not modules.NAME.fullmatch(mod):
        raise RuntimeError(f"the module name \"{mod}\" is not fit for a folder — was the process started outside hub-up?")
    path = os.path.join(state_dir(), "moddata", mod)
    import platforms
    platforms.current.makedirs_private(path)
    os.chmod(path, 0o700)   # it already existed with other permissions — set our own; on Windows chmod only touches "read-only"
    return path


_shown, _shown_lock = None, threading.Lock()


def _index():
    global _shown
    from core import shown
    with _shown_lock:
        if _shown is None:
            _shown = shown.Index(state_dir())
    return _shown


def servable(path):
    """Whether the page's /api/file serves this path: the real path (after realpath) or None. The same rule as the page's
    (core/shown.py): an attachment for the owner sent by a session (hub send-file) or an image it named — across the
    whole journal history; the file exists on disk. The first call reads the archive parses (<archive>.shown.json; unparsed
    ones this process parses in the background, until then their paths are None — servable_state), afterwards only what
    was appended to the journal. A list (any non-string that can be iterated, 2.6) gives {path: real | None}, the journal
    is read up once for the whole list."""
    if isinstance(path, Iterable) and not isinstance(path, (str, bytes, bytearray, memoryview)):
        paths = list(path)
        if not all(isinstance(p, str) for p in paths):
            raise TypeError("servable: paths are strings")
        return _index().servable_many(paths)
    return _index().servable(path)


def servable_state():
    """Whether the servable index is complete (2.6): "ready" — the journal history is parsed; "building" — archives are
    still being parsed in the background (their paths are None for now — ask later); "partial" — an archive failed to
    parse, a retry comes no sooner than 10 minutes."""
    return _index().state()


def manifest(folder=None):
    with open(os.path.join(folder or os.getcwd(), "module.json"), encoding="utf-8-sig") as f:
        return json.load(f)


def settings():
    """The hub.json section named after the module ("<name>": {…}) — its settings; none — {}."""
    from core import catalog
    got = catalog.load_config(state_dir()).get(name())
    return got if isinstance(got, dict) else {}


def fact(type, data=None, entity=None, dedup_key=None, wait=None):
    """A fact into the core inbox on behalf of the module: hubd will write it to the journal (owner.notify, its own mod.<name>.* — EXTENDING).
    dedup_key (2.6) — a repeat without a duplicate: the same key from this module within a day is rejected by the core
    (code duplicate) with the number of the accepted one; keys are shared with its mod.* and the owner's word. wait=seconds
    (up to MAX_WAIT, 2.6) — wait for the core's decision, like owner(wait=): (seq, None) — accepted, (seq, reason) —
    rejected (its .code is the code, if any), (None, None) — the core did not record it within wait. Without wait — the file name in the inbox."""
    _check_dedup_wait(dedup_key, wait, data)
    # like `hub` from a module process (bin/hub): not the owner's word, labeled "from module <name>" (reducer._origin)
    data = {**(data or {}), "channel": "plugin", "owner": False, "sender": f"mod:{name()}"}
    return _put_wait(type, entity, data, wait, **({} if dedup_key is None else {"dedup": dedup_key}))


def _put_wait(type, entity, data, wait, **put):
    """A fact into the inbox; with wait — a req label and waiting for the core's decision (journal.decided), without — the file name in the inbox."""
    from core import journal, spool
    path = os.path.join(state_dir(), "journal.jsonl")
    mark = {"req": uuid.uuid4().hex[:12]} if wait is not None else {}   # decided finds the fact in the journal by it
    start = journal.mark(path) if wait is not None else None   # before the inbox: hubd may record the fact at once
    name_ = spool.put(os.path.join(state_dir(), "spool"), type, entity, {**data, **mark}, src="module", **put)
    if wait is None:
        return name_
    return journal.decided(path, mark["req"], wait, start=start)


_SURROGATE = re.compile("[\ud800-\udfff]")


def _check_dedup_wait(dedup_key, wait, data, bad_key=ValueError):
    """dedup_key and wait of fact and owner are checked before the inbox: otherwise the core would reject the key only after the route replied."""
    if dedup_key is not None and not (isinstance(dedup_key, str) and 0 < len(dedup_key) <= 128
                                      and not _SURROGATE.search(dedup_key)):   # the inbox (UTF-8) cannot write it
        raise bad_key("dedup_key is a string of up to 128 characters")
    if wait is not None and not (isinstance(wait, (int, float)) and not isinstance(wait, bool)
                                 and 0 < wait <= MAX_WAIT):   # NaN is out too
        raise ValueError(f"wait is seconds, above 0 and at most {MAX_WAIT}")
    if wait is not None and isinstance(data, dict) and "req" in data:
        raise ValueError("req is the label wait uses to find the fact in the journal: name your own field differently")


def owner(req, type, entity=None, data=None, dedup_key=None, wait=None):
    """The owner's word from a module route (EXTENDING "The owner's word"): a fact as the page writes it (message.received,
    answers, commands to sessions), with channel mod:<name>. Only for a request that passed the owner check (req.owner); from a view or
    from a module process without a request — HTTPError 403 before the inbox (a route that does not catch it replies 403 to the client).
    dedup_key — a repeat without a duplicate: the same key within a day is rejected by the core with the number of the
    accepted one; a key that is not a string of up to 128 chars — HTTPError 400 (it is also a ValueError, as before 2.6).
    wait=seconds (up to MAX_WAIT) — wait for the core's decision, like page commands: (seq, None) — accepted, (seq, reason
    in the core's words) — rejected, (None, None) — the core did not record it within wait (reply to the client — NOT_DECIDED).
    Without wait — the file name in the inbox."""
    if not (isinstance(req, Request) and req.owner and not req.view):
        raise HTTPError(403, "the owner's word is only on the owner's own request (X-Hub-Owner), not from a view or from a process")
    if not name():
        raise RuntimeError("HUB_MODULE is not set: the owner's word is written by a module process started by the hub")
    # the key usually comes from client data — it gets 400, not 500 (_BadInput is also a ValueError, as before 2.6)
    _check_dedup_wait(dedup_key, wait, data, bad_key=_BadInput)
    data = {**(data or {}), "channel": f"mod:{name()}", "owner": True,
            **({} if dedup_key is None else {"dedup_key": dedup_key})}
    return _put_wait(type, entity, data, wait)


VOICE_CAP = "voice.transcribe"


class TranscribeError(Exception):
    """A transcribe refusal: code is denied (no voice.transcribe capability or the process was not started by the hub), off (the hub's voice is
    not configured), format (the audio type is not accepted), timeout, cancelled, failed (provider failure: the text is its reason)."""

    def __init__(self, code, text=""):
        super().__init__(text or code)
        self.code = code


def _voice_config(folder):
    """The voice section of the hub settings for the provider call; None — voice is not configured. No capability —
    TranscribeError denied, settings unreadable — failed."""
    try:
        caps = manifest(folder).get("capabilities") if name() and os.environ.get("HUB_STATE_DIR") else None
    except (OSError, ValueError, AttributeError):
        caps = None
    if not isinstance(caps, list) or VOICE_CAP not in caps:
        raise TranscribeError("denied", f"audio transcription needs the capability {VOICE_CAP} in capabilities of module.json")
    from core import catalog
    from modules.voice import transcribe as voice
    try:
        cfg = {"voice": catalog.load_config(state_dir()).get("voice") or {}}
    except (OSError, ValueError) as e:
        raise TranscribeError("failed", f"the hub settings cannot be read: {e}") from None
    return cfg if voice.enabled(cfg) else None


def voice_available(folder=None):
    """Is voice configured on the hub? True/False without calling the provider — so a module can tell the client "no
    transcription" (404) before recording audio. Refusals as in transcribe: denied — no capability, failed — settings unreadable (Module API 2.4)."""
    return _voice_config(folder) is not None


def transcribe(audio, mime, timeout=None, cancel=None, folder=None):
    """Audio -> {"text"} or {"empty": True} (no speech) via the hub's voice provider — the same one as the page microphone
    (the voice section of the hub settings; hubmod reads it itself and does not give it to the module). mime is the audio
    type ("audio/webm", "audio/wav"…); timeout — seconds for the whole call; cancel — a threading.Event: once set, a call in progress is interrupted.
    A refusal is a TranscribeError with a code. The voice.transcribe capability goes in capabilities of module.json (Module API 2.4)."""
    cfg = _voice_config(folder)
    if cfg is None:
        raise TranscribeError("off", "voice is off on the hub (Settings -> Modules -> Voice, or voice.cmd in the hub settings)")
    from modules.voice import transcribe as voice
    suffix = voice.suffix_of_mime(mime)
    if not suffix:
        raise TranscribeError("format", f"audio {mime or 'without a type'} is not accepted (accepted: "
                                        f"{', '.join(sorted(voice.MIME_SUFFIX))})")
    if cancel is not None and cancel.is_set():
        raise TranscribeError("cancelled", "cancelled")
    if not audio:
        return {"empty": True}
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        return {"text": voice.transcribe(bytes(audio), cfg, suffix=suffix, cancel=cancel, deadline=deadline)}
    except voice.Empty:
        return {"empty": True}
    except voice.Stopped as e:
        raise TranscribeError(e.why, str(e)) from None
    except voice.VoiceError as e:
        raise TranscribeError("failed", str(e)) from None


MODEL_CAP = "model"
MODEL_PATH = "/api/module-model"   # a page route: its process makes the call (core/modelcall.py)


class ModelError(Exception):
    """A hubmod.model refusal, field code: denied (no model capability or the process was not started by the hub), off (the owner did not allow
    the model for the module), bad (wrong model, input too long, max_tokens or timeout out of range), busy (2 module
    calls or 4 in total are running), limit (60 calls per hour), unavailable (the hub page does not answer, HUB_NO_MODEL), timeout, failed
    (claude answered with an error: account limit, network, login — text for the log)."""

    def __init__(self, code, text=""):
        super().__init__(text or code)
        self.code = code


def model(prompt, text="", model="haiku", schema=None, max_tokens=1024, timeout=60):
    """A short model call through the core (Module API 2.7, the model capability in module.json; the owner allows it in "Modules"):
    prompt is the system prompt, text — the input, model — "haiku" or "sonnet", schema — the JSON Schema of the answer, max_tokens —
    the output ceiling (up to 4096), timeout — seconds (up to 120). -> {"text": …} (with schema — {"json": …}), "usd" (the
    API list price or None) and "model" (what was called). The hub page makes the call on a pool account: the token does not
    reach the module, the spending goes to "Spending" as the module's line. A refusal is a ModelError with a code. Synchronous: call from your own thread."""
    import urllib.error
    import urllib.request
    secret = os.environ.get("HUB_MODULE_SECRET")
    if MODEL_CAP not in _caps() or not name() or not secret or not os.environ.get("HUB_STATE_DIR"):
        raise ModelError("denied", f"a model call needs the {MODEL_CAP} capability in module.json and a start by the hub")
    if os.environ.get("HUB_NO_MODEL"):   # tests: do not call the live model
        raise ModelError("unavailable", "the model is off (HUB_NO_MODEL)")
    body = {"prompt": prompt, "text": text, "model": model, "max_tokens": max_tokens, "timeout": timeout,
            **({} if schema is None else {"schema": schema})}
    from core import instance
    url = f"http://127.0.0.1:{instance.port('web')}{MODEL_PATH}"
    req = urllib.request.Request(url, json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST", headers={
        "Content-Type": "application/json", "X-Hub-Module": name(), "X-Hub-Module-Secret": secret})
    wait = (timeout if isinstance(timeout, (int, float)) and 0 < timeout <= 600 else 60) + 15
    try:
        with urllib.request.urlopen(req, timeout=wait) as r:   # to itself — bypassing the proxy (core.net)
            got = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        with e:
            try:
                got = json.loads(e.read() or b"{}")
            except (OSError, ValueError):
                got = {}
        got = got if isinstance(got, dict) else {}
        raise ModelError(str(got.get("code") or ("denied" if e.code == 403 else "failed")),
                         str(got.get("error") or f"the page answered {e.code}")) from None
    except (OSError, ValueError) as e:   # not running, another port, cut the reply
        raise ModelError("unavailable", f"the hub page does not answer: {e}") from None
    if not isinstance(got, dict) or not ("text" in got or "json" in got):
        raise ModelError("failed", "the page answered in a wrong form")
    return got


# --- decorators -----------------------------------------------------------------------------------------------------

def hook(point):
    """The function answers a core point (POST /hook/<point>): it receives a JSON request and returns an answer per the point's schema."""
    def wrap(fn):
        _hooks[point] = fn
        return fn
    return wrap


def on_ready(fn):
    """A function without arguments, called when the core has recorded that the module is ready in this launch
    (module.ready: after hello and migration), once per process, in its own thread: here the module rebuilds its snapshot
    keys (snapshot.keys, Module API 2.4) — a value written by a previous launch is not shown in the snapshot."""
    _ready.append(fn)
    return fn


def route(path, methods=("GET",), stream=False, body="memory"):
    """A page route /api/mod/<name><path> (capability http). path is "/items", "/items/<id>" (req.params["id"]).
    stream=True — a generator function, each value is a text/event-stream event (dict and list — JSON, str — as is).
    body="file" — the request body goes to a temporary file req.file (deleted after the reply), with no memory limit —
    only the manifest's http.max_body."""
    return _register(path, methods, stream, body, view=False)


def view(path, methods=("GET",)):
    """An HTML view /mod/<name>/view<path>: the page opens it in a sandboxed <iframe>; str — text/html. The view calls its
    own routes through hub.call from VIEW_JS."""
    return _register(path, methods, False, "memory", view=True)


def _register(path, methods, stream, body, view):
    if not (isinstance(path, str) and path.startswith("/")):
        raise ValueError(f"a handle path must start with \"/\": {path!r}")
    methods = tuple(m.upper() for m in ([methods] if isinstance(methods, str) else methods))
    if not methods or set(methods) - set(METHODS):
        raise ValueError(f"handle methods are among {', '.join(METHODS)}")
    if body not in ("memory", "file"):
        raise ValueError('body: "memory" or "file"')
    pattern = re.compile("^" + re.sub(r"<([A-Za-z_][A-Za-z0-9_]*)>", r"(?P<\1>[^/]+)",
                                      re.escape(path).replace(r"\<", "<").replace(r"\>", ">")) + "$")

    def wrap(fn):
        _routes.append({"path": path, "re": pattern, "methods": methods, "stream": stream, "body": body,
                        "view": view, "fn": fn})
        return fn
    return wrap


class HTTPError(Exception):
    """A route reply with an error: the client gets status and {"error": text}."""

    def __init__(self, status, text=""):
        super().__init__(text)
        self.status, self.text = int(status), str(text)


class _BadInput(HTTPError, ValueError):
    """400 for a route that did not catch the refusal; a ValueError as before 2.6, so a module's old `except ValueError` catches it."""

    def __init__(self, text):
        super().__init__(400, text)


class Response:
    """A whole route reply: body is dict/list (JSON), str, bytes or file=path (served in chunks)."""

    def __init__(self, body=None, status=200, headers=None, type=None, file=None):
        self.body, self.status, self.headers, self.type, self.file = body, int(status), dict(headers or {}), type, file


class Request:
    """A request to a route: method, path (from the module root, "/items"), params (from <…> in the path), query {name: value},
    headers, owner — passed the owner check (X-Hub-Owner), view — sent by the module's HTML view, body (bytes),
    file (body="file"), json(), text()."""

    def __init__(self, method, path, query, headers, params, owner, view):
        self.method, self.path, self.query, self.headers, self.params = method, path, query, headers, params
        self.owner, self.view, self.body, self.file = owner, view, b"", None

    def text(self):
        return self.body.decode("utf-8")

    def json(self):
        return json.loads(self.body or b"null")


# --- own state: facts mod.<name>.set|del (core/modstate.py) ------------------------------------------------------------

_NO_REV = object()


def _slice():
    """The module's own part of the hub state {ready, mods, others} (core/modstate.slice): the slice hubd writes, not the
    whole state.json (on a big hub that is +100 MB of peak for the process); no slice (hubd has not written yet) — the hub state, as before."""
    from core import modrun
    got = modrun.slice_of(state_dir(), name())
    return got if got is not None else _from_state()


def _from_state(sessions=False):
    from core import journal, modstate
    try:   # with the sessions capability — rows of the same form as in the slice (with accounts — the session's account too)
        return modstate.slice(journal.load_state(state_dir()), name(), _caps() if sessions else ())
    except (OSError, ValueError):
        return {"ready": None, "mods": {}, "others": 0, **({"sessions": {}, "renamed": {}} if sessions else {})}


def sessions():
    """Hub sessions {name: {state, parked, asleep, busy, model, …}} — for a module with the sessions capability in module.json (EXTENDING
    "Hub sessions"): state is as in the core (idle, busy, asleep…); parked — parked (free for the owner, a message
    will start it); asleep — no process, the core accepts wake (also for a parked one — a message is enough for it); busy — a
    turn is running or the process is starting; model — the model id; since 2.6 — retired, owner_msg, asks, forms, reacts, hub_wait,
    limit_cut {ts, resets}; since 2.10 — raised_by, since, worktree {branch, base} (and account with the accounts capability; core/modstate.session_rows). Each call reads the fresh slice hubd
    writes after an event (it may lag the journal for a moment). No capability — PermissionError."""
    return _sessions()["sessions"]


def session(who):
    """A session by name, including an old one (renamed): its record from sessions() with the field name — the current name;
    no such session (or removed) — None."""
    got = _sessions()
    now = who if who in got["sessions"] else got["renamed"].get(who)
    rec = got["sessions"].get(now) if isinstance(now, str) else None
    return {**rec, "name": now} if rec else None


def _sessions():
    """Sessions from its own slice: hubd puts them there for a module with the capability; no slice — by its own module.json: with the capability —
    from the hub state, without — a refusal, reading nothing."""
    from core import modrun
    got = modrun.slice_of(state_dir(), name())
    if got is None or "sessions" not in got:   # no slice or it was written before the capability (the tick will add it) — as without a slice
        got = _from_state(sessions=True) if "sessions" in _caps() else {}
    if not isinstance(got.get("sessions"), dict):
        raise PermissionError("hub sessions — for a module with the sessions capability in capabilities of module.json (Module API 2.4)")
    return {"sessions": got["sessions"], "renamed": got.get("renamed") or {}}


def accounts():
    """Limits of the hub's Claude accounts {active, blocked: {name: until}, exhausted: {until} or None, seen: {name: {status, type,
    windows, ts}}} — for a module with the
    accounts capability (2.6, EXTENDING "Hub sessions"): without tokens. From its own slice, which hubd writes after an event;
    no slice or it is unreadable — None ("don't know", ask later): this call does not read the whole hub state. No
    capability — PermissionError."""
    got = _view("accounts", "accounts")
    return got if isinstance(got, dict) else None


def owner_message(mid):
    """An owner message mN: {to — to whom (as the core understood), ts, answers — the card qN it was counted as an answer to, or None,
    form — the request_id of the form it answered, or None} — for a module with the messages capability (2.6); without the text. No such message
    (not recorded yet, already removed, not from the owner) or no slice — None. No capability — PermissionError."""
    got = _view("owner_messages", "messages")
    rec = got.get(mid) if isinstance(got, dict) and isinstance(mid, str) else None
    return dict(rec) if isinstance(rec, dict) else None


def _view(key, right):
    """Part key of its own slice (capability right) or None — no slice or it was written before this capability (hubd will add it on a tick);
    without the capability — PermissionError, reading nothing."""
    from core import modrun
    got = modrun.slice_of(state_dir(), name())
    if got is not None and key in got:
        return got[key]
    if right in _caps():
        return None
    raise PermissionError(f"{key} — for a module with the {right} capability in capabilities of module.json (Module API 2.6)")


def _caps():
    """The module's capabilities from its module.json; unreadable — none."""
    try:
        caps = manifest().get("capabilities") or []
    except (OSError, ValueError, AttributeError):
        return []
    return [c for c in caps if isinstance(c, str)] if isinstance(caps, list) else []


class _State:
    """The module's own state in the hub journal. get reads from a copy in the process: it is read from its own slice of
    the hub state (_slice) once and afterwards knows its own writes at once; a write is a fact in the inbox, hubd does the
    fold. The core limits (the key, 256 KB per module, 512 KB for all — other modules as of the last refresh) are
    checked here too — a ValueError with the reason before the inbox. get and all give a copy, set takes a copy (2.6):
    what is edited after reading or writing does not reach the copy — a module that got a refusal mid-edit will not carry
    the half-changed value into the next write. A write hubd still rejected (wrong if_rev, the module has not answered
    hello yet, other modules grew) is core.rejected in the journal, and the copy keeps it until refresh(): refresh()
    re-reads the slice (a command could change it too)."""

    def __init__(self):
        self._lock, self._data, self._others = threading.RLock(), None, 0

    def _mine(self):
        with self._lock:
            if self._data is None:
                self.refresh()
            return self._data

    def refresh(self):
        got = _slice()
        got, others = got["mods"], got["others"]
        with self._lock:
            self._data = {k: dict(v) for k, v in got.items() if isinstance(v, dict)}
            self._others = others

    def get(self, key, default=None):
        with self._lock:   # set and delete run in other routes' threads; they do not change a value in the copy — the copy is made outside the lock
            rec = self._mine().get(key)
        return copy.deepcopy(rec["value"]) if rec else default

    def rev(self, key):
        """The key's revision after the last refresh (0 — no such key; its own write without refresh — None)."""
        with self._lock:
            rec = self._mine().get(key)
        return rec.get("rev") if rec else 0

    def all(self):
        with self._lock:
            got = {k: r["value"] for k, r in self._mine().items()}
        return copy.deepcopy(got)

    def set(self, key, value, if_rev=_NO_REV):
        from core import modstate
        if not (isinstance(key, str) and modstate.KEY.fullmatch(key)):
            raise ValueError("key: lowercase Latin letters, digits and \"_.-\", up to 64 characters")
        try:
            new = modstate.size(key, value)
            # a copy in the form the value will have in the journal (a tuple becomes a list, keys become strings): the same until refresh
            kept = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
        except (ValueError, TypeError) as e:
            raise ValueError(f"the value is not JSON: {e}") from None
        if if_rev is None:   # rev() of its own write before refresh(): there is no revision yet — writing silently without if_rev is not allowed
            raise ValueError("the key revision is unknown (own write before refresh) — call state.refresh() and rev() again")
        with self._lock:   # routes run in server threads: the check and the write to the copy happen under one lock
            mine = self._mine()
            used = sum(modstate.size(k, r["value"]) for k, r in mine.items() if k != key)
            if used + new > modstate.MAX_MODULE:
                raise ValueError(f"the module state would be {used + new} B — the limit is {modstate.MAX_MODULE >> 10} KB; "
                                 "keep big data behind a module handle")
            if self._others + used + new > modstate.MAX_ALL:   # other modules — as of the last refresh
                raise ValueError(f"the state of all modules would be {self._others + used + new} B — the limit is "
                                 f"{modstate.MAX_ALL >> 10} KB")
            if key not in mine and len(mine) >= modstate.MAX_KEYS:
                raise ValueError(f"the module already has {modstate.MAX_KEYS} keys")
            # process launch: a snapshot key (snapshot.keys) is visible while the launch that wrote it is ready (core/modclaims.py)
            launch = os.environ.get("HUB_MODULE_LAUNCH")
            _put(f"mod.{name()}.set", {"key": key, "value": value, **({"launch": launch} if launch else {}),
                                       **({} if if_rev is _NO_REV else {"if_rev": if_rev})})
            mine[key] = {"value": kept, "rev": None}

    def delete(self, key, if_rev=_NO_REV):
        if if_rev is None:
            raise ValueError("the key revision is unknown (own write before refresh) — call state.refresh() and rev() again")
        with self._lock:
            _put(f"mod.{name()}.del", {"key": key, **({} if if_rev is _NO_REV else {"if_rev": if_rev})})
            self._mine().pop(key, None)


def _put(kind, data):
    from core import spool
    return spool.put(os.path.join(state_dir(), "spool"), kind, None, data, src="module")


state = _State()


# --- view: the link to the module's own routes -----------------------------------------------------------------------

# Insert into the view's HTML (the script only inside the document — the view's CSP). hub.call(path, {method, body, type}) -> Promise
# {status, type, body} — a route of ITS OWN module (/api/mod/<name><path>) through the hub page: it gives the view a port
# when the frame loads and passes requests only to this module; hub.json(path, {method, json}) — JSON both ways;
# hub.go(path) — another page of its own view (/mod/<name>/view<path>) or a reload with a port.
VIEW_JS = """<script>
window.hub = (() => {
  let port = null, n = 0;
  const wait = new Map(), queue = [];
  addEventListener("message", (e) => {
    if (port || e.source !== parent || !e.data || e.data.hub !== "port" || !e.ports[0]) return;
    port = e.ports[0];
    port.onmessage = (m) => { const w = wait.get(m.data.id); if (w) { wait.delete(m.data.id); w(m.data); } };
    queue.splice(0).forEach((f) => f());
  });
  function call(path, opts = {}) {
    return new Promise((resolve, reject) => {
      const send = () => {
        const id = ++n;
        wait.set(id, (r) => (r.error ? reject(new Error(r.error)) : resolve(r)));
        port.postMessage({ id, path, method: opts.method || "GET", body: opts.body, type: opts.type });
      };
      port ? send() : queue.push(send);
    });
  }
  async function json(path, opts = {}) {
    const body = opts.json === undefined ? undefined : JSON.stringify(opts.json);
    const r = await call(path, { method: opts.method || (body === undefined ? "GET" : "POST"), body,
                                 type: body === undefined ? undefined : "application/json" });
    let data = null;
    try { data = r.body ? JSON.parse(r.body) : null; } catch (e) { if (r.status < 400) throw e; }
    if (r.status >= 400) throw new Error((data && data.error) || "response " + r.status);
    return data;
  }
  // go to another page of the view (or reload): the hub page drives the navigation, since a new document gets a new
  // port; the view itself (a link, location) loses its port
  function go(path) {
    const send = () => port.postMessage({ go: String(path) });
    port ? send() : queue.push(send);
  }
  return { call, json, go };
})();
</script>"""


# --- server ---------------------------------------------------------------------------------------------------------

def _check_start(man, points, routes):
    """Before the server: points of this API version and the capabilities for them and for routes — otherwise exit with a
    reason (the core records "did not start", and the reason is in the module log)."""
    from core import modrun, version
    caps, errs = set(man.get("capabilities") or []), []
    # an optional point the core does not call is no reason to stop: hello leaves it out (_hello_for) — one module
    # version runs on a core with the Codex pool and on one without it (the open flavor)
    known = set(modrun.callable_points()) | set(version.OPTIONAL_POINTS)
    for p in sorted(points):
        if p == "hello":
            errs.append("hubmod answers hello itself, from module.json")
        elif p not in known:
            errs.append(f"the hub does not call the point {p} (known: {', '.join(sorted(known))})")
        elif (modrun.POINTS[p].get("cap") or "") and modrun.POINTS[p]["cap"] not in caps:
            errs.append(f"the point {p} needs the capability {modrun.POINTS[p]['cap']} in capabilities of module.json")
    if routes and "http" not in caps:
        errs.append("handles and views need the http capability in capabilities of module.json")
    if errs:
        sys.exit("hubmod: " + "; ".join(errs))


def _checked(point, res, man):
    """A point answer per the core schema (core/modrun.POINTS): off-schema — an exception with a reason (the core would count
    it a failure, and the reason is visible here in the module log)."""
    from core import modrun
    spec = modrun.POINTS.get(point)
    if point == "migrate" and isinstance(res, dict) and res.get("error"):
        return res   # a migration refusal with a reason is an answer by contract, not a schema failure
    if spec:
        ctx = {"name": man.get("name"), "version": man.get("version"), "capabilities": man.get("capabilities"),
               "env": man.get("env"), "api": man.get("api")}
        try:
            spec["check"](res, ctx)
        except modrun.BadResult as e:
            raise ValueError(f"the answer of the point {point} does not match the core schema — {e}") from None
    return res


def serve(points, folder=None, block=True):
    """Listen on HUB_MODULE_PORT: points is {point: function(request) -> JSON answer}. hello answers itself: name and version from
    module.json, points are the keys of points. An exception in a function gives a 500 reply (the core records the failure and does its own thing), its text goes
    to the process stderr. block=False — the server runs in a thread, return it."""
    # the old form — as before, no check before start
    return _serve(dict(points), [], folder, block, check=False, ready=list(_ready))


def run(folder=None, block=True):
    """Listen on HUB_MODULE_PORT with @hook points, @route / @view routes and @on_ready."""
    return _serve(dict(_hooks), list(_routes), folder, block, ready=list(_ready))


def _when_ready(fns):
    """@on_ready: wait for its own readiness in the hub state (modules.<name>.ready — this launch; a paused module waits for
    its end, hence no deadline) and call the functions. Migration runs in this same process — its own copy of the state
    is already after it. Without a launch label (the process was not started by the core) — do not call, the reason goes to the module log."""
    launch, wait = os.environ.get("HUB_MODULE_LAUNCH"), READY_EVERY[0]
    if not launch:
        print("hubmod: no HUB_MODULE_LAUNCH (the process was not started by the core) — @on_ready not called", file=sys.stderr,
              flush=True)
        return
    while True:
        if _slice()["ready"] == launch:
            break
        time.sleep(wait)
        wait = min(wait * 1.5, READY_EVERY[1])
    for fn in fns:
        try:
            fn()
        except Exception:  # noqa: BLE001 — a failure of its own function: to the module log, the server lives
            traceback.print_exc(file=sys.stderr)
            sys.stderr.flush()


def _hello_for(hello, request):
    """hello's answer for this hubd: an optional point (version.OPTIONAL_POINTS) the request does not offer is not
    declared — a hubd still on the previous code (the tree on disk updated, its restart pending) would refuse the whole
    module for a point it does not call; the rest as registered (an unknown one is refused as before)."""
    from core import version
    offered = request.get("points") if isinstance(request, dict) else None
    if not isinstance(offered, list):
        return hello
    optional = set(version.OPTIONAL_POINTS)   # the same tree on disk as this hubmod
    return {**hello, "points": [p for p in hello["points"] if p not in optional or p in offered]}


class _Server(nolookup.NoLookup, ThreadingHTTPServer):
    """The module's server: bound without asking the name service who 127.0.0.1 is (the module would wait for a slow resolver)."""


def _serve(points, routes, folder, block, check=True, ready=()):
    man = manifest(folder)
    if check:
        _check_start(man, points, routes)
    secret = os.environ["HUB_MODULE_SECRET"].encode()
    hello = {"name": man["name"], "version": man["version"], "points": sorted(points)}
    prefixes = (("api", f"/api/mod/{man['name']}"), ("view", f"/mod/{man['name']}/view"))

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *a):
            pass

        def reply(self, code, obj, ctype="application/json", headers=None):
            if isinstance(obj, (bytes, bytearray)):
                body = bytes(obj)
            elif isinstance(obj, str):
                body = obj.encode("utf-8")
            else:
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def secret_ok(self):
            if hmac.compare_digest(self.headers.get("X-Hub-Secret", "").encode(), secret):
                return True
            self.reply(403, {"error": "secret"})
            return False

        def do_POST(self):
            if not self.secret_ok():
                return
            if self.path.startswith("/hook/"):
                return self.hook()
            return self.http()

        def do_GET(self):
            if self.secret_ok():
                self.http()

        do_PUT = do_PATCH = do_DELETE = do_GET

        def hook(self):
            size = int(self.headers.get("Content-Length") or 0)
            if size > MAX_BODY:
                return self.reply(413, {"error": "body"})
            try:
                payload = json.loads(self.rfile.read(size) or b"null")
            except ValueError:
                return self.reply(400, {"error": "json"})
            point = self.path[len("/hook/"):]
            if point == "hello":
                return self.reply(200, _hello_for(hello, payload))
            if point not in points:
                return self.reply(404, {"error": f"no point {point}"})
            try:
                return self.reply(200, _checked(point, points[point](payload), man))
            except Exception as e:  # noqa: BLE001 — a module failure: the core does its own thing
                traceback.print_exc(file=sys.stderr)
                sys.stderr.flush()
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"[:300]})

        def http(self):
            parts = urlsplit(self.path)
            kind, rel = None, None
            for k, prefix in prefixes:
                if parts.path == prefix or parts.path.startswith(prefix + "/"):
                    # by path parts: an encoded "/" inside a part (%2F) stays part of it and does not split the path
                    kind = k
                    rel = "/".join(unquote(x).replace("/", "%2F") for x in parts.path[len(prefix):].split("/")) or "/"
                    break
            if kind is None:
                return self.reply(404, {"error": "no such handle"})
            mine = [r for r in routes if r["view"] == (kind == "view")]
            hit = [(r, m) for r in mine for m in [r["re"].match(rel)] if m]
            if not hit:
                return self.reply(404, {"error": f"no handle {rel}"})
            allowed = [(r, m) for r, m in hit if self.command in r["methods"]]
            if not allowed:
                methods = sorted({x for r, _ in hit for x in r["methods"]})
                return self.reply(405, {"error": "wrong method"}, headers={"Allow": ", ".join(methods)})
            r, m = allowed[0]
            req = Request(self.command, rel, dict(parse_qsl(parts.query, keep_blank_values=True)), self.headers,
                          {k: v.replace("%2F", "/") for k, v in m.groupdict().items()}, self.headers.get("X-Hub-Owner") == "1" and kind == "api",
                          kind == "view" or self.headers.get("X-Hub-View") == "1")
            size = (self.headers.get("Content-Length") or "").strip()
            size = int(size) if size.isascii() and size.isdigit() and len(size) < 20 else 0   # "²" is not a number to int()
            try:
                if r["body"] == "file":
                    req.file = self.body_file(size)
                elif size > MAX_BODY:
                    return self.reply(413, {"error": f"the body is over {MAX_BODY >> 20} MB — the handle needs body=\"file\""})
                else:
                    req.body = self.rfile.read(size) if size else b""
                if r["stream"]:
                    return self.stream(r["fn"](req))
                return self.answer(r["fn"](req), r["view"])
            except HTTPError as e:
                return self.reply(e.status, {"error": e.text})
            except Exception as e:  # noqa: BLE001 — a route failure: the client gets 500, the text goes to the module log
                traceback.print_exc(file=sys.stderr)
                sys.stderr.flush()
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"[:300]})
            finally:
                if req.file:
                    try:
                        os.unlink(req.file)
                    except OSError:
                        pass

        def body_file(self, size):
            fd, path = tempfile.mkstemp(prefix="hubmod-body-")
            with os.fdopen(fd, "wb") as f:
                left = size
                while left > 0:
                    chunk = self.rfile.read(min(CHUNK, left))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
            return path

        def answer(self, res, is_view):
            if isinstance(res, tuple) and len(res) == 2 and isinstance(res[0], int):
                res = Response(res[1], status=res[0])
            if not isinstance(res, Response):
                res = Response(res)
            if res.file:
                return self.send_file(res)
            body, ctype = res.body, res.type
            if body is None:
                code = 204 if res.status == 200 else res.status
                self.send_response(code)
                self.send_header("Content-Length", "0")
                for k, v in res.headers.items():
                    self.send_header(k, v)
                return self.end_headers()
            if ctype is None:
                ctype = ("application/octet-stream" if isinstance(body, (bytes, bytearray))
                         else ("text/html; charset=utf-8" if is_view else "text/plain; charset=utf-8")
                         if isinstance(body, str) else "application/json")
            return self.reply(res.status, body, ctype, res.headers)

        def send_file(self, res):
            size = os.path.getsize(res.file)
            self.send_response(res.status)
            self.send_header("Content-Type", res.type or "application/octet-stream")
            self.send_header("Content-Length", str(size))
            for k, v in res.headers.items():
                self.send_header(k, v)
            self.end_headers()
            with open(res.file, "rb") as f:
                while True:
                    chunk = f.read(CHUNK)
                    if not chunk:
                        break
                    self.wfile.write(chunk)

        def stream(self, gen):
            """The generator runs in its own thread: here we wait for its events and send a ping while it is silent; if the client leaves,
            the generator is closed at its next event."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            q, stop, done = queue.Queue(8), threading.Event(), object()

            def produce():
                try:
                    for item in gen:
                        if stop.is_set():
                            break
                        q.put(item)
                except Exception:  # noqa: BLE001 — a generator failure: the thread ends, the text goes to the module log
                    traceback.print_exc(file=sys.stderr)
                    sys.stderr.flush()
                finally:
                    getattr(gen, "close", lambda: None)()
                    q.put(done)
            threading.Thread(target=produce, daemon=True).start()
            try:
                while True:
                    try:
                        item = q.get(timeout=PING)
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    if item is done:
                        break
                    text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
                    self.wfile.write(("".join(f"data: {line}\n" for line in text.split("\n")) + "\n").encode())
                    self.wfile.flush()
            except OSError:
                pass
            finally:
                stop.set()
                while True:   # release the generator if it waits for room in the queue
                    try:
                        if q.get_nowait() is done:
                            break
                    except queue.Empty:
                        break

    srv = _Server(("127.0.0.1", int(os.environ["HUB_MODULE_PORT"])), Handler)
    srv.daemon_threads = True
    if ready:   # @on_ready — once the core records the readiness of this launch
        threading.Thread(target=_when_ready, args=(ready,), daemon=True).start()
    if block:
        srv.serve_forever()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
