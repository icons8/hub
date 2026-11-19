"""The journal archive holds each event once. Rotation links the whole old journal into the archive (readers of the page,
the Telegram bot and the watchdog go on reading it by its file), so the newest archive repeats what the previous rotation
carried and its state snapshots. At the next rotation that archive is rewritten: the snapshot the new events start from
plus the new events, nothing else. Feeds, history pages, spending and the "Flow" read the same as from whole archives.
Archives written before this change are rewritten only by `hub journal compact`."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from core import flow, history, journal, lock, usage
from core.daemon import Hub
from tests.fakes import can
from tests.test_snapshot import NullHost

OWNER = {"channel": "web", "owner": True}
ROUNDS = 5
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def clock():
    clock.now += 1
    return clock.now


clock.now = 1000.0


def play(state_dir, compact=True):
    """Two sessions over ROUNDS rotations on the fly; each round a tool call starts before the rotation and gets its
    result after it, and a periodic snapshot sits in the middle - as in a real journal. compact - the hub's
    journal.compact_on_rotate."""
    clock.now = 1000.0
    hub = Hub(state_dir, NullHost(), registry_dir=state_dir, clock=clock)
    hub.config = {**hub.config, "journal": {"compact_on_rotate": compact}}
    for name in ("a", "b"):
        hub.record("session.requested", name, {"session_id": f"s-{name}", "cwd": "/w", **OWNER})
        hub.record("session.started", name, {})
    keep, journal.KEEP, per, journal.PER_SESSION = journal.KEEP, 6, journal.PER_SESSION, 3
    try:
        for r in range(ROUNDS):
            for i in range(4):
                hub.record("message.received", None, {"to": "a" if i % 2 else "b", "text": f"task {r}.{i}", **OWNER})
                hub.record("turn.started", "a", {"marker": f"m{r}.{i}"})
                hub.record("turn.result", "a", {"text": f"result {r}.{i}", "cost_usd": 0.01 * (4 * r + i + 1)})
                hub.record("turn.stopped", "a", {})
                if i == 1:
                    hub.snapshot("periodic")
            hub.record("message.received", None, {"to": "b", "text": f"long {r}", "channel": "session", "sender": "a"})
            hub.record("turn.started", "b", {"marker": f"x{r}", "prompt_head": f"[hub:x{r} from a] long {r}"})
            hub.record("turn.tool", "a", {"id": f"t{r}", "name": "Bash", "input": {"command": f"make {r}"}})
            hub.rotate_bytes, hub.carried = 1, 0
            hub.rotate_if_big()
            hub.record("turn.tool_result", "a", {"id": f"t{r}", "text": f"collected {r}"})
            hub.record("turn.result", "b", {"text": f"done {r}"})
            hub.record("turn.stopped", "b", {})
    finally:
        journal.KEEP, journal.PER_SESSION = keep, per
    hub.snapshot("stop")
    return hub


def events(path):
    return [e for e in journal.read(path) if e["type"] != "core.snapshot"]


def rows(state_dir, name):
    """The whole archived feed of a session as the page pages it, without the reader's own keys."""
    h = history.History(state_dir)
    h.build_all()
    got = h.older(name, 10 ** 9, 10 ** 6, [])["items"]
    return [{k: v for k, v in it.items() if k not in ("rev", "k")} for it in got]


