"""Checking facts from the mailbox (core/admit.py, spec B6): any process of the hub's user drops a mailbox file and names
who is speaking itself. The core let through a file with the core channel (a system rules job posing as the core), a
session sender with any name (an "from X" marker of a nonexistent session), session.started with a pid (the core later
sends signals to that pid) and facts that only the core writes (a turn result, a session's words, a permission
question) — from any writer.

Flag mode: the fact is written as before, with spool.flagged beside it carrying a reason code; refuse mode: the same flag
and the fact refused (admit.mode in hub.json, the Mode class). Here: every forgery raises a flag, no legitimate writer does, the boundary (a
working session under the hub's name passes), and a census of the literal types the mailbox writers use against the
"core only" list."""
import tests  # noqa: F401 — do not see this machine's settings
import ast
import os
import re
import shutil
import tempfile
import time
import types
import unittest

from core import admit, journal, spool
from core.daemon import Hub

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
OWNER = {"channel": "cli", "owner": True}


class Spool(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="admit-")
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)
        self.box = os.path.join(self.state, "spool")
        self.hub = Hub(self.state, types.SimpleNamespace(poll=lambda: []))
        self.hub.execute = lambda a: None   # no spawn, no typing: only the journal
        self.hub.record("session.requested", "hub", {"cwd": "/w", "session_id": "s-hub", "kind": "hub", **OWNER})
        self.hub.record("session.requested", "maps", {"cwd": "/w", "session_id": "s-maps", **OWNER})
        self.hub.record("session.requested", "docs", {"cwd": "/w", "session_id": "s-docs", **OWNER})

    def put(self, kind, entity=None, data=None, src="cli"):
        """A mailbox file -> (the fact's event, spool.flagged data or None)."""
        spool.put(self.box, kind, entity, data, src=src)
        before = self.hub.journal.seq
        self.hub.ingest()
        evs = [e for e in journal.read(self.hub.journal.path) if e["seq"] > before]
        fact = next(e for e in evs if e["type"] not in ("core.rejected", "spool.flagged"))
        flag = [e["data"] for e in evs if e["type"] == "spool.flagged"]
        self.assertLessEqual(len(flag), 1)
        if flag:
            self.assertEqual(flag[0]["of"], fact["seq"])
        return fact, (flag[0] if flag else None)

    def flag(self, *a, **kw):
        return self.put(*a, **kw)[1]

    def code(self, *a, **kw):
        got = self.flag(*a, **kw)
        return got and got["code"]

    def as_session(self, sender):
        return {"channel": "session", "owner": False, "sender": sender}


