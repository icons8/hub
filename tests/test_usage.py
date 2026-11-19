"""Spending and limits (owner request 05.10: a plugin with limits and statistics): session spending from increments
of cost_usd in turn.result, account limits from account.limits — for the preinstalled Usage plugin."""
import tests  # noqa: F401 — keep the machine's settings out
import gc
import json
import os
import shutil
import tempfile
import threading
import tracemalloc
import unittest
from unittest import mock

from core import journal, usage
from tests.test_zones import has_tzdata

H = 3600
NOW = 1_791_160_000.0   # 05.10 02:26 Paris time, 00:26 UTC


class Journal:
    """A journal of the test in a temp state folder and the report over it (a mixin: Usage and CodexUsage share it)."""
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="usage-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.lines, self.seq = [], 0
        usage.reset()

    def ev(self, ts, kind, entity=None, **data):
        self.seq += 1
        self.lines.append({"seq": self.seq, "ts": NOW + ts, "src": "host", "type": kind, "entity": entity, "data": data})

    def result(self, ts, name, cost, text="ok"):
        self.ev(ts, "turn.result", name, text=text, cost_usd=cost, turns=3, duration_ms=60000)

    def limits(self, ts, five, week, five_reset, week_reset, status="allowed"):
        self.ev(ts, "account.limits", None, status=status, windows={
            "five_hour": {"used": five, "resets": NOW + five_reset}, "seven_day": {"used": week, "resets": NOW + week_reset}})

    def write(self, name="journal.jsonl", lines=None):
        with open(os.path.join(self.dir, name), "w") as f:
            f.write("".join(journal._line(x) for x in (self.lines if lines is None else lines)))

    def report(self, tz="Europe/Paris", account=None):
        self.write()
        return usage.report(self.dir, now=NOW, tz=tz, account=account)

    def spent(self, rep, days=7):
        out = {}
        for d in rep["days"][-days:]:
            for k, v in d["by"].items():
                out[k] = round(out.get(k, 0) + v, 2)
        return out


