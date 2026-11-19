"""A session's set (spec d2547143): which skills and MCP servers its process loaded. The owner was sure the sessions
used all their skills and they did not; the page showed only what the core granted. The host now says the set itself
(core/kit.py): Claude Code's init and its get_context_usage, Codex's skills/list and mcpServerStatus/list. The shapes
below are what Claude Code 2.1.309 printed for a session with a plugin, repo skills, a disable-model-invocation skill
and a missing plugin folder (probe 17.11), and the schema of codex-cli 0.167.0."""
import tests  # noqa: F401 — do not see the machine's settings
import os
import tempfile
import types
import unittest
from unittest import mock

from core import admit, kit, reducer
from hosts import codex_cmds, codex_mcp, streamjson

OWNER = {"channel": "web", "owner": True}


def skill(root, rel, name, hidden=False):
    path = os.path.join(root, *rel.split("/"), "SKILL.md")   # the separators of the system, as the code joins them
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"---\nname: {name}\ndescription: about {name}\n"
                + ("disable-model-invocation: true\n" if hidden else "") + "---\nbody\n")
    return path


class Fixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="kit-")
        self.repo = os.path.join(self.dir, "maps")
        os.makedirs(os.path.join(self.repo, ".git"))
        self.notes = skill(self.repo, ".claude/skills/release-notes", "release-notes")
        self.hidden = skill(self.repo, ".claude/skills/hidden-cmd", "hidden-cmd", hidden=True)
        self.plugin = os.path.join(self.dir, "plug", "geo")
        self.tiles = skill(self.plugin, "skills/tiles", "tiles")
        self.styles = skill(self.plugin, "skills/styles", "styles", hidden=True)

    def init(self, **over):
        return {"type": "system", "subtype": "init", "cwd": self.repo, "session_id": "s-maps",
                "skills": ["hidden-cmd", "release-notes", "deep-research", "geo:styles", "geo:tiles", "code-review"],
                "plugins": [{"name": "geo", "path": self.plugin, "source": "geo@inline"},
                            {"name": "cc-plugin-telemetry", "path": "builtin", "source": "cc-plugin-telemetry@builtin"}],
                "plugin_errors": [{"plugin": "inline[1]", "type": "path-not-found",
                                   "message": "Path not found: /home/x/plug/missing", "path": "/home/x/plug/missing"}],
                "mcp_servers": [{"name": "atlas", "status": "connected"}, {"name": "my.search", "status": "needs-auth"}],
                "tools": ["Bash", "Read", "Skill", "mcp__atlas__render", "mcp__atlas__skill_reference",
                          "mcp__my_search__find"], **over}

    def usage(self):
        return {"skills": {"totalSkills": 4, "includedSkills": 4, "tokens": 297, "skillFrontmatter": [
            {"name": "release-notes", "source": "projectSettings", "path": self.notes, "tokens": 30},
            {"name": "geo:tiles", "source": "plugin", "pluginName": "geo", "pluginId": "geo@inline", "path": self.tiles,
             "tokens": 23},
            {"name": "code-review", "source": "built-in", "tokens": 214},
            {"name": "security-review", "source": "built-in", "tokens": 30}]}}


