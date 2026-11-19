"""A failed update of the app goes to the company's Sentry (app spec §7.1, slice A5; the owner's decision of 2026-11-07:
"a bug report with all the paths and errors — it is an internal tool"): we fix it and ship the next version, the hub
stays on the version it runs and the owner does nothing by hand.

- Where: the DSN of the shell's build — layers/deliver.json "report" (tools/package/build.py --report-dsn, the team
  build); else update.report_url in hub.json (an open build's own receiver, off by default). None — nothing goes.
  update.report: false — off. A DSN gives only sending events to its project.
- When: a step of the update failed (STEPS), or a step of the one-line install (install.<step>, its log attached): the releases unreadable, a download that broke off after its tries, a
  layer that did not pass the check (sum, archive, MANIFEST), a layer the hub did not come up on (the previous code
  writes it up), the release put back because the core did not start on an edited layer. Passing failures — no network,
  a 5xx of the server — are a state, not a report.
- Once per version and step (layers/reports.json): a repeat is counted, and the counts go with the next report sent.
  A report that could not go (no network, the receiver busy) waits (its body in layers/reports/) and goes at a later
  pass — at most PENDING_MAX of them, for PENDING_DAYS; a receiver that does not answer is asked once a pass.
- What, unmasked: the versions from and to, the build, the channel, the OS and the architecture, the shell's
  environment number, Claude Code's version, the step, the result, the error and its Python traceback with the real
  paths, the update's and hub-up's own lines of hub-up.log (the parts started, exited with a code, a traceback's frames)
  and the watchdog's log (each up to LOG_MAX), the update's own records
  (history, failed, next, trial), what differs in the running layer from its MANIFEST, the free space, the busy
  sessions, the plugins and own modules with versions, the machine and the user, the hub's id (as in the statistics).
  Not: the texts of sessions and of the owner, the journal, hub.json — a failed update needs none of them (that is the
  report's scope, not a mask). Cut: the build key, wherever it stands — it is access to the releases, not a path or
  an error — and the known forms of other keys (core/redact.mask: a token in a log line, a password in an address).
- How: one envelope of the Sentry protocol (/api/<project>/envelope/), the stdlib and core/nethttp; no SDK."""
import contextlib
import datetime
import getpass
import json
import os
import platform
import re
import shutil
import sys
import time
import traceback
import urllib.parse
import uuid

import platforms
from core import catalog

REPORTS = "reports.json"     # <state>/layers/reports.json: what was sent, what waits, the repeats not yet told
WAITING = "reports"          # <state>/layers/reports/<event id>.envelope: the bodies that wait (the ledger stays small)
STEPS = ("check", "download", "verify", "switch", "trial", "restore", "shell")
INSTALL = "install."   # install.<step>: a step of the one-line install after the key is kept (deploy/oneline/setup.py)
KEPT = 200                   # the version-and-step records kept (old versions fall off)
PENDING_MAX = 5
PENDING_DAYS = 3
LOG_MAX = 512 * 1024         # bytes of each log in a report (two logs: about a megabyte)
DIFF_MAX = 200               # entries of the running layer's difference from its MANIFEST
TIMEOUT = 20
KEY = re.compile(r"[0-9A-Za-z]{8,64}\Z")
BUILD_KEY = "[build key]"


def dsn(text):
    """A DSN "https://<key>@<host>/<project>" -> {url: the envelope endpoint, key}; http only to this machine (a test
    receiver). None — not one."""
    if not isinstance(text, str):
        return None
    try:
        u = urllib.parse.urlsplit(text.strip())
        host, port = u.hostname, u.port
    except ValueError:
        return None
    local = host in ("127.0.0.1", "localhost", "::1")
    if not (u.scheme == "https" or (u.scheme == "http" and local)) or not host or not KEY.match(u.username or ""):
        return None
    prefix, _, project = u.path.rstrip("/").rpartition("/")   # a Sentry under a path keeps it: <prefix>/api/<project>
    if not project.isdigit():
        return None
    where = f"[{host}]" if ":" in host else host
    netloc = f"{where}:{port}" if port else where
    return {"url": f"{u.scheme}://{netloc}{prefix}/api/{project}/envelope/", "key": u.username}


