"""Codex (experimental): a repo's Claude Code permission rules (.claude/settings.json and
settings.local.json, `permissions.deny|ask|allow`) on a hub's Codex session. With Claude, Claude Code holds them
itself; on Codex the core's hook (hooks/deny_hint.py, hooks/repo_rules.py) holds them. The expected verdicts are the
real claude 2.1.309's (probe 2026-11-05: --permission-mode bypassPermissions --setting-sources project,local on a fake
Anthropic API with a fake shell, nothing ran); where ours is stricter, the case says so."""
import tests  # noqa: F401 — do not see the machine's settings (~/.config/hub-core), see tests/__init__.py
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import platforms
from tests import winsafe

from core.minihub import _rule_path
from hooks import deny_hint, repo_rules
from hosts import codex_rules

# the disk's root in a rule's form: "" on POSIX, "/c" on Windows (Claude Code's //c/…): /etc/hosts there is C:\etc\hosts
DISK = _rule_path(os.path.abspath(os.sep))


def disk_of(root):
    """The disk of the project's folder in a rule's form: a path "/etc/hosts" in a command is resolved on the disk of
    the folder the command runs in (the project's), not of the folder the test process was started in."""
    return _rule_path(os.path.splitdrive(os.path.abspath(root))[0] + os.sep)

DENY = ["Bash(git push:*)", "Bash(git push * main)", "Bash(sudo *)", "Bash(kubectl:*)", "Bash(git clean -f*)",
        "Bash(docker rm *)", "Bash(rm -rf /)", "Bash(rake:*)", "Bash(npm publish*)", "Bash(ls *)",
        "Bash(git reset --hard *)", "Read(.env)", "Read(**/*.key)", "Read(**/secrets*)", "Read(./repos/**/.env)",
        "Read(.env.production*)", "Read(**/credentials/**)", "Read(/@DISK@/etc/host*)", "Read(~/.ssh/**)",
        "Read(/api/pyproject.toml)", "Edit(**/Dockerfile)", "Edit(pyproject.toml)"]
ASK = ["Bash(docker compose up:*)", "Bash(docker compose down *-v*)", "Edit(**/.env*)"]
ALLOW = ["Bash(git status:*)", "Bash(npm test)", "Edit(src/**)"]

