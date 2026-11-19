"""Migration to executors on other people's hubs (owner decision 2026-10-04: automatic, plus a rollback command): after an
update the update module runs hub migrate once; hub migrate --rollback restores the journal from the backup on hub restart."""
import tests  # noqa: F401 — keep the machine's settings out of the test
import json
import os
import subprocess
import sys
import tempfile
import unittest

from core import journal, migrate

HUB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")
OWNER = {"channel": "cli", "owner": True}


def hub(state, *args, me=None):
    env = {**os.environ, "HUB_STATE_DIR": state, "HUB_CLI_WAIT": "0"}
    env.pop("HUB_SESSION", None)
    if me:
        env["HUB_SESSION"] = me
    return subprocess.run([sys.executable, HUB, *args], env=env, capture_output=True, text=True)


class AfterUpdate(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="auto-")

    def test_runs_after_an_update_and_once_on_a_hub_that_never_migrated(self):
        # 10-04, another person's hub: the update was merged by hand (git), the update module did not set the flag —
        # no migration happened. A hub that never migrated (no migrated file) migrates on the first start of the new code
        from modules.update import update
        calls = []
        run = lambda state, args: calls.append((state, args)) or (0, "ok")  # noqa: E731
        update.post_update(self.state, run=run)
        self.assertEqual(calls, [(self.state, ["migrate", "--auto"])])
        open(os.path.join(self.state, migrate.DONE), "w").close()        # the migration happened (hub migrate --auto writes it)
        update.post_update(self.state, run=run)
        self.assertEqual(len(calls), 1)
        open(os.path.join(self.state, update.MIGRATE_FLAG), "w").close()
        update.post_update(self.state, run=run)
        update.post_update(self.state, run=run)
        self.assertEqual(len(calls), 2)                                   # after an update — once more, just one
        self.assertFalse(os.path.exists(os.path.join(self.state, update.MIGRATE_FLAG)))

    def test_the_command_goes_to_this_hub_not_the_one_in_the_environment(self):
        # check 10-04: a command without a state folder went to the hub from the environment (a live one) — the migration and the question to the owner happened there
        from modules.update import update
        other = os.environ["HUB_STATE_DIR"]
        code, out = update.hub_cli(self.state, ["migrate", "--auto"])
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.exists(os.path.join(self.state, migrate.DONE)))
        self.assertFalse(os.path.exists(os.path.join(other, migrate.DONE)))

    def test_auto_migrate_happens_once_per_hub(self):
        j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        j.append("session.requested", "hub", {"cwd": "/h", "session_id": "s-hub", "kind": "hub", **OWNER})
        j.append("session.requested", "stale", {"cwd": "/w", "session_id": "s-stale", **OWNER})
        r = hub(self.state, "migrate", "--auto")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("stale", r.stdout)
        again = hub(self.state, "migrate", "--auto")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("already", again.stdout)                       # the hub must not ask the owner after every update


class Rollback(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="back-")
        self.j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        self.j.append("session.requested", "hub", {"cwd": "/h", "session_id": "s-hub", "kind": "hub", **OWNER})
        self.j.append("session.requested", "fixer", {"cwd": "/w", "session_id": "s-fixer", **OWNER})
        self.j.append("delegation.created", None, {"session": "fixer", "context": "x", **OWNER})
        with open(os.path.join(self.state, "journal.jsonl"), "rb") as f:
            self.before = f.read()
        self.assertEqual(hub(self.state, "migrate").returncode, 0)
        self.j.append("message.received", None, {"to": "hub", "text": "after the switch", **OWNER})

    def test_rollback_asks_a_restart_and_the_restart_restores_the_backup(self):
        r = hub(self.state, "migrate", "--rollback")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("after the migration", r.stdout)                         # warns: events after the migration will be lost
        with open(os.path.join(self.state, "restart")) as f:
            self.assertEqual(json.load(f), {"parts": []})
        with open(os.path.join(self.state, "journal.jsonl"), "rb") as f:
            self.assertNotEqual(f.read(), self.before)           # until the hub restarts the journal is untouched
        migrate.apply_rollback(self.state)                       # hub-up between the stop and the start
        with open(os.path.join(self.state, "journal.jsonl"), "rb") as f:
            self.assertEqual(f.read(), self.before)
        self.assertFalse(os.path.exists(os.path.join(self.state, migrate.ROLLBACK)))
        kept = [d for d in os.listdir(os.path.join(self.state, "backup")) if d.endswith("before-rollback")]
        self.assertEqual(len(kept), 1)                           # what was there is not lost
        self.assertFalse(migrate.apply_rollback(self.state))     # second time — nothing to do

    def test_a_later_scrub_or_compaction_backup_is_not_the_backup_of_the_migration(self):
        # `hub journal scrub --backup` (backup/scrub-<time>) and `hub journal compact --backup` (backup/compact-<time>)
        # sort after the digits of the migration's <date>-<time>: the bare rollback took the archives' copy and found no journal
        root = os.path.join(self.state, "backup")
        migration = sorted(os.listdir(root))
        self.assertEqual(len(migration), 1)
        for name in ("scrub-20990101-000000", "compact-20990101-000000", "20990102-000000-before-rollback"):
            os.makedirs(os.path.join(root, name))
            with open(os.path.join(root, name, "journal-archive.1.jsonl"), "w") as f:
                f.write("{}\n")
        self.assertEqual(os.path.basename(migrate.latest_backup(self.state)), migration[0])
        r = hub(self.state, "migrate", "--rollback")
        self.assertEqual(r.returncode, 0, r.stderr)
        migrate.apply_rollback(self.state)
        with open(os.path.join(self.state, "journal.jsonl"), "rb") as f:
            self.assertEqual(f.read(), self.before)

    def test_only_the_owner_rolls_back(self):
        r = hub(self.state, "migrate", "--rollback", me="fixer")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.state, migrate.ROLLBACK)))

    def test_no_backup_no_rollback(self):
        empty = tempfile.mkdtemp(prefix="back0-")
        r = hub(empty, "migrate", "--rollback")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no backup", r.stderr)


if __name__ == "__main__":
    unittest.main()