def scrub(text, key):
    """The build key out of a text: the key itself, `key=…` of an address, a bearer token."""
    if key:
        text = text.replace(key, BUILD_KEY)
    text = re.sub(r"([?&]key=)(?!\[build key\])[^&\s\"']+", r"\1" + BUILD_KEY, text)
    return re.sub(r"(Bearer )[A-Za-z0-9_.~+/=-]{8,}", r"\1" + BUILD_KEY, text)


def clean(value, key=None):
    """Every text of a report without the build key and without the known forms of other keys (core/redact.mask_data:
    a token in a log line, a password in an address; a value whose name says it is a key) — the paths, names and errors
    stay as they are (the owner's decision: no mask). String by string, so the JSON around them stays whole."""
    from core import redact
    if isinstance(value, dict):   # {"api_token": "…"}: the name says what the string alone does not
        value = {k: redact.MASK if isinstance(v, str) and isinstance(k, str) and redact.secret_name(k) else v
                 for k, v in value.items()}
        return {k: clean(v, key) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v, key) for v in value]
    return scrub(redact.mask_data(value), key) if isinstance(value, str) else value


def envelope(event, files, target, key=None):
    """The bytes of one envelope: its header, the event, the files as attachments (text/plain). The texts are cleaned
    (clean) before each part's length is counted: Sentry reads a part by its length."""
    lines = [json.dumps({"event_id": event["event_id"], "dsn": target,
                         "sent_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}).encode()]
    item = json.dumps(clean(event, key), ensure_ascii=False, default=str).encode()
    lines += [json.dumps({"type": "event", "length": len(item)}).encode(), item]
    for name, data in files.items():
        data = clean(data.decode("utf-8", "replace"), key).encode()
        lines += [json.dumps({"type": "attachment", "length": len(data), "filename": name,
                              "content_type": "text/plain", "attachment_type": "event.attachment"}).encode(), data]
    return b"\n".join(lines) + b"\n"


def _read(path, kind):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return kind()
    return data if isinstance(data, kind) else kind()


def _tail(path, size=LOG_MAX, keep=None):
    """The last `size` bytes of a log (whole lines), only the lines `keep` takes when given."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            f.seek(max(0, end - size * (8 if keep else 1)))
            data = f.read()
    except OSError:
        return b""
    lines = data.split(b"\n")[1 if len(data) < end else 0:]
    if keep:
        lines = [x for x in lines if keep(x)]
    out = b"\n".join(x for x in lines if x)
    return out[-size:].split(b"\n", 1)[-1] if len(out) > size else out


TRACE = re.compile(rb'Traceback \(most recent call last\)|  File "|[A-Za-z_][\w.]*(Error|Exception)\b')


def _update_line(line):
    """The update's own lines, hub-up's (which part started, exited with what code) and the frames of a traceback: what
    a hub that did not come up on a release needs; the other lines of the log (a session's words) stay home."""
    head = line[:40].lower()
    return head.startswith((b"update:", b"trial:")) or b"hub-up: " in head or TRACE.match(line) is not None


def frames(exc):
    """A Python traceback as Sentry's stack frames, the real paths."""
    out = []
    for f in traceback.extract_tb(exc.__traceback__):
        out.append({"filename": f.filename, "abs_path": f.filename, "lineno": f.lineno, "function": f.name,
                    "context_line": f.line or ""})
    return out


class Reporter:
    """One per pass of the update (modules/update/layer.py): failed() for a step that failed, flush() sends what waits."""

    def __init__(self, state_dir, cfg, now=time.time, send=None):
        self.state_dir, self.cfg, self.now = state_dir, cfg, now
        self.send = send or _post
        self.flushed = False   # one sending round per pass: a receiver that is down is not asked twice in a row
        self.outcome = {}      # key -> "sent" | "waits" | "refused" of this pass's round

    def layers(self):
        return os.path.join(self.state_dir, "layers")

    def deliver(self):
        return _read(os.path.join(self.layers(), "deliver.json"), dict)

    def target(self):
        """(the DSN, its parts) of where reports go, or None: off, or no address."""
        up = self.cfg.get("update") or {}
        if up.get("report") is False:
            return None
        for text in (self.deliver().get("report"), up.get("report_url")):
            got = dsn(text)
            if got:
                return text.strip(), got
        return None

    def configured(self):
        """Whether the hub has an address to report to at all (the page's line names it, on or off)."""
        up = self.cfg.get("update") or {}
        return any(dsn(t) for t in (self.deliver().get("report"), up.get("report_url")))

    # --- the ledger

    def _ledger(self):
        got = _read(os.path.join(self.layers(), REPORTS), dict)
        return {"done": got.get("done") if isinstance(got.get("done"), dict) else {},
                "pending": [p for p in got.get("pending") or [] if isinstance(p, dict)
                            and isinstance(p.get("id"), str) and re.fullmatch(r"[0-9a-f]{32}", p["id"])],
                "repeats": got.get("repeats") if isinstance(got.get("repeats"), dict) else {},
                "marks": got.get("marks") if isinstance(got.get("marks"), dict) else {}}

    def _body(self, event_id):
        return os.path.join(self.layers(), WAITING, f"{event_id}.envelope")

    def _drop(self, p):
        with contextlib.suppress(OSError):
            os.unlink(self._body(p["id"]))

    def mark(self, name, value):
        """A value this hub has reported for name (the `at` of the launcher's restore record): mark(name) gives it."""
        ledger = self._ledger()
        ledger["marks"][name] = value
        self._save(ledger)

    def marked(self, name):
        return self._ledger()["marks"].get(name)

    def _save(self, ledger):
        done = ledger["done"]
        if len(done) > KEPT:
            ledger["done"] = dict(sorted(done.items(), key=lambda kv: kv[1].get("at", 0) if isinstance(kv[1], dict)
                                         else 0)[-KEPT:])
        os.makedirs(self.layers(), exist_ok=True)
        catalog.write_private(os.path.join(self.layers(), REPORTS), ledger)

    # --- one failure

    def failed(self, step, to, updater=None, result="", error="", exc=None, extra=None):
        """A step of the update failed for the version `to`. -> "sent" | "waits" | "refused" | "repeat" | "off"."""
        where = self.target()
        if where is None or not (step in STEPS or step.startswith(INSTALL)):
            return "off"
        from core import appcode
        if appcode.OWN in str(to or ""):   # a hub's own build of the person's edits: not a release's failure
            return "off"
        ledger = self._ledger()
        key = f"{to or '?'}|{step}"
        if key in ledger["done"]:
            seen = ledger["done"][key] if isinstance(ledger["done"][key], dict) else {}
            seen["repeats"] = int(seen.get("repeats") or 0) + 1
            ledger["done"][key] = seen
            ledger["repeats"][key] = int(ledger["repeats"].get(key) or 0) + 1
            self._save(ledger)
            return "repeat"
        event, files = self.event(step, to, updater, result, error, exc, extra)
        if ledger["repeats"]:
            event["extra"]["repeats"] = dict(ledger["repeats"])
        text, _ = where
        build = self.deliver().get("key")
        body = envelope(event, files, text, build if isinstance(build, str) and build else None)
        platforms.current.makedirs_private(os.path.join(self.layers(), WAITING))
        fd = os.open(self._body(event["event_id"]) + ".part", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(body)
        os.replace(self._body(event["event_id"]) + ".part", self._body(event["event_id"]))
        ledger["done"][key] = {"at": round(self.now()), "repeats": 0}
        ledger["repeats"] = {}
        pending = ledger["pending"] + [{"key": key, "at": round(self.now()), "id": event["event_id"]}]
        for p in pending[:-PENDING_MAX]:
            self._drop(p)
        ledger["pending"] = pending[-PENDING_MAX:]
        self._save(ledger)   # first: a pass cut off mid-send does not send it twice as a new one
        self.flush(ledger)
        return self.outcome.get(key, "waits")

    def flush(self, ledger=None):
        """Send the reports that wait. -> how many went."""
        where = self.target()
        ledger = ledger or self._ledger()
        if where is None or not ledger["pending"] or self.flushed:
            return 0
        self.flushed = True
        sent, keep, down = 0, [], False
        for p in ledger["pending"]:
            if self.now() - (p.get("at") or 0) > PENDING_DAYS * 86400:
                self._drop(p)
                continue
            if down:   # the receiver did not answer the one before: the rest wait for the next pass, not its timeout each
                keep.append(p)
                self.outcome[p.get("key")] = "waits"
                continue
            try:
                with open(self._body(p["id"]), "rb") as f:
                    body = f.read()
            except OSError:
                continue   # its body is gone: nothing to send
            got = self.send(where[1], body)
            if got == "sent":
                sent += 1
                self.outcome[p.get("key")] = "sent"
                self._drop(p)
            elif got == "retry":
                keep.append(p)
                self.outcome[p.get("key")] = "waits"
                down = True
            else:   # refused by the receiver (a form, too big): sending it again changes nothing
                self.outcome[p.get("key")] = "refused"
                self._drop(p)
                print(f"update: the receiver refused the report ({p.get('key')}): {got}", flush=True)
        ledger["pending"] = keep
        self._save(ledger)
        self._sweep(keep)
        return sent

    def _sweep(self, pending):
        """Bodies no waiting report names (a pass cut off between writing one and the ledger) off the disk."""
        folder = os.path.join(self.layers(), WAITING)
        named = {f"{p['id']}.envelope" for p in pending}
        for n in os.listdir(folder) if os.path.isdir(folder) else []:
            if n not in named:
                with contextlib.suppress(OSError):
                    os.unlink(os.path.join(folder, n))

    # --- what goes

    def event(self, step, to, updater, result, error, exc, extra):
        from core import appcode, packaged
        up = self.cfg.get("update") or {}
        name, man = updater.running() if updater is not None and hasattr(updater, "running") else (None, {})
        have = man.get("version") or ""
        mark = packaged.info() or {}
        app = getattr(updater, "app", None) or up.get("app") or {}
        layers = self.layers()
        info = {
            "step": step, "result": result, "error": error, "from": have, "to": to or "",
            "flavor": man.get("flavor") or mark.get("flavor") or "", "channel": str(up.get("channel") or "stable"),
            "os": f"{platform.system()} {platform.release()} ({platform.version()})", "arch": platform.machine(),
            "python": sys.version, "runtime": app.get("runtime") if isinstance(app, dict) else None,
            "packaged": mark, "claude": _safe(getattr(updater, "claude", None)),
            "free_bytes": _safe(lambda: shutil.disk_usage(self.state_dir).free),
            "machine": platform.node(), "user": _safe(getpass.getuser), "hub": hub_id(),
            "state_dir": self.state_dir, "busy_sessions": _safe(lambda: busy(self.state_dir)),
            "layer": {"running": name, "manifest": man, "pointer": os.path.join(self.state_dir, "code"),
                      "next": appcode.pending(self.state_dir),
                      "differs": _safe(lambda: self._differs(name))},
            "history": _read(os.path.join(layers, "history.json"), list),
            "failed": _read(os.path.join(layers, "failed.json"), dict),
            "trial": _read(os.path.join(self.state_dir, "update", "trial.json"), dict),
            "plugins": plugins(self.cfg), "modules": _safe(lambda: own_modules(self.cfg, self.state_dir)),
            **(extra or {})}
        event = {"event_id": uuid.uuid4().hex, "timestamp": self.now(), "platform": "python", "level": "error",
                 "logger": "hub.update", "release": f"agents-hub@{have or '?'}",
                 "environment": info["channel"], "server_name": info["machine"],
                 "user": {"id": info["hub"] or "", "username": info["user"] or ""},
                 "message": {"formatted": f"update {have or '?'} → {to or '?'}: {step} — {result}: {error}"[:8000]},
                 "tags": {"step": step, "from": have, "to": to or "", "flavor": info["flavor"],
                          "channel": info["channel"], "os": platform.system(), "arch": info["arch"],
                          "result": result or step},
                 # one issue per version and step in Sentry, as the hub sends it
                 "fingerprint": ["update", step, to or "?"],
                 "contexts": {"os": {"name": platform.system(), "version": platform.release()},
                              "runtime": {"name": platform.python_implementation(), "version": platform.python_version()}},
                 "extra": info}
        if exc is not None:
            event["exception"] = {"values": [{"type": type(exc).__name__, "value": str(exc)[:8000],
                                              "module": type(exc).__module__,
                                              "stacktrace": {"frames": frames(exc)}}]}
            info["traceback"] = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        files = {"update.log": _tail(os.path.join(self.state_dir, "logs", "hub-up.log"), keep=_update_line),
                 "update-trial.log": _tail(os.path.join(self.state_dir, "logs", "update-trial.log"))}
        return event, {k: v for k, v in files.items() if v}

    def _differs(self, name):
        from core import appcode
        if not name:
            return None
        root = os.path.join(self.layers(), name)
        return appcode.diff(root, appcode.load(root))[:DIFF_MAX]


def _safe(fn):
    """What fn gives, or the text of its error: a report is never lost to one of its own fields."""
    if fn is None:
        return None
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 — the field says what went wrong instead
        return f"{type(e).__name__}: {e}"[:300]


def hub_id():
    """The hub's id as the statistics name it (modules/stats: stats-id in the settings folder); never made here."""
    try:
        with open(os.path.join(catalog.config_dir(), "stats-id")) as f:
            return str(uuid.UUID(f.read().strip()))
    except (OSError, ValueError):
        return None


def busy(state_dir):
    """The sessions busy at the last checkpoint of the core (state.json)."""
    st = _read(os.path.join(state_dir, "state.json"), dict)
    sessions = (st.get("state") or st).get("sessions") or {}
    return sorted(n for n, s in sessions.items() if isinstance(s, dict) and s.get("state") == "busy")


def plugins(cfg):
    out = []
    for name, ext in sorted((cfg.get("extensions") or {}).items()):
        if isinstance(ext, dict):
            out.append({"id": name, "source": ext.get("source") or "local", "version": ext.get("version"),
                        "disabled": bool(ext.get("disabled"))})
    return out


def own_modules(cfg, state_dir):
    from core import modules
    return [{"name": m["name"], "version": m["version"], "origin": m["origin"], "dir": m["dir"],
             **({"errors": m["errors"][:5]} if m.get("errors") else {})}
            for m in modules.discover(cfg, state_dir, builtin=False)]


def _post(where, body):
    """POST one envelope. -> "sent" | "retry" (no network, 429, 5xx) | the text of a refusal."""
    from core import nethttp
    headers = {"Content-Type": "application/x-sentry-envelope", "User-Agent": "agents-hub (update report)",
               "X-Sentry-Auth": f"Sentry sentry_version=7, sentry_client=agents-hub/1, sentry_key={where['key']}"}
    if os.environ.get("HUB_NO_REPORT") and urllib.parse.urlsplit(where["url"]).hostname not in ("127.0.0.1", "::1",
                                                                                                "localhost"):
        return "retry"   # tests (tests/__init__.py): never the real receiver
    try:
        status, _, answer = nethttp.request("POST", where["url"], body, headers, timeout=TIMEOUT, limit=2000)
    except OSError as e:
        print(f"update: the report did not go: {e}", flush=True)
        return "retry"
    if 200 <= status < 300:
        return "sent"
    if status == 429 or status >= 500:
        return "retry"
    with contextlib.suppress(Exception):
        return f"HTTP {status}: {answer[:300].decode('utf-8', 'replace')}"
    return f"HTTP {status}"