class Compact(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="compact-")
        self.old = tempfile.mkdtemp(prefix="compact-old-")
        usage.reset()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        shutil.rmtree(self.old, ignore_errors=True)
        usage.reset()

    def whole(self):
        """The same journal as an earlier hub (or one without journal.compact_on_rotate) wrote it: every archive whole."""
        play(self.old, compact=False)

    def test_by_default_rotation_leaves_archives_whole(self):
        hub = Hub(self.dir, NullHost(), registry_dir=self.dir, clock=clock)
        self.assertIs(hub.journal_settings().get("compact_on_rotate"), False)
        self.whole()
        paths = journal.archives(self.old)
        self.assertGreater(len(paths), 2)
        for p in paths:
            self.assertFalse((journal.read(p)[0].get("data") or {}).get("compact"))
        for p in paths[1:]:   # each still repeats the carry and keeps its snapshots - nothing was rewritten
            self.assertGreater(len([e for e in journal.read(p) if e["type"] == "core.snapshot"]), 1)
        self.assertTrue(journal.compact_archives(self.old))   # the command still finds what to drop

    def test_each_event_lies_in_the_archive_chain_once(self):
        play(self.dir)
        paths = journal.archives(self.dir)
        self.assertEqual(len(paths), ROUNDS)
        *older, newest = paths
        seen = {}
        for p in older:
            for e in events(p):
                self.assertNotIn(e["seq"], seen, f"{e['seq']} — and in {seen.get(e['seq'])}, and in {p}")
                seen[e["seq"]] = p
            snaps = [e for e in journal.read(p) if e["type"] == "core.snapshot"]
            self.assertLessEqual(len(snaps), 1)
        self.assertFalse(set(seen) & {e["seq"] for e in events(newest) if e["seq"] > journal.archive_last(older[-1])})
        everything = set(seen) | {e["seq"] for e in events(newest)} | {e["seq"] for e in events(
            os.path.join(self.dir, "journal.jsonl"))}
        self.whole()
        truth = set()
        for p in journal.archives(self.old) + [os.path.join(self.old, "journal.jsonl")]:
            truth |= {e["seq"] for e in events(p)}
        self.assertEqual(everything, truth)   # nothing lost
        self.assertLess(sum(map(os.path.getsize, older)),
                        sum(map(os.path.getsize, journal.archives(self.old)[:-1])) // 2)

    def test_the_newest_archive_is_still_the_former_journal_itself(self):
        # readers go on reading the archive by the file they held (journal.rotated_from)
        play(self.dir)
        path = os.path.join(self.dir, "journal.jsonl")
        held = journal.identity(path)
        offset = os.path.getsize(path)
        hub = Hub(self.dir, NullHost(), registry_dir=self.dir)
        hub.record("message.received", None, {"to": "a", "text": "before the rotation", **OWNER})
        hub.rotate_bytes, hub.carried = 1, 0
        hub.rotate_if_big()
        got = journal.go_on(path, held, offset, journal.identity(path))
        self.assertIsNotNone(got)
        self.assertIn("before the rotation", [(e.get("data") or {}).get("text") for e in got[0]])

    def test_feeds_spending_and_flow_are_the_same_as_from_whole_archives(self):
        play(self.dir)
        self.whole()
        for name in ("a", "b"):
            self.assertEqual(rows(self.dir, name), rows(self.old, name))
        mine, theirs = usage.facts(self.dir), usage.facts(self.old)
        self.assertEqual(mine, theirs)
        self.assertTrue(any(f["type"] == "turn.result" for f in mine))
        signals = flow.history(self.dir, 0, now=clock.now)
        # a turn started before a rotation ends after it: its result goes to whoever asked for it
        self.assertEqual(len([s for s in signals if s["kind"] == "result" and s["from"] == "b" and s["to"] == "a"]),
                         ROUNDS)
        self.assertEqual(signals, flow.history(self.old, 0, now=clock.now))

    def test_a_tool_call_across_a_rotation_ends_in_the_history(self):
        play(self.dir)
        tools = {it["id"]: it for it in rows(self.dir, "a") if it.get("kind") == "tool"}
        for r in range(ROUNDS - 1):
            self.assertEqual((tools[f"t{r}"].get("status"), tools[f"t{r}"].get("result")), ("done", f"collected {r}"))

    def test_mtime_and_access_of_a_compacted_archive_are_kept(self):
        self.whole()
        older = journal.archives(self.old)[:-1]
        before = {p: (os.stat(p).st_mtime_ns, os.stat(p).st_mode) for p in older}
        found = journal.compact_archives(self.old, apply=True)
        self.assertEqual(len(found), len(older))
        self.assertEqual({p: (os.stat(p).st_mtime_ns, os.stat(p).st_mode) for p in older}, before)

    def test_archives_written_without_the_setting_are_left_alone_by_rotation(self):
        self.whole()
        sizes = {p: os.path.getsize(p) for p in journal.archives(self.old)}
        hub = Hub(self.old, NullHost(), registry_dir=self.old)
        hub.config = {**hub.config, "journal": {"compact_on_rotate": True}}   # turned on after an update
        hub.record("message.received", None, {"to": "a", "text": "after the update", **OWNER})
        hub.rotate_bytes, hub.carried = 1, 0
        hub.rotate_if_big()   # the journal began without the setting: the archive before it is not rewritten
        self.assertEqual({p: os.path.getsize(p) for p in sizes}, sizes)
        hub.record("message.received", None, {"to": "a", "text": "more", **OWNER})
        hub.rotate_bytes, hub.carried = 1, 0
        hub.rotate_if_big()   # this journal began under the new rule: the archive it began from is rewritten
        changed = [p for p in sizes if os.path.getsize(p) != sizes[p]]
        self.assertEqual(changed, [])
        newer = [p for p in journal.archives(self.old) if p not in sizes]
        self.assertEqual(len(newer), 2)
        last = max(map(journal.archive_last, sizes))
        self.assertTrue(all(e["seq"] > last for e in events(newer[0])))
        self.assertEqual(len([e for e in journal.read(newer[0]) if e["type"] == "core.snapshot"]), 1)

    def test_the_command_rewrites_old_archives_and_counts_first(self):
        self.whole()
        shutil.copytree(self.old, self.dir, dirs_exist_ok=True)
        for p in journal.archives(self.dir):   # parses of the whole archives: they stay valid
            history.build(p)
        paths = journal.archives(self.dir)
        sizes = {p: os.path.getsize(p) for p in paths}
        found = journal.compact_archives(self.dir)                     # counting only
        self.assertEqual({p: os.path.getsize(p) for p in paths}, sizes)
        self.assertEqual([p for p, _, _ in found], paths[:-1])         # the newest stays whole: readers hold it
        self.assertTrue(all(after < before for _, before, after in found))
        done = journal.compact_archives(self.dir, apply=True,
                                       backup_dir=os.path.join(self.dir, "backup", "compact-1"))
        self.assertEqual([(p, b, a) for p, b, a in done], found)
        self.assertEqual([os.path.getsize(p) for p in paths[:-1]], [a for _, _, a in found])
        backup = [d for d in os.listdir(os.path.join(self.dir, "backup")) if d.startswith("compact-")]
        self.assertEqual(len(backup), 1)
        for p in paths[:-1]:   # the originals as they were
            kept = os.path.join(self.dir, "backup", backup[0], os.path.basename(p))
            self.assertEqual(os.path.getsize(kept), sizes[p])
        self.assertEqual(journal.compact_archives(self.dir, apply=True), [])   # a repeat finds nothing
        usage.reset()
        for name in ("a", "b"):
            self.assertEqual(rows(self.dir, name), rows(self.old, name))
        for p in paths:   # and parsed anew from the compacted archives - the same feed
            for side in (history.CACHE, history.INDEX, history.OLD_INDEX):
                os.remove(p + side)
        for name in ("a", "b"):
            self.assertEqual(rows(self.dir, name), rows(self.old, name))
        self.assertEqual(usage.facts(self.dir), usage.facts(self.old))

    def test_events_are_kept_when_the_archive_they_were_carried_from_is_gone(self):
        self.whole()
        paths = journal.archives(self.old)
        os.remove(paths[1])   # the carry of the third archive now lies nowhere else
        before = {e["seq"] for e in events(paths[2])}
        journal.compact_archives(self.old, apply=True)
        self.assertEqual({e["seq"] for e in events(paths[2])}, before)

    def test_an_archive_of_an_earlier_hub_is_read_as_before(self):
        self.whole()
        for name in ("a", "b"):
            self.assertTrue(rows(self.old, name))
        texts = [it.get("text") for it in rows(self.old, "b") if it.get("kind") == "in" and "task" in it.get("text")]
        self.assertEqual(texts, [f"task {r}.{i}" for r in range(ROUNDS) for i in (0, 2)])


    def hub_cli(self, *args, session=None):
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE")}
        env.update(HUB_STATE_DIR=self.old, HUB_LANG="en", **({"HUB_SESSION": session} if session else {}))
        return subprocess.run([sys.executable, os.path.join(ROOT, "bin", "hub"), "journal", "compact", *args], env=env,
                              capture_output=True, text=True, timeout=120)

    def test_the_owner_counts_then_compacts_from_a_terminal_and_a_session_cannot(self):
        self.whole()
        paths = journal.archives(self.old)
        sizes = [os.path.getsize(p) for p in paths]
        r = self.hub_cli("--apply", session="a")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual([os.path.getsize(p) for p in paths], sizes)
        r = self.hub_cli()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("MB to free", r.stdout)
        self.assertIn(os.path.basename(paths[0]), r.stdout)
        self.assertEqual([os.path.getsize(p) for p in paths], sizes)
        r = self.hub_cli("--apply", "--backup")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Rewrote 4 archives", r.stdout)
        self.assertNotIn("Without --backup", r.stderr)
        self.assertLess(sum(os.path.getsize(p) for p in paths), sum(sizes))
        self.assertTrue(os.path.isdir(os.path.join(self.old, "backup")))
        self.assertIn("the archives repeat nothing", self.hub_cli().stdout)

    def test_apply_without_backup_warns_that_it_cannot_be_undone(self):
        self.whole()
        self.assertNotIn("Without --backup", self.hub_cli().stderr)   # only counted: nothing to warn about
        bare = self.hub_cli("--apply")
        self.assertEqual(bare.returncode, 0, bare.stderr)
        self.assertIn("Without --backup the original archives are not kept", bare.stderr)
        self.assertIn("Rewrote", bare.stdout)

    def test_a_stop_right_after_a_rotation_keeps_the_rotation_snapshot_first(self):
        # hubd stopped before any new event: its "stop" snapshot follows the rotation one — the archive still begins
        # with the rotation snapshot, so readers find the archive it continues
        hub = play(self.dir)
        hub.record("message.received", None, {"to": "a", "text": "before the rotation", **OWNER})
        hub.rotate_bytes, hub.carried = 1, 0
        hub.rotate_if_big()
        hub.snapshot("stop")
        hub.snapshot("start")
        for text in ("after the stop", "more"):
            hub.record("message.received", None, {"to": "a", "text": text, **OWNER})
            hub.rotate_bytes, hub.carried = 1, 0
            hub.rotate_if_big()
        compacted = journal.archives(self.dir)[-2]
        first = journal.read(compacted)[0]
        self.assertEqual((first["type"], first["data"]["why"]), ("core.snapshot", "rotate"))
        self.assertEqual(journal.before(compacted), journal.archives(self.dir)[-3])
        self.assertEqual(len([e for e in journal.read(compacted) if e["type"] == "core.snapshot"]), 1)

    def test_an_archive_without_events_of_its_own_keeps_its_state(self):
        folder = os.path.join(self.dir, journal.ARCHIVE)
        os.makedirs(folder)

        def ev(seq, type="message.received", **data):
            return {"seq": seq, "ts": seq, "src": "core", "type": type, "entity": None, "data": data}
        state = {"sessions": {}}
        lines = {"journal-1-3.jsonl": [ev(1), ev(2), ev(3)],
                 "journal-2-5.jsonl": [ev(2, "core.snapshot", why="rotate-start", archive="journal-1-3.jsonl",
                                          compact=True, per_session=3, state=state), ev(3),
                                       ev(4, "core.snapshot", why="rotate", archive="journal-1-3.jsonl", compact=True,
                                          state=state), ev(5, "core.snapshot", why="stop", state=state)]}
        for name, evs in lines.items():
            with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
                f.writelines(journal._line(e) for e in evs)
        path = os.path.join(folder, "journal-2-5.jsonl")
        self.assertIsNotNone(journal.compact(path))
        self.assertEqual([(e["seq"], e["data"].get("why")) for e in journal.read(path)], [(4, "rotate")])

    # Windows does not let another writer replace a file compact holds open: there the archive stays whole, and the
    # callers leave it so (journal.rotate, compact_all — "a reader holding the file")
    @unittest.skipUnless(can.replace_open(), "a file held open cannot be replaced here (Windows)")
    def test_an_archive_rewritten_meanwhile_is_left_as_the_other_writer_made_it(self):
        # `hub journal scrub` replaces archives too: what compact planned from the old file must not land on the new one
        self.whole()
        path = journal.archives(self.old)[1]
        with open(path, "rb") as f:
            theirs = f.read()
        real = journal.platforms.current.sync

        def other_writer(fd):
            real(fd)
            other = path + ".other"
            with open(other, "wb") as f:
                f.write(theirs + b"\n")
            os.replace(other, path)
        with mock.patch.object(journal.platforms.current, "sync", other_writer):
            with self.assertRaises(OSError):
                journal.compact(path)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), theirs + b"\n")
        self.assertEqual([n for n in os.listdir(os.path.dirname(path)) if n.startswith(".")], [])

    def test_compact_replaces_under_the_lock_scrub_replaces_under(self):
        # between compact's check and its replacement `hub journal scrub` cannot put its cleaned file (it would be lost)
        self.whole()
        path, free = journal.archives(self.old)[1], []
        real = journal.platforms.current.replace

        def replace(src, dst):
            f = lock.acquire(os.path.join(self.old, "archive.lock"))
            free.append(f is not None)
            if f:
                f.close()
            return real(src, dst)
        with mock.patch.object(journal.platforms.current, "replace", replace):
            self.assertIsNotNone(journal.compact(path))
        self.assertEqual(free, [False])

    @unittest.skipUnless(can.replace_open(), "a file held open cannot be replaced here (Windows): compact waits for scrub")
    def test_scrub_reads_again_an_archive_compact_replaced_meanwhile(self):
        # compact replaces the archive while `hub journal scrub` reads it: scrub must not mix the two files
        from core import scrub
        self.whole()
        path = journal.archives(self.old)[1]
        key = "ghp_" + "Q" * 36
        with open(path, "rb") as f:
            raw = f.read()
        at = raw.rindex(b'"command":"make ') + len(b'"command":"')   # an event of its own, not of the carry
        with open(path, "wb") as f:
            f.write(raw[:at] + key.encode() + b" " + raw[at:])
        real, calls = scrub._lines, []

        def lines(p):
            calls.append(p)
            for i, got in enumerate(real(p)):
                yield got
                if i == 0 and len(calls) == 1:
                    journal.compact(path)
        with mock.patch.object(scrub, "_lines", lines):
            self.assertGreater(scrub.rewrite(path), 0)
        with open(path, "rb") as f:
            self.assertNotIn(key.encode(), f.read())
        got = journal.read(path)
        self.assertEqual([e["seq"] for e in got], sorted({e["seq"] for e in got}))
        self.assertEqual(len([e for e in got if e["type"] == "core.snapshot"]), 1)   # compacted, and stays so

    def test_the_archive_is_closed_before_it_is_replaced(self):
        # Windows refuses to replace a file this process holds open (open() has no FILE_SHARE_DELETE): compact must let
        # go of the archive first, or it is never rewritten there
        self.whole()
        path = journal.archives(self.old)[1]
        opened, real_open, real_replace = [], open, journal.platforms.current.replace

        def tracked(file, *a, **kw):
            f = real_open(file, *a, **kw)
            opened.append((file, f))
            return f

        def windows_replace(src, dst):
            if any(file == dst and not f.closed for file, f in opened):
                raise PermissionError(13, "the file is open", dst)
            return real_replace(src, dst)
        with mock.patch.object(journal, "open", tracked, create=True), \
                mock.patch.object(journal.platforms.current, "replace", windows_replace):
            self.assertIsNotNone(journal.compact(path))
        self.assertEqual([n for n in os.listdir(os.path.dirname(path)) if n.startswith(".")], [])

    def test_the_command_goes_on_past_an_archive_it_cannot_rewrite(self):
        self.whole()
        paths = journal.archives(self.old)
        real, skipped = journal.compact, []

        def held(path, *a, **kw):
            if path == paths[1] and kw.get("apply", True):
                raise PermissionError(32, "the file is in use by another process", path)
            return real(path, *a, **kw)
        with mock.patch.object(journal, "compact", held):
            done = journal.compact_archives(self.old, apply=True, failed=lambda path, e: skipped.append(path))
        self.assertEqual(skipped, [paths[1]])
        self.assertEqual([p for p, _, _ in done], [p for p in paths[:-1] if p != paths[1]])


if __name__ == "__main__":
    unittest.main()
