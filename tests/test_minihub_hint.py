"""A mini-hub on Claude Code writing into its own folder through the shell (sed -i, >, tee): Claude Code refuses it by
the Edit rule of core/minihub.deny with a bare "Permission to use Bash ... has been denied", and the agents looked for a
way around (a script run from /tmp). The core's PreToolUse hook (hooks/deny_hint.py) now refuses first, with the way
that works: an executor. The ban itself is the same; Codex's shell writes are left as they were."""
import tests  # noqa: F401 — hide the machine's settings
import json
import os
import tempfile
import unittest
from unittest import mock

import platforms
from core import minihub
from hooks import deny_hint
from hosts import common
from tests import winsafe


def fwd(path):
    """A path as a command of the shell has it (Git Bash on Windows too): `/` — a `\\` there is an escape."""
    return path.replace(os.sep, "/")


class ShellWrites(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="mhhint-")
        self.addCleanup(platforms.current.rmtree, self.state)
        self.work = os.path.join(self.state, "work")
        self.elsewhere = os.path.join(self.state, "elsewhere")
        os.makedirs(os.path.join(self.work, "notes"))
        os.makedirs(self.elsewhere)
        self.w, self.e = fwd(self.work), fwd(self.elsewhere)
        self.rules = minihub.deny(self.work, keep=(os.path.join(self.state, "agents", "maps", "CLAUDE.md"), self.state))

    def why(self, command, rules=None, cwd=None, shell=True):
        return deny_hint.reason({"command": command}, "Bash", cwd or self.work,
                                self.rules if rules is None else rules, shell_writes=shell)

    def test_hint_names_the_way_that_works(self):
        self.assertIn('hub task run <name> "<brief>" --work normal', deny_hint.MINIHUB_HINT)
        self.assertIn("executor", deny_hint.MINIHUB_HINT)

    def test_shell_writes_into_own_folder_get_the_hint(self):
        n = fwd(os.path.join(self.work, "notes", "a.md"))
        for c in (f"sed -i 's/a/b/' {n}", f"echo hi > {n}", f"echo hi >> {n}", f"echo hi | tee {n}",
                  f"cd {self.w} && echo q > n.txt", "sed -i 's/a/b/' notes/a.md", "touch notes/new.md",
                  f"mv {self.e}/x {self.w}/x", f"cp {n} {n}.bak"):
            self.assertEqual(self.why(c), deny_hint.MINIHUB_HINT, c)

    def test_an_unknown_cd_is_not_the_launch_folder(self):
        # `cd "$D"` leads nowhere the hook knows: later relative paths are unknown, not the mini-hub's folder
        for c in ('cd "$D" && echo q > n.txt', 'cd "$D" && cd notes && echo q > n.txt',
                  f'cd {self.e} && cd "$D" && echo q > n.txt'):
            self.assertIsNone(self.why(c), c)

    def test_an_unknown_cd_does_not_make_a_later_absolute_cd_unknown(self):
        self.assertEqual(self.why(f'cd "$D" && cd {self.w} && echo q > n.txt'), deny_hint.MINIHUB_HINT)

    def test_a_cd_in_a_subshell_or_a_substitution_does_not_reach_the_commands_after_it(self):
        e = self.e
        for c in (f"(cd {e} && ls); echo q > n.txt", f"(cd {e}) && echo q > n.txt", f"(cd {e}; pwd) > n.txt",
                  f"echo $(cd {e}; pwd) > n.txt", f"echo `cd {e}; pwd` > n.txt", f'echo "$(cd {e}; pwd)" > n.txt',
                  f"(cd {e} && (cd /; ls) && ls); echo q > n.txt", '(cd "$D" && true); echo q > n.txt',
                  "case x in a) echo q > n.txt;; esac"):
            self.assertEqual(self.why(c), deny_hint.MINIHUB_HINT, c)
        # inside the subshell the cd holds; after a stray `)` there is nothing to close
        for c in (f"(cd {e} && echo q > n.txt)", f"(cd {e}; (echo q > n.txt))", f"cd {e}; ) echo q > n.txt"):
            self.assertIsNone(self.why(c), c)

    def test_a_line_the_scan_cannot_read_is_refused_not_passed(self):
        self.assertEqual(self.why("echo hi > a", cwd=5), deny_hint.MINIHUB_UNREAD)   # not a string: TypeError in the scan
        from hooks import repo_rules
        for failure in (IndexError, StopIteration, RecursionError, ValueError, OSError):
            with mock.patch.object(repo_rules, "bash", side_effect=failure("x")):
                self.assertEqual(self.why("echo hi > a"), deny_hint.MINIHUB_UNREAD, failure)
        self.assertIn("executor", deny_hint.MINIHUB_UNREAD)

    def test_a_folder_behind_a_link_is_closed_by_both_of_its_rules(self):
        link = os.path.join(self.state, "link")
        fl = fwd(link)
        if not winsafe.try_symlink(self.work, link):
            self.skipTest("no links")
        rules = minihub.deny(link, keep=(os.path.join(self.state, "agents", "maps", "CLAUDE.md"), self.state))
        self.assertEqual(len([r for r in rules if r.startswith("Edit(")]), 2)
        for c in (f"echo q > {fl}/n.txt", f"echo q > {self.w}/n.txt", f"cd {fl} && echo q > notes/n.txt",
                  f"cd {self.w} && echo q > notes/n.txt"):
            self.assertEqual(self.why(c, rules=rules, cwd=link), deny_hint.MINIHUB_HINT, c)
        self.assertIsNone(self.why(f"echo q > {self.e}/n.txt", rules=rules, cwd=link))

    def test_reads_and_writes_elsewhere_pass(self):
        n = fwd(os.path.join(self.work, "notes", "a.md"))
        for c in (f"cat {n}", f"ls {self.w}", f"grep -r x {self.w}", f"echo hi > {self.e}/o.md",
                  f"cd {self.e} && echo q > n.txt", f"cp {n} {self.e}/copy.md", "git status",
                  "hub task run maps-1 'edit the notes' --work normal"):
            self.assertIsNone(self.why(c), c)

    def test_git_commit_and_push_get_the_same_hint(self):
        for c in ("git commit -m x", "cd notes && git push origin main"):
            self.assertEqual(self.why(c), deny_hint.MINIHUB_HINT, c)

    def test_not_a_mini_hub_or_codex_is_as_before(self):
        n = fwd(os.path.join(self.work, "notes", "a.md"))
        self.assertIsNone(self.why(f"echo hi > {n}", rules=[]))                 # an executor: no rules
        self.assertIsNone(self.why(f"echo hi > {n}", shell=False))              # Codex: shell writes are not caught
        self.assertEqual(self.why("git commit -m x", shell=False), deny_hint.MINIHUB_HINT)

    def test_a_folder_that_holds_its_own_files_is_not_closed(self):
        rules = minihub.deny(self.state, keep=(os.path.join(self.state, "agents", "maps", "CLAUDE.md"), self.state))
        self.assertFalse([r for r in rules if r.startswith("Edit(")])
        self.assertIsNone(self.why(f"echo hi > {self.state}/x", rules=rules, cwd=self.state))

    def test_broken_input_is_silence(self):
        for rules in (["Edit(//", "Edit(//x)"], [None, 5]):
            self.assertIsNone(self.why("echo hi > /x/y", rules=rules))


