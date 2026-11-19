"""A fair queue for the full-run lock (core/testlock): a waiter takes a ticket, the slot goes to the earliest live
ticket — on 16.10, with ~20 executors, a run that came earlier lost the polling race to newer ones for an hour."""
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import platforms
from core import lock, testlock


class Queue(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="queue-")
        self.path = os.path.join(self.dir, "full.lock")
        self.queue = self.path + ".queue"
        self.held = []

    def tearDown(self):
        for f in self.held:
            f.close()

    def tickets(self):
        return sorted(os.listdir(self.queue)) if os.path.isdir(self.queue) else []

    def ticket(self, pid, stamp=1, hold=True):
        """Another's ticket in the queue: hold means its process is alive and holds the ticket's lock."""
        os.makedirs(self.queue, exist_ok=True)
        name = os.path.join(self.queue, f"{stamp:020d}-{pid}-x")
        f = open(name, "a+")
        if hold:
            platforms.current.try_lock(f)
            self.held.append(f)
        else:
            f.close()
        return name

    def test_the_one_who_came_first_gets_the_slot_first(self):
        busy = lock.acquire(self.path)
        a_waits, a_go, got = threading.Event(), threading.Event(), []

        def a_sleep(_):
            a_waits.set()
            a_go.wait(5)

        a = threading.Thread(target=lambda: got.append(testlock.wait_turn(path=self.path, sleep=a_sleep)))
        a.start()
        self.assertTrue(a_waits.wait(5))
        said = []

        def b_sleep(_):
            busy.close()   # the slot is freed while the first still sleeps — the second does not take it
        with self.assertRaises(testlock.Busy):
            testlock.wait_turn(path=self.path, sleep=b_sleep, patience=30, every=10, waiting=said.append)
        self.assertEqual(said, [1])             # "1 run ahead of you"
        a_go.set()
        a.join(5)
        self.assertFalse(a.is_alive())
        self.held.append(got[0])
        self.assertEqual(self.tickets(), [])    # both tickets are gone: the one that got the slot and the one that gave up

    def test_waiting_says_how_many_runs_are_ahead_and_again_when_it_changes(self):
        first = self.ticket(os.getpid(), stamp=1)
        self.ticket(os.getpid(), stamp=2)
        said, n = [], [0]

        def sleep(_):
            n[0] += 1
            if n[0] == 2:
                self.held[0].close()   # the first one left
                os.remove(first)
        with self.assertRaises(testlock.Busy):
            testlock.wait_turn(path=self.path, sleep=sleep, patience=30, every=10, waiting=said.append)
        self.assertEqual(said, [2, 1])

    def test_a_dead_waiter_does_not_hold_the_queue(self):
        p = subprocess.Popen([sys.executable, "-c", ""])
        p.wait()
        dead = self.ticket(p.pid, hold=False)
        testlock.wait_turn(path=self.path, patience=0).close()
        self.assertFalse(os.path.exists(dead))

    def test_a_ticket_nobody_holds_is_dead_even_if_its_pid_is_reused(self):
        stale = self.ticket(os.getpid(), hold=False)   # the pid is taken again, but nobody holds the ticket
        testlock.wait_turn(path=self.path, patience=0).close()
        self.assertFalse(os.path.exists(stale))

    def test_liveness_is_asked_from_platforms(self):
        self.ticket(12345)   # the lock is held, but there is no process
        with mock.patch.object(platforms.current, "alive", return_value=False) as alive:
            testlock.wait_turn(path=self.path, patience=0).close()
        alive.assert_called_with(12345)

    def test_a_live_waiter_ahead_keeps_the_free_slot_for_itself(self):
        self.ticket(os.getpid())
        with self.assertRaises(testlock.Busy):
            testlock.wait_turn(path=self.path, patience=0)
        self.assertEqual(len(self.tickets()), 1)   # the one that gave up removed its own ticket

    def test_a_dead_ticket_that_windows_will_not_delete_is_skipped(self):
        # Windows: a file open in another process (antivirus, a handle not yet closed) cannot be deleted — PermissionError
        dead = self.ticket(os.getpid(), hold=False)
        real = os.remove

        def remove(p):
            if p == dead:
                raise PermissionError(13, "in use")
            real(p)
        with mock.patch.object(testlock.os, "remove", side_effect=remove):
            testlock.wait_turn(path=self.path, patience=0).close()
        self.assertEqual(self.tickets(), [os.path.basename(dead)])

    def test_the_own_ticket_is_closed_before_it_is_removed(self):
        # Windows: an open file cannot be deleted — close one's own ticket first, then delete it
        closed, real_open, real_remove = [], open, os.remove
        handles = {}

        def opener(p, *a, **k):
            f = real_open(p, *a, **k)
            if os.path.dirname(p) == self.queue:
                handles[p] = f
            return f

        def remove(p):
            if p in handles:
                closed.append(handles[p].closed)
            real_remove(p)
        with mock.patch("builtins.open", side_effect=opener), mock.patch.object(testlock.os, "remove", side_effect=remove):
            testlock.wait_turn(path=self.path, patience=0).close()
        self.assertEqual(closed, [True])

    def test_tickets_are_numbered_in_arrival_order_even_on_a_coarse_clock(self):
        # Windows: time.time() ticks in ~15.6 ms steps — two in one tick still line up in arrival order
        later = self.ticket(os.getpid(), stamp=10 ** 19)   # the clock is "behind" the ticket already in the queue
        busy = lock.acquire(self.path)
        names = []
        with self.assertRaises(testlock.Busy):
            testlock.wait_turn(path=self.path, patience=10, every=10, sleep=lambda _: names.extend(self.tickets()))
        busy.close()
        self.assertEqual(len(names), 2)
        self.assertEqual(names[0], os.path.basename(later))   # its own ticket comes after the one already there

    def test_one_slot_without_settings_is_still_a_fair_queue(self):
        self.ticket(os.getpid())
        with self.assertRaises(testlock.Busy):
            testlock.wait_turn(path=self.path, patience=0, config=None)


if __name__ == "__main__":
    unittest.main()
