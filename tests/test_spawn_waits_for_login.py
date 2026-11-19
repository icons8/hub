"""A new hub with Claude Code but no sign-in yet (the app before «Setup» signed in): the core does not spawn sessions
that would answer every turn «Not logged in» — it waits for the sign-in, as it waits for the program
(test_spawn_waits_for_claude): no host.error, no session.exited, no restarts. Once Claude Code is signed in (the page's
«Setup», `claude auth login` in a terminal) the session spawns by itself, without a restart. The question is Claude
Code's own `claude auth status` (the page asks the same), with the session's environment and folder; it is not asked
where the sign-in is known without it: a key in the session's environment (a pool account, a module, the owner's
settings), a cloud provider, the account gateway — and never on a hub whose session has answered once."""
import tests  # noqa: F401
import json
import os
import tempfile
import unittest

import platforms
from core import journal, restart
from core.daemon import Hub
from hosts.streamjson import StreamHost
from tests.fakes import exe
from tests.fakes import cut

FAKE = os.path.join(os.path.dirname(__file__), "fakes", "fake_claude_stream.py")


def claude_script(log, signed):
    """A Claude Code that answers `auth status` by a file (signed — there is a sign-in; a file with text — that text,
    not JSON) and runs the stream protocol otherwise; every start is a line in log."""
    return (f"import json, os, runpy, sys\n"
            f"with open({log!r}, 'a') as f:\n"
            f"    f.write(json.dumps({{'argv': sys.argv[1:3], 'cwd': os.getcwd(),"
            f" 'key': os.environ.get('ANTHROPIC_API_KEY')}}) + '\\n')\n"
            f"if sys.argv[1:3] == ['auth', 'status']:\n"
            f"    raw = open({signed!r}).read() if os.path.exists({signed!r}) else None\n"
            f"    if raw:\n"
            f"        print(raw)\n"
            f"        sys.exit(1)\n"
            f"    print(json.dumps({{'loggedIn': raw is not None, 'authMethod': 'claude.ai' if raw is not None else 'none'}}))\n"
            f"    sys.exit(0 if raw is not None else 1)\n"
            f"sys.argv[0] = {FAKE!r}\n"
            f"runpy.run_path({FAKE!r}, run_name='__main__')\n")


