"""The stops audit precheck (modules/agent_dev/stops-audit.py, the core job agent-dev-stops): due -> not marked ->
"MISSED" on every tick -> --done -> quiet -> early when a file grew / after 7 days. The check itself is light: it reads
neither the journal nor its archives (the first cache build reads gigabytes) — only --bundle does."""
import tests  # noqa: F401 — keeps the machine's settings out
import glob
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from core import journal
from tests import winsafe
from tests.fakes import exe
from tests.fakes import cut

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "modules", "agent_dev", "stops-audit.py")


class Hub(unittest.TestCase):
    """A state folder with two agents and a journal of accepted cards; the script is run as a process."""

    def setUp(self):
        self.h = tempfile.mkdtemp(prefix="stops-")
        self.addCleanup(shutil.rmtree, self.h, True)
        os.makedirs(os.path.join(self.h, "agents", "sid1"))
        os.makedirs(os.path.join(self.h, "agents", "prof2"))
        os.makedirs(os.path.join(self.h, "journal-archive"))
        sessions = {"maps": {"role": "agent", "session_id": "sid1", "state": "idle"},
                    # a system agent that came back without its conversation keeps the old file: the profile names it
                    "docs": {"role": "agent", "session_id": "sid9", "profile": "prof2", "state": "idle"},
                    "w": {"role": "worker", "session_id": "x", "state": "idle"}}
        with open(os.path.join(self.h, "state.json"), "w") as f:
            json.dump({"state": {"sessions": sessions}}, f)
        self.agent = os.path.join(self.h, "agents", "sid1", "CLAUDE.md")
        self.write(self.agent, "# maps\nAsk the owner before merging.\n")
        self.write(os.path.join(self.h, "agents", "prof2", "CLAUDE.md"), "# docs\nShow the owner the text first.\n")
        ev = []
        for i in range(6):   # six cards of one kind, all accepted as proposed
            rid, q = f"r{i}", f"Merge MR !{i}?"
            ev.append({"seq": 10 + 2 * i, "ts": time.time() - 3600, "type": "permission.requested", "entity": "maps",
                       "data": {"request_id": rid, "tool": "AskUserQuestion", "input": {"questions": [
                           {"question": q, "header": "Merge", "options": [{"label": "Merge (Recommended)"}, {"label": "No"}]}]}}})
            ev.append({"seq": 11 + 2 * i, "ts": time.time() - 3500, "type": "permission.answered", "entity": "maps",
                       "data": {"request_id": rid, "answers": {q: "Merge (Recommended)"}}})
        self.write(os.path.join(self.h, "journal.jsonl"),
                   "".join(json.dumps(e, separators=(",", ":")) + "\n" for e in ev))

    @staticmethod
    def write(path, text, mode="w"):
        with open(path, mode, encoding="utf-8") as f:
            f.write(text)

    def read(self, *parts):
        with open(os.path.join(self.h, "stops-audit", *parts), encoding="utf-8") as f:
            return f.read()

    def run_(self, *args):
        env = {**os.environ, "HUB_STATE_DIR": self.h}
        p = subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True, env=env,
                           stdin=subprocess.DEVNULL, timeout=60)
        return p.returncode, p.stdout + p.stderr

    def age(self, days):
        st = json.loads(self.read("state.json"))
        st["audited_at"] -= days * 86400
        self.write(os.path.join(self.h, "stops-audit", "state.json"), json.dumps(st))


