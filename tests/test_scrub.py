"""Keys that an earlier hub already wrote (OSS security spec §2.2): `hub journal scrub` rewrites archives and
snapshots the way the current hub writes them — each event on its own line with the same seq and type, unchanged
lines byte for byte; without --apply it only counts, with --backup the old files go to backup/. The live journal is
cleaned by a rotation with scrubbing (hubd on a tick, or the command itself when hubd is not running). Only by the
command. All values are made up."""
import tests  # noqa: F401 — keep the machine's settings out (tests/__init__.py)
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from core import history, journal, lock, reader_cache, redact, reducer, scrub, shown, spool, tail, usage
from core.daemon import Hub
from hosts.streamjson import StreamHost

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = "tracker-" + "s3cr3t" * 4
GH = "ghp_" + "Q" * 36
SERVER = {"command": "npx", "args": ["mcp-tracker", "--token", "sk-proj-" + "x9Y8z7W6" * 5], "env": {"TRACKER_TOKEN": KEY}}
OWNER = {"channel": "cli", "owner": True}


def ev(seq, type, entity=None, src="core", **data):
    return {"seq": seq, "ts": float(seq), "src": src, "type": type, "entity": entity, "data": data}


def events():
    tools = {"tracker": {"kind": "mcp", "server": SERVER, "description": "Tasks", "risk": []}}
    return [
        ev(1, "catalog.loaded", tools=tools),
        ev(2, "message.received", to="w1", text="hi", **OWNER),
        ev(3, "turn.tool", "w1", "host", id="t1", name="Bash", input={"command": f"export GH={GH}"}),
        ev(4, "core.snapshot", why="stop", state={"seq": 3, "catalog": tools, "sessions": {}}),
    ]