# (command, claude's verdict): deny | ask | run
BASH = [
    # prefix rules: whole words, white space collapsed, every simple command of the line
    ("git push", "deny"), ("git  push origin x", "deny"), ("git pushx", "run"), ("echo git push", "run"),
    ("# git push", "run"), ("true | git push", "deny"), ("true || git push", "deny"), ("true; git push", "deny"),
    ("true & git push", "deny"), ("git status\ngit push", "deny"), ("(git push)", "deny"), ("{ git push; }", "deny"),
    ("if true; then git push; fi", "deny"), ("for i in 1; do git push; done", "deny"),
    ("echo $(git push)", "deny"), ("echo `git push`", "deny"), ("x=$(git push)", "deny"),
    ("git push 2>&1", "deny"), ("git push > /tmp/x", "deny"),
    # env assignments, wrappers and the program's path come off
    ("FOO=1 git push", "deny"), ("timeout 5 git push", "deny"), ("nice git push", "deny"), ("nohup git push", "deny"),
    ("env git push", "deny"), ("time git push", "deny"), ("watch git push", "deny"), ("command git push", "deny"),
    ("exec git push", "deny"), ("xargs git push", "deny"), ("/usr/bin/git push", "deny"),
    ("GIT_X=1 timeout 3 git push", "deny"),
    # not looked into, as with Claude
    ("bash -c 'git push'", "run"), ("sh -c \"git push\"", "run"), ("eval git push", "run"), ("git -C . push", "run"),
    ("git -c x=y push", "run"),
    # patterns: `*` over anything, anchored; a trailing ` *` may match nothing
    ("sudo", "deny"), ("sudo ls", "deny"), ("printf x | sudo tee y", "deny"), ("kubectl get pods", "deny"),
    ("kubectl-foo", "run"), ("git clean -f", "deny"), ("git clean -fdx", "deny"), ("git clean -n", "run"),
    ("docker rm x", "deny"), ("docker rm", "deny"), ("docker rmi x", "run"), ("rake db:migrate", "deny"),
    ("bundle exec rake x", "run"), ("npm publish", "deny"), ("npm publishx", "deny"), ("npm  publish", "deny"),
    ("ls", "deny"), ("ls -la", "deny"), ("lsof", "run"), ("git reset --hard", "deny"), ("git reset --hardx", "run"),
    ("rm -rf /", "deny"), ("rm -rf / ", "deny"), ("rm -rf /tmp/x", "run"),
    # ask rules
    ("docker compose up -d", "ask"), ("docker compose down -v", "ask"), ("docker compose down --volumes", "ask"),
    ("docker compose down", "run"),
    # Read rules hold reading commands, Edit rules writing ones
    ("cat .env", "deny"), ("cat ./.env", "deny"), ("cat sub/.env", "deny"), ("head -1 .env", "deny"),
    ("grep x .env", "deny"), ("sed -n 1p .env", "deny"), ("tail .env", "deny"), ("awk 1 .env", "deny"),
    ("wc -l .env", "deny"), ("rg x .env", "deny"), ("ls .env", "deny"), ("cp .env /tmp/y", "deny"),
    ("cat < .env", "deny"), ("diff .env x.pem", "deny"), ("cat .env | head", "deny"), ("cat x.pem .env", "deny"),
    ("cat -- .env", "deny"), ("cd sub && cat .env", "deny"), ("cat a.key", "deny"), ("cat x/b.key", "deny"),
    ("cat .env.local", "run"), ("cat .env.production.x", "deny"), ("cat secrets.txt", "deny"),
    ("cat api/secrets.yml", "deny"), ("cat repos/a/.env", "deny"), ("cat repos/a/.npmrc", "run"),
    ("cat c/credentials/k", "deny"), ("cat credentials", "deny"), ("cat /etc/hosts", "deny"),
    ("cat ~/.ssh/config", "deny"), ("cat api/pyproject.toml", "deny"), ("cat pyproject.toml", "run"),
    ("source .env", "run"), ("cat .e*", "run"), ("cat $HOME/.env", "run"),
    ("cat repos/*/.env", "run"), ("find . -name .env", "run"), ("grep -r x .", "run"),
    ("find src -newer .env.production.x", "deny"), ("find .env.production.x -type f", "deny"),
    ("grep -n x api/secrets/*.py", "deny"), ("cat src/*.py", "run"),
    ("git diff main -- docs .env.production.x", "deny"), ("git grep -n x -- api/secrets", "deny"),
    ("git diff main .env.production.x", "deny"), ("git grep -n x api/secrets", "deny"),
    ("git log -p -- .env.production.x", "run"), ("git show HEAD -- .env.production.x", "run"),
    ("echo a > Dockerfile", "deny"), ("echo a >> api/Dockerfile", "deny"), ("echo a>Dockerfile", "deny"),
    ("sed -i s/a/b/ Dockerfile", "deny"), ("tee Dockerfile", "deny"), ("touch Dockerfile", "deny"),
    ("rm Dockerfile", "deny"), ("mv x.pem Dockerfile", "deny"), ("cp x.pem api/Dockerfile", "deny"),
    ("echo a > api/pyproject.toml", "deny"), ("printf a | tee -a pyproject.toml", "deny"), ("cat Dockerfile", "run"),
    ("chmod +x Dockerfile", "run"), ("git checkout Dockerfile", "run"),
    # a path's ask rule holds the file tools, not the shell (2.1.309 in bypassPermissions, default and acceptEdits)
    ("echo x > .env.local", "run"), ("sed -i s/a/b/ .env.dev", "run"), ("git status 2>/dev/null", "run"),
]
# stricter than Claude Code (the safe side): a pattern also tried without quotes, a line continuation joined, a path
# also after a `cd` in the line
STRICTER = [("git push origin 'main'", "deny"), ("git push\\\n origin", "deny"),
            ("cd api && cat pyproject.toml", "deny"),
            ("less .env", "deny")]   # Claude Code 2.1.309 lets a viewer read it; the release review closed that here
