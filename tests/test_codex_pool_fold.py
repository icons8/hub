"""Codex (experimental): the Codex account pool in the fold — the same machinery as the Claude pool
(reducer._account, _account_stale, limit_cut) on the Codex pool's record st["codex"]["account"]. A switch moves the
Codex sessions after their turn, a turn cut by a limit at once and once; the Claude pool is not touched; an account's
lost sign-in is not the hub's while another works; a late fact of a previous sign-in does not spoil the new one."""
import tests  # noqa: F401 — keep the machine's settings out
import unittest

from core import reducer

NOW = 1_791_160_000
H = 3600
OWNER = {"channel": "cli", "owner": True}
HUB_HOME = "/home/x/state/codex-home"
B_HOME = "/home/x/state/moddata/accounts/codex/codex-2"
ROWS = [{"name": "codex-1", "home": HUB_HOME, "hub": True}, {"name": "codex-2", "home": B_HOME}]


def codex_limits(five, status="allowed", account=None, **more):
    return {"status": status, "type": None, "overage": False, "engine": "codex", "plan": "plus",
            "windows": {"five_hour": {"used": five, "resets": NOW + H}}, **({"account": account} if account else {}),
            **more}


class Fleet:
    """A hub entirely on Codex with hub and maps (helpers; not a TestCase)."""

    def setUp(self):
        self.st, self.seq, self.acts = reducer.empty(), 0, []

    def ev(self, t, e=None, ts=None, **d):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts or NOW + self.seq, "type": t,
                                                      "entity": e, "data": d})
        self.acts = acts
        return why

    def spawns(self):
        return {a["session"]: (a.get("account"), a.get("home")) for a in self.acts if a.get("do") == "spawn"}

    def fleet(self, whole=True):
        self.ev("codex.host", None, on=True, channel="core", owner=False, **({"whole": True} if whole else {}))
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **OWNER)
        self.ev("session.requested", "maps", cwd="/m", session_id="s-m", **OWNER)
        for n in ("hub", "maps"):
            self.ev("session.started", n)

    def turn(self, name):
        """The message typed to the session is taken by its turn (delivered)."""
        typed = [m["id"] for m in self.st["messages"].values() if m["session"] == name and m["state"] == "typing"]
        self.ev("turn.started", name, **({"marker": typed[0]} if typed else {}))

    def pool(self, active="codex-1"):
        self.ev("codex.pool", None, on=True, switch_at=0.95, profiles=ROWS, by="accounts", channel="core")
        if active:
            self.ev("codex.switched", None, to=active, restart=True, why="accounts enabled", channel="core")


