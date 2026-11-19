"""Codex (experimental): tools/codex_compat.py — the check of Codex CLI versions without a model. The live part
needs a real codex and is run by hand before a release of the Codex host; here — its fake model, the binary search in an
npm install and the schema check."""
import tests  # noqa: F401 — do not see the machine's settings (~/.config/hub-core), see tests/__init__.py
import json
import os
import tempfile
import unittest
import urllib.request

from tools import codex_compat


class StateSeen(unittest.TestCase):
    def test_a_windows_path_as_repr_shows_it(self):
        # the server answers with repr of HUB_STATE_DIR: on Windows the backslashes are doubled (winstand, 2026-11-05)
        state = "C:\\x\\state"
        self.assertTrue(codex_compat.state_seen("echo state=" + repr(state)[1:-1], state))
        self.assertTrue(codex_compat.state_seen("state=/home/x/state ok", "/home/x/state"))
        self.assertFalse(codex_compat.state_seen("state=None", state))


class Robust(unittest.TestCase):
    """A version that hangs or has no `features list` is its own row: the table goes on (review high, 2026-11-05)."""

    def test_a_version_that_hangs_is_a_row(self):
        import subprocess
        from unittest import mock

        def hang(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        with tempfile.TemporaryDirectory() as root, mock.patch.object(codex_compat.subprocess, "run", hang):
            row = codex_compat.check("/x/codex", root, 5)
        self.assertFalse(row["ok"])
        self.assertIn("did not answer within 60 s", row["problems"][0])

    def test_what_was_found_before_a_hang_stays(self):
        import subprocess
        from unittest import mock

        def hang(*a, **kw):
            raise subprocess.TimeoutExpired(["x"], 180)
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(codex_compat, "version_of", lambda b, r: "0.1"), \
                mock.patch.object(codex_compat, "schema_problems", lambda b, r: ["no TurnStartParams.additionalContext"]), \
                mock.patch.object(codex_compat, "feature_problems", lambda b, r: []), \
                mock.patch.object(codex_compat, "live", hang):
            row = codex_compat.check("/x/codex", root, 120)
        self.assertEqual(len(row["problems"]), 2)
        self.assertIn("additionalContext", row["problems"][0])

    def test_an_npm_install_that_hangs_is_a_row_and_the_folder_goes(self):
        import subprocess
        from unittest import mock

        def run(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        roots = []
        real = codex_compat.readable_folder

        def folder(*a, **kw):
            roots.append(real(*a, **kw))
            return roots[-1]
        with mock.patch.object(codex_compat.subprocess, "run", run), \
                mock.patch.object(codex_compat.shutil, "which", lambda name: "/x/" + name if name == "npm" else None), \
                mock.patch.object(codex_compat, "readable_folder", folder), \
                mock.patch("hosts.codex.codex_bin", lambda: "/nonexistent/codex"):
            rc = codex_compat.main(["--versions", "9.9.9", "--json"])
        self.assertEqual(rc, 3)
        self.assertFalse(os.path.exists(roots[0]))

    def test_no_features_list_is_one_problem_not_every_flag(self):
        import subprocess
        from unittest import mock
        done = subprocess.CompletedProcess([], 2, stdout="", stderr="error: unrecognized subcommand 'features'")
        with tempfile.TemporaryDirectory() as run, mock.patch.object(codex_compat.subprocess, "run", lambda *a, **k: done):
            got = codex_compat.feature_problems("/x/codex", run)
        self.assertEqual(len(got), 1)
        self.assertIn("unrecognized subcommand", got[0])


class FakeModel(unittest.TestCase):
    def setUp(self):
        self.api = codex_compat.FakeApi()
        self.addCleanup(self.api.stop)

    def ask(self, body):
        req = urllib.request.Request(self.api.url + "/responses", data=json.dumps(body).encode(),
                                     headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            events = [json.loads(line[6:]) for line in r.read().decode().splitlines() if line.startswith("data: ")]
        return [e for e in events if e["type"] == "response.output_item.done"][0]["item"]

    @staticmethod
    def user(text):
        return {"role": "user", "type": "message", "content": [{"type": "input_text", "text": text}]}

    def test_plain_call_and_its_output(self):
        self.assertEqual(self.ask({"input": [self.user("hi")]})["content"][0]["text"], "ok")
        call = self.ask({"input": [self.user('CALL mcp__x.echo {"a": 1}')]})
        self.assertEqual((call["type"], call["namespace"], call["name"], json.loads(call["arguments"])),
                         ("function_call", "mcp__x", "echo", {"a": 1}))
        plain = self.ask({"input": [self.user("CALL exec_command")]})
        self.assertEqual((plain["name"], plain.get("namespace"), plain["arguments"]), ("exec_command", None, "{}"))
        out = self.ask({"input": [self.user("CALL x {}"), {"type": "function_call_output", "output": "res"}]})
        self.assertEqual(out["content"][0]["text"], "done: res")
        self.assertEqual(len(self.api.bodies), 4)   # what the "model" saw is kept

    def test_chained_steps_one_per_tool_output(self):
        chain = self.user('[hub:m1] CALL exec_command {"cmd": "a"} ;; CALL exec_command {"cmd": "b"}')
        first = self.ask({"input": [chain]})
        self.assertEqual(json.loads(first["arguments"]), {"cmd": "a"})
        out = {"type": "function_call_output", "output": "x"}
        self.assertEqual(json.loads(self.ask({"input": [chain, out]})["arguments"]), {"cmd": "b"})
        last = self.ask({"input": [chain, out, {"type": "function_call_output", "output": "y"}]})
        self.assertEqual(last["content"][0]["text"], "done: y")


    def test_a_custom_call_gives_the_patch_as_its_input(self):
        call = self.ask({"input": [self.user("CUSTOM apply_patch *** Begin Patch\n*** Add File: a\n+x\n*** End Patch")]})
        self.assertEqual((call["type"], call["name"], call["input"]),
                         ("custom_tool_call", "apply_patch", "*** Begin Patch\n*** Add File: a\n+x\n*** End Patch\n"))
        tools = [{"type": "function", "name": "apply_patch", "parameters": {}}]
        call = self.ask({"input": [self.user("CUSTOM apply_patch *** Begin Patch")], "tools": tools})
        self.assertEqual((call["type"], json.loads(call["arguments"])), ("function_call", {"input": "*** Begin Patch\n"}))
        out = self.ask({"input": [self.user("CUSTOM apply_patch x"), {"type": "custom_tool_call_output", "output": "r"}]})
        self.assertEqual(out["content"][0]["text"], "done: r")


class ApplyPatchForm(unittest.TestCase):
    """The row is red when a version gives a repo's hooks apply_patch in another form than the one hooks/edit_hooks.py
    reads — before every patch in such a repo is refused."""
    RAW = {"tool_name": "apply_patch", "tool_input": {"command": "*** Begin Patch\n*** Add File: allowed.txt\n+ok\n"}}
    CLAUDE = {"tool_name": "Write", "tool_input": {"file_path": "/home/x/w/allowed.txt", "content": "ok\n"}}

    def test_codexs_form_and_claudes_calls_are_ok(self):
        self.assertEqual(codex_compat.patch_form([self.RAW], [self.CLAUDE]), [])

    def test_another_form_is_named(self):
        for raw in ({**self.RAW, "tool_name": "ApplyPatch"}, {**self.RAW, "tool_input": {"patch": "*** Add File: allowed.txt"}},
                    {**self.RAW, "tool_input": "*** Add File: allowed.txt"}):
            (got,) = codex_compat.patch_form([raw], [self.CLAUDE])
            self.assertTrue(got.startswith("apply_patch form: the hook got"), got)

    def test_no_hook_call_no_claude_call_no_tool(self):
        self.assertIn("was not called", codex_compat.patch_form([], [self.CLAUDE])[0])
        self.assertIn("did not get Write", codex_compat.patch_form([self.RAW], [])[0])
        self.assertIn("did not get Write", codex_compat.patch_form(
            [self.RAW], [{**self.CLAUDE, "tool_input": {"file_path": "/home/x/w/allowed.txt"}}])[0])
        self.assertIn("no apply_patch", codex_compat.patch_form([], [], offered=False)[0])


class Install(unittest.TestCase):
    def test_the_native_binary_not_the_node_launcher(self):
        prefix = tempfile.mkdtemp()
        launcher = os.path.join(prefix, "node_modules", ".bin")
        exe = os.path.join(prefix, "node_modules", "@openai", "codex-linux-x64", "vendor", "x86_64-unknown-linux-musl",
                           "bin")
        for d in (launcher, exe):
            os.makedirs(d)
        for f in (os.path.join(launcher, "codex"), os.path.join(exe, "codex"), os.path.join(exe, "codex-code-mode-host")):
            open(f, "w").close()
        self.assertEqual(codex_compat.native(prefix), os.path.join(exe, "codex"))
        self.assertIsNone(codex_compat.native(tempfile.mkdtemp()))


class Schema(unittest.TestCase):
    def test_a_missing_method_or_field_is_named(self):
        methods, params = codex_compat.host_calls()
        out = tempfile.mkdtemp()
        exp = write_schema(out, methods, params, drop=("turn/steer",))
        for folder in (out, exp):   # a field the host reads, an input kind it sends
            with open(os.path.join(folder, "ClientRequest.json"), encoding="utf-8") as f:
                req = json.load(f)
            del req["definitions"]["TurnStartParams"]["properties"]["effort"]
            req["definitions"]["UserInput"]["oneOf"] = [u for u in req["definitions"]["UserInput"]["oneOf"]
                                                        if u["properties"]["type"]["enum"] != ["skill"]]
            with open(os.path.join(folder, "ClientRequest.json"), "w", encoding="utf-8") as f:
                json.dump(req, f)
        self.assertEqual(sorted(codex_compat.check_schema(out, experimental=exp)),
                         ["no TurnStartParams.effort", "no UserInput {type: skill}", "no turn/steer"])

    def test_no_experimental_folder_at_all_is_named(self):
        methods, params = codex_compat.host_calls()
        out = tempfile.mkdtemp()
        write_schema(out, methods, params)
        self.assertIn("no ClientRequest.json (--experimental)", codex_compat.check_schema(out, experimental=tempfile.mkdtemp()))


class HostCalls(unittest.TestCase):
    """What the host sends is read from its code (hosts/codex*.py, core/codex*.py), not kept by hand: a slice that adds
    a request, a parameter or a value is checked against every version without touching this tool."""

    def test_methods_keys_and_values_come_from_the_code(self):
        methods, params = codex_compat.host_calls()
        for m in ("thread/inject_items", "thread/turns/list", "thread/backgroundTerminals/terminate", "skills/list",
                  "mcpServerStatus/list", "mcpServer/oauth/login", "config/mcpServer/reload", "turn/plan/updated",
                  "item/permissions/requestApproval", "mcpServer/elicitation/request", "hooks/list"):
            self.assertIn(m, methods)
        self.assertFalse([m for m in methods if m.startswith("hub/")])   # the host's posts to itself
        self.assertEqual(set(params["thread/backgroundTerminals/terminate"]), {"threadId", "processId"})
        self.assertEqual(params["thread/turns/list"]["itemsView"], {"full"})
        self.assertEqual(params["thread/turns/list"]["sortDirection"], {"desc"})
        self.assertIn("additionalContext", params["turn/start"])   # a conditional spread: **({…} if extra else {})
        # the core's model: the key is chosen by an expression, both are sent
        self.assertTrue({"baseInstructions", "developerInstructions", "ephemeral"} <= set(params["thread/start"]))
        self.assertIn("read-only", params["thread/start"]["sandbox"])
        self.assertIn("never", params["thread/start"]["approvalPolicy"])
        # a session's mode is set in a step (hosts/codex_rules.MODES), not at the call
        self.assertTrue({"untrusted", "on-request"} <= params["thread/resume"]["approvalPolicy"])
        self.assertIn("workspace-write", params["thread/start"]["sandbox"])
        self.assertEqual(params["account/login/start"]["type"], {"chatgptDeviceCode"})
        self.assertIn("threadId", params["thread/resume"])   # {**params, "threadId": …}
        self.assertTrue({"initialize", "error"} <= methods)   # no "/" in the name, checked all the same

    def test_a_new_call_in_a_host_file_is_checked(self):
        repo = tempfile.mkdtemp()
        os.makedirs(os.path.join(repo, "hosts"))
        os.makedirs(os.path.join(repo, "core"))
        with open(os.path.join(repo, "hosts", "codex_new.py"), "w") as f:
            f.write('def go(proc):\n    proc.call("thread/brandNew", {"threadId": proc.thread, "mode": "fast"})\n'
                    '    if method == "thread/somethingHappened":\n        pass\n')
        with open(os.path.join(repo, "core", "codex_other.py"), "w") as f:   # a dict of the same name elsewhere
            f.write('def a(proc):\n    params = {"threadId": 1}\n    proc.call("thread/other", params)\n\n\n'
                    'def b():\n    params = {"foo": 2}\n\n    def inner():\n        params = {"bar": 3}\n\n\n'
                    'def c(proc):\n    params = {"cwd": 1}\n\n    def later():   # a closure sees the enclosing name\n'
                    '        proc.call("thread/closure", params)\n    proc.on(lambda: proc.call("thread/lambda", {"x": "y"}))\n')
        methods, params = codex_compat.host_calls(repo)
        self.assertEqual(params.pop("thread/other"), {"threadId": set()})
        self.assertEqual(params.pop("thread/closure"), {"cwd": set()})
        self.assertEqual(params.pop("thread/lambda"), {"x": {"y"}})
        self.assertEqual(methods, {"thread/brandNew", "thread/somethingHappened", "thread/other", "thread/closure",
                                   "thread/lambda", "error"})
        self.assertEqual(params, {"thread/brandNew": {"threadId": set(), "mode": {"fast"}}})


class HookTrust(unittest.TestCase):
    """A thread's hooks run only with the trusted_hash Codex computes itself: a key or hash of the core's that differs
    makes the version incompatible (the hooks would be skipped without a word)."""

    def test_a_different_hash_or_key_is_named(self):
        got = {"/<session-flags>/config.toml:stop:0:0": "sha256:a", "/<session-flags>/config.toml:pre_tool_use:0:0": "sha256:b"}
        self.assertEqual(codex_compat.compare_hooks(got, dict(got)), [])
        mine = {"/<session-flags>/config.toml:stop:0:0": "sha256:x", "C:\\<session-flags>:pre_tool_use:0:0": "sha256:b",
                "/<session-flags>/config.toml:stop:0:1": "sha256:c"}
        got_problems = codex_compat.compare_hooks(got, mine)
        self.assertEqual(len(got_problems), 3, got_problems)
        # the prefix differs (Windows): both keys named, the hash still compared (equal here)
        self.assertIn("hook key: Codex /<session-flags>/config.toml:pre_tool_use:0:0, core C:\\<session-flags>",
                      got_problems[0])
        self.assertIn("Codex computes sha256:a, core sha256:x", got_problems[1])
        self.assertIn("the core expects it", got_problems[2])

    def test_a_core_without_thread_hooks_compares_nothing(self):
        from unittest import mock
        from hosts import codex_set
        with mock.patch.object(codex_set, "hook_trust", None, create=True), \
                mock.patch.object(codex_compat.subprocess, "Popen", side_effect=AssertionError("no codex run")):
            self.assertEqual(codex_compat.hook_problems("/x/codex", tempfile.mkdtemp()), [])


class TrustKeyInTomlForm(unittest.TestCase):
    """The core writes the hook's trust key as a TOML string (hosts/codex_rules: "\\" escaped) — real Codex takes it
    (codex.exe 0.162.1 on Windows, 2026-11-05); the fake looked for the raw path and called the hook untrusted on
    Windows: every Codex session of the tests failed there. A folder with "\\" in its name is the same on POSIX."""

    @unittest.skipIf(os.name == "nt", "a backslash is a separator there: the Windows run itself covers it")
    def test_a_key_with_a_backslash_is_trusted(self):
        import sys
        from hosts import codex_rules
        from tests.test_codex_rules import CONFIG, FAKE_CODEX
        state = os.path.join(tempfile.mkdtemp(prefix="hubcxk-"), "a\\b")
        home = os.path.join(state, "codex-home")
        os.makedirs(home)
        with open(os.path.join(home, "config.toml"), "w") as f:
            f.write(CONFIG)
        codex_rules.prepare_home(home, codex=(sys.executable, FAKE_CODEX), env={**os.environ, "CODEX_HOME": home})
        self.assertTrue(codex_rules.trusted(state))


class Pins(unittest.TestCase):
    """Every app-server outside a session's — the core's model, the hook's trust check, the sign-in and limits — carries
    the isolation pins after the subcommand (hosts/codex_set.args): a trusted folder whose .codex/config.toml turned
    multi_agent on gave the core's model subagents (live, codex 0.155.1, 0.162.1, 0.166.0 with a fake API, 2026-11-05)."""

    def test_every_app_server_outside_the_host_is_pinned(self):
        from unittest import mock
        from core import codexcli, codexmodel
        from hosts import codex_rules, codex_set
        cmds = []

        def popen(cmd, *a, **kw):
            cmds.append(list(cmd))
            raise OSError("no codex here")
        home = tempfile.mkdtemp()
        with mock.patch("subprocess.Popen", popen), mock.patch("hosts.codex.codex_bin", lambda: "/x/codex"):
            with self.assertRaises(codexmodel.Off):
                codexmodel.run(["/x/codex"], "q", "p", None, "m", "low", 5, {})
            self.assertIsNone(codex_rules._hooks_list(["/x/codex"], {}, home))
            with self.assertRaises(OSError):
                codexcli.Rpc(tempfile.mkdtemp(), home=home)
            from core import cutout
            if cutout.present("speech"):   # the voice reply's realtime thread (the open flavor has no built-in voice)
                from modules.voice import speak
                with self.assertRaises(OSError):
                    speak.Rpc("/x/codex")
        pins = codex_set.args()
        self.assertIn("features.multi_agent=false", pins)
        self.assertEqual([c[1:2 + len(pins)] for c in cmds], [["app-server", *pins]] * (4 if cutout.present("speech") else 3))


class RollbackToClaude(unittest.TestCase):
    """A hub goes back from Codex to Claude Code (`"host"` removed): a session whose conversation is only a Codex thread
    is started afresh with the same --session-id, not `--resume`d — Claude Code has no such conversation and every
    start failed with "No conversation found" (stand 2026-11-05: the hub crash-looped, agent-dev on its first message)."""

    class Host:
        def __init__(self):
            self.spawns = []

        def spawn(self, name, **kw):
            self.spawns.append(kw)
            return "spawned"

    def resume_of(self, host, threads=None, transcript=False):
        from core import daemon
        state, claude = tempfile.mkdtemp(), tempfile.mkdtemp()
        if threads is not None:
            os.makedirs(os.path.join(state, "sessions", "s"))
            with open(os.path.join(state, "sessions", "s", "codex-threads.json"), "w") as f:
                json.dump(threads, f)
        if transcript:   # in another project folder than the session's: Claude Code finds it there too (carry_transcript)
            os.makedirs(os.path.join(claude, "projects", "-elsewhere"))
            open(os.path.join(claude, "projects", "-elsewhere", "sid.jsonl"), "w").close()
        h = daemon.Hub(state, host, registry_dir=state)
        h.wait_claude = lambda a: a
        for wait in ("wait_release", "wait_modules", "wait_private", "wait_gateway", "wait_codex_login", "wait_login"):
            setattr(h, wait, lambda a: False)
        from unittest import mock
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": claude}):
            h.execute({"do": "spawn", "session": "s", "session_id": "sid", "resume": True, "cwd": state})
        return host.spawns[-1]["resume"]

    def test_a_conversation_only_in_codex_starts_afresh_on_claude(self):
        self.assertIs(self.resume_of(self.Host(), threads={"sid": "thread-1"}), False)

    def test_a_claude_conversation_is_resumed_as_before(self):
        self.assertIs(self.resume_of(self.Host()), True)                                   # a Claude hub that never had Codex
        self.assertIs(self.resume_of(self.Host(), threads={"other": "thread-1"}), True)    # another conversation was Codex's
        self.assertIs(self.resume_of(self.Host(), threads={"sid": "t"}, transcript=True), True)   # Claude's before Codex

    def test_the_codex_host_decides_its_own(self):
        class Codex(self.Host):   # a host of Codex sessions answers for its threads (hosts/codex.CodexHost.resumable)
            def resumable(self, name, sess):
                return False
        self.assertIs(self.resume_of(Codex(), threads={"sid": "thread-1"}), True)


def write_schema(out, methods, params, drop=(), exp_only=()):
    """A schema folder that has what the host sends (methods with parameters are requests, the rest notifications),
    minus `drop` ("m" or "m.key"); `exp_only` — moved to the --experimental folder (returned)."""
    exp = tempfile.mkdtemp()
    stable_defs, exp_defs, stable_req, exp_req, notes = {}, {}, [], [], []
    for k, v in codex_compat.FIELDS.items():
        stable_defs[k] = {"properties": {f: {} for f in v}}
    for k, v in codex_compat.ANSWERS.items():
        stable_defs[k] = {"properties": {f: ({"enum": list(vals)} if vals else {}) for f, vals in v.items()}}
    stable_defs["UserInput"] = {"oneOf": [{"properties": {"type": {"enum": [k]}}} for k in codex_compat.USER_INPUT]}
    for m in sorted(methods):
        if m in drop:
            continue
        if m not in params:
            notes.append({"properties": {"method": {"enum": [m]}}})
            continue
        name = "P" + m.replace("/", "_")
        entry = {"properties": {"method": {"enum": [m]}, "params": {"$ref": "#/definitions/" + name}}}
        props = {k: ({"enum": sorted(v)} if v else {}) for k, v in params[m].items() if f"{m}.{k}" not in drop}
        (exp_req if m in exp_only else stable_req).append(entry)
        stable_defs[name] = {"properties": {k: v for k, v in props.items() if f"{m}.{k}" not in exp_only}}
        exp_defs[name] = {"properties": props}
    exp_req += stable_req
    for folder, req, defs in ((out, stable_req, stable_defs), (exp, exp_req, {**stable_defs, **exp_defs})):
        for name, one in (("ClientRequest.json", req), ("ServerNotification.json", notes), ("ServerRequest.json", [])):
            with open(os.path.join(folder, name), "w") as f:
                json.dump({"oneOf": one, "definitions": defs if name == "ClientRequest.json" else {}}, f)
    return exp


class SchemaCalls(unittest.TestCase):
    def setUp(self):
        self.methods, self.params = codex_compat.host_calls()

    def problems(self, **kw):
        out = tempfile.mkdtemp()
        exp = write_schema(out, self.methods, self.params, **kw)
        return codex_compat.check_schema(out, experimental=exp)

    def test_everything_the_host_sends_is_there(self):
        self.assertEqual(self.problems(), [])

    def test_a_missing_parameter_or_value_is_named(self):
        got = self.problems(drop=("thread/turns/list.itemsView", "thread/backgroundTerminals/terminate"))
        self.assertIn("no thread/turns/list {itemsView}", got)
        self.assertIn("no thread/backgroundTerminals/terminate", got)
        out = tempfile.mkdtemp()
        params = {**self.params, "thread/turns/list": {**self.params["thread/turns/list"], "sortDirection": {"asc"}}}
        exp = write_schema(out, self.methods, params)   # the version knows only "asc", the host sends "desc"
        got = codex_compat.check_schema(out, experimental=exp)
        self.assertIn("thread/turns/list {sortDirection: desc} — the version does not know the value", got)

    def test_the_handshake_and_a_stable_value_by_the_stable_schema(self):
        self.assertIn("no initialize", self.problems(drop=("initialize",)))
        out = tempfile.mkdtemp()
        params = {**self.params, "thread/turns/list": {**self.params["thread/turns/list"], "sortDirection": {"asc"}}}
        exp = write_schema(out, self.methods, params)   # the stable schema knows only "asc"…
        with open(os.path.join(exp, "ClientRequest.json"), encoding="utf-8") as f:
            req = json.load(f)
        req["definitions"]["Pthread_turns_list"]["properties"]["sortDirection"] = {"enum": ["asc", "desc"]}
        with open(os.path.join(exp, "ClientRequest.json"), "w", encoding="utf-8") as f:
            json.dump(req, f)   # …the experimental one takes "desc" too: a stable call still may not send it
        self.assertIn("thread/turns/list {sortDirection: desc} — the version does not know the value",
                      codex_compat.check_schema(out, experimental=exp))

    def test_an_answer_value_the_version_does_not_take_is_named(self):
        out = tempfile.mkdtemp()
        exp = write_schema(out, self.methods, self.params)
        with open(os.path.join(out, "ClientRequest.json"), encoding="utf-8") as f:
            req = json.load(f)
        req["definitions"]["CommandExecutionRequestApprovalResponse"]["properties"]["decision"] = {
            "oneOf": [{"enum": ["accept"]}, {"enum": ["cancel"]}]}
        with open(os.path.join(out, "ClientRequest.json"), "w", encoding="utf-8") as f:
            json.dump(req, f)
        self.assertEqual(codex_compat.check_schema(out, experimental=exp),
                         ["CommandExecutionRequestApprovalResponse {decision: decline} — the version does not know the value"])

    def test_what_is_only_experimental_needs_the_experimental_schema(self):
        out = tempfile.mkdtemp()
        write_schema(out, self.methods, self.params, exp_only=("turn/start.additionalContext",
                                                                "thread/backgroundTerminals/terminate"))
        got = codex_compat.check_schema(out, experimental=None)
        self.assertIn("no turn/start {additionalContext} (--experimental)", got)
        self.assertIn("no thread/backgroundTerminals/terminate (--experimental)", got)
        exp = write_schema(out, self.methods, self.params, exp_only=("turn/start.additionalContext",
                                                                      "thread/backgroundTerminals/terminate"))
        self.assertEqual(codex_compat.check_schema(out, experimental=exp), [])


if __name__ == "__main__":
    unittest.main()
