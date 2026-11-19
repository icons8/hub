"""The repo scaffold — a skill that only a human runs (`disable-model-invocation: true`: /ship, /…-work). The session
drives the work through it as a human would (owner decision 2026-10-07: respect the repo's rules and run development
the way a human always would). The 2026-10-07 case: the hub sent a session "/repo-work <ticket>", the core put the
[hub:mN] label before the text — Claude Code took the command for plain text, the scaffold did not start (checked on
live 2.1.292: with the label after the command the skill fires, the hook sees the label).

A command from the owner, the hub or the session itself (`hub slash`) goes on the first line, the label after; from
another session — as text. At startup the core tells the session which such skills its repo has, and the rule. The
model is not called."""
import tests  # noqa: F401 — do not see the machine's settings
import os
import tempfile
import unittest
from unittest import mock

from core import forms, reducer
from hooks import hub_hook

OWNER = {"channel": "web", "owner": True}


def skill(root, name, user_only=True, description="Work backbone: triage, spec, plan, execute, review, deliver"):
    d = os.path.join(root, ".claude", "skills", name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w") as f:
        f.write(f"---\nname: {name}\ndescription: {description}\nuser-invocable: true\n"
                + ("disable-model-invocation: true\n" if user_only else "") + "---\n\nBody.\n")


class Core(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **OWNER)
        self.ev("session.requested", "a", cwd="/w", session_id="s-a", **OWNER)
        self.ev("session.requested", "b", cwd="/w", session_id="s-b", **OWNER)
        for n in ("hub", "a", "b"):
            self.ev("session.started", n)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, self.acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 100 + self.seq, "type": type,
                                                           "entity": entity, "data": data})
        return why

    def typed(self):
        return [a["text"] for a in self.acts if a.get("do") == "type"]

    def test_hub_command_goes_first_and_the_marker_after(self):
        self.assertIsNone(self.ev("message.received", None, to="a", text="/repo-work TICK-1",
                                  channel="session", sender="hub"))
        text, = self.typed()
        self.assertTrue(text.startswith("/repo-work TICK-1\n"), text)
        mid = f"m{self.seq}"
        self.assertIn(f"[hub:{mid} from hub]", text)
        # the hook finds the label at the end of the input too: delivery is confirmed as usual
        kind, data = hub_hook.translate({"hook_event_name": "UserPromptSubmit", "session_id": "s-a", "prompt": text})
        self.assertEqual(data["marker"], mid)
        self.ev(kind, "a", **data)
        self.assertEqual(self.st["messages"][mid]["state"], "delivered")

    def test_owner_command_too(self):
        self.ev("message.received", None, to="a", text="  /repo-work TICK-1", **OWNER)
        self.assertTrue(self.typed()[0].startswith("/repo-work TICK-1\n"))

    def test_another_session_cannot_type_a_command(self):
        self.ev("message.received", None, to="a", text="/clear", channel="session", sender="b")
        self.assertTrue(self.typed()[0].startswith(f"[hub:m{self.seq} from b] /clear"))

    def test_a_path_is_not_a_command(self):
        self.ev("message.received", None, to="a", text="/home/x/report.md take a look", **OWNER)
        self.assertTrue(self.typed()[0].startswith("[hub:"))

    def test_session_asks_for_its_backbone_and_the_turn_stays_the_task(self):
        self.ev("delegation.created", None, session="a", context="fix the report", **OWNER)
        did = next(iter(self.st["delegations"]))
        mid = self.st["delegations"][did]["message"]
        self.ev("turn.started", "a", marker=mid, prompt_head="x")
        # mid-turn it asks to enter the scaffold for itself: waits for the end of the turn
        self.assertIsNone(self.ev("message.received", None, to="a", text="/repo-work fix the report",
                                  channel="session", sender="a", slash=True))
        ask = f"m{self.seq}"
        self.assertEqual(self.typed(), [])
        self.assertIsNone(self.st["messages"][ask].get("held"))
        self.ev("turn.result", "a", text="Starting the work via /repo-work.")   # the turn ended — it enters
        text, = [t for t in self.typed() if not t.startswith("[hub:")]
        self.assertTrue(text.startswith("/repo-work fix the report\n"), text)
        self.assertIn(f"[hub:{ask} from a]", text)
        # the scaffold's turn is a turn of the same task: its result goes to whoever set it
        self.ev("turn.started", "a", marker=ask, prompt_head=text[:200])
        self.assertEqual(self.st["sessions"]["a"]["delegation"], did)

    def test_session_may_not_end_itself_or_another(self):
        for cmd in ("/clear", "/exit", "/logout", "/login", "/resume x"):
            self.assertIsNotNone(self.ev("message.received", None, to="a", text=cmd, channel="session", sender="a",
                                         slash=True), cmd)
        # hub slash — to itself only
        self.assertIsNotNone(self.ev("message.received", None, to="b", text="/repo-work x", channel="session",
                                     sender="a", slash=True))
        self.assertIsNotNone(self.ev("message.received", None, to="a", text="repo-work x", channel="session",
                                     sender="a", slash=True))

    def test_a_command_is_not_inserted_mid_turn(self):
        self.ev("turn.started", "a", marker=None, prompt_head="x")
        self.ev("message.received", None, to="a", text="/repo-work x", **OWNER)
        # the watchdog's own shape (modules/watch/watch.py insert): refused by the command rule, not by who asks
        self.assertEqual(self.ev("message.inject", f"m{self.seq}", channel="cli", owner=True), reducer.QUEUE_ONLY)

    def test_builtin_command_is_delivered_by_its_result(self):
        # 2026-10-07: "/compact" from the page is a built-in Claude Code command: no hook, no labeled turn, only a
        # result without steps a minute later. The core waited for the label, repeated and dropped the message; the
        # next one stood in the queue behind it
        self.ev("message.received", None, to="a", text="/compact", **OWNER)
        mid = f"m{self.seq}"
        self.ev("message.received", None, to="a", text="you there?", **OWNER)
        self.assertEqual(self.typed(), [])
        self.ev("turn.result", "a", text="", subtype="success", is_error=False, turns=0)
        self.assertEqual(self.st["messages"][mid]["state"], "delivered")
        self.assertTrue(self.typed()[0].startswith(f"[hub:m{self.seq - 1}{forms.ORIGIN_OWNER}] you there?"), self.typed())

    def test_builtin_command_goes_without_the_marker(self):
        # 2026-10-07: "/effort" from the page — the label after the command became its argument: "Invalid argument:
        # [hub:mN from owner]". A built-in command needs no label (no hook, the result confirms); which ones are
        # built-in is told by Claude Code itself at startup (slash_commands without skills)
        self.ev("session.info", "a", model="m", builtins=["compact", "effort"])
        self.ev("message.received", None, to="a", text="/effort high", **OWNER)
        self.assertEqual(self.typed(), ["/effort high"])
        mid = f"m{self.seq}"
        self.ev("turn.result", "a", text="Set effort level to high", subtype="success", is_error=False, turns=0)
        self.assertEqual(self.st["messages"][mid]["state"], "delivered")
        # a skill — still with the label after
        self.ev("message.received", None, to="a", text="/repo-work TICK-1", **OWNER)
        self.assertIn(f"[hub:m{self.seq}{forms.ORIGIN_OWNER}]", self.typed()[0])

    def test_before_the_session_names_its_builtins_a_known_list_is_used(self):
        # the built-in list comes with init, and init comes after the first input: the first "/effort" after startup
        # also goes without a label
        self.ev("message.received", None, to="a", text="/effort high", **OWNER)
        self.assertEqual(self.typed(), ["/effort high"])

    def test_builtin_command_stands_in_the_feed_before_its_answer(self):
        # 2026-10-07: the page places a message by delivery time; for /effort that was the result — after the reply.
        # The command stood under its own reply, and the reply went into the previous turn's steps ("30 steps … 1466
        # min")
        self.ev("message.received", None, to="a", text="/effort", **OWNER)
        mid = f"m{self.seq}"
        typed = self.st["messages"][mid]["since"]
        self.ev("session.said", "a", text="Usage: /effort <level>")
        self.ev("turn.result", "a", text="Usage: /effort <level>", subtype="success", is_error=False, turns=0)
        self.assertEqual(self.st["messages"][mid]["delivered_at"], typed)

    def test_long_compact_does_not_fail_the_message(self):
        # compacting a big conversation takes minutes: the reply deadline for the command is not the 30 s of an
        # ordinary message
        self.ev("message.received", None, to="a", text="/compact", **OWNER)
        mid = f"m{self.seq}"
        for n in range(1, 4):
            self.ev("timer.fired", mid, attempt=n, n=n)
        self.assertEqual(self.st["messages"][mid]["state"], "typing")
        self.assertGreaterEqual(self.st["messages"][mid]["deadline"] - self.st["messages"][mid]["since"], 300)

    def test_hub_news_does_not_go_into_command_arguments(self):
        # "Hub updated…" was appended to the end of the input — for a command those are its arguments (2026-10-07:
        # /…-work got them into the task)
        self.ev("news.loaded", None, notes=["`hub x` new"], channel="core", owner=False)
        self.ev("message.received", None, to="a", text="/repo-work TICK-1", channel="session", sender="hub")
        self.assertNotIn("`hub x` new", self.typed()[0])   # the news line itself, in any language of its head
        mid = f"m{self.seq}"
        self.ev("turn.started", "a", marker=mid, prompt_head="/repo-work")
        self.ev("turn.result", "a", text="done", subtype="success", is_error=False, turns=3)
        self.ev("message.received", None, to="a", text="and now?", **OWNER)
        self.assertIn("`hub x` new", self.typed()[0])


