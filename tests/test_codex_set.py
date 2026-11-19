"""Codex (experimental): what a Codex session gets of the plugins and the repo it works in, as a
Claude session of the hub does (hosts/codex_set.py) — plugin MCP servers, plugin hooks, the repo's hooks from
.claude/settings*.json and the repo's skills from .claude/skills; a skill with disable-model-invocation stays out of the
model's hands (probes 2026-11-05 on codex 0.162.1 with a fake Responses API)."""
import tests  # noqa: F401 — do not see the machine's settings (~/.config/hub-core), see tests/__init__.py
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

import platforms

from hooks import edit_hooks
from hosts import codex_set

HERE = os.path.dirname(__file__)
FAKE_CODEX = os.path.join(HERE, "fakes", "fake_codex.py")
FAKE_W = os.path.join(HERE, "fakes", "codex_w.py")   # codex.exe of an ordinary Windows user


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text if isinstance(text, str) else json.dumps(text))


def skill(root, name, extra=""):
    write(os.path.join(root, name, "SKILL.md"), f"---\nname: {name}\ndescription: about {name}\n{extra}---\nbody\n")
    return os.path.join(root, name, "SKILL.md")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="hubcxset-"))   # Codex reports skills by real paths
        self.addCleanup(platforms.current.rmtree, self.tmp)
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state)

    def repo(self, name="repo"):
        d = os.path.join(self.tmp, name)
        os.makedirs(d)
        subprocess.run(["git", "-C", d, "init", "-q"], check=True, capture_output=True)
        return d

    def env(self, **extra):
        return {"HUB_STATE_DIR": self.state, "HUB_SESSION": "a", "HOME": os.path.join(self.tmp, "home"), **extra}

    def collect(self, flags=(), cwd=None, env=None, roots=None):
        roots, mcp, env = list(roots or []), {}, self.env() if env is None else env
        extra = codex_set.collect(list(flags), roots, mcp, env, cwd=cwd)
        return roots, mcp, env, extra

    def plugin(self, name, files):
        d = os.path.join(self.tmp, "plugins", name)
        write(os.path.join(d, ".claude-plugin", "plugin.json"), {"name": name})
        for rel, text in files.items():
            write(os.path.join(d, rel), text)
        return d


class PluginMcp(Base):
    def test_a_plugins_mcp_servers_reach_the_thread(self):
        # the catalog writes a plugin's .mcp.json into the session's mcp.json (core/catalog.plugin_servers), and that
        # is what the thread's mcp_servers are made of: the row "MCP of a plugin" holds through the catalog
        from core import catalog
        d = self.plugin("pl", {".mcp.json": {"mcpServers": {"srv": {"command": "${CLAUDE_PLUGIN_ROOT}/bin/srv",
                                                                       "args": ["--x"]}}}})
        flags = catalog.launch_flags(tools=["pl@set"], cfg={}, kind="work", cwd=None, project="all",
                                     state_dir=self.state, name="a",
                                     catalog={"pl@set": {"kind": "plugin", "path": d}})
        _, mcp, _, _ = self.collect(flags)
        self.assertEqual(mcp["plugin:pl:srv"]["command"], f"{d}/bin/srv")
        self.assertEqual(mcp["plugin:pl:srv"]["args"], ["--x"])


