"""An agent's own CLAUDE.md (owner decision 2026-09-30: since agents now exist, they should write their own CLAUDE.md).
The file lives with the core (`<state>/agents/<id>/CLAUDE.md`), not in the repo: different sessions and people work in
one repo. The core hands it to the agent at launch (--append-system-prompt-file), and the agent maintains it itself.
The key is the agent's sessionId: renaming does not lose it."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from core import reducer
from hosts.streamjson import LESSONS, StreamHost

OWNER = {"channel": "cli", "owner": True}


class Core(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.actions = reducer.empty(), 0, []
        self.ev("session.requested", "devops", cwd="/k", session_id="s-dev", role="agent", about="DevOps", **OWNER)
        self.ev("session.requested", "w", cwd="/w", session_id="s-w", **OWNER)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 100 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.actions += acts
        return why

    def spawn(self, name):
        return [a for a in self.actions if a["do"] == "spawn" and a["session"] == name][-1]

    def test_agent_gets_its_file_worker_does_not(self):
        self.assertEqual((self.spawn("devops")["profile"], self.spawn("devops")["about"]), ("s-dev", "DevOps"))
        self.assertIsNone(self.spawn("w").get("profile"))

    def test_rename_keeps_the_agents_file(self):
        self.ev("session.started", "devops")
        self.st["sessions"]["devops"]["state"] = "asleep"
        self.ev("session.renamed", "devops", to="ops", **OWNER)
        self.ev("session.wake_requested", "ops")
        self.assertEqual(self.spawn("ops")["profile"], "s-dev")

    def test_cleared_conversation_keeps_the_agents_file(self):
        # 02.11: /clear (compaction thrash) gave the agent a new session_id, and the core handed it a fresh template
        # keyed by the new id instead of its own CLAUDE.md
        self.ev("session.started", "devops", session_id="s-dev")
        self.ev("session.started", "devops", session_id="s-dev-2", source="clear")
        self.ev("session.started", "devops", session_id="s-dev-3", source="clear")
        self.st["sessions"]["devops"]["state"] = "asleep"
        self.ev("session.wake_requested", "devops")
        self.assertEqual(self.spawn("devops")["profile"], "s-dev")

    def test_an_old_agent_without_profile_is_pinned_on_load(self):
        st = reducer.loaded({"sessions": {"a": {"name": "a", "role": "agent", "session_id": "s-a"},
                                          "w": {"name": "w", "session_id": "s-w"}}})
        self.assertEqual((st["sessions"]["a"].get("profile"), st["sessions"]["w"].get("profile")), ("s-a", None))

    def test_promoted_worker_gets_a_file(self):
        self.ev("session.role_set", "w", role="agent", about="SEO", **OWNER)
        self.st["sessions"]["w"]["state"] = "asleep"
        self.ev("session.wake_requested", "w")
        self.assertEqual(self.spawn("w")["profile"], "s-w")


class Roster(unittest.TestCase):
    """Owner decision 2026-09-30: every session gets the list of agents in the system at start — and goes to them by
    their profile (front end — to the front-end agent) instead of doing someone else's work itself."""

    def test_every_session_starts_with_the_list_of_agents(self):
        c = Core()
        c.setUp()
        c.ev("session.requested", "fe", cwd="/fe", session_id="s-fe", role="agent", about="Frontend", **OWNER)
        c.ev("session.requested", "task-1", cwd="/x", session_id="s-t", **OWNER)
        self.assertEqual(c.spawn("task-1")["agents"], [["devops", "DevOps"], ["fe", "Frontend"]])
        self.assertEqual(c.spawn("fe")["agents"], [["devops", "DevOps"]])       # it is not in its own list
        host = StreamHost(tempfile.mkdtemp(prefix="roster-"), claude=("echo",))
        cmd = host.command("task-1", "s-t", agents=[["devops", "DevOps"], ["fe", "Frontend"]])
        with open(cmd[cmd.index("--append-system-prompt-file") + 1], encoding="utf-8") as f:
            text = f.read()
        self.assertIn("- fe — Frontend", text)
        self.assertIn("hub send", text)
        self.assertIn("The hub's agents", text)
        cmd = host.command("a", "s-a")
        with open(cmd[cmd.index("--append-system-prompt-file") + 1], encoding="utf-8") as f:
            self.assertNotIn("The hub's agents", f.read())   # no agents — no list


