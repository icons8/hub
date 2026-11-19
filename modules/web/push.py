"""Push notifications to the phone client — about reasons that are waiting for the owner (the push spec): a question
as a card or a form, a permission, connecting a tool, an `owner.notify` notification. The list of open reasons is the
core snapshot's `alerts` (core/owner.py alerts); the module compares it every 3 s: a reason appeared — a banner; the
number `v` grew (a reminder, a notification update) — the same banner with new text; a reason closed — a silent push
to clients that can dismiss banners (`caps: clear`).

Off by default (the "Settings → Modules" switch, `push.enabled` in hub.json) and until there is an APNs key for the
app itself: `push: {enabled, key_file, key_id, team_id, topic}` (a .p8 from "Keys" in the Apple Developer account,
topic is the app's bundle id). When enabled with a key, the start of the reason's text (up to 160 characters), the
session name, the reason's hash key, the card number and a random hub id are sent to Apple.
The client sends the device token, a level (`mode`: `none` — nothing; `asks`, `results` — questions, reminders and
urgent notifications; `all` — all notifications too) and what it can do (`caps`: `clear` — dismiss a banner with a
silent push); the tokens, what was sent and the hub id live in the state folder, `push.json` (0600). The APNs key and
the tokens never go to the journal, the snapshot or other modules.
A pass only queues pushes; sending runs in its own thread with the system curl over HTTP/2 (Python's stdlib has none),
the JWT is signed with openssl. Retries happen on a network failure, 429 and 5xx while the reason is open.
The first enable only remembers what is open; after a restart, what closed during the downtime is dismissed, and what
is new (opened after the previous pass and no older than 10 min) arrives as a banner.
"""
import base64
import hashlib
import json
import os
import re
import subprocess
import threading
import time
import uuid

from core import i18n, net, owner

HOST = "https://api.push.apple.com"
SANDBOX = "https://api.sandbox.push.apple.com"
EVERY = 3
BODY = 160
MAX_TOKENS = 20   # the owner's devices; beyond that the oldest are evicted (a session must not be able to inflate the fan-out)
MODES = ("none", "asks", "results", "all")   # client level; asks and results are the same for now
CAPS = ("clear",)          # what the client can do: clear — dismiss a shown banner on a silent push
CURL_SECONDS = 10          # a long network failure holds the queue no longer than this per push
RETRY = (3, 30, 300)       # retries until APNs accepts the push (network, 429, 5xx) while the reason is open
PAUSE_429 = 3600           # 429 — pause this device for an hour
LOG_403 = 3600             # 403 (key, topic) — log a line no more than once an hour
SILENT_EVERY = 20 * 60     # silent pushes — at most 3 an hour per device (Apple advises no more than 2-3)
EXPIRE = 24 * 3600         # APNs keeps a banner for a day while the phone is offline; silent pushes are not stored
OPEN_MAX = 60              # more open reasons than this — do not send the open list (the client removes nothing by it)
CLOSED_MAX = 40            # closed reasons in one push — the latest ones
PAYLOAD_MAX = 4096         # APNs payload limit
FRESH = 10 * 60            # after a restart: what opened during the downtime and is no older than this — as a banner
SAVE_EVERY = 60            # "last pass" in push.json — no more often than this (the rest is saved immediately)
BURST = 3                  # more new reasons in a pass than this — 2 banners and a "N more" batch
SENDER_HOUR = 6            # notification banners from one sender per hour; beyond that — its batch
DEVICE_HOUR = 30           # banners per device per hour from all senders together; beyond that — a batch
BATCH_EVERY = 10 * 60      # a batch replaces its banner no more often than this
HOUR = 3600
SUMMARY_WAIT = 4           # the push waits for the model's topic no longer than this; then the fallback, the topic goes to reminders
SUMMARY_PROMPT = (
    "The input is a question an AI agent session asks its owner, and the owner's language. Write for a phone lock "
    "screen, in the owner's language: topic — 2 to 6 words, the subject of the decision, no session or agent name, no "
    "labels like Summary or TL;DR, no ids; decision — one sentence, at most 160 characters: what the owner is asked to "
    "decide and the key fact for it. Only facts from the input: copy every number and date from it. Do not say whether "
    "it is urgent or by when.")
