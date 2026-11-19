"""Waiting in tests: for the event, not for a pause. `until(pred)` ends as soon as the condition holds; its timeout is a
fuse against a hang, not the time the test takes. HUB_TEST_SLOW multiplies every fuse that goes through here (a slow
machine or Windows: 2–3; a probe for tests that live on a long pause: 0.1 — such a test goes red, one waiting for an
event stays green). The tests' own `until` copies move here one by one.

    from tests import waiting
    self.assertTrue(waiting.until(lambda: os.path.exists(path), 20), "the file did not appear")
"""
import math
import os
import time

SLOW_ENV = "HUB_TEST_SLOW"


def slow():
    """The multiplier from HUB_TEST_SLOW: a positive finite number, anything else — 1."""
    try:
        k = float(os.environ.get(SLOW_ENV) or 1)
    except ValueError:
        return 1.0
    return k if math.isfinite(k) and k > 0 else 1.0


def seconds(timeout):
    """A fuse for this machine: the timeout times HUB_TEST_SLOW."""
    return timeout * slow()


def until(pred, timeout=20.0, step=0.05, clock=None, sleep=None):
    """Call pred until it gives something true; returns that value, or the last false one once the fuse burns out.
    The clock and sleep are looked up at the call: a test that patches time.sleep still reaches this wait."""
    clock, sleep = clock or time.monotonic, sleep or time.sleep
    end = clock() + seconds(timeout)
    while True:
        got = pred()
        if got or clock() >= end:
            return got
        sleep(step)