class LiveRename(unittest.TestCase):
    """An agent cannot be parked, and only a sleeping session could be renamed: the core restarts a free live session
    under the new name with the same conversation."""

    def test_idle_session_is_restarted_under_the_new_name(self):
        c = Core()
        c.setUp()
        c.ev("session.started", "devops")
        c.actions.clear()
        self.assertIsNone(c.ev("session.renamed", "devops", to="ops", **OWNER))
        self.assertEqual(c.st["sessions"]["ops"]["state"], "starting")
        self.assertEqual([(a["do"], a["session"]) for a in c.actions], [("release", "devops"), ("spawn", "ops")])
        # a busy one renames itself once it is free (2026-10-01: a session did not become a direction — the rename
        # was rejected mid-turn and nobody retried it)
        c.ev("session.started", "ops")
        c.ev("turn.started", "ops", marker=None, prompt_head="x")
        self.assertIsNone(c.ev("session.renamed", "ops", to="ops2", **OWNER))
        self.assertIn("ops", c.st["sessions"])
        c.ev("turn.result", "ops", text="done")
        self.assertNotIn("ops", c.st["sessions"])
        self.assertEqual(c.st["sessions"]["ops2"]["state"], "starting")


def reducer_tools_min():
    return reducer.REFLECT_TOOLS


class Reflect(unittest.TestCase):
    """Owner decision 2026-10-01: agents did not add a line to their CLAUDE.md — one instruction is not enough. After a
    closed delegation the core itself asks the agent to add what is durable; no more than once per REFLECT_EVERY, and
    not to an executor or a plain worker."""

    def setUp(self):
        self.c = Core()
        self.c.setUp()
        self.c.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **OWNER)
        for n in ("hub", "devops", "w"):
            self.c.ev("session.started", n)

    def delegate(self, name, ts_text="done, MR !5"):
        c = self.c
        c.ev("message.received", None, to="hub", text="task", **OWNER)
        mid = max(c.st["messages"], key=lambda m: int(m[1:].split("x")[0]))
        c.ev("delegation.created", None, **{"session": name, "for": mid, "context": "do", "channel": "session",
                                            "owner": False, "sender": "hub"})
        d = max(c.st["delegations"].values(), key=lambda d: d["created"])
        c.ev("turn.started", name, marker=d["message"], prompt_head="x")
        c.ev("turn.result", name, text=ts_text)
        return d

    def asks(self, name):
        return [m for m in self.c.st["messages"].values() if m["session"] == name and "CLAUDE.md" in m["text"]]

    def test_agent_is_asked_after_a_closed_delegation_once_in_a_while(self):
        self.delegate("devops")
        (m,) = self.asks("devops")
        self.assertIn("$HUB_AGENT_FILE", m["text"])   # the path comes from the session environment: each hub has its own state folder
        self.assertIn("what lasts", m["text"])
        self.delegate("devops")                        # a second one right away — do not ask again
        self.assertEqual(len(self.asks("devops")), 1)

    def test_agent_is_asked_after_a_big_turn_without_a_delegation(self):
        # 2026-10-01: in half a day the reminder never came — agents are written to via hub send, they get few delegations
        c = self.c
        c.ev("turn.started", "devops", marker=None, prompt_head="fix the rollout")
        for i in range(reducer_tools_min()):
            c.ev("turn.tool", "devops", id=f"t{i}", name="Bash", input={})
        c.ev("turn.result", "devops", text="rolled out, checked")
        self.assertEqual(len(self.asks("devops")), 1)
        c.ev("turn.started", "devops", marker=None, prompt_head="a trifle")
        c.ev("turn.tool", "devops", id="x", name="Bash", input={})
        c.ev("turn.result", "devops", text="ok")
        self.assertEqual(len(self.asks("devops")), 1)

    def test_worker_is_not_asked(self):
        self.delegate("w")
        self.assertEqual(self.asks("w"), [])