class StopsAudit(Hub):
    def test_lifecycle(self):
        rc, out = self.run_()
        self.assertEqual(rc, 3, out)
        self.assertIn("no audit yet", out)
        self.assertIn("--bundle", out)
        self.assertIn("--done", out)
        self.assertFalse(os.path.exists(os.path.join(self.h, "stops-audit", "cards.jsonl")))   # the check read no journal
        self.assertEqual(self.run_("--bundle")[0], 0)
        idx = self.read("bundle", "INDEX.md")
        self.assertIn("maps · full", idx)
        self.assertIn("docs · full", idx)
        self.assertNotIn("- w ", idx)
        self.assertIn("6 cards, 6 of 6", self.read("bundle", "cards-maps.md"))
        rc, out = self.run_("--check")
        self.assertEqual(rc, 3)
        self.assertIn("MISSED", out)   # not marked — reminds again
        self.assertIn("REQUESTED", self.run_("--status")[1])
        self.assertEqual(self.run_("--done", "ok")[0], 0)
        self.assertEqual(self.run_(), (0, ""))   # quiet: nothing in the feed
        self.assertIn("last 0 d ago", self.run_("--status")[1])
        self.age(3)
        self.assertEqual(self.run_()[0], 0)   # 3 days, the file did not grow
        self.write(self.agent, "".join(f"- line {i} " + "x" * 100 + "\n" for i in range(120)), "a")
        rc, out = self.run_()
        self.assertEqual(rc, 3)
        self.assertIn("maps +13 KB", out)   # grew by 10 KB and more
        self.run_("--bundle")
        self.assertIn("maps · delta", self.read("bundle", "INDEX.md"))
        self.assertIn("docs · unchanged", self.read("bundle", "INDEX.md"))
        self.assertEqual(self.run_("--done")[0], 0)
        self.age(8)
        self.assertEqual(self.run_(), (0, ""))   # a week without edits: nothing new to read
        self.write(self.agent, "- one more line\n", "a")
        rc, out = self.run_()
        self.assertEqual(rc, 3)
        self.assertIn("every 7", out)
        self.run_("--bundle")
        self.assertIn("maps · delta", self.read("bundle", "INDEX.md"))
        self.assertIn("docs · unchanged", self.read("bundle", "INDEX.md"))

    def test_done_keeps_the_file_as_it_went_into_the_bundle_and_skip(self):
        self.run_("--bundle")
        self.write(self.agent, "- an edit during the audit, " + "y" * 50 + "\n", "a")   # after the bundle was made
        self.run_("--done")
        self.age(8)
        self.run_("--bundle")
        self.assertIn("maps · delta", self.read("bundle", "INDEX.md"))   # the edit during the audit is in the delta
        self.assertIn("an edit during the audit", self.read("bundle", "delta-maps.md"))
        self.run_("--done", "--skip", "maps")      # not checked — the old snapshot, the delta stays
        self.age(8)
        self.run_("--bundle")
        self.assertIn("maps · delta", self.read("bundle", "INDEX.md"))
        self.assertIn("docs · unchanged", self.read("bundle", "INDEX.md"))

    def test_first_pass_is_full_even_with_leftover_snapshots(self):
        os.makedirs(os.path.join(self.h, "stops-audit", "snap"))
        self.write(os.path.join(self.h, "stops-audit", "snap", "maps.md"), "# maps\n")
        self.run_("--bundle")
        self.assertIn("maps · full", self.read("bundle", "INDEX.md"))

    def test_cards_cache_reads_an_archive_once(self):
        arch = os.path.join(self.h, "journal-archive", "journal-1-9.jsonl")
        shutil.move(os.path.join(self.h, "journal.jsonl"), arch)
        self.run_("--bundle")
        self.assertIn("6 cards", self.read("bundle", "cards-maps.md"))
        self.assertIn("journal-1-9.jsonl", self.read("archives.json"))
        os.chmod(arch, 0)   # read again it would fail: it is not read again
        self.addCleanup(os.chmod, arch, 0o600)
        rc, out = self.run_("--bundle")
        self.assertEqual(rc, 0, out)
        self.assertIn("6 cards", self.read("bundle", "cards-maps.md"))

    def test_done_remembers_only_what_was_in_the_bundle(self):
        self.run_("--done")   # no bundle at all: nothing was read, so nothing is remembered
        self.age(8)
        self.run_("--bundle")
        self.assertIn("maps · full", self.read("bundle", "INDEX.md"))
        sessions = json.load(open(os.path.join(self.h, "state.json")))["state"]["sessions"]
        os.makedirs(os.path.join(self.h, "agents", "sid3"))
        self.write(os.path.join(self.h, "agents", "sid3", "CLAUDE.md"), "# news\nNew agent.\n")
        sessions["news"] = {"role": "agent", "session_id": "sid3", "state": "idle"}   # came after the bundle
        sessions["gone"] = {"role": "agent", "session_id": "sid1", "state": "closed"}  # not running — not audited
        self.write(os.path.join(self.h, "state.json"), json.dumps({"state": {"sessions": sessions}}))
        self.run_("--done")
        self.age(8)
        self.run_("--bundle")
        idx = self.read("bundle", "INDEX.md")
        self.assertIn("news · full", idx)
        self.assertIn("maps · unchanged", idx)
        self.assertNotIn("gone", idx)

    def test_a_quiet_week_wakes_nobody_unless_the_ledger_has_open_entries(self):
        self.run_("--bundle")
        self.run_("--done")
        self.age(8)
        self.assertEqual(self.run_(), (0, ""))   # no file changed, no ledger: nothing new to read
        ledger = os.path.join(self.h, "workspaces", "agent-dev", "review")   # no such session: its own folder
        os.makedirs(ledger)
        self.write(os.path.join(ledger, "stops.md"), "Statuses: asked, temporary, kept by owner\n"
                   "- maps · «Do only what was asked» · conditional · kept by owner 20.10\n")
        self.assertEqual(self.run_()[0], 0)   # the words are in a quote and a legend, not in a status
        self.write(os.path.join(ledger, "stops.md"), "- docs · «Show the owner the text first.» · asked 21.10\n", "a")
        rc, out = self.run_()
        self.assertEqual(rc, 3, out)
        self.assertIn("every 7", out)

    def ledger_due(self, cwd=None, extra=None):
        """Exit code of the check a week after an audit, no file changed, the ledger written to the folder `cwd`
        of the session agent-dev (the state folder knows it; no cwd — the session has no folder of its own)."""
        sess = {"role": "agent", "state": "idle", "session_id": "ad1"}
        if cwd:
            sess["cwd"] = cwd
        with open(os.path.join(self.h, "state.json")) as f:
            st = json.load(f)
        st["state"]["sessions"]["agent-dev"] = sess
        self.write(os.path.join(self.h, "state.json"), json.dumps(st))
        self.run_("--bundle")
        self.run_("--done")
        for d in extra or []:
            os.makedirs(os.path.dirname(d), exist_ok=True)
            self.write(d, "- docs · «Show the owner the text first.» · asked 21.10\n")
        self.age(8)
        return self.run_()[0]

    def test_the_ledger_is_read_from_the_folder_of_the_session_agent_dev(self):
        folder = tempfile.mkdtemp(prefix="agent-dev-")
        self.addCleanup(shutil.rmtree, folder, True)
        self.assertEqual(self.ledger_due(folder, [os.path.join(folder, "review", "stops.md")]), 3)

    def test_a_session_without_a_folder_keeps_the_ledger_in_workspaces(self):
        self.assertEqual(self.ledger_due(None, [os.path.join(self.h, "workspaces", "agent-dev", "review", "stops.md")]), 3)

    def test_a_missing_ledger_is_not_open_and_does_not_fail(self):
        folder = tempfile.mkdtemp(prefix="agent-dev-")
        self.addCleanup(shutil.rmtree, folder, True)
        self.assertEqual(self.ledger_due(folder), 0)

    def test_a_renamed_trainer_is_found_and_a_stale_file_in_the_state_folder_is_not_read(self):
        folder = tempfile.mkdtemp(prefix="agent-dev-")
        self.addCleanup(shutil.rmtree, folder, True)
        with open(os.path.join(self.h, "state.json")) as f:
            st = json.load(f)
        st["state"]["trainer"] = "trainer-x"
        st["state"]["sessions"]["trainer-x"] = {"role": "agent", "state": "idle", "session_id": "t1", "cwd": folder}
        self.write(os.path.join(self.h, "state.json"), json.dumps(st))
        stale = os.path.join(self.h, "agent-dev", "review", "stops.md")
        self.assertEqual(self.ledger_due(None, [stale]), 0)   # the old place is not the ledger any more
        os.makedirs(os.path.join(folder, "review"))
        self.write(os.path.join(folder, "review", "stops.md"), "- docs · «x» · temporary 21.10\n")
        self.assertEqual(self.run_()[0], 3)

    def test_a_symlink_from_the_state_folder_gives_the_same_answer(self):
        folder = tempfile.mkdtemp(prefix="agent-dev-")
        self.addCleanup(shutil.rmtree, folder, True)
        os.makedirs(os.path.join(self.h, "agent-dev"))
        winsafe.symlink(self, os.path.join(folder, "review"), os.path.join(self.h, "agent-dev", "review"))
        self.assertEqual(self.ledger_due(folder, [os.path.join(folder, "review", "stops.md")]), 3)

    def test_a_removed_line_is_a_change_too(self):
        self.run_("--bundle")
        self.run_("--done")
        self.write(self.agent, "# maps\n")   # the stop was deleted: no new lines, but the file changed
        self.age(8)
        self.assertEqual(self.run_()[0], 3)

    def test_a_second_done_without_a_bundle_remembers_nothing(self):
        self.run_("--bundle")
        self.assertIn("agents 2", self.run_("--done")[1])
        self.write(self.agent, "- an edit after the audit\n", "a")
        self.assertIn("agents 0", self.run_("--done")[1])   # the bundle was used up
        self.age(8)
        self.run_("--bundle")
        self.assertIn("maps · delta", self.read("bundle", "INDEX.md"))

    def test_a_week_is_counted_with_the_daily_tick_in_mind(self):
        self.run_("--bundle")
        self.run_("--done")
        self.write(self.agent, "- one more line\n", "a")
        self.age(6.99)   # marked minutes after the tick: the tick seven days later is "a week"
        self.assertEqual(self.run_()[0], 3)

    def test_no_agents_no_audit(self):
        self.write(os.path.join(self.h, "state.json"), json.dumps({"state": {"sessions": {}}}))
        self.assertEqual(self.run_(), (0, ""))

    def test_a_cut_budget_names_all_archives_left(self):
        for i in range(3):
            self.write(os.path.join(self.h, "journal-archive", f"journal-{i}-{i}.jsonl"), "{}\n")
        rc, out = self.run_("--bundle", "--budget", "-1")
        self.assertEqual(rc, 0, out)
        self.assertIn("journal archives not read to the end: 3", out)

    def test_an_error_is_one_line(self):
        os.remove(os.path.join(self.h, "state.json"))
        rc, out = self.run_()
        self.assertEqual(rc, 1)
        self.assertEqual(len(out.strip().splitlines()), 1, out)

    def test_extract_never_calls_the_model_in_tests(self):
        self.run_("--bundle")
        rc, out = self.run_("--extract")
        self.assertEqual(rc, 1)
        self.assertIn("HUB_NO_MODEL", out)

    def fake_claude(self, reply):
        """A `claude` first on PATH: records which login it got (match with the pool account's token, never the value)
        and answers `reply` (a dict -> the --output-format json object)."""
        bin_ = os.path.join(self.h, "bin")
        os.makedirs(bin_)
        py = os.path.join(bin_, "fake.py")
        self.write(py, "import json, os, sys\n" "sys.stdin.read()\n"
                       f"open({os.path.join(self.h, 'login')!r}, 'w').write("
                       "str(os.environ.get('CLAUDE_CODE_OAUTH_TOKEN') == 'pool-token'))\n"
                       f"print(json.dumps({reply!r}))\n")
        if os.name == "nt":
            self.write(os.path.join(bin_, "claude.cmd"), f'@"{sys.executable}" "{py}" %*\r\n')
        else:
            exe = os.path.join(bin_, "claude")
            self.write(exe, f'#!/bin/sh\nexec "{sys.executable}" "{py}" "$@"\n')
            os.chmod(exe, 0o755)
        return bin_

    def read_login(self):
        with open(os.path.join(self.h, "login"), encoding="utf-8") as f:
            return f.read()

    def wrapper_first(self):
        """The session wrapper of claude (hosts/nested, marker HUB_NESTED_FLAGS) ahead of the real one: it fails outside a session."""
        wrap = os.path.join(self.h, "wrap")
        os.makedirs(wrap)
        self.write(os.path.join(wrap, "claude"), "#!/bin/sh\n# HUB_NESTED_FLAGS\nexit 1\n")
        os.chmod(os.path.join(wrap, "claude"), 0o755)
        if os.name == "nt":
            self.write(os.path.join(wrap, "claude.cmd"), "@exit /b 1\r\n")
        return wrap

    def extract(self, reply, wrapper=False, given=False):
        """--extract of the first-pass bundle against a fake claude on the hub's pool account `pool`; `given` — the fake
        only as the core gives it to a session (HUB_REAL_CLAUDE), PATH has nothing but the session wrapper."""
        self.run_("--bundle")
        cfg = os.path.join(self.h, "config")
        os.makedirs(cfg)
        self.write(os.path.join(cfg, "accounts.json"), json.dumps({"accounts": [{"name": "pool", "token": "pool-token"}]}))
        # a snapshot that journal.load_state accepts (the journal position, its first and last seq), the pool account active in it
        path = os.path.join(self.h, "journal.jsonl")
        with open(path, "rb") as f:
            last = json.loads(f.read().splitlines()[-1])["seq"]
        with open(os.path.join(self.h, "state.json"), encoding="utf-8") as f:
            st = json.load(f)
        st["state"]["account"] = {"active": "pool"}
        self.write(os.path.join(self.h, "state.json"),
                   json.dumps({"offset": os.path.getsize(path), "first": journal._first_seq(path), "seq": last, "state": st["state"]}))
        env = {**os.environ, "HUB_STATE_DIR": self.h, "HUB_CONFIG_DIR": cfg, "CLAUDE_CODE_OAUTH_TOKEN": "machine-login",
               "PATH": self.fake_claude(reply) + os.pathsep + os.environ.get("PATH", "")}
        env.pop("HUB_REAL_CLAUDE", None)   # a run from a hub session carries the session's own: the machine's real claude
        if wrapper:
            env["PATH"] = self.wrapper_first() + os.pathsep + env["PATH"]
        if given:
            fake = env["PATH"].split(os.pathsep)[0]
            env["HUB_REAL_CLAUDE"] = os.path.join(fake, "claude.cmd" if os.name == "nt" else "claude")
            env["PATH"] = self.wrapper_first()
        env.pop("HUB_NO_MODEL", None)
        p = subprocess.run([sys.executable, SCRIPT, "--extract"], capture_output=True, text=True, env=env,
                           stdin=subprocess.DEVNULL, timeout=60)
        return p.returncode, p.stdout + p.stderr

    @cut.needs("profile")
    def test_extract_skips_the_session_wrapper_of_claude(self):
        rc, out = self.extract({"result": "[]", "total_cost_usd": 0.01}, wrapper=True)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("MODEL FAILURE", out)   # the wrapper fails outside a session: the real claude was called
        self.assertEqual(self.read_login(), "True")   # and with the pool account's token, not the machine's login

    @cut.needs("profile")
    def test_extract_takes_the_real_claude_the_core_gave_the_session(self):
        rc, out = self.extract({"result": "[]", "total_cost_usd": 0.01}, given=True)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("MODEL FAILURE", out)   # only the wrapper on PATH: before, it was called and failed outside a session
        self.assertEqual(self.read_login(), "True")

    @cut.needs("profile")
    def test_extract_goes_on_the_pool_account(self):
        rc, out = self.extract({"result": "[]", "total_cost_usd": 0.01})
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read_login(), "True")

    def codex_hub(self):
        """Codex (experimental): hub.json "host": "codex" and a fake codex app-server; PATH has no claude."""
        from tests.test_codex_model import SERVER, write_core_hooks
        write_core_hooks(os.path.join(self.h, "codex-home"))   # the core's model runs only where Codex trusts its hook
        conf, codex = os.path.join(self.h, "hub.json"), os.path.join(self.h, "codex")
        self.write(conf, json.dumps({"host": "codex"}))
        # a log per process: the extract runs the agents' chunks at once, and on Windows two processes appending to one
        # file mix their lines (its append is a seek and a write, not atomic)
        own = 'open(os.environ["LOG"] + "." + str(os.getpid()), "a")'
        assert SERVER.count('open(os.environ["LOG"], "a")') == 1
        server = SERVER.replace('open(os.environ["LOG"], "a")', own)
        codex = exe.script(codex, server)   # Windows: codex.cmd beside codex.py — a shebang file does not start there
        empty = os.path.join(self.h, "bin")
        os.makedirs(empty)
        return {"HUB_CONFIG": conf, "HUB_CODEX_BIN": codex, "LOG": os.path.join(self.h, "codex.log"), "PATH": empty}

    def test_extract_on_a_codex_hub_goes_to_codex(self):
        self.run_("--bundle")
        env = {**os.environ, "HUB_STATE_DIR": self.h, **self.codex_hub(), "HUB_NO_MODEL": "",
               "ANSWER": json.dumps([{"line": 2, "quote": "Ask the owner before merging.", "class": "conditional"}])}
        p = subprocess.run([sys.executable, SCRIPT, "--extract"], capture_output=True, text=True, env=env,
                           stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        stops = json.loads(self.read("bundle", "stops.json"))
        self.assertEqual(sorted(s["agent"] for s in stops), ["docs", "maps"])   # one chunk per agent, both answered
        self.assertIn("Codex", self.read("bundle", "stops.md"))
        log = []
        for name in glob.glob(glob.escape(env["LOG"]) + ".*"):
            with open(name, encoding="utf-8") as f:
                log += [json.loads(x) for x in f]
        self.assertEqual({x["env"]["CODEX_HOME"] for x in log if "env" in x}, {os.path.join(self.h, "codex-home")})
        start = next(m for m in log if m.get("method") == "thread/start")["params"]
        self.assertIn("stops", start["baseInstructions"])                       # the prompt of stops-extract.md
        self.assertEqual(start["model"], "gpt-6-luna")                            # haiku is the core's cheap model

    def test_extract_on_a_codex_hub_never_calls_the_model_in_tests(self):
        self.run_("--bundle")
        env = {**os.environ, "HUB_STATE_DIR": self.h, **self.codex_hub()}
        p = subprocess.run([sys.executable, SCRIPT, "--extract"], capture_output=True, text=True, env=env,
                           stdin=subprocess.DEVNULL, timeout=60)
        self.assertIn("HUB_NO_MODEL", p.stdout + p.stderr)
        self.assertEqual(glob.glob(glob.escape(env["LOG"]) + ".*"), [])

    def test_the_prompt_lies_next_to_the_script(self):
        with open(os.path.join(ROOT, "modules", "agent_dev", "stops-extract.md"), encoding="utf-8") as f:
            self.assertIn("JSON array", f.read())

class ExtractFailures(Hub):
    """--extract: a model failure is not "0 stops". The call is replaced (`run`), the live model is never called."""

    def load(self):
        spec = importlib.util.spec_from_file_location("stops_audit_x", SCRIPT)
        sa = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sa)
        return sa

    def extract(self, replies, *args):
        """main(["--extract", *args]) with run answering from `replies(agent_text)`; -> (rc, stdout, stderr, calls, envs)."""
        import contextlib
        import io
        from unittest import mock
        sa = self.load()
        calls, envs = [], []

        def run(cmd, input=None, env=None, **kw):
            calls.append(input)
            envs.append(env)
            rc, out = replies(input)
            return subprocess.CompletedProcess(cmd, rc, out, "")
        env = {k: v for k, v in os.environ.items() if k != "HUB_NO_MODEL"}
        env["HUB_STATE_DIR"] = self.h
        o, e = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sa, "model_env", lambda: {"PATH": "/x", "POOL": "1"}, create=True), \
                mock.patch.object(sa, "run_deadline", run), \
                contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
            rc = sa.main(["--extract", *args])
        return rc, o.getvalue(), e.getvalue(), calls, envs

    @staticmethod
    def ok(found="[]", cost=0.01):
        return 0, json.dumps({"result": found, "total_cost_usd": cost, "is_error": False})

    def state(self):
        return json.loads(self.read("state.json"))

    def setUp(self):
        super().setUp()
        self.assertEqual(self.run_("--bundle")[0], 0)

    def test_a_refused_sign_in_is_a_failure_not_zero_stops(self):
        # the real case: claude -p on an expired login exits 1, its JSON is valid (is_error, no result array, price 0)
        refused = (1, json.dumps({"is_error": True, "total_cost_usd": 0, "result": "Failed to authenticate: OAuth session expired"}))
        rc, out, err, calls, _ = self.extract(lambda text: refused)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("MODEL FAILURE", err)
        self.assertIn("OAuth session expired", err)
        self.assertEqual(set(self.state()["extract_failed"]), {"maps", "docs"})
        self.assertIn("NOT PARSED maps", self.read("bundle", "stops.md"))

    def test_a_call_past_its_deadline_fails_the_chunk_with_the_reason_in_short(self):
        # str(TimeoutExpired) starts with the whole command line: cut at 200 characters, it hid that time ran out
        def late(text):
            raise subprocess.TimeoutExpired(["/home/x/claude", "-p", "--model", "haiku", *["--flag"] * 40], 900)
        rc, out, err, *_ = self.extract(late)
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(set(self.state()["extract_failed"]), {"maps", "docs"})
        self.assertIn("no answer in 900 s", err)

    def test_every_kind_of_a_bad_answer_fails_the_chunk(self):
        bad = {"exit code": (2, json.dumps({"result": "[]", "total_cost_usd": 0.01})),
               "not json": (0, "Rate limit reached"),
               "no array": (0, json.dumps({"result": "I cannot help.", "total_cost_usd": 0.01})),
               "broken array": (0, json.dumps({"result": "[{\"line\": ", "total_cost_usd": 0.01})),
               "price 0 with an empty answer": self.ok("[]", 0),
               "is_error with exit 0": (0, json.dumps({"is_error": True, "result": "[]", "total_cost_usd": 0.01}))}
        for name, reply in bad.items():
            with self.subTest(name):
                rc, out, err, *_ = self.extract(lambda text, reply=reply: reply)
                self.assertEqual(rc, 1, out + err)
                self.assertEqual(set(self.state()["extract_failed"]), {"maps", "docs"})

    def test_an_empty_array_with_a_price_is_zero_stops_and_succeeds(self):
        rc, out, err, *_ = self.extract(lambda text: self.ok("[]", 0.004))
        self.assertEqual(rc, 0, out + err)
        self.assertIn("stops extracted 0, price $0.008", out)
        self.assertNotIn("extract_failed", self.state())

    def test_the_call_goes_on_the_pool_environment(self):
        _, _, _, calls, envs = self.extract(lambda text: self.ok())
        self.assertTrue(envs and all(e == {"PATH": "/x", "POOL": "1"} for e in envs), envs)

    def test_the_next_extract_takes_only_the_failed_agents_and_keeps_the_rest(self):
        stop = json.dumps([{"line": 2, "quote": "Ask the owner before merging.", "class": "unconditional"}])

        def first(text):
            return self.ok(stop) if "merging" in text else (1, json.dumps({"is_error": True, "result": "limit", "total_cost_usd": 0}))
        rc, out, err, calls, _ = self.extract(first)
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(list(self.state()["extract_failed"]), ["docs"])
        self.assertEqual([x["agent"] for x in json.loads(self.read("bundle", "stops.json"))], ["maps"])
        # the second run: only docs is asked, maps keeps its stop
        rc, out, err, calls, _ = self.extract(lambda text: self.ok("[]"))
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(len(calls), 1)
        self.assertIn("Show the owner", calls[0])
        self.assertNotIn("extract_failed", self.state())
        self.assertEqual([x["agent"] for x in json.loads(self.read("bundle", "stops.json"))], ["maps"])

    def test_done_refuses_an_agent_the_model_did_not_read(self):
        self.extract(lambda text: (1, json.dumps({"is_error": True, "result": "limit", "total_cost_usd": 0})))
        rc, out = self.run_("--done", "summary")
        self.assertEqual(rc, 1, out)
        self.assertIn("--extract did not parse", out)
        self.assertNotIn("files", self.state())   # nothing was marked
        rc, out = self.run_("--done", "summary", "--skip", "maps", "docs")
        self.assertEqual(rc, 0, out)

    def test_a_new_bundle_forgets_the_failures_of_the_old_one(self):
        self.extract(lambda text: (1, json.dumps({"is_error": True, "result": "limit", "total_cost_usd": 0})))
        self.assertEqual(self.run_("--bundle")[0], 0)
        self.assertNotIn("extract_failed", self.state())

    def test_a_run_on_one_agent_keeps_the_failure_of_the_other(self):
        down = (1, json.dumps({"is_error": True, "result": "limit", "total_cost_usd": 0}))
        self.extract(lambda text: down)
        self.assertEqual(set(self.state()["extract_failed"]), {"maps", "docs"})
        # --only maps succeeds: maps leaves the failures, docs stays (and --done still refuses it)
        rc, out, err, calls, _ = self.extract(lambda text: self.ok("[]"), "--only", "maps")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(len(calls), 1)
        self.assertEqual(set(self.state()["extract_failed"]), {"docs"})
        self.assertIn("NOT PARSED docs", self.read("bundle", "stops.md"))
        rc, out = self.run_("--done", "summary")
        self.assertEqual(rc, 1, out)
        self.assertIn("docs", out)
        # --only maps fails again: both are in the failures
        rc, out, err, *_ = self.extract(lambda text: down, "--only", "maps")
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(set(self.state()["extract_failed"]), {"maps", "docs"})

    def test_any_exception_of_one_chunk_is_a_failure_of_that_agent_not_a_crash(self):
        def run(text):
            if "merging" in text:
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad byte")   # not OSError/ValueError-of-json/Timeout
            return self.ok("[]")
        rc, out, err, calls, _ = self.extract(run)
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(len(calls), 2)
        self.assertEqual(list(self.state()["extract_failed"]), ["maps"])
        self.assertIn("UnicodeDecodeError", self.state()["extract_failed"]["maps"])

    def test_nothing_to_extract_needs_no_login(self):
        # no agent to ask about: no call goes out, so a missing login is not a failure
        import contextlib
        import io
        from unittest import mock
        sa = self.load()
        env = {k: v for k, v in os.environ.items() if k != "HUB_NO_MODEL"}
        env["HUB_STATE_DIR"] = self.h
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sa, "model_env", side_effect=RuntimeError("no login"), create=True), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            rc = sa.main(["--extract", "--only", "no-such-agent"])
        self.assertEqual(rc, 0, out.getvalue())