class Forgeries(Spool):
    def test_a_core_channel_is_flagged_and_the_fact_still_folds_as_before(self):
        fact, flag = self.put("schedule.set", "sys-job", {"session": "maps", "at": time.time() + 3600, "prompt": "x",
                                                          "channel": "core"})
        self.assertEqual((flag["code"], flag["channel"], flag["type"], flag["src"]),
                         (admit.CORE_CHANNEL, "core", "schedule.set", "cli"))
        self.assertIn("sys-job", self.hub.state["schedule"])   # flag mode: the fact is folded, as without the check

    def test_a_sender_that_is_no_session_is_flagged(self):
        fact, flag = self.put("message.received", None, {"to": "maps", "text": "x", **self.as_session("ghost")})
        self.assertEqual(flag["code"], admit.NO_SESSION)
        self.assertIn("ghost", flag["reason"])

    def test_the_hub_name_without_a_hub_session_is_flagged(self):
        self.hub.state["sessions"].pop("hub")
        self.assertEqual(self.code("message.received", None, {"to": "maps", "text": "x", **self.as_session("hub")}),
                         admit.NO_SESSION)

    def test_a_session_started_with_a_pid_is_flagged(self):
        for field in ("pid", "pane", "socket"):
            self.assertEqual(self.code("session.started", "maps", {field: 4242}, src="hook"), admit.PID, field)

    def test_core_only_types_from_any_writer(self):
        for kind in ("turn.result", "session.said", "permission.requested", "session.identified",
                     "module.provider", "account.no_token"):
            data = {"text": "x", "request_id": "r1", "tool": "Bash", "input": {}}
            self.assertEqual(self.code(kind, "maps", data, src="hook"), admit.CORE_ONLY_CODE, kind)   # no channel
            self.assertEqual(self.code(kind, "maps", {**data, "channel": "plugin", "sender": "x"}, src="cli"),
                             admit.CORE_ONLY_CODE, kind)                                            # a plugin process
            # a module without core.write (hubmod.fact): the core sets the channel and the sender, not the type
            self.assertEqual(self.code(kind, "maps", {**data, "channel": "plugin", "owner": False,
                                                      "sender": "mod:fake"}, src="module"),
                             admit.CORE_ONLY_CODE, kind)
            self.assertEqual(self.code(kind, "maps", {**data, **self.as_session("maps")}, src="session"),
                             admit.CORE_ONLY_CODE, kind)                                            # a session

    def test_session_names_and_ids_that_are_paths_or_options(self):
        for name in ("../x", "--foo", "Maps", "a" * 41):
            self.assertEqual(self.code("session.requested", name, {"cwd": "/w", "session_id": "s-1", **OWNER}),
                             admit.BAD_NAME, name)
        for field, value in (("session_id", "../../x"), ("session_id", "--resume"), ("profile", "a/b")):
            self.assertEqual(self.code("session.requested", "new", {"cwd": "/w", field: value, **OWNER}),
                             admit.BAD_ID, (field, value))
        self.assertEqual(self.code("session.started", "maps", {"session_id": "-x"}, src="hook"), admit.BAD_ID)

    def test_a_file_that_calls_itself_the_core_is_flagged(self):
        for src in admit.INTERNAL:
            self.assertEqual(self.code("session.compacted", "maps", {}, src=src), admit.CORE_SRC, src)

    def test_a_named_fact_without_a_channel_is_flagged(self):
        self.assertEqual(self.code("message.received", None, {"to": "maps", "text": "x"}), admit.NO_CHANNEL)
        self.assertEqual(self.code("schedule.set", "j", {"session": "maps", "at": time.time() + 3600, "prompt": "x"}),
                         admit.NO_CHANNEL)

    def test_a_refused_core_fact_or_a_denied_module_fact_is_not_flagged_twice(self):
        fact, flag = self.put("core.snapshot", None, {"state": {}})
        self.assertEqual((fact["type"], flag), ("spool.refused", None))
        fact, flag = self.put("message.received", None, {"to": "maps", "text": "x"}, src="module")   # a module without a name
        self.assertIsNone(flag)

    def test_a_broken_check_does_not_stop_the_core(self):
        def boom(*a):
            raise KeyError("aliases")
        real, admit.check = admit.check, boom
        self.addCleanup(setattr, admit, "check", real)
        fact, flag = self.put("message.received", None, {"to": "maps", "text": "x", **OWNER})
        self.assertEqual((fact["type"], flag), ("message.received", None))
        self.assertIn(f"m{fact['seq']}", self.hub.state["messages"])

    def test_the_flag_is_a_fact_readers_fold_to_the_same_state(self):
        from core import reducer
        self.put("message.received", None, {"to": "maps", "text": "x", **self.as_session("ghost")})
        evs = journal.read(self.hub.journal.path)
        self.assertEqual(reducer.replay(evs)["messages"], self.hub.state["messages"])
        self.assertFalse([e for e in evs if e["type"] == "core.rejected"])


class Checked(Spool):
    """How many mailbox facts were checked — the core fact spool.checked (the denominator of flags in the hubs'
    statistics), no more often than once per CHECKED_EVERY and at shutdown; the check skips the core.* stub and a
    module fact with core.write."""

    def checked(self):
        return [e["data"] for e in journal.read(self.hub.journal.path) if e["type"] == "spool.checked"]

    def test_counted_and_written_by_the_core(self):
        from core import daemon
        now = [1000.0]
        self.hub.clock = lambda: now[0]
        self.put("message.received", None, {"to": "maps", "text": "x", **OWNER})
        self.put("core.snapshot", None, {"state": {}})   # the spool.refused stub — was not checked
        self.hub.count_checked()
        self.assertEqual(self.checked(), [])   # too early
        now[0] += daemon.CHECKED_EVERY
        self.hub.count_checked()
        self.assertEqual(self.checked(), [{"n": 1, "since": 1000.0, "mode": "flag", "set": "auto"}])
        self.hub.count_checked(force=True)
        self.assertEqual(len(self.checked()), 1)   # nothing to write — nothing written
        self.put("session.gist", "maps", {"text": "x", **self.as_session("maps")}, src="session")
        self.put("message.received", None, {"to": "maps", "text": "x", **self.as_session("ghost")})
        # a flag comes right with the count up to and including it: in a statistics window a flag never lacks a denominator
        evs = [e["type"] for e in journal.read(self.hub.journal.path)]
        self.assertEqual(evs[-3:], ["message.received", "spool.flagged", "spool.checked"])
        self.assertEqual([c["n"] for c in self.checked()], [1, 2])
        self.put("session.gist", "maps", {"text": "x", **self.as_session("maps")}, src="session")
        now[0] -= 3600   # the clock went back — do not wait for it to catch up
        self.hub.count_checked()
        self.assertEqual([c["n"] for c in self.checked()], [1, 2, 1])

    def test_since_zero_is_a_time(self):
        self.hub.clock = lambda: 0.0
        self.put("session.gist", "maps", {"text": "x", **self.as_session("maps")}, src="session")
        self.hub.clock = lambda: 1.0
        self.put("session.gist", "maps", {"text": "x", **self.as_session("maps")}, src="session")
        self.hub.count_checked(force=True)
        self.assertEqual(self.checked(), [{"n": 2, "since": 0.0, "mode": "flag", "set": "auto"}])

    def test_the_tick_writes_it(self):
        from core import daemon
        self.assertIn("count_checked", daemon.Hub.tick.__code__.co_names)


