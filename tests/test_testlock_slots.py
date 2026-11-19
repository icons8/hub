"""The full-run lock with several slots (core/testlock): by default one run per machine, as before; test-lock.json in
the settings folder allows up to N at once while the machine has free memory and a low load."""
import json
import os
import tempfile
import unittest
from unittest import mock

from core import testlock


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="slots-")
        self.path = os.path.join(self.dir, "full.lock")
        self.held = []
        self.slept = []

    def tearDown(self):
        for f in self.held:
            f.close()

    def turn(self, cfg, mem=4000, load=1.0, patience=0, settle=0):
        f = testlock.wait_turn(path=self.path, patience=patience, every=10, sleep=self.slept.append, config=cfg,
                               probe=lambda: (mem, load), settle=settle)
        self.held.append(f)
        return f

    def cfg(self, **kw):
        return {"slots": 2, "min_free_mb": 2000, "max_load": 6, **kw}


class Slots(Case):
    def test_without_settings_the_second_run_waits_as_before(self):
        self.turn(None)
        with self.assertRaises(testlock.Busy):
            self.turn(None)
        self.assertEqual(self.slept, [])

    def test_one_slot_is_the_old_single_lock_even_with_free_memory(self):
        self.turn(self.cfg(slots=1))
        with self.assertRaises(testlock.Busy):
            self.turn(self.cfg(slots=1))

    def test_two_slots_and_free_memory_let_both_runs_go(self):
        self.turn(self.cfg())
        self.turn(self.cfg())
        with self.assertRaises(testlock.Busy):   # there is no third slot
            self.turn(self.cfg())

    def test_not_enough_memory_keeps_the_second_run_waiting(self):
        self.turn(self.cfg())
        with self.assertRaises(testlock.Busy):
            self.turn(self.cfg(), mem=1500)
        with self.assertRaises(testlock.Busy):
            self.turn(self.cfg(), mem=None)   # memory unknown: treated as busy

    def test_high_load_keeps_the_second_run_waiting(self):
        self.turn(self.cfg())
        with self.assertRaises(testlock.Busy):
            self.turn(self.cfg(), load=9.5)
        with self.assertRaises(testlock.Busy):
            self.turn(self.cfg(), load=None)

    def test_a_waiting_run_takes_the_extra_slot_once_resources_appear(self):
        self.turn(self.cfg())
        state = iter([(1000, 1.0), (1000, 1.0), (3000, 1.0)])
        f = testlock.wait_turn(path=self.path, every=10, sleep=self.slept.append, config=self.cfg(),
                               probe=lambda: next(state), settle=0)
        self.held.append(f)
        self.assertEqual(self.slept, [10, 10])

    def test_the_first_slot_is_taken_back_when_it_is_released(self):
        a = self.turn(self.cfg())
        self.turn(self.cfg())
        a.close()
        self.turn(self.cfg(), mem=0)   # slot 0 is free: memory is not asked about, as with a single lock

    def test_a_grant_settles_before_the_next_one(self):
        # the second run has not yet started taking memory: the third must not decide on the same numbers
        cfg = self.cfg(slots=3)
        self.turn(cfg, settle=3600)
        self.turn(cfg, settle=3600)
        with self.assertRaises(testlock.Busy):
            self.turn(cfg, settle=3600)
        self.turn(cfg, settle=0)

    def test_free_sees_any_free_slot(self):
        cfg = self.cfg()
        self.turn(cfg)
        self.assertTrue(testlock.free(self.path, config=cfg))
        self.turn(cfg)
        self.assertFalse(testlock.free(self.path, config=cfg))
        self.assertTrue(testlock.free(self.path, config=None) is False)


class Settings(Case):
    def write(self, body):
        with open(os.path.join(self.dir, "test-lock.json"), "w") as f:
            f.write(body if isinstance(body, str) else json.dumps(body))

    def test_no_file_means_off(self):
        self.assertIsNone(testlock.settings(self.dir))

    def test_the_file_gives_slots_and_thresholds(self):
        self.write({"slots": 3, "min_free_mb": 4096, "max_load": 8})
        self.assertEqual(testlock.settings(self.dir), {"slots": 3, "min_free_mb": 4096, "max_load": 8})

    def test_a_broken_or_partial_file_means_off(self):
        for bad in ("{", "[]", '{"slots": 2}', '{"slots": "x", "min_free_mb": 1, "max_load": 1}',
                    '{"slots": 1, "min_free_mb": 1, "max_load": 1}'):
            self.write(bad)
            self.assertIsNone(testlock.settings(self.dir), bad)

    def test_the_default_machine_lock_reads_the_settings_and_an_explicit_path_does_not(self):
        self.write({"slots": 2, "min_free_mb": 1, "max_load": 1000})
        with mock.patch.object(testlock, "PATH", self.path), mock.patch.dict(os.environ, {"HUB_CONFIG_DIR": self.dir}):
            a = testlock.wait_turn(patience=0, sleep=self.slept.append, probe=lambda: (10 ** 6, 0.0), settle=0)
            b = testlock.wait_turn(patience=0, sleep=self.slept.append, probe=lambda: (10 ** 6, 0.0), settle=0)
            self.held += [a, b]
            with self.assertRaises(testlock.Busy):
                testlock.wait_turn(path=self.path, patience=0, sleep=self.slept.append)   # its own path: a single lock


class Probe(unittest.TestCase):
    def test_free_memory_is_a_number_here(self):
        mb = testlock.free_mb()
        self.assertTrue(mb is None or mb > 0)

    def test_macos_counts_free_inactive_and_speculative_pages(self):
        out = ("Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free: 1000.\n"
               "Pages active: 5.\nPages inactive: 2000.\nPages speculative: 24.\n")
        with mock.patch("builtins.open", side_effect=OSError), mock.patch.object(testlock.shutil, "which", return_value="/usr/bin/vm_stat"), \
                mock.patch.object(testlock.subprocess, "run") as run:
            run.return_value = mock.Mock(stdout=out)
            self.assertAlmostEqual(testlock.free_mb(), 3024 * 16384 / 1048576)


if __name__ == "__main__":
    unittest.main()