SUMMARY_SCHEMA = {"type": "object", "required": ["topic", "decision"],
                  "properties": {"topic": {"type": "string"}, "decision": {"type": "string"}}}


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _der_to_raw(der):
    """An ECDSA signature from DER (openssl) to raw r||s, 32 bytes each, as ES256 requires."""
    i = 3 if der[1] < 0x80 else 4
    rl = der[i]
    r = der[i + 1:i + 1 + rl]
    j = i + 1 + rl + 1
    sl = der[j]
    s = der[j + 1:j + 1 + sl]
    return r.lstrip(b"\x00").rjust(32, b"\x00") + s.lstrip(b"\x00").rjust(32, b"\x00")


def jwt(key_file, key_id, team_id, now=None):
    head = _b64(json.dumps({"alg": "ES256", "kid": key_id, "typ": "JWT"}).encode())
    body = _b64(json.dumps({"iss": team_id, "iat": int(now or time.time())}).encode())
    msg = f"{head}.{body}".encode()
    der = subprocess.run(["openssl", "dgst", "-sha256", "-sign", key_file], input=msg, capture_output=True,
                         check=True).stdout
    return f"{head}.{body}.{_b64(_der_to_raw(der))}"


def _curl_quote(v):
    """A value inside a curl config's quotes: \\ and " get a backslash; newlines (JSON and headers have none) become spaces."""
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ")


def headers(kind, collapse=None, now=None):
    """APNs headers: a banner is alert, priority 10, expires in a day, with a collapse-id (the same reason replaces
    rather than adds); a silent push is background, priority 5, not stored."""
    if kind == "background":
        return {"apns-push-type": "background", "apns-priority": "5", "apns-expiration": "0"}
    return {"apns-push-type": "alert", "apns-priority": "10",
            "apns-expiration": str(int((now or time.time()) + EXPIRE)),
            **({"apns-collapse-id": collapse} if collapse else {})}


JWT_EVERY = 30 * 60        # one provider token per half hour: APNs answers 429 if it changes more often than once in 20 min
_jwt = {}


def provider_token(cfg, now=None):
    now = time.time() if now is None else now
    who = (os.path.expanduser(cfg["key_file"]), cfg["key_id"], cfg["team_id"])
    have = _jwt.get(who)
    if not have or now - have[1] >= JWT_EVERY:
        have = _jwt[who] = (jwt(*who, now=now), now)
    return have[0]


def send_apns(cfg, token, payload, host=HOST, head=None):
    """→ (HTTP code, response text); 0 means a network failure. 410/400 BadDeviceToken — the token is no longer valid."""
    auth = provider_token(cfg)
    head = head or headers("alert")
    # the key (JWT), the question text and the address go in a curl config on stdin (-K -): argv is visible to any
    # process on the machine (ps)
    conf = "".join(f'{k} = "{_curl_quote(v)}"\n' for k, v in (
        ("header", f"authorization: bearer {auth}"), ("header", f"apns-topic: {cfg['topic']}"),
        *(("header", f"{k}: {v}") for k, v in head.items()),
        ("data-raw", json.dumps(payload, ensure_ascii=False)), ("noproxy", net.no_proxy()),
        ("url", f"{host}/3/device/{token}")))
    r = subprocess.run(["curl", "-sS", "--http2", "-m", str(CURL_SECONDS), "-o", "-", "-w", "\n%{http_code}", "-K", "-"],
                       input=conf, capture_output=True, encoding="utf-8", errors="replace")
    out, _, code = r.stdout.rpartition("\n")
    return (int(code) if code.isdigit() else 0), out or r.stderr[:200]


def device_count(state_dir):
    """How many devices are registered (for the card in "Modules")."""
    try:
        with open(os.path.join(state_dir, "push.json")) as f:
            return len(json.load(f).get("tokens") or {})
    except (OSError, ValueError, AttributeError):
        return 0