# the shell's own spellings of the same command (review 2026-11-05): an assignment with a substitution, $'…', $"…"
SPELLINGS = [('X="$(date)" git push', "deny"), ("X=`true` git push", "deny"), ("x=1 y=$(echo) git push", "deny"),
             ("$'git' push", "deny"), ('$"git" push', "deny"), ("$'\\x67it' push", "deny"),
             ("cat > notes <<'END-X'\nthen git push\nEND-X\necho ok", "run"),
             ('cat <<"a.b"\ngit push\na.b', "run"), ("cat <<END-X\ngit push\nEND-X\ngit push", "deny"),
             ("cat <<\\END\nx\nEND\ngit push", "deny"), ('cat <<E"N"D\nx\nEND\ngit push', "deny"),
             ("$'\\u0067it' push", "deny"), ("sed -Ei s/a/b/ Dockerfile", "deny"),
             ("sed --in-place=.bak s/a/b/ Dockerfile", "deny"),
             # an unquoted heredoc body runs its substitutions; a brace expansion is the command it unfolds to; a file
             # a reader takes its script from is read too (review 2026-11-05)
             ("cat <<EOF\n$(git push)\nEOF", "deny"), ("cat <<EOF\nx `git push` y\nEOF", "deny"),
             ("cat <<'EOF'\n$(git push)\nEOF", "run"), ("cat <<EOF\n\\$(git push)\nEOF", "run"),
             ("{git,push}", "deny"), ("{git,status}", "run"), ("{git,} push", "deny"), ("gi{t,} push", "run"), ("{git,{push,x}}", "deny"),
             ("cat {~/.ssh/id_rsa,README}", "deny"), ("sed -nf .env notes.txt", "deny"), ("grep -f ~/.ssh/id_rsa notes.txt", "deny"),
             ("grep --file=/home/x/.ssh/k notes.txt", "deny"), ("sed -f .env notes.txt", "deny"),
             ("awk -f a.key notes.txt", "deny"), ("grep -e x notes.txt", "run")]

# the release review's spellings (2026-11-09): a pattern given attached, a line continuation inside double quotes,
# `function` and `coproc` bodies, cp -t, the common viewers
REVIEW = [("grep --regexp=x .env", "deny"), ("grep -ex .env", "deny"), ("grep -rex .env", "deny"),
          ("sed --expression=p .env", "deny"), ("sed -ep .env", "deny"), ("grep -e x notes.txt", "run"),
          ('"gi\\\nt" push', "deny"), ('cat ".e\\\nnv"', "deny"),
          ("function f { git push; }; f", "deny"), ("function f() { git push; }", "deny"), ("coproc git push", "deny"),
          ("coproc name { git push; }", "deny"), ("cp -t /tmp .env", "deny"), ("cp -rt /tmp .env", "deny"),
          ("cp -t/tmp .env", "deny"), ("cp --target-directory=/tmp .env", "deny"),
          ("less .env", "deny"), ("more .env", "deny"), ("less notes.txt", "run")]


def project(rules=None, local=None):
    root = tempfile.mkdtemp(prefix="hubcxn-")
    os.makedirs(os.path.join(root, ".claude"))
    deny = [r.replace("@DISK@", disk_of(root)) for r in DENY]
    with open(os.path.join(root, ".claude", "settings.json"), "w", encoding="utf-8") as f:
        json.dump({"permissions": rules or {"deny": deny, "ask": ASK, "allow": ALLOW}}, f)
    if local is not None:
        with open(os.path.join(root, ".claude", "settings.local.json"), "w", encoding="utf-8") as f:
            json.dump({"permissions": local}, f)
    return root