class Host(unittest.TestCase):
    def test_file_is_created_once_and_passed_to_claude(self):
        state = tempfile.mkdtemp(prefix="agentfile-")
        host = StreamHost(state, claude=("echo",))
        path = host.agent_file("s-dev", name="devops", about="DevOps: k8s")
        self.assertEqual(path, os.path.join(state, "agents", "s-dev", "CLAUDE.md"))
        with open(path) as f:
            text = f.read()
        self.assertIn("DevOps: k8s", text)
        self.assertIn(path, text)                              # the agent knows where its file is, so it can edit it
        with open(path, "a") as f:
            f.write("\n- lesson: do not touch prod without an MR\n")
        host.agent_file("s-dev", name="devops", about="other")  # exists — not overwritten
        with open(path) as f:
            self.assertIn("lesson: do not touch prod without an MR", f.read())
        cmd = host.command("devops", "s-dev", agent_file=path)
        with open(cmd[cmd.index("--append-system-prompt-file") + 1]) as f:
            text = f.read()
        self.assertIn("lesson: do not touch prod without an MR", text)   # the agent's file is in its system instructions
        self.assertIn(LESSONS, text)   # how to record a lesson: the agent keeps the file (2026-10-28, stops audit)
        cmd = host.command("devops-2", "s-ex", agent_file=path, executor_of="devops")
        with open(cmd[cmd.index("--append-system-prompt-file") + 1]) as f:
            self.assertNotIn(LESSONS, f.read())   # its executor hands lessons in the result instead


class Language(unittest.TestCase):
    """The template is in the person's language (catalog agent_file.*, languages spec §4.4): the agent keeps the file in
    the language it is written in. A Russian hub gets the Russian catalog's text in the same layout as before languages."""

    @staticmethod
    def ru(path):
        """The Russian template: the parts of the Russian catalog (agent_file.*) in the layout of the file."""
        from core import i18n

        def t(key, **kw):
            return i18n.tr("ru", f"agent_file.{key}", **kw)
        return (f"{t('title', name='devops', about='DevOps: k8s')}\n\n{t('intro', path=path)}\n\n{t('profile')}\n"
                f"DevOps: k8s\n\n{t('knowledge')}\n\n{t('how')}\n\n{t('lessons')}\n")

    def setUp(self):
        env = mock.patch.dict(os.environ)   # without the language pinned for tests (tests/__init__.py HUB_LANG)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("HUB_LANG", None)

    def made(self, lang, cfg=None):
        state = tempfile.mkdtemp(prefix="agentfile-")
        self.addCleanup(shutil.rmtree, state, ignore_errors=True)
        if cfg is not None:
            path = os.path.join(state, "hub.json")
            with open(path, "w") as f:
                json.dump(cfg, f)
            env = mock.patch.dict(os.environ, {"HUB_CONFIG": path})
            env.start()
            self.addCleanup(env.stop)
        path = StreamHost(state, claude=("echo",)).agent_file("s-dev", name="devops", about="DevOps: k8s", lang=lang)
        with open(path, encoding="utf-8") as f:
            return f.read(), path

    def test_russian_is_the_template_as_before(self):
        text, path = self.made("ru")
        self.assertEqual(text, self.ru(path))
        self.assertRegex(text, "[\u0410-\u044f\u0401\u0451]")   # the catalog is Russian, not the English fallback

    def test_english_and_other_languages(self):
        text, path = self.made("en")
        self.assertTrue(text.startswith("# Agent devops: DevOps: k8s\n\nThis is your own CLAUDE.md"))
        self.assertIn(path, text)
        self.assertNotRegex(text, "[\u0410-\u044f\u0401\u0451]")
        self.assertIn("## Wissen", self.made("de")[0])
        self.assertTrue(self.made(None)[0].startswith("# Agent devops"))    # language unknown — English

    def test_the_setting_wins(self):
        self.assertIn("## Profil\n", self.made("ru", {"language": "fr"})[0])

    def test_spawn_carries_the_language_of_the_state(self):
        from core import reducer
        st = reducer.empty()
        st["sessions"]["a"] = {"name": "a", "session_id": "s-a", "cwd": "/w", "state": "asleep"}
        st["ui_lang_default"] = "ru"
        self.assertEqual(reducer.spawn_action(st["sessions"]["a"], st=st)["ui_lang"], "ru")
        st["owner"]["lang"] = "uk"
        self.assertEqual(reducer.spawn_action(st["sessions"]["a"], st=st)["ui_lang"], "uk")
        st["owner"]["lang"], st["ui_lang_default"] = "latin", ""
        self.assertNotIn("ui_lang", reducer.spawn_action(st["sessions"]["a"], st=st))