def summarize_model(text, lang, account=None):
    """The question's topic and decision, from Haiku via `claude -p`, like the watcher's "what it is working on" line
    (tests use HUB_NO_MODEL), on the pool account `account` (from the core snapshot)."""
    from modules.watch import watch
    return watch._claude(f"Owner's language: {i18n.name(lang)}\n\n{text}", SUMMARY_PROMPT, SUMMARY_SCHEMA,
                         account=account, by="push")


def checked(out, source):
    """The model's answer is acceptable: the topic and decision are non-empty after cleaning, and every number and date
    is in the source text (a model invents "decisions" and deadlines line by line). Otherwise None — the fallback."""
    if not isinstance(out, dict):
        return None
    title, body = owner.push_clean(out.get("topic") or ""), owner.push_clean(out.get("decision") or "")
    if not title or not body or any(n not in source for n in re.findall(r"\d+(?:[.,:/]\d+)*", f"{title} {body}")):
        return None
    return {"title": owner.push_cut(title, owner.PUSH_TITLE), "body": owner.push_cut(body, owner.PUSH_BODY)}


def wants(meta, alert, reminder=False):
    """Whether the device gets a banner for this reason: none — nothing; questions, tool requests, reminders and
    urgent notifications — every other level; other notifications — only all."""
    mode = meta.get("mode", "asks")
    if mode == "none":
        return False
    if alert["kind"] != "notice" or reminder or alert.get("urgent"):
        return True
    return mode == "all"


