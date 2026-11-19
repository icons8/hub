"""Module API 2.6: a wider module state slice — session row fields (the sessions right), Claude account limits (the
accounts right), whom the owner's messages are addressed to and what counted as an answer (the messages right). No
texts, no tokens, no paths; parts nobody has the right to are not built; without the slice file
hubmod.accounts/owner_message return None, not the whole hub state."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import re
import unittest
from unittest import mock

from core import hubmod, journal, langdata, modrun, modstate, modules, owner, version

OWNER = {"channel": "web", "owner": True}
SECRETS = ("/home/x", "s-maps", "owner text", "session question", "line quote", "tok-fp-1", "Fix the test",
           "what we wait for")

# slice keys by allowlist: at every level only these (leaf values are numbers, flags, names, ids, time)
ROW = {"state", "parked", "asleep", "busy", "model", "retired", "owner_msg", "asks", "forms", "reacts", "hub_wait",
       "limit_cut", "raised_by", "since", "worktree"}


def _env(case, **env):
    old = {k: os.environ.get(k) for k in env}
    case.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                             for k, v in old.items()])
    for k, v in env.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


class Views(unittest.TestCase):
    def setUp(self):
        from tests.fakes.modstand import Stand
        self.s = Stand(self, extra=[{"name": "plain"}, {"name": "acc"}, {"name": "msgs"}])
        self.manifest("fake", ["sessions"])
        self.manifest("acc", ["sessions", "accounts"], api="^2.6")
        self.manifest("msgs", ["messages"], api="^2.6")
        self.s.hub.modules.reload(self.s.cfg, {})
        _env(self, HUB_MODULE="fake", HUB_STATE_DIR=self.s.state_dir, HUB_MODULE_LAUNCH=None)
        self.s.hub.modules.tick()
        h = self.s.hub
        for n in ("maps", "docs"):
            h.record("session.requested", n, {"cwd": "/home/x/" + n, "session_id": "s-" + n, **OWNER})
        h.record("message.received", "maps", {"to": "maps", "text": "owner text", **OWNER})
        self.mid = next(m for m, r in h.state["messages"].items() if r.get("from_owner"))

    def manifest(self, name, caps, api="^2.4"):
        path = os.path.join(self.s.root, name, "module.json")
        with open(path, encoding="utf-8") as f:
            man = json.load(f)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({**man, "api": api, "capabilities": caps}, f)

    def file(self, name="fake"):
        with open(os.path.join(modrun.run_dir(self.s.state_dir), f"{name}.state.json"), encoding="utf-8") as f:
            return json.load(f)

    def fill(self):
        """Everything the new fields carry — in the form the fold puts it — and an event: the slice is built from the result."""
        h = self.s.hub
        maps = h.state["sessions"]["maps"]
        maps["state"] = "retired"
        maps["asks"] = [{"id": "q7", "request_id": "r-1", "tool": "AskUserQuestion",
                         "input": {"questions": [{"question": "session question"}]}, "interactive": True, "since": 500.0}]
        maps["pending"] = [{"request_id": "r-2", "tool": "Bash", "input": {"command": "Fix the test"},
                            "interactive": True, "since": 600.0}]
        maps["reacts"] = {"41": {"emoji": "👍", "quote": "line quote"}}
        maps["limit_cut"] = {"ts": 700.0, "account": "a@x", "gen": 3, "marker": "m1", "task": "d9", "resets": 9000.0}
        maps["account"] = "a@x"
        maps.update(raised_by="docs", since=650.0,
                    worktree={"path": "/home/x/maps-copy", "branch": "task/maps", "base": "main", "repo": "/home/x/repo"})
        h.state.setdefault("schedule", {})["maps-wait-ab12-1"] = {
            "id": "maps-wait-ab12-1", "state": "active", "session": "maps", "by": "maps", "prompt": "what we wait for",
            "command": "", "kind": "every", "every": 60, "wait": "what we wait for"}
        h.state["account"] = {"active": "a@x", "blocked": {"b@x": 9e9}, "since": 1.0, "pinned": True,
                              "tokens": {"a@x": "tok-fp-1"}, "refreshed": {"a@x": 5.0},
                              "seen": {"a@x": {"status": "allowed", "type": "five_hour",
                                               "windows": {"five_hour": {"used": 0.3, "resets": 2e9, "note": "x"}},
                                               "ts": 100.0, "extra": "tok-fp-1"}}}
        h.state["messages"][self.mid].update(answers_question="q7", answered_form="r-1")
        h.record("session.waiting", "docs", {"text": "waiting", **OWNER})

    def test_the_rights_are_of_api_2_6(self):
        for n in ("acc", "msgs"):
            self.assertEqual(modules.read(os.path.join(self.s.root, n), "path")["errors"], [])
        self.manifest("plain", ["accounts"], api="^2.5")
        self.assertTrue(modules.read(os.path.join(self.s.root, "plain"), "path")["errors"])
        self.manifest("plain", ["messages"], api="^2.5")
        self.assertTrue(modules.read(os.path.join(self.s.root, "plain"), "path")["errors"])
        self.assertGreaterEqual(version.parse_version(version.MODULE_API), version.parse_version("2.6"))
        self.assertEqual(version.manifest_since("accounts"), "2.6")
        self.assertEqual(version.manifest_since("messages"), "2.6")

    def test_the_session_row_carries_the_new_fields(self):
        self.fill()
        maps = self.file()["sessions"]["maps"]
        self.assertTrue(maps["retired"])
        self.assertEqual(maps["owner_msg"], {"id": self.mid, "ts": self.s.hub.state["messages"][self.mid]["created"]})
        self.assertEqual(maps["asks"], [{"id": "q7", "since": 500.0, "until": 500.0 + owner.ASK_TTL}])
        self.assertEqual(maps["forms"], [{"id": "r-2", "tool": "Bash", "since": 600.0}])
        self.assertEqual(maps["reacts"], {"41": "👍"})
        self.assertTrue(maps["hub_wait"])
        self.assertEqual(maps["limit_cut"], {"ts": 700.0, "resets": 9000.0})   # without the accounts right — no account
        self.assertNotIn("account", maps)
        self.assertEqual((maps["raised_by"], maps["since"], maps["worktree"]),
                         ("docs", 650.0, {"branch": "task/maps", "base": "main"}))   # no path of the copy or the repo
        docs = self.file()["sessions"]["docs"]   # the fields are always present: empty means null, [], {}, false
        self.assertEqual({k: docs[k] for k in ROW - {"state", "parked", "asleep", "busy", "model", "since"}},
                         {"retired": False, "owner_msg": None, "asks": [], "forms": [], "reacts": {},
                          "hub_wait": False, "limit_cut": None, "raised_by": None, "worktree": None})
        self.assertIsInstance(docs["since"], float)   # the time its state began (it is always in a live session)

    def test_a_finished_wait_and_another_sessions_wait_do_not_count(self):
        self.fill()
        sched = self.s.hub.state["schedule"]
        sched["maps-wait-ab12-1"]["state"] = "done"
        sched["docs-check"] = {"id": "docs-check", "state": "active", "session": "docs", "command": ""}
        self.s.hub.record("session.waiting", "docs", {"text": "waiting more", **OWNER})
        rows = self.file()["sessions"]
        self.assertFalse(rows["maps"]["hub_wait"])
        self.assertFalse(rows["docs"]["hub_wait"])   # an ordinary schedule job — not `hub wait`

    def test_accounts_right_gives_limits_and_the_account_of_a_session(self):
        self.fill()
        got = self.file("acc")
        self.assertEqual(got["accounts"], {
            "active": "a@x", "blocked": {"b@x": 9e9}, "exhausted": None,
            "seen": {"a@x": {"status": "allowed", "type": "five_hour",
                             "windows": {"five_hour": {"used": 0.3, "resets": 2e9}}, "ts": 100.0}}})
        self.assertEqual(got["sessions"]["maps"]["account"], "a@x")
        self.assertEqual(got["sessions"]["maps"]["limit_cut"], {"ts": 700.0, "resets": 9000.0, "account": "a@x"})
        self.assertIsNone(got["sessions"]["docs"]["account"])
        self.assertNotIn("owner_messages", got)
        self.assertNotIn("accounts", self.file())

    def test_messages_right_gives_who_and_what_was_answered(self):
        self.fill()
        got = self.file("msgs")
        rec = self.s.hub.state["messages"][self.mid]
        self.assertEqual(got["owner_messages"], {self.mid: {"to": "maps", "ts": rec["created"], "answers": "q7",
                                                            "form": "r-1"}})
        self.assertNotIn("sessions", got)
        self.assertNotIn("accounts", got)
        self.assertNotIn("owner_messages", self.file())
        _env(self, HUB_MODULE="msgs")
        self.assertEqual(hubmod.owner_message(self.mid)["answers"], "q7")
        self.assertIsNone(hubmod.owner_message("m999999"))

    def test_messages_of_sessions_and_the_core_are_not_there(self):
        h = self.s.hub
        h.record("message.received", "docs", {"to": "docs", "text": "from a session", "channel": "session",
                                              "sender": "maps"})
        self.fill()
        self.assertEqual(set(self.file("msgs")["owner_messages"]), {self.mid})
        self.assertIsNone(self.file()["sessions"]["docs"]["owner_msg"])

    def test_no_text_no_token_no_path_and_only_known_keys(self):
        self.fill()
        for name in ("fake", "acc", "msgs", "plain"):
            got = self.file(name)
            text = json.dumps(got, ensure_ascii=False)
            for secret in SECRETS:
                self.assertNotIn(secret, text, f"{name}: {secret}")
            self.assertLessEqual(set(got), {"ready", "mods", "others", "sessions", "renamed", "accounts",
                                            "owner_messages"}, name)
            for row in (got.get("sessions") or {}).values():
                self.assertLessEqual(set(row), ROW | {"account"}, name)
                for a in row["asks"]:
                    self.assertEqual(set(a), {"id", "since", "until"})
                for f in row["forms"]:
                    self.assertEqual(set(f), {"id", "tool", "since"})
                self.assertTrue(all(isinstance(v, str) and len(v) <= 16 for v in row["reacts"].values()))
                for x in (row["owner_msg"], row["limit_cut"]):
                    self.assertLessEqual(set(x or {}), {"id", "ts", "account", "resets"})
            acc = got.get("accounts")
            if acc is not None:
                self.assertEqual(set(acc), {"active", "blocked", "exhausted", "seen"})
                self.assertLessEqual(set(acc["exhausted"] or {}), {"until"})
                for v in acc["seen"].values():
                    self.assertLessEqual(set(v), {"status", "type", "windows", "ts"})
                    for w in v["windows"].values():
                        self.assertLessEqual(set(w), {"used", "resets"})
            for m in (got.get("owner_messages") or {}).values():
                self.assertEqual(set(m), {"to", "ts", "answers", "form"})
                self.assertRegex(str(m["answers"] or "q1"), r"^q\d+")
        self.assertNotIn("accounts", self.file("plain"))
        self.assertNotIn("sessions", self.file("plain"))
        self.assertNotIn("owner_messages", self.file("plain"))

    def test_without_a_right_no_method(self):
        _env(self, HUB_MODULE="fake")
        with self.assertRaises(PermissionError):
            hubmod.accounts()
        with self.assertRaises(PermissionError):
            hubmod.owner_message(self.mid)
        _env(self, HUB_MODULE="msgs")
        with self.assertRaises(PermissionError):
            hubmod.sessions()

    def test_without_the_file_none_and_not_the_hub_state(self):
        for name, caps, call in (("acc", ["accounts"], hubmod.accounts),
                                 ("msgs", ["messages"], lambda: hubmod.owner_message(self.mid))):
            _env(self, HUB_MODULE=name)
            self.assertIsNotNone(call())
            os.unlink(os.path.join(modrun.run_dir(self.s.state_dir), f"{name}.state.json"))
            with mock.patch.object(hubmod, "manifest", return_value={"capabilities": caps}), \
                    mock.patch.object(journal, "load_state", side_effect=AssertionError("the whole hub state")):
                self.assertIsNone(call())
            with mock.patch.object(hubmod, "manifest", return_value={"capabilities": []}), \
                    mock.patch.object(journal, "load_state", side_effect=AssertionError("the whole hub state")):
                with self.assertRaises(PermissionError):
                    call()

    def test_the_file_is_not_rewritten_without_a_change(self):
        self.fill()
        paths = {n: os.path.join(modrun.run_dir(self.s.state_dir), f"{n}.state.json") for n in ("fake", "acc", "msgs")}
        for p in paths.values():   # Windows: a rewrite within the same clock tick does not change mtime — set an old time
            os.utime(p, ns=(10 ** 18, 10 ** 18))
        before = {n: os.stat(p).st_mtime_ns for n, p in paths.items()}
        self.s.hub.record("session.waiting", "docs", {"text": "waiting", **OWNER})
        self.assertEqual({n: os.stat(p).st_mtime_ns for n, p in paths.items()}, before)
        self.s.hub.state["account"]["active"] = "b@x"   # limits only — only the file with the accounts right is rewritten
        self.s.hub.record("session.waiting", "docs", {"text": "waiting", **OWNER})
        after = {n: os.stat(p).st_mtime_ns for n, p in paths.items()}
        self.assertEqual((after["fake"], after["msgs"]), (before["fake"], before["msgs"]))
        self.assertNotEqual(after["acc"], before["acc"])
        self.assertEqual(self.file("acc")["accounts"]["active"], "b@x")

    def test_parts_without_a_right_are_not_built(self):
        for n in ("acc", "msgs"):
            self.manifest(n, [], api="^2.6")
        self.s.hub.modules.reload(self.s.cfg, {})
        self.s.hub.modules.tick()
        with mock.patch.object(modstate, "account_view", wraps=modstate.account_view) as acc, \
                mock.patch.object(modstate, "owner_index", wraps=modstate.owner_index) as idx:
            self.s.hub.record("session.waiting", "docs", {"text": "waiting", **OWNER})
        self.assertEqual(acc.call_count, 0)
        self.assertEqual(idx.call_count, 1)   # owner_msg of session rows — one pass per event
        self.manifest("fake", [])
        self.s.hub.modules.reload(self.s.cfg, {})
        self.s.hub.modules.tick()
        with mock.patch.object(modstate, "owner_index", wraps=modstate.owner_index) as idx, \
                mock.patch.object(modstate, "session_rows", wraps=modstate.session_rows) as rows:
            self.s.hub.record("session.waiting", "docs", {"text": "waiting", **OWNER})
        self.assertEqual((idx.call_count, rows.call_count), (0, 0))

    def test_each_part_is_built_once_for_all_modules(self):
        with mock.patch.object(modstate, "owner_index", wraps=modstate.owner_index) as idx, \
                mock.patch.object(modstate, "session_rows", wraps=modstate.session_rows) as rows, \
                mock.patch.object(modstate, "account_view", wraps=modstate.account_view) as acc:
            self.s.hub.record("session.waiting", "docs", {"text": "waiting", **OWNER})
        self.assertEqual((idx.call_count, rows.call_count, acc.call_count), (1, 1, 1))

    def test_a_right_added_later_gets_its_part_on_the_tick(self):
        self.manifest("plain", ["accounts"], api="^2.6")
        self.s.hub.modules.reload(self.s.cfg, {})
        self.s.hub.modules.tick()
        self.assertIn("accounts", self.file("plain"))
        self.assertNotIn("sessions", self.file("plain"))
        self.manifest("acc", ["sessions"], api="^2.6")   # the right is revoked — the part is gone, the rows have no account
        self.s.hub.modules.reload(self.s.cfg, {})
        self.s.hub.modules.tick()
        self.assertNotIn("accounts", self.file("acc"))
        self.assertNotIn("account", self.file("acc")["sessions"]["maps"])

    def test_all_busy_until(self):
        self.fill()
        self.s.hub.state["account"]["exhausted"] = {"until": 3e9}
        self.s.hub.record("session.waiting", "docs", {"text": "waiting", **OWNER})
        self.assertEqual(self.file("acc")["accounts"]["exhausted"], {"until": 3e9})

    def test_a_card_closed_or_declined_is_not_an_answer_as_in_the_core(self):
        """As in tools.card_answer: × is a core note, not the owner's word; «Deny» is his message, but not an answer."""
        self.fill()
        h = self.s.hub
        rec = h.state["messages"][self.mid]
        h.state["messages"]["m900"] = {**rec, "id": "m900", "text": "The owner closed your question q7", "created":
                                       rec["created"] + 10, "answers_question": "q7", "card_dismissed": True,
                                       "answered_form": None}
        h.state["messages"]["m901"] = {**rec, "id": "m901", "text": "old note", "created": rec["created"] + 20,
                                       "answers_question": "q8", "answered_form": None}
        h.state["messages"]["m901"]["text"] = langdata.legacy("dismissed") + " q8"   # an earlier core's note, before the mark
        h.state["messages"]["m902"] = {**rec, "id": "m902", "text": "declined", "created": rec["created"] + 5,
                                       "answers_question": "q9", "card_declined": True, "answered_form": None}
        h.record("session.waiting", "docs", {"text": "waiting", **OWNER})
        got = self.file("msgs")["owner_messages"]
        self.assertEqual(set(got), {self.mid, "m902"})
        self.assertIsNone(got["m902"]["answers"])
        self.assertEqual(self.file()["sessions"]["maps"]["owner_msg"]["id"], "m902")

    def test_a_right_the_slice_does_not_carry_yet_is_not_known_not_refused(self):
        """Rights were added to module.json, hubd has not yet rewritten the slice (tick) — None, not «no right»."""
        _env(self, HUB_MODULE="fake")
        with mock.patch.object(hubmod, "manifest", return_value={"capabilities": ["sessions", "messages"]}):
            self.assertIsNone(hubmod.owner_message(self.mid))
        with mock.patch.object(hubmod, "manifest", side_effect=OSError("no file")):
            with self.assertRaises(PermissionError):
                hubmod.owner_message(self.mid)

    def test_sessions_from_a_slice_written_before_the_right_are_read_as_without_a_slice(self):
        self.s.hub.save_state()
        _env(self, HUB_MODULE="msgs", PWD=None)
        with mock.patch.object(hubmod, "manifest", return_value={"capabilities": ["messages", "sessions"]}):
            self.assertEqual(hubmod.session("maps")["state"], "starting")

    def test_a_revoked_right_leaves_no_part_even_before_the_tick(self):
        self.fill()
        self.s.hub.modules.written["acc"]["capabilities"] = ["sessions"]   # the tick has not yet rewritten the slice
        self.s.hub.state["sessions"]["docs"]["state"] = "retired"
        self.s.hub.record("session.waiting", "docs", {"text": "waiting", **OWNER})
        got = self.file("acc")
        self.assertNotIn("accounts", got)
        self.assertNotIn("account", got["sessions"]["maps"])

    def test_without_the_file_rows_have_the_same_shape(self):
        self.fill()
        self.s.hub.save_state()
        _env(self, HUB_MODULE="acc", PWD=None)
        os.unlink(os.path.join(modrun.run_dir(self.s.state_dir), "acc.state.json"))
        with mock.patch.object(hubmod, "manifest", return_value={"capabilities": ["sessions", "accounts"]}):
            row = hubmod.session("maps")
        self.assertEqual(row["account"], "a@x")
        self.assertEqual(row["limit_cut"], {"ts": 700.0, "resets": 9000.0, "account": "a@x"})

    def test_rows_with_the_account_only_for_a_module_with_both_rights(self):
        st = self.s.hub.state
        self.assertNotIn("sessions+accounts", modstate.views(st, [{"sessions"}, {"accounts"}]))
        self.assertIn("sessions+accounts", modstate.views(st, [{"sessions"}, {"sessions", "accounts"}]))

    def test_a_fresh_module_does_not_build_the_parts_twice(self):
        self.s.hub.modules.published.pop("fake")   # the slice was not written for it (the write failed) — the whole thing on the event
        with mock.patch.object(modstate, "owner_index", wraps=modstate.owner_index) as idx, \
                mock.patch.object(modstate, "session_rows", wraps=modstate.session_rows) as rows:
            self.s.hub.record("session.waiting", "docs", {"text": "waiting", **OWNER})
        self.assertEqual((idx.call_count, rows.call_count), (1, 1))
        self.assertIn("sessions", self.file())

    def test_a_renamed_session_keeps_its_rows_and_messages(self):
        self.fill()
        self.s.hub.state["sessions"]["maps"]["state"] = "asleep"   # a sleeping one — at once (for a live one — a question, the queue)
        self.s.hub.record("session.renamed", "maps", {"to": "atlas", **OWNER})
        self.assertEqual(self.file("msgs")["owner_messages"][self.mid]["to"], "atlas")
        self.assertEqual(self.file()["sessions"]["atlas"]["owner_msg"]["id"], self.mid)


class Labels(unittest.TestCase):
    """Every module.json right has a label on the page (the «Modules» tab, third-party modules): the owner sees what the
    module asks for, not the right's name."""

    def test_every_right_has_a_label(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "modules", "web", "static", "js", "modules.js"), encoding="utf-8") as f:
            js = f.read()
        caps = re.search(r"const CAPS = \{(.*?)\n\};", js, re.S)[1]
        named = set(re.findall(r'"?([a-z.]+)"?: t\("', caps))
        self.assertEqual(named, modules.CAPABILITIES)


if __name__ == "__main__":
    unittest.main()
