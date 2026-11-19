"""The relay transport of the hub bus: a part of hub-up that carries the core's envelopes to the relay the owner joined
and brings the envelopes waiting there (spec hubbus §5.8, §5.11). It sleeps while the bus is off or no relay is joined.

It sees only the transport interface the core writes (core/busd.py): `bus/transport.json` (whether the bus works, our
fingerprint, the relay's address and key, whom we take envelopes from, what to hand) and `bus/out/<out>.env`. It puts
an envelope from the relay into the core's folder `bus/in/` and offers it (`bus.offered`), says an envelope went
(`bus.handed`); the core checks and decides everything. It reads the transport key (`<settings>/bus-relay/key`) and
never the hub's keys: it cannot sign an envelope and does not see a text.

  - the allow list goes to the relay when it changes (and every half an hour);
  - an envelope is handed at once; a refused or failed one again after 30 s, 2 min, 10 min, 30 min (then every 30);
    a handed message that has no receipt again after 10 min, 30 min, 2 h, 6 h (the relay keeps one copy per id);
    receipts and introductions are not repeated by a timer;
  - a long poll brings what waits; each envelope is written (fsync, rename) and offered before it is acknowledged; with
    200 files or 20 MB in `bus/in/` nothing more is taken until the core has read them.
"""
import base64
import hashlib
import json
import os
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
from core import busd, busrelay, spool  # noqa: E402

SRC = "bus-relay"
IDLE = 5                                  # seconds between looks while the bus is off or no relay is joined
LOOK = 1                                  # seconds between looks at what to hand
RELAY_WIDE = ("network", "not_relay", "bad_answer", "unknown_hub", "removed", "rate", "clock", "unsigned",
              "bad_signature", "replay", "error", "no_transport_key", "full")   # the relay as a whole, not this envelope
RETRY = (30, 120, 600, 1800)              # after a failed hand
AGAIN = (600, 1800, 7200, 21600)          # a handed message without a receipt
ALLOW_EVERY = 1800
IN_FILES, IN_BYTES = 200, 20 * 2 ** 20    # the core's folder bus/in/: not more waiting for the core
SENDS_PER_PASS = 20                       # a pass hands at most this many, then looks at the view again
PEER_EVERY = 600                          # a contact's version on the relay is asked again after this long
WATCH_EVERY = 600                         # the permits of our links are looked at on the relay this often
JOB_RETRY = (30, 120, 600, 1800)          # a job the relay could not take now (the network, a limit) — again after