class Pool(Fleet, unittest.TestCase):
    def test_rows_are_written_by_the_core_only(self):
        self.assertIsNotNone(self.ev("codex.pool", None, on=True, switch_at=0.95, profiles=ROWS, **OWNER))
        self.assertIsNone(self.ev("codex.pool", None, on=True, switch_at=0.95, profiles=ROWS, by="accounts",
                                  channel="core"))
        self.assertEqual(self.st["codex"]["pool"]["profiles"], ROWS)

    def test_a_switch_moves_codex_sessions_after_their_turn_and_leaves_claude_alone(self):
        self.fleet()
        self.pool()
        self.ev("message.received", None, to="maps", text="do it", **OWNER)
        self.assertEqual(self.spawns()["maps"], ("codex-1", HUB_HOME))   # the pool turned on: moved to codex-1
        self.ev("session.started", "maps")
        self.turn("maps")
        self.assertIsNone(self.ev("codex.switched", None, to="codex-2", restart=True, why="codex-1: 5 h 96%",
                                  blocked={"codex-1": NOW + H}, channel="core", **{"from": "codex-1"}))
        cx = self.st["codex"]["account"]
        self.assertEqual((cx["active"], cx["blocked"]), ("codex-2", {"codex-1": NOW + H}))
        self.assertEqual(self.st.get("account", {}).get("active"), None)   # the Claude pool is not touched
        self.assertEqual(self.spawns(), {})                                # the running turn goes on on codex-1
        self.ev("turn.result", "maps", text="done", engine="codex")
        self.ev("message.received", None, to="maps", text="more", **OWNER)
        self.assertEqual(self.spawns(), {"maps": ("codex-2", B_HOME)})    # before its next message: on codex-2
        self.assertTrue(any(a["do"] == "release" and a["session"] == "maps" for a in self.acts))

    def test_the_claude_switch_does_not_move_codex_sessions(self):
        self.fleet()
        self.pool()
        self.ev("message.received", None, to="maps", text="do it", **OWNER)
        self.ev("session.started", "maps")
        self.ev("account.switched", None, to="work", restart=True, channel="core")
        self.ev("message.received", None, to="maps", text="more", **OWNER)
        self.assertEqual(self.spawns(), {})
        self.assertEqual(self.st["codex"]["account"]["active"], "codex-1")

    def test_a_cut_turn_moves_at_once_and_continues_once(self):
        self.fleet()
        self.pool()
        self.ev("message.received", None, to="maps", text="do it", **OWNER)
        self.ev("session.started", "maps")
        self.turn("maps")
        self.ev("account.limits", None, **codex_limits(1, "rejected", account="codex-1"))
        self.ev("turn.result", "maps", text="Codex: usage limit reached (usageLimitExceeded)", is_error=True,
                engine="codex", limit=True, limit_resets=NOW + H)
        cut = self.st["sessions"]["maps"]["limit_cut"]
        self.assertEqual((cut["engine"], cut["account"]), ("codex", "codex-1"))
        self.ev("codex.switched", None, to="codex-2", restart=True, why="codex-1: limit", channel="core",
                blocked={"codex-1": NOW + H}, **{"from": "codex-1"})
        self.assertEqual(self.spawns(), {"maps": ("codex-2", B_HOME)})    # cut — at once, not at the next message
        before = len(self.st["messages"])
        self.ev("session.started", "maps")
        go = [m for m in list(self.st["messages"].values())[before:] if m["session"] == "maps"]
        self.assertEqual(len(go), 1)
        self.assertIn("Codex", go[0]["text"])
        self.assertNotIn("limit_cut", self.st["sessions"]["maps"])
        self.ev("codex.limit_reset", None, ts=NOW + 2 * H, channel="core")   # codex-1's window resets later
        self.assertEqual(len(self.st["messages"]), before + 1)              # not a second continuation
        self.assertFalse(reducer.codex_due(self.st, NOW + 2 * H))

    def test_the_reset_continues_only_cuts_on_the_active_account(self):
        self.fleet()
        self.pool()
        self.ev("message.received", None, to="maps", text="do it", **OWNER)
        self.ev("session.started", "maps")
        self.turn("maps")
        self.ev("turn.result", "maps", text="Codex: usage limit reached", is_error=True, engine="codex", limit=True,
                limit_resets=NOW + H)
        self.ev("codex.exhausted", None, until=NOW + H, blocked={"codex-1": NOW + H}, why="all", channel="core")
        self.assertTrue(reducer.codex_due(self.st, NOW + H + 1))    # cut on codex-1, codex-1 is active
        self.st["codex"]["account"]["active"] = "codex-2"           # (the switch path continues it instead)
        self.assertFalse(reducer.codex_due(self.st, NOW + H + 1))

    def test_limits_go_to_their_account_and_the_row_is_the_active_ones(self):
        self.fleet()
        self.pool("codex-2")
        self.ev("account.limits", None, **codex_limits(0.4, account="codex-2"))
        self.ev("account.limits", None, **codex_limits(0.97, account="codex-1"))
        seen = self.st["codex"]["account"]["seen"]
        self.assertEqual((seen["codex-1"]["windows"]["five_hour"]["used"], seen["codex-2"]["windows"]["five_hour"]["used"]),
                         (0.97, 0.4))
        self.assertEqual(self.st["codex"]["limits"]["windows"]["five_hour"]["used"], 0.4)   # the active one's row
        self.assertEqual(self.st["limits"]["windows"]["five_hour"]["used"], 0.4)            # the hub's (entirely Codex)
        self.ev("codex.switched", None, to="codex-1", restart=True, channel="core", **{"from": "codex-2"})
        self.assertEqual(self.st["codex"]["limits"]["windows"]["five_hour"]["used"], 0.97)  # at once, its known numbers
        self.assertNotIn("codex-1", (self.st.get("account") or {}).get("seen") or {})       # not Claude's

    def test_without_a_pool_limits_stay_one_row(self):
        self.fleet()
        self.ev("account.limits", None, **codex_limits(0.4))
        self.assertEqual(self.st["codex"]["limits"]["windows"]["five_hour"]["used"], 0.4)

    def test_exhausted_tells_once_about_codex(self):
        self.fleet()
        self.pool()
        before = len(self.st["messages"])
        self.ev("codex.exhausted", None, until=NOW + H, blocked={"codex-1": NOW + H, "codex-2": NOW + H}, why="all",
                channel="core")
        said = [m["text"] for m in list(self.st["messages"].values())[before:]]
        self.assertEqual(len(said), 1)
        self.assertIn("Codex", said[0])
        self.assertNotIn("Claude", said[0])


