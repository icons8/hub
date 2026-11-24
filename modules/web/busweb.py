"""The page's «Hubs» (spec hubbus/ux §3.2–3.4, §3.6, §7, §8, §10): link this hub with another one, paste a link, the
contacts with their pair code and «Matches», the owners' chat, the rights, the agents a contact sees, the unlink.

The page server calls the owner's path (core/busflow.Flow) in its own process: a press on the page is the owner's word
(the facts go with channel web and owner — the core's strict guard for the bus, guards.from_owner_strict, takes only
that, never a session or a module); the core decides everything as from the terminal. The slow part (pip for the
library, waiting for the relay and the other hub) runs in a thread of this server, one job at a time — the page asks for
its steps. The secrets stay where they are: a pasted link goes to the core as a file 0600 (Flow.join), a made link
comes only in the answer to the page that asked for it, neither goes into a log, the journal or the snapshot.

POST only (the page's own requests: local host and X-Hub, server.py): /api/bus/act {op, …} — a quick action and the
views, /api/bus/start {what: link|join, …} — a job, /api/bus/job — its steps and outcome."""
import copy
import os
import sys
import threading
import time
import traceback

from core import bus, busflow, busrelay, busstate

TTLS = {"1h": 3600, "24h": 86400, "7d": 604800}
DECIDE = 10.0       # seconds for the core to record a request (hub in the terminal waits 5; a page job may wait longer)
JOB_KEEP = 3600     # a finished job's outcome is kept this long for the page (a reload sees «Linked», the link text)
CATCH_UP = 3.0      # seconds the page's reader may take to read what the core has just written
TEXT_MAX = 16384    # a pasted text (a link is ~3 KB at most, spec §4.1)
SAID_TAIL = 16 << 20   # the owner's chat words are looked for in this much of the journal's tail at first
JOB_STATE_WAIT = 30.0   # a job (link, join) waits this long for the reader that is busy reading the journal