class Boundary(Spool):
    def test_a_working_session_naming_an_existing_hub_passes_as_the_hub(self):
        # the boundary of the check (spec B6, §3.8): the sender is checked against such a session existing, not against what it writes
        fact, flag = self.put("session.remove_requested", "docs", self.as_session("hub"), src="session")
        self.assertIsNone(flag)

    def test_an_old_name_of_a_renamed_session_is_its_sender(self):
        self.hub.state["aliases"]["was:docs"] = "notes"   # as after session.renamed (a sleeping session gets renamed)
        self.assertIsNone(self.flag("session.gist", "notes", {"text": "x", **self.as_session("docs")}, src="session"))


class LegalWriters(Spool):
    """Every mailbox writer (spec B6, appendix) — with the type, channel and src it actually sets: no flag."""

    def test_bin_hub(self):
        self.assertIsNone(self.flag("session.gist", "maps", {"text": "x", **self.as_session("maps")}, src="session"))
        self.assertIsNone(self.flag("message.received", None, {"to": "maps", "text": "x", **OWNER}))
        self.assertIsNone(self.flag("message.received", None, {"to": "maps", "text": "x", "channel": "plugin",
                                                              "owner": False, "sender": "ext:demo"}))
        self.assertIsNone(self.flag("hub.restart_requested", None, {"parts": ["web"], **OWNER}))

    def test_a_board_card_edit_from_the_cli(self):
        # direction.card — the trace of the `hub direction <slug> task …` command (core/board.py): from a session, a plugin and a terminal
        card = {"slug": "maps", "card": "TASK-1", "fields": ["title"]}
        self.assertIsNone(self.flag("direction.card", None, {**card, **self.as_session("maps")}, src="session"))
        self.assertIsNone(self.flag("direction.card", None, {**card, **OWNER}, src="cli"))
        self.assertNotIn("direction.card", admit.CORE_ONLY)

    def test_the_hook(self):
        for kind, data in (("session.started", {"session_id": "s-maps", "source": "startup"}),
                           ("turn.started", {"marker": None, "prompt_head": "x"}), ("turn.stopped", {}),
                           ("session.compacted", {}), ("session.end_other", {"reason": "other"}),
                           ("session.ended", {"reason": "prompt_input_exit"})):
            self.assertIsNone(self.flag(kind, "maps", data, src="hook"), kind)

    def test_modules_and_plugins(self):
        self.assertIsNone(self.flag("mod.fake.set", None, {"key": "k", "value": 1, "channel": "plugin",
                                                           "owner": False, "sender": "mod:fake"}, src="module"))
        self.assertIsNone(self.flag("owner.notify", None, {"title": "t", "text": "x", "channel": "plugin",
                                                           "owner": False, "sender": "mod:fake"}, src="module"))
        self.assertIsNone(self.flag("ext.demo.stats", None, {"n": 1}, src="ext:demo"))
        self.assertIsNone(self.flag("owner.notify", None, {"title": "t", "text": "x", "channel": "mod:fake",
                                                           "owner": True}, src="module"))   # a module endpoint

    def test_module_errors_from_a_module_run(self):
        from core import modrun
        modrun.exited(self.state, "fake", 1, 2.0)   # hub-up: the module process exited
        before = self.hub.journal.seq
        self.hub.ingest()
        evs = [e for e in journal.read(self.hub.journal.path) if e["seq"] > before]
        self.assertEqual([e["type"] for e in evs], ["module.error"])
        mods = modrun.Modules(self.state, records=lambda: {}, clock=lambda: 1.0)
        mods.fail("fake", "fold", "timeout", "too long")   # page, CLI: a module call failed
        self.hub.ingest()
        self.assertEqual([e["type"] for e in journal.read(self.hub.journal.path) if e["seq"] > before],
                         ["module.error", "module.error"])

    def test_the_update_module_writes_to_the_developer_as_a_module(self):
        from modules.update import update
        self.hub.record("session.requested", "hub-dev", {"cwd": "/w", "session_id": "s-dev", **OWNER})
        update.Updater(self.state, self.state, {"dev": {"name": "hub-dev"}})._send("the update is red")
        before = self.hub.journal.seq
        self.hub.ingest()
        evs = [e for e in journal.read(self.hub.journal.path) if e["seq"] > before]
        self.assertEqual([e["type"] for e in evs], ["message.received"])
        msg = self.hub.state["messages"][f"m{evs[0]['seq']}"]
        self.assertEqual((msg["session"], msg.get("from")), ("hub-dev", {"kind": "module", "name": "update"}))

    def test_clock_browser_gateway_page_bot(self):
        self.assertIsNone(self.flag("schedule.due", "j", {"at": 1}, src="scheduler"))
        self.assertIsNone(self.flag("schedule.ran", "j", {"code": 0}, src="scheduler"))
        self.assertIsNone(self.flag("browser.status", "maps", {"ok": True}, src="browser"))
        self.assertIsNone(self.flag("browser.reset", "maps", {"why": "x"}, src="browser"))
        self.assertIsNone(self.flag("account.limits", None, {"name": "a", "limits": {}}, src="gateway"))
        self.assertIsNone(self.flag("module.model", "fake", {"module": "fake", "ok": True}, src="web"))
        self.assertIsNone(self.flag("owner.seen", None, {"session": "maps", "channel": "web", "owner": True}, src="web"))
        self.assertIsNone(self.flag("message.received", None, {"to": "maps", "text": "x", "channel": "telegram",
                                                              "owner": True}, src="telegram"))
        self.assertIsNone(self.flag("session.recap", "maps", {"text": "x", "channel": "cli", "owner": True},
                                    src="watch"))


