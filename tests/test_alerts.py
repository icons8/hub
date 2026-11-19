"""Alert reasons (push spec, §2.1): what the hub can tell a phone — a question and a tool request from the session
state, and an `owner.notify` notification from the core's `notices` registry. A notification remembers its sender by the
labels that `put` (bin/hub) and `hubmod.fact` set, is updated under the same id, lives 24 h, with at most 50 open. The
snapshot carries `alerts`: the open reasons as one list, which the client uses to reconcile banners on opening."""
import tests  # noqa: F401 — hide the machine's settings
import unittest

from core import owner, reducer, views

ASK = {"request_id": "r1", "tool": "AskUserQuestion", "input": {"questions": [{"question": "Ok?", "header": "Rollout"}]},
       "since": 10}
CARD = {**ASK, "request_id": "r2", "id": "q7", "since": 20}
TOOL = {"tool": "jira", "state": "needs-owner", "since": 30, "why": "tasks"}

MOD = {"channel": "plugin", "owner": False, "sender": "mod:shop"}     # hubmod.fact / hub from a module process
JOB = {"channel": "cli", "owner": True, "source": "gate"}              # hub notify from a schedule job
OWNER = {"channel": "cli", "owner": True, "source": None}              # the owner's hub notify from a terminal
CORE = {"channel": "core", "owner": False, "source": "modules"}        # daemon.record (the module did not start)


def fold(*events, st=None):
    """Events in order: (type, data[, src]) → state and the reasons for rejections."""
    st = st or reducer.empty()
    why = []
    for i, e in enumerate(events, 1):
        kind, data, src = (*e, "cli")[:3]
        st, _, w = reducer.reduce(st, {"seq": st["seq"] + 1, "ts": e[3] if len(e) > 3 else 1000 + i, "type": kind,
                                       "entity": None, "data": data, "src": src})
        why.append(w)
    return st, why


def note(text="disk is full", **kw):
    return {"text": text, **kw}


class Notices(unittest.TestCase):
    def test_a_plugin_process_cannot_take_a_core_name(self):
        # a plugin process is named after its folder: "core" or "owner" must not become the sender of the core or the owner
        st, _ = fold(("owner.notify", note(id="config", channel="plugin", owner=False, sender="core"), "cli"))
        self.assertEqual(list(st["notices"]), ["n:ext:core:config"])
        st["notices"]["n:core:config"] = {**st["notices"]["n:ext:core:config"], "id": "n:core:config", "sender": "core"}
        st, (why,) = fold(("owner.notify_resolved", {"id": "config", "channel": "plugin", "owner": False,
                                                     "sender": "core"}, "cli"), st=st)
        self.assertIn("n:core:config", st["notices"])          # its own ext:core:config is closed, the core's is not
        self.assertNotIn("n:ext:core:config", st["notices"])

    def test_an_id_like_a_full_one_is_refused(self):
        _, (why,) = fold(("owner.notify", note(id="n:1", **MOD), "module"))
        self.assertIsNotNone(why)

    def test_a_due_time_out_of_range_is_refused(self):
        for due in (5e10, -1, float("nan"), True):
            _, (why,) = fold(("owner.notify", note(urgency="now", due=due, **MOD), "module"))
            self.assertIsNotNone(why, due)

    def test_the_sender_comes_from_the_labels_put_and_hubmod_set(self):
        st, why = fold(("owner.notify", note(id="a", **MOD), "module"), ("owner.notify", note(id="a", **JOB)),
                       ("owner.notify", note(id="a", **OWNER)), ("owner.notify", note(id="a", **CORE), "core"),
                       ("owner.notify", note(id="a", channel="cli", owner=True, source="stats"), "stats"))
        self.assertEqual(why, [None] * 5)
        self.assertEqual(set(st["notices"]), {"n:mod:shop:a", "n:schedule:gate:a", "n:owner:a", "n:core:a",
                                              "n:stats:a"})

    def test_without_an_id_the_seq_is_the_id_and_with_the_same_id_it_is_an_update(self):
        st, _ = fold(("owner.notify", note(**OWNER)), ("owner.notify", note("first", id="disk", **MOD), "module"),
                     ("owner.notify", note("second", id="disk", **MOD), "module"))
        self.assertIn("n:1", st["notices"])
        n = st["notices"]["n:mod:shop:disk"]
        self.assertEqual((n["text"], n["v"], n["since"]), ("second", 3, 1002))   # v grows, the start stays
        self.assertEqual(len(st["notices"]), 2)

    def test_sessions_still_do_not_notify_and_bad_fields_are_refused(self):
        st, why = fold(("owner.notify", note(id="a", channel="session", owner=False, sender="w"), "session"),
                       ("owner.notify", note(id="x" * 129, **OWNER)), ("owner.notify", note(id=5, **OWNER)),
                       ("owner.notify", note(session="nobody", **OWNER)),
                       ("owner.notify", note(push="no", **OWNER)))
        self.assertTrue(all(why), why)
        self.assertEqual(st.get("notices") or {}, {})

    def test_session_and_push_are_kept(self):
        st = reducer.empty()
        st["sessions"]["maps"] = {"name": "maps", "state": "idle", "since": 1}
        st, why = fold(("owner.notify", note(id="a", session="maps", push=False, **MOD), "module"), st=st)
        self.assertEqual(why, [None])
        n = st["notices"]["n:mod:shop:a"]
        self.assertEqual((n["session"], n["push"]), ("maps", False))

    def test_at_most_fifty_open_the_oldest_goes_and_a_day_old_one_is_dropped(self):
        st, _ = fold(*[("owner.notify", note(id=str(i), **OWNER)) for i in range(owner.NOTICE_MAX + 3)])
        self.assertEqual(len(st["notices"]), owner.NOTICE_MAX)
        self.assertNotIn("n:owner:0", st["notices"])
        self.assertIn(f"n:owner:{owner.NOTICE_MAX + 2}", st["notices"])
        # a day later a new notification pushes the old ones out (the fold goes by event ts, not by the clock)
        st, _ = fold(("owner.notify", note(id="late", **OWNER), "cli", 1000 + owner.NOTICE_TTL + 100), st=st)
        self.assertEqual(list(st["notices"]), ["n:owner:late"])

    def test_an_old_journal_notify_without_id_is_a_closed_notice_after_a_day(self):
        # old owner.notify events (without id) on replay are n:<seq> notifications; those older than a day are not in alerts
        st, _ = fold(("owner.notify", note(**OWNER)))
        self.assertEqual([a["id"] for a in owner.alerts(st, 1001 + 60)], ["n:1"])
        self.assertEqual(owner.alerts(st, 1001 + owner.NOTICE_TTL + 1), [])


