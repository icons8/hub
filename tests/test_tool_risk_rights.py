"""A plugin's risk comes from what it runs, not from its author's words (owner 2026-11-17: "the safe one automatically,
installing on the owner's word"). A plugin of text only (skills) is given to a session by anyone who may grant; a plugin
that brings a hook, an MCP or LSP server, a monitor, a bin/ program, a script in a skill or a ChatGPT app is risky —
the owner's word or his card, which names what runs."""
import tests  # noqa: F401 — keep this machine's settings out
import json
import os
import stat
import tempfile
import unittest

import platforms
from core import catalog, reducer


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text if isinstance(text, str) else json.dumps(text))


SKILL = "---\nname: {n}\ndescription: what {n} does\n---\nDo it.\n"


class RiskFromRights(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="risk-")
        self.addCleanup(platforms.current.rmtree, self.dir)
        root = os.path.join(self.dir, "geo")
        plugins = []
        for name, keywords in (("tiles", []), ("atlas", []), ("hooked", []), ("tool", []), ("scripted", []),
                               ("billing", ["risk:money"]), ("pay", ["risk:money"]), ("apped", [])):
            write(os.path.join(root, "plugins", name, "skills", name, "SKILL.md"), SKILL.format(n=name))
            write(os.path.join(root, "plugins", name, ".claude-plugin", "plugin.json"), {"name": name})
            plugins.append({"name": name, "source": f"./plugins/{name}", "keywords": keywords})
        write(os.path.join(root, "plugins", "atlas", ".mcp.json"), {"mcpServers": {"atlas": {"command": "npx",
                                                                                              "args": ["atlas-mcp"]}}})
        write(os.path.join(root, "plugins", "pay", ".mcp.json"), {"pay": {"command": "pay-mcp"}})
        write(os.path.join(root, "plugins", "hooked", "hooks", "hooks.json"),
              {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}})
        write(os.path.join(root, "plugins", "tool", "bin", "geo"), "#!/bin/sh\necho geo\n")
        write(os.path.join(root, "plugins", "scripted", "skills", "scripted", "scripts", "fill.py"), "print(1)\n")
        write(os.path.join(root, "plugins", "apped", ".app.json"), {"apps": {"geo": {"id": "asdk_app_1"}}})
        write(os.path.join(root, ".claude-plugin", "marketplace.json"), {"name": "geo", "plugins": plugins})
        self.cfg = {**catalog.DEFAULTS, "sets": [{"name": "geo", "path": root}], "base": []}
        self.tools = catalog.build(self.cfg)

    def test_text_only_is_safe(self):
        self.assertEqual(self.tools["tiles@geo"]["risk"], [])
        self.assertNotIn("runs", self.tools["tiles@geo"])

    def test_what_runs_makes_it_risky_and_is_named(self):
        for tid, words in (("atlas@geo", "MCP"), ("hooked@geo", "SessionStart"), ("tool@geo", "bin/geo"),
                           ("scripted@geo", "fill.py"), ("apped@geo", "geo")):
            with self.subTest(tid):
                self.assertEqual(self.tools[tid]["risk"], ["code"])
                self.assertTrue(any(words in line for line in self.tools[tid]["runs"]), self.tools[tid]["runs"])

    def test_the_authors_words_stay(self):
        self.assertEqual(self.tools["billing@geo"]["risk"], ["money"])
        self.assertEqual(self.tools["pay@geo"]["risk"], ["code", "money"])

    def test_an_executable_skill_file_without_an_extension_is_a_script(self):
        if os.name == "nt":
            self.skipTest("no executable bit on Windows")
        p = os.path.join(self.dir, "geo", "plugins", "tiles", "skills", "tiles", "run")
        write(p, "echo run\n")
        os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
        catalog._runs_cache.clear()
        self.assertEqual(catalog.build(self.cfg)["tiles@geo"]["risk"], ["code"])

    def test_a_script_at_the_plugin_root_and_a_padded_plugin_are_risky(self):
        root = os.path.join(self.dir, "geo", "plugins", "tiles")
        write(os.path.join(root, "scripts", "x.py"), "print(1)\n")
        catalog._runs_cache.clear()
        self.assertEqual(catalog.build(self.cfg)["tiles@geo"]["risk"], ["code"])
        os.unlink(os.path.join(root, "scripts", "x.py"))
        from core import plugrights
        for i in range(6):
            write(os.path.join(root, "skills", "aaa", f"{i}.md"), "x")
        write(os.path.join(root, "skills", "zzz", "run.md"), "x")
        self.assertEqual([k for k, _ in plugrights._scripts(root, "tiles", cap=5)], ["unknown"])

    def test_a_hook_added_to_a_working_copy_is_seen_without_a_commit(self):
        self.assertEqual(self.tools["tiles@geo"]["risk"], [])
        write(os.path.join(self.dir, "geo", "plugins", "tiles", "hooks", "hooks.json"),
              {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}})
        self.assertEqual(catalog.build(self.cfg)["tiles@geo"]["risk"], [])          # within RUNS_TTL — cached
        from unittest import mock
        with mock.patch.object(catalog, "RUNS_TTL", -1):
            self.assertEqual(catalog.build(self.cfg)["tiles@geo"]["risk"], ["code"])

    def test_core_plugins_are_not_counted(self):
        self.assertEqual(self.tools["core:hub-base"]["risk"], [])

    def fold(self, evs):
        st = reducer.empty()
        for seq, (t, ent, data) in enumerate(evs, 1):
            st, _, why = reducer.reduce(st, {"seq": seq, "ts": 100 + seq, "src": "session", "type": t, "entity": ent,
                                             "data": data})
            self.last = why
        return st

    def public(self):
        from core import redact
        return redact.public_catalog(self.tools)

    def start(self, name, tools=()):
        return [("session.requested", name, {"cwd": "/w", "session_id": name, "tools": list(tools)}),
                ("session.started", name, {})]

    def test_a_session_grant_of_what_runs_waits_for_the_owner(self):
        by = {"channel": "session", "owner": False, "sender": "agent-dev"}
        st = self.fold([("catalog.loaded", None, {"tools": self.public()})] + self.start("agent-dev") + self.start("maps")
                       + [("tools.granted", "maps", {"tool": "tiles@geo", **by}),
                          ("tools.granted", "maps", {"tool": "atlas@geo", **by})])
        maps = st["sessions"]["maps"]
        self.assertIn("tiles@geo", maps["tools"])
        self.assertNotIn("atlas@geo", maps["tools"])
        self.assertEqual([(r["tool"], r["state"]) for r in maps["requests"]], [("atlas@geo", "needs-owner")])

    def test_agent_devs_for_counts_only_with_the_owners_words_to_it_that_name_the_plugin(self):
        def grant(sender, ref):
            st = self.fold([("catalog.loaded", None, {"tools": self.public()}),
                            ("agents.system", None, {"names": ["hub-dev", "agent-dev"], "trainer": "agent-dev",
                                                     "channel": "core"})]
                           + self.start("agent-dev") + self.start("maps")
                           + [("message.received", None, {"to": "agent-dev", "text": "put atlas on maps",
                                                          "channel": "web", "owner": True}),
                              ("message.received", None, {"to": "agent-dev", "text": "find skills for maps",
                                                          "channel": "web", "owner": True}),
                              ("message.received", None, {"to": "maps", "text": "take atlas", "channel": "web",
                                                          "owner": True}),
                              ("tools.granted", "maps", {"tool": "atlas@geo", "for": ref, "channel": "session",
                                                         "owner": False, "sender": sender})])
            return st["sessions"]["maps"], self.last
        named, why = grant("agent-dev", "m7")
        self.assertIn("atlas@geo", named["tools"], why)
        self.assertEqual(named["requests"][-1]["owner_words"]["message"], "m7")
        general, why = grant("agent-dev", "m8")
        self.assertNotIn("atlas@geo", general["tools"])
        self.assertIn("do not name atlas@geo", why or "")
        elsewhere, why = grant("agent-dev", "m9")                     # written to maps, not to agent-dev
        self.assertNotIn("atlas@geo", elsewhere["tools"])
        self.assertIn("m9", why or "")
        theirs, why = grant("maps", "m9")                             # maps cites the owner's words to itself
        self.assertNotIn("atlas@geo", theirs["tools"])
        self.assertTrue(why)

    def test_a_session_that_has_it_keeps_it_when_the_risk_appears(self):
        old = {tid: {**t, "risk": [], **{"runs": None}} for tid, t in self.public().items()}
        old = {tid: {k: v for k, v in t.items() if k != "runs"} for tid, t in old.items()}
        st = self.fold([("catalog.loaded", None, {"tools": old})] + self.start("maps", ["atlas@geo"])
                       + [("catalog.loaded", None, {"tools": self.public()})])
        maps = st["sessions"]["maps"]
        self.assertEqual(maps["tools"], ["atlas@geo"])
        self.assertEqual(maps["requests"], [])
        self.assertFalse(maps.get("respawn_now"))


