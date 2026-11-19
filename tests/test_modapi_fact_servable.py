"""Module API 2.6: hubmod.fact with a repeat that makes no duplicate and with waiting for the core's decision (the key is
the event's dedup field, written by hubd, not data: old journals fold as before) and hubmod.servable over a batch of
paths with the index state."""
import tests  # noqa: F401 — don't see the machine's settings
import json
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from core import forms, hubmod, journal, modstate, reducer, shown, spool
from tests.test_modapi_owner_sessions import _env

NOTE = {"title": "bundle-run", "text": "done"}


class FactDedup(unittest.TestCase):
    def setUp(self):
        from tests.fakes.modstand import Stand
        self.s = Stand(self)
        _env(self, HUB_MODULE="fake", HUB_STATE_DIR=self.s.state_dir, HUB_MODULE_LAUNCH=None)

    def decided(self, *a, **k):
        """fact(..., wait=) in the module's thread while hubd parses the mailbox meanwhile."""
        got, stop = [], threading.Event()

        def call():
            try:
                got.append(hubmod.fact(*a, **k))
            finally:
                stop.set()
        t = threading.Thread(target=call)
        t.start()
        while not stop.wait(0.02):
            self.s.hub.ingest()
        t.join()
        self.assertTrue(got, "fact() crashed")
        return got[0]

    def test_the_key_is_a_field_of_the_event_not_of_its_data(self):
        hubmod.fact("owner.notify", NOTE, dedup_key="k1")
        self.s.hub.ingest()
        (ev,) = self.s.events("owner.notify")
        self.assertEqual(ev["dedup"], "k1")
        self.assertNotIn("dedup_key", ev["data"])
        self.assertNotIn("dedup", ev["data"])
        self.assertEqual(ev["data"]["sender"], "mod:fake")

    def test_a_repeat_is_refused_with_a_code_and_the_accepted_number(self):
        first, why = self.decided("owner.notify", NOTE, dedup_key="k1", wait=5)
        self.assertIsNone(why)
        again, why = self.decided("owner.notify", NOTE, dedup_key="k1", wait=5)
        self.assertNotEqual(again, first)
        self.assertEqual(forms.code_of(why), forms.DUPLICATE)
        self.assertIn(f"event {first}", why)
        (rej,) = [e for e in self.s.events("core.rejected") if e["data"]["of"] == again]
        self.assertEqual(rej["data"]["code"], forms.DUPLICATE)
        self.assertEqual(len(self.s.events("owner.notify")), 2)   # both in the journal, one accepted
        self.assertEqual(len(self.s.hub.state["notices"]), 1)

    def test_another_key_and_no_key_pass(self):
        self.assertIsNone(self.decided("owner.notify", NOTE, dedup_key="k1", wait=5)[1])
        self.assertIsNone(self.decided("owner.notify", NOTE, dedup_key="k2", wait=5)[1])
        self.assertIsNone(self.decided("owner.notify", NOTE, wait=5)[1])
        self.assertIsNone(self.decided("owner.notify", NOTE, wait=5)[1])

    def test_one_space_with_the_modules_own_state_keys(self):
        self.s.hub.record("module.ready", data={"module": "fake", "version": "1", "points": [], "channel": "core"})
        spool.put(os.path.join(self.s.state_dir, "spool"), "mod.fake.set", None,
                  {"key": "a", "value": 1, "dedup_key": "k1"}, src="module")
        self.s.hub.ingest()
        self.assertEqual(self.s.hub.state["mods"]["fake"]["a"]["value"], 1)
        seq, why = self.decided("owner.notify", NOTE, dedup_key="k1", wait=5)
        self.assertEqual(forms.code_of(why), forms.DUPLICATE)

    def test_another_modules_key_is_not_touched(self):
        hubmod.fact("owner.notify", NOTE, dedup_key="k1")
        self.s.hub.ingest()
        _env(self, HUB_MODULE="other")
        self.assertIsNone(self.decided("owner.notify", NOTE, dedup_key="k1", wait=5)[1])
        self.assertEqual(sorted(self.s.hub.state["mod_dedup"]), ["fake", "other"])

    def test_the_key_is_checked_before_the_spool(self):
        for bad in ("", "x" * 129, 5, ["k"], "a\ud800"):
            with self.assertRaises(ValueError):
                hubmod.fact("owner.notify", NOTE, dedup_key=bad)
        for bad in (0, -1, 31, float("nan"), True, "5"):
            with self.assertRaises(ValueError):
                hubmod.fact("owner.notify", NOTE, wait=bad)
        with self.assertRaises(ValueError):
            hubmod.fact("owner.notify", {**NOTE, "req": "own"}, wait=1)
        self.assertEqual(spool.pending(os.path.join(self.s.state_dir, "spool")), 0)

    def test_without_the_new_arguments_as_before(self):
        name = hubmod.fact("owner.notify", NOTE)
        self.assertIsInstance(name, str)
        with open(os.path.join(self.s.state_dir, "spool", name + ".json"), encoding="utf-8") as f:
            self.assertNotIn("dedup", json.load(f))

    def test_not_recorded_in_time(self):
        self.assertEqual(hubmod.fact("owner.notify", NOTE, wait=0.3), (None, None))

    def test_a_broken_key_in_a_spool_file_is_refused_not_a_crash(self):
        """The mailbox file is also written by hand: a lone surrogate in the key becomes "?", as in data; a non-string is rejected; hubd stays alive."""
        box = os.path.join(self.s.state_dir, "spool")
        os.makedirs(box, exist_ok=True)
        for n, bad in enumerate(("\ud800", 5)):
            with open(os.path.join(box, f"{n:020d}-1-x.json"), "w", encoding="ascii") as f:
                json.dump({"type": "owner.notify", "entity": None, "data": {**NOTE, "sender": "mod:fake"},
                           "src": "module", "ts": 1.0, "dedup": bad}, f)   # ensure_ascii: "\ud800" is valid JSON
        self.s.hub.ingest()
        evs = self.s.events("owner.notify")
        self.assertEqual(len(evs), 2)
        self.assertEqual(evs[0]["dedup"], "?")   # same as a surrogate in data: the journal and the fold see the same thing
        rejected = {e["data"]["of"] for e in self.s.events("core.rejected")}
        self.assertEqual(rejected, {evs[1]["seq"]})

    def test_only_a_module_fact_carries_the_key(self):
        """The mailbox file's dedup field exists only on a module fact (sender mod:<name>): not on CLI, session or plugin."""
        box = os.path.join(self.s.state_dir, "spool")
        for src, data in (("cli", {"channel": "cli", "owner": True}), ("ext:theme", {})):
            spool.put(box, "owner.notify", None, {**NOTE, **data}, src=src)
        for name in os.listdir(box):
            p = os.path.join(box, name)
            with open(p, encoding="utf-8") as f:
                item = json.load(f)
            with open(p, "w", encoding="utf-8") as f:
                json.dump({**item, "dedup": "k1"}, f)
        self.s.hub.ingest()
        evs = self.s.events("owner.notify")
        self.assertEqual(len(evs), 2)
        self.assertFalse([e for e in evs if "dedup" in e])


