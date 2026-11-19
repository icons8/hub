"""The hub bus, slice L3 (spec hubbus/ux §3.2–3.4, §3.6, §7, §8, §10): the page's requests — /api/bus/act, /api/bus/start,
/api/bus/job of the page server (modules/web/busweb.py) call the owner's path (core/busflow.Flow) for the owner: two stand
hubs (tests/bus_stand.py — a core, its page server and its relay transport in one process each) through the relay of this
tree. The path end to end by requests; the made link only in the answer to the job, never in a journal, a state or the
snapshot; the first link asks consent; one job at a time; a session's request of the same kind is refused by the core
(its words are not the owner's); the views the page reads (core/busflow.page_view, list_view, chat_rows). The page itself
in a browser — tests/test_web_e2e.HubBus."""
import tests  # noqa: F401 — keeps the machine's settings out
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock

from tests.test_hubbus import need_lib, need_ssh
from core import bus, busflow, busrelay, spool

STAND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bus_stand.py")


class Stand:
    def __init__(self, case, label, named=True):
        self.root = tempfile.mkdtemp(prefix="hub-l3-")
        case.addCleanup(shutil.rmtree, self.root, True)
        self.log = open(os.path.join(self.root, "log"), "w+")
        self.p = subprocess.Popen([sys.executable, STAND, self.root, label] + ([] if named else ["--unnamed"]), stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=self.log, text=True)
        case.addCleanup(self.stop)
        self.port = int(self.p.stdout.readline())

    def stop(self):
        self.p.stdin.close()
        try:
            self.p.wait(60)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait(5)
        self.p.stdout.close()
        self.log.close()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=90)
        h = {"Host": f"127.0.0.1:{self.port}", "Content-Type": "application/json", "X-Hub": "1", **(headers or {})}
        c.request(method, path, None if body is None else json.dumps(body), h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, json.loads(data or b"{}")

    def act(self, op, **body):
        return self.req("POST", "/api/bus/act", {"op": op, **body})

    def job(self, seconds=180):
        end = time.time() + seconds
        while time.time() < end:
            _, d = self.req("POST", "/api/bus/job", {})
            if d["job"] and d["job"]["done"]:
                return d["job"]
            time.sleep(0.3)
        raise AssertionError("the job did not end")

    def until(self, fn, seconds=120):
        """Waits until fn gives something. The seconds are a fuse against a hang, not a limit on speed: a loaded machine
        takes tens of seconds for a pass through the relay, and the wait ends at once when the condition holds."""
        end = time.time() + seconds
        while time.time() < end:
            got = fn()
            if got:
                return got
            time.sleep(0.3)
        return fn()

    def journal(self):
        try:
            with open(os.path.join(self.root, "state", "journal.jsonl"), encoding="utf-8") as f:
                return [json.loads(line) for line in f if line.strip()]
        except FileNotFoundError:   # nothing written yet
            return []

    def kept(self):
        """Everything the hub keeps where a secret must not be: the journal, the state, the snapshot, the logs."""
        parts = []
        for folder, _, files in os.walk(os.path.join(self.root, "state")):
            for name in files:
                if name.endswith((".jsonl", ".json", ".log")) and "links" not in folder:
                    with open(os.path.join(folder, name), encoding="utf-8", errors="replace") as f:
                        parts.append(f.read())
        parts.append(json.dumps(self.req("GET", "/api/snapshot")[1]))
        with open(os.path.join(self.root, "log"), encoding="utf-8", errors="replace") as f:
            parts.append(f.read())
        return "\n".join(parts)


@need_lib
@need_ssh
class ThePageThroughTheRelay(unittest.TestCase):
    def setUp(self):
        from tests.test_hubbus_l0_relay import Relay
        self.relay = Relay()
        self.addCleanup(self.relay.stop)
        if self.relay.version() < 2:
            self.skipTest("the relay of this tree does not announce bus_v 2")
        self.a, self.b = Stand(self, "Hub A"), Stand(self, "Hub B")

    def link(self):
        """A on the relay (the operator's invitation), A's link, B pasted it: both linked."""
        a, b = self.a, self.b
        self.assertEqual(a.req("POST", "/api/bus/start", {"what": "join", "text": self.relay.invite()})[0], 202)
        self.assertEqual(a.job()["done"]["code"], "ok")
        self.assertEqual(a.req("POST", "/api/bus/start", {"what": "link"})[0], 202)
        job = a.job()
        self.assertEqual(job["done"]["code"], "ok", job)
        link = job["value"]
        self.assertEqual(busflow.kind_of(link), "link")
        self.assertEqual(b.req("POST", "/api/bus/start", {"what": "join", "text": "take it: " + link})[0], 202)
        job = b.job()
        self.assertEqual((job["done"]["code"], job["done"]["state"]), ("ok", "linked"), job)
        def coded():   # A has the code once B's introduction came through the relay
            cs = a.act("view")[1]["contacts"]
            return cs[0] if cs and cs[0]["code"] else None
        ca = a.until(coded)
        cb = b.act("view")[1]["contacts"][0]
        return link, ca, cb, job

    def test_link_chat_check_rights_unlink(self):
        a, b = self.a, self.b
        link, ca, cb, job = self.link()
        # one pair code on both hubs; the hub's name from Settings is what the other one sees
        self.assertEqual(ca["code"], cb["code"])
        self.assertEqual(job["done"]["pair_code"], cb["code"])
        self.assertEqual((ca["label"], cb["label"]), ("Hub B", "Hub A"))
        self.assertEqual((ca["verified"], ca["via"], ca["status"]), (False, "link", "known"))
        # the link: in the job's answer only — never in a journal, a state, the snapshot, the job's view or a log
        for hub in (a, b):
            self.assertNotIn(link[len("hub-link-1:"):][:40], hub.kept())
            self.assertNotIn(link[len("hub-link-1:"):][:40], json.dumps(hub.act("view")[1]))
        # the session list's group
        snap = b.req("GET", "/api/snapshot")[1]
        self.assertIn("bus", snap["features"])
        self.assertEqual([c["name"] for c in snap["bus"]["contacts"]], [cb["name"]])
        # «Tell the hub» waits for the check (its words go to the liaison of that hub): «Matches» on both
        code, d = a.act("chat", name=ca["name"], text="Hello from A")
        self.assertEqual(code, 409)
        self.assertIn("is not checked", d["error"])
        self.assertEqual(a.act("verify", name=ca["name"])[0], 200)
        self.assertEqual(b.act("verify", name=cb["name"])[0], 200)
        self.assertEqual(a.act("verify", name=ca["name"], code="000-000" if ca["code"] != "000-000" else "111-111")[0],
                         409)
        # no model turn, the state of ours, the other side reads it among the liaison's threads
        turns = sum(e["type"] == "turn.started" for hub in (a, b) for e in hub.journal())
        self.assertEqual(a.act("chat", name=ca["name"], text="Hello from A")[0], 200)
        rows = a.until(lambda: [r for r in a.act("history", name=ca["name"])[1]["rows"] if r["status"] == "delivered"])
        self.assertEqual([(r["way"], r["kind"], r["text"]) for r in rows], [("out", "chat", "Hello from A")])
        got = b.until(lambda: b.act("history", name=cb["name"])[1]["rows"])
        self.assertEqual([(r["way"], r["kind"], r["text"], r["agent"]) for r in got],
                         [("in", "thread", "Hello from A", "owner")])
        self.assertEqual(sum(e["type"] == "turn.started" for hub in (a, b) for e in hub.journal()), turns)
        # a key-like text asks first: 409 with confirm, then it goes
        key = "sk-" + "ant-" + "api03-" + "Q" * 40
        code, d = a.act("chat", name=ca["name"], text="key " + key)
        self.assertEqual((code, d.get("code"), d.get("confirm")), (409, "secret", True))
        self.assertTrue(a.act("view")[1]["contacts"][0]["verified"])
        for gone in ("rights", "expose"):   # no table of rights any more: the liaison decides
            self.assertEqual(a.act(gone, name=ca["name"], may_write=["maps"])[0], 400)
        for key in ("may_write", "auto_from", "exposed"):
            self.assertNotIn(key, a.act("view")[1]["contacts"][0])
        # whose hub it is: a record of the owner's contacts book, new or existing, or none
        import tempfile
        from unittest import mock
        book = os.path.join(tempfile.mkdtemp(prefix="book-"), "contacts.json")
        with mock.patch.dict(os.environ, {"HUB_CONTACTS": book}):
            self.assertEqual(a.act("people")[1], {"people": []})
            self.assertEqual(a.act("person", name=ca["name"], person={"new": "Pat Lee"})[0], 200)
            self.assertEqual(a.act("view")[1]["contacts"][0]["person"], "Pat Lee")
            self.assertEqual([p["name"] for p in a.act("people")[1]["people"]], ["Pat Lee"])
            self.assertEqual(a.act("person", name=ca["name"], person={"name": "no such person"})[0], 409)
            # a link pasted into the name field (the page cuts it at 100 characters) is no name: refused, no record made
            pasted = bus.LINK_PREFIX + "A" * 89
            code, d = a.act("person", name=ca["name"], person={"new": pasted})
            self.assertEqual((code, d.get("code")), (409, "bad_person"))
            self.assertEqual(a.req("POST", "/api/bus/start", {"what": "join", "text": link, "person": {"new": pasted}})[0], 409)
            self.assertEqual([p["name"] for p in a.act("people")[1]["people"]], ["Pat Lee"])
            self.assertEqual(a.act("person", name=ca["name"], person=None)[0], 200)
            self.assertIsNone(a.act("view")[1]["contacts"][0]["person"])
        # «Unlink» on A: B loses the contact by itself
        self.assertEqual(a.act("unlink", name=ca["name"])[0], 200)
        self.assertEqual(a.act("view")[1]["contacts"], [])
        self.assertEqual(b.until(lambda: not b.act("view")[1]["contacts"]), True)
        self.assertTrue(b.until(lambda: b.req("GET", "/api/snapshot")[1]["bus"] is None))

    def test_the_first_link_asks_consent_one_job_at_a_time_and_refusals_in_words(self):
        a = self.a
        code, d = a.req("POST", "/api/bus/start", {"what": "link"})
        self.assertEqual((code, d.get("consent")), (409, True))
        self.assertFalse(a.act("view")[1]["enabled"])   # nothing was turned on without the consent
        self.assertEqual(a.req("POST", "/api/bus/start", {"what": "link", "consent": True})[0], 202)
        code, d = a.req("POST", "/api/bus/start", {"what": "link", "consent": True})
        self.assertIn(code, (409, 202))   # 409 while the first runs (it may be over already on a fast machine)
        job = a.job()
        # no relay yet: the failed step names it, the bus is on (the keys are made)
        self.assertEqual((job["done"]["code"], job["done"]["step"]), ("no_relay", "relay"))
        self.assertEqual([s["step"] for s in job["steps"]][:4], ["ssh", "lib", "selfcheck", "keys"])
        self.assertTrue(a.act("view")[1]["enabled"])
        # the paste field: what kind of text it is, in the owner's words
        self.assertEqual(a.act("preview", text="just words")[1]["code"], "not_a_link")
        self.assertEqual(a.act("preview", text=self.relay.invite())[1]["kind"], "invite")
        self.assertEqual(a.req("POST", "/api/bus/start", {"what": "join", "text": "not a link"})[0], 400)
        self.assertEqual(a.act("chat", name="nobody", text="x")[0], 404)
        self.assertEqual(a.act("dance")[0], 400)
        self.assertEqual(a.act("history", name=[1])[0], 400)   # not a name: a refusal, not the server's error
        self.assertEqual(a.req("POST", "/api/bus/start", {"what": "link", "ttl": [1]})[0], 400)
        # a request not from the page: refused (cross-site and outside requests never reach the bus)
        c = http.client.HTTPConnection("127.0.0.1", a.port, timeout=10)
        c.request("POST", "/api/bus/start", json.dumps({"what": "link"}), {"Host": f"127.0.0.1:{a.port}"})
        self.assertEqual(c.getresponse().status, 403)

    def test_without_the_hub_name_nothing_is_made_or_accepted_and_the_invitation_may_carry_its_own(self):
        """The hub's name for the others is the one required setting: the page refuses before any consent,
        the name is the owner's through the page, an invitation signs with it or with the field of that invitation."""
        a = Stand(self, "Hub A", named=False)
        self.assertIsNone(a.act("view")[1]["name"])
        for body in ({"what": "link"}, {"what": "link", "consent": True}, {"what": "join", "text": self.relay.invite()}):
            code, d = a.req("POST", "/api/bus/start", body)
            self.assertEqual((code, d.get("code"), bool(d.get("consent"))), (409, "no_name", False), body)
        self.assertFalse(a.act("view")[1]["enabled"])
        for text in ("", "   ", "x" * 101, 5):
            self.assertIn(a.act("name", text=text)[0], (400, 409), text)
        self.assertEqual(a.act("name", text="  Sales department hub ")[0], 200)
        self.assertEqual(a.until(lambda: a.act("view")[1]["name"]), "Sales department hub")
        self.assertEqual(a.req("POST", "/api/bus/start", {"what": "join", "text": self.relay.invite()})[0], 202)
        self.assertEqual(a.job()["done"]["code"], "ok")
        for label, seen in ((None, "Sales department hub"), ("  Hub for partners ", "Hub for partners")):
            self.assertEqual(a.req("POST", "/api/bus/start", {"what": "link", **({"label": label} if label else {})})[0], 202)
            job = a.job()
            self.assertEqual(job["done"]["code"], "ok", job)
            self.assertEqual(bus.read_link(job["value"], time.time())["card"]["label"], seen)
        self.assertEqual(a.req("POST", "/api/bus/start", {"what": "link", "label": 5})[0], 400)
        self.assertEqual(a.req("POST", "/api/bus/start", {"what": "link", "label": "x" * 101})[0], 202)
        self.assertEqual(a.job()["done"]["code"], "bad_label")   # the flow refuses a name it cannot sign with, before any step

    def test_a_session_cannot_link_or_write_for_the_owner(self):
        """The same requests from a session (channel session, its sender) — the core refuses them: only the page, the
        terminal outside sessions and the owner's Telegram speak for the owner on the bus (guards.from_owner_strict)."""
        a = self.a
        box = os.path.join(a.root, "state", "spool")
        for kind, data in (("bus.on_requested", {}), ("bus.link_requested", {"ttl": 86400}),
                           ("bus.chat_requested", {"contact": "x", "text": "hi"})):
            spool.put(box, kind, None, {**data, "channel": "session", "owner": False, "sender": "maps"}, src="session")
        want = {"bus.on_requested", "bus.link_requested", "bus.chat_requested"}

        def refusals():
            out = [e for e in a.journal() if e["type"] == "core.rejected" and (e["data"].get("type") or "").startswith("bus.")]
            return out if {e["data"]["type"] for e in out} >= want else None

        # wait for all three: the core takes the spool one by one, and on a loaded machine the first refusal came alone
        refused = a.until(refusals) or [e for e in a.journal() if e["type"] == "core.rejected"]
        got = {e["data"]["type"] for e in refused}
        self.assertEqual(got, want, refused)
        self.assertFalse(a.act("view")[1]["enabled"])
        # the page's own request carries the owner's channel
        a.req("POST", "/api/bus/start", {"what": "link", "consent": True})
        a.job()
        mine = [e for e in a.journal() if e["type"] == "bus.on_requested" and e["data"].get("channel") == "web"]
        self.assertEqual([(e["data"]["channel"], e["data"]["owner"]) for e in mine], [("web", True)])


class Views(unittest.TestCase):
    """What the page reads from the core's state: no keys, no envelopes, no link texts; the pair code, the check, the
    rights; the owners' words with the state of ours; the liaison's threads with that hub."""

    ST = {"sessions": {"hub-core": {"kind": "hub", "state": "idle"}},
          "bus": {"enabled": True, "crypto_ready": True, "me": {"fp": "hb1-me", "label": "Mine", "sign_pub": "K"},
                  "relay": {"url": "https://relay.example.test", "key": "R", "bus_v": 2},
                  "contacts": {"maps": {"fp": "hb1-x", "label": "Maps hub", "status": "known", "verified": False,
                                        "via": "link", "pair": "p1", "sas": {"id": "p1", "code": "482-913"},
                                        "sign_pub": "S", "enc_pub": "E", "may_write": [], "bus_v": 2}},
                  "links": {"l1": {"state": "open", "exp": 2_000_000_000, "ts": 1, "pub": "SECRET-PUB",
                                   "nonce": "SECRET-N"}},
                  "inbox": {"b5": {"contact": "maps", "for": "owner", "text": "hello", "ts": 5, "status": "ok",
                                   "outcome": "owner"},
                            "b6": {"contact": "maps", "text": "for the agent", "ts": 6, "status": "unverified",
                                   "agent_from": "api"}},
                  "out": {"o7": {"contact": "maps", "kind": "message", "for": "owner", "status": "handed", "ts": 7,
                                 "of": 7, "envelope": "ENVELOPE"},
                          "o8": {"contact": "maps", "kind": "message", "for": "owner", "status": "refused",
                                 "code": "secret", "ts": 8, "text": "key"}}}}

    def test_page_view(self):
        v = busflow.page_view(self.ST, now=10)
        self.assertEqual(v["contacts"][0]["code"], "482-913")
        self.assertEqual(v["contacts"][0]["chat_in"], 1)
        self.assertEqual(v["hub_session"], "hub-core")
        self.assertEqual([l["id"] for l in v["links"]], ["l1"])
        text = json.dumps(v)
        for secret in ("SECRET-PUB", "SECRET-N", "ENVELOPE", '"S"', '"E"', '"K"'):
            self.assertNotIn(secret, text)

    def test_list_view_only_with_contacts(self):
        self.assertIsNone(busflow.list_view({"bus": {"enabled": True}}))
        self.assertIsNone(busflow.list_view({}))
        v = busflow.list_view(self.ST)
        self.assertEqual(v["contacts"][0]["waiting"], 1)   # the handed one; the refused one is not on its way

    def test_list_view_says_when_the_hub_last_wrote_any_word(self):
        # "Now" lists a hub that wrote something new: the words of its sessions (a thread, an answer to ours) count, not
        # only those of its owner (chat_in) — a muted one does not
        st = json.loads(json.dumps(self.ST))
        v = busflow.list_view(st)["contacts"][0]
        self.assertEqual((v["chat_in"], v["heard"]), (1, 6))
        st["bus"]["inbox"]["b9"] = {"contact": "maps", "text": "muted", "ts": 9, "status": "muted", "agent_from": "api"}
        self.assertEqual(busflow.list_view(st)["contacts"][0]["heard"], 6)
        st["bus"]["inbox"] = {}
        self.assertEqual(busflow.list_view(st)["contacts"][0]["heard"], 0)

    def test_list_view_says_when_the_hub_last_answered_the_owner(self):
        # "Now" by the rules of sessions: a hub that answered him (in the thread of his words, or a session's request
        # from his turn, given back to that session: the core marks it when it delivers) stays until he opens it — the
        # core keeps what he opened (`seen`); any other word is "on its own"
        st = json.loads(json.dumps(self.ST))
        v = busflow.list_view(st)["contacts"][0]
        self.assertEqual((v["answered"], v["seen"]), (0, 0))
        inbox = st["bus"]["inbox"]
        inbox["b7"] = {"contact": "maps", "text": "the tiles are ready", "ts": 7, "status": "ok", "source": "api",
                       "in_owner_turn": True, "passed_to": ["web"]}   # passed to another session: not his answer
        self.assertEqual(busflow.list_view(st)["contacts"][0]["answered"], 0)
        inbox["b7"]["answered_owner"] = True
        self.assertEqual(busflow.list_view(st)["contacts"][0]["answered"], 7)
        inbox["b8"] = {"contact": "maps", "text": "yes", "ts": 8, "status": "ok", "for": "owner", "reply": True}
        inbox["b9"] = {"contact": "maps", "text": "news", "ts": 9, "status": "ok", "source": "api", "passed_to": ["api"]}
        inbox["b10"] = {"contact": "maps", "text": "muted", "ts": 10, "status": "muted", "reply": True}
        # remote mode: an answer in his thread went to the hub session as his task, not to him
        inbox["b11"] = {"contact": "maps", "text": "for the hub", "ts": 11, "status": "ok", "reply": True}
        st["owner"] = {"seen": {":hub:maps": 8.5, "maps": 99}}   # a session of the same name is another mark
        v = busflow.list_view(st)["contacts"][0]
        self.assertEqual((v["answered"], v["heard"], v["seen"]), (8, 11, 8.5))

    def test_chat_rows(self):
        path = os.path.join(tempfile.mkdtemp(prefix="hub-l3v-"), "journal.jsonl")
        self.addCleanup(shutil.rmtree, os.path.dirname(path), True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"seq": 7, "type": "bus.chat_requested", "data": {"text": "from the journal"}},
                               separators=(",", ":")) + "\n")
        rows = busflow.chat_rows(self.ST, "maps", path)
        self.assertEqual([(r["id"], r["way"], r["kind"], r["text"], r["status"]) for r in rows],
                         [("b5", "in", "chat", "hello", "ok"), ("b6", "in", "thread", "for the agent", "unverified"),
                          ("o7", "out", "chat", "from the journal", "sent")])   # a key-like refused one never went

    def test_words_no_longer_in_the_journal_tail_come_from_the_archive_once(self):
        """A delivered word keeps no text in the state; after a rotation it is only in an archive — the page server
        finds it there (core/journal.events_at) once, and the liaison's send gets the words of the session it named."""
        from core import journal
        from modules.web.busweb import BusWeb
        state = tempfile.mkdtemp(prefix="hub-l3a-")
        self.addCleanup(shutil.rmtree, state, True)
        os.makedirs(os.path.join(state, journal.ARCHIVE))
        line = lambda e: json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n"   # noqa: E731
        with open(os.path.join(state, journal.ARCHIVE, "journal-1-9.jsonl"), "w", encoding="utf-8") as f:
            f.write(line({"seq": 7, "ts": 7, "type": "bus.chat_requested", "data": {"contact": "maps", "text": "older"}}))
            f.write(line({"seq": 8, "ts": 8, "type": "message.received", "data": {"to": "liaison", "text": "from a session"}}))
            f.write(line({"seq": 9, "ts": 9, "type": "bus.send_requested", "data": {"text_from": "m8"}}))
        with open(os.path.join(state, "journal.jsonl"), "w", encoding="utf-8") as f:
            f.write(line({"seq": 10, "ts": 10, "type": "owner.notify", "data": {}}))
        st = {"bus": {"contacts": {"maps": {}}, "out": {
            "o7": {"contact": "maps", "kind": "message", "for": "owner", "status": "delivered", "ts": 7, "of": 7},
            "o9": {"contact": "maps", "kind": "message", "status": "delivered", "ts": 9, "of": 9, "basis": "m8",
                   "source": "maps-dev"}}}}
        web = type("W", (), {"state_dir": state})()
        page = BusWeb(web)
        seen = []
        real = journal.events_at
        with unittest.mock.patch.object(journal, "events_at", lambda d, s, **k: seen.append(set(s)) or real(d, s, **k)):
            rows = page.history(st, "maps")
            again = page.history(st, "maps")
        self.assertEqual([r["text"] for r in rows], ["older", "from a session"])
        self.assertEqual(again, rows)
        self.assertEqual(seen, [{7, 8, 9}])   # read once; the second request takes them from memory
        self.assertNotIn("of", rows[0])