class BusWeb:
    def __init__(self, web):
        self.web = web
        self.lock = threading.RLock()   # public() is read under it from start()
        self.job = None
        self.n = 0
        self.config_dir = None   # the hub's settings folder (the keys); None — core/catalog.config_dir
        self.decide_wait = DECIDE
        self.said_lock, self.said_at, self._said = threading.Lock(), (None, 0), {}   # the owner's chat words read so far
        self.found = {}   # {seq: text | None} looked up in the archives once
        self.idle = None          # between two looks at the core's view (the tests' stand: None — half a second)

    # --- the core's state and requests -------------------------------------------------------------------------------

    def state(self):
        """The bus part of the core's state and the sessions' kinds, read after the journal's tail (the page's reader);
        None while the journal is read for the first time."""
        def pick(reader):
            st = reader.state
            return {"bus": copy.deepcopy(st.get("bus") or {}),
                    "deputies": copy.deepcopy(st.get("deputies") or {}),   # the owner's deputies (core/deputies.py)
                    "sessions": {n: {"kind": s.get("kind"), "state": s.get("state")}
                                 for n, s in (st.get("sessions") or {}).items()}}
        tail, path = self.web.tail, os.path.join(self.web.state_dir, "journal.jsonl")
        try:
            end = os.path.getsize(path)   # what the core has written by now: the core's answer just read is in it
        except OSError:
            end = 0
        until = time.monotonic() + CATCH_UP
        while True:
            got = tail.peek(lambda r: (tail.offset, pick(r)), self.web.READ_WAIT, poll=True)
            if got is tail.BUSY:
                return None
            if got[0] >= end or time.monotonic() >= until:
                return got[1]
            time.sleep(0.05)   # a fresh journal is read by the reader's own thread (a request reads only the tail)

    def flow(self, say=None, wait=0):
        """wait: seconds to wait for a reader that is busy reading the journal — only the background job (run) waits;
        a request of the page does not, as before (act() and start() answer 503 when the reader is busy before the flow)."""
        rejected = {}

        def put(type_, entity, data, say=False):
            seq, why, _ = self.web.decide(type_, entity, data, wait=self.decide_wait)   # channel web, owner: his press
            if why:
                rejected[seq] = self.web.said(why)   # its core phrases in the language of the page's request
            return seq

        def current():
            # the reader may be busy for a moment after the job started (a rotation, a long read): wait for it, the
            # job is the owner's press — failing at once showed «failure (Error)» and nothing was asked of the core
            end = time.time() + wait
            got = self.state()
            while got is None and time.time() < end:
                (self.idle or (lambda: time.sleep(0.5)))()
                got = self.state()
            if got is None:
                raise bus.Error("reader_busy", "")   # not the relay's «busy» (its queue is full)
            return got
        return busflow.Flow(put, current, self.web.state_dir, rejected=rejected,
                            note=lambda type_, data: self.web.put(type_, None, data), say=say, idle=self.idle,
                            config_dir=self.config_dir)

    def said(self):
        """{seq: text} of the owner's chat requests (a delivered message keeps no text in the state): the journal's
        tail once, then only what was appended — a contact's page asks for its history every few seconds. Read through
        one open file (its inode and size), so a rotation between the look and the read is not mixed in."""
        path = os.path.join(self.web.state_dir, "journal.jsonl")
        with self.said_lock:
            try:
                with open(path, "rb") as f:
                    got = os.fstat(f.fileno())
                    ino, pos = self.said_at
                    if ino != got.st_ino or pos > got.st_size:   # a new journal (rotation): its tail again
                        self._said, pos = {}, max(0, got.st_size - SAID_TAIL)
                    f.seek(pos)
                    chunk = f.read(got.st_size - pos)
            except OSError:
                return self._said
            cut = chunk.rfind(b"\n") + 1   # whole lines only: the core may be in the middle of one
            busflow.said_lines(chunk[:cut], self._said)
            self.said_at = (got.st_ino, pos + cut)
            return self._said   # read with get only; new words are added under the lock

    def history(self, st, name):
        """A contact's feed; the words of ours the state no longer keeps and the journal's tail has not — once from the
        archives (core/journal.events_at: the owner's request, or the session's message a liaison's send named)."""
        rows = busflow.chat_rows(st, name, said=self.said())
        lost = [r for r in rows if r["way"] == "out" and r.get("text") is None]
        want = set()
        for r in lost:
            want.add(r.get("of"))
            if isinstance(r.get("basis"), str) and r["basis"][1:].isdigit():
                want.add(int(r["basis"][1:]))
        want = {n for n in want if isinstance(n, int) and n not in self.found}
        if want:
            from core import journal
            got = journal.events_at(self.web.state_dir, want)
            with self.said_lock:
                for n in want:
                    data = (got.get(n) or {}).get("data") or {}
                    self.found[n] = data.get("text") if isinstance(data.get("text"), str) else None
        for r in lost:
            base = r["basis"][1:] if isinstance(r.get("basis"), str) else ""
            r["text"] = self.found.get(r.get("of")) or (self.found.get(int(base)) if base.isdigit() else None)
        for r in rows:
            r.pop("of", None)
            r.pop("basis", None)
        return rows

    def t(self, key, **params):
        return self.web.t(key, **params)

    # --- /api/bus/act ------------------------------------------------------------------------------------------------

    OPS = ("view", "preview", "history", "link_text", "revoke", "cancel", "off", "chat", "verify", "unlink", "rename",
           "name", "people", "person", "mode")

    def act(self, body):
        op = body.get("op")
        if op not in self.OPS:
            return 400, {"error": self.t("web.bus.bad_op")}
        if op == "people":   # the records of the owner's contacts book for «Whose hub»: names and kinds of paths — the
            return 200, {"people": busflow.people()}   # book is a file of its own, not the journal: no wait for the reader
        st = self.state()
        if st is None:
            return 503, {"error": self.t("web.srv.receipt_busy")}
        if op == "view":   # a hub on no relay yet: the public one its install names, said before «Link»
            # read from the install's pages only once the bus is on or the owner pressed «Link» (its consent shows)
            asked = st["bus"].get("enabled") or body.get("consent") is True
            public, reading = busflow.public_relay_now(self.web.state_dir) if asked and not st["bus"].get("relay") \
                else (None, False)
            return 200, {**busflow.page_view(st), "job": self.public(self.job), "public_relay": public,
                         "public_relay_reading": reading}
        if op == "preview":
            return self.preview(body, st)
        names = (st["bus"].get("contacts") or {})
        name = body.get("name")
        if name is not None and not isinstance(name, str):
            return 400, {"error": self.t("web.bus.bad_op")}
        if op == "history":
            if name not in names:
                return 404, {"error": busflow.why("no_contact")}
            return 200, {"rows": self.history(st, name)}
        flow = self.flow()
        if op == "link_text":   # the text of our open link again (the page was reloaded): the core's file 0600
            lid = body.get("id")
            rec = (st["bus"].get("links") or {}).get(lid) if isinstance(lid, str) else None
            if not rec or rec.get("state") != "open" or (rec.get("exp") or 0) <= time.time():
                return 404, {"error": busflow.why("used")}
            try:
                with open(busflow.busd.link_path(self.web.state_dir, lid), encoding="ascii") as f:
                    return 200, {"value": f.read().strip()}
            except (OSError, ValueError):
                return 404, {"error": busflow.why("lost")}
        if op == "revoke":
            return self.answer(flow.revoke(str(body.get("id") or "")))
        if op == "cancel":   # B: «Cancel» while the join waits for A
            done, _ = flow.ask("bus.join_cancel_requested", {"id": str(body.get("id") or "")})
            return self.answer(done)
        if op == "off":
            done, _ = flow.ask("bus.off_requested", {})
            return self.answer(done)
        if op == "name":   # this hub's own name for the other hubs: the one required setting
            text = body.get("text")
            if not isinstance(text, str):
                return 400, {"error": self.t("web.bus.bad_op")}
            done, _ = flow.ask("bus.name_requested", {"name": text})
            return self.answer(done)
        if op == "mode":   # nobody sits at this hub's page: the linked hubs are its owners (spec d2505378, part D, remote)
            if body.get("mode") not in busstate.MODES:
                return 400, {"error": self.t("web.bus.bad_op")}
            done, _ = flow.ask("bus.mode_requested", {"mode": body["mode"]})
            return self.answer(done)
        if not isinstance(name, str) or name not in names:
            return 404, {"error": busflow.why("no_contact")}
        if op == "chat":
            text = body.get("text")
            if not isinstance(text, str) or not text.strip():
                return 400, {"error": self.t("web.bus.empty")}
            done = flow.chat(name, text, confirm=body.get("confirm") is True)
            return self.answer(done, secret=done.code == "secret")
        if op == "verify":
            code = body.get("code")
            return self.answer(flow.verify(name, code if isinstance(code, str) and code.strip() else None))
        if op == "person":   # whose hub this is: a record of the book (or a new one), or none to clear
            spec = body.get("person")
            person = None
            if spec is not None:
                person, code = busflow.person_for(spec)
                if person is None:
                    return 409, {"error": busflow.why(code), "code": code}
            done, _ = flow.ask("bus.contact_requested", {"op": "link", "name": name, "person": person})
            return self.answer(done)
        if op == "unlink":
            done, _ = flow.ask("bus.contact_requested", {"op": "remove", "name": name})
            return self.answer(done)
        if op == "rename":   # the owner's own name for the hub: only here, the other hub is not told; empty — cleared
            alias = body.get("alias")
            if not isinstance(alias, str):
                return 400, {"error": self.t("web.bus.bad_op")}
            done, _ = flow.ask("bus.contact_requested", {"op": "rename", "name": name, "alias": alias})
            return self.answer(done)
        return 400, {"error": self.t("web.bus.bad_op")}

    def answer(self, done, secret=False):
        if done.ok:
            return 200, {"ok": True}
        return 409, {"error": done.text or done.code, "code": done.code, **({"confirm": True} if secret else {})}

    def preview(self, body, st):
        """What a pasted text is (spec §3.3): a link — whose, its relay, what this hub still needs; the relay operator's
        invitation — its relay; a card — the advanced way (a command); anything else — not a link."""
        text = body.get("text")
        if not isinstance(text, str) or len(text) > TEXT_MAX:
            return 400, {"error": busflow.why("not_a_link"), "code": "not_a_link"}
        kind, token = busflow.find(text)
        if kind == "link":
            try:
                info = busflow.preview(token, st["bus"], time.time())
            except bus.Error as e:
                return 409, {"error": busflow.why(e.code, str(e) if e.code == "other_relay" else ""), "code": e.code,
                             "kind": kind}
            return 200, {"kind": kind, **{k: info[k] for k in ("id", "label", "relay", "exp", "needs")}}
        if kind == "invite":
            try:
                inv = busrelay.read_invite(token)[0]
            except busrelay.Error as e:
                return 409, {"error": busflow.why("bad_invite", str(e)), "code": "bad_invite", "kind": kind}
            relay = st["bus"].get("relay") or {}
            if relay and relay.get("key") != inv["key"]:
                return 409, {"error": busflow.why("relay_other", relay.get("url") or ""), "code": "relay_other",
                             "kind": kind}
            return 200, {"kind": kind, "relay": inv["relay"], "joined": bool(relay),
                         "needs": [] if st["bus"].get("enabled") else ["enable"]}
        if kind == "card":
            return 409, {"error": busflow.why("card"), "code": "card", "kind": kind,
                         "command": "hub bus contact add <card> --as <name>"}
        return 409, {"error": busflow.why("not_a_link"), "code": "not_a_link", "kind": None}

    # --- jobs: link, join --------------------------------------------------------------------------------------------

    def start(self, body):
        what = body.get("what")
        st = self.state()
        if st is None:
            return 503, {"error": self.t("web.srv.receipt_busy")}
        if what in ("link", "join") and not st["bus"].get("name"):   # before any consent: nothing goes without the name
            return 409, {"error": busflow.why("no_name"), "code": "no_name"}
        if what == "link":
            ttl = body.get("ttl") or "24h"
            ttl = TTLS.get(ttl) if isinstance(ttl, str) else None
            if ttl is None:
                return 400, {"error": self.t("web.bus.bad_op")}
            # the first time: one explicit consent to the keys, the library and the outside relay (spec §3.2, §5.3)
            if not st["bus"].get("enabled") and body.get("consent") is not True:
                return 409, {"error": self.t("web.bus.consent_needed"), "consent": True}
            label = body.get("label")   # the name for this invitation alone (the page's field), else the hub's own
            if label is not None and not isinstance(label, str):
                return 400, {"error": self.t("web.bus.bad_op")}
            person, why = self.person(body)
            if why:
                return why
            args = {"ttl": ttl, "label": label, "person": person}   # checked by the flow before anything is done
        elif what == "join":
            text = body.get("text")
            if not isinstance(text, str) or len(text) > TEXT_MAX or busflow.kind_of(text) not in ("link", "invite"):
                return 400, {"error": busflow.why("not_a_link"), "code": "not_a_link"}
            person, why = self.person(body)
            if why:
                return why
            args = {"text": text, "person": person}   # a secret: in this thread's memory only, never in the job's view
        else:
            return 400, {"error": self.t("web.bus.bad_op")}
        with self.lock:
            if self.job and self.job["done"] is None:
                return 409, {"error": self.t("web.bus.busy"), "job": self.public(self.job)}
            self.n += 1
            job = self.job = {"n": self.n, "what": what, "steps": [], "done": None, "ts": time.time()}
        threading.Thread(target=self.run, args=(job, what, args), name="bus-job", daemon=True).start()
        return 202, {"ok": True, "job": self.public(job)}

    def person(self, body):
        """«Whose hub» of an invitation or an accepted one: (the book's {id, name} or None, a refusal or None)."""
        spec = body.get("person")
        if spec is None:
            return None, None
        person, code = busflow.person_for(spec)
        if person is None:
            return None, (409, {"error": busflow.why(code), "code": code})
        return person, None

    def run(self, job, what, args):
        def say(step, state, text):
            with self.lock:
                row = {"step": step, "state": state, "text": busflow._printable(text)[:300]}
                if step == "join":   # B's steps after the request: one row per step of the core's join
                    row = {"step": "join:" + str(state or ""), "state": "ok" if state != "run" else "run",
                           "text": row["text"]}
                steps = job["steps"]
                if steps and steps[-1]["step"] == row["step"]:
                    steps[-1] = row
                else:
                    steps.append(row)
        flow = self.flow(say, wait=JOB_STATE_WAIT)
        try:
            if what == "link":
                done = flow.link(ttl=args["ttl"], label=args["label"], person=args["person"])
            else:
                done = flow.join(args.pop("text"), person=args["person"])
        except (bus.Error, busrelay.Error) as e:   # a refusal with a code: its own words, not the class's name
            done = busflow.Done(e.code, busflow.why(e.code, str(e)))   # why() decides which codes show the cause
        except Exception as e:  # noqa: BLE001 — a job always ends with an outcome the page can show
            # the type and the stack, no text: the job holds a pasted link (as the Telegram path, telegram/hubs.py)
            sys.stderr.write(f"web: hub link, {what}: {type(e).__name__}\n" + "".join(traceback.format_tb(e.__traceback__)))
            done = busflow.Done("error", busflow.why("error", type(e).__name__))
        out = {"code": done.code, "text": done.text, "command": done.command, "step": done.step,
               **{k: done.data[k] for k in ("id", "exp", "posted", "label", "pair_code", "state", "url")
                  if k in done.data}}
        with self.lock:
            job["done"] = out
            job["value"] = done.value if what == "link" and done.ok else None   # the link: the page that asked shows it
            job["end"] = time.time()

    def public(self, job, value=False):
        if not job or (job.get("end") and time.time() - job["end"] > JOB_KEEP):
            return None
        with self.lock:
            out = {k: copy.deepcopy(job[k]) for k in ("n", "what", "steps", "done", "ts")}
            if value and job.get("value"):
                out["value"] = job["value"]
        return out

    def job_view(self, body):
        return 200, {"job": self.public(self.job, value=True)}


def snapshot_key(st):
    """The snapshot's `bus` (the session list's «Hubs» group): None until the bus has contacts."""
    return busflow.list_view(st)

