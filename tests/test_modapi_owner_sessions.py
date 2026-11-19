"""Module API 2.4: the owner's word with waiting for the core's decision (hubmod.owner(..., wait=)) and lightweight
reading of sessions by a module (the sessions right: the modrun/<name>.state.json slice carries session state,
hubmod.session/sessions) — without its own journal reader and without the whole state.json."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import threading
import unittest
from unittest import mock

from core import hubmod, journal, modrun, modstate, modules

OWNER = {"channel": "web", "owner": True}


def _env(case, **env):
    old = {k: os.environ.get(k) for k in env}
    case.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                             for k, v in old.items()])
    for k, v in env.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def _req(owner=True, view=False):
    return hubmod.Request("POST", "/x", {}, {}, {}, owner, view)


class OwnerWait(unittest.TestCase):
    """owner(wait=seconds) — like the page's put_decided: (seq, None) accepted, (seq, reason in the core's words) refused,
    (None, None) the core did not record within wait."""

    def setUp(self):
        from tests.fakes.modstand import Stand
        self.s = Stand(self)
        _env(self, HUB_MODULE="fake", HUB_STATE_DIR=self.s.state_dir, HUB_MODULE_LAUNCH=None)
        self.s.hub.record("session.requested", "maps", {"cwd": "/w", "session_id": "s-maps", **OWNER})

    def decided(self, *a, **k):
        """owner(...) in a handler thread while hubd meanwhile processes the inbox."""
        got, stop = [], threading.Event()

        def call():
            try:
                got.append(hubmod.owner(*a, **k))
            finally:
                stop.set()
        t = threading.Thread(target=call)
        t.start()
        while not stop.wait(0.02):
            self.s.hub.ingest()
        t.join()
        self.assertTrue(got, "owner() crashed")
        return got[0]

    def test_accepted_gives_the_seq(self):
        seq, why = self.decided(_req(), "session.waiting", "maps", {"text": "waiting"}, wait=5)
        self.assertIsNone(why)
        (ev,) = [e for e in self.s.events("session.waiting")]
        self.assertEqual(seq, ev["seq"])
        self.assertEqual(ev["data"]["channel"], "mod:fake")
        self.assertTrue(ev["data"]["owner"])

    def test_refusal_comes_with_the_core_reason(self):
        seq, why = self.decided(_req(), "session.wake_requested", "nobody", {}, wait=5)
        (rej,) = [e for e in self.s.events("core.rejected") if e["data"]["of"] == seq]
        self.assertEqual(why, rej["data"]["reason"])
        self.assertTrue(why)
        self.assertIsNone(getattr(why, "code", None))   # a refusal without a code has no code, not «repeat»

    def test_a_repeat_with_the_same_key_is_refused_with_the_accepted_number(self):
        first, why = self.decided(_req(), "session.waiting", "maps", {"text": "waiting"}, dedup_key="k1", wait=5)
        self.assertIsNone(why)
        again, why = self.decided(_req(), "session.waiting", "maps", {"text": "waiting"}, dedup_key="k1", wait=5)
        self.assertNotEqual(again, first)
        self.assertIn(str(first), why)
        # a repeat is recognised by the code, not the text (2.6): the code is also in the journal next to the reason, the text is unchanged
        self.assertEqual(getattr(why, "code", None), "duplicate")
        self.assertTrue(why.startswith("repeat of dedup_key k1: already accepted"), why)
        (rej,) = [e for e in self.s.events("core.rejected") if e["data"]["of"] == again]
        self.assertEqual((rej["data"]["code"], rej["data"]["reason"]), ("duplicate", why))

    def test_not_recorded_in_time(self):
        self.assertEqual(hubmod.owner(_req(), "session.waiting", "maps", {"text": "waiting"}, wait=0.3), (None, None))
        self.assertEqual(hubmod.NOT_DECIDED, journal.NOT_DECIDED)
        self.assertIn("hub status", hubmod.NOT_DECIDED)

    def test_without_wait_as_before(self):
        name = hubmod.owner(_req(), "session.waiting", "maps", {"text": "waiting", "req": "own"})
        self.assertIsInstance(name, str)
        (item,) = self.s.spooled()
        self.assertEqual(item["data"]["req"], "own")   # without wait the core sets no marker

    def test_wait_is_checked_before_the_spool(self):
        for bad in (0, -1, True, "3", 61, float("nan")):
            with self.assertRaises(ValueError, msg=bad):
                hubmod.owner(_req(), "session.waiting", "maps", {}, wait=bad)
        with self.assertRaises(hubmod.HTTPError):
            hubmod.owner(_req(view=True), "session.waiting", "maps", {}, wait=1)
        self.assertEqual(self.s.spooled(), [])

    def test_req_is_the_core_mark(self):
        with self.assertRaises(ValueError):
            hubmod.owner(_req(), "session.waiting", "maps", {"req": "own"}, wait=1)
        self.assertEqual(self.s.spooled(), [])

    def test_the_fact_is_found_behind_a_burst_of_events(self):
        """hubd catches up with the queue in a batch: more than 200 events land after a module's fact between two polls."""
        orig, done = journal.decided, []

        def burst(path, req, wait, max_bytes=4 << 20, start=None):
            self.s.hub.ingest()
            for i in range(300):
                self.s.hub.record("session.waiting", "maps", {"text": f"waiting {i}", **OWNER})
            done.append(1)
            return orig(path, req, wait, max_bytes, start)
        with mock.patch.object(journal, "decided", burst):
            seq, why = hubmod.owner(_req(), "session.wake_requested", "nobody", {}, wait=2)
        self.assertEqual(done, [1])
        self.assertIsNotNone(seq)
        self.assertTrue(why)

    def test_the_mark_survives_a_trimmed_fragment_and_a_shorter_file(self):
        """A fragment at the end of the journal is cut by hubd back to its start and the next record is written there; the
        repair at startup shortens the file — the fact is still found."""
        path = os.path.join(self.s.state_dir, "journal.jsonl")
        with open(path, "ab") as f:
            f.write('{"seq": 99, "type": "trun'.encode())
        at = journal.mark(path)
        with open(path, "rb+") as f:
            f.truncate(at[1])
            f.seek(at[1])
            f.write(json.dumps({"seq": 7, "type": "x", "data": {"req": "r1"}}).encode() + b"\n")
        self.assertEqual(journal.decided(path, "r1", 0.5, start=at), (7, None))
        big = (at[0], at[1] + 10 ** 6)   # the mark is past the end — the file was shortened
        self.assertEqual(journal.decided(path, "r1", 0.5, start=big), (7, None))

    def test_the_page_uses_the_same_text(self):
        from modules.web import server
        with open(server.__file__, encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("the core has not recorded it yet", src)


class Sessions(unittest.TestCase):
    """A module with the sessions right gets the hub's sessions in its slice: state, parking, asleep, a turn in progress,
    model — and the old names of renamed ones; texts, paths, conversation ids — no. Without the right — nothing about sessions."""

    def setUp(self):
        from tests.fakes.modstand import Stand
        self.s = Stand(self, extra=[{"name": "plain"}])
        self.manifest("fake", ["sessions"])
        self.s.hub.modules.reload(self.s.cfg, {})
        _env(self, HUB_MODULE="fake", HUB_STATE_DIR=self.s.state_dir, HUB_MODULE_LAUNCH=None)
        self.s.hub.modules.tick()
        h = self.s.hub
        for n in ("maps", "docs", "old"):
            h.record("session.requested", n, {"cwd": "/home/x/" + n, "session_id": "s-" + n, **OWNER})

    def manifest(self, name, caps, api="^2.4"):
        path = os.path.join(self.s.root, name, "module.json")
        with open(path, encoding="utf-8") as f:
            man = json.load(f)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({**man, "api": api, "capabilities": caps}, f)

    def file(self, name="fake"):
        with open(os.path.join(modrun.run_dir(self.s.state_dir), f"{name}.state.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_the_right_is_of_api_2_4(self):
        self.assertEqual(modules.read(os.path.join(self.s.root, "fake"), "path")["errors"], [])
        self.manifest("plain", ["sessions"], api="^2.3")
        self.assertTrue(modules.read(os.path.join(self.s.root, "plain"), "path")["errors"])

    def test_the_slice_carries_sessions_only_with_the_right(self):
        got = self.file()["sessions"]
        self.assertEqual(set(got), {"maps", "docs", "old"})
        self.assertEqual({k: got["maps"][k] for k in ("state", "parked", "asleep", "busy", "model")},
                         {"state": "starting", "parked": False, "asleep": False, "busy": True, "model": None})
        self.assertNotIn("sessions", self.file("plain"))
        text = json.dumps(self.file(), ensure_ascii=False)
        for secret in ("/home/x", "s-maps"):
            self.assertNotIn(secret, text)

    def test_the_slice_follows_session_changes(self):
        h = self.s.hub
        h.state["sessions"]["maps"].update(state="parked")
        h.state["sessions"]["docs"].update(state="asleep")
        h.record("session.requested", "shut", {"cwd": "/w", "session_id": "s-shut", **OWNER})
        h.state["sessions"]["shut"].update(state="closed")   # wake does not wake a closed one (the SESSION state machine)
        h.state["sessions"]["old"].update(state="idle", info={"model": "claude-opus-5-5", "cwd": "/home/x"})
        h.record("session.renamed", "old", {"to": "new", **OWNER})
        h.record("session.waiting", "new", {"text": "waiting", **OWNER})   # any event: the slice is built from the fold result
        got = self.file()
        self.assertEqual({k: got["sessions"]["maps"][k] for k in ("state", "parked", "asleep", "busy", "model")},
                         {"state": "asleep", "parked": True, "asleep": True, "busy": False, "model": None})
        self.assertTrue(got["sessions"]["docs"]["asleep"])
        self.assertFalse(got["sessions"]["shut"]["asleep"])
        self.assertEqual(got["sessions"]["new"]["model"], "claude-opus-5-5")
        self.assertEqual(got["renamed"], {"old": "new"})
        self.assertEqual(hubmod.session("old")["name"], "new")
        self.assertEqual(hubmod.session("new")["name"], "new")
        self.assertIsNone(hubmod.session("nobody"))
        self.assertEqual(set(hubmod.sessions()), {"maps", "docs", "new", "shut"})

    def test_removed_sessions_are_not_shown(self):
        self.s.hub.state["sessions"]["docs"]["state"] = "removed"
        self.s.hub.record("session.waiting", "maps", {"text": "waiting", **OWNER})
        self.assertNotIn("docs", self.file()["sessions"])
        self.assertIsNone(hubmod.session("docs"))

    def test_reads_the_slice_not_the_hub_state(self):
        with mock.patch.object(journal, "load_state", side_effect=AssertionError("the whole hub state")):
            self.assertEqual(hubmod.session("maps")["state"], "starting")

    def test_without_the_right_the_module_is_told(self):
        _env(self, HUB_MODULE="plain")
        with self.assertRaises(PermissionError):
            hubmod.sessions()
        os.unlink(os.path.join(modrun.run_dir(self.s.state_dir), "plain.state.json"))
        with mock.patch.object(hubmod, "manifest", return_value={"capabilities": []}), \
                mock.patch.object(journal, "load_state", side_effect=AssertionError("the whole hub state")):
            with self.assertRaises(PermissionError):   # without the right and without a slice — a refusal, reading nothing
                hubmod.sessions()

    def test_without_the_slice_the_hub_state(self):
        self.s.hub.save_state()
        os.unlink(os.path.join(modrun.run_dir(self.s.state_dir), "fake.state.json"))
        _env(self, PWD=None)
        with mock.patch.object(hubmod, "manifest", return_value={"capabilities": ["sessions"]}):
            self.assertEqual(hubmod.session("maps")["state"], "starting")
        with mock.patch.object(hubmod, "manifest", return_value={"capabilities": []}):
            with self.assertRaises(PermissionError):
                hubmod.session("maps")

    def test_an_event_rebuilds_only_the_sessions(self):
        """hubd calls this on every event: module records and the weight of others' are not rebuilt, the file only on a change."""
        path = os.path.join(modrun.run_dir(self.s.state_dir), "fake.state.json")
        before = os.stat(path).st_mtime_ns
        with mock.patch.object(modstate, "slices", wraps=modstate.slices) as calls:
            self.s.hub.record("session.waiting", "maps", {"text": "waiting", **OWNER})
        self.assertEqual(calls.call_count, 0)
        self.assertEqual(os.stat(path).st_mtime_ns, before)   # same sessions — the file is not rewritten
        self.s.hub.state["sessions"]["maps"]["state"] = "idle"
        self.s.hub.record("session.waiting", "maps", {"text": "waiting", **OWNER})
        self.assertEqual(self.file()["sessions"]["maps"]["state"], "idle")

    def test_slices_without_watchers_are_not_rebuilt_on_every_event(self):
        self.manifest("fake", [])
        self.s.hub.modules.reload(self.s.cfg, {})
        self.s.hub.modules.tick()
        with mock.patch.object(modstate, "slices", wraps=modstate.slices) as calls:
            self.s.hub.record("session.waiting", "maps", {"text": "waiting", **OWNER})
        self.assertEqual(calls.call_count, 0)


if __name__ == "__main__":
    unittest.main()