class RepoSkills(Base):
    def test_the_repos_skills_from_the_folder_up_to_the_repo_root(self):
        # Claude Code (2.1.309, getProjectDirsUpToHome) reads <dir>/.claude/skills from the session folder up to the
        # repo root, nearest first; Codex reads none of them (probe 2026-11-05: not in skills/list)
        r = self.repo()
        sub = os.path.join(r, "apps", "x")
        skill(os.path.join(sub, ".claude", "skills"), "near")
        skill(os.path.join(r, ".claude", "skills"), "far")
        above = os.path.join(self.tmp, ".claude", "skills")   # above the repo root: not the repo's
        skill(above, "outside")
        roots, _, _, _ = self.collect(cwd=sub)
        self.assertEqual(roots, [os.path.join(sub, ".claude", "skills"), os.path.join(r, ".claude", "skills")])

    def test_not_above_the_home_folder(self):
        # outside a repo the walk stops below HOME: ~/.claude/skills is the user's own scope, which a hub session does not
        # get (--setting-sources project,local)
        home = os.path.join(self.tmp, "home")
        work = os.path.join(home, "w")
        skill(os.path.join(work, ".claude", "skills"), "here")
        skill(os.path.join(home, ".claude", "skills"), "users")
        roots, _, _, _ = self.collect(cwd=work, env=self.env(HOME=home))
        self.assertEqual(roots, [os.path.join(work, ".claude", "skills")])

    def test_a_worktree_without_skills_takes_the_main_checkouts(self):
        # Claude Code: a git worktree whose own .claude/skills is missing gets the main checkout's
        r = self.repo()
        skill(os.path.join(r, ".claude", "skills"), "ship")
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@x"}
        subprocess.run(["git", "-C", r, "commit", "-q", "--allow-empty", "-m", "x"], check=True, capture_output=True,
                       env=env)
        wt = os.path.join(self.tmp, "wt")
        subprocess.run(["git", "-C", r, "worktree", "add", "-q", wt], check=True, capture_output=True, env=env)
        roots, _, _, _ = self.collect(cwd=wt)
        self.assertEqual([os.path.realpath(x) for x in roots], [os.path.realpath(os.path.join(r, ".claude", "skills"))])

    def test_a_human_only_skill_is_hidden_from_the_model_but_callable(self):
        # Codex ignores disable-model-invocation (the skill was in the model's list) and keeps its own policy:
        # agents/openai.yaml allow_implicit_invocation: false — out of the list, an explicit skill input still works.
        # A switched-off skill is not callable at all, a link to SKILL.md is lost (Codex takes the real path): the
        # session gets a copy with the policy, the repo's own file is switched off
        r = self.repo()
        sk = os.path.join(r, ".claude", "skills")
        ship = skill(sk, "ship", "disable-model-invocation: true\n")
        plain = skill(sk, "plain")
        write(os.path.join(sk, "ship", "scripts", "go.sh"), "echo go\n")
        write(os.path.join(sk, "ship", "notes.md"), "notes\n")
        roots, _, _, extra = self.collect(cwd=r)
        shadow = os.path.join(self.state, "sessions", "a", "codex-skills")
        self.assertEqual(roots, [sk, shadow])
        self.assertEqual(extra["skills"], [{"path": ship, "enabled": False}])
        self.assertNotIn(plain, [e["path"] for e in extra["skills"]])
        self.assertEqual(os.listdir(shadow), ["0"])
        copy = os.path.join("0", "ship")   # the skill's own folder name: Codex names a skill without `name:` so
        with open(os.path.join(shadow, copy, "SKILL.md"), encoding="utf-8") as f, open(ship, encoding="utf-8") as g:
            self.assertEqual(f.read(), g.read())
        self.assertFalse(os.path.islink(os.path.join(shadow, copy, "SKILL.md")))
        with open(os.path.join(shadow, copy, "agents", "openai.yaml"), encoding="utf-8") as f:
            self.assertIn("allow_implicit_invocation: false", f.read())
        with open(os.path.join(shadow, copy, "scripts", "go.sh"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "echo go\n")   # the skill's own files next to it
        with open(os.path.join(shadow, copy, "notes.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "notes\n")
        config = codex_set.thread_config({}, {}, **extra)
        self.assertIn({"path": ship, "enabled": False}, config["skills"]["config"])

    def test_the_flag_in_any_spelling_hides_and_a_false_one_does_not(self):
        r = self.repo()
        sk = os.path.join(r, ".claude", "skills")
        hidden = [skill(sk, "a", "disable-model-invocation: True\n"), skill(sk, "b", "disable_model_invocation: 'true'\n"),
                  skill(sk, "c", "disable-model-invocation: yes  # gate\n"),
                  skill(sk, "f", "disableModelInvocation: true\n")]
        skill(sk, "d", "disable-model-invocation: false\n")
        skill(sk, "e", "description2: disable-model-invocation: true\n")
        _, _, _, extra = self.collect(cwd=r)
        self.assertEqual(sorted(e["path"] for e in extra["skills"]), sorted(hidden))

    def test_a_plugins_human_only_skill_too(self):
        d = self.plugin("pl", {})
        team = skill(os.path.join(d, "skills"), "team", "disable-model-invocation: true\n")
        roots, _, _, extra = self.collect(["--plugin-dir", d])
        self.assertEqual(roots[0], os.path.join(d, "skills"))
        self.assertEqual(extra["skills"], [{"path": team, "enabled": False}])
        self.assertEqual(len(os.listdir(roots[1])), 1)

    def test_without_the_sessions_folder_a_human_only_skill_is_not_given_at_all(self):
        # no place for the copy: better no /ship than a gate skill the model may run by itself
        r = self.repo()
        ship = skill(os.path.join(r, ".claude", "skills"), "ship", "disable-model-invocation: true\n")
        roots, _, _, extra = self.collect(cwd=r, env={"HOME": os.path.join(self.tmp, "home")})
        self.assertEqual(roots, [os.path.join(r, ".claude", "skills")])
        self.assertEqual(extra["skills"], [{"path": ship, "enabled": False}])

    def test_the_nearest_skill_of_a_name_wins(self):
        r = self.repo()
        sub = os.path.join(r, "apps")
        skill(os.path.join(sub, ".claude", "skills"), "work")
        far = skill(os.path.join(r, ".claude", "skills"), "work")
        _, _, _, extra = self.collect(cwd=sub)
        self.assertEqual(extra["skills"], [{"path": far, "enabled": False}])

    def test_a_restart_rewrites_the_copies(self):
        r = self.repo()
        sk = os.path.join(r, ".claude", "skills")
        skill(sk, "ship", "disable-model-invocation: true\n")
        self.collect(cwd=r)
        platforms.current.rmtree(os.path.join(sk, "ship"))
        skill(sk, "deploy", "disable-model-invocation: true\n")
        roots, _, _, _ = self.collect(cwd=r)
        self.assertEqual((os.listdir(roots[-1]), os.listdir(os.path.join(roots[-1], "0"))), (["0"], ["deploy"]))

    def test_what_claude_does_not_read_is_off(self):
        # Claude Code reads <root>/<name>/SKILL.md only; Codex finds SKILL.md six folders deep, through links (probe
        # 2026-11-05) — a nested gate skill would be the model's
        r = self.repo()
        sk = os.path.join(r, ".claude", "skills")
        deep = [skill(os.path.join(sk, "release"), "ship", "disable-model-invocation: true\n"),
                skill(os.path.join(sk, "a", "b", "c", "d", "e"), "f")]
        write(os.path.join(sk, "SKILL.md"), "---\nname: top\ndescription: x\n---\n")
        deep.append(os.path.join(sk, "SKILL.md"))
        skill(os.path.join(sk, "a", "b", "c", "d", "e", "f"), "g")   # seven folders down: Codex does not see it
        write(os.path.join(sk, "x", "node_modules", "p", "SKILL.md"), "---\nname: p\ndescription: x\n---\n")
        plain = skill(sk, "plain")
        _, _, _, extra = self.collect(cwd=r)
        self.assertEqual(sorted(e["path"] for e in extra["skills"]), sorted(deep))
        self.assertNotIn(plain, [e["path"] for e in extra["skills"]])

    def test_the_users_own_skills_as_deep_as_codex_looks(self):
        home = os.path.join(self.tmp, "home")
        mine = skill(os.path.join(home, ".agents", "skills", "a", "b", "c", "d", "e"), "deep")
        self.assertEqual([e["path"] for e in codex_set.user_skills_off(home)], [mine])

    def test_a_path_in_any_script_is_put_in(self):
        r = os.path.join(self.tmp, "project")
        write(os.path.join(r, ".claude", "settings.json"), {"hooks": {"Stop": [{"hooks": [
            {"command": "${CLAUDE_PROJECT_DIR}/h.sh"}]}]}})
        _, _, _, extra = self.collect(cwd=r)
        self.assertEqual(extra["hooks"]["Stop"][0]["hooks"][0]["command"], f"{r}/h.sh")

    def test_a_link_loop_is_walked_once(self):
        r = self.repo()
        sk = os.path.join(r, ".claude", "skills")
        plain = skill(sk, "plain")
        from tests import winsafe
        winsafe.symlink(self, sk, os.path.join(sk, "plain", "loop"))
        self.assertEqual([p for p in codex_set.skill_files(sk) if p.startswith(os.path.join(sk, "plain"))][:1], [plain])
        self.assertEqual(codex_set.skill_files(sk), [plain])

    def test_a_gate_without_a_name_keeps_its_folder_name(self):
        r = self.repo()
        write(os.path.join(r, ".claude", "skills", "ship", "SKILL.md"),
              "---\ndescription: x\ndisable-model-invocation: true\n---\n")
        roots, _, _, _ = self.collect(cwd=r)
        self.assertTrue(os.path.isfile(os.path.join(roots[-1], "0", "ship", "SKILL.md")))

    def test_a_copy_that_fails_does_not_stop_a_session(self):
        from unittest import mock
        r = self.repo()
        ship = skill(os.path.join(r, ".claude", "skills"), "ship", "disable-model-invocation: true\n")
        with mock.patch.object(codex_set.shutil, "copyfile", side_effect=OSError("disk full")):
            roots, _, _, extra = self.collect(cwd=r)
        self.assertEqual(extra["skills"], [{"path": ship, "enabled": False}])   # off, and no copy
        self.assertEqual(roots, [os.path.join(r, ".claude", "skills")])

    def test_a_half_made_copy_is_never_a_skill_without_the_policy(self):
        from unittest import mock
        import platforms
        r = self.repo()
        sk = os.path.join(r, ".claude", "skills")
        skill(sk, "a", "disable-model-invocation: true\n")
        skill(sk, "b", "disable-model-invocation: true\n")
        import builtins
        real, wrote = builtins.open, []

        def fail_second_policy(path, *a, **kw):   # gate b's policy cannot be written
            if str(path).endswith("openai.yaml"):
                wrote.append(path)
                if len(wrote) == 2:
                    raise OSError("disk full")
            return real(path, *a, **kw)
        with mock.patch.object(codex_set, "open", fail_second_policy, create=True), \
                mock.patch.object(platforms.current, "rmtree", lambda p: True):   # and the half copy is not removed
            roots, _, _, _ = self.collect(cwd=r)
        made = codex_set.skill_files(roots[-1])
        self.assertEqual(len(made), 1)
        for p in made:
            self.assertTrue(os.path.isfile(os.path.join(os.path.dirname(p), "agents", "openai.yaml")))

    def test_not_without_project_settings(self):
        # the hub's setting_sources: without "project" a Claude session reads no repo skills
        r = self.repo()
        skill(os.path.join(r, ".claude", "skills"), "ship")
        roots, _, _, _ = self.collect(["--setting-sources", "local"], cwd=r)
        self.assertEqual(roots, [])


class Hooks(Base):
    def test_codex_computes_the_trust_hash_so(self):
        # live 0.162.1: hooks/list of hooks given as session flags, the very handlers below
        hooks = {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 /x/a b.py",
                                                               "timeout": 30},
                                                              {"type": "command", "command": "echo 2",
                                                               "statusMessage": "checking"}]},
                                {"hooks": [{"type": "command", "command": "echo nomatch"}]}]}
        key = codex_set.SESSION_FLAGS + ":pre_tool_use:"
        self.assertEqual(codex_set.hook_trust(hooks), {
            key + "0:0": "sha256:2b8b923d5752eee887e6ca5a793dbcebe86592a13ed9de57db311544f42faac1",
            key + "0:1": "sha256:e909d8f377d8fa38539433c2cb413ef66b1d55436d86ee64f22f13b5b1bf42bc",
            key + "1:0": "sha256:9739b11cd8261fd822fca754d7322bea9f6c8a6593e99b9f2aa556f858cc8611"})

    def test_the_key_starts_with_the_path_codex_reports(self):
        # live codex.exe 0.162.1 on Windows (an ordinary user, 2026-11-05): "C:\\<session-flags>\\config.toml:…" — not the
        # POSIX path (Codex trusted none of the hooks, the repo's and plugins' did not run) and not the drive of the
        # session's folder or of CODEX_HOME; the hash is the same
        hooks = {"Stop": [{"hooks": [{"type": "command", "command": "echo 1"}]}]}
        win = "C:\\<session-flags>\\config.toml"
        self.assertEqual(list(codex_set.hook_trust(hooks, win)), [win + ":stop:0:0"])
        self.assertEqual(list(codex_set.hook_trust(hooks).values()), list(codex_set.hook_trust(hooks, win).values()))
        self.assertEqual(list(codex_set.hook_trust(hooks)), [codex_set.SESSION_FLAGS + ":stop:0:0"])

    def test_a_plugins_hooks_reach_the_thread_trusted(self):
        # Claude Code runs a plugin's hooks/hooks.json; Codex runs a thread config's hooks only with their trusted_hash
        # in the same config (probe 2026-11-05: without it the hook did not run, with it it denied the command)
        d = self.plugin("pl", {"hooks/hooks.json": {"hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",
                                                          "command": "${CLAUDE_PLUGIN_ROOT}/guard.sh", "timeout": 5}]}],
            "Notification": [{"hooks": [{"type": "command", "command": "notify"}]}],
            "Stop": [{"hooks": [{"type": "prompt", "prompt": "check"}, {"type": "command", "command": "echo done",
                                                                        "async": True}]}]}}})
        _, _, _, extra = self.collect(["--plugin-dir", d])
        hooks = extra["hooks"]
        (pre,) = hooks["PreToolUse"]
        self.assertEqual(pre["matcher"], "Bash")
        (h,) = pre["hooks"]
        self.assertIn(d + "/guard.sh", h["command"])   # ${CLAUDE_PLUGIN_ROOT}/guard.sh: the root put in as it is
        self.assertEqual(h["timeout"], 5)
        self.assertNotIn("Notification", hooks)   # Codex has no such event: a thread config with it would not start
        (stop,) = hooks["Stop"]
        self.assertEqual(stop["hooks"], [{"type": "command", "command": stop["hooks"][0]["command"], "timeout": 60,
                                          "async": True}])   # Claude Code's default timeout; a prompt hook is a model call
        state = {k: v for k, v in hooks.items() if k == "state"}["state"]
        self.assertEqual(state, {k: {"trusted_hash": v} for k, v in
                                 codex_set.hook_trust({k: v for k, v in hooks.items() if k != "state"}).items()})
        config = codex_set.thread_config({}, {}, **extra)
        self.assertEqual(config["hooks"], hooks)

    def test_a_plugin_hook_gets_its_root_in_either_form(self):
        d = self.plugin("pl", {"hooks/hooks.json": {"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": "run \"$CLAUDE_PLUGIN_ROOT/a\" $CLAUDE_PLUGIN_ROOT_X ${CLAUDE_PLUGIN_ROOT}/b"}]}]}}})
        _, _, _, extra = self.collect(["--plugin-dir", d])
        self.assertEqual(extra["hooks"]["Stop"][0]["hooks"][0]["command"],
                         f"run \"{d}/a\" $CLAUDE_PLUGIN_ROOT_X {d}/b")

    def test_a_hook_file_outside_the_plugin_is_not_read(self):
        outside = os.path.join(self.tmp, "else.json")
        write(outside, {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x"}]}]}})
        d = self.plugin("pl", {})
        write(os.path.join(d, ".claude-plugin", "plugin.json"), {"name": "pl", "hooks": "../../else.json"})
        _, _, _, extra = self.collect(["--plugin-dir", d])
        self.assertNotIn("hooks", extra)

    def test_the_manifest_may_carry_hooks_inline_or_by_path(self):
        d = self.plugin("pl", {"more.json": {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "b"}]}]}}})
        write(os.path.join(d, ".claude-plugin", "plugin.json"), {"name": "pl", "hooks": [
            "./more.json", {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "c"}]}]}}]})
        _, _, _, extra = self.collect(["--plugin-dir", d])
        self.assertEqual([g["hooks"][0]["command"] for g in extra["hooks"]["Stop"]], ["b", "c"])

    def test_the_repos_hooks_from_its_settings(self):
        # a Claude session of the hub (--setting-sources project,local) runs the hooks of <folder>/.claude/settings.json
        # and of .claude/settings.local.json of the repo root; their commands find their scripts by $CLAUDE_PROJECT_DIR,
        # which Codex does not set (probe 2026-11-05: a hook inherits app-server's environment)
        r = self.repo()
        sub = os.path.join(r, "apps")
        write(os.path.join(sub, ".claude", "settings.json"), {"hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/a.sh"}]}]}})
        write(os.path.join(r, ".claude", "settings.local.json"), {"hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "local.sh"}]}]}})
        write(os.path.join(r, ".claude", "settings.json"), {"hooks": {"PreToolUse": [
            {"hooks": [{"type": "command", "command": "root-project-not-read"}]}]}})
        _, _, env, extra = self.collect(cwd=sub)
        cmds = [h["command"] for g in extra["hooks"]["PreToolUse"] for h in g["hooks"]]
        # put into the text too: on Windows Codex runs a hook in PowerShell, where ${…} is not the environment
        self.assertEqual(cmds, [f"{sub}/.claude/hooks/a.sh", "local.sh"])
        self.assertEqual(env["CLAUDE_PROJECT_DIR"], sub)

    def test_without_hooks_nothing_of_claude_in_the_environment(self):
        r = self.repo()
        _, _, env, extra = self.collect(cwd=r)
        self.assertNotIn("hooks", extra)
        self.assertFalse([k for k in env if k.startswith("CLAUDE")])

    def test_the_hubs_setting_sources(self):
        r = self.repo()
        write(os.path.join(r, ".claude", "settings.json"), {"hooks": {"Stop": [{"hooks": [{"command": "p"}]}]}})
        write(os.path.join(r, ".claude", "settings.local.json"), {"hooks": {"Stop": [{"hooks": [{"command": "l"}]}]}})
        got = {}
        for src in ("project,local", "project", "local", ""):
            _, _, _, extra = self.collect(["--setting-sources", src], cwd=r)
            got[src] = [g["hooks"][0]["command"] for g in (extra.get("hooks") or {}).get("Stop", [])]
        self.assertEqual(got, {"project,local": ["p", "l"], "project": ["p"], "local": ["l"], "": []})

    def test_not_the_users_own_settings_when_the_folder_is_home(self):
        # a session without a folder of its own runs in HOME: ~/.claude/settings.json is the user's, not a repo's
        home = os.path.join(self.tmp, "home")
        write(os.path.join(home, ".claude", "settings.json"), {"hooks": {"Stop": [{"hooks": [{"command": "mine"}]}]}})
        write(os.path.join(home, ".claude", "settings.local.json"), {"hooks": {"Stop": [{"hooks": [{"command": "x"}]}]}})
        _, _, _, extra = self.collect(cwd=home, env=self.env(HOME=home))
        self.assertNotIn("hooks", extra)

    def test_a_path_a_shell_would_read_again_is_not_put_in(self):
        r = os.path.join(self.tmp, "a$(id)`x`")
        write(os.path.join(r, ".claude", "settings.json"), {"hooks": {"Stop": [{"hooks": [
            {"command": "\"$CLAUDE_PROJECT_DIR/h.sh\""}]}]}})
        _, _, env, extra = self.collect(cwd=r)
        self.assertEqual(extra["hooks"]["Stop"][0]["hooks"][0]["command"], "\"$CLAUDE_PROJECT_DIR/h.sh\"")
        self.assertEqual(env["CLAUDE_PROJECT_DIR"], r)   # the shell takes it from the environment, as with Claude

    def test_hooks_switched_off_by_the_repo(self):
        r = self.repo()
        d = self.plugin("pl", {"hooks/hooks.json": {"hooks": {"Stop": [{"hooks": [{"type": "command",
                                                                                    "command": "x"}]}]}}})
        write(os.path.join(r, ".claude", "settings.json"), {"disableAllHooks": True, "hooks": {"Stop": [
            {"hooks": [{"type": "command", "command": "y"}]}]}})
        _, _, _, extra = self.collect(["--plugin-dir", d], cwd=r)
        self.assertNotIn("hooks", extra)

    def test_an_unreadable_or_odd_settings_file_does_not_stop_a_session(self):
        r = self.repo()
        write(os.path.join(r, ".claude", "settings.json"), "{not json")
        write(os.path.join(r, ".claude", "settings.local.json"), {"hooks": {
            "PreToolUse": [{"matcher": 3, "hooks": [{"type": "command", "command": "ok", "timeout": "x"}, "junk",
                                                    {"type": "command"}]}, "junk"], "Stop": "junk"}})
        _, _, _, extra = self.collect(cwd=r)
        (group,) = extra["hooks"]["PreToolUse"]
        self.assertEqual(list(group), ["hooks"])   # no matcher: every tool, so hooks/edit_hooks.py stands in front
        (h,) = group["hooks"]
        matcher, command, timeout = h["command"].split()[-3:]
        self.assertEqual((edit_hooks.unb64(matcher), edit_hooks.unb64(command), timeout), ("[null]", "ok", "60"))


class ThroughTheHost(Base):
    """A real CodexHost with the fake app-server: what reaches skills/extraRoots/set and thread/start."""

    def start(self, cwd, env=None, codex=None):
        from core import spool
        from core.daemon import Hub
        from hosts.codex import CodexHost
        calls = os.path.join(self.state, "calls.jsonl")
        host = CodexHost(self.state, codex=(sys.executable, codex or FAKE_CODEX), all_sessions=True, default_model="gpt-test",
                         env={"FAKE_TURN_S": "0.2", "FAKE_CODEX_CALLS": calls,
                              "FAKE_ARGV_DIR": os.path.join(self.state, "argv"), **(env or {})})
        hub = Hub(self.state, host, registry_dir=os.path.join(self.state, "registry"))
        self.addCleanup(host.kill_server)
        hub.start()
        spool.put(os.path.join(self.state, "spool"), "session.requested", "a", {"cwd": cwd, "session_id": "sid-a"})
        end = time.time() + 10
        while hub.state["sessions"].get("a", {}).get("state") != "idle" and time.time() < end:
            hub.tick()
            time.sleep(0.03)
        self.assertEqual(hub.state["sessions"]["a"]["state"], "idle")
        with open(calls, encoding="utf-8") as f:
            got = [json.loads(line) for line in f]
        argv = []
        for n in os.listdir(os.path.join(self.state, "argv")):
            with open(os.path.join(self.state, "argv", n), encoding="utf-8") as f:
                argv.append(json.load(f))
        (spawned,) = [a for a in argv if a["env"].get("HUB_SESSION") == "a"]
        return got, spawned

    def test_a_session_in_a_repo_gets_its_skills_and_hooks(self):
        r = self.repo()
        ship = skill(os.path.join(r, ".claude", "skills"), "ship", "disable-model-invocation: true\n")
        write(os.path.join(r, ".claude", "settings.json"), {"hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR}/guard.sh"}]}]}})
        got, spawned = self.start(r)
        (roots,) = [c["params"]["extraRoots"] for c in got if c["method"] == "skills/extraRoots/set"]
        self.assertIn(os.path.join(r, ".claude", "skills"), roots)
        self.assertIn(os.path.join(self.state, "sessions", "a", "codex-skills"), roots)
        (start,) = [c["params"] for c in got if c["method"] == "thread/start"]
        self.assertIn({"path": ship, "enabled": False}, start["config"]["skills"]["config"])
        self.assertEqual(start["config"]["hooks"]["PreToolUse"][0]["matcher"], "Bash")
        self.assertIn(codex_set.SESSION_FLAGS + ":pre_tool_use:0:0", start["config"]["hooks"]["state"])
        self.assertEqual(spawned["env"]["CLAUDE_PROJECT_DIR"], r)

    def test_on_windows_the_trust_keys_are_the_ones_codex_names(self):
        # the hub's trust check asks its Codex where a thread config's hooks come from (codex_rules.flags_path):
        # tests/fakes/codex_w.py names them as codex.exe does, "C:\\<session-flags>\\config.toml"
        r = self.repo()
        write(os.path.join(r, ".claude", "settings.json"), {"hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "guard"}]}]}})
        got, _ = self.start(r, codex=FAKE_W, env={"FAKE_PLATFORM": "windows"})
        (start,) = [c["params"] for c in got if c["method"] == "thread/start"]
        self.assertEqual(list(start["config"]["hooks"]["state"]), ["C:\\<session-flags>\\config.toml:pre_tool_use:0:0"])

    def test_a_session_outside_a_repo_starts_as_before(self):
        home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(home, "w"))
        got, _ = self.start(os.path.join(home, "w"), env={"HOME": home})
        (start,) = [c["params"] for c in got if c["method"] == "thread/start"]
        self.assertNotIn("hooks", start["config"])
        (roots,) = [c["params"]["extraRoots"] for c in got if c["method"] == "skills/extraRoots/set"]
        self.assertEqual(len(roots), 1)   # hub-base's skills only


if __name__ == "__main__":
    unittest.main()
