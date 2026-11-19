"""The in-memory temp folder of a test run (core/testtmp, 17.10: test runs were writing to the server disk): only
tmpfs with headroom for every lock slot, only our own folder inside the shared /dev/shm, and folders of finished runs
are cleaned up."""
import tests  # noqa: F401
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from core import lock, testtmp
from tests import winsafe


@unittest.skipIf(os.name == "nt", "POSIX only: uid, folder permissions; on Windows make() is None by /proc/mounts")
class InMemory(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.mounts = os.path.join(tempfile.mkdtemp(), "mounts")
        with open(self.mounts, "w") as f:
            f.write(f"proc /proc proc rw 0 0\ntmpfs {self.root} tmpfs rw,nosuid,nodev 0 0\n")
        self.addCleanup(shutil.rmtree, self.root, True)

    def make(self, slots=1, free=10_000, mounts=None):
        got = testtmp.make(slots, root=self.root, mounts=mounts or self.mounts, free_mb=lambda _: free)
        if got:
            self.addCleanup(got[1].close)
        return got

    def test_a_run_folder_in_its_own_closed_folder_with_a_held_lock(self):
        run, held = self.make()
        home = os.path.join(self.root, f"hub-tests-{os.getuid()}")
        self.assertEqual(os.path.dirname(run), home)
        self.assertEqual(os.path.basename(run), str(os.getpid()))
        self.assertEqual(os.stat(home).st_mode & 0o777, 0o700)
        self.assertIsNone(lock.acquire(os.path.join(run, testtmp.LOCK)))   # held by this run

    def test_as_before_without_proc_mounts_tmpfs_or_room(self):
        self.assertIsNone(self.make(mounts=os.path.join(self.root, "none")))   # macOS, Windows: there is no /proc/mounts
        self.assertIsNone(self.make(free=testtmp.PER_RUN_MB - 1))
        self.assertIsNone(self.make(slots=3, free=testtmp.PER_RUN_MB * 3 - 1))   # headroom for every lock slot
        self.assertTrue(self.make(slots=3, free=testtmp.PER_RUN_MB * 3))
        with open(self.mounts, "w") as f:
            f.write(f"/dev/sda1 {self.root} ext4 rw 0 0\n")
        self.assertIsNone(self.make())

    def test_a_foreign_or_open_folder_or_a_link_is_refused(self):
        home = os.path.join(self.root, f"hub-tests-{os.getuid()}")
        os.mkdir(home, 0o755)
        os.chmod(home, 0o755)
        self.assertIsNone(self.make())                     # visible to others: could have been planted
        os.rmdir(home)
        winsafe.symlink(self, tempfile.mkdtemp(), home)
        self.assertIsNone(self.make())                     # a link into someone else's place

    def test_gone_runs_are_swept_live_and_new_ones_kept(self):
        home = os.path.join(self.root, f"hub-tests-{os.getuid()}")
        os.mkdir(home, 0o700)
        names = ("1-dead", "2-live", "3-new", "4-old", "5-just-killed")
        dead, live, new, old, killed = (os.path.join(home, n) for n in names)
        for d in (dead, live, new, old, killed):
            os.mkdir(d)
        for d in (dead, killed):
            open(os.path.join(d, testtmp.LOCK), "w").close()          # the holder is gone
        open(os.path.join(dead, "journal.jsonl"), "w").close()
        held = lock.acquire(os.path.join(live, testtmp.LOCK))
        self.addCleanup(held.close)
        past = time.time() - 2 * testtmp.YOUNG
        for d in (dead, live, old):
            os.utime(d, (past, past))
        stopped = []
        with mock.patch.object(testtmp, "_stop_chromes", stopped.append):
            run, mine = self.make()
        self.assertEqual(sorted(os.listdir(home)), sorted(["2-live", "3-new", "5-just-killed", os.path.basename(run)]))
        # Chrome: for everyone without a holder, and for one just killed (the next run removes its folder)
        self.assertEqual(sorted(stopped), sorted([dead, new, old, killed]))

    def test_sweep_leaves_a_foreign_open_or_linked_home_alone(self):
        # release review: the update calls sweep(top()) without make(); /dev/shm is shared, so a planted link to a
        # foreign folder would otherwise lead rmtree through its subfolders
        victim = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, victim, True)
        os.mkdir(os.path.join(victim, "data"))
        past = time.time() - 2 * testtmp.YOUNG
        os.utime(os.path.join(victim, "data"), (past, past))
        home = os.path.join(self.root, "h")
        winsafe.symlink(self, victim, home)
        stopped = []
        with mock.patch.object(testtmp, "_stop_chromes", stopped.append):
            testtmp.sweep(home)
            self.assertTrue(os.path.isdir(os.path.join(victim, "data")))
            os.remove(home)
            os.mkdir(home, 0o700)
            os.chmod(home, 0o755)
            os.mkdir(os.path.join(home, "9"))
            os.utime(os.path.join(home, "9"), (past, past))
            testtmp.sweep(home)                            # visible to others: folders could have been planted
            self.assertTrue(os.path.isdir(os.path.join(home, "9")))
            self.assertEqual(stopped, [])                  # and it does not kill Chrome in a foreign folder
            os.chmod(home, 0o700)
            testtmp.sweep(home)                            # our own closed one: removed, as before
            self.assertFalse(os.path.exists(os.path.join(home, "9")))

    def test_a_folder_removed_by_another_sweeper_is_skipped(self):
        # review: two cleaners at once; open(lock, "a+") in a folder the other had removed raised FileNotFoundError
        home = os.path.join(self.root, "h")
        os.mkdir(home, 0o700)
        os.mkdir(os.path.join(home, "8"))
        open(os.path.join(home, "8", testtmp.LOCK), "w").close()

        def gone(path):
            raise FileNotFoundError(path)   # another cleaner managed to delete the folder
        with mock.patch.object(testtmp.lock, "acquire", gone):
            testtmp.sweep(home)

    def test_a_run_being_created_is_not_removed(self):
        # review: between mkdir and the lock a foreign cleaner took the free lock and deleted the new folder
        home = os.path.join(self.root, "h")
        os.mkdir(home, 0o700)
        os.mkdir(os.path.join(home, "7"))
        open(os.path.join(home, "7", testtmp.LOCK), "w").close()
        with mock.patch.object(testtmp, "_stop_chromes", lambda run: None):
            testtmp.sweep(home)
        self.assertTrue(os.path.isdir(os.path.join(home, "7")))

    def test_chrome_socket_path_fits_under_the_run_folder(self):
        # 17.10: Chrome's SingletonSocket lives in TMPDIR/hub-core-tests-run-*/com.google.Chrome.*/, socket path up to 107 bytes
        run = os.path.join(testtmp.top(), "4194304")   # the longest Linux pid
        sock = os.path.join(run, "hub-core-tests-run-abcdefgh", "com.google.Chrome.ABCDEF", "SingletonSocket")
        self.assertLessEqual(len(sock.encode()), 107 - 4, sock)   # headroom for a longer uid

    def test_remove_takes_the_whole_run_folder(self):
        run, held = self.make()
        os.makedirs(os.path.join(run, "hub-core-tests-run-x", "chrome"))
        testtmp.remove(run, held)
        self.assertFalse(os.path.exists(run))


if __name__ == "__main__":
    unittest.main()