class OldJournals(unittest.TestCase):
    """The fold never looked at dedup_key in the data of a module fact that is neither its own mod.* nor the owner's word — and still doesn't."""

    def test_a_key_in_data_of_a_plain_module_fact_folds_as_before(self):
        st = reducer.empty()
        evs = [{"seq": n, "ts": 100.0 + n, "src": "module", "type": "owner.notify", "entity": None,
                "data": {**NOTE, "dedup_key": "k1", "channel": "plugin", "owner": False, "sender": "mod:fake"}}
               for n in (1, 2)]
        for ev in evs:
            st, _, why = reducer.reduce(st, ev)
            self.assertIsNone(why)
        self.assertNotIn("mod_dedup", st)

    def test_scope_reads_the_event_field(self):
        ev = {"seq": 1, "ts": 1.0, "type": "owner.notify", "dedup": "k1",
              "data": {"channel": "plugin", "owner": False, "sender": "mod:fake"}}
        self.assertEqual(modstate._scope(ev)[1:], ("mod_dedup", "fake", "k1"))
        self.assertIsNone(modstate._scope({**ev, "data": {**ev["data"], "sender": "maps"}}))


class ServableBatch(unittest.TestCase):
    def setUp(self):
        self.state = os.path.realpath(tempfile.mkdtemp(prefix="hubservable-"))
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)
        self.j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        self.j.append("session.requested", "a", {"session_id": "s", "cwd": "/w"})
        _env(self, HUB_MODULE="fake", HUB_STATE_DIR=self.state)
        self.addCleanup(setattr, hubmod, "_shown", None)
        hubmod._shown = None

    def file(self, name):
        p = os.path.join(self.state, "files", name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(b"%PDF")
        return p

    def sent(self, path):
        self.j.append("owner.file", None, {"path": path, "caption": "", "channel": "session", "owner": False,
                                           "sender": "a"})

    def test_one_refresh_for_the_whole_list(self):
        paths = [self.file(f"r{n}.pdf") for n in range(5)]
        for p in paths[:3]:
            self.sent(p)
        calls = []
        real = shown.Index.refresh
        with mock.patch.object(shown.Index, "refresh", lambda idx: (calls.append(1), real(idx))[1]):
            got = hubmod.servable(paths + ["/etc/hosts"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(got, {**{p: p for p in paths[:3]}, paths[3]: None, paths[4]: None, "/etc/hosts": None})

    def test_a_single_path_as_before(self):
        p = self.file("one.pdf")
        self.sent(p)
        self.assertEqual(hubmod.servable(p), p)
        self.assertIsNone(hubmod.servable(None))

    def test_a_list_of_not_strings_is_refused(self):
        with self.assertRaises(TypeError):
            hubmod.servable(["/a", 5])

    def test_any_iterable_is_a_batch_and_the_keyword_stays(self):
        p = self.file("one.pdf")
        self.sent(p)
        self.assertEqual(hubmod.servable(x for x in [p]), {p: p})
        self.assertEqual(hubmod.servable({p}), {p: p})
        self.assertEqual(hubmod.servable(path=p), p)

    def test_a_path_after_the_first_mebibyte_of_the_journal_is_found(self):
        for n in range(40):
            self.j.append("session.said", "a", {"text": f"line {n} " + "x" * 40000})
        self.assertGreater(os.path.getsize(self.j.path), 1 << 20)
        p = self.file("late.pdf")
        self.sent(p)
        self.assertEqual(hubmod.servable([p]), {p: p})

    def rotate(self):
        journal.rotate(self.state, reducer.replay(journal.read(self.j.path)), keep=1, per_session=1)
        self.j = journal.Journal(os.path.join(self.state, "journal.jsonl"))

    def test_ready_without_archives(self):
        self.assertEqual(hubmod.servable_state(), "ready")

    def test_building_while_archives_are_parsed_then_ready(self):
        self.sent(self.file("old.pdf"))
        self.rotate()
        go = threading.Event()
        real = shown.side_of

        def slow(path):
            go.wait(5)
            return real(path)
        with mock.patch.object(shown, "side_of", slow):
            self.assertEqual(hubmod.servable_state(), "building")
            go.set()
            hubmod._shown.building.join(5)
        self.assertEqual(hubmod.servable_state(), "ready")

    def test_partial_when_an_archive_does_not_parse(self):
        self.sent(self.file("old.pdf"))
        self.rotate()

        def broken(path):
            raise UnicodeDecodeError("utf-8", b"", 0, 1, "x")
        with mock.patch.object(shown, "of_file", broken):
            hubmod.servable_state()
            hubmod._shown.building.join(5)
            self.assertEqual(hubmod.servable_state(), "partial")

    def test_a_retried_archive_that_parses_is_no_longer_partial(self):
        pdf = self.file("old.pdf")
        self.sent(pdf)
        self.rotate()
        idx = shown.Index(self.state, background=False)
        with mock.patch.object(shown, "of_file", mock.Mock(side_effect=OSError("busy"))):
            self.assertEqual(idx.state(), "partial")
        with mock.patch.object(shown, "RETRY", 0):   # same archive folder: a retry is by timeout, not by the folder changing
            self.assertEqual(idx.state(), "ready")
        self.assertEqual(idx.servable(pdf), pdf)

    def test_an_archive_parsed_elsewhere_or_gone_is_no_longer_partial(self):
        for name in ("a.pdf", "b.pdf"):
            self.sent(self.file(name))
            self.rotate()
        first, second = journal.archives(self.state)
        idx = shown.Index(self.state, background=False)
        with mock.patch.object(shown, "of_file", mock.Mock(side_effect=OSError("busy"))):
            self.assertEqual(idx.state(), "partial")
        self.assertEqual(len(idx.failed), 2)
        shown.write_side(first, shown.of_file(first))   # another process (the page) parsed it
        os.unlink(second)                              # the archive was removed
        with mock.patch.object(shown, "RETRY", 0):
            self.assertEqual(idx.state(), "ready")
        self.assertEqual(idx.failed, {})


if __name__ == "__main__":
    unittest.main()
