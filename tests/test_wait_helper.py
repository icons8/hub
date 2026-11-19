"""tests/waiting.py: the tests' one wait for an event — it ends when the condition holds, the timeout is a fuse, and
HUB_TEST_SLOW stretches every fuse on a slow machine (and shrinks it for a probe: a test living on a long pause goes red)."""
import tests  # noqa: F401 — keep the machine's settings out
import unittest
from unittest import mock

from tests import waiting


class Clock:
    """Time that moves only by sleep: the fuse is checked without waiting for it."""
    def __init__(self):
        self.now, self.slept = 1000.0, []

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += s


class Until(unittest.TestCase):
    def until(self, c, pred, timeout, step=0.05):
        return waiting.until(pred, timeout, step, clock=c.monotonic, sleep=c.sleep)

    def test_a_condition_that_holds_ends_the_wait_at_once_with_its_value(self):
        c = Clock()
        self.assertEqual(self.until(c, lambda: ["done"], 60), ["done"])
        self.assertEqual(c.slept, [])

    def test_the_wait_ends_when_the_condition_comes_not_at_the_fuse(self):
        c = Clock()
        answers = iter([None, 0, "", "yes"])
        self.assertEqual(self.until(c, lambda: next(answers), 60, step=0.1), "yes")
        self.assertEqual(len(c.slept), 3)

    def test_a_burnt_fuse_gives_the_last_false_answer(self):
        c = Clock()
        with mock.patch.dict(waiting.os.environ, {waiting.SLOW_ENV: ""}):
            self.assertEqual(self.until(c, lambda: [], 2, step=0.5), [])
        self.assertEqual(sum(c.slept), 2)

    def test_hub_test_slow_stretches_and_shrinks_the_fuse(self):
        for value, fuse in (("3", 6), ("0.1", 0.2)):
            c = Clock()
            with mock.patch.dict(waiting.os.environ, {waiting.SLOW_ENV: value}):
                self.assertEqual(waiting.seconds(2), fuse)
                self.until(c, lambda: False, 2, step=0.1)
            self.assertAlmostEqual(sum(c.slept), fuse, delta=0.1)

    def test_a_bad_multiplier_is_one(self):
        for bad in ("abc", "0", "-2", "nan", "inf", "1e400", ""):
            with mock.patch.dict(waiting.os.environ, {waiting.SLOW_ENV: bad}):
                self.assertEqual(waiting.slow(), 1.0, bad)

    def test_the_runner_lets_the_multiplier_through_to_the_tests(self):
        # tests/__init__.py drops the hub's HUB_* from the tests' environment, but not the run's own HUB_TEST_*
        self.assertTrue(waiting.SLOW_ENV.startswith("HUB_TEST_"))


if __name__ == "__main__":
    unittest.main()