class Deadline(unittest.TestCase):
    """The model call of --extract ends at its deadline even when a grandchild keeps the pipes open."""

    def test_a_stray_grandchild_holding_the_pipes_does_not_hang_the_run(self):
        # before: subprocess.run (Windows) read the pipes after the kill with no limit, and the grandchild lived on;
        # a hang here is caught by the run's own limit, so no seconds are compared: the grandchild is stopped too. 8 s:
        # two interpreters start and the child writes the grandchild's number well before the deadline
        spec = importlib.util.spec_from_file_location("stops_audit_d", SCRIPT)
        sa = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sa)
        import platforms
        d = tempfile.mkdtemp(prefix="stops-")
        self.addCleanup(platforms.current.rmtree, d)
        pidf = os.path.join(d, "grandchild.pid")
        child = ("import subprocess,sys,time; g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                 "open(sys.argv[1], 'w').write(str(g.pid)); time.sleep(60)")
        with self.assertRaises(subprocess.TimeoutExpired):
            sa.run_deadline([sys.executable, "-c", child, pidf], input="x", timeout=8, capture_output=True, text=True,
                            encoding="utf-8", cwd=d)
        with open(pidf, encoding="utf-8") as f:
            pid = int(f.read())
        for _ in range(100):   # the kill is sent before run_deadline returns; this waits only for the reaping
            if not platforms.current.alive(pid):
                break
            time.sleep(0.1)
        self.assertFalse(platforms.current.alive(pid), "the grandchild outlived the deadline")

    def test_a_normal_run_returns_its_output(self):
        spec = importlib.util.spec_from_file_location("stops_audit_d2", SCRIPT)
        sa = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sa)
        r = sa.run_deadline([sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"], input="ab", timeout=30,
                            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "AB"))