@unittest.skipIf(os.name == "nt", "a script by path without .exe: the stand-in for claude.exe is POSIX here")
class SpawnWaitsForLogin(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="nologin-")
        self.addCleanup(platforms.current.rmtree, self.root)
        self.state = os.path.join(self.root, "state")
        os.makedirs(self.state)
        self.log = os.path.join(self.root, "claude-runs.jsonl")
        self.signed = os.path.join(self.root, "signed")
        self.claude = exe.script(os.path.join(self.root, "claude"), claude_script(self.log, self.signed))
        self.now = [1_000_000.0]
        self.host = StreamHost(self.state, claude=(self.claude,))
        for k in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            self.host.env[k] = ""   # whatever the machine running the tests has: the sessions' sign-in is this test's
        self.addCleanup(self.host.kill_server)
        self.hub = Hub(self.state, self.host, registry_dir=os.path.join(self.root, "reg"), clock=lambda: self.now[0])

    def events(self, kind):
        return [e for e in journal.read(self.hub.journal.path) if e["type"] == kind]

    def spawned(self):
        return [e["entity"] for e in self.events("host.done") if e["data"].get("do") == "spawn"
                and e["data"].get("result") == "spawned"]

    def asked(self):
        """How many times Claude Code was asked about its sign-in."""
        try:
            with open(self.log) as f:
                return [json.loads(x) for x in f if json.loads(x)["argv"] == ["auth", "status"]]
        except FileNotFoundError:
            return []

    def passes(self, n, step=5):
        for _ in range(n):
            self.now[0] += step
            self.hub.run_delayed()
            self.hub.login_tick()

    def request(self, name="hub-core", **extra):
        self.hub.record("session.requested", name, {"cwd": self.root, "session_id": f"s-{name}", "channel": "cli",
                                                    "owner": True, **({"kind": "hub"} if name == "hub-core" else {}),
                                                    **extra})

    def sign_in(self):
        open(self.signed, "w").close()

    def test_no_sign_in_no_spawn_and_no_crashes(self):
        self.request()
        self.passes(10)
        sess = self.hub.state["sessions"]["hub-core"]
        self.assertEqual(self.spawned(), [])
        self.assertEqual([e for e in self.events("host.error") if e["data"].get("do") == "spawn"], [])
        self.assertEqual(self.events("session.exited"), [])
        self.assertEqual((sess["state"], sess["restarts"]), ("starting", 0))
        self.assertIn("hub-core", self.hub.waiting())
        self.assertTrue(self.hub.state.get("login_missing"))
        self.assertEqual([e["data"] for e in self.events("core.login_missing")], [{"missing": True}])   # said once
        self.assertEqual(self.asked()[0]["cwd"], os.path.realpath(self.root))   # in the session's folder (its project settings)

    def test_the_session_spawns_by_itself_once_signed_in(self):
        self.request()
        self.request("maps", role="agent")
        self.passes(4)
        self.assertEqual(self.spawned(), [])
        self.sign_in()
        self.passes(2)
        self.assertEqual(sorted(self.spawned()), ["hub-core", "maps"])
        self.assertEqual(self.hub.waiting(), set())
        self.assertEqual(self.hub.state["sessions"]["hub-core"]["restarts"], 0)
        self.assertNotIn("login_missing", self.hub.state)
        self.assertEqual([e["data"] for e in self.events("core.login_missing")], [{"missing": True}, {"missing": False}])

    def test_waiting_spawns_ask_together_not_each(self):
        for name in ("hub-core", "maps", "notes", "mail"):
            self.request(name, **({} if name == "hub-core" else {"role": "agent"}))
        self.passes(6)   # 30 s
        self.assertLessEqual(len(self.asked()), 7)   # once per LOGIN_STEP for all of them, not 4 sessions × every step

    def test_signed_in_spawns_at_once_and_asks_once(self):
        self.sign_in()
        self.request()
        self.request("maps", role="agent")
        self.assertEqual(self.spawned(), ["hub-core", "maps"])
        self.assertEqual(len(self.asked()), 1)
        self.assertEqual(self.events("core.login_missing"), [])

    def test_an_unclear_answer_does_not_hold_the_spawn(self):
        with open(self.signed, "w") as f:
            f.write("error: unknown command 'auth'")   # an older Claude Code, a crash: unknown — not «signed out»
        self.request()
        self.assertEqual(self.spawned(), ["hub-core"])

    def test_a_key_in_the_session_environment_is_a_sign_in_without_asking(self):
        # the owner's settings env, a pool account, a module: the core gave the session a key
        self.host.env["ANTHROPIC_API_KEY"] = "sk-test-not-real"
        self.request()
        self.assertEqual(self.spawned(), ["hub-core"])
        self.assertEqual(self.asked(), [])

    @cut.needs("profile")
    def test_a_pool_account_spawns_without_asking(self):
        from modules.accounts import accounts
        cfg = os.path.join(self.root, "cfg")
        os.makedirs(cfg)
        self.addCleanup(os.environ.__setitem__, "HUB_CONFIG_DIR", os.environ["HUB_CONFIG_DIR"])
        os.environ["HUB_CONFIG_DIR"] = cfg
        with open(accounts.path(), "w") as f:
            json.dump({"accounts": [{"name": "work", "token": "sk-ant-oat01-not-real"}]}, f)
        self.hub.record("account.switched", None, {"to": "work", "from": None, "restart": True, "manual": True,
                                                   "channel": "cli", "owner": True})
        self.request("maps", role="agent")
        self.assertEqual(self.spawned(), ["maps"])
        self.assertEqual(self.asked(), [])

    @cut.needs("profile")
    def test_an_account_added_to_the_pool_while_it_waits_spawns_it_without_a_restart(self):
        from modules.accounts import accounts
        cfg = os.path.join(self.root, "cfg")
        os.makedirs(cfg)
        self.addCleanup(os.environ.__setitem__, "HUB_CONFIG_DIR", os.environ["HUB_CONFIG_DIR"])
        os.environ["HUB_CONFIG_DIR"] = cfg
        self.request("maps", role="agent")
        self.passes(3)   # no sign-in and no pool: it waits with an action that has no account
        self.assertEqual(self.spawned(), [])
        self.assertTrue(self.hub.state.get("login_missing"))
        with open(accounts.path(), "w") as f:
            json.dump({"accounts": [{"name": "work", "token": "sk-ant-oat01-not-real"}]}, f)
        self.hub.record("account.switched", None, {"to": "work", "from": None, "restart": True, "manual": True,
                                                   "channel": "cli", "owner": True})
        self.passes(3)
        self.assertEqual(self.spawned(), ["maps"])
        self.assertEqual(self.hub.state["sessions"]["maps"]["account"], "work")
        self.assertNotIn("login_missing", self.hub.state)

    def test_a_held_spawn_follows_the_pool_it_waits_in(self):
        self.request("maps", role="agent")
        held = {"do": "spawn", "session": "maps", "login_wait": True, "account": "work", "account_gen": 7}
        pool = self.hub.state.setdefault("account", {})
        pool["active"], pool["refreshed"] = "work", {"work": 7}
        self.assertIs(self.hub.fresh_account(held), held)   # the same account and token: as it was
        pool["refreshed"] = {"work": 9}   # a fresh token of the same account
        got = self.hub.fresh_account(held)
        self.assertEqual((got["account"], got["account_gen"], got.get("account_fresh")), ("work", 9, True))
        pool["active"] = None   # the account left the pool meanwhile: no account, and the mark says so
        got = self.hub.fresh_account(got)
        self.assertEqual((got.get("account"), got.get("account_gen"), got.get("account_fresh")), (None, None, True))
        self.hub.record("host.done", "maps", {"do": "spawn", "result": "spawned", "account": None, "gen": None})
        self.assertNotIn("account", self.hub.state["sessions"]["maps"])
        self.hub.state["sessions"]["maps"].update(account="work", account_gen=7)
        self.hub.record("host.done", "maps", {"do": "spawn", "result": "spawned", "account": "other", "gen": 9})
        self.assertEqual([self.hub.state["sessions"]["maps"][k] for k in ("account", "account_gen")], ["other", 9])

    def hold_once(self, wait):
        """The named wait holds the spawn for 30 s, once (the previous process, modules, the login module, the gateway,
        a folder move), as it would: the action goes back to the delayed queue as it is."""
        real, held = getattr(self.hub, wait), [True]

        def once(action):
            if held[0]:
                held[0] = False
                self.hub.delayed.append((self.now[0] + 30, action))
                return True
            return real(action)
        setattr(self.hub, wait, once)

    def pool(self, *names):
        from modules.accounts import accounts
        cfg = os.path.join(self.root, "cfg")
        os.makedirs(cfg, exist_ok=True)
        self.addCleanup(os.environ.__setitem__, "HUB_CONFIG_DIR", os.environ["HUB_CONFIG_DIR"])
        os.environ["HUB_CONFIG_DIR"] = cfg
        with open(accounts.path(), "w") as f:
            json.dump({"accounts": [{"name": n, "token": "sk-ant-oat01-not-real"} for n in names]}, f)

    @cut.needs("profile")
    def test_a_spawn_held_by_any_wait_follows_the_pool_that_changed_meanwhile(self):
        for wait in ("wait_release", "wait_modules", "wait_private", "wait_gateway", "wait_forward"):
            with self.subTest(wait=wait, change="added"):
                self.setUp()
                self.sign_in()
                self.hold_once(wait)
                self.request("maps", role="agent")
                self.assertEqual(self.spawned(), [])
                self.pool("work")
                self.hub.record("account.switched", None, {"to": "work", "from": None, "restart": True,
                                                           "manual": True, "channel": "cli", "owner": True})
                self.passes(7)
                self.assertEqual(self.spawned(), ["maps"])
                self.assertEqual(self.hub.state["sessions"]["maps"].get("account"), "work")
                self.assertEqual(self.asked(), [])   # its key was in the environment: a sign-in, not asked
            with self.subTest(wait=wait, change="removed"):
                self.setUp()
                self.pool("work")
                self.hub.record("account.switched", None, {"to": "work", "from": None, "restart": True,
                                                           "manual": True, "channel": "cli", "owner": True})
                self.hold_once(wait)
                self.request("maps", role="agent")
                self.assertEqual(self.hub.state["sessions"]["maps"].get("account"), "work")
                self.hub.record("account.switched", None, {"to": None, "from": "work", "restart": True,
                                                           "manual": True, "channel": "cli", "owner": True})
                self.passes(7)
                self.assertEqual(self.spawned(), [])   # no pool, no sign-in: it waits for the sign-in as any other
                held = [a for _, a in self.hub.delayed if a.get("session") == "maps"]
                self.assertEqual(len(held), 1)
                self.assertNotIn("account", held[0])
                self.assertTrue(held[0].get("account_fresh"))
                self.sign_in()
                self.passes(3)
                self.assertEqual(self.spawned(), ["maps"])
                self.assertNotIn("account", self.hub.state["sessions"]["maps"])

    def test_the_gateway_is_a_sign_in_without_asking(self):
        from core import profiles
        env = profiles.through({}, 4567, "s" * 20, stub=False)   # this computer's login through the gateway
        self.assertTrue(self.host.signed_in(env, self.root))
        self.assertTrue(self.host.signed_in({"CLAUDE_CODE_USE_BEDROCK": "1"}, self.root))   # a cloud provider
        self.assertEqual(self.asked(), [])

    def test_a_hub_whose_session_answered_never_asks(self):
        # a key only in a project's settings, a login Claude Code reports oddly: the hub works — nothing changes for it
        self.request("maps", role="agent")
        self.passes(1)
        self.sign_in()
        self.passes(2)
        self.hub.record("turn.result", "maps", {"text": "done", "turns": 1})
        self.assertTrue(self.hub.state["sessions"]["maps"].get("last_out"))
        os.unlink(self.signed)
        asked = len(self.asked())
        host = StreamHost(self.state, claude=(self.claude,))   # a hub restart: a new host, the same state
        host.env.update(self.host.env)
        self.addCleanup(host.kill_server)
        hub = Hub(self.state, host, registry_dir=os.path.join(self.root, "reg"), clock=lambda: self.now[0])
        hub.record("session.requested", "notes", {"cwd": self.root, "session_id": "s-notes", "channel": "cli",
                                                  "owner": True, "role": "agent"})
        self.assertIn("notes", [e["entity"] for e in journal.read(hub.journal.path) if e["type"] == "host.done"
                                and e["data"].get("result") == "spawned"])
        self.assertEqual(len(self.asked()), asked)

    def test_restart_of_the_whole_hub_does_not_wait_for_sessions_that_wait_for_the_sign_in(self):
        self.request()
        self.request("maps", role="agent")
        self.passes(2)
        self.assertEqual(restart.blockers(self.hub.state), [])

    def test_the_mark_goes_when_no_spawn_waits_any_more(self):
        self.request("maps", role="agent")
        self.passes(2)
        # an executor stopped while it waited (hub session stop): run_delayed drops a spawn of a session not «starting»
        self.hub.state["sessions"]["maps"]["state"] = "asleep"
        self.passes(2)
        self.assertEqual(self.hub.waiting(), set())
        self.assertNotIn("login_missing", self.hub.state)

    def test_a_newer_spawn_of_the_session_replaces_the_waiting_one(self):
        self.request()
        self.passes(1)
        self.hub.execute({"do": "spawn", "session": "hub-core", "session_id": "s-hub-core", "cwd": self.root,
                          "effort": "high"})
        self.passes(1)
        self.sign_in()
        self.passes(3)
        self.assertEqual(self.spawned(), ["hub-core"])

    def test_a_spawn_that_then_waits_for_something_else_spawns_once_signed_in(self):
        self.request()
        self.passes(1)
        self.sign_in()
        real, held = self.hub.wait_release, [True]

        def once(action):   # the previous process (or modules, the login module, the gateway) holds it for a while
            if held[0]:
                held[0] = False
                self.hub.delayed.append((self.now[0] + 30, action))
                return True
            return real(action)
        self.hub.wait_release = once
        self.passes(1)
        self.assertEqual(self.spawned(), [])
        self.passes(6)
        self.assertEqual(self.spawned(), ["hub-core"])
        self.assertNotIn("login_missing", self.hub.state)   # it went: the core no longer says «waits for the sign-in»
        self.assertEqual([e["data"] for e in self.events("core.login_missing")], [{"missing": True}, {"missing": False}])

    def test_the_mark_does_not_swing_while_another_wait_holds_a_spawn(self):
        # still signed out: a spawn held for the sign-in, then by another wait — «signed in» is never said
        self.request()
        real, held = self.hub.wait_release, [True]

        def once(action):
            if held[0]:
                held[0] = False
                self.hub.delayed.append((self.now[0] + 12, action))
                return True
            return real(action)
        self.hub.wait_release = once
        self.passes(8)
        self.assertEqual([e["data"] for e in self.events("core.login_missing")], [{"missing": True}])

    def test_a_hanging_auth_status_is_unknown_in_time(self):
        import time
        from core import claudeauth
        slow = exe.script(os.path.join(self.root, "slow"), "import time\ntime.sleep(60)\n")
        t = time.monotonic()
        self.assertIsNone(claudeauth.status([slow], timeout=1))
        self.assertLess(time.monotonic() - t, 10)

    def test_only_the_core_writes_the_mark(self):
        from core import spool
        spool.put(os.path.join(self.state, "spool"), "core.login_missing", None, {"missing": True})
        self.hub.collect()
        self.assertNotIn("login_missing", self.hub.state)


if __name__ == "__main__":
    unittest.main()