class Rules(unittest.TestCase):
    def setUp(self):
        self.env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self.env)))
        os.environ.pop("HUB_ON_CODEX", None)
        os.environ.pop("CODEX_HOME", None)

    def test_claude_takes_the_rules_from_the_nested_deny_variable(self):
        deny = minihub.deny("/home/x/maps", keep=())
        os.environ["HUB_NESTED_DENY"] = json.dumps(deny)
        self.assertEqual(deny_hint.session_file()["deny"], deny)
        os.environ["HUB_NESTED_DENY"] = "not json"
        self.assertEqual(deny_hint.session_file()["deny"], [])
        os.environ.pop("HUB_NESTED_DENY")
        self.assertEqual(deny_hint.session_file()["deny"], [])


class Settings(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="mhhint-set-")
        self.addCleanup(platforms.current.rmtree, self.dir)

    def bash_entries(self, **kw):
        with open(common.write_hook_settings(os.path.join(self.dir, "s.json"), **kw), encoding="utf-8") as f:
            return [h for h in json.load(f)["hooks"]["PreToolUse"] if h["matcher"] == "Bash"]

    def test_every_session_gets_one_hook_on_every_bash_call(self):
        entries = self.bash_entries()
        self.assertEqual(len(entries), 1)
        hook = entries[0]["hooks"][0]
        self.assertNotIn("if", hook)
        self.assertIn("deny_hint.py", " ".join([hook["command"], *hook.get("args", [])]))   # Windows: command + args

    def test_host_uses_the_one_settings_file_for_a_mini_hub_too(self):
        from hosts.streamjson import StreamHost
        host = StreamHost(tempfile.mkdtemp(prefix="mhhint-host-"))
        self.addCleanup(platforms.current.rmtree, host.state_dir)
        self.assertIn(host.settings, host.command("maps", "sid", minihub=True))
        self.assertIn(host.settings, host.command("maps-1", "sid"))
        both = host.command("maps", "sid", minihub=True,
                            spec={"task": "d7", "path": "specs/d7/spec.md", "boss": "owner", "open": True})
        self.assertIn(os.path.join(host.state_dir, "hooks-settings-spec.json"), both)
        with open(os.path.join(host.state_dir, "hooks-settings-spec.json"), encoding="utf-8") as f:
            entries = json.load(f)["hooks"]["PreToolUse"]
        self.assertEqual(entries[0]["matcher"], "Edit|Write|MultiEdit|NotebookEdit")   # the spec guard stays first
        self.assertEqual(len([h for h in entries if h["matcher"] == "Bash" and "if" not in h["hooks"][0]]), 1)


if __name__ == "__main__":
    unittest.main()