class Usage(Journal, unittest.TestCase):
    def test_cost_is_cumulative_over_the_conversation_and_survives_a_restart(self):
        # 05.10: claude continues cost_usd after --resume — a turn after a restart costs the increment, not everything from scratch
        self.ev(-3 * H, "session.started", "maps", session_id="s1")
        self.result(-3 * H, "maps", 1.0)
        self.result(-2 * H, "maps", 1.5)
        self.ev(-2 * H + 10, "session.exited", "maps")
        self.ev(-2 * H + 20, "session.started", "maps", session_id="s1")
        self.result(-1 * H, "maps", 2.25)
        self.assertEqual(self.spent(self.report()), {"maps": 2.25})

    def test_codex_turns_count_in_tokens(self):
        # Codex (experimental): a Codex turn has no cost_usd; tokens per turn (not cumulative) — sums in tokens
        for ts, tin in ((-3 * H, 20000), (-2 * H, 15000)):
            self.ev(ts, "turn.result", "maps", text="ok", cost_usd=None, engine="codex", tokens_in=tin, tokens_out=500,
                    turns=1, duration_ms=1000)
        rep = self.report()
        self.assertEqual(rep["unit"], "tokens")
        self.assertEqual(self.spent(rep), {"maps": 36000})
        self.assertEqual(self.report()["top"][0]["usd"], 20500)

    def test_codex_model_spend_counts_like_a_session(self):
        # the Codex watcher's tokens (codex.spent) — in "Usage" as the line "codex:watch" (not a session name), like turns
        self.ev(-2 * H, "turn.result", "maps", text="ok", cost_usd=None, engine="codex", tokens_in=1000, tokens_out=0,
                turns=1, duration_ms=1)
        self.ev(-1 * H, "codex.spent", "codex:watch", model="gpt-6-luna", tokens_in=9000, tokens_out=100)
        self.assertEqual(self.spent(self.report()), {"maps": 1000, "codex:watch": 9100})

    def test_claude_report_stays_in_dollars(self):
        self.result(-1 * H, "maps", 1.0)
        self.ev(-1 * H + 5, "turn.result", "cx", text="ok", cost_usd=None, engine="codex", tokens_in=60000, tokens_out=1,
                turns=1, duration_ms=1)
        rep = self.report()
        self.assertEqual(rep["unit"], "usd")
        self.assertEqual(self.spent(rep), {"maps": 1.0})   # Codex tokens did not land as dollars

    def test_old_claude_turn_does_not_hide_codex(self):
        self.result(-30 * 24 * H, "maps", 1.0)   # long ago, outside the shown days
        self.ev(-1 * H, "turn.result", "cx", text="ok", cost_usd=None, engine="codex", tokens_in=1000, tokens_out=0,
                turns=1, duration_ms=1)
        rep = self.report()
        self.assertEqual(rep["unit"], "tokens")
        self.assertEqual(self.spent(rep), {"cx": 1000})

    def test_new_conversation_starts_from_zero(self):
        self.result(-3 * H, "maps", 5.0)
        self.result(-2 * H, "maps", 0.4)          # less than before — a new conversation: the turn cost 0.4, not −4.6
        self.assertEqual(self.spent(self.report()), {"maps": 5.4})

    def test_resume_from_an_earlier_point_is_not_a_new_conversation(self):
        # 02.10: after a restart the total went back 215.83 → 207.98 (a saved conversation point) and grew from there
        self.result(-5 * H, "maps", 200.0)
        self.result(-4 * H, "maps", 207.5)
        self.result(-3 * H, "maps", 215.0)
        self.result(-2 * H, "maps", 208.0)
        self.result(-1 * H, "maps", 209.0)
        self.assertEqual(self.spent(self.report()), {"maps": 216.5})   # 215 + 0.5 + 1, not + 208 + 1

    def test_fork_inherits_the_parent_cost_and_pays_only_its_own(self):
        # 05.10: a fork started at 474.99 — the parent's accumulated total; without this the fork "cost" hundreds of dollars
        self.result(-3 * H, "maps", 10.0)
        self.ev(-2 * H, "session.requested", "maps-2", session_id="s2", fork_of="maps")
        self.result(-1 * H, "maps-2", 10.5)
        self.result(-0.5 * H, "maps-2", 11.0)
        rep = self.report()
        self.assertEqual(self.spent(rep), {"maps": 10.0, "maps-2": 1.0})
        self.assertEqual(rep["parents"], {"maps-2": "maps"})

    def test_fork_from_an_earlier_point_of_the_parent(self):
        # 05.10: maps-3 started at 553.18 with the parent at 571.04 — the fork continued the parent's conversation from
        # an earlier point; the baseline is the nearest parent total not above the fork's first total, not a "new
        # conversation" worth all 553
        self.result(-4 * H, "maps", 9.0)
        self.result(-3 * H, "maps", 10.0)
        self.ev(-2 * H, "session.requested", "maps-2", session_id="s2", fork_of="maps")
        self.result(-1 * H, "maps-2", 9.4)
        self.result(-0.5 * H, "maps-2", 9.9)
        self.assertEqual(self.spent(self.report()), {"maps": 10.0, "maps-2": 0.9})

    def test_executor_is_counted_apart_and_linked_to_who_raised_it(self):
        self.ev(-2 * H, "session.requested", "maps-fix", session_id="s3", executor=True, raised_by="maps")
        self.result(-1 * H, "maps-fix", 0.7)
        rep = self.report()
        self.assertEqual((self.spent(rep), rep["parents"]), ({"maps-fix": 0.7}, {"maps-fix": "maps"}))

    def test_rename_keeps_the_baseline(self):
        self.result(-3 * H, "maps", 3.0)
        self.ev(-2 * H, "session.renamed", "maps", to="atlas")
        self.result(-1 * H, "atlas", 3.5)
        self.assertEqual(self.spent(self.report()), {"maps": 3.0, "atlas": 0.5})

    def test_old_name_after_a_rename_keeps_its_parent(self):
        # 05.10: an executor was renamed while turn results kept coming under the old name — it stays under whoever raised it
        self.ev(-3 * H, "session.requested", "maps-fix", session_id="s3", executor=True, raised_by="maps")
        self.ev(-2 * H, "session.renamed", "maps-fix", to="atlas")
        self.result(-1 * H, "maps-fix", 0.5)
        self.assertEqual(self.report()["parents"], {"maps-fix": "maps", "atlas": "maps"})

    def test_rejected_rename_changes_nothing(self):
        # 01.10: the core rejected the first rename — the baseline stayed with the old name, the turn does not cost the whole conversation
        self.result(-4 * H, "maps", 90.0)
        self.ev(-3 * H, "session.renamed", "maps", to="atlas")
        self.ev(-3 * H, "core.rejected", None, type="session.renamed", of=self.seq, reason="name taken")
        self.result(-2 * H, "maps", 91.0)
        self.assertEqual(self.spent(self.report()), {"maps": 91.0})

    @unittest.skipUnless(has_tzdata(), "no timezone database (Windows without tzdata): Europe/Paris here is the machine's time")
    def test_days_follow_the_owner_timezone(self):
        # NOW is 05.10 02:26 Paris time (00:26 UTC): a turn 4 hours ago is still 04.10 in both; 2 hours ago it is
        # 05.10 in Paris but 04.10 in UTC
        self.result(-4 * H, "maps", 1.0)
        self.result(-2 * H, "maps", 3.0)
        rep = self.report()
        self.assertEqual([d["day"] for d in rep["days"]][-2:], ["2026-10-04", "2026-10-05"])
        self.assertEqual([d["total"] for d in rep["days"]][-2:], [1.0, 2.0])
        self.assertEqual(len(rep["days"]), 7)
        self.assertEqual([d["total"] for d in self.report(tz="UTC")["days"]][-2:], [3.0, 0])

    def test_week_window_starts_at_an_early_reset_and_forecasts(self):
        # 04.10 18:41: the weekly limit reset early (0.99 → 0) — the window counts from that moment
        self.result(-30 * H, "maps", 4.0)
        self.limits(-30 * H, 0.2, 0.98, 2 * H, 80 * H)
        self.limits(-20 * H, 0.0, 0.0, 3 * H, 80 * H)      # an early reset
        self.result(-10 * H, "maps", 6.0)
        self.result(-3 * H, "seo", 4.0)
        self.limits(-4 * H, 0.1, 0.06, 1 * H, 80 * H)
        self.limits(-1 * H, 0.3, 0.12, 1 * H, 80 * H)
        rep = self.report()
        week = next(w for w in rep["windows"] if w["name"] == "seven_day")
        self.assertEqual(week["start"], NOW - 20 * H)
        self.assertEqual((week["used"], week["total"], week["by"]), (0.12, 6.0, {"maps": 2.0, "seo": 4.0}))
        self.assertEqual(week["per_pct"], 0.5)                 # $6 for 12% — $0.5 per percent
        # the rate over the last day of the window (from the window start, it is younger than a day): 12% in 19 h; to
        # 100% — 88% / rate
        self.assertAlmostEqual(week["rate_h"], 12 / 19, places=3)
        self.assertAlmostEqual(week["eta"], NOW - 1 * H + 88 / (12 / 19) * H, delta=60)
        self.assertGreater(week["eta"], week["resets"])        # at this rate it lasts until the reset: ~62% by then
        self.assertAlmostEqual(week["at_reset"], 0.12 + 12 / 19 / 100 * 81, places=3)
        five = next(w for w in rep["windows"] if w["name"] == "five_hour")
        self.assertEqual((five["start"], five["total"]), (NOW - 4 * H, 4.0))

    def test_jitter_of_parallel_sessions_is_not_a_reset(self):
        # 05.10 04:16: sessions report the share interleaved — 0.57, 0.56, 0.57; this is not an early reset
        self.limits(-3 * H, 0.2, 0.1, 2 * H, 80 * H)
        self.limits(-2 * H, 0.57, 0.15, 2 * H, 80 * H)
        self.limits(-2 * H + 1, 0.56, 0.15, 2 * H, 80 * H)
        self.limits(-2 * H + 2, 0.57, 0.15, 2 * H, 80 * H)
        five = next(w for w in self.report()["windows"] if w["name"] == "five_hour")
        self.assertEqual(five["start"], NOW - 3 * H)

    def test_no_forecast_from_a_moment_of_data(self):
        self.limits(-10, 0.02, 0.1, 5 * H - 20, 80 * H)        # the window has just started — there is no rate yet
        five = next(w for w in self.report()["windows"] if w["name"] == "five_hour")
        self.assertEqual((five["used"], five["rate_h"], five["eta"]), (0.02, None, None))

    def test_window_past_its_reset_is_empty(self):
        self.limits(-6 * H, 0.5, 0.3, -1 * H, 80 * H)
        rep = self.report()
        five = next(w for w in rep["windows"] if w["name"] == "five_hour")
        self.assertEqual((five["used"], five["eta"]), (0, None))

    def test_forecast_follows_one_account_when_resets_coincide(self):
        # an account pool: two have the window reset in the same hour (the reset is on the hour boundary); the rate
        # is only for the account of the last event, otherwise another account's shares land in the same series
        for ts, used, acc in ((-4 * H, 0.1, "a"), (-3 * H, 0.8, "b"), (-2 * H, 0.2, "a"), (-1 * H, 0.3, "a")):
            self.ev(ts, "account.limits", None, status="allowed", account=acc,
                    windows={"five_hour": {"used": used, "resets": NOW + 1 * H}})
        five = next(w for w in self.report(account={"active": "a"})["windows"] if w["name"] == "five_hour")
        self.assertEqual((five["start"], five["used"]), (NOW - 4 * H, 0.3))   # 0.8 → 0.2 — not an early reset
        self.assertAlmostEqual(five["rate_h"], 20 / 3, places=3)               # 0.1 → 0.3 in 3 h for "a"

    def test_windows_are_the_active_accounts_not_the_last_event(self):
        # the core is on "a", an old process "b" appended its limits after the switch — the Usage row is "a", as in the
        # core; "a" took a fresh token while a process with the old one (lower gen) appended its own — not about the
        # new one (as in reducer: account.limits)
        self.ev(-3 * H, "account.limits", None, status="allowed", account="a", gen=200,
                windows={"five_hour": {"used": 0.2, "resets": NOW + 2 * H}})
        self.ev(-2 * H, "account.limits", None, status="allowed", account="a", gen=100,
                windows={"five_hour": {"used": 0.9, "resets": NOW + 1 * H}})
        self.ev(-1 * H, "account.limits", None, status="allowed", account="b",
                windows={"five_hour": {"used": 0.7, "resets": NOW + 3 * H}})
        five = lambda **acc: next(w for w in self.report(account=acc)["windows"] if w["name"] == "five_hour")   # noqa: E731
        self.assertEqual(five(active="a", refreshed={"a": 150})["used"], 0.2)
        self.assertEqual(five(active="b", refreshed={"a": 150})["used"], 0.7)
        self.assertEqual(self.report(account={"active": "c"})["windows"], [])   # nothing about "c" — empty, as in the core

    def test_a_window_reset_at_this_very_second_is_past(self):
        # the boundary is the same as in the executor queue (executors.reset_passed): a reset right now — the window is already new
        self.limits(-1 * H, 0.4, 0.5, 0, 3 * 86400)
        five = next(w for w in self.report()["windows"] if w["name"] == "five_hour")
        self.assertEqual(five["used"], 0)
        self.assertIs(five["gone"], True)   # the page says "limit reset, no fresh reading" instead of 0%

    def test_a_live_window_is_not_gone(self):
        self.limits(-1 * H, 0.4, 0.5, 2 * H, 3 * 86400)
        five = next(w for w in self.report()["windows"] if w["name"] == "five_hour")
        self.assertIs(five["gone"], False)

    def test_biggest_turns_are_listed_with_what_they_did(self):
        self.result(-3 * H, "maps", 1.0, text="small")
        self.result(-2 * H, "maps", 9.0, text="Rebuilt the map index: " + "x" * 400)
        self.result(-1 * H, "seo", 0.5, text="checked robots")
        rep = self.report()
        top = rep["top"][0]
        self.assertEqual((top["session"], top["usd"], top["ts"]), ("maps", 8.0, NOW - 2 * H))
        self.assertTrue(top["text"].startswith("Rebuilt the map index"))
        self.assertLessEqual(len(top["text"]), 200)

    def test_archive_and_current_journal_overlap_without_double_counting(self):
        # rotation carries the sessions' tail into the new journal — the same seq in two files
        self.result(-3 * H, "maps", 1.0)
        self.result(-2 * H, "maps", 2.0)
        os.makedirs(os.path.join(self.dir, "journal-archive"))
        self.write("journal-archive/journal-1-2.jsonl")
        self.result(-1 * H, "maps", 4.0)
        self.write(lines=self.lines[1:])
        self.assertEqual(self.spent(usage.report(self.dir, now=NOW, tz="UTC")), {"maps": 4.0})

    def test_journal_is_read_incrementally(self):
        self.result(-3 * H, "maps", 1.0)
        self.write()
        self.assertEqual(self.spent(usage.report(self.dir, now=NOW, tz="UTC")), {"maps": 1.0})
        with open(os.path.join(self.dir, "journal.jsonl"), "a") as f:   # appended, and the last line is not finished yet
            f.write(journal._line({"seq": 2, "ts": NOW - H, "src": "host", "type": "turn.result",
                                              "entity": "maps", "data": {"cost_usd": 3.0}}) + '{"seq":3,"ts":')
        self.assertEqual(self.spent(usage.report(self.dir, now=NOW, tz="UTC")), {"maps": 3.0})

    def test_big_journal_is_not_held_in_memory_while_reading(self):
        # Usage reads all archives: from an archive of tens of MB only a few lines are needed, memory must not hold the rest
        self.result(-3 * H, "maps", 1.0)
        filler = journal._line({"seq": 0, "ts": NOW, "src": "host", "type": "session.said", "entity": "maps",
                                "data": {"text": "x" * 400}})
        with open(os.path.join(self.dir, "journal.jsonl"), "w") as f:
            f.write(journal._line(self.lines[0]))
            f.write(filler * 50_000)   # ~20 MB
        tracemalloc.start()
        try:
            rep = usage.report(self.dir, now=NOW, tz="UTC")
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(self.spent(rep), {"maps": 1.0})
        self.assertLess(peak, 4_000_000)

    def test_tails_repeated_by_rotation_are_kept_once(self):
        # 15.10: each archive repeats the sessions' tails from earlier ones (rotation), and Usage held the facts of
        # every file — 70 thousand instead of 19 thousand unique, 58 MB in the page's memory, growing with each archive
        for i in range(2000):
            self.result(-5 * H + i, f"s{i % 50}", 1.0 + i / 100, text="turn " + "é" * 150)
        os.makedirs(os.path.join(self.dir, "journal-archive"))

        def retained(copies):
            usage.reset()
            for k in range(copies):
                self.write(f"journal-archive/journal-{k}-{2000 + k}.jsonl")
            self.write(lines=self.lines[-10:])
            gc.collect()
            tracemalloc.start()
            try:
                before = tracemalloc.get_traced_memory()[0]
                rep = usage.report(self.dir, now=NOW, tz="UTC")
                gc.collect()
                return tracemalloc.get_traced_memory()[0] - before, rep
            finally:
                tracemalloc.stop()
        one, rep1 = retained(1)
        many, rep8 = retained(8)
        self.assertEqual(rep1["days"], rep8["days"])
        self.assertLess(many, one * 1.5)

    def test_archive_is_not_read_again_after_a_restart(self):
        # 25.10: after a page restart the first /api/usage re-read all archives (19 GB) — minutes with no answer; an
        # archive does not change — its facts are kept in a file next to it
        self.result(-3 * H, "maps", 1.0)
        self.result(-2 * H, "maps", 2.0)
        os.makedirs(os.path.join(self.dir, "journal-archive"))
        self.write("journal-archive/journal-1-2.jsonl")
        self.result(-1 * H, "maps", 4.0)
        self.write(lines=self.lines[2:])
        first = usage.report(self.dir, now=NOW, tz="UTC")
        usage.reset()   # the page was restarted
        read = []
        scan = usage._scan
        with mock.patch.object(usage, "_scan", side_effect=lambda f, out: read.append(f.name) or scan(f, out)):
            again = usage.report(self.dir, now=NOW, tz="UTC")
        self.assertEqual(again, first)
        self.assertEqual(self.spent(again), {"maps": 4.0})
        self.assertFalse([p for p in read if "journal-archive" in p])

    def test_facts_saved_before_codex_turns_are_kept(self):
        # the format before Codex turns (experiment): a Claude hub's archive has no Codex facts — not reread by the new
        # version (every archive at once, minutes); a file of another older format is
        self.result(-3 * H, "maps", 1.0)
        os.makedirs(os.path.join(self.dir, "journal-archive"))
        archive = os.path.join(self.dir, "journal-archive", "journal-1-1.jsonl")
        self.write("journal-archive/journal-1-1.jsonl")
        self.lines = []
        self.write()
        usage.report(self.dir, now=NOW, tz="UTC")
        with open(archive + usage.SIDECAR, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["format"], usage.FORMAT)
        # 2 — the file of the experiment before the engine of limits (a Codex stand only): read again
        for fmt, kinds, kept in ((1, [k for k in usage.KINDS if k != "codex.spent"], True), (0, usage.KINDS, False),
                                 (2, usage.KINDS, False)):
            with open(archive + usage.SIDECAR, "w") as f:
                json.dump({**saved, "format": fmt, "types": kinds,
                           "facts": [{**x, "cost": 7.0} for x in saved["facts"]]}, f)
            usage.reset()
            with self.subTest(format=fmt):
                self.assertEqual(self.spent(usage.report(self.dir, now=NOW, tz="UTC")), {"maps": 7.0 if kept else 1.0})

    def test_saved_facts_of_another_archive_are_not_trusted(self):
        # a facts file of the wrong size (the archive was replaced) or corrupt — the archive is read again
        self.result(-3 * H, "maps", 1.0)
        os.makedirs(os.path.join(self.dir, "journal-archive"))
        archive = os.path.join(self.dir, "journal-archive", "journal-1-1.jsonl")
        self.write("journal-archive/journal-1-1.jsonl")
        self.lines = []
        self.write()
        self.assertEqual(self.spent(usage.report(self.dir, now=NOW, tz="UTC")), {"maps": 1.0})
        size, kinds = os.path.getsize(archive), usage.KINDS
        # and of the same size, but with a different set of types (TYPES changed) or not facts
        for broken in ("{", json.dumps({"format": usage.FORMAT, "types": kinds, "size": 1, "facts": []}),
                       json.dumps({"format": usage.FORMAT, "types": kinds[:-1], "size": size, "facts": []}),
                       json.dumps({"format": usage.FORMAT, "types": kinds, "size": size, "facts": [1]}),
                       json.dumps({"format": usage.FORMAT, "types": kinds, "size": size, "facts": [{"ts": 1}]}),
                       json.dumps({"format": usage.FORMAT, "types": kinds, "size": size, "facts": [{"seq": 5}]})):
            with open(archive + usage.SIDECAR, "w") as f:
                f.write(broken)
            usage.reset()
            self.assertEqual(self.spent(usage.report(self.dir, now=NOW, tz="UTC")), {"maps": 1.0})

    def test_lines_across_pieces_of_the_file(self):
        # an archive is read in pieces: a line across the join of pieces, a long unneeded line (a state snapshot) and
        # a type mark not at the start of a line (reply text) — same as when reading line by line
        self.result(-5 * H, "maps", 1.0)
        self.ev(-4 * H, "core.snapshot", None, why="rotate", state={"x": "y" * 5000})
        self.ev(-4 * H, "session.said", "maps", text="x" * 300 + '"type":"turn.result"')
        self.result(-3 * H, "maps", 3.0, text="é" * 700)
        self.result(-2 * H, "maps", 6.0)
        for size in (7, 64, 199, 200, 201, 4096, 1 << 20):
            usage.reset()
            with self.subTest(size=size), mock.patch.object(usage, "CHUNK", size, create=True):
                rep = self.report()
                self.assertEqual(self.spent(rep), {"maps": 6.0})
                self.assertEqual(sorted(x["usd"] for x in rep["top"]), [1.0, 2.0, 3.0])

    def test_rotated_journal_is_not_read_again_as_an_archive(self):
        # rotation turns the journal into an archive (the same file): its facts are already read — do not re-read 170 MB every half hour
        self.result(-3 * H, "maps", 1.0)
        self.result(-2 * H, "maps", 2.0)
        self.write()
        self.assertEqual(self.spent(usage.report(self.dir, now=NOW, tz="UTC")), {"maps": 2.0})
        self.result(-90 * 60, "maps", 3.0)   # appended after the last read — the rest is read from the archive
        self.write()
        os.makedirs(os.path.join(self.dir, "journal-archive"))
        os.link(os.path.join(self.dir, "journal.jsonl"), os.path.join(self.dir, "journal-archive", "journal-1-2.jsonl"))
        os.remove(os.path.join(self.dir, "journal.jsonl"))
        self.result(-1 * H, "maps", 4.0)
        read = []
        scan = usage._scan
        with mock.patch.object(usage, "_scan", side_effect=lambda f, out: read.append((f.name, f.tell())) or scan(f, out)):
            self.write(lines=self.lines[1:])   # the new journal: the carried-over tail and a new turn
            rep = usage.report(self.dir, now=NOW, tz="UTC")
        self.assertEqual(self.spent(rep), {"maps": 4.0})
        self.assertEqual(sorted(x["usd"] for x in rep["top"]), [1.0, 1.0, 1.0, 1.0])
        self.assertFalse([p for p, at in read if "journal-archive" in p and at == 0])

    def test_page_is_answered_while_archives_are_read(self):
        # the first read of archives (after an update) takes minutes: the request does not hang but hears "later"; once read — the report
        self.result(-10 * 24 * H, "maps", 1.0)   # older than a week — only a baseline for the next turn
        os.makedirs(os.path.join(self.dir, "journal-archive"))
        self.write("journal-archive/journal-1-1.jsonl")
        self.lines = []
        self.result(-1 * H, "maps", 4.0)
        self.write()
        go = threading.Event()
        archive = usage._archive
        with mock.patch.object(usage, "_archive", side_effect=lambda *a: go.wait(10) and archive(*a)):
            with self.assertRaises(usage.Busy):
                usage.report(self.dir, now=NOW, tz="UTC", wait=0.05)
            with self.assertRaises(usage.Busy):   # the second request — the same reading thread, not a second one
                usage.report(self.dir, now=NOW, tz="UTC", wait=0.05)
            self.assertEqual(sum(t.name == "usage-archives" for t in threading.enumerate()), 1)
            go.set()
            self.assertEqual(self.spent(usage.report(self.dir, now=NOW, tz="UTC", wait=10)), {"maps": 3.0})

    def test_only_files_named_by_rotation_are_archives(self):
        # a file in the archive folder under another name (a copy, a renamed one) is not an archive: its spending is not counted
        self.result(-3 * H, "copied", 1.0)
        os.makedirs(os.path.join(self.dir, "journal-archive"))
        self.write("journal-archive/journal-1-1 copy.jsonl")
        self.lines = []
        self.result(-1 * H, "maps", 4.0)
        self.assertEqual(self.spent(self.report()), {"maps": 4.0})

    def test_archive_removed_while_reading(self):
        self.result(-H, "s", 1.0)
        self.write()
        gone = os.path.join(self.dir, "journal-archive", "journal-1-9.jsonl")
        with mock.patch.object(usage.journal, "archives", return_value=[gone]):
            self.assertEqual(self.spent(usage.report(self.dir, now=NOW, tz="UTC")), {"s": 1.0})