class Resolve(unittest.TestCase):
    """owner.notify_resolved {id} (§2.6): the same sender, the hub or the owner may close it; someone else's — rejected."""

    def setUp(self):
        st = reducer.empty()
        st["sessions"]["hub"] = {"name": "hub", "kind": "hub", "state": "idle", "since": 1}
        st["sessions"]["w"] = {"name": "w", "state": "idle", "since": 1}
        self.st, _ = fold(("owner.notify", note(id="disk", **MOD), "module"), st=st)

    def resolve(self, data, src="cli"):
        st, (why,) = fold(("owner.notify_resolved", data, src), st=self.st)
        return "n:mod:shop:disk" in st["notices"], why

    def test_the_same_sender_closes_its_own(self):
        self.assertEqual(self.resolve({"id": "disk", **MOD}, "module"), (False, None))

    def test_another_module_cannot_close_it_and_its_own_namespace_has_no_such(self):
        other = {**MOD, "sender": "mod:spy"}
        self.assertEqual(self.resolve({"id": "disk", **other}, "module"), (True, None))   # its own "disk" — there is none
        still, why = self.resolve({"id": "n:mod:shop:disk", **other}, "module")
        self.assertTrue(still)
        self.assertIsNotNone(why)

    def test_the_owner_and_the_hub_close_any_by_its_full_id(self):
        self.assertEqual(self.resolve({"id": "n:mod:shop:disk", **OWNER}), (False, None))
        self.assertEqual(self.resolve({"id": "n:mod:shop:disk", "channel": "session", "owner": False, "sender": "hub"},
                                      "session"), (False, None))

    def test_a_session_that_is_not_the_hub_cannot(self):
        still, why = self.resolve({"id": "n:mod:shop:disk", "channel": "session", "owner": False, "sender": "w"},
                                  "session")
        self.assertTrue(still)
        self.assertIsNotNone(why)

    def test_a_closed_notice_is_out_of_the_alerts_and_the_same_id_opens_it_again(self):
        st, _ = fold(("owner.notify_resolved", {"id": "disk", **MOD}, "module"),
                     ("owner.notify", note("again", id="disk", **MOD), "module"), st=self.st)
        (a,) = [a for a in owner.alerts(st, 1100) if a["kind"] == "notice"]
        self.assertEqual(a["since"], 1002)                    # a new notification, not a continuation of the closed one

    def test_a_module_without_core_write_may_write_it(self):
        import types
        from core import corewrite
        hub = types.SimpleNamespace(state_dir="/nonexistent", state={}, clock=lambda: 0,
                                    modules=types.SimpleNamespace(written=None, found=[]))
        cw = corewrite.CoreWrite(hub)
        self.assertEqual(cw.admit({"module": "shop", "type": "owner.notify_resolved"}), (None, None))
        self.assertIsNotNone(cw.admit({"module": "shop", "type": "session.gist"})[1])

    def test_the_core_closes_its_notice_when_the_module_comes_up(self):
        st, _ = fold(("owner.notify", note(id="module:shop", **CORE), "core"))
        st, (why,) = fold(("module.ready", {"module": "shop", "version": "1", "points": [], "channel": "core"}, "core"),
                          st=st)
        self.assertIsNone(why)
        self.assertNotIn("n:core:module:shop", st["notices"])

    def test_module_api_has_the_event(self):
        from core import version
        self.assertIn("2.5", version.MODULE_POINTS)   # the event exists since 2.5; the hub version is not lower