class Transport:
    def __init__(self, state_dir, config_dir=None, put=None, clock=time.time, log=None):
        self.state_dir, self.config_dir, self.clock = state_dir, config_dir, clock
        self.put = put or (lambda kind, data: spool.put(os.path.join(state_dir, "spool"), kind, None, data, src=SRC))
        self.log = log or (lambda text: print(f"{time.strftime('%d.%m %H:%M:%S')} bus-relay: {text}"[:400], flush=True))
        self.tries = {}            # out -> (n, next try)
        self.allowed = (None, 0)   # (the list sent, when)
        self.client_of = (None, None)
        self.lock = threading.Lock()
        self.status = {"sent": 0, "got": 0}
        self.stop = threading.Event()
        self.job_tries = {}        # job -> (n, next try)
        self.peers = {}            # fp -> (version heard, when asked)
        self.watched = (None, 0)   # (what /v1/invites said about our links' permits, when)
        self.hello_due, self.hello_at = False, 0

    # what the core says
    def view(self):
        """The view while the bus works and the hub is on a relay — or has a job for one (a registration by a link's
        permit comes before the hub is on any relay)."""
        v = busd.read_transport(self.state_dir)
        if not v or not v.get("enabled") or not v.get("me") or not (v.get("relay") or v.get("jobs")):
            return None
        return v

    def client(self, v):
        found = busrelay.transport_key(self.config_dir)
        if found is None:
            raise busrelay.Error("no_transport_key", "no transport key: hub bus relay join <invitation> again")
        sig = (v["relay"]["url"], v["relay"]["key"], v["relay"].get("ts"), v["me"], found[0])
        if self.client_of[0] != sig:   # another relay or a new join: the relay knows nothing sent before
            c = busrelay.Client(v["relay"]["url"], v["relay"]["key"], v["me"], found[0])
            self.client_of = (sig, c)
            self.allowed = (None, 0)
            self.peers = {}
            self.hello_due = True   # the relay's protocol is asked once per client (chores)
        return self.client_of[1]

    # the relay's jobs, the versions, the permits (spec hubbus/ux §4.0, §4.3, §5.1)
    def chores(self, v):
        """The jobs the core named, then — at their pace — the contacts' versions and our links' permits."""
        now = self.clock()
        live = {j["job"] for j in v.get("jobs") or []}
        for k in [k for k in self.job_tries if k not in live]:
            del self.job_tries[k]
        for job in v.get("jobs") or []:
            n, at = self.job_tries.get(job["job"], (0, None))
            if at is not None and now < at:
                continue
            try:
                code = self.job(v, job)
            except busrelay.Error as e:
                self.job_tries[job["job"]] = (n + 1, now + JOB_RETRY[min(n, len(JOB_RETRY) - 1)])
                if e.code in RELAY_WIDE or e.code in ("joins_day", "quota", "busy"):
                    self.failed(e)
                    continue
                code = e.code
            if code == "wait":
                continue
            self.put("bus.job_done", {"job": job["job"], **({"code": code} if code else {})})
            self.job_tries[job["job"]] = (n, now + JOB_RETRY[0])   # until the core drops it from the view
        if not v.get("relay"):
            return
        c = self.client(v)
        if self.hello_due or now - self.hello_at >= PEER_EVERY:   # the relay may be updated while the hub runs
            self.hello_due, self.hello_at = False, now   # a failing relay is not asked again at every pass
            seen = c.hello()   # a relay without v2 gets no v2 envelope: the core decides by what this says
            if seen != v["relay"].get("bus_v"):
                self.put("bus.relay_seen", {"bus_v": seen})
        if (v["relay"].get("bus_v") or 1) >= 2:   # a relay of the old code has no /v1/peer: the core counts it as v1
            for fp in v.get("peers") or []:
                seen, at = self.peers.get(fp, (None, None))
                if fp not in (v.get("peers_now") or []) and at is not None and now - at < PEER_EVERY:
                    continue
                try:
                    got = c.peer(fp)
                except busrelay.Error as e:
                    if e.code in RELAY_WIDE:
                        raise
                    got = None
                self.peers[fp] = (got, now)
                if got != seen or fp in (v.get("peers_now") or []):
                    self.put("bus.peer_seen", {"fp": fp, "bus_v": got})
        watch = set(v.get("watch") or [])
        if watch and now - self.watched[1] >= WATCH_EVERY:
            self.watched = (self.watched[0], now)
            items = [{"id": x["id"], "used_by": x.get("used_by"), "used": x.get("used")} for x in c.invites()
                     if isinstance(x, dict) and x.get("id") in watch and x.get("used_by")]
            if items and items != self.watched[0]:
                self.put("bus.invites_seen", {"items": items})
            self.watched = (items, now)

    def job(self, v, job):
        """One job: None — done; a code — refused for good (the core says why to the owner); "wait" — its file is not
        there yet (the core writes it before the view names the job: a cut in between)."""
        path = os.path.join(busd.jobs_dir(self.state_dir), f"{job['job']}.json")
        secret = None
        if job["do"] in ("invite", "join"):
            try:
                with open(path, encoding="utf-8") as f:
                    secret = json.load(f)
            except (OSError, ValueError):
                return "wait"
        if job["do"] == "join":
            found = busrelay.transport_key(self.config_dir)
            if found is None:
                return "no_transport_key"
            busrelay.Client(secret["url"], secret["key"], secret["fp"], found[0]).join_prepared(secret)
            return None
        if not v.get("relay"):
            return "wait"
        c = self.client(v)
        if job["do"] == "invite":
            try:
                c.invite(secret["invite"])
            except busrelay.Error as e:
                if e.code != "conflict":   # recorded before, the answer was lost: the same permit is there
                    raise
            return None
        if job["do"] == "revoke":
            return None if c.revoke(job["invite"]) else "used"
        return "unknown_job"

    # the outgoing side
    def due(self, o, now):
        """Is the envelope to be handed now: a new one — at once, a failed one — by its retry, a handed message
        without a receipt — by AGAIN; a receipt or an introduction once handed — never again (the core drops them)."""
        n, at = self.tries.get(o["out"], (0, None))
        if o["status"] == "signed":
            return at is None or now >= at
        if o.get("kind") != "message" or o.get("remote"):
            return False
        if at is None:
            at = (o.get("handed") or now) + AGAIN[0]
            self.tries[o["out"]] = (0, at)
        return now >= at

    def hand(self, v):
        """One pass: the relay's jobs and checks, the allow list if it changed, then the due envelopes."""
        self.chores(v)
        if not v.get("relay"):
            return 0
        c = self.client(v)
        now = self.clock()
        allow = list(v.get("allow") or [])
        if self.allowed[0] != allow or now - self.allowed[1] >= ALLOW_EVERY:
            c.allow(allow)
            self.allowed = (allow, now)
        live = {o["out"] for o in v.get("out") or []}
        for k in [k for k in self.tries if k not in live]:
            del self.tries[k]   # delivered, expired or refused: the core no longer names it
        sent = 0
        for o in v.get("out") or []:
            if sent >= SENDS_PER_PASS or self.stop.is_set():
                break
            if not self.due(o, now):
                continue
            path = os.path.join(busd.out_dir(self.state_dir), f"{o['out']}.env")
            try:
                with open(path, "rb") as f:
                    raw = f.read()
            except OSError:
                continue   # the core writes the file before the view; gone — no longer to hand
            n, _ = self.tries.get(o["out"], (0, None))
            try:
                try:
                    c.send(o["fp"], raw)
                except busrelay.Error as e:
                    # an introduction of ours with this id already waits for the receiver: the same answer of a link
                    # sent again (its id is the nonce the link committed to) — the one waiting is it, handed
                    if not (e.code == "conflict" and o.get("kind") == "intro" and o["status"] == "signed"):
                        raise
            except busrelay.Error as e:
                if o["status"] == "signed":
                    self.tries[o["out"]] = (n + 1, now + RETRY[min(n, len(RETRY) - 1)])
                else:   # a handed message: its next repeat is the next AGAIN step, the count of repeats stays
                    self.tries[o["out"]] = (n, now + AGAIN[min(n, len(AGAIN) - 1)])
                self.waiting(o["out"], e.code, n + 1)
                if e.code in RELAY_WIDE:
                    raise   # the relay as a whole: the pass stops, the loop waits longer
                continue
            sent += 1
            self.status["sent"] += 1
            self.waiting(o["out"], None)
            if o["status"] == "signed":
                self.put("bus.handed", {"out": o["out"], "via": "relay"})
                self.tries[o["out"]] = (0, now + AGAIN[0])
            else:
                self.tries[o["out"]] = (n + 1, now + AGAIN[min(n + 1, len(AGAIN) - 1)])
        self.ok()
        return sent

    # the incoming side
    def room(self):
        folder = busd.in_dir(self.state_dir)
        try:
            names = [n for n in os.listdir(folder) if n.endswith(".env")]
            size = sum(os.path.getsize(os.path.join(folder, n)) for n in names)
        except OSError:
            return IN_FILES, IN_BYTES
        return IN_FILES - len(names), IN_BYTES - size

    def take(self, v, wait=busrelay.WAIT):
        """One long poll: every envelope that fits into the core's folder is written and offered, then acknowledged;
        what does not fit stays on the relay for the next poll."""
        files, size = self.room()
        if files <= 0 or size <= 0 or not v.get("relay"):
            self.stop.wait(IDLE)   # the core has not read what it has (the relay keeps the rest), or no relay yet
            return 0
        items = self.client(v).inbox(wait)
        folder = busd.in_dir(self.state_dir)
        os.makedirs(folder, exist_ok=True)
        done = []
        for item in items:
            try:
                raw = base64.b64decode(item["envelope"], validate=True)
            except (KeyError, TypeError, ValueError):
                if isinstance(item, dict) and isinstance(item.get("from"), str) and isinstance(item.get("id"), str):
                    done.append((item["from"], item["id"]))   # not an envelope: acknowledged and dropped, not asked again
                continue
            if files <= 0 or size < len(raw):
                break
            digest = hashlib.sha256(raw).hexdigest()
            path = os.path.join(folder, digest + ".env")
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            self.put("bus.offered", {"path": path, "sha256": digest, "size": len(raw)})
            done.append((item["from"], item["id"]))
            files, size = files - 1, size - len(raw)
        if done:
            self.client(v).ack(done)
            self.status["got"] += len(done)
        elif items:
            self.stop.wait(IDLE)   # the first waiting one does not fit yet: the relay keeps it, no asking again at once
        self.ok()
        return len(done)

    # what it says about itself (hub bus relay status)
    def waiting(self, out, code, tries=0):
        with self.lock:
            w = {x["out"]: x for x in self.status.get("waiting") or []}
            if code is None:
                w.pop(out, None)
            else:
                w[out] = {"out": out, "code": code, "tries": tries}
            self.status["waiting"] = sorted(w.values(), key=lambda x: x["out"])[:50]
            self.write()

    def ok(self):
        """A success: written when the last error goes or a minute has passed (not every second of looking)."""
        with self.lock:
            now = int(self.clock())
            if self.status.pop("error", None) is not None or now - self.status.get("ok", 0) >= 60:
                self.status["ok"] = now
                self.write()
            else:
                self.status["ok"] = self.status.get("ok") or now

    def failed(self, e):
        with self.lock:
            self.status["error"] = {"code": e.code, "text": str(e)[:300], "ts": int(self.clock())}
            self.write()

    def write(self):
        path = busd.relay_status_path(self.state_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(self.status, f, ensure_ascii=False)
        os.replace(path + ".tmp", path)

    # the loops
    def _loop(self, step, name, every):
        """step until stopped: every `every` seconds while it works; after a failure the pause doubles up to 5 min."""
        pause = 0
        while not self.stop.is_set():
            v = self.view()
            if v is None:
                self.stop.wait(IDLE)
                continue
            try:
                step(v)
                pause = 0
            except busrelay.Error as e:
                self.failed(e)
                self.log(f"{name}: {e.code}: {e}")
                pause = min(max(pause * 2, IDLE), 300)
            except Exception as e:  # noqa: BLE001 — a step does not bring the part down
                self.log(f"{name}: {type(e).__name__}: {e}")
                pause = min(max(pause * 2, IDLE), 300)
            self.stop.wait(pause or every)

    def run(self):
        """Two loops: handing (a look every second) and the long poll (one at a time)."""
        t = threading.Thread(target=self._loop, args=(self.hand, "hand", LOOK), name="bus-relay-hand", daemon=True)
        t.start()
        self._loop(self.take, "take", 0)


def main():
    from core.daemon import default_state_dir
    Transport(default_state_dir()).run()
