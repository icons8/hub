"""A session feed scrolls up into the past (journal entries are loaded from the archive as the
session is scrolled up): what is older than the live tail comes from the core's feed, then from archived journals; each
archive is parsed once, the parse lies next to it."""
import tests  # noqa: F401 — do not see the machine's settings
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import quote

from core import history, journal, views
from core.daemon import Hub
from tests.fakes import lang
from tests.test_snapshot import NullHost

OWNER = {"channel": "web", "owner": True}


class History(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="history-")
        self.hub = Hub(self.dir, NullHost(), registry_dir=self.dir)
        self.hub.record("session.requested", "a", {"session_id": "s-a", "cwd": "/w", **OWNER})
        self.hub.record("session.started", "a", {})

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def say(self, n, start=0):
        for i in range(start, start + n):
            self.hub.record("message.received", None, {"to": "a", "text": f"errand {i}", **OWNER})

    def live(self):
        r = views.Reader()
        r.feed_all(journal.read(os.path.join(self.dir, "journal.jsonl")))
        return r

    def texts(self, items):
        return [it.get("text") for it in items if it.get("kind") == "in"]

    def test_pages_go_back_through_the_archive(self):
        self.say(50)
        journal.rotate(self.dir, self.hub.state, keep=5, per_session=5)
        self.hub = Hub(self.dir, NullHost(), registry_dir=self.dir)
        self.say(3, 50)
        live = self.live()
        shown = live.timelines["a"]
        self.assertEqual(self.texts(shown)[-1], "errand 52")
        h = history.History(self.dir)
        h.build_all()                                         # parsing the archives (on the server — in the background)
        page = h.older("a", shown[0]["seq"], 20, live.timelines["a"])
        self.assertEqual(len(page["items"]), 20)
        self.assertTrue(page["more"])
        seqs = [it["seq"] for it in page["items"]]
        self.assertEqual(seqs, sorted(seqs))
        older = h.older("a", page["items"][0]["seq"], 1000, live.timelines["a"])
        self.assertEqual(self.texts(older["items"])[0], "errand 0")
        self.assertFalse(older["more"])
        everything = self.texts(older["items"] + page["items"] + shown)
        self.assertEqual(everything, [f"errand {i}" for i in range(53)])   # no gaps and no repeats
        self.assertTrue(any(n.endswith(history.CACHE) for n in os.listdir(os.path.join(self.dir, journal.ARCHIVE))))

    def test_an_old_line_of_a_quiet_executor_does_not_hide_the_hours_before_it(self):
        # rotation carries over the last events of each session; the old result of a long-asleep executor landed in the
        # delegator's feed as the first line, and the past was loaded only from before it — everything between it and
        # the live tail was lost
        by_a = {"channel": "session", "owner": False, "sender": "a"}
        self.hub.record("session.requested", "x", {"session_id": "s-x", "cwd": "/w", **OWNER})
        self.hub.record("session.started", "x", {})
        self.hub.record("delegation.created", None, {"session": "x", "for_session": "a", "context": "count", **by_a})
        self.assertTrue(self.live().timelines["a"][-1]["seq"] > 3)      # the line about the delegation to x is in a's feed
        for start in (0, 25):                                           # two rotations: the archive also starts with carried-over lines
            self.say(25, start)
            journal.rotate(self.dir, self.hub.state, keep=5, per_session=5)
            self.hub = Hub(self.dir, NullHost(), registry_dir=self.dir)
        self.say(3, 50)
        live = self.live()
        shown = live.timelines["a"]
        self.assertEqual(self.texts(shown[:1]), ["errand 45"])          # a feed without gaps: the old line x is in the archive
        h = history.History(self.dir)
        h.build_all()
        older = h.older("a", shown[0]["seq"], 1000, shown)
        self.assertEqual(self.texts(older["items"] + shown), [f"errand {i}" for i in range(53)])
        self.assertFalse(older["more"])
        # an archive parse made before the fix still holds the old line x — it is in its place, the past is not lost
        newest = h.archives()[0]
        path = os.path.join(h.dir, newest)
        with open(path + history.CACHE, encoding="utf-8") as f:
            parsed = json.load(f)
        parsed["a"].insert(0, dict(older["items"][0], text="an old line"))
        history._write(path, parsed)
        h.index.clear()
        again = h.older("a", shown[0]["seq"], 1000, shown)
        self.assertEqual(self.texts(again["items"]), [f"errand {i}" for i in range(45)])
        page = h.older("a", shown[0]["seq"], 10, shown)
        self.assertEqual((self.texts(page["items"]), page["more"]), ([f"errand {i}" for i in range(35, 45)], True))

    def test_a_carried_message_parsed_twice_with_another_text_is_one_line(self):
        # rotation carry-over: the same core message in two archives, the text is from the state at parse time
        a = {"seq": 7, "kind": "in", "id": "m7", "text": "sessions will switch at the next message", "k": 1}
        b = {**a, "text": "on the gateway — already on it", "k": 9}
        self.assertEqual(history._key(a), history._key(b))
        self.assertNotEqual(history._key({"seq": 7, "kind": "sys", "text": "x"}), history._key({"seq": 7, "kind": "sys",
                                                                                                "text": "y"}))

    def test_a_renamed_session_scrolls_back_into_its_old_name(self):
        # archives from before the rename hold the feed under the old name, scrolling did not find them
        self.say(20)
        journal.rotate(self.dir, self.hub.state, keep=5, per_session=5)
        self.hub = Hub(self.dir, NullHost(), registry_dir=self.dir)
        self.hub.record("session.ended", "a", {})                             # a sleeping session is renamed
        self.hub.record("session.renamed", "a", {"to": "b", **OWNER})
        self.hub.record("message.received", None, {"to": "b", "text": "errand 20", **OWNER})
        live = self.live()
        shown = live.timelines["b"]
        h = history.History(self.dir)
        h.build_all()
        older = h.older("b", shown[0]["seq"], 1000, shown, names=history.names(self.hub.state, "b"))
        self.assertEqual(self.texts(older["items"] + shown), [f"errand {i}" for i in range(21)])

    def test_live_items_older_than_the_page_come_first(self):
        self.say(30)
        live = self.live()
        all_items = live.timelines["a"]
        h = history.History(self.dir)
        page = h.older("a", all_items[-5]["seq"], 10, all_items)
        self.assertEqual([it["seq"] for it in page["items"]], [it["seq"] for it in all_items[-15:-5]])
        self.assertTrue(page["more"])

    def rotated(self, rounds=4, each=10):
        """Session a writes, b alongside it; after each batch — a rotation with short tails."""
        self.hub.record("session.requested", "b", {"session_id": "s-b", "cwd": "/w", **OWNER})
        self.hub.record("session.started", "b", {})
        for k in range(rounds):
            self.say(each, k * each)
            self.hub.record("message.received", None, {"to": "b", "text": f"beta {k} " + "z" * 2000, **OWNER})
            journal.rotate(self.dir, self.hub.state, keep=2, per_session=2)
            self.hub = Hub(self.dir, NullHost(), registry_dir=self.dir)
        return self.live()

    def spy(self):
        """What History parses from JSON: the page must read from disk only the needed session's feed."""
        seen, real = [], history.json

        class Spy:
            def __getattr__(self, name):
                return getattr(real, name)

            def loads(self, s, *a, **k):
                seen.append(s.decode("utf-8") if isinstance(s, bytes) else s)
                return real.loads(s, *a, **k)

            def load(self, f, *a, **k):
                return self.loads(f.read(), *a, **k)
        patcher = mock.patch.object(history, "json", Spy())
        patcher.start()
        self.addCleanup(patcher.stop)
        return seen

    def test_reads_only_the_session_it_pages(self):
        # 2026-10-15: the page held the parse of three archives whole (up to 24 MB of JSON each, hundreds of MB in memory)
        # to serve one session's feed; now — only its slice of the parse, by an offset pointer
        live = self.rotated()
        h = history.History(self.dir)
        h.build_all()
        seen = self.spy()
        got, before = [], live.timelines["a"][0]["seq"]
        while True:
            page = h.older("a", before, 3, live.timelines["a"])
            got = page["items"] + got
            if not page["more"]:
                break
            before = page["items"][0]["seq"]
        self.assertEqual(self.texts(got + live.timelines["a"]), [f"errand {i}" for i in range(40)])
        self.assertTrue(seen)
        self.assertFalse([s for s in seen if "beta " in s])          # b's feed was not parsed
        seen.clear()
        h2 = history.History(self.dir)              # after a restart: which sessions are in which archive — from disk
        h2.build_all()
        self.assertFalse([s for s in seen if "errand" in s or "beta " in s])   # ready archives are not read at start
        self.assertEqual(h2.older("nobody", 10 ** 9, 10, [])["items"], [])
        self.assertFalse([s for s in seen if "errand" in s or "beta " in s])   # nor for a session that is not in them

    def test_a_parse_of_an_earlier_version_is_made_again(self):
        # a parse from an earlier version (no pointer, or a pointer of an earlier parse version) lacks what this one
        # builds: the archive is parsed again; the format stays the same, the previous version after a rollback reads it
        live = self.rotated(rounds=2)
        h = history.History(self.dir)
        h.build_all()
        arch = os.path.join(self.dir, journal.ARCHIVE)
        names = h.archives()
        for i, n in enumerate(names):
            with open(os.path.join(arch, n + history.CACHE), "w", encoding="utf-8") as f:
                json.dump({"a": []}, f)   # stale
            if i:
                os.remove(os.path.join(arch, n + history.INDEX))
            else:
                with open(os.path.join(arch, n + history.INDEX), "w", encoding="utf-8") as f:
                    json.dump({"size": os.path.getsize(os.path.join(arch, n + history.CACHE)), "sessions": {},
                               "lang": "en"}, f)
        h = history.History(self.dir)
        self.assertFalse(h.ready())
        h.build_all()
        self.assertTrue(h.ready())
        page = h.older("a", live.timelines["a"][0]["seq"], 1000, live.timelines["a"])
        self.assertEqual(self.texts(page["items"] + live.timelines["a"]), [f"errand {i}" for i in range(20)])
        with open(os.path.join(arch, names[0] + history.CACHE), encoding="utf-8") as f:
            self.assertIn("a", json.load(f))

    def test_a_parse_of_an_earlier_version_is_served_until_it_is_made_again(self):
        # 2026-11-02: the parse version went up, and after the update every hub parsed all its archives again (hundreds,
        # hours) with the feed past closed meanwhile; an earlier parse of the same language differs only in a few rows
        # (a call closed in the next archive) — it is served until the background parse replaces it
        live = self.rotated(rounds=2)
        h = history.History(self.dir)
        h.build_all()
        arch = os.path.join(self.dir, journal.ARCHIVE)
        for n in h.archives():   # as the previous version wrote it: no parse field, its own rows
            path = os.path.join(arch, n)
            with open(path + history.CACHE, encoding="utf-8") as f:
                old = {k: [dict(it, text=it["text"] + " (earlier)") if it.get("kind") == "in" else it for it in v]
                       for k, v in json.load(f).items()}
            history._write(path, old, "en")   # the tests' language
            with open(path + history.INDEX, encoding="utf-8") as f:
                index = json.load(f)
            del index["parse"]
            with open(path + history.INDEX, "w", encoding="utf-8") as f:
                json.dump(index, f)
        h = history.History(self.dir)
        self.assertEqual(self.texts(live.timelines["a"]), ["errand 19"])
        before, archived = live.timelines["a"][0]["seq"], [f"errand {i} (earlier)" for i in range(19)]
        self.assertFalse(h.ready())
        with mock.patch.object(h, "build_in_background") as build:
            page = h.older("a", before, 1000, live.timelines["a"], build=False)
            self.assertFalse(page["building"])
            self.assertEqual(self.texts(page["items"]), archived)
            build.assert_called_once()   # and the parse of this version is made in the background
            page = h.newer("a", 0, 1000, live.timelines["a"])
            self.assertFalse(page["building"])
            self.assertEqual(self.texts(page["items"]), archived + ["errand 19 (earlier)"])   # carried: archive first
        got = history.search(self.dir, ["a"], "errand 3", 10 ** 12)
        self.assertTrue(got["done"])
        self.assertEqual([x["snippet"] for x in got["hits"]], ["errand 3 (earlier)"])
        for n in (h.archives()[0], h.archives()[-1]):   # parsed again behind the pointers the page read
            history.build(os.path.join(arch, n))
        with mock.patch.object(h, "build_in_background"):
            page = h.older("a", before, 1000, live.timelines["a"], build=False)
            self.assertEqual((page["items"], page["building"]), ([], True))   # not a silent gap: the page asks again
            page = h.newer("a", 0, 1000, live.timelines["a"])
            self.assertEqual((page["items"], page["building"]), ([], True))
        h._build_outside()
        page = h.older("a", before, 1000, live.timelines["a"], build=False)   # the pointer read before is not used
        self.assertEqual(self.texts(page["items"]), [f"errand {i}" for i in range(19)])
        self.assertTrue(h.ready())

    def test_a_broken_parse_is_a_gap_not_endless_building(self):
        # the parse file is damaged under its own pointer (same size): nothing from it, as before — "building" here
        # would have the page ask again every 3 s forever
        live = self.rotated(rounds=2)
        h = history.History(self.dir)
        h.build_all()
        name = h.archives()[0]
        at = h.sessions(name)["a"]
        with open(os.path.join(self.dir, journal.ARCHIVE, name + history.CACHE), "r+b") as f:
            f.seek(at[1])
            f.write(b"{")
        page = h.older("a", live.timelines["a"][0]["seq"], 1000, live.timelines["a"], build=False)
        self.assertFalse(page["building"])
        self.assertTrue(self.texts(page["items"]))   # the older archive is read

    def test_a_parse_written_without_a_language_is_in_the_process_language(self):
        # the default was Russian: a parse written by a caller that names no language then passed for Russian
        from core import i18n
        self.addCleanup(i18n.use, i18n.current())
        for lang in ("en", "ru"):
            i18n.use(lang)
            path = os.path.join(self.dir, "x-" + lang)
            history._write(path, {"a": [{"seq": 1, "kind": "in", "text": "t"}]})
            with open(path + history.INDEX, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["lang"], lang)

    def test_another_language_parses_the_archive_again(self):
        # service lines of the feed are in the hub's language (core/i18n.py): a parse in the previous language after a
        # change is redone; a parse from the previous version without a lang field is Russian
        from core import i18n
        self.addCleanup(i18n.use, i18n.current())
        live = self.rotated(rounds=2)
        h = history.History(self.dir)
        h.build_all()
        arch = os.path.join(self.dir, journal.ARCHIVE)
        name = h.archives()[-1]
        sys_text = lambda: [it.get("text") for it in h.items(name, "a") if it.get("kind") == "sys"][0]   # noqa: E731
        self.assertEqual(sys_text(), i18n.tr("en", "core.views.sys.created"))
        i18n.use("ru")
        self.assertFalse(h.ready())                       # pointers in English are no good
        h.build_all()
        self.assertEqual(sys_text(), i18n.tr("ru", "core.views.sys.created"))
        with open(os.path.join(arch, name + history.INDEX), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["lang"], "ru")
        i18n.use("en")
        self.assertFalse(h.ready())
        h.build_all()
        self.assertEqual(sys_text(), i18n.tr("en", "core.views.sys.created"))
        self.assertTrue(live.timelines["a"])

    def test_archive_is_parsed_outside_the_page_process(self):
        # parsing a new archive peaks at hundreds of MB (a 60 MB archive whole in memory); in the page process it stayed
        # in its memory after the parse — a separate process parses it
        live = self.rotated(rounds=2)
        h = history.History(self.dir)
        with mock.patch.object(history.views, "Reader", side_effect=AssertionError("parsed in the page process")):
            self.assertTrue(h.older("a", 10 ** 9, 5, [], build=False)["building"])
            h.building.join(60)
            self.assertTrue(h.ready())
            page = h.older("a", live.timelines["a"][0]["seq"], 1000, live.timelines["a"], build=False)
        self.assertEqual(self.texts(page["items"] + live.timelines["a"]), [f"errand {i}" for i in range(20)])

    def test_not_ready_yet(self):
        self.say(5)
        journal.rotate(self.dir, self.hub.state, keep=2, per_session=2)
        h = history.History(self.dir)
        page = h.older("a", 10 ** 9, 10, [], build=False)
        self.assertTrue(page["building"])


    def outside(self, run=None):
        """Background parsing to the end; parsing in the page process is a test error."""
        h = history.History(self.dir)
        with mock.patch.object(h, "build_all", side_effect=AssertionError("parsed in the page process")), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            if run:
                with mock.patch.object(history.subprocess, "run", run):
                    h._build_outside()
            else:
                h._build_outside()
        return h, err.getvalue()

    def test_packaged_hub_without_sources_parses_outside(self):
        # an older .deb (compiled with Nuitka until 2026-11-01) has no core/*.py — only the built module, __file__ points to a
        # non-existent core/history.py: a launch by path always failed, and parsing ran in the page process
        self.rotated(rounds=2)
        with mock.patch.object(history, "__file__", os.path.join(os.path.dirname(history.__file__), "history-gone.py")):
            h, err = self.outside()
        self.assertTrue(h.ready(), err)

    def test_child_failure_is_logged_not_parsed_in_the_page(self):
        # it crashed (out of memory, an exception) — parsing in the page would give the same peak; the page gets what is
        # already parsed, without a new parse every 3 s while the page keeps asking
        live = self.rotated(rounds=2)
        failed = lambda *a, **k: subprocess.CompletedProcess(a, -9, b"", b"MemoryError")  # noqa: E731
        h, err = self.outside(failed)
        self.assertIn("MemoryError", err)
        self.assertFalse(h.ready())
        with mock.patch.object(h, "build_in_background", side_effect=AssertionError("repeated at once")):
            page = h.older("a", live.timelines["a"][0]["seq"], 10, live.timelines["a"], build=False)
        self.assertEqual((page["items"], page["building"]), ([], False))
        with mock.patch.object(history.time, "time", return_value=time.time() + history.RETRY + 1), \
                mock.patch.object(h, "build_in_background") as again:
            self.assertTrue(h.older("a", 10 ** 9, 10, [], build=False)["building"])
        again.assert_called_once()

    def test_child_that_cannot_start_parses_in_the_page(self):
        self.rotated(rounds=2)
        h = history.History(self.dir)
        with mock.patch.object(history.subprocess, "run", side_effect=OSError("no python")):
            h._build_outside()
        self.assertTrue(h.ready())

    def test_hung_child_is_stopped(self):
        # it hung — /api/history answered building:true forever
        self.rotated(rounds=2)
        real = subprocess.run

        def hang(args, **kw):
            return real([sys.executable, "-c", "import time; time.sleep(20)"], **kw)
        started = time.time()
        with mock.patch.object(history, "GIVE_UP", 1, create=True):
            h, err = self.outside(hang)
        self.assertLess(time.time() - started, 10)
        self.assertIn("did not finish", err)
        self.assertFalse(h.ready())

    def in_child(self, work, timeout):
        """subprocess.run for the parse process: before the parse it does work (code, the archive parse goes after it),
        its TIMEOUT is timeout."""
        real = subprocess.run

        def run(args, **kw):
            code = "\n".join(["history.TIMEOUT = %r" % timeout, "_build = history.build", "def build(*a, **k):",
                              *("    " + line for line in work.splitlines()), "    return _build(*a, **k)",
                              "history.build = build", "history.main("])
            return real(args[:-1] + [args[-1].replace("history.main(", "\n" + code)], **kw)
        return run

    def parse_in_child(self, work, timeout):
        """self.outside with in_child; the clock backstop (GIVE_UP, hours) is cut: a watch that does not stop the child
        fails the test, not hangs it under the test lock for hours."""
        with mock.patch.object(history, "GIVE_UP", 120):
            return self.outside(self.in_child(work, timeout))

    def test_slow_parse_on_a_loaded_machine_is_not_stopped(self):
        # at load ~200 a parse yielding to the hub took 18 minutes by the clock and 4 of CPU; stopped after 10
        # by the clock, it started from scratch again and again — the feed past stayed empty for hours
        self.rotated(rounds=2)
        slow = "import time\nend = time.monotonic() + 4\nwhile time.monotonic() < end:\n" \
               "    t = time.process_time()\n    while time.process_time() - t < 0.02: pass\n    time.sleep(0.1)"
        with mock.patch.object(history, "TIMEOUT", 2, create=True):
            h, err = self.parse_in_child(slow, 2)
        self.assertTrue(h.ready(), err)
        self.assertEqual(h.failed, 0.0)

    def test_parse_without_work_stops_itself(self):
        # blocked: no CPU for TIMEOUT seconds — it stops itself, the page does not wait for the clock backstop (left alone,
        # the sleep ends and the archive gets parsed: ready)
        self.rotated(rounds=2)
        h, err = self.parse_in_child("import time\ntime.sleep(60)", 1)
        self.assertIn("without work", err)
        self.assertFalse(h.ready())

    def test_parse_in_a_loop_stops_itself(self):
        self.rotated(rounds=2)
        h, err = self.parse_in_child("while True: pass", 1)
        self.assertIn("more than 1 s of CPU", err)
        self.assertFalse(h.ready())

    def test_relative_state_folder(self):
        # the parsing process runs in the code folder: a relative path to the archives pointed there, no parsing happened
        self.rotated(rounds=2)
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        os.chdir(os.path.dirname(self.dir))
        h = history.History(os.path.basename(self.dir))
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            h._build_outside()
        self.assertTrue(h.ready(), err.getvalue())

    def test_one_process_and_timeout_per_archive(self):
        # a timeout on the whole parse killed a long tail of unparsed archives after an update entirely; the unfinished
        # parse of the killed process stayed next to the archive
        self.rotated(rounds=3)
        arch = os.path.join(self.dir, journal.ARCHIVE)
        names = history.History(self.dir).archives()
        real, runs = subprocess.run, []

        def run(args, **kw):
            runs.append((args[-1], kw["timeout"]))
            return real(args, **kw)
        h, err = self.outside(run)
        self.assertTrue(h.ready(), err)
        self.assertEqual(len(runs), len(names))
        for (code, timeout), name in zip(runs, names):
            self.assertIn(repr(name), code)
            self.assertEqual(timeout, history.GIVE_UP)

    @unittest.skipUnless(hasattr(os, "nice"), "Windows: the priority is given at the start (platforms.background)")
    def test_parse_process_gives_way_to_the_hub(self):
        # after a parse version bump a hub reparses hundreds of archives in the background — hours of CPU: the parse
        # process runs at a lower priority than the page that starts it
        if os.nice(0) >= 19:
            self.skipTest("the run itself is at the lowest priority")
        self.rotated(rounds=1)
        real, seen = subprocess.run, []

        def run(args, **kw):
            probe = args[:-1] + [args[-1] + "\nimport os, sys; sys.stdout.write(str(os.nice(0)))"]
            seen.append(real(probe, **{**kw, "stdout": subprocess.PIPE}).stdout.decode())
            return real(args, **kw)
        h, err = self.outside(run)
        self.assertTrue(h.ready(), err)
        self.assertEqual(len(seen), 1)
        self.assertGreater(int(seen[0]), os.nice(0))

    def test_unfinished_parse_older_than_the_timeout_is_removed(self):
        # the unfinished parse of a killed process (up to hundreds of MB) — by age, not by pid: after a machine restart
        # or in another namespace the process number is taken by a stranger, and such a file was never deleted
        self.rotated(rounds=1)
        arch = os.path.join(self.dir, journal.ARCHIVE)
        name = history.History(self.dir).archives()[0]
        dead = subprocess.Popen([sys.executable, "-c", ""])
        dead.wait()
        old = os.path.join(arch, f"{name}{history.CACHE}.{os.getpid()}.tmp")     # the pid is alive, the file is older than TIMEOUT
        fresh = os.path.join(arch, f"{name}{history.INDEX}.{dead.pid}.tmp")      # younger — may still be being written
        for path in (old, fresh):
            with open(path, "w") as f:
                f.write("{")
        past, writing = time.time() - history.TIMEOUT - 60, time.time() + 60   # the second is still being written — touched after the start
        os.utime(old, (past, past))
        os.utime(fresh, (writing, writing))
        history.main(arch, name)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))
        self.assertIsNotNone(history._load_index(os.path.join(arch, name)))

    def test_unfinished_parse_left_just_before_a_restart_goes_after_success(self):
        # the page was restarted mid-parse: the unfinished file is younger than TIMEOUT, and the archive was parsed by a
        # new process — main will not be called for it again, the file would stay forever
        self.rotated(rounds=1)
        arch = os.path.join(self.dir, journal.ARCHIVE)
        name = history.History(self.dir).archives()[0]
        left = os.path.join(arch, f"{name}{history.CACHE}.1.tmp")
        open(left, "w").close()
        past = time.time() - 5
        os.utime(left, (past, past))
        history.main(arch, name)
        self.assertFalse(os.path.exists(left))
        self.assertIsNotNone(history._load_index(os.path.join(arch, name)))

    def test_unfinished_parse_removed_by_someone_else_meanwhile(self):
        self.rotated(rounds=1)
        arch = os.path.join(self.dir, journal.ARCHIVE)
        name = history.History(self.dir).archives()[0]
        old = os.path.join(arch, f"{name}{history.CACHE}.1.tmp")
        open(old, "w").close()
        past = time.time() - history.TIMEOUT - 60
        os.utime(old, (past, past))
        with mock.patch.object(history.os, "remove", side_effect=FileNotFoundError(old)):
            history.main(arch, name)
        self.assertIsNotNone(history._load_index(os.path.join(arch, name)))

    def test_one_failed_archive_does_not_stop_the_others(self):
        # the parse of one archive failed — the others in the same round are parsed; the failed one — again no sooner than RETRY
        self.rotated(rounds=3)
        names = history.History(self.dir).archives()
        real = subprocess.run

        def run(args, **kw):
            if repr(names[0]) in args[-1]:
                return subprocess.CompletedProcess(args, 1, b"", b"ValueError")
            return real(args, **kw)
        h, err = self.outside(run)
        self.assertIn(names[0], err)
        self.assertEqual([h.indexed(n) is None for n in names], [True, False, False])
        self.assertGreater(h.failed, 0)

    def test_paging_stops_at_an_unparsed_archive(self):
        # a middle archive is unparsed (its parse failed): the feed does not jump over it to an older one — a hole in the
        # past would look like the whole history; we return what was gathered and more, after parsing — read the rest without gaps
        live = self.rotated(rounds=3)
        arch = os.path.join(self.dir, journal.ARCHIVE)
        h = history.History(self.dir)
        newest, middle, oldest = h.archives()
        for n in (newest, oldest):
            history.build(os.path.join(arch, n))
        h.failed = time.time()
        page = h.older("a", live.timelines["a"][0]["seq"], 1000, live.timelines["a"], build=False)
        texts = self.texts(page["items"])
        self.assertTrue(page["more"])
        self.assertIn("errand 25", texts)
        self.assertNotIn("errand 5", texts)
        again = h.older("a", page["items"][0]["seq"], 1000, live.timelines["a"], build=False)
        self.assertEqual((again["items"], again["more"]), ([], True))
        history.build(os.path.join(arch, middle))
        rest = h.older("a", page["items"][0]["seq"], 1000, live.timelines["a"], build=False)
        self.assertEqual(self.texts(rest["items"] + page["items"] + live.timelines["a"]),
                         [f"errand {i}" for i in range(30)])
        self.assertFalse(rest["more"])

    def test_one_look_at_the_disk_per_request(self):
        # an unparsed archive: the pointer from disk and the archive list once per request, not on every check
        live = self.rotated(rounds=3)
        h = history.History(self.dir)
        h.failed = time.time()
        with mock.patch.object(history, "_read_index", wraps=history._read_index) as load, \
                mock.patch.object(history.journal, "archives", wraps=history.journal.archives) as listed:
            h.older("a", live.timelines["a"][0]["seq"], 1000, live.timelines["a"], build=False)
        self.assertEqual(listed.call_count, 1)
        self.assertEqual(sorted(c.args[0] for c in load.call_args_list),
                         sorted(os.path.join(h.dir, n) for n in h.archives()))

    def test_unparsed_older_archive_is_not_the_beginning(self):
        # the parse of an old archive failed: more=False — the page wrote "Start of history" and stopped asking
        live = self.rotated(rounds=2)
        h = history.History(self.dir)
        newest = h.archives()[0]
        history.build(os.path.join(self.dir, journal.ARCHIVE, newest))
        h.failed = time.time()
        page = h.older("a", live.timelines["a"][0]["seq"], 1000, live.timelines["a"], build=False)
        self.assertNotIn("errand 0", self.texts(page["items"]))
        self.assertTrue(page["more"])

    def test_older_version_index_is_kept_for_rollback(self):
        # the previous version knows only <archive>.sessions.json: without it, at start it would read the parse of every
        # archive whole (2026-10-11: 1.4 GB)
        self.rotated(rounds=2)
        h = history.History(self.dir)
        h.build_all()
        arch = os.path.join(self.dir, journal.ARCHIVE)
        for n in h.archives():
            with open(os.path.join(arch, n + history.OLD_INDEX), encoding="utf-8") as f:
                old = json.load(f)
            self.assertEqual(old, {k: v[0] for k, v in h.sessions(n).items()})
            os.remove(os.path.join(arch, n + history.INDEX))   # a parse from the previous version: the pointer is rebuilt
        history.History(self.dir).build_all()
        self.assertTrue(all(os.path.exists(os.path.join(arch, n + history.OLD_INDEX)) for n in h.archives()))

    def test_cache_that_is_not_a_parse_is_rebuilt(self):
        live = self.rotated(rounds=1)
        arch = os.path.join(self.dir, journal.ARCHIVE)
        for n in history.History(self.dir).archives():
            with open(os.path.join(arch, n + history.CACHE), "w") as f:
                f.write("[]")
        h = history.History(self.dir)
        h.build_all()
        page = h.older("a", live.timelines["a"][0]["seq"], 1000, live.timelines["a"])
        self.assertEqual(self.texts(page["items"] + live.timelines["a"]), [f"errand {i}" for i in range(10)])

    def test_only_files_named_by_rotation_are_archives(self):
        # a copy of an archive under another name is not an archive: it is neither parsed nor scrolled
        live = self.rotated(rounds=2)
        arch = os.path.join(self.dir, journal.ARCHIVE)
        names = history.History(self.dir).archives()
        for n in names:
            shutil.copy2(os.path.join(arch, n), os.path.join(arch, n[:-len(".jsonl")] + " copy.jsonl"))
        open(os.path.join(arch, "journal-old-copy.jsonl"), "w").close()
        h = history.History(self.dir)
        self.assertEqual(h.archives(), names)
        h.build_all()
        self.assertFalse([n for n in os.listdir(arch) if "copy" in n and n.endswith(history.CACHE)])
        page = h.older("a", live.timelines["a"][0]["seq"], 1000, live.timelines["a"])
        self.assertEqual(self.texts(page["items"] + live.timelines["a"]), [f"errand {i}" for i in range(20)])