class Matching(unittest.TestCase):
    def setUp(self):
        self.root = project()
        self.rules = repo_rules.load(repo_rules.files(self.root))
        # on the project's disk, as a real home is (expanduser answers with a disk): without it "/home/x" is resolved
        # on the disk of the folder a command runs in, while a rule's `~/…` base on the disk of the test process, and
        # they differ when TEMP is on another disk than the process's folder; "" on POSIX
        self.home = os.path.splitdrive(os.path.abspath(self.root))[0] + "/home/x"

    def tearDown(self):
        platforms.current.rmtree(self.root)

    def verdict(self, command):
        hit = repo_rules.bash(command, self.rules, self.root, home=self.home)
        return "run" if not hit or hit[0] == "allow" else hit[0]

    def test_shell_commands_as_claude_code_decides(self):
        wrong = [(c, want, self.verdict(c)) for c, want in BASH + STRICTER + SPELLINGS + REVIEW if self.verdict(c) != want]
        self.assertEqual(wrong, [])

    def test_allow_needs_every_simple_command(self):
        self.assertEqual(repo_rules.bash("git status -s", self.rules, self.root)[0], "allow")
        self.assertEqual(repo_rules.bash("git status && npm test", self.rules, self.root)[0], "allow")
        self.assertIsNone(repo_rules.bash("git status && make", self.rules, self.root))
        self.assertEqual(repo_rules.bash("git status && git push", self.rules, self.root)[0], "deny")
        # what the line writes, or reads outside the project, Claude Code asks about: no allow
        self.assertIsNone(repo_rules.bash("git status > /etc/profile", self.rules, self.root))
        self.assertIsNone(repo_rules.bash("git status > notes.txt", self.rules, self.root))
        self.assertIsNone(repo_rules.bash("npm test < /etc/passwd", self.rules, self.root))
        self.assertEqual(repo_rules.bash("npm test < README.md", self.rules, self.root)[0], "allow")
        # a path or a folder the scan cannot know: no allow either
        for line in ('git status > "$HOME/.bashrc"', "npm test < $HOME/.ssh/id_rsa", 'git status | tee "$F"',
                     'cd "$HOME" && git status', "pushd /etc && git status", "cd -P /etc && git status"):
            self.assertIsNone(repo_rules.bash(line, self.rules, self.root), line)
        self.assertEqual(repo_rules.bash("cd src && git status", self.rules, self.root)[0], "allow")

    def test_paths_by_their_real_and_windows_forms(self):
        with open(os.path.join(self.root, ".env"), "w") as f:
            f.write("x")
        if winsafe.try_symlink(os.path.join(self.root, ".env"), os.path.join(self.root, "notes")):
            self.assertEqual(self.verdict("cat notes"), "deny")
        self.assertTrue(repo_rules.path_match("//c/Users/u/.env", "C:\\Users\\u\\.env", "C:\\w"))
        self.assertTrue(repo_rules.path_match(".env", "C:\\w\\sub\\.env", "C:\\w"))
        self.assertFalse(repo_rules.path_match("./.env", "D:\\w\\.env", "C:\\w"))

    def test_file_tools_and_codex_calls(self):
        r = self.root
        call = lambda tool, inp: repo_rules.call(tool, inp, self.rules, r, home=self.home)   # noqa: E731
        patch = "*** Begin Patch\n*** {} File: {}\n+x\n*** End Patch\n"
        self.assertEqual(call("apply_patch", {"command": patch.format("Update", "web/Dockerfile")})[0], "deny")
        self.assertEqual(call("apply_patch", {"command": patch.format("Add", r + "/pyproject.toml")})[0], "deny")
        self.assertEqual(call("apply_patch", {"command": patch.format("Add", ".env.dev")})[0], "ask")
        self.assertEqual(call("apply_patch", {"command": patch.format("Update", "src/a.py")})[0], "allow")
        self.assertIsNone(call("apply_patch", {"command": patch.format("Update", "README.md")}))
        moved = "*** Begin Patch\n*** Update File: a.txt\n*** Move to: Dockerfile\n*** End Patch\n"
        self.assertEqual(call("apply_patch", {"command": moved})[0], "deny")
        self.assertEqual(call("view_image", {"path": "keys/a.key"})[0], "deny")
        self.assertIsNone(call("view_image", {"path": "shot.png"}))
        self.assertIsNone(call("get_goal", {}))

    def test_tool_rules_by_name(self):
        rules = repo_rules.load(repo_rules.files(self.root))
        rules["deny"].append(("mcp__acme", None, "mcp__acme", "x"))
        rules["deny"].append(("mcp__other__*", None, "mcp__other__*", "x"))
        self.assertEqual(repo_rules.call("mcp__acme__delete", {}, rules, self.root)[0], "deny")
        self.assertEqual(repo_rules.call("mcp__other__get", {}, rules, self.root)[0], "deny")
        self.assertIsNone(repo_rules.call("mcp__acmex__get", {}, rules, self.root))
        whole = {"deny": [("Bash", None, "Bash", "x")], "ask": [], "allow": []}
        self.assertEqual(repo_rules.bash("echo hi", whole, self.root)[0], "deny")

    def test_rule_forms(self):
        self.assertEqual(repo_rules.parse("Bash(git push:*)"), ("Bash", "git push:*"))
        self.assertEqual(repo_rules.parse("Read"), ("Read", None))
        self.assertEqual(repo_rules.parse("Bash(*)"), ("Bash", None))
        self.assertEqual(repo_rules.parse("Edit(C:/x/\\(drafts\\)/**)"), ("Edit", "C:/x/(drafts)/**"))
        self.assertIsNone(repo_rules.parse("Bash(unclosed"))
        self.assertIsNone(repo_rules.parse(7))
        self.assertTrue(repo_rules.path_match("**/credentials/**", "/p/credentials", "/p"))
        self.assertFalse(repo_rules.path_match("./.env", "/q/.env", "/p"))
        self.assertTrue(repo_rules.path_match(f"/{DISK}/etc/hosts", "/etc/hosts", "/p"))