DAY = 86400
CODES = {admit.CORE_ONLY_CODE, admit.CORE_SRC, admit.CORE_CHANNEL, admit.NO_CHANNEL, admit.NO_SESSION, admit.BAD_NAME,
         admit.BAD_ID, admit.PID}
BASE = 20000 * DAY + 3600   # 01:00 UTC of some day: a whole UTC day ahead of every step below


class Mode(Spool):
    """admit.mode in hub.json (spec B6, §3.3): auto (the default) counts with flags until the hub's own window is clean —
    at least CLEAN_DAYS of the last WINDOW_DAYS UTC days with checked facts and not one flag — and then refuses, and
    stays refusing; flag is respected always; refuse refuses at once. Writers outside the core (a hub owner's own
    scripts and modules) that put a named fact without a channel are flagged in flag mode, refused in refuse mode, and
    keep an auto hub in flag mode."""
    # writers outside the core: a named fact without a channel from a script, a sync process, a local gateway
    FOREIGN = (("chat.mirrored", None, {"text": "x"}, "cli"), ("owner.peers", None, {"peers": []}, "hub-sync"),
               ("gateway.upstream", None, {"url": "http://203.0.113.7"}, "gateway"))

    def setUp(self):
        super().setUp()
        self.now = [BASE]
        self.hub.clock = lambda: self.now[0]
        self.hub.config = {}

    def set(self, value):
        self.hub.config = {} if value is None else {"admit": {"mode": value}}
        self.hub.settle_admit()

    def mode(self):
        return admit.mode(self.hub.state)

    def rejected(self, fact):
        """Refusals of the check (with its reason code); this core's fold refuses the foreign types themselves too —
        they are unknown to it, unlike to the core of the hub that writes them."""
        return [e["data"] for e in journal.read(self.hub.journal.path) if e["type"] == "core.rejected"
                and e["data"].get("of") == fact["seq"] and e["data"].get("code") in CODES]

    def legal(self):
        self.put("session.gist", "maps", {"text": "x", **self.as_session("maps")}, src="session")

    def days(self, n, foreign_every=0):
        """n days of mailbox traffic, the count written each day (as the tick does); a foreign writer every few days."""
        for day in range(n):
            self.legal()
            if foreign_every and day % foreign_every == 0:
                self.put(*self.FOREIGN[0][:3], src=self.FOREIGN[0][3])
            self.hub.count_checked(force=True)
            self.hub.settle_admit()
            if day < n - 1:
                self.now[0] += DAY

    def test_the_setting_from_hub_json(self):
        for cfg, want in (({}, "auto"), ({"admit": {}}, "auto"), ({"admit": {"mode": "auto"}}, "auto"),
                          ({"admit": {"mode": "flag"}}, "flag"), ({"admit": {"mode": "refuse"}}, "refuse"),
                          # broken — only counting: a typo never starts refusing
                          ({"admit": {"mode": "Refuse"}}, "flag"), ({"admit": {"mode": None}}, "flag"),
                          ({"admit": {"mode": 1}}, "flag"), ({"admit": "refuse"}, "flag"), ({"admit": None}, "flag"),
                          (None, "auto")):
            self.assertEqual(admit.setting(cfg), want, cfg)

    def test_foreign_writers_pass_with_a_flag_in_flag_mode(self):
        for setting in (None, "flag"):
            self.set(setting)
            for kind, entity, data, src in self.FOREIGN:
                fact, flag = self.put(kind, entity, data, src=src)
                self.assertEqual((flag["code"], flag["type"], flag["src"]), (admit.NO_CHANNEL, kind, src))
                self.assertEqual(self.rejected(fact), [], kind)
        self.assertEqual(self.mode(), "flag")

    def test_explicit_refuse_refuses_foreign_writers_and_forgeries(self):
        self.set("refuse")
        self.assertEqual(self.mode(), "refuse")
        for kind, entity, data, src in self.FOREIGN:
            fact, flag = self.put(kind, entity, data, src=src)
            self.assertEqual(flag["code"], admit.NO_CHANNEL)   # still counted for the statistics
            self.assertEqual(len(self.rejected(fact)), 1, kind)
            self.assertIn("without a channel", self.rejected(fact)[0]["reason"])
        fact, flag = self.put("schedule.set", "sys-job", {"session": "maps", "at": BASE + 3600, "prompt": "x",
                                                          "channel": "core"})
        self.assertEqual(flag["code"], admit.CORE_CHANNEL)
        self.assertNotIn("sys-job", self.hub.state["schedule"])   # refused: the fold did not apply it
        fact, flag = self.put("message.received", None, {"to": "maps", "text": "x", **self.as_session("ghost")})
        self.assertNotIn(f"m{fact['seq']}", self.hub.state["messages"])
        # legitimate writers are not touched
        fact, flag = self.put("message.received", None, {"to": "maps", "text": "x", **OWNER})
        self.assertEqual((flag, self.rejected(fact)), (None, []))
        self.assertIn(f"m{fact['seq']}", self.hub.state["messages"])

    def test_the_cli_learns_of_the_refusal(self):
        self.set("refuse")
        req = "r-admit-1"
        fact, _ = self.put("message.received", None, {"to": "maps", "text": "x", "req": req})
        self.assertEqual(self.rejected(fact)[0].get("req"), req)

    def test_auto_refuses_after_a_clean_window(self):
        self.days(admit.CLEAN_DAYS - 1)
        self.assertEqual(self.mode(), "flag")   # a day short
        fact, flag = self.put(*self.FOREIGN[1][:3], src=self.FOREIGN[1][3])
        self.assertEqual((flag["code"], self.rejected(fact)), (admit.NO_CHANNEL, []))
        self.now[0] += DAY
        self.days(1)
        self.assertEqual(self.mode(), "flag")   # that flag holds the window
        self.now[0] += admit.WINDOW_DAYS * DAY   # its day leaves the window, and so do the others
        self.days(admit.CLEAN_DAYS)
        self.assertEqual(self.mode(), "refuse")
        switched = [e for e in journal.read(self.hub.journal.path) if e["type"] == "spool.mode"]
        self.assertEqual([(e["data"]["mode"], e["data"]["set"]) for e in switched], [("refuse", "auto")])
        for kind, entity, data, src in self.FOREIGN:
            fact, flag = self.put(kind, entity, data, src=src)
            self.assertEqual(len(self.rejected(fact)), 1, kind)

    def test_a_hub_with_its_own_writers_stays_in_flag_mode(self):
        self.days(3 * admit.WINDOW_DAYS, foreign_every=3)   # a foreign writer that speaks every few days
        self.assertEqual(self.mode(), "flag")
        self.assertFalse([e for e in journal.read(self.hub.journal.path) if e["type"] == "spool.mode"])
        for kind, entity, data, src in self.FOREIGN:
            fact, flag = self.put(kind, entity, data, src=src)
            self.assertEqual((flag["code"], self.rejected(fact)), (admit.NO_CHANNEL, []), kind)

    def test_auto_does_not_relax_by_itself_and_explicit_flag_wins(self):
        self.days(admit.CLEAN_DAYS)
        self.assertEqual(self.mode(), "refuse")
        for _ in range(3):   # refused facts are counted as flags — they do not take an auto hub back to flag mode
            self.put(*self.FOREIGN[0][:3], src=self.FOREIGN[0][3])
            self.now[0] += DAY
            self.days(1)
        self.assertEqual(self.mode(), "refuse")
        self.set("flag")   # the owner's way out: respected at once, whatever the window
        self.assertEqual(self.mode(), "flag")
        fact, flag = self.put(*self.FOREIGN[0][:3], src=self.FOREIGN[0][3])
        self.assertEqual(self.rejected(fact), [])
        self.set(None)   # back to auto: the window has to be clean again
        self.assertEqual(self.mode(), "flag")
        self.now[0] += admit.WINDOW_DAYS * DAY
        self.days(admit.CLEAN_DAYS)
        self.assertEqual(self.mode(), "refuse")
        self.set("flag")
        self.days(admit.WINDOW_DAYS)
        self.assertEqual(self.mode(), "flag")   # explicit flag holds over any clean window

    def test_unreadable_settings_change_nothing(self):
        self.set("refuse")
        self.hub.settings_known, self.hub.config = False, {}
        self.hub.settle_admit()
        self.assertEqual(self.mode(), "refuse")
        self.hub.settings_known = True
        self.set("flag")
        self.hub.settings_known, self.hub.config = False, {"admit": {"mode": "refuse"}}
        self.hub.settle_admit()
        self.assertEqual(self.mode(), "flag")

    def test_the_count_lives_in_the_core_state_and_folds_from_the_journal(self):
        from core import reducer
        self.days(admit.CLEAN_DAYS)
        self.assertEqual(self.mode(), "refuse")
        evs = journal.read(self.hub.journal.path)
        self.assertEqual(reducer.replay(evs)["admit"], self.hub.state["admit"])
        self.hub.save_state()
        again = Hub(self.state, types.SimpleNamespace(poll=lambda: []))   # a restart keeps the mode and the window
        self.assertEqual(admit.mode(again.state), "refuse")
        self.assertEqual(again.state["admit"], self.hub.state["admit"])
        # the window keeps only what can still count: a few days of buckets, not the hub's whole history
        self.now[0] += 30 * DAY
        self.days(2)
        self.assertLessEqual(len(self.hub.state["admit"]["days"]), admit.WINDOW_DAYS + 1)

    def test_the_count_carries_the_mode(self):
        self.set("refuse")
        self.legal()
        self.hub.count_checked(force=True)
        got = [e["data"] for e in journal.read(self.hub.journal.path) if e["type"] == "spool.checked"]
        self.assertEqual((got[-1]["mode"], got[-1]["set"]), ("refuse", "refuse"))

    def test_the_checks_own_facts_are_the_cores_own(self):
        # a mailbox file of the check's own types — even one calling itself src core — is the spool.refused stub, as core.*:
        # the fold keeps the window and the mode from them
        self.assertIn("spool.mode", admit.CORE_ONLY)
        for src in ("cli", "core"):
            for kind, data in (("spool.mode", {"mode": "refuse", "set": "auto"}), ("spool.checked", {"n": 5, "since": BASE}),
                               ("spool.flagged", {"type": "x", "code": "y"})):
                fact, flag = self.put(kind, None, {**data, **OWNER}, src=src)
                self.assertEqual((fact["type"], fact["data"]["type"]), ("spool.refused", kind))
                # the attempt is still a flag under its own type: it holds the auto window and shows in statistics
                self.assertEqual((flag["type"], flag["code"]), (kind, admit.CORE_ONLY_CODE))
                rej = [e["data"] for e in journal.read(self.hub.journal.path)
                       if e["type"] == "core.rejected" and e["data"]["of"] == fact["seq"]]
                self.assertEqual(len(rej), 1)
                self.assertIn("only the core writes", rej[0]["reason"])
        self.assertEqual(self.mode(), "flag")
        # only the core's own count: six attempts checked and flagged; not the forged n=5 and not the forged flags
        self.assertEqual(self.hub.state["admit"]["days"], {str(BASE // DAY): [6, 6]})
        self.set("refuse")   # refuse mode: the stub is the one refusal, the attempt still flagged
        fact, flag = self.put("spool.mode", None, {"mode": "flag", "set": "flag", **OWNER}, src="core")
        self.assertEqual(flag["code"], admit.CORE_ONLY_CODE)
        self.assertEqual(len([e for e in journal.read(self.hub.journal.path)
                              if e["type"] == "core.rejected" and e["data"]["of"] == fact["seq"]]), 1)
        self.assertEqual(self.mode(), "refuse")
        # an older core wrote such a file as is (src from the file): its replay neither switches nor counts
        from core import reducer
        st = reducer.replay([{"seq": 1, "ts": BASE, "type": "spool.mode", "src": "cli", "entity": None,
                              "data": {"mode": "refuse", "set": "auto"}},
                             {"seq": 2, "ts": BASE, "type": "spool.checked", "src": "cli", "entity": None,
                              "data": {"n": 5, "since": BASE}}])
        self.assertEqual(admit.mode(st), "flag")
        self.assertFalse((st.get("admit") or {}).get("days"))

    def test_a_refusal_the_owner_took_off_goes_by_the_window(self):
        self.put(*self.FOREIGN[0][:3], src=self.FOREIGN[0][3])   # a flag in the window
        self.set("refuse")
        self.set(None)
        self.assertEqual(self.mode(), "flag")   # the window is not clean — auto does not keep the owner's refusal
        self.now[0] += admit.WINDOW_DAYS * DAY
        self.set("refuse")
        self.days(admit.CLEAN_DAYS)
        self.set(None)   # a clean window — auto refuses, and now holds it as its own
        self.assertEqual(self.mode(), "refuse")
        self.put(*self.FOREIGN[0][:3], src=self.FOREIGN[0][3])
        self.now[0] += DAY
        self.days(1)
        self.assertEqual(self.mode(), "refuse")
        modes = [(e["data"]["mode"], e["data"]["set"]) for e in journal.read(self.hub.journal.path)
                 if e["type"] == "spool.mode"]
        self.assertEqual(modes, [("refuse", "refuse"), ("flag", "auto"), ("refuse", "refuse"), ("refuse", "auto")])

    def test_a_setting_change_is_written_even_when_the_mode_stays(self):
        # a hub that never touched the setting writes no spool.mode until the window switches it (journals stay as they
        # were); the owner's explicit flag is written at once, though the mode was flag already
        self.days(admit.CLEAN_DAYS - 1)
        self.hub.load_catalog()
        self.assertFalse([e for e in journal.read(self.hub.journal.path) if e["type"] == "spool.mode"])
        self.set("flag")
        self.set("flag")
        self.assertEqual([(e["data"]["mode"], e["data"]["set"]) for e in journal.read(self.hub.journal.path)
                          if e["type"] == "spool.mode"], [("flag", "flag")])
        self.assertEqual(self.hub.state["admit"]["set"], "flag")


class Census(unittest.TestCase):
    """The literal types of mailbox writers in the tree (spool.put, put, _put) do not write what is "core only", and a
    writer without a channel writes only a type from the "no name" list. Dynamic writers — the type is not a literal:
    hubmod.fact (the module's type, the core sets the channel and the sender), modrun.emit (module.error outside hubd —
    no channel), the hook (types from translate — LegalWriters), evals/driver.py (from a case — writes core types
    straight into the journal)."""

    TYPE = re.compile(r"[a-z_]+(?:\.[a-z_]+)+")

    @staticmethod
    def _own(scope, name):
        """A def name directly in the body of scope (module, class, function), or None."""
        return next((n for n in getattr(scope, "body", []) if isinstance(n, FUNCS) and n.name == name), None)

    def target(self, node, chain):
        """The wrapper a put/_put call invokes: a def by scope; a function parameter or something foreign — None."""
        fn = node.func
        if isinstance(fn, ast.Attribute):   # self.put — a method of the enclosing class
            if getattr(fn.value, "id", None) != "self":
                return None
            cls = next((c for c in reversed(chain) if isinstance(c, ast.ClassDef)), None)
            return cls and self._own(cls, fn.attr)
        for scope in reversed(chain):
            if isinstance(scope, FUNCS) and fn.id in [a.arg for a in scope.args.posonlyargs + scope.args.args
                                                      + scope.args.kwonlyargs + [scope.args.vararg, scope.args.kwarg] if a]:
                return None   # put is passed as a parameter (bin/hub supplies its own) — cannot tell
            if not isinstance(scope, ast.ClassDef) and self._own(scope, fn.id):
                return self._own(scope, fn.id)
        return None

    def names_channel(self, node, chain):
        """Whether the call names a channel: True, False or None (cannot tell from the code — data is not a literal, a foreign wrapper)."""
        fn = node.func
        if isinstance(fn, ast.Attribute) and getattr(fn.value, "id", None) == "spool":
            data = node.args[3] if len(node.args) > 3 else next((k.value for k in node.keywords if k.arg == "data"),
                                                               None)
            if data is None:
                return False
            if not isinstance(data, ast.Dict):
                return None
            if any(isinstance(k, ast.Constant) and k.value == "channel" for k in data.keys):
                return True
            return None if any(k is None for k in data.keys) else False   # {**x, …}: what is in x is not a literal
        wrapper = self.target(node, chain)
        return None if wrapper is None else self.sets_channel(wrapper)

    @staticmethod
    def sets_channel(wrapper):
        """The wrapper sets the channel: a "channel" key in a dict, data["channel"] = …, channel=… (data.update) — by
        code, not by text (a mention in a docstring does not count)."""
        for n in ast.walk(wrapper):
            if isinstance(n, ast.Dict) and any(isinstance(k, ast.Constant) and k.value == "channel" for k in n.keys):
                return True
            if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Store) and \
                    isinstance(n.slice, ast.Constant) and n.slice.value == "channel":
                return True
            if isinstance(n, ast.keyword) and n.arg == "channel":
                return True
        return False

    def calls(self, node, chain):
        if isinstance(node, ast.Call):
            yield node, chain
        inner = chain + [node] if isinstance(node, (ast.Module, ast.ClassDef) + FUNCS) else chain
        for child in ast.iter_child_nodes(node):
            yield from self.calls(child, inner)

    def writers(self):
        files = [os.path.join(HERE, "bin", "hub")]
        for top in ("core", "modules", "hooks", "hosts", "evals", "bin"):
            for root, _, names in os.walk(os.path.join(HERE, top)):
                files += [os.path.join(root, n) for n in names if n.endswith(".py")]
        for path in files:
            with open(path, encoding="utf-8") as f:
                src = f.read()
            for node, chain in self.calls(ast.parse(src, path), []):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
                if name not in ("put", "_put"):
                    continue
                lits = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
                kind = next((x for x in lits if self.TYPE.fullmatch(x)), None)
                if kind:
                    yield os.path.relpath(path, HERE), node.lineno, kind, self.names_channel(node, chain)

    def test_no_writer_puts_a_core_only_type(self):
        found = list(self.writers())
        self.assertGreater(len(found), 50)   # the census sees writers, not an empty set
        self.assertIn("hub.restart_requested", {w[2] for w in found})
        bad = [w for w in found if w[2] in admit.CORE_ONLY]
        self.assertEqual(bad, [])

    def test_a_writer_without_a_channel_writes_only_unnamed_types(self):
        found = list(self.writers())
        quiet = [w for w in found if w[3] is False]
        self.assertIn("schedule.due", {w[2] for w in quiet})   # the clock: a wrapper without a channel is recognized
        bad = [w for w in quiet if w[2] not in admit.UNNAMED and not w[2].startswith(admit.UNNAMED_PREFIXES)]
        self.assertEqual(bad, [])

    def test_a_wrapper_names_a_channel_by_code_not_by_text(self):
        tree = ast.parse('''
def quiet(type, data):
    """Without "channel": the caller sets it."""
    spool.put(box, type, None, data)
def loud(type, data):
    spool.put(box, type, None, {**data, "channel": "cli"})
def update(type, data):
    data.update(channel="cli")
def item(type, data):
    data["channel"] = "cli"
''')
        self.assertEqual([self.sets_channel(f) for f in tree.body], [False, True, True, True])

    def test_the_lists_do_not_overlap(self):
        self.assertEqual(admit.CORE_ONLY & admit.UNNAMED, frozenset())
        self.assertFalse([t for t in admit.CORE_ONLY if t.startswith("core.")])   # core.* — the spool.refused stub


if __name__ == "__main__":
    unittest.main()