if __name__ == "__main__":
    unittest.main()


class _Archives:
    """Session a and journal archives with its messages — search helpers (not a TestCase: a subclass would pull in foreign tests)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hsearch-")
        self.hub = Hub(self.dir, NullHost(), registry_dir=self.dir)
        self.hub.record("session.requested", "a", {"session_id": "s-a", "cwd": "/w", **OWNER})
        self.hub.record("session.started", "a", {})

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def say(self, *texts):
        for text in texts:
            self.hub.record("message.received", None, {"to": "a", "text": text, **OWNER})

    def rotate(self):
        journal.rotate(self.dir, self.hub.state, keep=2, per_session=2)
        self.hub = Hub(self.dir, NullHost(), registry_dir=self.dir)

    def archives(self, n=3):
        """n archives of 5 messages each: a numbered "line <i>" and also a word starting with a capital
        Russian "yo" in every third (the search folds it with "ye", tests/fakes/lang.YO/YE)."""
        k = 0
        for _ in range(n):
            for _ in range(5):
                self.say(f"line {k}" + (f" about {lang.YO.upper()}lku" if k % 3 == 0 else ""))
                k += 1
            self.rotate()
        history.History(self.dir).build_all()

    def texts(self, got):
        return sorted(h["snippet"] for h in got["hits"])


class Search(_Archives, unittest.TestCase):
    """Search over the feed text in the past (first what is loaded, then the archive, no
    model): archives from newest to oldest in portions, a slice without the needle is not parsed, a line from several
    archives is one match."""

    def test_finds_in_every_archive_once_case_and_yo_insensitive(self):
        self.archives()
        got = history.search(self.dir, ["a"], f"{lang.YE.upper()}LK", 10 ** 12)
        self.assertTrue(got["done"])
        self.assertEqual(self.texts(got), sorted(f"line {k} about {lang.YO.upper()}lku" for k in range(15) if k % 3 == 0))
        self.assertEqual(len({h["key"] for h in got["hits"]}), len(got["hits"]))   # a rotation carry-over — once
        self.assertTrue(all(h["key"] == f"{h['seq']}in{h['id']}" for h in got["hits"]))

    def test_only_older_than_the_loaded_part(self):
        self.archives()
        everything = history.search(self.dir, ["a"], f"{lang.YE}lk", 10 ** 12)["hits"]
        cut = sorted(h["seq"] for h in everything)[2]
        got = history.search(self.dir, ["a"], f"{lang.YE}lk", cut)
        self.assertEqual(sorted(h["seq"] for h in got["hits"]), sorted(h["seq"] for h in everything if h["seq"] < cut))

    def test_portions_by_cursor_give_every_hit_exactly_once(self):
        self.archives(4)
        whole = history.search(self.dir, ["a"], "line", 10 ** 12)
        seen, cursor, calls = [], None, 0
        while True:
            # one archive per answer; budget 0 is not enough: the Windows clock of Python 3.12 ticks in ~15.6 ms, a fast
            # search spends "0 s" and takes every archive at once
            got = history.search(self.dir, ["a"], "line", 10 ** 12, cursor, budget=-1)
            calls += 1
            seen += [h["key"] for h in got["hits"]]
            if got["done"]:
                break
            self.assertEqual(got["left"], len(history.History(self.dir).archives()) - history.History(self.dir).archives().index(got["cursor"]))
            cursor = got["cursor"]
        self.assertGreater(calls, 2)
        # one line can appear twice in different portions (a rotation carry-over into a neighbouring archive) — the page merges by key
        self.assertEqual(set(seen), {h["key"] for h in whole["hits"]})

    def test_limit_stops_the_search(self):
        self.archives()
        got = history.search(self.dir, ["a"], "line", 10 ** 12, limit=4)
        self.assertEqual(len(got["hits"]), 4)
        self.assertTrue(got["capped"] and got["done"])

    def test_quotes_newlines_and_backslashes_are_found(self):
        # a slice of the parse is checked as JSON text: the needle goes through the same escaping, otherwise the slice would be dropped
        tricky = ['said "hello" to him', "first\nsecond", r"C:\path\to", "tab\tin", "emoji 🙂 here"]
        self.say(*tricky)
        self.rotate()
        history.History(self.dir).build_all()
        for q in ['"hello"', "first\nsecond", r"\path\to", "tab\tin", "🙂 here"]:
            got = history.search(self.dir, ["a"], q, 10 ** 12)
            self.assertEqual(len(got["hits"]), 1, q)

    def test_renamed_session_is_searched_under_its_old_name(self):
        self.say("old name: beacon")
        self.rotate()
        history.History(self.dir).build_all()
        self.assertEqual(len(history.search(self.dir, ["b", "a"], "beacon", 10 ** 12)["hits"]), 1)
        self.assertEqual(history.search(self.dir, ["b"], "beacon", 10 ** 12)["hits"], [])

    def test_unparsed_archive_stops_with_its_name(self):
        self.archives(2)
        newest = history.History(self.dir).archives()[0]
        os.remove(os.path.join(self.dir, journal.ARCHIVE, newest + history.INDEX))
        got = history.search(self.dir, ["a"], "line", 10 ** 12)
        self.assertEqual((got["unparsed"], got["cursor"], got["done"]), (True, newest, False))

    def test_a_chunk_without_the_text_is_not_parsed(self):
        self.archives()
        real = json.loads

        def loads(s, *a, **kw):   # pointers are read with json.load (as a string) — that is fine for them; a slice of the parse arrives as bytes
            if isinstance(s, bytes):
                raise AssertionError("a chunk without the needle was parsed")
            return real(s, *a, **kw)
        with mock.patch.object(history.json, "loads", side_effect=loads):
            self.assertEqual(history.search(self.dir, ["a"], "nowhere", 10 ** 12)["hits"], [])

    def test_runs_in_its_own_process_with_the_language(self):
        self.archives()
        h = history.History(self.dir)
        seen = []
        real = subprocess.Popen

        def spy(cmd, **kw):
            seen.append(kw["env"].get("HUB_LANG"))
            return real(cmd, **kw)
        with mock.patch.object(history.subprocess, "Popen", side_effect=spy):
            got = h.search(["a"], f"{lang.YE}lk", 10 ** 12)
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0])
        self.assertEqual(self.texts(got), self.texts(history.search(self.dir, ["a"], f"{lang.YE}lk", 10 ** 12)))

    def test_a_new_search_of_the_same_tab_cancels_the_old_one(self):
        h = history.History(self.dir)
        started, killed = threading.Event(), threading.Event()

        class Slow:
            returncode = None

            def __init__(self, *a, **kw):
                started.set()

            def communicate(self, data=None, timeout=None):
                killed.wait(5)
                self.returncode = -9
                return b"", b""

            def kill(self):
                killed.set()
        out = {}
        with mock.patch.object(history.subprocess, "Popen", Slow):
            t = threading.Thread(target=lambda: out.setdefault("old", h.search(["a"], "one", 10 ** 12, tab="t1")))
            t.start()
            self.assertTrue(started.wait(5))
        self.archives(1)
        out["new"] = h.search(["a"], "line", 10 ** 12, tab="t1")
        t.join(5)
        self.assertTrue(out["old"]["cancelled"])
        self.assertEqual(len(out["new"]["hits"]), 5)

    def test_a_search_waiting_for_a_slot_is_cancelled_at_once(self):
        # both slots are taken by long searches from other tabs; this tab typed further — its previous request does not wait for a slot
        h, release, started = history.History(self.dir), threading.Event(), threading.Semaphore(0)

        class Slow:
            returncode = 0

            def __init__(self, *a, **kw):
                started.release()

            def communicate(self, data=None, timeout=None):
                release.wait(10)
                return b'{"hits": [], "cursor": null, "done": true}', b""

            def kill(self):
                release.set()
        out = {}
        with mock.patch.object(history.subprocess, "Popen", Slow):
            busy = [threading.Thread(target=h.search, args=(["a"], "one", 10 ** 12), kwargs={"tab": f"b{i}"}) for i in range(2)]
            for x in busy:
                x.start()
            self.assertTrue(started.acquire(timeout=5) and started.acquire(timeout=5))
            waiting = threading.Thread(target=lambda: out.setdefault("old", h.search(["a"], "one", 10 ** 12, tab="me")))
            waiting.start()
            time.sleep(0.3)
            t = time.monotonic()
            again = threading.Thread(target=lambda: out.setdefault("new", h.search(["a"], "two", 10 ** 12, tab="me")))
            again.start()
            waiting.join(2)
            self.assertFalse(waiting.is_alive())                # withdrawn while waiting for a slot — without waiting for the others
            self.assertLess(time.monotonic() - t, 1.5)
            self.assertTrue(out["old"]["cancelled"])
            release.set()
            for x in busy + [again]:
                x.join(5)
        self.assertTrue(out["new"]["done"])

    def test_snippet_of_a_text_whose_fold_changes_length(self):
        got = history.hit({"seq": 1, "kind": "out", "text": "İİİİ " * 30 + "beacon"}, "İİİİ " * 30 + "beacon", "beacon")
        self.assertIn("beacon", got["snippet"])                   # the match position is by the characters of the original text

    def test_process_that_cannot_start_is_not_searched_in_the_page(self):
        h = history.History(self.dir)
        with mock.patch.object(history.subprocess, "Popen", side_effect=OSError("no")), \
                mock.patch.object(history, "search", side_effect=AssertionError("search in the page process")):
            got = h.search(["a"], "line", 10 ** 12)
        self.assertTrue(got["failed"] and got["unavailable"])

    def test_at_most_two_processes_at_once(self):
        self.assertEqual(history.SEARCH_AT_ONCE, 2)
        h, now, top, lock = history.History(self.dir), [0], [0], threading.Lock()
        real = subprocess.Popen

        class Count:
            def __init__(self, *a, **kw):
                with lock:
                    now[0] += 1
                    top[0] = max(top[0], now[0])
                self.p = real(*a, **kw)
                self.returncode = None

            def communicate(self, data=None, timeout=None):
                time.sleep(0.2)
                got = self.p.communicate(data, timeout)
                self.returncode = self.p.returncode
                with lock:
                    now[0] -= 1
                return got

            def kill(self):
                self.p.kill()
        with mock.patch.object(history.subprocess, "Popen", Count):
            ts = [threading.Thread(target=h.search, args=(["a"], "line", 10 ** 12)) for _ in range(5)]
            for x in ts:
                x.start()
            for x in ts:
                x.join(20)
        self.assertEqual(top[0], 2)

    def test_window_after_a_hit_goes_through_archives_into_the_live_feed(self):
        self.archives(3)
        self.say("live 1", "live 2")
        live = views.Reader()
        live.feed_all(journal.read(os.path.join(self.dir, "journal.jsonl")))
        h = history.History(self.dir)
        hit = min(history.search(self.dir, ["a"], "line 2", 10 ** 12)["hits"], key=lambda x: x["seq"])
        got = h.newer("a", hit["seq"], 100, live.timelines["a"])
        texts = [it["text"] for it in got["items"] if it.get("kind") == "in"]
        self.assertEqual(texts[0], f"line 3 about {lang.YO.upper()}lku")
        self.assertEqual(texts[-2:], ["live 1", "live 2"])
        self.assertEqual(len(texts), len(set(texts)))
        seqs = [it["seq"] for it in got["items"]]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(h.newer("a", hit["seq"], 3, live.timelines["a"])["items"]), 3)

    def test_window_after_through_an_unparsed_archive_starts_its_parse(self):
        self.archives(3)
        h = history.History(self.dir)
        names = list(reversed(h.archives()))   # from oldest to newest
        os.remove(os.path.join(self.dir, journal.ARCHIVE, names[1] + history.INDEX))
        h = history.History(self.dir)
        with mock.patch.object(h, "build_in_background") as build:
            got = h.newer("a", 0, 100, [])
        self.assertTrue(got["building"])
        build.assert_called_once()


class RotationUnderThePage(_Archives, unittest.TestCase):
    """The page runs, the journal rotates: the new archive is parsed without anyone scrolling the feed (hub show --grep
    searches only parsed archives)."""

    def page(self):
        from modules.web.server import Web
        web = Web(self.dir)
        self.addCleanup(web.stop.set)
        web.tail.poll()
        if web.history.building:
            web.history.building.join(30)
        return web

    def test_new_archive_is_parsed_after_rotation(self):
        web = self.page()
        self.say("before the rotation")
        self.rotate()
        self.say("after")
        web.tail.poll()                                # the page sees the journal started anew
        if web.history.building:
            web.history.building.join(60)
        self.assertEqual(len(web.history.archives()), 1)
        self.assertTrue(web.history.ready())
        self.assertEqual([who for who, _ in history.grep(self.dir, "before the rotation")[0]], ["a"])

    def test_archive_that_came_during_a_parse_is_parsed_after_it(self):
        h = history.History(self.dir)
        inside, release, passes = threading.Event(), threading.Event(), []

        def outside(tried):
            passes.append(1)
            inside.set()
            release.wait(10)
        with mock.patch.object(h, "_build_outside", side_effect=outside):
            h.build_in_background()
            self.assertTrue(inside.wait(10))
            h.build_in_background()                     # scrolling: leaves the running parse alone
            h.build_in_background(appeared=True)        # an archive came after it listed them
            release.set()
            h.building.join(10)
        self.assertEqual(len(passes), 2)
        self.assertFalse(h.running)

    def test_scrolling_during_a_parse_does_not_make_another_pass(self):
        h = history.History(self.dir)
        inside, release, passes = threading.Event(), threading.Event(), []

        def outside(tried):
            passes.append(1)
            inside.set()
            release.wait(10)
        with mock.patch.object(h, "_build_outside", side_effect=outside):
            h.build_in_background()
            self.assertTrue(inside.wait(10))
            h.build_in_background()
            release.set()
            h.building.join(10)
        self.assertEqual(len(passes), 1)

    def test_an_archive_that_failed_is_not_retried_by_the_next_pass(self):
        self.say("one")
        self.rotate()
        h = history.History(self.dir)
        with mock.patch.object(history.subprocess, "run", side_effect=history.subprocess.TimeoutExpired("x", 1)) as run:
            tried = set()
            h._build_outside(tried)
            h._build_outside(tried)
        self.assertEqual(run.call_count, 1)

    def test_the_first_read_is_not_a_rotation(self):
        self.say("one")
        self.rotate()
        from modules.web.server import Web
        web = Web(self.dir)
        self.addCleanup(web.stop.set)
        with mock.patch.object(web.history, "build_in_background") as build:
            web.tail.poll()
            web.tail.poll()
        build.assert_not_called()


class SearchFields(unittest.TestCase):
    """Where in a feed line the text is searched — one list in the core (views.SEARCH_FIELDS), the page takes it from the snapshot."""

    def test_fields_by_kind(self):
        f = lambda it, q: views.found(it, views.fold(q))
        self.assertTrue(f({"kind": "tool", "name": "Bash", "input": {"command": "git log", "x": [{"y": "deep"}]}}, "DEEP"))
        self.assertTrue(f({"kind": "tool", "name": "Bash", "input": {}, "result": "command output"}, "output"))
        self.assertTrue(f({"kind": "in", "text": "Answer to question q1", "label": "Pressed: Yes"}, "pressed"))
        self.assertFalse(f({"kind": "in", "text": "Answer to question q1", "label": "Pressed: Yes"}, "question q1"))  # label instead of text
        self.assertTrue(f({"kind": "answer", "answers": {"Where?": "left"}}, "left"))
        self.assertFalse(f({"kind": "answer", "behavior": "allow"}, "allow"))   # the page writes the "Allowed" label
        self.assertFalse(f({"kind": "out", "text": "one", "junk": "two"}, "two"))
        self.assertFalse(f({"kind": "out", "text": "glu", "x": "ed"}, "glued"))

    def test_text_filter_is_never_narrower_than_the_check(self):
        # history.search drops a slice if the needle is not in its JSON text: everything found will find is in there
        yo, ye, word = lang.YO.upper(), lang.YE, lang.word("words", "hub")   # the folded pair and a plain Russian word
        items = [{"kind": "tool", "name": "Edit", "input": {"old": 'a "b"\n\tc\\d', "new": [yo, {"k": "\u2028x"}]},
                  "result": "</script>"},
                 {"kind": "in", "text": f"{yo}{word.upper()} \"{ye}\" {word}", "quote": f"{word}\r\n",
                  "attachments": [{"name": f"{ye}.png"}]},
                 {"kind": "summary", "decided": [{"session": "s", "text": word}]}]
        for it in items:
            chunk = views.fold(json.dumps([it], ensure_ascii=False, separators=(",", ":")))
            for s in views.search_texts(it):
                for a in range(len(s)):
                    for b in range(a + 1, min(len(s), a + 6) + 1):
                        q = s[a:b]
                        if views.found(it, views.fold(q)) is not None:
                            self.assertIn(views.fold(json.dumps(q, ensure_ascii=False)[1:-1]), chunk, repr(q))

    def test_every_kind_the_feed_draws_or_the_core_writes_has_fields(self):
        import re
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "modules", "web", "static", "js", "feed.js"), encoding="utf-8") as f:
            feed = f.read()
        body = feed[feed.index("const KINDS = {"):]
        body = body[:body.index("\n};")]
        drawn = set(re.findall(r"^  ([a-z_]+):", body, re.M)) - {"group"}   # group is a page-side merge, not a record
        with open(os.path.join(root, "core", "views.py"), encoding="utf-8") as f:
            src = f.read()
        written = set(re.findall(r'"kind": "([a-z_]+)"', src)) - {"gone"}   # gone belongs to "Flow", not the feed
        self.assertGreater(len(drawn), 8)
        self.assertEqual(sorted((drawn | written) - set(views.SEARCH_FIELDS)), [])

    def test_snapshot_carries_the_fields(self):
        r = views.Reader()
        self.assertEqual(r.snapshot()["search_fields"], views.SEARCH_FIELDS)


class SearchPage(_Archives, unittest.TestCase):
    """The page's /api/history/search: the core's live feed older than what is loaded, then the archives; `after` on
    /api/history is a window around the match."""

    def serve(self):
        import http.client
        from modules.web.server import Web
        self.web = Web(self.dir)
        server = self.web.serve(port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(self.web.stop.set)
        self.web.tail.poll()
        port = server.server_address[1]

        def get(path):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
            c.request("GET", path, headers={"Host": f"127.0.0.1:{port}", "X-Hub": "1"})
            r = c.getresponse()
            body = r.read()
            c.close()
            return r.status, json.loads(body)
        return get

    def test_live_feed_then_archives_without_repeats(self):
        self.archives(2)
        self.say(f"live about {lang.YO}lku")
        get = self.serve()
        code, got = get(f"/api/history/search?session=a&q={quote(lang.YE.upper())}LK&before=1000000000000&tab=x")
        self.assertEqual(code, 200)
        texts = [h["snippet"] for h in got["hits"]]
        self.assertIn(f"live about {lang.YO}lku", texts)
        self.assertEqual(len(texts), len(set(h["key"] for h in got["hits"])))
        self.assertEqual(sorted(t for t in texts if t.startswith("line")),
                         sorted(f"line {k} about {lang.YO.upper()}lku" for k in range(10) if k % 3 == 0))
        self.assertTrue(got["done"])

    def test_short_query_is_refused(self):
        get = self.serve()
        self.assertEqual(get("/api/history/search?session=a&q=%D1%91")[0], 400)

    def test_window_after(self):
        self.archives(2)
        get = self.serve()
        code, whole = get("/api/history?session=a&before=1000000000000&n=500")
        first = [it for it in whole["items"] if it.get("kind") == "in"][0]
        code, got = get(f"/api/history?session=a&after={first['seq']}&n=3")
        self.assertEqual(code, 200)
        nxt = [it["seq"] for it in whole["items"] if it["seq"] > first["seq"]][:3]   # exactly the next three, not the last ones
        self.assertEqual([it["seq"] for it in got["items"]], nxt)

    def test_path_is_the_core_s(self):
        from core import modclaims
        self.assertIn("/api/history/search", modclaims.CORE_PATHS)