class WhileTheJournalIsRead(unittest.TestCase):
    """The page server reads the journal anew (a restart, a loaded machine): the views of the core's state answer 503
    «ask in a few seconds»; the owner's contacts book is a file of its own and answers at once (the «Whose hub»
    list stayed empty while the journal was read)."""

    def test_the_book_answers_and_the_state_views_say_busy(self):
        from modules.web.busweb import BusWeb
        web = unittest.mock.Mock()
        web.t = lambda key, **_: key
        page = BusWeb(web)
        book = [{"name": "Pat Lee", "kinds": ["Teams"]}]
        with unittest.mock.patch.object(BusWeb, "state", return_value=None), \
                unittest.mock.patch.object(busflow, "people", return_value=book):
            self.assertEqual(page.act({"op": "people"}), (200, {"people": book}))
            self.assertEqual(page.act({"op": "view"}), (503, {"error": "web.srv.receipt_busy"}))

    def test_a_job_waits_for_the_reader_and_a_refusal_says_its_cause(self):
        """The owner pressed «Create an invitation» while the reader was busy: the job waits for it instead of ending
        with «failure (Error)»; a refusal that does end the job reads as its cause, not as the name of a class."""
        from modules.web.busweb import BusWeb
        web = unittest.mock.Mock()
        web.t = lambda key, **_: key
        page = BusWeb(web)
        looks = []
        page.idle = lambda: looks.append(1)
        ready = {"bus": {}}
        from modules.web import busweb
        with unittest.mock.patch.object(BusWeb, "state", side_effect=[None, None, ready]):
            self.assertIs(page.flow(wait=busweb.JOB_STATE_WAIT).current(), ready)
        self.assertEqual(len(looks), 2)
        with unittest.mock.patch.object(BusWeb, "flow", autospec=True, side_effect=BusWeb.flow) as made, \
                unittest.mock.patch.object(busflow.Flow, "link", return_value=busflow.Done("ok")):
            page.run({"n": 1, "what": "link", "steps": [], "done": None, "ts": 0}, "link",
                     {"ttl": 3600, "label": "", "person": None})
        self.assertEqual(made.call_args.kwargs.get("wait"), busweb.JOB_STATE_WAIT)

        job = {"n": 1, "what": "link", "steps": [], "done": None, "ts": 0}
        with unittest.mock.patch.object(busflow.Flow, "link", side_effect=bus.Error("no_relay", "")):
            page.run(job, "link", {"ttl": 3600, "label": "", "person": None})
        self.assertEqual(job["done"]["code"], "no_relay")
        self.assertEqual(job["done"]["text"], busflow.why("no_relay"))
        self.assertNotIn("Error", job["done"]["text"])

    def page(self):
        from modules.web.busweb import BusWeb
        web = unittest.mock.Mock()
        web.t = lambda key, **_: key
        return BusWeb(web)

    def job(self, page, fails):
        job = {"n": 1, "what": "link", "steps": [], "done": None, "ts": 0}
        log = []
        with unittest.mock.patch.object(busflow.Flow, "link", side_effect=fails), \
                unittest.mock.patch.object(sys, "stderr", unittest.mock.Mock(write=log.append)):
            page.run(job, "link", {"ttl": 3600, "label": "", "person": None})
        return job["done"], "".join(log)

    def test_a_reader_that_stays_busy_has_its_own_code_not_the_relays_busy(self):
        # the relay answers «busy» when the receiver's queue is full (relay/server.py) and busrelay.Client passes it on:
        # that must not read as «the page is still reading the journal»
        from modules.web import busweb
        page = self.page()
        page.idle = lambda: None
        with unittest.mock.patch.object(type(page), "state", return_value=None), \
                unittest.mock.patch.object(busweb, "JOB_STATE_WAIT", 0):
            with self.assertRaises(bus.Error) as e:
                page.flow().current()
        self.assertEqual(e.exception.code, "reader_busy")
        reader = busflow.why("reader_busy")
        self.assertNotEqual(busflow.why("busy"), reader)
        done, _ = self.job(page, busrelay.Error("busy", "receiver queue is full", 429))
        self.assertNotEqual(done["text"], reader)

    def test_a_request_of_the_page_does_not_wait_for_the_reader(self):
        # revoke, chat, verify… answer at once while the journal is read: the 30 s wait is the background job's only
        # (review: a request held its connection 30 s per state() and then ended in a 500)
        page = self.page()
        page.idle = unittest.mock.Mock(side_effect=AssertionError("a request waited for the reader"))
        with unittest.mock.patch.object(type(page), "state", return_value=None):
            with self.assertRaises(bus.Error) as e:
                page.flow().current()
        self.assertEqual(e.exception.code, "reader_busy")

    def test_a_failure_writes_no_words_of_the_exception_to_the_log_or_the_page(self):
        # the job holds a pasted link: a failure's text may carry a piece of it (as the Telegram path, only the type)
        piece = "hub-link-1:eyJ2IjoyfQ"
        done, log = self.job(self.page(), RuntimeError(f"cannot read {piece} at all"))
        self.assertEqual(done["code"], "error")
        self.assertIn("RuntimeError", log)
        self.assertIn("RuntimeError", done["text"])
        self.assertNotIn(piece, log)
        self.assertNotIn(piece, done["text"])
        self.assertNotIn("cannot read", log + done["text"])

    def test_a_refusal_keeps_its_cause_where_why_shows_one(self):
        done, _ = self.job(self.page(), busrelay.Error("network", "the relay did not answer in 15 s"))
        self.assertEqual(done["code"], "network")
        self.assertIn("the relay did not answer in 15 s", done["text"])


if __name__ == "__main__":
    unittest.main()
