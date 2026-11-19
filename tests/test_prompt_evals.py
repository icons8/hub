"""Prompt evals (evals/prompts.py): the hub prompt is taken from two code versions, a real model answers the same
inputs, checks judge the answers. Here a fake stands in for claude (no model turns); the case files in evals/ have no
machine paths and the right shape."""
import tests  # noqa: F401 — do not see the machine's settings (~/.config/hub-core), see tests/__init__.py
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "evals"))
import platforms  # noqa: E402
import prompts  # noqa: E402
from common import case_dirs, case_files, find_case  # noqa: E402
from tests.fakes.exe import script  # noqa: E402

# machine paths (made-up — /home/x/…: cases on a hub journal carry them as an example)
PRIVATE = ("/data/", "/home/" + "hub", "/Users/", "/root/")  # concatenated: whole, it is the home form itself


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


class Source(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="hubeval-")
        self.addCleanup(shutil.rmtree, self.repo, True)
        git(self.repo, "init", "-q", "-b", "main")
        os.makedirs(os.path.join(self.repo, "pkg"))
        open(os.path.join(self.repo, "pkg", "__init__.py"), "w").close()
        with open(os.path.join(self.repo, "pkg", "m.py"), "w") as f:
            f.write('PROMPT = ("old " "text {language}")\nSCHEMA = {"type": "object"}\n')
        with open(os.path.join(self.repo, "doc.md"), "w") as f:
            f.write("old doc\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "one")
        self.old = git(self.repo, "rev-parse", "HEAD")
        with open(os.path.join(self.repo, "pkg", "m.py"), "w") as f:
            f.write('PROMPT = "new text {language}"\nSCHEMA = {"type": "object"}\n')
        with open(os.path.join(self.repo, "doc.md"), "w") as f:
            f.write("new doc\n")
        git(self.repo, "commit", "-q", "-am", "two")

    def test_a_python_constant_is_read_at_each_version(self):
        spec = {"file": "pkg/m.py", "name": "PROMPT"}
        self.assertEqual(prompts.prompt_at(self.repo, self.old, spec), "old text {language}")
        self.assertEqual(prompts.prompt_at(self.repo, "HEAD", spec), "new text {language}")
        self.assertEqual(prompts.prompt_at(self.repo, "HEAD", {"file": "pkg/m.py", "name": "SCHEMA"}), {"type": "object"})

    def test_a_document_is_read_whole_and_a_renamed_file_by_its_before_path(self):
        self.assertEqual(prompts.prompt_at(self.repo, self.old, {"file": "doc.md"}), "old doc\n")
        git(self.repo, "mv", "doc.md", "new.md")
        git(self.repo, "commit", "-q", "-am", "three")
        spec = {"file": "new.md", "before_file": "doc.md"}
        self.assertEqual(prompts.prompt_at(self.repo, "HEAD", spec, before=False), "new doc\n")
        self.assertEqual(prompts.prompt_at(self.repo, self.old, spec, before=True), "old doc\n")

    def test_a_missing_file_at_the_version_is_an_error_naming_it(self):
        with self.assertRaises(SystemExit) as cm:
            prompts.prompt_at(self.repo, self.old, {"file": "nope.md"})
        self.assertIn("nope.md", str(cm.exception))


class Checks(unittest.TestCase):
    ANSWER = {"text": "Run `hub direction resume x` first.", "fields": {"trouble": True, "p": 0.8, "now": "Fixes the feed"}}

    def c(self, **k):
        return prompts.check(k, self.ANSWER)[0]

    def test_fields(self):
        self.assertTrue(self.c(type="field", name="trouble", equals=True))
        self.assertFalse(self.c(type="field", name="trouble", equals=False))
        self.assertTrue(self.c(type="field", name="p", min=0.6))
        self.assertFalse(self.c(type="field", name="p", max=0.5))
        self.assertTrue(self.c(type="field", name="now", re="^Fixes"))
        self.assertTrue(self.c(type="field", name="now", max_words=3))
        self.assertFalse(self.c(type="field", name="now", max_words=2))
        self.assertFalse(self.c(type="field", name="absent", equals=True))   # no field — a failure, not a pass
        self.assertTrue(self.c(type="field", name="now", not_re="archive"))
        self.assertFalse(self.c(type="field", name="now", not_re="^Fixes"))

    def test_text(self):
        self.assertTrue(self.c(type="said", re="hub direction resume"))
        self.assertFalse(self.c(type="said", re="archive"))
        self.assertTrue(self.c(type="not_said", re="archive"))
        self.assertTrue(self.c(type="any_of", checks=[{"type": "said", "re": "nothing"}, {"type": "said", "re": "resume"}]))

    def test_a_field_with_what_it_runs_from_another_field(self):
        # PROGRESS evals on Codex: the monitor is often a script the first command writes; the scheduled command runs
        # it — the check reads both. Only then: a first command that merely notifies once is not the monitor, and a
        # shell variable it sets is gone when the schedule runs
        check = {"type": "field", "name": "schedule_command", "with": "first_command",
                 "re": r"hub schedule add\b.*--command\s.*hub notify"}
        script = {"schedule_command": "hub schedule add --id p --every 30m --command 'bash /tmp/p.sh'",
                  "first_command": "cat > /tmp/p.sh <<'SH'\nhub notify --id p \"$k of 40\"\nSH\nbash /tmp/p.sh"}
        variable = {"schedule_command": 'hub schedule add --id p --every 30m --command "$MON"',
                    "first_command": "MON='k=$(cat n); hub notify --id p \"$k of 40\"'"}
        for fields in (script, {**script, "first_command": "tee /tmp/p.sh <<'SH'\nhub notify --id p x\nSH"},
                       {**script, "first_command": "cat <<'SH' > /tmp/p.sh\nhub notify --id p x\nSH"}):
            self.assertTrue(prompts.check(check, {"fields": fields})[0], fields)
        unrelated = {"schedule_command": "hub schedule add --id p --every 30m --command 'bash /tmp/q.sh'",
                     "first_command": "hub notify --id p 'started'"}
        once = {**script, "first_command": "cat > /tmp/p.sh <<'SH'\nk=1\nSH\nhub notify --id p \"$k of 40\""}   # outside it
        other_file = {**script, "schedule_command": script["schedule_command"].replace("/tmp/p.sh", "/tmp/xp.sh")}
        bare = {"schedule_command": "hub schedule add --id p --command 'bash /tmp/p.sh'"}
        for fields in (unrelated, variable, once, other_file, bare):
            self.assertFalse(prompts.check(check, {"fields": fields})[0], fields)
        # without `with` the other field is not read
        self.assertFalse(prompts.check({k: v for k, v in check.items() if k != "with"}, {"fields": script})[0])

    def test_a_list_field_is_judged_command_by_command_from_its_start(self):
        # a command that writes a file is an edit; a hub command whose text names the file is not (the old whole-list
        # regex read "-> PLAN.md" inside a note as a write)
        check = {"type": "field", "name": "cmds", "item_not_re": r"^(?!\s*hub\b).*>\s*\S*PLAN\.md"}
        self.assertTrue(prompts.check(check, {"fields": {"cmds": ['hub task note "plan -> PLAN.md"', "ls"]}})[0])
        self.assertFalse(prompts.check(check, {"fields": {"cmds": ["ls", "echo x > PLAN.md"]}})[0])
        self.assertTrue(prompts.check(check, {"fields": {"cmds": []}})[0])
        some = {"type": "field", "name": "cmds", "item_re": r"^hub task run\b"}
        self.assertTrue(prompts.check(some, {"fields": {"cmds": ["hub task show", "hub task run x"]}})[0])
        self.assertFalse(prompts.check(some, {"fields": {"cmds": ['hub send x "hub task run x"']}})[0])
        # with `with` the items are still the list's own commands, not its text
        self.assertTrue(prompts.check({**some, "with": "other"}, {"fields": {"cmds": ["hub task run x"], "other": ""}})[0])
        # and the script a command runs, brought in by `with`, is one more item
        banned = {"type": "field", "name": "cmds", "with": "other", "item_not_re": r"^hub task run\b"}
        script = {"cmds": ["bash /tmp/m.sh"], "other": "cat > /tmp/m.sh <<'SH'\nhub task run x\nSH"}
        self.assertFalse(prompts.check(banned, {"fields": script})[0])

    def test_text_is_the_fields_too_for_a_schema_answer(self):
        a = {"text": "", "fields": {"reason": "Loop dropped"}}
        self.assertTrue(prompts.check({"type": "field", "name": "reason", "re": "[a-z]"}, a)[0])
        self.assertTrue(prompts.check({"type": "said", "re": "dropped"}, a)[0])


class TraceChecks(unittest.TestCase):
    """The checks over what a model did with its tools (case: "trace"): its own calls and the files it left."""
    ANSWER = {"text": "", "tools": [{"name": "Bash", "input": {"command": "python3 -c 'print(1)'"}},
                                     {"name": "Write", "input": {"file_path": "specs/d1/spec.md", "content": "x"}},
                                     {"name": "Bash", "input": {"command": "git commit -am x"}}],
              "changed": ["specs/d1/spec.md", "src/a.py"], "files": {"specs/d1/spec.md": "# spec\nabc", "src/a.py": "x = 1\n"}}

    def c(self, **k):
        return prompts.check(k, self.ANSWER)[0]

    def test_tool_calls_are_found_by_name_and_by_command_or_path(self):
        self.assertTrue(self.c(type="tool", name="Bash", re="python"))
        self.assertFalse(self.c(type="tool", name="Bash", re="pytest"))
        self.assertTrue(self.c(type="no_tool", name="Bash", re=r"\bgit\s+push\b"))
        self.assertFalse(self.c(type="no_tool", name="Bash", re=r"\bgit\s+commit\b"))
        # a write to the spec's own path does not count against "no writes": except_re
        self.assertFalse(self.c(type="no_tool", name="Edit|Write", re=""))
        self.assertTrue(self.c(type="no_tool", name="Edit|Write", except_re=r"^specs/d1/spec\.md$"))
        self.assertFalse(self.c(type="no_tool", name="Edit|Write", except_re=r"^specs/d2/"))
        self.assertTrue(self.c(type="no_tool", name="Edit"))   # the whole name: Edit is not Write
        self.assertTrue(prompts.check({"type": "no_tool", "name": "Bash"}, {"text": "", "tools": []})[0])   # traced, no calls
        # no trace at all is not "no calls": the guarantee was not checked
        self.assertFalse(prompts.check({"type": "no_tool", "name": "Bash"}, {"text": ""})[0])
        self.assertFalse(prompts.check({"type": "tool", "name": "Bash"}, {"text": ""})[0])

    def test_what_the_tools_gave_back_is_judged_too(self):
        # spec d2441132: a value the model read is a failure whatever the command (listing a file's key names is not)
        seen = {"text": "", "tools": [], "seen": ["DB_URL\nGITLAB_TOKEN", '[{"type": "text", "text": "tok-MARK-1"}]']}
        self.assertTrue(prompts.check({"type": "not_seen", "re": "SECRET"}, seen)[0])
        self.assertFalse(prompts.check({"type": "not_seen", "re": "MARK"}, seen)[0])
        self.assertFalse(prompts.check({"type": "not_seen", "re": "MARK"}, {"text": ""})[0])   # untraced: no guarantee
        events = [{"type": "user", "message": {"content": [{"type": "tool_result", "content": "a"},
                                                            {"type": "tool_result", "content": [{"type": "text", "text": "b"}]}]}},
                  {"type": "assistant", "message": {"content": [{"type": "text", "text": "c"}]}}]
        self.assertEqual(prompts.tool_results(events), ["a", '[{"type": "text", "text": "b"}]'])

    def test_changed_files_are_judged_against_a_pattern(self):
        self.assertFalse(self.c(type="changed", only_re=r"^specs/"))   # src/a.py is outside
        self.assertTrue(self.c(type="changed", only_re=r"^(specs|src)/"))
        self.assertTrue(self.c(type="changed", has_re=r"^src/a\.py$"))
        self.assertFalse(self.c(type="changed", has_re=r"^src/b\.py$"))
        self.assertFalse(prompts.check({"type": "changed", "has_re": "x"}, {"changed": []})[0])   # nothing changed, a change wanted
        self.assertTrue(prompts.check({"type": "changed", "only_re": "x"}, {"changed": []})[0])

    def test_a_file_the_model_left(self):
        self.assertTrue(self.c(type="file", path="specs/d1/spec.md", min_len=5, re="abc"))
        self.assertFalse(self.c(type="file", path="specs/d1/spec.md", min_len=500))
        self.assertFalse(self.c(type="file", path="specs/d1/spec.md", not_re="abc"))
        self.assertFalse(self.c(type="file", path="nope.md"))
        self.assertTrue(self.c(type="file", path="nope.md", exists=False))

    def test_the_folder_is_compared_with_its_seed_without_the_runs_own_leavings(self):
        d = tempfile.mkdtemp(prefix="hubeval-")
        self.addCleanup(platforms.current.rmtree, d)
        for rel, body in {"a.py": "1", "pkg/b.py": "2", "pkg/__pycache__/b.pyc": "x", ".hub-eval/calls/1.json": "[]"}.items():
            os.makedirs(os.path.dirname(os.path.join(d, rel)), exist_ok=True)
            with open(os.path.join(d, rel), "w") as f:
                f.write(body)
        self.assertEqual(prompts.snapshot(d), {"a.py": "1", "pkg/b.py": "2"})


class FakeClaude(unittest.TestCase):
    """A whole run on a script instead of claude: it answers by the system prompt it received."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hubeval-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        # a file per call: trials run in parallel, and on Windows appends of two processes to one file are not atomic
        # (a line was torn: JSONDecodeError)
        self.log = os.path.join(self.dir, "calls")
        os.makedirs(self.log)
        self.fake = script(os.path.join(self.dir, "claude"), r'''
import json, os, sys, uuid
argv = sys.argv[1:]
system = argv[argv.index("--system-prompt") + 1]
with open(os.path.join(%r, uuid.uuid4().hex + ".json"), "w") as log:
    log.write(json.dumps({"argv": argv, "stdin": sys.stdin.read(),
                          "thinking": os.environ.get("MAX_THINKING_TOKENS")}))
if "--json-schema" in argv:
    print(json.dumps({"is_error": False, "result": "", "structured_output": {"trouble": "NEW" in system}}))
else:
    print(json.dumps({"is_error": False, "result": "answer " + system[:3]}))
''' % self.log)
        self.env = {**os.environ, "EVAL_CLAUDE": self.fake, "EVALS_DIR": os.path.join(self.dir, "work")}

    def calls(self):
        out = []
        for name in os.listdir(self.log):
            with open(os.path.join(self.log, name)) as f:
                out.append(json.load(f))
        return out

    def test_both_versions_get_their_prompt_and_the_table_counts_passes(self):
        repo = os.path.join(self.dir, "repo")
        os.makedirs(repo)
        git(repo, "init", "-q", "-b", "main")
        for body in ("OLD {language}", "NEW {language}"):
            with open(os.path.join(repo, "p.md"), "w") as f:
                f.write(body)
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", body)
        case = {"id": "t", "prompt": {"file": "p.md", "format": {"language": "English"}, "schema": {"type": "object"}},
                "before": "HEAD~1",
                "turns": [{"id": "a", "input": "step one", "checks": [{"type": "field", "name": "trouble", "equals": True}]},
                          {"id": "b", "input": {"before": "old step", "after": "step"},
                           "checks": [{"type": "field", "name": "trouble", "equals": True}]}]}
        path = os.path.join(self.dir, "case.json")
        with open(path, "w") as f:
            json.dump(case, f)
        r = subprocess.run([sys.executable, os.path.join(REPO, "evals", "prompts.py"), path, "--repo", repo, "--trials", "2"],
                           env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = [x for x in r.stdout.splitlines() if x.startswith("| t")]
        self.assertEqual(len(rows), 4, r.stdout)   # 2 turns × before/after
        by = {(x.split("|")[1].strip(), x.split("|")[2].strip().split()[0]): x.split("|")[3].strip() for x in rows}
        self.assertEqual(by[("t/a", "before")], "0/2")   # the old prompt has no NEW
        self.assertEqual(by[("t/a", "after")], "2/2")
        stdins = {(c["argv"][c["argv"].index("--system-prompt") + 1][:3], c["stdin"]) for c in self.calls()}
        self.assertEqual(stdins, {("OLD", "step one"), ("OLD", "old step"), ("NEW", "step one"), ("NEW", "step")})
        self.assertTrue(all("English" in c["argv"][c["argv"].index("--system-prompt") + 1] for c in self.calls()))
        # asked the way the hub asks the cheap model: low effort (the new Haiku ignores a thinking budget of 0)
        for c in self.calls():
            self.assertEqual(c["argv"][c["argv"].index("--effort") + 1] if "--effort" in c["argv"] else None, "low")
            self.assertIsNone(c["thinking"])

    @unittest.skipIf(os.name == "nt", "the fake is a .cmd on Windows, and cmd.exe cuts an argument at the first line break")
    def test_appended_notes_follow_the_prompt_and_only_they_are_formatted(self):
        # the session's system prompt is its document plus the core's note; the document may have its own curly braces
        repo = os.path.join(self.dir, "repo")
        os.makedirs(repo)
        git(repo, "init", "-q", "-b", "main")
        with open(os.path.join(repo, "doc.md"), "w") as f:
            f.write("DOC with {braces}")
        with open(os.path.join(repo, "note.py"), "w") as f:
            f.write('NOTE = "write in {lang}"\n')
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "x")
        case = {"id": "t", "prompt": {"file": "doc.md", "append": [{"file": "note.py", "name": "NOTE"}]}, "before": None,
                "turns": [{"id": "a", "input": "q", "format": {"lang": "German"}, "checks": [{"type": "said", "re": "answer"}]}]}
        path = os.path.join(self.dir, "case.json")
        with open(path, "w") as f:
            json.dump(case, f)
        r = subprocess.run([sys.executable, os.path.join(REPO, "evals", "prompts.py"), path, "--repo", repo, "--trials", "1"],
                           env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        (call,) = self.calls()
        self.assertEqual(call["argv"][call["argv"].index("--system-prompt") + 1], "DOC with {braces}\n\nwrite in German")

    @unittest.skipIf(os.name == "nt", "the fake is a .cmd on Windows, and cmd.exe cuts an argument at the first line break")
    def test_the_owners_language_is_the_word_the_core_of_each_version_passes(self):
        # a case names the language by any word a core used; each version's rule gets that version's own word
        repo = os.path.join(self.dir, "repo")
        os.makedirs(os.path.join(repo, "core"))
        git(repo, "init", "-q", "-b", "main")
        with open(os.path.join(repo, "note.py"), "w") as f:
            f.write('NOTE = "write in {lang}"\n')
        for words in ('{"ru": "rus-old"}', '{"ru": "rus-new"}'):
            with open(os.path.join(repo, "core", "forms.py"), "w") as f:
                f.write('LANG_CODES = {"rus-old": "ru", "rus-new": "ru", "ru": "ru"}\nLANG_WORDS = %s\n' % words)
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", words)
        case = {"id": "t", "prompt": {"file": "note.py", "name": "NOTE"}, "before": "HEAD~1",
                "turns": [{"id": "a", "input": "q", "format": {"lang": "rus-old"}, "checks": [{"type": "said", "re": "x"}]},
                          # a word only a later core uses: the older version gets its own
                          {"id": "b", "input": "q", "format": {"lang": "Russian"}, "checks": [{"type": "said", "re": "x"}]}]}
        path = os.path.join(self.dir, "case.json")
        with open(path, "w") as f:
            json.dump(case, f)
        r = subprocess.run([sys.executable, os.path.join(REPO, "evals", "prompts.py"), path, "--repo", repo, "--trials", "1"],
                           env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        systems = sorted(c["argv"][c["argv"].index("--system-prompt") + 1] for c in self.calls())
        self.assertEqual(systems, ["write in rus-new", "write in rus-new", "write in rus-old", "write in rus-old"])

    @unittest.skipIf(os.name == "nt", "the fake is a .cmd on Windows, and cmd.exe cuts an argument at the first line break")
    def test_the_cores_words_inside_an_input_are_of_each_version(self):
        repo = os.path.join(self.dir, "repo")
        os.makedirs(repo)
        git(repo, "init", "-q", "-b", "main")
        for hint in ("old hint for {name}", "new hint for {name}"):
            with open(os.path.join(repo, "hints.py"), "w") as f:
                f.write("SYS = 'sys'\nHINT = %r\n" % hint)
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", hint)
        case = {"id": "t", "prompt": {"file": "hints.py", "name": "SYS"}, "before": "HEAD~1",
                "turns": [{"id": "a", "input": "denied. ⟦H⟧ go on", "checks": [{"type": "said", "re": "x"}],
                           "consts": {"H": {"file": "hints.py", "name": "HINT", "format": {"name": "maps"}}}}]}
        path = os.path.join(self.dir, "case.json")
        with open(path, "w") as f:
            json.dump(case, f)
        r = subprocess.run([sys.executable, os.path.join(REPO, "evals", "prompts.py"), path, "--repo", repo, "--trials", "1"],
                           env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(c["stdin"] for c in self.calls()),
                         ["denied. new hint for maps go on", "denied. old hint for maps go on"])
        case["turns"][0]["input"] = "denied. ⟦X⟧"   # a placeholder without its constant is a broken case, not a prompt
        with open(path, "w") as f:
            json.dump(case, f)
        r = subprocess.run([sys.executable, os.path.join(REPO, "evals", "prompts.py"), path, "--repo", repo, "--trials", "1"],
                           env=self.env, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)

    @unittest.skipIf(os.name == "nt", "the fake is a .cmd on Windows, and cmd.exe cuts an argument at the first line break")
    def test_a_traced_run_records_the_tool_calls_and_what_it_changed_in_its_folder(self):
        # the model's own tool calls come from stream-json; the folder is compared with the seed (a case's files, a turn's
        # laid over them) after the run, and the version's constant a case marks optional is empty where it is missing
        script(os.path.join(self.dir, "claude"), r'''
import json, os, sys
argv = sys.argv[1:]
sys.stdin.read()
assert argv[argv.index("--output-format") + 1] == "stream-json" and "--verbose" in argv
os.remove("seed.txt")
with open("new.txt", "w") as f:
    f.write("hello")
os.makedirs("__pycache__")
open("__pycache__/x.pyc", "w").close()
for e in ({"type": "system"},
          {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"},
                                                          {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                                                          {"type": "tool_use", "name": "Write",
                                                           "input": {"file_path": os.getcwd() + "/new.txt"}}]}},
          {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}},
          {"type": "result", "is_error": False, "result": "done", "total_cost_usd": 0.5}):
    print(json.dumps(e))
''')
        repo = os.path.join(self.dir, "repo")
        os.makedirs(repo)
        git(repo, "init", "-q", "-b", "main")
        with open(os.path.join(repo, "p.md"), "w") as f:
            f.write("SYS")
        with open(os.path.join(repo, "c.py"), "w") as f:
            f.write("HAVE = 'a line'\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "x")
        case = {"id": "t", "prompt": {"file": "p.md"}, "before": "HEAD", "trace": True, "files": {"seed.txt": "s", "keep.txt": "k", ".hub-eval/state/config/c.json": "{}"},
                "turns": [{"id": "a", "input": "q ⟦HAVE⟧ ⟦LACK⟧ end", "files": {"turn.txt": "t"},
                           "consts": {"HAVE": {"file": "c.py", "name": "HAVE", "optional": True},
                                      "LACK": {"file": "c.py", "name": "LACK", "optional": True}},
                           "checks": [{"id": "ran", "type": "tool", "name": "Bash", "re": "^ls$"},
                                      {"id": "changed", "type": "changed", "only_re": r"^(seed|new|turn)\.txt$", "has_re": "new"},
                                      {"id": "left", "type": "file", "path": "new.txt", "re": "^hello$"},
                                      {"id": "kept", "type": "file", "path": "keep.txt", "re": "^k$"},   # not changed, still read
                                      {"id": "gone", "type": "file", "path": "seed.txt", "exists": False},
                                      {"id": "own_leavings_are_not_changes", "type": "changed", "only_re": r"^(seed|new)\.txt$"}]}]}
        path = os.path.join(self.dir, "case.json")
        with open(path, "w") as f:
            json.dump(case, f)
        r = subprocess.run([sys.executable, os.path.join(REPO, "evals", "prompts.py"), path, "--repo", repo, "--trials", "1",
                            "--only", "after"], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("| 1/1 |", r.stdout.replace("`", ""), r.stdout)
        log = os.path.join(self.dir, "work", "prompt-runs")
        with open(os.path.join(log, os.listdir(log)[0]), encoding="utf-8") as f:
            (row,) = [json.loads(x) for x in f]
        self.assertEqual(row["answer"]["changed"], ["new.txt", "seed.txt"])   # the seed file removed, a new one made; not the cache
        # the model's own paths are as the folder sees them, so a check can anchor them like a changed path
        self.assertEqual(row["answer"]["tools"], [{"name": "Bash", "input": {"command": "ls"}},
                                                  {"name": "Write", "input": {"file_path": "new.txt"}}])

    def test_a_claude_without_a_login_is_not_a_pass(self):
        script(os.path.join(self.dir, "claude"),
               'import json\nprint(json.dumps({"is_error": True, "result": "Not logged in · Please run /login"}))\n')
        repo = os.path.join(self.dir, "repo")
        os.makedirs(repo)
        git(repo, "init", "-q", "-b", "main")
        with open(os.path.join(repo, "p.md"), "w") as f:
            f.write("X")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "x")
        case = {"id": "t", "prompt": {"file": "p.md"}, "before": "HEAD",
                "turns": [{"id": "a", "input": "q", "checks": [{"type": "not_said", "re": "never"}]}]}
        path = os.path.join(self.dir, "case.json")
        with open(path, "w") as f:
            json.dump(case, f)
        r = subprocess.run([sys.executable, os.path.join(REPO, "evals", "prompts.py"), path, "--repo", repo, "--only", "after", "--trials", "1"],
                           env=self.env, capture_output=True, text=True, timeout=60)
        self.assertIn("0/0 (+1 n/a)", r.stdout)


CYR = "\u0400-\u052f\u1c80-\u1c8f\u2de0-\u2dff\ua640-\ua69f"   # the Cyrillic blocks, as escapes: this file stays clean
LETTER_RANGE = f"[{CYR}]-[{CYR}]"
LETTER_CLASS = re.compile(r"\[\^?[^\]]*?" + LETTER_RANGE + r"[^\]]*\]")   # a regular-expression class with a letter range
CYRILLIC_WORD = re.compile(f"[{CYR}]{{2,}}")
A, YA, YO = chr(0x430), chr(0x44f), chr(0x451)   # Russian letters, each its own piece (the ratchet counts words)
WORD = "".join(chr(c) for c in (0x43f, 0x443, 0x441, 0x43a))   # a Russian word


def russian_words(text):
    """The Russian in a case: runs of two or more Cyrillic letters outside a letter class — a check that the answer is not
    Russian (`[...-...]` with a letter range in it) is not Russian text, a lone letter is not a word. The text is
    the case's JSON read and written back, so a \\u-escaped word is seen as the word it is."""
    flat = json.dumps(json.loads(text), ensure_ascii=False)
    return CYRILLIC_WORD.findall(LETTER_CLASS.sub("", flat))


class CaseFiles(unittest.TestCase):
    """What is in evals/ and, in a copy that has it, evals/locale/ru/ (the cases of a Russian-speaking owner): no machine
    paths, and every prompt case has the right weight."""

    def files(self, sub):
        return case_files(sub)

    def test_no_case_names_a_machine_or_a_private_conversation(self):
        for path in self.files("cases") + self.files("prompts"):
            with open(path, encoding="utf-8") as f:
                text = f.read()
            for bad in PRIVATE:
                self.assertNotIn(bad, text, f"{os.path.basename(path)}: {bad}")

    def test_prompt_cases_have_two_turns_each_with_checks(self):
        found = self.files("prompts")
        self.assertTrue(found)
        for path in found:
            with open(path, encoding="utf-8") as f:
                case = json.load(f)
            name = os.path.basename(path)
            self.assertTrue(case.get("title") and case.get("note"), name)
            self.assertGreaterEqual(len(case["turns"]), 2, name)
            self.assertTrue(os.path.isfile(os.path.join(REPO, case["prompt"]["file"])), f"{name}: {case['prompt']['file']}")
            for turn in case["turns"]:
                self.assertTrue((turn.get("input") or turn.get("args")) and turn.get("checks"), f"{name}: {turn.get('id')}")
            self.assertEqual(len({t["id"] for t in case["turns"]}), len(case["turns"]), name)

    def test_a_case_with_russian_in_it_is_a_russian_owners_case_and_lives_in_the_unpublished_folder(self):
        # the open tree has English cases only: a case with Russian words is marked "owner_lang": "ru" and kept in
        # evals/locale/ru/ (publish.sh does not publish it); a case without the mark is red here
        for sub in ("cases", "prompts"):
            for d in case_dirs(sub):
                russian_folder = os.path.join("locale", "ru") in d
                for name in sorted(os.listdir(d)):
                    if not name.endswith(".json"):
                        continue
                    with open(os.path.join(d, name), encoding="utf-8") as f:
                        text = f.read()
                    mark = json.loads(text).get("owner_lang")
                    where = os.path.join(os.path.relpath(d, REPO), name)
                    if russian_folder:
                        self.assertEqual(mark, "ru", f"{where}: a case here is marked \"owner_lang\": \"ru\"")
                    else:
                        self.assertNotEqual(mark, "ru", f"{where}: a case marked ru belongs in evals/locale/ru/{sub}/")
                        self.assertEqual(russian_words(text)[:3], [], f"{where}: Russian in a case without the ru mark — "
                                         f"write it in English, or mark it \"owner_lang\": \"ru\" and move it to evals/locale/ru/{sub}/")

    def test_a_case_is_told_by_its_file_name_alone(self):
        # `prompts.py hub-home` takes the one case of that name: no two cases share a file name across the folders
        names = [os.path.basename(p) for sub in ("cases", "prompts") for p in case_files(sub, ext="")]
        self.assertEqual([n for n in set(names) if names.count(n) > 1 and n.endswith(".json")], [])

    def test_a_fixture_a_case_names_is_there(self):
        for path in self.files("cases") + self.files("prompts"):
            with open(path, encoding="utf-8") as f:
                text = f.read()
            for ref in re.findall(r"\$evals/([\w./-]+\.jsonl)", text):
                self.assertTrue(os.path.isfile(os.path.join(REPO, "evals", ref)), f"{os.path.basename(path)}: {ref}")

    def test_the_words_of_a_case_are_told_from_a_letter_class(self):
        self.assertEqual(russian_words('{"re": "[' + A + "-" + YA + YO + ']{4}"}'), [])   # a-ya, yo
        self.assertEqual(russian_words('["[^' + A + "-" + YA + 'a-z]{1,3}"]'), [])
        self.assertEqual(russian_words('["' + YA + ' alone"]'), [])
        self.assertEqual(russian_words('{"re": "[' + A + "-" + YA + YO + '|/]{4}"}'), [])   # any class
        self.assertEqual(russian_words('{"x": "' + chr(92) + "u%04x" % ord(WORD[0]) + chr(92) + 'u%04x"}' % ord(WORD[1])), [WORD[:2]])   # escaped
        self.assertEqual(russian_words('["restart|' + WORD + '"]'), [WORD])


class CaseFolders(unittest.TestCase):
    """evals/common.py: the public case folder and the Russian owner's one are read together."""

    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="hubeval-")
        self.addCleanup(platforms.current.rmtree, self.repo)
        for rel in ("cases/a.json", "prompts/b.json", "locale/ru/prompts/c.json", "locale/ru/cases/d.json", "prompts/e.txt"):
            os.makedirs(os.path.dirname(os.path.join(self.repo, "evals", rel)), exist_ok=True)
            with open(os.path.join(self.repo, "evals", rel), "w") as f:
                f.write("{}")

    def test_both_folders_are_listed_and_a_name_is_found_in_either(self):
        names = lambda sub: [os.path.relpath(p, os.path.join(self.repo, "evals")) for p in case_files(sub, self.repo)]
        self.assertEqual(names("prompts"), [os.path.join("prompts", "b.json"), os.path.join("locale", "ru", "prompts", "c.json")])
        self.assertEqual(names("cases"), [os.path.join("cases", "a.json"), os.path.join("locale", "ru", "cases", "d.json")])
        self.assertEqual(find_case("c", self.repo), os.path.join(self.repo, "evals", "locale", "ru", "prompts", "c.json"))
        self.assertEqual(find_case("b.json", self.repo), os.path.join(self.repo, "evals", "prompts", "b.json"))
        self.assertEqual(find_case(os.path.join(self.repo, "evals", "cases", "a.json")),
                         os.path.join(self.repo, "evals", "cases", "a.json"))
        self.assertIsNone(find_case("e", self.repo))

    def test_a_copy_without_the_russian_folder_has_only_the_public_one(self):
        platforms.current.rmtree(os.path.join(self.repo, "evals", "locale"))
        self.assertEqual([os.path.basename(p) for p in case_files("prompts", self.repo)], ["b.json"])
        self.assertIsNone(find_case("c", self.repo))

    def test_the_runners_name_a_case_they_do_not_find(self):
        # no hub of this machine is read: settings, state and home are the test's own
        env = {**os.environ, "HUB_CONFIG_DIR": self.repo, "HUB_STATE_DIR": self.repo, "HOME": self.repo}
        for runner in ("prompts.py", "run.py"):
            r = subprocess.run([sys.executable, os.path.join(REPO, "evals", runner), "no-such-case-anywhere"], env=env,
                               capture_output=True, text=True, timeout=60)
            self.assertNotEqual(r.returncode, 0, runner)
            self.assertIn("no such case: no-such-case-anywhere", r.stderr, runner)


if __name__ == "__main__":
    unittest.main()
