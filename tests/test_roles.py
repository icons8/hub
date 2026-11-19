"""Session roles (decision of 2026-09-30): an agent is a permanent professional with its own tools (DevOps, SEO) that stays
until it is dismissed; a worker is a session for one task, and it is put to sleep. The hub used to confuse who should
get what: now the core records what each agent does, and the hub looks at agents first."""
import tests  # noqa: F401 — ignore the machine's settings
import os
import subprocess
import sys
import tempfile
import unittest

from core import guards, reducer, spool
from core.views import Reader

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(REPO, "bin", "hub")
OWNER = {"channel": "cli", "owner": True}
FROM_HUB = {"channel": "session", "owner": False, "sender": "hub"}


class Core(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.requested", "devops", cwd="/k8s", session_id="s-d", role="agent",
                about="DevOps: k8s, ArgoCD, service configs", icon="🛠", **FROM_HUB)
        self.ev("session.requested", "wk-1", cwd="/w", session_id="s-w", **FROM_HUB)
        for name in ("hub", "devops", "wk-1"):
            self.ev("session.started", name)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": self.seq, "type": type, "entity": entity,
                                                   "data": data})
        return why

    def sess(self, name):
        return self.st["sessions"][name]

    def test_new_session_is_a_worker_unless_asked(self):
        self.assertEqual((self.sess("wk-1")["role"], self.sess("wk-1").get("about")), ("worker", None))
        d = self.sess("devops")
        self.assertEqual((d["role"], d["about"], d["icon"]), ("agent", "DevOps: k8s, ArgoCD, service configs", "🛠"))

    def test_hub_hint_for_a_requested_session_carries_role_flags(self):
        # a session asks for an agent with a role: the hub's hint repeats --agent, --about and --icon (quoted), so the
        # agent is not raised without its role; a plain request keeps the short hint
        asker = {"channel": "session", "owner": False, "sender": "devops"}
        before = set(self.st["messages"])
        self.assertIsNone(self.ev("session.requested", "maps-2", cwd="/w/maps", session_id="s-m", role="agent",
                                  about="Maps: tiles; styles", icon="🗺", task="make tiles", **asker))
        (text,) = [m["text"] for k, m in self.st["messages"].items() if k not in before and m["to"] == "hub"]
        self.assertIn("--agent --about 'Maps: tiles; styles' --icon '🗺'", text)
        before = set(self.st["messages"])
        self.assertIsNone(self.ev("session.requested", "maps-3", cwd="/w/maps", session_id="s-m3", **asker))
        (text,) = [m["text"] for k, m in self.st["messages"].items() if k not in before and m["to"] == "hub"]
        self.assertIn("hub session new maps-3 --cwd /w/maps, then", text)
        self.assertNotIn("--agent", text)

    def test_agent_is_not_put_to_sleep(self):
        self.assertIn("agent", guards.closable(self.st, self.sess("devops")))
        self.assertIsNone(guards.closable(self.st, self.sess("wk-1")))
        why = self.ev("session.close_requested", "devops", **FROM_HUB)
        self.assertIn("agent", why)
        self.assertEqual(self.sess("devops")["state"], "idle")

    def test_hub_promotes_owner_fires(self):
        self.assertIsNone(self.ev("session.role_set", "wk-1", role="agent", about="SEO: site maps, robots",
                                  icon="🔎", **FROM_HUB))
        self.assertEqual((self.sess("wk-1")["role"], self.sess("wk-1")["about"]), ("agent", "SEO: site maps, robots"))
        self.assertIsNone(self.ev("session.role_set", "wk-1", about="SEO and indexing", **FROM_HUB))  # description only
        self.assertEqual((self.sess("wk-1")["role"], self.sess("wk-1")["icon"]), ("agent", "🔎"))
        self.assertIn("owner", self.ev("session.role_set", "wk-1", role="worker", **FROM_HUB))  # dismissing takes the owner
        self.assertIsNone(self.ev("session.role_set", "wk-1", role="worker", **OWNER))
        self.assertEqual(self.sess("wk-1")["role"], "worker")

    def test_hub_fires_an_agent_on_the_owners_word(self):
        # the owner answered the hub's "remove the agent?" card, and the hub dismisses on his word (--for with the
        # message or question id of the answer), like a risky tool via tools grant --for; without a word, on someone
        # else's, an old one, or one not from the owner: refused
        self.assertIn("owner", self.ev("session.role_set", "devops", role="worker", **FROM_HUB))
        self.ev("message.received", None, to="wk-1", text="yes, remove it", channel="web", owner=True)
        other = f"m{self.seq}"   # the owner's, but not to the hub
        self.ev("message.received", None, to="hub", text="remove devops", channel="session", owner=False,
                sender="wk-1")
        not_owner = f"m{self.seq}"
        for ref in (other, not_owner, "m999999"):
            self.assertIn("--for", self.ev("session.role_set", "devops", role="worker", **FROM_HUB, **{"for": ref}))
        self.ev("message.received", None, to="hub", text="yes, remove devops", channel="web", owner=True)
        word = f"m{self.seq}"
        self.seq += 25 * 3600   # older than a day
        self.assertIn("--for", self.ev("session.role_set", "devops", role="worker", **FROM_HUB, **{"for": word}))
        self.ev("message.received", None, to="hub", text="yes, remove devops", channel="web", owner=True)
        word = f"m{self.seq}"
        self.assertIsNotNone(self.ev("session.role_set", "devops", role="worker", channel="session", owner=False,
                                     sender="wk-1", **{"for": word}))   # the owner's word to the hub, not to another session
        self.assertEqual(self.sess("devops")["role"], "agent")
        self.assertIsNone(self.ev("session.role_set", "devops", role="worker", **FROM_HUB, **{"for": word}))
        self.assertEqual(self.sess("devops")["role"], "worker")

    def test_hub_fires_an_agent_on_the_owners_answer_to_its_card(self):
        self.ev("turn.started", "hub", marker=None, prompt_head="x")
        self.ev("permission.requested", "hub", request_id="r1", tool="AskUserQuestion", interactive=True,
                input={"questions": [{"question": "Remove devops?", "options": [{"label": "Yes"}, {"label": "No"}]}]})
        self.ev("turn.result", "hub", text="asked")
        self.ev("turn.stopped", "hub")
        q = self.sess("hub")["asks"][-1]["id"]
        self.ev("message.received", None, to="hub", text="Yes", channel="web", owner=True)
        self.assertEqual(self.st["messages"][f"m{self.seq}"].get("answers_question"), q)
        self.assertIsNone(self.ev("session.role_set", "devops", role="worker", **FROM_HUB, **{"for": q}))
        self.assertEqual(self.sess("devops")["role"], "worker")

    def test_no_and_close_on_its_card_are_not_the_owners_word(self):
        # "Deny" and × on the hub's card are also owner messages to the hub, but not his "yes": neither by question
        # id nor by message id
        for n, close in enumerate((
                lambda rid: self.ev("permission.answered", "hub", request_id=rid, behavior="deny", **OWNER),
                lambda rid: self.ev("question.dismissed", "hub", request_id=rid, **OWNER))):
            rid = f"r{n}"
            self.ev("turn.started", "hub", marker=None, prompt_head="x")
            self.ev("permission.requested", "hub", request_id=rid, tool="AskUserQuestion", interactive=True,
                    input={"questions": [{"question": "Remove devops?", "options": [{"label": "Yes"}, {"label": "No"}]}]})
            self.ev("turn.result", "hub", text="asked")
            self.ev("turn.stopped", "hub")
            q = self.sess("hub")["asks"][-1]["id"]
            self.assertIsNone(close(rid))
            m = next(m["id"] for m in self.st["messages"].values() if m.get("answers_question") == q)
            self.assertIn("--for", self.ev("session.role_set", "devops", role="worker", **FROM_HUB, **{"for": q}))
            self.assertIn("not their \"yes\"", self.ev("session.role_set", "devops", role="worker", **FROM_HUB, **{"for": m}))
            self.assertEqual(self.sess("devops")["role"], "agent")

    def test_rejections(self):
        self.assertIn("what it does", self.ev("session.role_set", "wk-1", role="agent", **FROM_HUB))
        self.assertIn("hub", self.ev("session.role_set", "hub", role="agent", about="x", **OWNER))
        self.assertIn("role", self.ev("session.role_set", "wk-1", role="boss", about="x", **OWNER))
        self.assertIsNotNone(self.ev("session.role_set", "nope", role="agent", about="x", **OWNER))
        self.assertIsNotNone(self.ev("session.role_set", "devops", about="myself", channel="session", owner=False,
                                     sender="devops"))