class SignIn(Fleet, unittest.TestCase):
    def test_an_accounts_lost_sign_in_is_not_the_hubs_while_another_works(self):
        self.fleet()
        self.pool("codex-2")
        self.ev("codex.account_lost", None, account="codex-2", why="401", channel="core")
        cx = self.st["codex"]
        self.assertIsNone(cx.get("lost"))                                     # the hub still lets in (codex-1)
        self.assertEqual(cx["account"]["lost"]["codex-2"]["why"], "401")
        self.assertEqual(cx["account"]["seen"]["codex-2"]["type"], "auth")   # the pool's decide moves away from it
        self.ev("codex.auth_lost", None, account="codex-1", why="refresh token was already used", channel="core")
        self.assertIsNotNone(self.st["codex"].get("lost"))                   # every row lost — the hub does not let in

    def test_without_a_pool_the_hub_folders_loss_is_the_hubs(self):
        self.fleet()
        self.ev("codex.auth_lost", None, why="401", channel="core")
        self.assertIsNotNone(self.st["codex"].get("lost"))

    def test_a_turn_failed_on_the_sign_in_cuts_and_marks_its_account(self):
        self.fleet()
        self.pool("codex-2")
        self.ev("message.received", None, to="maps", text="do it", **OWNER)
        self.ev("session.started", "maps")
        self.turn("maps")
        self.ev("turn.result", "maps", text="Codex: 401 unauthorized", is_error=True, engine="codex", auth_lost=True,
                account="codex-2")
        self.assertEqual(self.st["sessions"]["maps"]["limit_cut"]["account"], "codex-2")
        self.assertIn("codex-2", self.st["codex"]["account"]["lost"])
        self.assertIsNone(self.st["codex"].get("lost"))

    def test_a_sign_in_again_continues_the_turn_cut_on_it(self):
        # one row only works (the other is busy): a 401 cuts the turn, no switch can continue it; the owner signs in again
        self.fleet()
        self.pool("codex-2")
        self.ev("message.received", None, to="maps", text="do it", **OWNER)
        self.ev("session.started", "maps")
        self.turn("maps")
        self.ev("turn.result", "maps", text="Codex: 401", is_error=True, engine="codex", auth_lost=True, account="codex-2")
        self.assertIn("limit_cut", self.st["sessions"]["maps"])
        self.ev("codex.account_login", None, account="codex-2", email="b@example.com", **OWNER)
        self.assertEqual(self.spawns(), {"maps": ("codex-2", B_HOME)})   # the new generation, at once: it was cut
        before = len(self.st["messages"])
        self.ev("session.started", "maps")
        self.assertEqual(len([m for m in list(self.st["messages"].values())[before:] if m["session"] == "maps"]), 1)

    def test_a_row_whose_folder_was_dropped_is_not_marked(self):
        self.fleet()
        self.pool("codex-2")
        self.ev("codex.pool", None, on=True, switch_at=0.95, profiles=ROWS[:1], by="accounts", channel="core")
        self.ev("message.received", None, to="maps", text="do it", **OWNER)
        self.assertEqual(self.spawns(), {"maps": (None, None)})   # the hub folder, not marked codex-2

    def test_a_cut_from_before_the_pool_is_due_by_its_reset(self):
        # a session cut before this version (its mark was a Claude account's name, a mixed hub): without a Codex pool
        # the reset continues it as before
        self.fleet()
        self.ev("message.received", None, to="maps", text="do it", **OWNER)
        self.ev("session.started", "maps")
        self.turn("maps")
        self.st["sessions"]["maps"]["account"] = "claude-work"
        self.ev("turn.result", "maps", text="Codex: usage limit reached", is_error=True, engine="codex", limit=True,
                limit_resets=NOW + H)
        self.assertTrue(reducer.codex_due(self.st, NOW + H + 1))

    def test_a_sign_in_is_a_new_generation(self):
        self.fleet()
        self.pool("codex-1")
        t = NOW + 1000
        self.ev("codex.account_lost", None, account="codex-2", why="401", channel="core")
        self.ev("account.limits", None, **codex_limits(1, "rejected", account="codex-2"))
        self.ev("codex.account_login", None, ts=t, account="codex-2", email="b@example.com", plan="plus", **OWNER)
        acc = self.st["codex"]["account"]
        self.assertNotIn("codex-2", acc.get("lost") or {})
        self.assertNotIn("codex-2", acc.get("seen") or {})   # the numbers were someone else's sign-in
        self.assertEqual(acc["logins"]["codex-2"]["email"], "b@example.com")
        # a measurement begun before the sign-in, landing after it, is about the previous one
        self.ev("codex.account_lost", None, ts=t + 5, account="codex-2", why="401", started=t - 3, channel="plugin",
                sender="mod:accounts")
        self.ev("account.limits", None, ts=t + 6, **codex_limits(1, "rejected", account="codex-2", started=t - 2))
        acc = self.st["codex"]["account"]
        self.assertNotIn("codex-2", acc.get("lost") or {})
        self.assertNotIn("codex-2", acc.get("seen") or {})
        self.ev("codex.account_lost", None, ts=t + 9, account="codex-2", why="401", started=t + 7, channel="plugin",
                sender="mod:accounts")
        self.assertIsNotNone(self.ev("codex.account_lost", None, account="codex-2", why="x", channel="plugin"))
        self.assertIsNotNone(self.ev("codex.account_lost", None, account="codex-2", why="x", channel="session",
                                     session="maps"))
        self.assertIn("codex-2", self.st["codex"]["account"]["lost"])

    def test_an_accounts_sign_in_is_the_owners_word(self):
        self.fleet()
        self.pool()
        self.assertIsNotNone(self.ev("codex.account_login", None, account="codex-2", email="b@example.com",
                                     channel="session", owner=False, session="maps"))
        self.assertIsNotNone(self.ev("codex.account_logout", None, account="codex-2", channel="session", owner=False,
                                     session="maps"))
        self.assertIsNone(self.ev("codex.account_logout", None, account="codex-2", **OWNER))

    def test_the_owners_notice_of_an_accounts_loss_is_marked_told(self):
        self.fleet()
        self.pool("codex-2")
        self.ev("codex.account_lost", None, account="codex-2", why="401", channel="core")
        self.ev("owner.notify", None, id=reducer.codex_notice("codex-2"), title="Codex", text="sign-in lost",
                channel="core", owner=False)
        self.assertTrue(self.st["codex"]["account"]["lost"]["codex-2"].get("told"))
        self.assertIn("codex-2", reducer.codex_account_lost_text(self.st, "codex-2"))