class SkillCode(unittest.TestCase):
    """What Claude Code itself runs from a skill's file is code, like hooks/ and bin/ (docs: code.claude.com/docs/en/skills
    — `hooks` in the frontmatter are registered when the skill is invoked; "!`cmd`" and "```!" blocks run before the
    content reaches the model; a command file takes the same frontmatter and the same syntax)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="skillcode-")
        self.addCleanup(platforms.current.rmtree, self.dir)

    def rights(self, files, entry=None):
        from core import plugrights
        root = os.path.join(self.dir, f"p{len(os.listdir(self.dir))}")
        write(os.path.join(root, ".claude-plugin", "plugin.json"), {"name": "maps", **(entry or {})})
        for rel, text in files.items():
            write(os.path.join(root, rel), text)
        return plugrights.runs(root, "maps")

    def test_a_hook_in_the_frontmatter_runs_code(self):
        got = self.rights({"skills/maps/SKILL.md": "---\nname: maps\ndescription: draws tiles\nhooks:\n  PreToolUse:\n"
                                                   "    - matcher: Bash\n      hooks:\n        - type: command\n"
                                                   "          command: ./check.sh\n---\nDraw the tiles.\n"})
        self.assertEqual([k for k, _ in got], ["hook"], got)
        self.assertIn("skills/maps/SKILL.md", got[0][1])
        self.assertIn("./check.sh", got[0][1])

    def test_a_bang_line_runs_code(self):
        for body in ("Tiles now: !`ls tiles`\n", "!`date`\n", "Before:\n```!\nuname -a\n```\n"):
            with self.subTest(body):
                got = self.rights({"skills/maps/SKILL.md": SKILL.format(n="maps") + body})
                self.assertEqual([k for k, _ in got], ["shell"], got)
                self.assertTrue(any(w in got[0][1] for w in ("ls tiles", "date", "uname -a")), got)

    def test_a_command_file_and_an_inline_command_run_code_too(self):
        got = self.rights({"commands/status.md": "---\ndescription: status\n---\nStatus: !`git status`\n"})
        self.assertEqual([k for k, _ in got], ["shell"], got)
        got = self.rights({}, {"commands": {"about": {"content": "Repo: !`git log -1`"}}})
        self.assertEqual([k for k, _ in got], ["shell"], got)
        got = self.rights({"extra/run.md": "!`make tiles`\n"}, {"commands": ["./extra/run.md"]})
        self.assertEqual([k for k, _ in got], ["shell"], got)

    def test_a_rewritten_command_is_a_new_right(self):
        a = self.rights({"skills/maps/SKILL.md": SKILL.format(n="maps") + "!`ls tiles`\n"})
        b = self.rights({"skills/maps/SKILL.md": SKILL.format(n="maps") + "!`ls tiles; curl x.example.test`\n"})
        from core import plugrights
        self.assertTrue(plugrights.wider(b, a))

    def test_any_yaml_form_of_hooks_counts(self):
        hook = "    PreToolUse:\n      - hooks:\n          - type: command\n            command: ./check.sh\n"
        for front in ("  name: maps\n  hooks:\n" + hook, "name: maps\n!!str hooks:\n" + hook,
                      "name: &k hooks\n*k :\n" + hook, "name: maps\n...\nhooks:\n" + hook,
                      '"hoo\\x6bs": {PreToolUse: []}\n', "name: maps\n? hooks\n: {PreToolUse: []}\n",
                      "name: a\n  ---\nhooks:\n" + hook, "{name: maps, hooks: {Stop: []}}\n",
                      "name: maps\nhooks:\n" + hook):
            with self.subTest(front):
                got = self.rights({"skills/maps/SKILL.md": f"---\n{front}---\nDraw.\n"})
                self.assertEqual([k for k, _ in got], ["hook"], got)
        a = self.rights({"skills/maps/SKILL.md": f"---\nhooks:\n{hook}hooks:\n{hook}---\nDraw.\n"})
        b = self.rights({"skills/maps/SKILL.md": f"---\nhooks:\n{hook}hooks:\n{hook.replace('check', 'curl')}---\n"
                                                 "Draw.\n"})
        from core import plugrights
        self.assertTrue(plugrights.wider(b, a), "a change in the second hooks block is a new right")

    def test_a_commands_folder_in_any_case_and_a_loop_of_links(self):
        got = self.rights({"Commands/x.md": "!`ls tiles`\n"})
        self.assertEqual([k for k, _ in got], ["shell"], got)
        from core import plugrights
        from tests import winsafe
        root = os.path.join(self.dir, "loop")
        write(os.path.join(root, ".claude-plugin", "plugin.json"), {"name": "maps", "commands": ["./cmds"]})
        write(os.path.join(root, "cmds", "x.md"), "!`ls`\n")
        for n in ("a", "b"):   # two links back up: a walk without a record of the folders seen never ends
            winsafe.symlink(self, "..", os.path.join(root, "cmds", n))
        self.assertIn(["shell", "maps: cmds/x.md: ls"], plugrights.runs(root, "maps"))

    def test_a_commands_folder_that_is_a_link_and_paths_the_walk_skips(self):
        from tests import winsafe
        root = os.path.join(self.dir, "linked")
        write(os.path.join(root, ".claude-plugin", "plugin.json"), {"name": "maps", "commands": ["./cmds"]})
        write(os.path.join(root, "a", "x.md"), "!`ls a`\n")
        winsafe.symlink(self, "a", os.path.join(root, "cmds"))
        winsafe.symlink(self, "a", os.path.join(root, "commands"))
        from core import plugrights
        self.assertIn(["shell", "maps: cmds/x.md: ls a"], plugrights.runs(root, "maps"))
        self.assertIn(["shell", "maps: commands/x.md: ls a"], plugrights.runs(root, "maps"))
        got = self.rights({"cmd.markdown": "!`ls m`\n", ".git/cmds/y.md": "!`ls g`\n"},
                          {"commands": {"x": {"source": "./cmd.markdown"}, "y": {"source": "./.git/cmds/y.md"}}})
        self.assertEqual([v for _, v in got], ["maps: .git/cmds/y.md: ls g", "maps: cmd.markdown: ls m"])

    def test_an_inline_command_of_the_set_entry_names_the_set_file(self):
        from core import plugrights
        root = os.path.join(self.dir, "entry")
        write(os.path.join(root, ".claude-plugin", "plugin.json"), {"name": "maps"})
        got = plugrights.runs(root, "maps", {"commands": {"about": {"content": "!`git log -1`"}}})
        self.assertEqual(got, [["shell", "maps: .claude-plugin/marketplace.json commands.about: git log -1"]])

    def test_a_hub_extensions_skill_set_carries_it_in_its_rights(self):
        from core import plugrights
        root = os.path.join(self.dir, "ext")
        write(os.path.join(root, ".claude-plugin", "marketplace.json"),
              {"name": "geo", "plugins": [{"name": "maps", "source": "./plugins/maps"}]})
        write(os.path.join(root, "plugins", "maps", "skills", "maps", "SKILL.md"), SKILL.format(n="maps"))
        before = plugrights.of({"skills": True}, root)
        write(os.path.join(root, "plugins", "maps", "skills", "maps", "SKILL.md"), SKILL.format(n="maps") + "!`ls`\n")
        self.assertEqual(plugrights.wider(plugrights.of({"skills": True}, root), before),
                         [["shell", "maps: skills/maps/SKILL.md: ls"]])

    def test_a_text_skill_stays_text(self):
        for desc in ("sets up git hooks: pre-commit", "writes *good* git hooks", "webhooks: a guide",
                     "Hooks, agents and skills: a tour"):
            with self.subTest(desc):
                skill = f"---\nname: maps\ndescription: {desc}\n---\nGo.\n"
                self.assertEqual(self.rights({"skills/maps/SKILL.md": skill}), [])
        got = self.rights({"skills/maps/SKILL.md": "---\nname: maps\ndescription: draws the tiles\n---\n"
                                                   "Say KEY=!`cmd` to the user. Wow!`x` stays. Write ` ! ` alone.\n"
                                                   "```bash\necho not run\n```\nhooks: are words here.\n",
                           "agents/rev.md": "---\nname: rev\nhooks:\n  Stop: []\n---\n!`ls`\n",
                           "README.md": "Try !`ls` in your shell.\n",
                           "vendor/commands/x.md": "!`ls`\n"})
        self.assertEqual(got, [])


if __name__ == "__main__":
    unittest.main()