class Push:
    def __init__(self, state_dir, snapshot, config, sender=send_apns, lang=lambda: None, log=print,
                 summarize=summarize_model, spawn=None):
        self.path = os.path.join(state_dir, "push.json")
        self.lang = lang           # () -> the hub language (core/i18n.py human): notification captions
        self.snapshot = snapshot   # () -> {alerts: open reasons (core/owner.py alerts), sessions: the snapshot's sessions,
        #                                  account: the pool account (for the summarizer model)}
        self.config = config       # () -> hub.json
        self.sender = sender
        self.log = log
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.queue = []            # [{at, token, kind, alert?, attempt}] — flush sends them in its own thread
        self.open = {}             # reason id -> the reason (with key) from the last pass: the snapshot and open in the push come from it
        self.paused = {}           # token -> until when not to send (429)
        self.told403 = 0
        self.hour = {}             # ("s", sender) / ("d", token) -> banner times over the last hour
        self.summarize = summarize
        self.summaries = {}        # question id -> {done, text: {title, body} | None} (the summarizer)
        self.spawn = spawn or (lambda f: threading.Thread(target=f, daemon=True).start())
        self.saved = 0
        self.data = self._load()
        self.primed = False

    def _load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
        d = d if isinstance(d, dict) else {}
        hub = d.get("hub") if isinstance(d.get("hub"), str) and d.get("hub") else uuid.uuid4().hex
        data = {"hub": hub, "rev": d.get("rev") if isinstance(d.get("rev"), int) else 0, "last": d.get("last"),
                "tokens": d.get("tokens") if isinstance(d.get("tokens"), dict) else {}, "sent": {},
                # batches: id -> {members, sender, since, v, shown_v, shown_at, to} — open while its reason is open
                "batches": d.get("batches") if isinstance(d.get("batches"), dict) else {}}
        for k, v in (d.get("sent") or {}).items():   # the old format {id: ts} → {key, at, v: 0}
            v = v if isinstance(v, dict) else {"at": v, "v": 0}
            data["sent"][k] = {"key": self._key(k, hub), "at": v.get("at") or 0, "v": v.get("v") or 0}
        return data

    def _save(self, now=None):
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # device tokens — readable by the owner only
        with os.fdopen(fd, "w") as f:
            json.dump(self.data, f, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        self.saved = now or time.time()

    def _key(self, aid, hub=None):
        """A reason's key for the collapse-id and for the client to reconcile: h + 16 hex of sha256(<hub id>/<reason id>)
        — at most 64 bytes; session and tool names never go into the header."""
        return "h" + hashlib.sha256(f"{hub or self.data['hub']}/{aid}".encode()).hexdigest()[:16]

    def settings(self):
        c = self.config().get("push")
        c = c if isinstance(c, dict) else {}
        if c.get("enabled") is not True:
            return None
        return c if all(c.get(k) for k in ("key_file", "key_id", "team_id", "topic")) else None

    def register(self, token, env="production", mode=None, caps=None):
        token = str(token or "").strip().lower()
        if not token or len(token) < 32 or not all(ch in "0123456789abcdef" for ch in token):
            return False
        with self.lock:
            old = self.data["tokens"].get(token) or {}
            # without mode and caps (the client re-registers the token at launch; an old client) keep the previous ones;
            # an unknown mode means questions only
            had = old.get("mode", "asks")
            caps = old.get("caps") or [] if caps is None else \
                [c for c in CAPS if isinstance(caps, list) and c in caps]
            self.data["tokens"][token] = {**{k: old[k] for k in ("closed", "silent_at") if k in old},
                                          "env": "sandbox" if env == "sandbox" else "production", "at": time.time(),
                                          "mode": had if mode is None else mode if mode in MODES else "asks",
                                          **({"caps": caps} if caps else {})}
            for t in sorted(self.data["tokens"], key=lambda t: self.data["tokens"][t]["at"])[:-MAX_TOKENS]:
                del self.data["tokens"][t]
            self._save()
        return True

    def view(self):
        """For the page snapshot: open reasons with keys and rev — after the first pass, the ones that went into pushes
        (rev is consistent with them); before it, the ones computed now."""
        with self.lock:
            if self.primed:
                return {"alerts": [dict(a) for a in self.open.values()], "alerts_rev": self.data["rev"]}
        alerts = (self.snapshot() or {}).get("alerts") or []
        return {"alerts": [{**a, "key": self._key(a["id"])} for a in alerts], "alerts_rev": self.data["rev"]}

    # --- pass -----------------------------------------------------------------------------------------------------

    def tick(self, now=None):
        """One pass: compare the open reasons with what was sent and queue pushes. → how many were queued."""
        now = time.time() if now is None else now
        if not self.settings():   # off or no key: neither snapshot parsing nor "remember" — the first pass after enabling will remember
            self.primed = False
            if self.data.get("last") is not None:
                with self.lock:
                    self.data["last"] = None
                    self._save(now)
            return 0
        snap = self.snapshot() or {}
        alerts = {a["id"]: {**a, "key": self._key(a["id"])} for a in snap.get("alerts") or []}
        summarize = (self.config().get("push") or {}).get("summarize") is True
        sources = {f"q:{s.get('name')}:{r.get('request_id')}": owner.question_text(r) for s in snap.get("sessions") or []
                   for r in s.get("pending") or [] if r.get("tool") == "AskUserQuestion"} if summarize else {}
        asks = []   # questions for the summarizer — after the lock: its thread writes the answer under the same lock
        with self.lock:
            for i in [i for i in self.summaries if i not in alerts]:
                del self.summaries[i]
            sent, tokens = self.data["sent"], self.data["tokens"]
            fresh = sorted((a for i, a in alerts.items() if i not in sent), key=lambda a: a["since"])
            grown = [a for i, a in alerts.items() if i in sent and a["v"] > sent[i]["v"]]
            for bid, b in list(self.data["batches"].items()):   # a batch stays open while at least one of its reasons is open
                b["members"] = [m for m in b["members"] if m in alerts]
                if not b["members"]:
                    del self.data["batches"][bid]
                elif b["shown_v"]:
                    alerts[bid] = self._batch_alert(bid, b, alerts)
            closed = [sent[i]["key"] for i in sent if i not in alerts]
            first, last = not self.primed, self.data.get("last")
            self.primed = True
            if first:   # after a restart: only remember the old ones; on the first enable (no last) — everything
                keep = [a for a in fresh if last is not None and a["since"] > last and now - a["since"] <= FRESH]
                for a in fresh:
                    if a not in keep:
                        sent[a["id"]] = {"key": a["key"], "at": now, "v": a["v"]}
                fresh = keep
                if last is None:
                    closed, grown = [], []
            changed = bool(fresh or grown or closed) or set(self.open) != set(alerts)
            if changed:
                self.data["rev"] += 1
            self.open = alerts
            for i in [i for i in sent if i not in alerts]:
                del sent[i]
            for t, meta in tokens.items():   # what closed goes to every device, until a silent push or a banner
                if closed and meta.get("mode") != "none":
                    meta["closed"] = ((meta.get("closed") or []) + closed)[-CLOSED_MAX:]
            queued = 0
            if not tokens:   # nobody to send to — do not accumulate "new", or everything would arrive at once with the first token
                for a in fresh + grown:
                    sent[a["id"]] = {"key": a["key"], "at": now, "v": a["v"]}
            else:
                for a in fresh:
                    if summarize and a["id"] in sources and a["id"] not in self.summaries:
                        self.summaries[a["id"]] = {"done": False, "text": None}
                        asks.append((a["id"], sources[a["id"]]))
                queued = self._enqueue(fresh, grown, now)
            self.data["last"] = now
            if changed or queued or now - self.saved >= SAVE_EVERY:
                self._save(now)
        lang = self.lang() if asks else None
        account = (snap.get("account") or {}).get("active")
        for aid, source in asks:
            self._summarize(aid, source, lang, account)
        if queued:
            self.wake.set()
        return queued

    def _recent(self, who, now):
        times = [t for t in self.hour.get(who) or [] if now - t < HOUR]
        self.hour[who] = times
        return times

    def _batch_alert(self, bid, b, alerts):
        n, lang = len(b["members"]), self.lang()
        title = (i18n.tr(lang, "web.push.batch_from", sender=owner._sender_label(b["sender"]), count=n) if b["sender"]
                 else i18n.tr(lang, "web.push.batch", count=n))
        body = ", ".join(dict.fromkeys(str(alerts[m].get("title") or "") for m in b["members"] if m in alerts))
        return {"id": bid, "kind": "batch", "key": self._key(bid), "title": title, "body": body, "since": b["since"],
                "v": b["v"]}

    def _to_batch(self, a, sender, tokens, now):
        """Put a reason into a batch (the sender's or the shared one): an open one is reused, otherwise a new one; the
        devices that need it are tokens."""
        bid = next((i for i, b in self.data["batches"].items() if b["sender"] == sender), None)
        if bid is None:
            bid = f"b:{sender}:{self.data['rev']}" if sender else f"b:{self.data['rev']}"
            self.data["batches"][bid] = {"members": [], "sender": sender, "since": now, "v": 0, "shown_v": 0,
                                         "shown_at": 0, "to": []}
        b = self.data["batches"][bid]
        if a["id"] not in b["members"]:
            b["members"].append(a["id"])
            b["v"] += 1
        b["to"] = sorted(set(b["to"]) | set(tokens))

    def _enqueue(self, fresh, grown, now):
        """Banners for new and grown reasons, batches and silent pushes per device (under the lock)."""
        queued, sent, tokens = 0, self.data["sent"], self.data["tokens"]
        live = tokens   # devices paused after a 429 too: the banner is queued for the end of the pause
        single, burst = [], 0
        morning = sum(1 for a in fresh if a.get("quiet")) > 1   # what piled up in non-working hours (quiet_review): one push
        for a, reminder in [(a, False) for a in fresh] + [(a, a["kind"] != "notice") for a in grown]:
            sent[a["id"]] = {"key": a["key"], "at": now, "v": a["v"]}
            who = [t for t, m in live.items() if wants(m, a, reminder)]
            if not who:
                continue
            if a["kind"] == "notice":   # a noisy sender: 6 banners an hour, then its batch
                if len(self._recent(("s", a["sender"]), now)) >= SENDER_HOUR:
                    self._to_batch(a, a["sender"], who, now)
                    continue
                self.hour[("s", a["sender"])].append(now)
            if not reminder and a in fresh and morning and a.get("quiet"):
                self._to_batch(a, None, who, now)
                continue
            if not reminder and a in fresh:
                burst += 1
                if burst > BURST - 1 and len(fresh) > BURST:   # many at once: 2 banners, the rest as a batch
                    self._to_batch(a, None, who, now)
                    continue
            single.append((a, who))
        for a, who in single:
            for t in who:
                if len(self._recent(("d", t), now)) >= DEVICE_HOUR:
                    self._to_batch(a, None, [t], now)
                    continue
                self.hour[("d", t)].append(now)
                hold = self.summaries.get(a["id"])
                self.queue.append({"at": max(now, self.paused.get(t, 0)), "token": t, "kind": "alert", "alert": a, "attempt": 0,
                                   **({"hold": now + SUMMARY_WAIT} if hold and not hold["done"] else {})})
                queued += 1
        for bid, b in self.data["batches"].items():   # the batch grew — replace its banner, no more often than once in 10 min
            if b["v"] > b["shown_v"] and b["to"] and (not b["shown_v"] or now - b["shown_at"] >= BATCH_EVERY):
                a = self._batch_alert(bid, b, self.open)
                self.open[bid] = a
                sent[bid] = {"key": a["key"], "at": now, "v": a["v"]}
                b.update(shown_v=b["v"], shown_at=now)
                for t in [t for t in b.pop("to") if t in live]:
                    self.queue.append({"at": max(now, self.paused.get(t, 0)), "token": t, "kind": "alert", "alert": a, "attempt": 0})
                    queued += 1
                b["to"] = []
        for t, meta in self.data["tokens"].items():
            if ("clear" in (meta.get("caps") or []) and meta.get("closed") and meta.get("mode") != "none"
                    and now - (meta.get("silent_at") or 0) >= SILENT_EVERY and self.paused.get(t, 0) <= now
                    and not any(j["token"] == t and j["kind"] == "background" for j in self.queue)):
                meta["silent_at"] = now
                self.queue.append({"at": now, "token": t, "kind": "background", "attempt": 0})
                queued += 1
        return queued

    def _summarize(self, aid, source, lang, account=None):
        """The topic and decision from the model, in its own thread (not under the lock): the push waits up to
        SUMMARY_WAIT, a late answer is kept for reminders."""

        def work():
            try:
                text = checked(self.summarize(source, lang, account=account), source)
            except Exception:   # noqa: BLE001 — the model did not answer, a limit, HUB_NO_MODEL: the fallback
                text = None
            with self.lock:
                if aid in self.summaries:
                    self.summaries[aid] = {"done": True, "text": text}
            self.wake.set()
        self.spawn(work)

    # --- sending --------------------------------------------------------------------------------------------------

    def payload(self, job, meta):
        """The push body: a banner carries the reason's text, a silent push only the reconciliation. hub: the hub id,
        rev, key, closed ones and (if there are no more than OPEN_MAX) all open ones — the client removes shown banners
        that are not in open."""
        opened = [a["key"] for a in self.open.values()]
        hub = {"v": 1, "hub": self.data["hub"], "rev": self.data["rev"],
               "closed": list(meta.get("closed") or [])[-CLOSED_MAX:],
               **({"open": opened} if len(opened) <= OPEN_MAX else {})}
        if job["kind"] == "background":
            body = {"aps": {"content-available": 1}, "hub": hub}
        else:
            a = job["alert"]
            better = (self.summaries.get(a["id"]) or {}).get("text")
            if better:   # the topic always, the text only for the first banner (a reminder has its own: "Waiting for an answer ...")
                a = {**a, "title": better["title"], **({"body": better["body"]} if not a["v"] else {})}
            alert = {"title": a.get("title") or "", **({"subtitle": a["subtitle"]} if a.get("subtitle") else {}),
                     "body": " ".join(str(a.get("body") or "").split())[:BODY]}
            thread = a.get("session") or a.get("sender") or "hub"
            body = {"aps": {"alert": alert, **({"sound": "default"} if not job["attempt"] else {}),
                            "thread-id": thread, "mutable-content": 1},
                    **({"session": a["session"]} if a.get("session") else {}),
                    "hub": {**hub, "key": a["key"], "kind": a["kind"],
                            **({"n": a["v"]} if a["kind"] != "notice" else {}),   # reminder number
                            **({"sender": a["sender"]} if a.get("sender") else {}),   # client: core/watch — quiet
                            **({"card": a["card"]} if a.get("card") else {})}}
        size = lambda: len(json.dumps(body, ensure_ascii=False).encode())   # noqa: E731
        if size() > PAYLOAD_MAX:   # APNs limit: first drop open, then shorten closed
            body["hub"].pop("open", None)
            while size() > PAYLOAD_MAX and body["hub"]["closed"]:
                body["hub"]["closed"] = body["hub"]["closed"][1:]
        return body

    def flush(self, now=None):
        """Send everything that is due. → how many APNs accepted. The network call runs without the lock: a long curl
        does not hold up a pass."""
        accepted = 0
        while True:
            t = time.time() if now is None else now
            with self.lock:
                cfg = self.settings()
                due = next((j for j in sorted(self.queue, key=lambda j: j["at"]) if j["at"] <= t and (
                    t >= j.get("hold", 0) or (self.summaries.get(j["alert"]["id"]) or {}).get("done", True))), None)
                if due is None or not cfg:
                    if not cfg:
                        self.queue = []
                    return accepted
                self.queue.remove(due)
                meta = self.data["tokens"].get(due["token"])
                if (meta is None or due["kind"] == "alert" and (self.open.get(due["alert"]["id"]) or {}).get("v")
                        != due["alert"]["v"]):
                    continue   # the device is gone, the reason is closed or the text is stale (v grew) — do not send
                if self.paused.get(due["token"], 0) > t:   # 429: the device is paused — requeue for the end of the pause
                    self.queue.append({**due, "at": self.paused[due["token"]]})
                    continue
                body = self.payload(due, meta)
                took = list(body["hub"]["closed"])
                host = SANDBOX if meta.get("env") == "sandbox" else HOST
                head = headers(due["kind"], due.get("alert", {}).get("key"), t)
            try:
                code, out = self.sender(cfg, due["token"], body, host, head)
            except Exception as e:  # noqa: BLE001 — openssl, the key, etc.: treated like a network failure
                code, out = 0, str(e)[:200]
            with self.lock:
                accepted += self._result(due, code, out, took, t)
        return accepted

    def _result(self, job, code, out, took, now):
        token = job["token"]
        meta = self.data["tokens"].get(token)
        if code == 200:
            if meta is not None and job["kind"] == "background":
                meta["closed"] = [k for k in meta.get("closed") or [] if k not in took]
                self._save(now)
            return 1
        if code == 410 or (code == 400 and "BadDeviceToken" in out):
            self.data["tokens"].pop(token, None)
            self.queue = [j for j in self.queue if j["token"] != token]
            self._save(now)
            return 0
        if code == 429:
            self.paused[token] = now + PAUSE_429
        if code == 403:
            _jwt.clear()   # the provider token was rejected (the .p8 was fixed, it expired) — the next push signs a new one
        if code == 403 and now - self.told403 >= LOG_403:
            self.told403 = now
            self.log(f"push: APNs 403 — {str(out)[:200]}", flush=True)
        if (code == 0 or code == 429 or code >= 500) and job["kind"] == "alert" and job["attempt"] < len(RETRY):
            wait = PAUSE_429 if code == 429 else RETRY[job["attempt"]]
            self.queue.append({**job, "at": now + wait, "attempt": job["attempt"] + 1})
        return 0

    def run(self, stop):
        threading.Thread(target=self.sending, args=(stop,), daemon=True).start()
        while not stop.wait(EVERY):
            try:
                self.tick()
            except Exception as e:   # noqa: BLE001 — notifications must not bring the page down
                print(f"push: {e}", flush=True)

    def sending(self, stop):
        while not stop.is_set():
            self.wake.wait(1)
            self.wake.clear()
            try:
                self.flush()
            except Exception as e:   # noqa: BLE001
                print(f"push: {e}", flush=True)