class Files(unittest.TestCase):
    """Which settings Claude Code reads (probe 2026-11-05): settings.json of the launch folder only;
    settings.local.json of the launch folder and of the canonical repo root (also the main checkout for a worktree)."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="hubcxf-")

    def tearDown(self):
        platforms.current.rmtree(self.root)

    def test_launch_folder_subfolder_and_worktree(self):
        main = os.path.join(self.root, "main")
        os.makedirs(os.path.join(main, ".git", "worktrees", "wt"))
        os.makedirs(os.path.join(main, "sub"))
        wt = os.path.join(self.root, "wt")
        os.makedirs(wt)
        with open(os.path.join(wt, ".git"), "w") as f:
            f.write(f"gitdir: {os.path.join(main, '.git', 'worktrees', 'wt')}\n")
        with open(os.path.join(main, ".git", "worktrees", "wt", "commondir"), "w") as f:
            f.write("../..\n")
        j = os.path.join
        # by the real path: git names the main repo so (macOS's TMPDIR is a link: /var → /private/var)
        r = lambda paths: [os.path.realpath(x) for x in paths]   # noqa: E731
        self.assertEqual(r(repo_rules.files(main)), r([j(main, ".claude", "settings.json"),
                                                       j(main, ".claude", "settings.local.json")]))
        self.assertEqual(r(repo_rules.files(j(main, "sub"))), r([j(main, "sub", ".claude", "settings.json"),
                                                                 j(main, "sub", ".claude", "settings.local.json"),
                                                                 j(main, ".claude", "settings.local.json")]))
        self.assertEqual(r(repo_rules.files(wt)), r([j(wt, ".claude", "settings.json"),
                                                     j(wt, ".claude", "settings.local.json"),
                                                     j(main, ".claude", "settings.local.json")]))
        self.assertEqual(repo_rules.files(None), [])
        # the hub's --setting-sources, as claude reads them
        self.assertEqual(repo_rules.files(j(main, "sub"), ("project",)), [j(main, "sub", ".claude", "settings.json")])
        self.assertEqual(r(repo_rules.files(wt, ("local",))), r([j(wt, ".claude", "settings.local.json"),
                                                                  j(main, ".claude", "settings.local.json")]))
        self.assertEqual(repo_rules.files(main, ()), [])
        # never a home folder's settings: those are the user's own (~/.claude/settings.json)
        from unittest import mock
        with mock.patch.dict(os.environ, {"HOME": main, "USERPROFILE": main}):
            self.assertEqual(repo_rules.files(main), [])
            self.assertEqual(repo_rules.files(j(main, "sub")), [j(main, "sub", ".claude", "settings.json"),
                                                               j(main, "sub", ".claude", "settings.local.json")])

    def test_a_broken_file_is_skipped_and_both_files_add_up(self):
        root = project({"deny": ["Bash(git push:*)"]}, local={"deny": ["Bash(make deploy*)"], "ask": "x"})
        try:
            rules = repo_rules.load(repo_rules.files(root) + [os.path.join(root, "missing.json")])
            self.assertEqual([r[2] for r in rules["deny"]], ["Bash(git push:*)", "Bash(make deploy*)"])
            with open(os.path.join(root, ".claude", "settings.json"), "w") as f:
                f.write("{not json")
            self.assertEqual([r[2] for r in repo_rules.load(repo_rules.files(root))["deny"]], ["Bash(make deploy*)"])
        finally:
            platforms.current.rmtree(root)


class Hook(unittest.TestCase):
    """The core's hook on a Codex call: the repo's deny and ask refuse with a reason (Codex's PreToolUse takes only
    deny); with Claude the hook does not read the repo's rules — Claude Code holds them itself."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubcxh-")
        self.home = os.path.join(self.state, "codex-home")
        os.makedirs(self.home)
        self.repo = project()
        codex_rules.thread({}, name="maps", state_dir=self.state, cwd=self.repo)

    def tearDown(self):
        platforms.current.rmtree(self.state)
        platforms.current.rmtree(self.repo)

    def run_hook(self, tool, tool_input, codex=True):
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_SESSION": "maps"}
        if codex:
            env["CODEX_HOME"] = self.home
        else:
            env.pop("CODEX_HOME", None)
        hook = {"hook_event_name": "PreToolUse", "cwd": self.repo, "tool_name": tool, "tool_input": tool_input}
        r = subprocess.run([sys.executable, deny_hint.__file__], input=json.dumps(hook), capture_output=True,
                           text=True, env=env, timeout=30, encoding="utf-8")
        out = json.loads(r.stdout)["hookSpecificOutput"] if r.stdout.strip() else {}
        return r.returncode, out.get("permissionDecision"), out.get("permissionDecisionReason")

    def test_the_session_file_names_the_repos_settings(self):
        with open(os.path.join(self.state, "sessions", "maps", codex_rules.RULES), encoding="utf-8") as f:
            got = json.load(f)
        self.assertEqual(got["settings"], repo_rules.files(self.repo))
        self.assertEqual(got["root"], os.path.abspath(self.repo))
        codex_rules.thread({}, name="other", state_dir=self.state, cwd=self.repo, sources=())   # setting_sources ""
        with open(os.path.join(self.state, "sessions", "other", codex_rules.RULES), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["settings"], [])
        codex_rules.thread({}, name="liaison", state_dir=self.state, cwd=self.repo, liaison=True)
        with open(os.path.join(self.state, "sessions", "liaison", codex_rules.RULES), encoding="utf-8") as f:
            self.assertNotIn("settings", json.load(f))

    def test_a_codex_pool_account_folder_holds_the_repos_rules_too(self):
        pool = os.path.join(self.state, "codex-pool", "acct-2")
        os.makedirs(pool)
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_SESSION": "maps", "CODEX_HOME": pool,
               "HUB_ON_CODEX": "1"}
        hook = {"hook_event_name": "PreToolUse", "cwd": self.repo, "tool_name": "Bash",
                "tool_input": {"command": "cd x && git push origin main"}}
        r = subprocess.run([sys.executable, deny_hint.__file__], input=json.dumps(hook), capture_output=True,
                           text=True, env=env, timeout=30, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        spec = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(spec["permissionDecision"], "deny")
        self.assertIn("Bash(git push:*)", spec["permissionDecisionReason"])

    def test_deny_and_ask_refuse_on_codex(self):
        code, decision, why = self.run_hook("Bash", {"command": "cd x && git push origin main"})
        self.assertEqual((code, decision), (0, "deny"))
        self.assertIn("Bash(git push:*)", why)
        self.assertIn(".claude/settings.json", why)
        code, decision, why = self.run_hook("Bash", {"command": "docker compose up -d"})
        self.assertEqual((code, decision), (0, "deny"))
        self.assertIn('the owner\'s "yes"', why)
        self.assertEqual(self.run_hook("apply_patch", {"command": "*** Begin Patch\n*** Add File: Dockerfile\n+x\n"
                                                                  "*** End Patch\n"})[1], "deny")
        self.assertEqual(self.run_hook("Bash", {"command": "git status && echo ok"}), (0, None, None))
        self.assertEqual(self.run_hook("Bash", {"command": "pkill -f x"})[1], "deny")   # the core's own bans stay

    def test_an_edit_of_the_settings_holds_at_once(self):
        self.assertEqual(self.run_hook("Bash", {"command": "make deploy"}), (0, None, None))
        with open(os.path.join(self.repo, ".claude", "settings.local.json"), "w") as f:
            json.dump({"permissions": {"deny": ["Bash(make deploy*)"]}}, f)
        self.assertEqual(self.run_hook("Bash", {"command": "make deploy"})[1], "deny")

    def test_claude_keeps_its_own_rules(self):
        self.assertEqual(self.run_hook("Bash", {"command": "git push"}, codex=False), (0, None, None))

    def test_a_broken_session_file_refuses(self):
        path = os.path.join(self.state, "sessions", "maps", codex_rules.RULES)
        with open(path, "w") as f:
            json.dump({"deny": [], "settings": "not a list"}, f)
        self.assertEqual(self.run_hook("Bash", {"command": "echo hi"})[:2], (0, "deny"))   # a deny: Windows lets exit 2 by


class Proc:
    def __init__(self, host, cwd):
        self.asks, self.backlog, self.items, self.host = {}, [], {}, host
        self.start_params = {"cwd": cwd, "approvalPolicy": "untrusted", "sandbox": "workspace-write"}


class Host:
    def auto_allow_on(self):
        return False


class Approvals(unittest.TestCase):
    """Codex asks (a mode other than full access): what the repo's allow rules cover Claude Code would not ask about —
    the core answers yes itself (auto=allow); anything else is the owner's card, as before."""

    def setUp(self):
        self.repo = project()

    def tearDown(self):
        platforms.current.rmtree(self.repo)

    def card(self, method, params, cwd=None):
        proc = Proc(Host(), cwd or self.repo)
        ((_, _, data),) = codex_rules.request(proc, "maps", 3, method, params)
        return data.get("auto")

    def command(self, cmd):
        return self.card("item/commandExecution/requestApproval",
                         {"itemId": "c", "command": f"/bin/bash -lc '{cmd}'", "cwd": self.repo,
                          "commandActions": [{"type": "unknown", "command": cmd}]})

    def test_the_real_command_and_every_path_decide(self):
        # the card shows Codex's summary of the command; the core judges what will run (review 2026-11-05)
        self.assertIsNone(self.card("item/commandExecution/requestApproval",
                                    {"itemId": "c", "command": "/bin/bash -lc 'git status && make deploy'",
                                     "cwd": self.repo, "commandActions": [{"type": "unknown", "command": "git status"}]}))
        self.assertEqual(self.card("execCommandApproval", {"command": ["git", "status"], "cwd": self.repo}), "allow")
        moved = {"src/a.py": {"type": "update", "move_path": "/etc/a.py"}}
        self.assertIsNone(self.card("applyPatchApproval", {"fileChanges": moved}))
        self.assertIsNone(self.card("applyPatchApproval", {"fileChanges": {"src/a.py": {"type": "update"}},
                                                           "grantRoot": "/"}))
        for extra in ({"additionalPermissions": {"fileSystem": {"write": ["/"]}}},
                      {"networkApprovalContext": {"host": "203.0.113.5"}}):
            self.assertIsNone(self.card("execCommandApproval", {"command": ["git", "status"], "cwd": self.repo,
                                                                **extra}), extra)
        proc = Proc(Host(), self.repo)
        proc.items["i1"] = {"changes": [{"path": "src/b.py", "kind": {"type": "update", "move_path": "lib/b.py"}}]}
        ((_, _, data),) = codex_rules.request(proc, "maps", 4, "item/fileChange/requestApproval", {"itemId": "i1"})
        self.assertNotIn("auto", data)
        proc.items["i2"] = {"changes": [{"path": "src/c.py", "kind": {"type": "add"}}]}
        ((_, _, data),) = codex_rules.request(proc, "maps", 5, "item/fileChange/requestApproval", {"itemId": "i2"})
        self.assertEqual(data.get("auto"), "allow")

    def test_allowed_commands_and_edits_go_without_a_card(self):
        self.assertEqual(self.command("git status -s"), "allow")
        # a hub whose setting_sources leave the project's settings out: their allow rules approve nothing
        proc = Proc(Host(), self.repo)
        proc.sources = ("local",)
        ((_, _, data),) = codex_rules.request(proc, "maps", 6, "execCommandApproval",
                                              {"command": ["git", "status"], "cwd": self.repo})
        self.assertNotIn("auto", data)
        self.assertIsNone(self.command("make build"))
        self.assertIsNone(self.command("git status && git push"))
        self.assertIsNone(self.command("docker compose up -d"))
        self.assertEqual(self.card("applyPatchApproval", {"fileChanges": {"src/a.py": {"type": "update"}}}), "allow")
        self.assertIsNone(self.card("applyPatchApproval", {"fileChanges": {"README.md": {"type": "update"}}}))
        nowhere = tempfile.mkdtemp(prefix="hubcxa-")
        try:
            self.assertIsNone(self.card("item/commandExecution/requestApproval",
                                        {"itemId": "c", "command": "git status", "cwd": nowhere}, cwd=nowhere))
        finally:
            platforms.current.rmtree(nowhere)



class Case(unittest.TestCase):
    """Release review 2026-11-09: on macOS and Windows .ENV is the file .env."""

    def test_a_case_blind_file_system_matches_any_case(self):
        top = os.path.realpath(tempfile.mkdtemp(prefix="hubcxc-"))
        self.addCleanup(platforms.current.rmtree, top)
        root = os.path.join(top, "Proj")   # a mixed case the fold changes
        os.makedirs(root)
        with mock.patch.object(repo_rules, "FOLD", True):
            self.assertTrue(repo_rules.path_match(".env", os.path.join(root, ".ENV"), root))
            self.assertTrue(repo_rules.path_match("**/*.key", os.path.join(root, "a", "B.KEY"), root))
            try:   # realpath answers in the disk's own case: the link's target is matched after the fold
                os.symlink(os.path.join(root, ".env"), os.path.join(root, "notes"))
            except OSError:   # Windows without the right to make links
                return
            self.assertTrue(repo_rules.path_match(".env", os.path.join(root, "notes"), root))
        with mock.patch.object(repo_rules, "FOLD", False):
            self.assertFalse(repo_rules.path_match(".env", os.path.join(root, ".ENV"), root))


if __name__ == "__main__":
    unittest.main()