def write(path, evs):
    with open(path, "w", encoding="utf-8") as f:
        for e in evs:
            f.write(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n")


def raw(path):
    with open(path, "rb") as f:
        return f.read()


def secrets(text):
    return [s for s in (KEY, GH, SERVER["args"][2]) if s in text]


class Archives(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="scrub-")
        self.addCleanup(shutil.rmtree, self.state, True)
        self.archive = os.path.join(self.state, journal.ARCHIVE)
        os.makedirs(self.archive)
        self.old = os.path.join(self.archive, "journal-1-4.jsonl")
        write(self.old, events())
        self.raw = raw(self.old)

    def test_without_apply_it_only_counts(self):
        self.assertEqual(scrub.run(self.state), [(self.old, 3)])   # the catalog, a tool call, a snapshot
        self.assertEqual(raw(self.old), self.raw)
        self.assertFalse(os.path.exists(os.path.join(self.state, scrub.DONE)))

    def test_apply_removes_the_keys_and_keeps_every_event_in_its_place(self):
        scrub.run(self.state, apply=True)
        self.assertEqual(secrets(raw(self.old).decode()), [])
        before, after = events(), journal.read(self.old)
        self.assertEqual([(e["seq"], e["type"]) for e in after], [(e["seq"], e["type"]) for e in before])
        self.assertEqual(after[0]["data"]["tools"]["tracker"]["server"],
                         {"command": "npx", "args": ["mcp-tracker", "--token", redact.MASK]})
        self.assertEqual(self.raw.split(b"\n")[1], raw(self.old).split(b"\n")[1])   # a clean line — byte for byte
        if os.name == "posix":
            self.assertEqual(os.stat(self.old).st_mode & 0o077, 0)
        self.assertFalse(os.path.exists(os.path.join(self.state, "backup")))   # without --backup there is no copy

    def test_backup_keeps_the_original_aside(self):
        scrub.run(self.state, apply=True, backup=True)
        copies = [os.path.join(d, f) for d, _, fs in os.walk(os.path.join(self.state, "backup")) for f in fs]
        self.assertEqual(len(copies), 1)
        self.assertEqual(raw(copies[0]), self.raw)
        if os.name == "posix":
            self.assertEqual(os.stat(os.path.dirname(copies[0])).st_mode & 0o077, 0)

    def test_a_second_run_reads_nothing_and_an_archive_changed_since_is_cleaned_again(self):
        scrub.run(self.state, apply=True)
        with open(os.path.join(self.state, scrub.DONE), encoding="utf-8") as f:
            self.assertIn("journal-1-4.jsonl", json.load(f))
        self.assertEqual(scrub.run(self.state), [])
        # a rollback to the old code and back: the archive is rewritten with the key again — it is not in scrub.done in
        # this form
        write(self.old, events())
        self.assertEqual(scrub.run(self.state), [(self.old, 3)])
        scrub.run(self.state, apply=True)
        self.assertEqual(secrets(raw(self.old).decode()), [])
        self.assertEqual(scrub.count(self.old), 0)   # scrubbing again changes nothing

    def test_background_permission_requests_and_alerts_are_cleaned_and_questions_kept(self):
        ask = {"questions": [{"question": f"Take {GH}?", "options": []}]}
        pending = [{"request_id": "r9", "tool": "Bash", "input": {"command": f"GH={GH} make"}, "since": 1.0}]
        write(self.old, events() + [
            ev(5, "session.background", "w1", "host", id="b1", kind="bash", title=f"GH_TOKEN={GH} gh release"),
            ev(6, "session.background_output", "w1", "watch", id="b1", at=6.0, line=f"token {GH}"),
            ev(7, "permission.requested", "w1", "host", request_id="r1", tool="Bash", input={"command": f"x {GH}"}),
            ev(8, "permission.requested", "w1", "host", request_id="r2", tool="AskUserQuestion", input=ask),
            ev(9, "watch.alert", "w1", "watch", kind="background", reason=f"output: {GH}"),
            ev(10, "core.snapshot", why="stop", state={"seq": 9, "sessions": {"w1": {"pending": pending}}})])
        self.assertEqual(scrub.run(self.state), [(self.old, 8)])
        scrub.run(self.state, apply=True)
        cleaned = journal.read(self.old)
        self.assertEqual([e["seq"] for e in cleaned if GH in json.dumps(e, ensure_ascii=False)], [8])
        self.assertTrue(cleaned[6]["data"]["masked"])
        self.assertEqual(cleaned[7]["data"]["input"], ask)
        self.assertTrue(cleaned[9]["data"]["state"]["sessions"]["w1"]["pending"][0]["masked"])

    def test_the_start_of_a_turn_is_cleaned(self):
        # an earlier hook put the start of a turn into the journal as written: a notification about a background
        # command with a token in it
        head = f"<task-notification>\n<task-id>b1</task-id>\n<summary>curl -H 'PRIVATE-TOKEN: {GH}'</summary>"
        write(self.old, events() + [ev(5, "turn.started", "w1", "hook", marker=None, prompt_head=head,
                                       notice={"summary": f"curl -H 'PRIVATE-TOKEN: {GH}'", "ended": ["b1"]}),
                                    ev(6, "core.run_ended", "r1", code=0, tail=f"token {GH}")])
        self.assertEqual(scrub.run(self.state), [(self.old, 5)])
        scrub.run(self.state, apply=True)
        self.assertEqual(secrets(raw(self.old).decode()), [])
        self.assertEqual(journal.read(self.old)[4]["data"]["notice"]["ended"], ["b1"])

    def test_an_archive_without_keys_is_not_rewritten(self):
        clean = os.path.join(self.archive, "journal-5-6.jsonl")
        write(clean, [ev(5, "message.received", to="w1", text="x", **OWNER), ev(6, "turn.tool", "w1", name="Read",
                                                                                 input={"file_path": "/tmp/a"})])
        before = os.stat(clean)
        scrub.run(self.state, apply=True)
        after = os.stat(clean)
        self.assertEqual((after.st_ino, after.st_mtime_ns), (before.st_ino, before.st_mtime_ns))
        self.assertEqual([n for n in os.listdir(self.archive) if n.startswith(".")], [])   # no temporary files

    def test_without_room_for_the_new_file_the_archive_is_skipped_and_taken_next_time(self):
        short = []
        full = types.SimpleNamespace(total=1 << 30, used=1 << 30, free=1 << 10)
        with mock.patch.object(scrub.shutil, "disk_usage", return_value=full):
            scrub.run(self.state, apply=True, no_space=lambda path, need: short.append(path))
        self.assertEqual(short, [self.old])
        self.assertEqual(raw(self.old), self.raw)
        self.assertEqual(scrub.run(self.state), [(self.old, 3)])   # not recorded as scrubbed

    def test_lines_cut_off_by_a_repair_are_cleaned_too(self):
        cut = os.path.join(self.archive, "cut-1700000000000000000.jsonl")
        write(cut, events())
        self.assertEqual(sorted(scrub.run(self.state)), sorted([(self.old, 3), (cut, 3)]))
        scrub.run(self.state, apply=True)
        self.assertEqual(secrets(raw(cut).decode()), [])

    def test_the_pages_parse_of_a_cleaned_archive_goes_and_of_a_clean_one_stays(self):
        clean = os.path.join(self.archive, "journal-5-6.jsonl")
        write(clean, [ev(5, "message.received", to="w1", text="x", **OWNER), ev(6, "session.said", "w1", text="y")])
        sides = (history.CACHE, history.INDEX, shown.SIDE, usage.SIDECAR, history.OLD_INDEX)
        for path in (self.old, clean):
            for side in sides:
                with open(path + side, "w", encoding="utf-8") as f:
                    json.dump({"w1": [{"input": {"command": f"export GH={GH}"}}]}, f)
        scrub.run(self.state)
        self.assertTrue(os.path.exists(self.old + history.CACHE))   # counting touches nothing
        scrub.run(self.state, apply=True)
        self.assertEqual([os.path.exists(self.old + s) for s in sides], [False, False, False, False, True])
        self.assertTrue(all(os.path.exists(clean + s) for s in sides))

    def test_the_live_journal_is_not_rewritten_by_the_archive_pass(self):
        live = os.path.join(self.state, "journal.jsonl")
        write(live, events())
        scrub.run(self.state, apply=True)
        self.assertIn(KEY, raw(live).decode())   # hubd writes to it — the live journal is cleaned by rotation (rotate_live)


def old_journal():
    """An earlier hub's journal (no mask): a synthetic key in every type the current hub masks, in the forms that the
    reducer accepts and lays out into state."""
    tools = {"tracker": {"kind": "mcp", "server": SERVER, "description": "Tasks", "risk": []}}
    tok = lambda n: f"GH_TOKEN={GH} step{n}"  # noqa: E731
    return [
        ev(1, "catalog.loaded", tools=tools),
        ev(2, "session.requested", "a", cwd="/tmp", session_id="s-a", **OWNER),
        ev(3, "session.started", "a", "hook", session_id="s-a"),
        ev(4, "turn.started", "a", "hook", marker=None, prompt_head="release"),
        ev(5, "turn.tool", "a", "host", id="t1", name="Bash", input={"command": tok(5), "run_in_background": True}),
        ev(6, "turn.tool_result", "a", "host", id="t1", error=False, text=f"Command running in background with ID: b1 {GH}"),
        ev(7, "session.background", "a", "host", id="b1", kind="bash", title=tok(7), output="/tmp/b1.output"),
        ev(8, "session.background_output", "a", "watch", id="b1", at=8.0, line=tok(8), channel="cli", owner=True),
        ev(9, "session.background", "a", "host", tasks=[{"id": "b1", "kind": "bash", "title": tok(9)},
                                                        {"id": "b2", "kind": "agent", "title": tok(9)}]),
        ev(10, "permission.requested", "a", "host", request_id="r1", tool="Bash", input={"command": tok(10)},
           interactive=False, why=None),
        ev(11, "session.requested", "b", cwd="/tmp", session_id="s-b", **OWNER),
        ev(12, "session.started", "b", "hook", session_id="s-b"),
        ev(13, "turn.started", "b", "hook", marker=None, prompt_head="background"),
        ev(14, "session.background", "b", "host", id="c1", kind="bash", title=tok(14)),
        ev(15, "session.exited", "b", "hook", why="other"),   # a cut-off background task — titles in cut_turn
        ev(16, "message.received", None, to="a", text=f"verbatim {GH}", **OWNER),
    ]


def keys_outside_messages(state):
    st = {k: v for k, v in state.items() if k != "messages"}   # message texts are delivered verbatim — they are not touched
    return secrets(json.dumps(st, ensure_ascii=False))


class FoldedState(unittest.TestCase):
    """The field list of clean_state is not written by hand: a journal with the key in every masked type is reduced,
    and the whole state is searched. A new field found — add it to scrub.clean_state."""

    def test_every_type_reaches_the_state_and_the_folded_state_is_clean_after_scrub(self):
        st = reducer.empty()
        for e in old_journal():   # the reducer accepted every event: otherwise the key from it would not have reached the state
            st, _, why = reducer.reduce(st, e, "accepted")
            self.assertIsNone(why, (e["seq"], e["type"]))
        self.assertIn(GH, json.dumps(st["sessions"], ensure_ascii=False))   # the test sees that the key reached the state
        self.assertEqual(st["sessions"]["a"]["state"], "dialog")
        self.assertEqual(keys_outside_messages(scrub.clean_state(st) or st), [])
        self.assertTrue(scrub.clean_state(st)["sessions"]["a"]["pending"][0]["masked"])
        self.assertIsNone(scrub.clean_state(scrub.clean_state(st)))   # already scrubbed — unchanged

    def test_a_journal_cleaned_event_by_event_folds_to_a_clean_state(self):
        st = reducer.replay([scrub.clean(e) or e for e in old_journal()])
        self.assertEqual(keys_outside_messages(st), [])
        self.assertEqual(sorted(st["sessions"]["a"]["background"]), ["b1", "b2"])   # the same reduction, except for the mask

    def test_an_old_snapshot_is_cleaned_in_the_new_journal_and_its_rotate_start_snapshot(self):
        # the key is only in the snapshot (background and request); the source events are outside the carried-over tail
        state = tempfile.mkdtemp(prefix="scrub-rot-")
        self.addCleanup(shutil.rmtree, state, True)
        snap = reducer.replay(old_journal())
        snap = {k: v for k, v in snap.items() if not k.startswith("_")}
        evs = [ev(100, "core.snapshot", why="stop", state={**snap, "seq": 99})] + \
              [ev(100 + i, "session.said", "a", text=f"line {i}") for i in range(1, 6)]
        path = os.path.join(state, "journal.jsonl")
        write(path, evs)
        journal.rotate(state, journal.load_state(state), keep=2, per_session=0, scrub=True)
        self.assertEqual(keys_outside_messages(journal.load_state(state)), [])
        new = journal.read(path)
        snaps = [e for e in new if e["type"] == "core.snapshot"]
        self.assertEqual([e["data"]["why"] for e in snaps], ["rotate-start", "rotate"])
        for e in snaps:
            self.assertEqual(keys_outside_messages(e["data"]["state"]), [])
        archived = os.path.join(state, journal.ARCHIVE, "journal-100-105.jsonl")
        self.assertIn(GH, raw(archived).decode())   # the archive is as it was; scrub.run cleans it
        scrub.run(state, apply=True)
        self.assertEqual(secrets(raw(archived).decode().replace(f"verbatim {GH}", "")), [])

    def test_an_ordinary_rotation_leaves_events_as_they_were(self):
        # scrubbing only by the command: an ordinary rotation (by size) carries the tail over as is
        state = tempfile.mkdtemp(prefix="scrub-rot-")
        self.addCleanup(shutil.rmtree, state, True)
        write(os.path.join(state, "journal.jsonl"), old_journal())
        journal.rotate(state, journal.load_state(state))
        self.assertIn(f"GH_TOKEN={GH} step5", raw(os.path.join(state, "journal.jsonl")).decode())


class LiveJournal(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="scrub-live-")
        self.addCleanup(shutil.rmtree, self.state, True)
        self.path = os.path.join(self.state, "journal.jsonl")
        write(self.path, old_journal())
        journal.save_state(self.state, reducer.replay(old_journal()))
        os.makedirs(os.path.join(self.state, "cache"))
        for name in (tail.CACHE, tail.SESSION_CACHE):   # hub show's parse: the owner's and the sessions'
            with open(os.path.join(self.state, name), "wb") as f:
                f.write(GH.encode())
        for name in (reader_cache.STORE, os.path.join("cache", ".web-reader.cut")):   # the page's reader checkpoint
            with open(os.path.join(self.state, name), "wb") as f:
                f.write(GH.encode())

    def test_without_hubd_the_command_rotates_it_under_hubds_lock(self):
        self.assertEqual(scrub.live(self.state), (8, True))   # all old_journal events with the key, except the message
        rotated, archived = scrub.rotate_live(self.state, ask=lambda: self.fail("hubd is not running — do not ask"))
        self.assertEqual((rotated, archived), (True, ["journal-1-16.jsonl"]))
        self.assertEqual(scrub.live(self.state), (0, False))
        self.assertEqual(secrets(raw(self.path).decode().replace(f"verbatim {GH}", "")), [])
        self.assertFalse(os.path.exists(os.path.join(self.state, tail.CACHE)))
        self.assertEqual(os.listdir(os.path.join(self.state, "cache")), [])   # and the web reader's checkpoint with its temp file
        scrub.run(self.state, apply=True)
        for path in scrub.archives(self.state):
            self.assertEqual(secrets(raw(path).decode().replace(f"verbatim {GH}", "")), [])

    def hubd(self):
        held = lock.acquire(os.path.join(self.state, "hubd.lock"))   # "hubd is running"
        self.addCleanup(held.close)
        host = StreamHost(self.state, claude=("claude-not-run",))
        self.answers = []
        host.spawn = lambda *a, **kw: "spawned"
        host.alive, host.sessions, host.info = (lambda n: True), (lambda: ["a"]), (lambda n: {"pid": 1, "pane": None})
        host.respond = lambda name, request_id, response, **_: self.answers.append(response) or "answered"
        return Hub(self.state, host, registry_dir=os.path.join(self.state, "registry"))

    def test_with_hubd_running_it_asks_hubd_and_waits_for_the_new_journal(self):
        h = self.hubd()

        def ask():
            spool.put(h.spool_dir, "journal.scrub_requested", None, OWNER)
            h.tick()
            return True
        rotated, archived = scrub.rotate_live(self.state, ask, timeout=5)
        self.assertTrue(rotated)
        (name,) = archived
        self.assertIn(os.path.join(self.state, journal.ARCHIVE, name), journal.archives(self.state))
        self.assertEqual(keys_outside_messages(h.state), [])
        self.assertEqual(scrub.live(self.state), (0, False))
        # a request written by the earlier hub without a mask awaits an answer: "Allow" executes it as it was
        (req,) = h.state["sessions"]["a"]["pending"]
        self.assertTrue(req["masked"])
        spool.put(h.spool_dir, "permission.answered", "a", {"request_id": "r1", "behavior": "allow", **OWNER})
        h.tick()
        self.assertEqual(self.answers[-1]["updatedInput"]["command"], f"GH_TOKEN={GH} step10")

    def test_an_ordinary_rotation_in_between_is_not_taken_for_the_cleaning_one(self):
        # between the request and the hubd tick the journal was rotated by size: the new file carries a tail with
        # keys — keep waiting
        h = self.hubd()
        calls = []

        def ask():
            journal.rotate(self.state, journal.load_state(self.state))   # an ordinary one, no scrubbing
            h.journal = journal.Journal(self.path)
            spool.put(h.spool_dir, "journal.scrub_requested", None, OWNER)
            return True

        def sleep(_):
            calls.append(1)
            if len(calls) == 2:
                h.tick()   # hubd got to the request
        rotated, archived = scrub.rotate_live(self.state, ask, timeout=60, sleep=sleep)
        self.assertTrue(rotated)
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(scrub.live(self.state)[0], 0)
        self.assertEqual(len(archived), 2)   # both the ordinary and the scrubbing rotation — rewriting both waits for SETTLE
        self.assertEqual(archived[0], "journal-1-16.jsonl")

    def test_the_temp_file_goes_even_when_closing_it_fails_on_a_full_disk(self):
        tmp_dir = os.path.join(self.state, journal.ARCHIVE)
        os.makedirs(tmp_dir, exist_ok=True)
        write(os.path.join(tmp_dir, "journal-1-16.jsonl"), old_journal())
        real = scrub.journal._line
        with mock.patch.object(scrub.journal, "_line", side_effect=[real(old_journal()[0]), OSError(28, "No space")]):
            with self.assertRaises(OSError):
                scrub.rewrite(os.path.join(tmp_dir, "journal-1-16.jsonl"))
        self.assertEqual([n for n in os.listdir(tmp_dir) if n.startswith(".")], [])

    def test_a_rotation_seen_before_the_time_ran_out_still_waits_for_the_readers(self):
        h = self.hubd()
        t = [0.0]

        def ask():   # rotation by size — a tail with keys; a scrubbing rotation never comes
            journal.rotate(self.state, journal.load_state(self.state))
            h.journal = journal.Journal(self.path)
            return True

        def sleep(s):
            t[0] += s
        self.assertEqual(scrub.rotate_live(self.state, ask, timeout=3, clock=lambda: t[0], sleep=sleep),
                         (False, ["journal-1-16.jsonl"]))

    def test_a_refusal_is_a_failure_at_once(self):
        self.hubd()
        self.assertEqual(scrub.rotate_live(self.state, ask=lambda: False), (False, []))

    def test_the_archive_just_rotated_is_rewritten_only_after_the_readers_had_time(self):
        scrub.rotate_live(self.state, ask=None)
        waits = []
        scrub.run(self.state, apply=True, settle={"journal-1-16.jsonl": 1030.0}, now=lambda: 1000.0, sleep=waits.append)
        self.assertEqual(waits, [30.0])

    def test_a_session_cannot_ask_hubd_to_rotate(self):
        st = reducer.replay(old_journal())
        _, _, why = reducer.reduce(st, ev(99, "journal.scrub_requested", src="session", channel="session",
                                          owner=False, sender="a"), "accepted")
        self.assertTrue(why)

    def test_hubd_that_does_not_rotate_in_time_is_a_failure_not_a_hang(self):
        held = lock.acquire(os.path.join(self.state, "hubd.lock"))
        self.addCleanup(held.close)
        t = [0.0]

        def sleep(s):
            t[0] += s
        self.assertEqual(scrub.rotate_live(self.state, ask=lambda: True, timeout=3, clock=lambda: t[0], sleep=sleep),
                         (False, []))
        self.assertGreaterEqual(t[0], 3)


class Command(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="scrub-cli-")
        self.addCleanup(shutil.rmtree, self.state, True)
        os.makedirs(os.path.join(self.state, journal.ARCHIVE))
        self.old = os.path.join(self.state, journal.ARCHIVE, "journal-1-4.jsonl")
        write(self.old, events())
        self.raw = raw(self.old)

    def hub(self, *args, session=None):
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE")}
        env.update(HUB_STATE_DIR=self.state, **({"HUB_SESSION": session} if session else {}))
        return subprocess.run([sys.executable, os.path.join(ROOT, "bin", "hub"), "journal", "scrub", *args], env=env,
                              capture_output=True, text=True, timeout=120)

    def test_a_session_cannot_run_it(self):
        r = self.hub("--apply", session="w1")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(raw(self.old), self.raw)

    def test_the_owner_counts_then_cleans_from_a_terminal(self):
        r = self.hub()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("journal-1-4.jsonl", r.stdout)
        self.assertEqual(secrets(r.stdout + r.stderr), [])
        self.assertEqual(raw(self.old), self.raw)
        r = self.hub("--apply", "--backup")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(secrets(raw(self.old).decode()), [])
        self.assertTrue(os.path.isdir(os.path.join(self.state, "backup")))
        self.assertIn("no keys in the journals", self.hub().stdout)


if __name__ == "__main__":
    unittest.main()
