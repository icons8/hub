"""The core's measurement of a login module's pool (core/accountprobe.py): the "Refresh" button of Settings → Accounts and
`hub accounts probe` while a module leads. In the core, not in the built-in pool: the open flavor is cut without it
(core/cutout.py) and a module still measures there — so these tests run on the cut tree too."""
import tests  # noqa: F401 — hide the machine's settings
import json
import os
import tempfile
import unittest

from core import accountprobe


class Mods:
    """The module's cli point: answer(payload) -> its raw answer (None — silent)."""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def call(self, name, point, payload, ctx=None):
        self.calls.append((name, point, payload))
        return self.answer(payload)


class Probe(unittest.TestCase):
    def test_the_module_measures_and_its_rows_are_kept_in_shape(self):
        rows = [{"name": "work", "code": "ok", "req": "r1", "limits": {"status": "allowed", "windows": {
                    "five_hour": {"used": 0.4, "resets": 1000}, "weird": {"used": 1}, "seven_day": {"used": "x"}}}},
                {"name": "corp", "code": "command", "status": True, "error": "no token"}]
        mods = Mods(lambda p: {"out": json.dumps({"results": rows}), "code": 0})
        got, refusal = accountprobe.module_probe(mods, "acc", ["work"], {"owner": True}, "1.1.0")
        self.assertIsNone(refusal)
        self.assertEqual(mods.calls[0][1:], ("cli", {"command": "accounts", "args": ["probe", "--json", "work"],
                                                     "caller": {"owner": True}}))
        self.assertEqual(got, [{"name": "work", "code": "ok", "req": "r1",
                                "limits": {"status": "allowed", "windows": {"five_hour": {"used": 0.4, "resets": 1000},
                                                                            "seven_day": {}}}},
                               {"name": "corp", "code": "command", "error": "no token"}])

    def test_a_silent_old_or_refusing_module_is_a_refusal(self):
        self.assertEqual(accountprobe.module_probe(Mods(lambda p: None), "acc", [], {})[0], None)
        old, why = accountprobe.module_probe(Mods(lambda p: {"out": "usage: hub accounts …", "code": 0}), "acc", [], {},
                                             "1.0.9")
        self.assertIsNone(old)
        self.assertIn("1.0.9", why)
        self.assertEqual(accountprobe.module_probe(Mods(lambda p: {"out": json.dumps({"error": " busy "})}), "acc", [],
                                                   {}), (None, "busy"))

    def test_recorded_waits_for_every_reading_within_one_wait(self):
        # one reading the core never records does not eat the wait of the others: all are looked for at once
        from core import journal
        state = tempfile.mkdtemp(prefix="acc-rec-")
        j = journal.Journal(os.path.join(state, "journal.jsonl"))
        j.append("core.started", None, {})
        start = journal.mark(j.path)
        a = j.append("account.limits", None, {"account": "a", "req": "req-a"})
        b = j.append("account.limits", None, {"account": "b", "req": "req-b"})
        j.append("core.rejected", None, {"of": b["seq"], "type": "account.limits", "reason": "no"})
        got = accountprobe.recorded(j.path, {"lost": "req-lost", "a": "req-a", "b": "req-b"}, 1.0, start)
        self.assertEqual({k: (seq, str(why) if why else why) for k, (seq, why) in got.items()},
                         {"lost": (None, None), "a": (a["seq"], None), "b": (b["seq"], "no")})

    def test_a_reading_recorded_at_the_end_of_the_wait_is_still_recorded(self):
        # the core records it just as the wait runs out: one more pass for a refusal behind it, not "not recorded"
        from unittest import mock
        from core import journal
        state = tempfile.mkdtemp(prefix="acc-rec-end-")
        j = journal.Journal(os.path.join(state, "journal.jsonl"))
        j.append("core.started", None, {})
        start, real, late = journal.mark(j.path), journal.time.sleep, []

        def sleep(s):
            if not late:
                late.append(j.append("account.limits", None, {"account": "a", "req": "req-a"}))
            real(s)
        with mock.patch.object(journal.time, "sleep", sleep):
            got = accountprobe.recorded(j.path, {"a": "req-a"}, 0.05, start)
        self.assertEqual(got, {"a": (late[0]["seq"], None)})


if __name__ == "__main__":
    unittest.main()