class Build(Fixture):
    def test_init_alone_names_every_skill_with_where_it_comes_from(self):
        got = kit.build(kit.from_init(self.init()), None, self.repo)
        rows = {r["name"]: r for r in got["skills"]}
        self.assertEqual(rows["geo:tiles"], {"name": "geo:tiles", "source": "plugin", "plugin": "geo", "path": self.tiles})
        self.assertEqual(rows["geo:styles"]["path"], self.styles)
        self.assertEqual(rows["hidden-cmd"], {"name": "hidden-cmd", "source": "repo", "path": self.hidden})
        self.assertEqual(rows["deep-research"], {"name": "deep-research", "source": "built-in"})
        self.assertNotIn("in_prompt", rows["release-notes"])          # the model's list is not known yet
        self.assertEqual(got["mcp"], [{"name": "atlas", "tools": 2}, {"name": "my.search", "tools": 1}])
        (err,) = got["plugin_errors"]
        self.assertEqual((err["plugin"], err["type"]), ("inline[1]", "path-not-found"))

    def test_the_models_list_marks_what_it_sees_and_adds_what_init_does_not_name(self):
        got = kit.build(kit.from_init(self.init()), kit.from_usage(self.usage()), self.repo)
        rows = {r["name"]: r for r in got["skills"]}
        # disable-model-invocation: in init, not in the model's list (Claude Code 2.1.309, probe 17.11)
        self.assertEqual((rows["hidden-cmd"]["in_prompt"], rows["geo:styles"]["in_prompt"]), (False, False))
        self.assertEqual(rows["geo:tiles"], {"name": "geo:tiles", "source": "plugin", "plugin": "geo",
                                             "path": self.tiles, "tokens": 23, "in_prompt": True})
        self.assertEqual(rows["security-review"], {"name": "security-review", "source": "built-in", "tokens": 30,
                                                   "in_prompt": True})
        self.assertEqual(got["listed"], {"in_prompt": 4, "total": 4, "tokens": 297})   # what the list takes of the context

    def test_a_claude_code_that_does_not_count_tokens_leaves_them_out(self):
        usage = self.usage()
        del usage["skills"]["tokens"]
        for row in usage["skills"]["skillFrontmatter"]:
            del row["tokens"]
        usage["skills"]["skillFrontmatter"][0]["tokens"] = "many"      # not a number: not kept
        got = kit.build(kit.from_init(self.init()), kit.from_usage(usage), self.repo)
        self.assertEqual(got["listed"], {"in_prompt": 4, "total": 4, "tokens": None})   # "not said", not 0
        self.assertTrue(all("tokens" not in r for r in got["skills"]))

    def test_codex_says_no_tokens(self):
        self.assertNotIn("listed", kit.from_codex([{"name": "geo-tiles", "path": "/a/SKILL.md", "scope": "user"}], {}))

    def test_an_executors_copy_names_the_parent_repos_skill_once(self):
        part = kit.from_init(self.init(skills=["release-notes", ".claude/worktrees/maps-3:release-notes"]))
        self.assertEqual(part["skills"], ["release-notes"])

    def test_codex_skills_and_server_tool_counts(self):
        got = kit.from_codex([{"name": "geo-tiles", "path": "/home/x/.agents/skills/geo-tiles/SKILL.md", "scope": "user"},
                              {"name": "tiles", "path": "/home/x/p/skills/tiles/SKILL.md", "scope": "user",
                               "pluginId": "geo@team"},
                              {"name": "notes", "path": "/home/x/maps/.agents/skills/notes/SKILL.md", "scope": "repo"}],
                             {"atlas": 9})
        rows = {r["name"]: r for r in got["skills"]}
        self.assertEqual((rows["geo-tiles"]["source"], rows["tiles"]["plugin"], rows["notes"]["source"]),
                         ("user", "geo", "repo"))
        self.assertEqual(got["mcp"], [{"name": "atlas", "tools": 9}])


class ClaudeHost(Fixture):
    def setUp(self):
        super().setUp()
        self.host = streamjson.StreamHost(self.dir, claude=["claude"])
        self.proc = streamjson.StreamProcess(mock.MagicMock())
        self.proc.popen.pid = 4242
        self.proc.popen.poll.return_value = None
        self.written = []
        self.proc.write = self.written.append
        self.host.procs["maps"] = self.proc

    def kits(self, msg):
        return [f[2] for f in self.host._line("maps", msg) if f[0] == "session.kit"]

    def result(self):
        return self.kits({"type": "result", "subtype": "success", "num_turns": 2, "result": "done"})

    def asked(self):
        return [w for w in self.written if (w.get("request") or {}).get("subtype") == "get_context_usage"]

    def test_the_set_once_per_change_and_the_models_list_after_the_turn(self):
        (first,) = self.kits(self.init())
        self.assertEqual(first["engine"], "claude")
        self.assertEqual(self.kits(self.init()), [])                     # the next turn's same init: nothing
        self.assertEqual(self.asked(), [])                               # not mid-turn
        self.result()
        (ask,) = self.asked()
        self.assertTrue(ask["request_id"].startswith("kit-4242-"))
        (second,) = self.kits({"type": "control_response", "response": {
            "subtype": "success", "request_id": ask["request_id"], "response": self.usage()}})
        self.assertFalse({r["name"]: r for r in second["skills"]}["hidden-cmd"]["in_prompt"])
        self.assertEqual(second["listed"]["tokens"], 297)                # the fact carries what the list takes
        self.result()
        self.assertEqual(len(self.asked()), 1)                           # asked once while init stays the same
        self.kits(self.init(skills=["geo:tiles"]))                       # a plugin went away
        self.result()
        self.assertEqual(len(self.asked()), 2)

    def test_an_older_claude_code_without_the_request_keeps_the_init_set(self):
        self.kits(self.init())
        self.result()
        (ask,) = self.asked()
        self.assertEqual(self.kits({"type": "control_response", "response": {
            "subtype": "error", "request_id": ask["request_id"],
            "error": "Unsupported control request subtype: get_context_usage"}}), [])

    def test_a_late_answer_of_the_previous_process_is_not_about_this_one(self):
        self.kits(self.init())
        self.assertEqual(self.kits({"type": "control_response", "response": {
            "subtype": "success", "request_id": "kit-1111-5", "response": self.usage()}}), [])

    def test_an_init_without_skills_says_nothing(self):
        self.assertEqual(self.kits({"type": "system", "subtype": "init", "session_id": "s-maps"}), [])
        self.result()
        self.assertEqual(self.asked(), [])