class Alerts(unittest.TestCase):
    def state(self, **sess):
        st = reducer.empty()
        st["sessions"] = {n: {"name": n, "since": 1, "state": "idle", **s} for n, s in sess.items()}
        return st

    def test_questions_cards_tools_and_notices_in_one_list(self):
        st = self.state(maps={"pending": [ASK], "asks": [CARD], "requests": [TOOL]})
        st, _ = fold(("owner.notify", note(id="a", **MOD), "module"),
                     ("owner.notify", note(id="b", push=False, **MOD), "module"), st=st)
        got = {a["id"]: a for a in owner.alerts(st, 100)}
        self.assertEqual(set(got), {"q:maps:r1", "q:maps:r2", "q:maps:tool:jira", "n:mod:shop:a"})   # push:false — not there
        self.assertEqual({k: got["q:maps:r2"][k] for k in ("kind", "session", "card", "since", "v")},
                         {"kind": "question", "session": "maps", "card": "q7", "since": 20, "v": 0})
        self.assertNotIn("card", got["q:maps:r1"])          # a turn form is not a card: no number
        self.assertEqual(got["q:maps:tool:jira"]["kind"], "tool")
        self.assertEqual((got["n:mod:shop:a"]["kind"], got["n:mod:shop:a"]["v"]), ("notice", 1))
        self.assertTrue(all(a["title"] for a in got.values()))

    def test_a_reminder_raises_v(self):
        st = self.state(maps={"pending": [ASK]})
        st["owner"]["reminders"]["q:maps:r1"] = 2
        (a,) = owner.alerts(st, 100)
        self.assertEqual(a["v"], 2)

    def test_a_removed_session_has_no_alerts_and_a_stale_card_is_gone(self):
        st = self.state(gone={"state": "removed", "asks": [CARD]}, maps={"asks": [CARD]})
        self.assertEqual([a["id"] for a in owner.alerts(st, 100)], ["q:maps:r2"])
        self.assertEqual(owner.alerts(st, 20 + owner.ASK_TTL + 1), [])

    def test_the_snapshot_does_not_compute_them_the_push_module_does(self):
        # building the push text for every event costs extra milliseconds when pushes are off: Tail computes it on a module's request
        import os
        import tempfile
        from core import journal, tail
        st = self.state(maps={"pending": [ASK]})
        r = views.Reader()
        r.state = st
        self.assertNotIn("alerts", r.snapshot(now=100))
        d = tempfile.mkdtemp()
        j = journal.Journal(os.path.join(d, "journal.jsonl"))
        j.append("session.requested", "maps", {"cwd": d, "channel": "cli", "owner": True})
        t = tail.Tail(j.path)
        t.poll()
        self.assertEqual(t.alerts_now(), owner.alerts(t.reader.state, 10 ** 10))


if __name__ == "__main__":
    unittest.main()