class OneAppendFlag(unittest.TestCase):
    """2026-10-06, Windows stand: Claude Code 2.1.218 does not accept `--append-system-prompt` together with
    `--append-system-prompt-file` — an agent with its own file and the agent list crashed at launch in a loop. The core
    passes one flag: the agent's file and the core's addendum as one file in the session folder."""

    def test_agent_file_and_note_go_as_one_file(self):
        state = tempfile.mkdtemp(prefix="oneflag-")
        own = os.path.join(state, "own.md")
        with open(own, "w") as f:
            f.write("# Agent devops\nlesson: do not touch prod without an MR\n")
        host = StreamHost(state, claude=("echo",))
        host.fresh_prompt = False
        cmd = host.command("devops", "s-dev", agent_file=own, agents=[["fe", "Frontend"]])
        self.assertNotIn("--append-system-prompt", cmd)
        self.assertEqual(cmd.count("--append-system-prompt-file"), 1)
        with open(cmd[cmd.index("--append-system-prompt-file") + 1]) as f:
            text = f.read()
        self.assertIn("lesson: do not touch prod without an MR", text)   # the agent's file is fresh, as is
        self.assertIn("- fe — Frontend", text)                # and the core's addendum
        with open(own, "a") as f:
            f.write("a new lesson\n")
        cmd = host.command("devops", "s-dev", agent_file=own, agents=[["fe", "Frontend"]])
        with open(cmd[cmd.index("--append-system-prompt-file") + 1]) as f:
            self.assertIn("a new lesson", f.read())             # rebuilt on every launch

    def test_without_an_agent_file_the_note_goes_as_a_file_too(self):
        # 2026-11-06, Windows: the suffix as an argument (rules ~19.5 thousand characters and more) overran the command line
        # (~32 thousand), WinError 206, the session did not come up. Without an agent file the suffix is a file as well, one flag
        host = StreamHost(tempfile.mkdtemp(prefix="oneflag-"), claude=("echo",))
        host.fresh_prompt = False
        long_rule = ("A long hub rule. " * 2000).strip()   # ~33 thousand characters: more than a Windows command line takes
        host.hub_settings = lambda: {"rules": [long_rule]}
        cmd = host.command("t", "s-t", agents=[["fe", "Frontend"]])
        self.assertEqual((cmd.count("--append-system-prompt"), cmd.count("--append-system-prompt-file")), (0, 1))
        self.assertLess(max(len(a) for a in cmd), 1000)   # no long text in the arguments
        with open(cmd[cmd.index("--append-system-prompt-file") + 1], encoding="utf-8") as f:
            text = f.read()
        self.assertIn(long_rule, text)
        self.assertIn("- fe — Frontend", text)
        with open(cmd[cmd.index("--append-system-prompt-file") + 1], "rb") as f:
            self.assertNotIn(b"\r", f.read())   # Windows: the lines as written, without "\r" in every line of the prompt


class FreshOnResume(unittest.TestCase):
    """2026-10-06, agent-dev: edits to an agent's own CLAUDE.md did not reach it. Claude Code (2.1.29x) records the
    system prompt at the first request of a conversation and on --resume sends the recording until the conversation is
    compacted (--system-prompt-snapshot on by default). An agent gets `--system-prompt-snapshot off`: the file and the
    agent list are re-read; an old claude without this flag gets none (an unknown flag would crash the launch)."""

    def host(self, supports):
        h = StreamHost(tempfile.mkdtemp(prefix="snap-"), claude=("echo",))
        h.fresh_prompt = supports
        return h

    def test_an_agent_is_launched_with_a_fresh_prompt(self):
        cmd = self.host(True).command("devops", "s-dev", resume=True, agent_file="/x/CLAUDE.md")
        i = cmd.index("--system-prompt-snapshot")
        self.assertEqual(cmd[i + 1], "off")

    def test_an_old_claude_gets_no_flag(self):
        self.assertNotIn("--system-prompt-snapshot",
                         self.host(False).command("devops", "s-dev", resume=True, agent_file="/x/CLAUDE.md"))

    def test_a_session_without_its_own_file_gets_a_fresh_prompt_too(self):
        # the core's addendum (language rule, 10-09) is for everyone: an old session on --resume must get the new one
        cmd = self.host(True).command("w", "s-w", resume=True)
        self.assertEqual(cmd[cmd.index("--system-prompt-snapshot") + 1], "off")

    def test_support_is_read_from_claude_help(self):
        h = StreamHost(tempfile.mkdtemp(prefix="snap-"),
                       claude=(sys.executable, "-c", "print('  --system-prompt-snapshot <on|off>')"))
        self.assertTrue(h.fresh_prompt)
        h = StreamHost(tempfile.mkdtemp(prefix="snap-"), claude=(sys.executable, "-c", "print('  --verbose')"))
        self.assertFalse(h.fresh_prompt)


if __name__ == "__main__":
    unittest.main()