def launch_text(cmd):
    """What claude gets: the command line and the suffix file it names (the suffix goes as a file, PR #499)."""
    text = " ".join(cmd)
    if "--append-system-prompt-file" in cmd:
        with open(cmd[cmd.index("--append-system-prompt-file") + 1], encoding="utf-8") as f:
            text += " " + f.read()
    return text


class Prompt(unittest.TestCase):
    """Human-only skills in the session's repo — as a fact in its system instructions, with the rule."""

    def command(self, cwd, **kw):
        from hosts import streamjson
        state = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"HUB_CONFIG": os.path.join(state, "hub.json")}):
            return launch_text(streamjson.StreamHost(state).command("s1", "id-1", cwd=cwd, **kw))

    def test_minihub_hands_the_backbone_to_an_executor(self):
        # a mini-hub with a scaffold in the repo got two conflicting rules — "assign" and "hub slash /<scaffold> to
        # yourself"; the second won: the scaffold opened a working copy, and the core rejected the mini-hub's edits
        # (2026-10-16). The mini-hub gets its own rule
        repo = tempfile.mkdtemp()
        os.mkdir(os.path.join(repo, ".git"))
        skill(repo, "repo-work")
        line = self.command(repo, minihub=True)
        self.assertIn("/repo-work — Work backbone", line)
        self.assertNotIn('hub slash "/<scaffold>', line)
        self.assertIn("through /<scaffold>", line)
        self.assertIn("`s1-<gist>`", line)   # a mini-hub cannot see its own name in the environment — the core writes it in
        plain = self.command(repo)
        self.assertIn('hub slash "/<scaffold>', plain)
        self.assertNotIn("through /<scaffold>", plain)
        self.assertNotIn("through /<scaffold>", self.command(tempfile.mkdtemp(), minihub=True))   # no scaffold — nothing

    def test_the_host_names_the_backbone_on_start(self):
        # the core does not enter the scaffold for a mini-hub (core/reducer._command): which repo skills are
        # human-only is a host fact
        from hosts.streamjson import StreamHost
        repo = tempfile.mkdtemp()
        os.mkdir(os.path.join(repo, ".git"))
        skill(repo, "repo-work")
        skill(repo, "helper", user_only=False)
        host = StreamHost(tempfile.mkdtemp())
        host.inbox.put(("a", {"type": "system", "subtype": "init", "model": "m", "cwd": repo, "mcp_servers": []}))
        (fact,) = [f for f in host.poll() if f[0] == "session.info"]
        self.assertEqual(fact[2]["backbone"], ["repo-work"])

    def test_user_only_skills_of_the_repo_are_named_with_the_rule(self):
        repo = tempfile.mkdtemp()
        os.mkdir(os.path.join(repo, ".git"))
        skill(repo, "repo-work")
        skill(repo, "repo-doctor", description="Harness self-healing")
        skill(repo, "helper", user_only=False)
        wt = os.path.join(repo, "src")
        os.makedirs(wt)
        line = self.command(wt)   # a session in a repo subfolder — the repo root's skills are the same
        self.assertIn("/repo-work — Work backbone", line)
        self.assertIn("/repo-doctor", line)
        self.assertNotIn("/helper", line)
        self.assertIn('hub slash "/<scaffold>', line)

    def test_commands_folder_counts_too(self):
        repo = tempfile.mkdtemp()
        os.makedirs(os.path.join(repo, ".claude", "commands"))
        with open(os.path.join(repo, ".claude", "commands", "ship.md"), "w") as f:
            f.write("---\ndescription: Ship a change end to end\ndisable-model-invocation: true\n---\nBody\n")
        self.assertIn("/ship — Ship a change", self.command(repo))

    def test_multiline_description(self):
        # a YAML block `>-` / `|` — text on the following indented lines (another session, 2026-10-07: "/x — >-" for
        # all skills)
        repo = tempfile.mkdtemp()
        for name, style in (("folded-work", ">-"), ("literal-work", "|")):
            d = os.path.join(repo, ".claude", "skills", name)
            os.makedirs(d)
            with open(os.path.join(d, "SKILL.md"), "w") as f:
                f.write(f"---\nname: {name}\ndescription: {style}\n  Takes a task from ticket\n  to MR.\n"
                        "argument-hint: \"[TICK-1]\"\ndisable-model-invocation: true\n---\nBody\n")
        line = self.command(repo)
        self.assertIn("/folded-work — Takes a task from ticket to MR.", line)
        self.assertIn("/literal-work — Takes a task from ticket to MR.", line)
        self.assertNotIn(">-", line)

    def test_no_such_skills_no_rule(self):
        repo = tempfile.mkdtemp()
        skill(repo, "helper", user_only=False)
        self.assertNotIn("hub slash", self.command(repo))
        self.assertNotIn("hub slash", self.command(None))

    def test_spawn_passes_the_cwd(self):
        from hosts import streamjson
        repo = tempfile.mkdtemp()
        skill(repo, "repo-work")
        host = streamjson.StreamHost(tempfile.mkdtemp())
        seen = {}

        def popen(cmd, **kw):
            seen["cmd"] = cmd
            raise OSError("do not launch")
        with mock.patch.object(streamjson.subprocess, "Popen", popen), self.assertRaises(OSError):
            host.spawn("s1", "id-1", cwd=repo)
        self.assertIn("/repo-work", launch_text(seen["cmd"]))


class Cli(unittest.TestCase):
    def test_hub_slash_needs_a_command_and_a_session(self):
        import subprocess
        import sys
        hub = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")
        state = tempfile.mkdtemp()
        env = {**os.environ, "HUB_STATE_DIR": state, "HUB_SESSION": "a"}
        r = subprocess.run([sys.executable, hub, "slash", "repo-work", "x"], env=env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("/", r.stderr)
        env.pop("HUB_SESSION")
        r = subprocess.run([sys.executable, hub, "slash", "/repo-work", "x"], env=env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        r = subprocess.run([sys.executable, hub, "slash", "--help"], env={**env, "HUB_SESSION": "a"},
                           capture_output=True, text=True)
        self.assertIn("hub slash", r.stdout)


if __name__ == "__main__":
    unittest.main()