class Sift(unittest.TestCase):
    """The second pass of --extract: what the book already holds and what does not wait for the owner is not given again."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("stops_audit", SCRIPT)
        cls.sa = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.sa)

    BOOK = ("# book\n\n"
            "- ops · «Deploys to production go out only after the owner says yes to this exact image.» · unconditional · unconditional\n"
            "- ops · «Never delete a customer database snapshot without asking the owner first.» · unconditional · unconditional\n"
            "- docs · «Show the owner the final text before it is sent to a customer.» · conditional · candidate\n")

    def sift(self, found, book=BOOK):
        return self.sa.sift(found, self.sa.book_quotes(book))

    @staticmethod
    def stop(agent, quote, **kw):
        return {"agent": agent, "line": 1, "quote": quote, "class": "unconditional", **kw}

    def test_a_line_the_book_holds_is_dropped_and_counted(self):
        again = self.stop("ops", "Deploys to production go out only after the owner says yes to this exact image")
        new = self.stop("ops", "Rotating the signing keys needs a yes from the owner on the date")
        rest, known, other = self.sift([again, new])
        self.assertEqual(rest, [new])
        self.assertEqual((known, other), (1, 0))

    def test_a_line_with_six_tenths_of_the_words_stays(self):
        q = self.stop("ops", "Never delete the volume backup, owner")
        mine, held = self.sa.stems(q["quote"]), self.sa.stems("Never delete a customer database snapshot without asking the owner first.")
        self.assertEqual((len(mine & held), len(mine)), (3, 5))   # 0.6 of the words are in the book entry: below 0.7
        rest, known, _ = self.sift([q])
        self.assertEqual((rest, known), ([q], 0))

    def test_the_book_of_another_agent_does_not_hide_a_line(self):
        rest, known, _ = self.sift([self.stop("docs", "Deploys to production go out only after the owner says yes to this exact image")])
        self.assertEqual(known, 0)
        self.assertEqual(len(rest), 1)

    def test_a_quote_of_two_words_is_never_called_known(self):
        rest, known, _ = self.sift([self.stop("ops", "owner says yes")])
        self.assertEqual((len(rest), known), (1, 0))

    def test_a_threshold_that_waits_for_nobody_is_dropped(self):
        thr = self.stop("ops", "STOP nvme: write latency above 2.5 ms pauses the heavy jobs", waits_owner=False)
        real = self.stop("ops", "Rotating the signing keys needs a yes from the owner on the date", waits_owner=True)
        legacy = self.stop("ops", "Moving the zone to another registrar needs a yes from the owner")   # no field: keep
        rest, known, other = self.sift([thr, real, legacy])
        self.assertEqual(rest, [real, legacy])
        self.assertEqual((known, other), (0, 1))

    def test_no_book_means_no_comparison(self):
        again = self.stop("ops", "Deploys to production go out only after the owner says yes to this exact image")
        rest, known, _ = self.sa.sift([again], self.sa.book_quotes(None))
        self.assertEqual((rest, known), ([again], 0))

    def test_the_book_reads_quotes_with_guillemets_inside(self):
        book = self.sa.book_quotes("- ops · «Only on «yes» from the owner, then merge.» · unconditional · kept\n"
                                   "# comment\n- broken line\n")
        self.assertEqual([(a, q) for a, q, _ in book], [("ops", "Only on «yes» from the owner, then merge.")])


if __name__ == "__main__":
    unittest.main()