if __name__ == "__main__":
    unittest.main()


class CodexUsage(Journal, unittest.TestCase):
    """Codex (experimental): a hub with Codex sessions. Codex gives tokens, Claude gives dollars: the two never
    go into one sum, and neither hides the other; a Claude hub's report stays as it was."""

    def codex(self, ts, name, tokens):
        self.ev(ts, "turn.result", name, text="ok", cost_usd=None, engine="codex", tokens_in=tokens, tokens_out=0,
                turns=1, duration_ms=1000)

    def codex_limits(self, ts, windows, status="allowed", type=None):
        # as hosts/codex_limits.limits_fact writes it: windows by duration, the "codex" engine, no account name
        self.ev(ts, "account.limits", None, status=status, type=type, overage=False, engine="codex", plan="plus",
                windows={n: {"used": u, "resets": NOW + r} for n, (u, r) in windows.items()})

    def test_mixed_hub_shows_codex_tokens_next_to_claude_dollars(self):
        # report.md p. 9: a dollar turn of Claude in the shown days hid every Codex token
        self.result(-2 * H, "maps", 1.0)
        self.codex(-1 * H, "cx", 60000)
        rep = self.report()
        self.assertEqual(rep["unit"], "usd")
        self.assertEqual(self.spent(rep), {"maps": 1.0})   # tokens did not land as dollars
        self.assertEqual(rep["other"]["unit"], "tokens")
        self.codex(-10, "cx", 1)   # Codex heard from last: the page does not jump while Claude has turns
        self.assertEqual(self.report()["unit"], "usd")
        self.assertEqual(self.spent(rep["other"]), {"cx": 60000})
        self.assertEqual([t["session"] for t in rep["top"]], ["maps"])
        self.assertEqual([(t["session"], t["usd"]) for t in rep["other"]["top"]], [("cx", 60000)])

    def test_each_engine_has_its_own_limit_windows(self):
        # both write account.limits with no account name: Claude's windows from Claude's facts, Codex's from Codex's
        self.limits(-3 * H, 0.2, 0.3, 2 * H, 3 * 24 * H)
        self.result(-2 * H, "maps", 1.0)
        self.codex_limits(-1 * H, {"five_hour": (0.7, H), "seven_day": (0.9, 2 * 24 * H)})
        self.codex(-1 * H + 5, "cx", 5000)
        rep = self.report()
        self.assertEqual({w["name"]: w["used"] for w in rep["windows"]}, {"five_hour": 0.2, "seven_day": 0.3})
        self.assertEqual({w["name"]: w["total"] for w in rep["windows"]}, {"five_hour": 1.0, "seven_day": 1.0})
        self.assertEqual({w["name"]: w["used"] for w in rep["other"]["windows"]}, {"five_hour": 0.7, "seven_day": 0.9})
        self.assertEqual({w["name"]: w["total"] for w in rep["other"]["windows"]}, {"five_hour": 5000, "seven_day": 5000})

    def test_codex_hub_limits_are_not_claudes(self):
        # a hub moved to Codex: an old Claude reading does not stand in for the Codex windows
        self.limits(-8 * 24 * H, 0.95, 0.95, -8 * 24 * H + 5 * H, 2 * 24 * H)   # before the shown days
        self.codex_limits(-1 * H, {"five_hour": (0.1, 4 * H)})
        self.codex(-1 * H + 5, "cx", 100)
        rep = self.report()
        self.assertEqual(rep["unit"], "tokens")
        self.assertNotIn("other", rep)
        self.assertEqual({w["name"]: w["used"] for w in rep["windows"]}, {"five_hour": 0.1})

    def test_a_hub_moved_to_codex_keeps_claudes_last_reading_below(self):
        # moved 30 h ago: no Claude turns since, its last limits reading within the shown days — Codex on top
        self.limits(-30 * H, 0.95, 0.95, -25 * H, 2 * 24 * H)
        self.codex_limits(-1 * H, {"five_hour": (0.1, 4 * H)})
        self.codex(-1 * H + 5, "cx", 100)
        rep = self.report()
        self.assertEqual((rep["unit"], [w["used"] for w in rep["windows"]]), ("tokens", [0.1]))
        self.assertEqual((rep["other"]["unit"], [w["name"] for w in rep["other"]["windows"]]),
                         ("usd", ["five_hour", "seven_day"]))

    def test_a_codex_window_of_another_length_is_shown(self):
        # codex_limits names a window it does not know by its length (codex_<N>m) — "Usage" knows its length too
        self.codex_limits(-1 * H, {"codex_60m": (0.4, 30 * 60)})
        self.codex(-10 * 60, "cx", 700)
        self.codex(-2 * H, "cx", 900)   # before the hour window started
        (w,) = self.report()["windows"]
        self.assertEqual((w["name"], w["used"], w["start"], w["total"]), ("codex_60m", 0.4, NOW - 30 * 60, 700))

    def test_codex_limits_alone_bring_the_codex_block(self):
        # a Claude hub with Codex sessions that have not finished a turn yet: their limits are already there
        self.result(-1 * H, "maps", 1.0)
        self.codex_limits(-1 * H, {"five_hour": (0.3, H)})
        rep = self.report()
        self.assertEqual(self.spent(rep["other"]), {})
        self.assertEqual([w["name"] for w in rep["other"]["windows"]], ["five_hour"])

    def test_an_old_codex_trial_brings_no_block(self):
        # Codex limits from before the shown days: the hub is not on Codex now
        self.codex_limits(-9 * 24 * H, {"five_hour": (0.3, -9 * 24 * H + H)})
        self.result(-1 * H, "maps", 1.0)
        self.assertNotIn("other", self.report())

    def test_claude_limits_stay_with_codex_turns_only(self):
        # Claude sessions idle for the shown days, their limits still read; Codex works: Claude's block is not dropped
        self.limits(-3 * 24 * H, 0.5, 0.97, -3 * 24 * H + 5 * H, 2 * 24 * H, status="rejected")
        self.codex(-1 * H, "cx", 100)
        rep = self.report()
        self.assertEqual((rep["unit"], self.spent(rep)), ("tokens", {"cx": 100}))
        self.assertEqual((rep["other"]["unit"], rep["other"]["status"], [w["name"] for w in rep["other"]["windows"]]),
                         ("usd", "rejected", ["five_hour", "seven_day"]))

    def test_claude_hub_report_is_as_before(self):
        # no Codex facts: the same keys and the same numbers as before Codex — no second block
        self.limits(-3 * H, 0.2, 0.3, 2 * H, 3 * 24 * H)
        self.result(-2 * H, "maps", 1.0)
        rep = self.report()
        self.assertEqual(set(rep), {"now", "tz", "unit", "days", "windows", "status", "top", "parents", "unpriced"})
        self.assertEqual((rep["unit"], self.spent(rep), [w["name"] for w in rep["windows"]]),
                         ("usd", {"maps": 1.0}, ["five_hour", "seven_day"]))

    def test_claude_pool_does_not_drop_codex_limits(self):
        # a Claude account pool (active "a"): Codex limits have no account name and are not the pool's
        self.ev(-3 * H, "account.limits", None, status="allowed", account="a",
                windows={"five_hour": {"used": 0.5, "resets": NOW + H}})
        self.codex_limits(-1 * H, {"five_hour": (0.6, H)})
        self.codex(-1 * H + 5, "cx", 10)
        self.result(-1 * H + 9, "maps", 1.0)
        rep = self.report(account={"active": "a"})
        self.assertEqual([w["used"] for w in rep["windows"]], [0.5])
        self.assertEqual([w["used"] for w in rep["other"]["windows"]], [0.6])

    def test_facts_keep_the_engine_of_limits(self):
        self.codex_limits(-1 * H, {"five_hour": (0.6, H)})
        self.limits(-1 * H, 0.1, 0.1, H, H)
        self.write()
        got = [f for f in usage.facts(self.dir) if f["type"] == "account.limits"]
        self.assertEqual([f.get("engine") for f in got], ["codex", None])
        self.assertNotIn("engine", got[1])   # a Claude fact is the same as before Codex