class Codex(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.proc = types.SimpleNamespace(start_params={"cwd": "/home/x/maps"}, thread="t1", call=self.call)

    def call(self, method, params):
        self.calls.append((method, params))
        return len(self.calls)

    def listed(self, rows):
        codex_cmds._list_skills(self.proc)
        return codex_cmds.response(self.proc, "maps", "skills/list", {}, {"id": len(self.calls), "result": {"data": [
            {"cwd": "/home/x/maps", "errors": [], "skills": rows}]}})

    def test_skills_list_gives_the_set_and_a_change_lists_them_again(self):
        row = {"name": "geo-tiles", "path": "/home/x/.agents/skills/geo-tiles/SKILL.md", "scope": "user",
               "enabled": True, "description": ""}
        (fact,) = self.listed([row, {**row, "name": "off", "enabled": False}])
        self.assertEqual((fact[0], fact[2]["engine"], [r["name"] for r in fact[2]["skills"]]),
                         ("session.kit", "codex", ["geo-tiles"]))
        self.assertEqual(self.listed([row, {**row, "name": "off", "enabled": False}]), [])   # the same set
        self.assertEqual(codex_cmds.notice(self.proc, "maps", "skills/changed", {}), [])
        self.assertEqual(self.calls[-1][0], "skills/list")

    def test_the_mcp_status_adds_tool_counts(self):
        self.listed([])
        st = codex_mcp._of(self.proc)
        facts = codex_mcp._status_answer(self.proc, "maps", ("status", None, None, []), {"result": {"data": [
            {"name": "atlas", "runtimeStatus": "ready", "tools": {"render": {}, "skill_reference": {}}}]}})
        (kit_fact,) = [f for f in facts if f[0] == "session.kit"]
        self.assertEqual(kit_fact[2]["mcp"], [{"name": "atlas", "tools": 2}])
        self.assertIsNotNone(st)


class Fold(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "lead", cwd="/w", session_id="s-lead", **OWNER)
        self.ev("session.started", "lead")
        self.ev("session.requested", "maps", cwd="/w", session_id="s-maps", executor=True, raised_by="lead", **OWNER)
        self.ev("session.started", "maps")

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, _, self.why = reducer.reduce(self.st, {"seq": self.seq, "ts": 100.0 + self.seq, "type": type,
                                                        "entity": entity, "data": data, "src": "host"})

    def test_the_set_is_kept_until_the_session_is_removed(self):
        self.ev("session.kit", "maps", engine="claude", skills=[{"name": "geo:tiles"}], plugin_errors=[], mcp=[])
        self.assertEqual(self.st["sessions"]["maps"]["kit"]["skills"], [{"name": "geo:tiles"}])
        self.ev("session.info", "maps", model="claude-opus-5-5")      # info is replaced whole; the set stays
        self.assertIn("kit", self.st["sessions"]["maps"])
        self.ev("session.remove_requested", "maps", **OWNER)            # a free executor is removed at once
        self.assertEqual(self.st["sessions"]["maps"]["state"], "removed")
        self.assertNotIn("kit", self.st["sessions"]["maps"])

    def test_only_the_host_says_it(self):
        self.assertIn("session.kit", admit.CORE_ONLY)


if __name__ == "__main__":
    unittest.main()