class ActiveFolder(Fleet, unittest.TestCase):
    def test_the_active_folder_is_the_account_rows_only_while_the_pool_works(self):
        self.fleet()
        self.pool("codex-2")
        self.assertEqual(reducer.codex_active_home(self.st), B_HOME)
        self.ev("codex.switched", None, to="codex-1", restart=True, channel="core")
        self.assertIsNone(reducer.codex_active_home(self.st))                  # the hub row: the hub folder
        self.ev("codex.switched", None, to="codex-2", restart=True, channel="core")
        self.ev("codex.pool", None, on=False, switch_at=0.95, profiles=ROWS, by="accounts", channel="core")
        self.assertIsNone(reducer.codex_active_home(self.st))                  # the pool off, its rows kept
        self.ev("codex.pool", None, on=True, switch_at=0.95, profiles=ROWS, by="accounts", channel="core")
        self.ev("codex.host", None, on=False, channel="core", owner=False)
        self.assertIsNone(reducer.codex_active_home(self.st))                  # back on claude


class Mixed(Fleet, unittest.TestCase):
    def test_a_claude_session_of_a_mixed_hub_follows_the_claude_pool(self):
        self.fleet(whole=False)
        self.ev("session.requested", "seo", cwd="/s", session_id="s-s", **OWNER)
        self.ev("session.started", "seo")
        self.st["sessions"]["maps"]["model"] = "codex/gpt-5.5"   # a Codex session next to claude ones
        self.pool()
        self.ev("account.switched", None, to="work", restart=True, channel="core")
        self.ev("message.received", None, to="seo", text="more", **OWNER)
        self.assertEqual(self.spawns(), {"seo": ("work", None)})
        self.ev("message.received", None, to="maps", text="more", **OWNER)
        self.assertEqual(self.spawns(), {"maps": ("codex-1", HUB_HOME)})


if __name__ == "__main__":
    unittest.main()