class View(unittest.TestCase):
    def test_snapshot_carries_role_about_icon(self):
        r = Reader()
        for i, (t, name, d) in enumerate([
                ("session.requested", "devops", {"cwd": "/k", "session_id": "s", "role": "agent",
                                                 "about": "DevOps", "icon": "🛠", **OWNER}),
                ("session.requested", "w", {"cwd": "/w", "session_id": "s2", **OWNER})], 1):
            r.feed({"seq": i, "ts": i, "type": t, "entity": name, "data": d, "src": "cli"})
        by = {s["name"]: s for s in r.snapshot()["sessions"]}
        self.assertEqual((by["devops"]["role"], by["devops"]["about"], by["devops"]["icon"]), ("agent", "DevOps", "🛠"))
        self.assertEqual(by["w"]["role"], "worker")


class Cli(unittest.TestCase):
    def cli(self, *args):
        state = tempfile.mkdtemp(prefix="roles-")
        e = {**os.environ, "HUB_STATE_DIR": state, "HUB_CLI_WAIT": "0"}
        e.pop("HUB_SESSION", None)
        subprocess.run([sys.executable, HUB, *args], env=e, capture_output=True, text=True)
        return [ev for _, ev in spool.take(os.path.join(state, "spool"))]

    def test_help_of_a_subcommand_prints_help_and_sends_nothing(self):
        # 30.09: a session ran "hub session new --help" and the core asked the hub to open a session named "--help"
        for args in (("session", "new", "--help"), ("session", "new", "-h"), ("schedule", "--help"),
                     ("session", "new", "-x", "--cwd", "/tmp")):
            self.assertEqual(self.cli(*args), [], args)
        # "hub decided --help" recorded "--help" as the owner's decision; for commands with free text, help is
        # shown only when the help flag is the whole text
        for cmd in ("decided", "notify", "close-ready", "send", "answer", "deny"):
            for flag in ("--help", "-h"):
                self.assertEqual(self.cli(cmd, flag), [], (cmd, flag))
        (ev,) = self.cli("decided", "added", "--help", "to the help")
        self.assertEqual((ev["type"], ev["data"]["text"]), ("owner.decided", "added --help to the help"))
        state = tempfile.mkdtemp(prefix="roles-")
        r = subprocess.run([sys.executable, HUB, "session", "new", "--help"], capture_output=True, text=True,
                           env={**os.environ, "HUB_STATE_DIR": state, "HUB_CLI_WAIT": "0"})
        self.assertIn("hub session new", r.stdout)
        r = subprocess.run([sys.executable, HUB, "session", "new", "Bad Name"], capture_output=True, text=True,
                           env={**os.environ, "HUB_STATE_DIR": state, "HUB_CLI_WAIT": "0"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("name", r.stderr + r.stdout)

    def test_commands(self):
        (ev,) = self.cli("session", "new", "devops", "--cwd", "/tmp", "--agent", "--about", "DevOps: k8s", "--icon", "🛠")
        self.assertEqual((ev["type"], ev["entity"]), ("session.requested", "devops"))
        self.assertEqual({k: ev["data"][k] for k in ("role", "about", "icon", "task")},
                         {"role": "agent", "about": "DevOps: k8s", "icon": "🛠", "task": None})
        (ev,) = self.cli("session", "role", "wk-1", "agent", "--about", "SEO")
        self.assertEqual((ev["type"], ev["entity"], ev["data"]["role"], ev["data"]["about"]),
                         ("session.role_set", "wk-1", "agent", "SEO"))
        (ev,) = self.cli("session", "role", "wk-1", "worker")
        self.assertEqual(ev["data"]["role"], "worker")
        (ev,) = self.cli("session", "role", "devops", "worker", "--for", "q12")
        self.assertEqual((ev["data"]["role"], ev["data"]["for"]), ("worker", "q12"))


if __name__ == "__main__":
    unittest.main()
