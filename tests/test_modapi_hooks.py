"""Module API points of slice C2 (Module API spec, §2.1, §2.4): launch.env and the sign-in rule, hub commands from a
module (cli), hub doctor checks, migrate on a version change, the hubmod helper and the example module. A module stays
silent, crashes, answers with garbage — the core does its own thing; without modules the core behaves as before."""
import tests  # noqa: F401 — keep the machine's settings out
import functools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from core import hubmod, modrun, modules, version
from hosts import streamjson
from hosts.streamjson import StreamHost
from core import cutout  # noqa: E402 — the open flavor's run has no built-in pool (core/cutout.py)
accounts = None
if cutout.present("profile"):
    from modules.accounts import accounts
from tests.fakes.exe import script
from tests.fakes.modstand import Stand
from tests.fakes.procs import kill

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(ROOT, "bin", "hub")
EXAMPLE = os.path.join(ROOT, "examples", "module-hello")
AUTH = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


class Mods:
    """Modules without processes: ready records, want and launch files, replies instead of HTTP (modrun.post is replaced)."""

    def __init__(self, case, answers, env=None, points=("launch.env",)):
        self.dir = tempfile.mkdtemp(prefix="modhooks-")
        os.makedirs(modrun.run_dir(self.dir))
        self.answers, self.ports, self.errors, self.payloads, recs = {}, {}, [], [], {}
        for i, (name, answer) in enumerate(answers.items()):
            d = os.path.join(self.dir, "m", name)
            os.makedirs(d)
            with open(os.path.join(d, "module.json"), "w") as f:
                json.dump({"name": name, "version": "1.0.0", "api": "^2.0", "run": ["x"],
                           "capabilities": ["launch.env"], "env": (env or {}).get(name, [])}, f)
            modrun._write(os.path.join(modrun.run_dir(self.dir), f"{name}.want.json"),
                          {"name": name, "cmd": ["x"], "cwd": d, "version": "1.0.0", "order": i, "budget": {},
                           "capabilities": ["launch.env"], "key": name})
            modrun._write(os.path.join(modrun.run_dir(self.dir), f"{name}.run.json"),
                          {"pid": 1, "port": 1000 + i, "secret": "s", "launch": f"L{i}", "started": 0})
            recs[name] = {"ready": f"L{i}", "points": list(points)}
            self.ports[1000 + i], self.answers[name] = name, answer
        self.mods = modrun.Modules(self.dir, records=lambda: recs,
                                   emit=lambda t, e=None, d=None: self.errors.append(d))
        patcher = mock.patch.object(modrun, "post", self.post)
        patcher.start()
        case.addCleanup(patcher.stop)

    def post(self, port, secret, path, payload, budget):
        self.payloads.append((path, payload))
        got = self.answers[self.ports[port]]
        return got if isinstance(got, tuple) else (None, got)


def host(mods=None, state_dir=None):
    h = StreamHost(state_dir or tempfile.mkdtemp(prefix="modhooks-host-"), claude=("echo",))
    if mods is not None:
        h.modules = mods.mods
    return h


class LaunchEnvAnswer(unittest.TestCase):
    """The launch.env reply schema: which names a module may set for a session and which arguments it may add to claude."""

    def check(self, res, env=()):
        return modrun.POINTS["launch.env"]["check"](res, {"name": "m", "env": list(env)})

    def test_keeps_anthropic_claude_own_and_listed_names_drops_the_rest(self):
        got = self.check({"env": {"ANTHROPIC_BASE_URL": "http://x", "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "9",
                                  "HUB_MOD_M_TOKEN": "t", "MY_VAR": "1", "OTHER": "2", "PATH": "/evil",
                                  "HUB_SESSION": "x", "HUB_MOD_OTHER_X": "y", "CLAUDE_CONFIG_DIR": "/x",
                                  "LD_PRELOAD": "/x.so", "lower": "1"}}, env=["MY_VAR"])
        self.assertEqual(got["env"], {"ANTHROPIC_BASE_URL": "http://x", "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "9",
                                      "HUB_MOD_M_TOKEN": "t", "MY_VAR": "1"})

    def test_args_long_options_only_and_not_those_that_change_what_claude_loads(self):
        self.assertEqual(self.check({"args": ["--model", "sonnet"]})["args"], ["--model", "sonnet"])
        for bad in (["-p"], ["--"], ["--mcp-config", "x"], ["--settings=x"], ["--append-system-prompt", "x"],
                    ["--dangerously-skip-permissions"], ["--resume", "x"], ["--output-format", "text"],
                    ["--Plugin_Dir", "x"], ["x"] * 33, ["--" + "a" * 600], ["--add-dir", "/"],
                    # a word without a flag in first place would stick to the last flag before it (the core's --mcp-config takes a list)
                    ["/tmp/evil-mcp.json"], ["x", "--model", "sonnet"]):
            with self.assertRaises(modrun.BadResult, msg=bad):
                self.check({"args": bad})

    def test_network_of_the_session_and_the_provider_are_never_the_modules(self):
        # proxies, trusted CAs, TLS verification, a client certificate — the manifest env does not help either; the provider choice
        net = {"HTTPS_PROXY": "http://p", "HTTP_PROXY": "http://p", "ALL_PROXY": "http://p", "NO_PROXY": "*",
               "NODE_EXTRA_CA_CERTS": "/ca.pem", "SSL_CERT_FILE": "/ca.pem", "SSL_CERT_DIR": "/ca",
               "NODE_TLS_REJECT_UNAUTHORIZED": "0", "NODE_USE_SYSTEM_CA": "1", "REQUESTS_CA_BUNDLE": "/ca.pem",
               "CLAUDE_CODE_HTTPS_PROXY": "http://p", "CLAUDE_CODE_PROXY_URL": "http://p",
               "CLAUDE_CODE_CLIENT_CERT": "/c.pem", "CLAUDE_CODE_CERT_STORE": "x",
               "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_USE_VERTEX": "1", "CLAUDE_CODE_USE_FOUNDRY": "1",
               "CLAUDE_CODE_USE_MANTLE": "1"}
        net.update(SSLKEYLOGFILE="/k", CURL_HOME="/c", WGETRC="/w", DOCKER_HOST="tcp://x")
        ok = {"CLAUDE_CODE_USE_POWERSHELL_TOOL": "1", "HUB_MOD_M_PROXY": "own", "BUNDLE_GEMFILE": "/g"}
        got = self.check({"env": {**net, **ok}}, env=list(net) + ["BUNDLE_GEMFILE"])
        self.assertEqual(got["env"], ok)
        for name in ("HTTPS_PROXY", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "NODE_TLS_REJECT_UNAUTHORIZED"):
            errs = modules.validate({"name": "m", "version": "1.0.0", "api": f"^{version.MODULE_API}", "run": ["x"],
                                     "env": [name]})
            self.assertTrue([e for e in errs if e.startswith("env")], name)

    def test_shape(self):
        self.assertEqual(self.check({}), {})
        for bad in (None, [], {"env": []}, {"env": {"A": 1}}, {"args": "x"}, {"other": 1}):
            with self.assertRaises(modrun.BadResult, msg=bad):
                self.check(bad)


class LaunchEnv(unittest.TestCase):
    def setUp(self):
        self.cfg = tempfile.mkdtemp(prefix="modhooks-cfg-")
        self.addCleanup(os.environ.__setitem__, "HUB_CONFIG_DIR", os.environ["HUB_CONFIG_DIR"])
        os.environ["HUB_CONFIG_DIR"] = self.cfg
        if accounts is None:
            self.skipTest("the built-in pool is cut out of this tree")
        accounts.save({"accounts": [{"name": "work", "token": "POOL"}]}, os.path.join(self.cfg, "accounts.json"))

    def test_without_modules_the_environment_and_command_are_as_before(self):
        sd = tempfile.mkdtemp(prefix="modhooks-host-")
        plain, empty = host(state_dir=sd), host(state_dir=sd)
        empty.modules = modrun.Modules(tempfile.mkdtemp(prefix="modhooks-none-"))
        for h in (plain, empty):
            h.env["ANTHROPIC_API_KEY"] = "OWNER"
        a, b = [], []
        self.assertTrue(plain.launch_env("s", account="work", args=a) == empty.launch_env("s", account="work", args=b))
        self.assertEqual(a, b)
        self.assertEqual(a, [])

    def test_module_adds_environment_and_arguments_and_gets_the_session(self):
        m = Mods(self, {"m": {"env": {"HUB_MOD_M_X": "1", "PATH": "/evil"}, "args": ["--model", "sonnet"]}})
        args = []
        env = host(m).launch_env("s", args=args, info={"cwd": "/w", "kind": "work"})
        self.assertEqual(env["HUB_MOD_M_X"], "1")
        self.assertNotEqual(env.get("PATH"), "/evil")
        self.assertEqual(args, ["--model", "sonnet"])
        self.assertEqual(m.payloads, [("/hook/launch.env", {"session": "s", "cwd": "/w", "kind": "work"})])

    def test_empty_value_removes_a_variable_and_later_module_wins(self):
        m = Mods(self, {"a": {"env": {"CLAUDE_CODE_X": "a", "ANTHROPIC_MODEL": "a"}},
                        "b": {"env": {"CLAUDE_CODE_X": "b", "ANTHROPIC_MODEL": ""}}})
        env = host(m).launch_env("s")
        self.assertEqual(env["CLAUDE_CODE_X"], "b")
        self.assertNotIn("ANTHROPIC_MODEL", sorted(env))

    def test_module_address_keeps_only_that_modules_key(self):
        m = Mods(self, {"m": {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:9", "ANTHROPIC_AUTH_TOKEN": "MOD"}}})
        h = host(m)
        h.env.update(ANTHROPIC_API_KEY="OWNER", CLAUDE_CODE_OAUTH_TOKEN="OWNER2")
        env = h.launch_env("s", account="work")
        self.assertEqual({k: env.get(k) for k in AUTH}, {"CLAUDE_CODE_OAUTH_TOKEN": None, "ANTHROPIC_API_KEY": None,
                                                         "ANTHROPIC_AUTH_TOKEN": "MOD"})
        self.assertNotIn("HUB_ACCOUNT", sorted(env))   # the session is not on a pool account: do not attribute its limits to it
        self.assertEqual(m.errors, [])

    def test_module_address_without_its_key_is_refused(self):
        m = Mods(self, {"m": {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:9", "HUB_MOD_M_X": "1"}}})
        h = host(m)
        h.env["ANTHROPIC_API_KEY"] = "OWNER"
        env = h.launch_env("s", account="work")
        self.assertNotIn("ANTHROPIC_BASE_URL", sorted(env))        # the module's addition is rejected entirely
        self.assertNotIn("HUB_MOD_M_X", sorted(env))
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "POOL")   # the rest is as without the module
        self.assertEqual(env["HUB_ACCOUNT"], "work")
        self.assertEqual([(e["module"], e["hook"], e["kind"]) for e in m.errors], [("m", "launch.env", "schema")])

    def test_module_address_drops_inherited_headers_and_keeps_its_own(self):
        hdr = "ANTHROPIC_CUSTOM_HEADERS"
        m = Mods(self, {"m": {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:9", "ANTHROPIC_AUTH_TOKEN": "MOD"}}})
        h = host(m)
        h.env[hdr] = "X-Owner-Auth: OWNER"
        self.assertNotIn(hdr, sorted(h.launch_env("s", account="work")))
        m = Mods(self, {"m": {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:9", "ANTHROPIC_AUTH_TOKEN": "MOD",
                                      hdr: "X-Mod: 1"}}})
        h = host(m)
        h.env[hdr] = "X-Owner-Auth: OWNER"
        self.assertEqual(h.launch_env("s")[hdr], "X-Mod: 1")
        m = Mods(self, {"m": {"env": {"CLAUDE_CODE_X": "1"}}})     # not the module's address — headers as they were
        h = host(m)
        h.env[hdr] = "X-Owner-Auth: OWNER"
        self.assertEqual(h.launch_env("s")[hdr], "X-Owner-Auth: OWNER")

    def test_headers_alone_are_not_the_modules_key(self):
        m = Mods(self, {"m": {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
                                      "ANTHROPIC_CUSTOM_HEADERS": "X-Mod: 1"}}})
        env = host(m).launch_env("s", account="work")
        self.assertNotIn("ANTHROPIC_BASE_URL", sorted(env))
        self.assertEqual([e["module"] for e in m.errors], ["m"])

    def test_only_the_key_of_the_module_whose_address_stays(self):
        m = Mods(self, {"a": {"env": {"ANTHROPIC_BASE_URL": "http://a", "ANTHROPIC_API_KEY": "A"}},
                        "b": {"env": {"ANTHROPIC_BASE_URL": "http://b", "ANTHROPIC_AUTH_TOKEN": "B"}}})
        env = host(m).launch_env("s")
        self.assertEqual((env["ANTHROPIC_BASE_URL"], env.get("ANTHROPIC_API_KEY"), env["ANTHROPIC_AUTH_TOKEN"]),
                         ("http://b", None, "B"))

    def test_refused_later_address_falls_back_to_the_earlier_module(self):
        m = Mods(self, {"a": {"env": {"ANTHROPIC_BASE_URL": "http://a", "ANTHROPIC_API_KEY": "A"}},
                        "b": {"env": {"ANTHROPIC_BASE_URL": "http://b"}}})
        env = host(m).launch_env("s")
        self.assertEqual((env["ANTHROPIC_BASE_URL"], env["ANTHROPIC_API_KEY"]), ("http://a", "A"))
        self.assertEqual([e["module"] for e in m.errors], ["b"])

    def test_any_name_that_moves_claude_elsewhere_takes_the_login_rule(self):
        # not only ANTHROPIC_BASE_URL: provider addresses, a socket, a sign-in address, a cloud region and address (from
        # the manifest env); the inherited provider mode and all the core's sign-in keys are removed, the module's key stays
        own = {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_BEARER_TOKEN_BEDROCK": "OWNER-AWS",
               "ANTHROPIC_FOUNDRY_API_KEY": "OWNER-F", "CLAUDE_CODE_OAUTH_REFRESH_TOKEN": "OWNER-R",
               "ANTHROPIC_API_KEY": "OWNER", "ANTHROPIC_CUSTOM_HEADERS": "X-Owner: 1"}
        for name in ("ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_VERTEX_BASE_URL", "ANTHROPIC_FOUNDRY_BASE_URL",
                     "ANTHROPIC_FOUNDRY_RESOURCE", "ANTHROPIC_UNIX_SOCKET", "CLAUDE_CODE_CUSTOM_OAUTH_URL",
                     "CLAUDE_CODE_API_BASE_URL", "CLOUD_ML_REGION", "AWS_REGION", "AWS_ENDPOINT_URL_BEDROCK_RUNTIME",
                     "VERTEX_REGION_CLAUDE_X"):
            m = Mods(self, {"m": {"env": {name: "http://127.0.0.1:9", "HUB_MOD_M_X": "1"}}}, env={"m": [name]})
            h = host(m)
            h.env.update(own)
            env = h.launch_env("s", account="work")
            self.assertNotIn(name, sorted(env), name)                          # without its own key — rejected entirely
            self.assertEqual((env["CLAUDE_CODE_OAUTH_TOKEN"], env["HUB_ACCOUNT"]), ("POOL", "work"), name)
            self.assertEqual([e["module"] for e in m.errors], ["m"], name)
            m = Mods(self, {"m": {"env": {name: "http://127.0.0.1:9", "ANTHROPIC_AUTH_TOKEN": "MOD"}}},
                     env={"m": [name]})
            h = host(m)
            h.env.update(own)
            env = h.launch_env("s", account="work")
            self.assertEqual(env[name], "http://127.0.0.1:9", name)
            self.assertEqual({k: env[k] for k in env if k in own or k in AUTH or k == "HUB_ACCOUNT"},
                             {"ANTHROPIC_AUTH_TOKEN": "MOD", "AWS_BEARER_TOKEN_BEDROCK": "OWNER-AWS"}, name)
            self.assertEqual(m.errors, [], name)

    def test_names_claude_does_not_read_are_not_an_address(self):
        # the module's own names and names for the session's tools from the manifest env are not a claude address,
        # nor are cloud names without a provider mode (and with it switched off, "0") — then only the session's tools read them
        add = {"HUB_MOD_M_URL": "http://x", "MY_SERVICE_URL": "http://x", "AWS_REGION": "eu-west-1",
               "AWS_PROFILE": "tools"}
        m = Mods(self, {"m": {"env": add}}, env={"m": ["MY_SERVICE_URL", "AWS_REGION", "AWS_PROFILE"]})
        h = host(m)
        h.env["CLAUDE_CODE_USE_BEDROCK"] = "0"
        env = h.launch_env("s", account="work")
        self.assertEqual({k: env[k] for k in add}, add)
        self.assertEqual((env["CLAUDE_CODE_OAUTH_TOKEN"], env["HUB_ACCOUNT"]), ("POOL", "work"))
        self.assertEqual(m.errors, [])

    def test_two_modules_moving_claude_by_different_names_the_later_is_refused(self):
        m = Mods(self, {"a": {"env": {"ANTHROPIC_BASE_URL": "http://a", "ANTHROPIC_API_KEY": "A"}},
                        "b": {"env": {"CLAUDE_CODE_CUSTOM_OAUTH_URL": "http://b", "ANTHROPIC_AUTH_TOKEN": "B"}}})
        env = host(m).launch_env("s")
        self.assertEqual((env["ANTHROPIC_BASE_URL"], env["ANTHROPIC_API_KEY"], env.get("ANTHROPIC_AUTH_TOKEN"),
                          env.get("CLAUDE_CODE_CUSTOM_OAUTH_URL")), ("http://a", "A", None, None))
        self.assertEqual([e["module"] for e in m.errors], ["b"])
        self.assertIn("module a ", m.errors[0]["error"])

    def test_inherited_address_is_not_the_modules_business(self):
        m = Mods(self, {"m": {"env": {"CLAUDE_CODE_X": "1"}}})
        h = host(m)
        h.env.update(ANTHROPIC_BASE_URL="http://own", ANTHROPIC_API_KEY="OWNER")
        env = h.launch_env("s")
        self.assertEqual((env["ANTHROPIC_BASE_URL"], env["ANTHROPIC_API_KEY"]), ("http://own", "OWNER"))

    def test_failed_module_changes_nothing(self):
        m = Mods(self, {"m": ("timeout", "no answer within 5 s"), "n": {"env": {"PATH": "/x"}, "args": ["-p"]}})
        h = host(m)
        args = []
        self.assertTrue(h.launch_env("s", args=args) == host(state_dir=h.state_dir).launch_env("s"))
        self.assertEqual(args, [])
        self.assertEqual([(e["module"], e["kind"]) for e in m.errors], [("m", "timeout"), ("n", "schema")])

    def test_spawn_passes_session_facts_and_appends_arguments(self):
        m = Mods(self, {"m": {"args": ["--model", "haiku", "--effort", "max"]}})
        h = host(m)
        h.tool_flags = lambda **kw: ["--effort", "medium"]
        h.command = lambda *a, **kw: ["claude", "-p"]

        class Spawned(Exception):
            pass
        with mock.patch.object(streamjson.subprocess, "Popen", side_effect=Spawned) as popen, \
                mock.patch.object(streamjson.platforms.current, "below_hub", lambda cmd, **kw: list(cmd), create=True):
            with self.assertRaises(Spawned):
                h.spawn("s", "sid", cwd=h.state_dir, kind="work", model="opus")
        cmd = popen.call_args[0][0]
        # the module's arguments go after the core's flags (a dangling module flag will not swallow a core flag); its --effort is the last
        self.assertEqual(cmd, ["claude", "-p", "--effort", "medium", "--model", "haiku", "--effort", "max"])
        sent = m.payloads[0][1]
        self.assertEqual((sent["session"], sent["cwd"], sent["kind"], sent["model"], sent["resume"]),
                         ("s", h.state_dir, "work", "opus", False))


class LaunchEnvProcess(unittest.TestCase):
    """launch.env on a real module process: the reply reaches the session's environment; silent, garbage reply, or a crash
    mid-request — the environment is as without the module, within the budget, with module.error."""

    def test_answer_silence_garbage_and_crash(self):
        s = Stand(self, points=["launch.env"], caps=["launch.env"], budget={"launch.env": 0.5})
        s.update(**{"launch.env": {"do": "ok", "answer": {"env": {"HUB_MOD_FAKE_X": "1"}}}})
        s.launch()
        s.until_ready()
        h = StreamHost(tempfile.mkdtemp(prefix="modhooks-host-"), claude=("echo",))
        h.modules = s.hub.modules.mods
        plain = StreamHost(h.state_dir, claude=("echo",)).launch_env("s")
        self.assertEqual(h.launch_env("s")["HUB_MOD_FAKE_X"], "1")
        for do, kind in (("silent", "slow"), ("garbage", "schema"), ("exit", "closed")):   # silence right after a good answer is "slow"
            s.update(**{"launch.env": {"do": do}})
            began = time.monotonic()
            self.assertTrue(h.launch_env("s") == plain, do)
            self.assertLess(time.monotonic() - began, 0.5 + 1.0, do)   # the budget + a margin for load
            self.assertEqual(s.events("module.error")[-1]["data"]["kind"], kind, do)
            s.hub.state["modules"]["fake"].update(errors=[], state="ok", until=None, suspensions=0)


def real_claude():
    """The real claude, bypassing the hub's wrapper (hosts/nested), or None."""
    c = os.environ.get("HUB_REAL_CLAUDE")
    if c and os.path.isfile(c):
        return c
    return shutil.which("claude", path=streamjson.NESTED_SHIM["without_shims"](os.environ.get("PATH", "")))


@functools.cache
def claude_version():
    try:
        out = subprocess.run([real_claude(), "--version"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return "?"
    return (out.split() or ["?"])[0]


class Receiver:
    """A receiver on 127.0.0.1: records which key claude came with, and answers with a request error."""

    def __init__(self, case):
        self.seen = []
        seen = self.seen

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                seen.append({k.lower(): v for k, v in self.headers.items()})
                body = b'{"type":"error","error":{"type":"invalid_request_error","message":"stand"}}'
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            do_GET = do_POST

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        case.addCleanup(self.srv.server_close)
        case.addCleanup(self.srv.shutdown)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def header(self, name):
        """The values of header `name` in all of claude's requests."""
        return {h[name.lower()] for h in self.seen if name.lower() in h}

    def keys(self):
        """The sign-in keys claude came with: Authorization values (without Bearer) and x-api-key."""
        out = set()
        for h in self.seen:
            if h.get("authorization"):
                out.add(h["authorization"].split(" ", 1)[-1])
            if h.get("x-api-key"):
                out.add(h["x-api-key"])
        return out


def stand_certs():
    """A substitute CA and an api.anthropic.com certificate issued by it (openssl) — a folder, or None."""
    if not shutil.which("openssl"):
        return None
    d = tempfile.mkdtemp(prefix="modhooks-ca-")
    run = [("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem", "-days", "2",
            "-subj", "/CN=Stand CA", "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign"),
           ("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "srv.key", "-out", "srv.csr", "-subj",
            "/CN=api.anthropic.com"),
           ("x509", "-req", "-in", "srv.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-out", "srv.pem",
            "-days", "2", "-extfile", "ext.cnf")]
    with open(os.path.join(d, "ext.cnf"), "w") as f:
        f.write("subjectAltName=DNS:api.anthropic.com\n")
    for args in run:
        if subprocess.run(["openssl", *args], cwd=d, capture_output=True).returncode:
            return None
    return d


class Proxy:
    """A CONNECT proxy on 127.0.0.1: records where clients asked to go, breaks TLS with its own CA's certificate and
    records the request headers (the sign-in key), answering with a request error — nothing goes to the network."""

    def __init__(self, case, certs):
        import socket
        import ssl
        self.connects, self.seen = [], []
        self.ls = socket.socket()
        self.ls.bind(("127.0.0.1", 0))
        self.ls.listen(50)
        case.addCleanup(self.ls.close)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(os.path.join(certs, "srv.pem"), os.path.join(certs, "srv.key"))
        seen = self.seen

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                seen.append({k.lower(): v for k, v in self.headers.items()})
                body = b'{"type":"error","error":{"type":"invalid_request_error","message":"stand"}}'
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            do_GET = do_POST

        def serve(c):
            try:
                f = c.makefile("rb")
                line = f.readline().decode("latin-1").strip()
                while f.readline() not in (b"\r\n", b""):
                    pass
                self.connects.append(line)
                if line.startswith("CONNECT"):
                    c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                    H(ctx.wrap_socket(c, server_side=True), ("127.0.0.1", 0), None)
            except OSError:
                pass
            finally:
                c.close()

        def loop():
            while True:
                try:
                    c, _ = self.ls.accept()
                except OSError:
                    return
                threading.Thread(target=serve, args=(c,), daemon=True).start()
        threading.Thread(target=loop, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.ls.getsockname()[1]}"

    def keys(self):
        return {h.get("authorization", "").split(" ")[-1] or h.get("x-api-key", "") for h in self.seen}


def machine_net(name):
    """A machine variable that decides where and how claude goes to the network: proxy, TLS trust, provider and address."""
    return (name.lower() in ("http_proxy", "https_proxy", "all_proxy", "no_proxy", "node_extra_ca_certs",
                             "ssl_cert_file", "ssl_cert_dir", "node_tls_reject_unauthorized", "node_options",
                             "node_use_env_proxy", "node_use_system_ca")
            or name.startswith(("ANTHROPIC_", "CLAUDE_CODE_USE_", "CLAUDE_CODE_SKIP_", "CLAUDE_CODE_CLIENT_", "AWS_",
                                "CLOUD_ML_", "GOOGLE_", "VERTEX_")))


@unittest.skipUnless(real_claude(), "no real claude")
class LoginRuleWithRealClaude(unittest.TestCase):
    """The sign-in rule, with the real claude and fake keys against our own receiver (spec §2.4, §7): with a module
    address claude arrives only with the module's key, not with the pool's or an inherited one.

    The checks with the rule are strict. The "without the rule" controls check claude itself (that it would carry the
    key there), and that depends on its version and platform: if it did not work out — a skip with a reason, not red
    (a hub update runs these tests on its side and rolls back on red). The machine's proxy and provider do not get
    into the launch — only those the test sets."""

    def setUp(self):
        env = mock.patch.dict(os.environ)   # will also restore HUB_CONFIG_DIR below
        env.start()
        self.addCleanup(env.stop)
        for k in [k for k in os.environ if machine_net(k)]:
            del os.environ[k]
        self.unmet = []
        self.cfg = tempfile.mkdtemp(prefix="modhooks-cfg-")
        os.environ["HUB_CONFIG_DIR"] = self.cfg
        if accounts is None:
            self.skipTest("the built-in pool is cut out of this tree")
        accounts.save({"accounts": [{"name": "work", "token": "FAKE-POOL"}]}, os.path.join(self.cfg, "accounts.json"))
        self.recv = Receiver(self)

    def run_claude(self, env, home=None):
        home = home or tempfile.mkdtemp(prefix="modhooks-home-")
        env = {**env, "HOME": home, "CLAUDE_CONFIG_DIR": os.path.join(home, ".claude"), "DISABLE_TELEMETRY": "1",
               "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1"}
        try:   # claude's retries after a TLS refusal can outlast the deadline: what it managed to send is what the checks decide on
            subprocess.run([real_claude(), "-p", "stand", "--max-turns", "1"], env=env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, cwd=home)
        except subprocess.TimeoutExpired:
            pass

    def premise(self, ok, what):
        """The "without the rule" control: claude of this version and platform does what the rule guards against. If it
        does not — remember it; the strict checks go on, and the test ends with a skip (premises)."""
        if not ok:
            self.unmet.append(what)

    def premises(self):
        if self.unmet:
            self.skipTest(f"the control without the rule did not work for claude {claude_version()} ({sys.platform}): "
                          + "; ".join(self.unmet))

    def test_claude_comes_to_the_module_address_with_the_module_key_only(self):
        for account, inherited in (("work", None), (None, "FAKE-OWNER")):
            self.recv.seen.clear()
            m = Mods(self, {"m": {"env": {"ANTHROPIC_BASE_URL": self.recv.url, "ANTHROPIC_AUTH_TOKEN": "FAKE-MOD"}}})
            h = host(m)
            if inherited:
                h.env["ANTHROPIC_API_KEY"] = inherited
            self.run_claude(h.launch_env("s", account=account))
            self.assertTrue(self.recv.seen, f"claude did not come to the module address ({account})")
            self.assertEqual(self.recv.keys(), {"FAKE-MOD"}, account)

    def test_inherited_custom_headers_do_not_reach_the_module_address(self):
        for own in ({}, {"ANTHROPIC_CUSTOM_HEADERS": "X-Mod-Hdr: FAKE-MODHDR"}):
            self.recv.seen.clear()
            m = Mods(self, {"m": {"env": {"ANTHROPIC_BASE_URL": self.recv.url, "ANTHROPIC_AUTH_TOKEN": "FAKE-MOD",
                                          **own}}})
            h = host(m)
            h.env["ANTHROPIC_CUSTOM_HEADERS"] = "X-Owner-Auth: FAKE-HDR"
            self.run_claude(h.launch_env("s", account="work"))
            self.assertTrue(self.recv.seen, "claude did not come to the module address")
            self.assertEqual(self.recv.header("X-Owner-Auth"), set(), own)
            self.assertEqual(self.recv.header("X-Mod-Hdr"), {"FAKE-MODHDR"} if own else set())
        # without the rule claude carries the same header to the module's address
        self.recv.seen.clear()
        env = host().launch_env("s")
        env.update(ANTHROPIC_BASE_URL=self.recv.url, ANTHROPIC_AUTH_TOKEN="FAKE-MOD",
                   ANTHROPIC_CUSTOM_HEADERS="X-Owner-Auth: FAKE-HDR")
        self.run_claude(env)
        self.premise(self.recv.header("X-Owner-Auth") == {"FAKE-HDR"}, "does not carry ANTHROPIC_CUSTOM_HEADERS to the address")
        self.premises()

    def test_module_proxy_with_its_own_ca_never_sees_the_login(self):
        # a module proxy with its own CA would read all of claude's HTTPS (and the session tools'); the owner has their
        # own proxy with its own CA (a corporate one): also a stand, answers with a request error — nothing goes to the network
        certs, owner_certs = stand_certs(), stand_certs()
        if not (certs and owner_certs):
            self.skipTest("no openssl")
        ca = os.path.join(certs, "ca.pem")
        for var, trust in (("HTTPS_PROXY", {"NODE_EXTRA_CA_CERTS": ca}), ("ALL_PROXY", {"SSL_CERT_FILE": ca}),
                           ("HTTPS_PROXY", {"NODE_TLS_REJECT_UNAUTHORIZED": "0"})):
            mitm, owner = Proxy(self, certs), Proxy(self, owner_certs)
            add = {var: mitm.url, **trust}
            m = Mods(self, {"m": {"env": add}}, env={"m": list(add)})
            h = host(m)
            h.env.update({var: owner.url, "NODE_EXTRA_CA_CERTS": os.path.join(owner_certs, "ca.pem")})
            env = h.launch_env("s", account="work")
            self.assertEqual(env[var], owner.url, var)   # the rule leaves the owner's proxy alone — strict
            self.run_claude(env)
            self.assertEqual((mitm.connects, mitm.seen), ([], []), var)
            self.premise(owner.connects, f"with {var} it does not go through the owner's proxy")
            # without the rule the same environment gives the pool key to the module's proxy
            mitm = Proxy(self, certs)
            h = host()
            h.env.update({var: owner.url, "NODE_EXTRA_CA_CERTS": os.path.join(owner_certs, "ca.pem")})
            env = h.launch_env("s", account="work")
            env.update({var: mitm.url, **trust})
            self.assertTrue("FAKE-POOL" in env.values(), "the pool did not put the sign-in into the launch")   # our part of the control — strict
            self.run_claude(env)
            if mitm.seen:   # it reached the module's proxy — from here the hub's environment decides the key: strict
                self.assertTrue("FAKE-POOL" in mitm.keys(), var)
            else:   # some versions under ALL_PROXY go to the API bypassing the proxy (2.1.218)
                went = any(c.startswith("CONNECT api.anthropic.com") for c in mitm.connects)
                self.premise(False, f"with {var} and {next(iter(trust))} " + (
                    "sends the API request through the proxy but does not trust the CA" if went else "sends the API request past the proxy"))
        self.premises()

    def test_provider_address_does_not_get_the_cloud_login(self):
        # the owner's Bedrock mode + a Bedrock address from the module: claude would carry the owner's cloud key there
        owner_recv = Receiver(self)
        cloud = {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_REGION": "us-east-1", "AWS_BEARER_TOKEN_BEDROCK": "FAKE-AWS",
                 "ANTHROPIC_BEDROCK_BASE_URL": owner_recv.url, "ANTHROPIC_BASE_URL": owner_recv.url}
        m = Mods(self, {"m": {"env": {"ANTHROPIC_BEDROCK_BASE_URL": self.recv.url,
                                      "ANTHROPIC_AUTH_TOKEN": "FAKE-MOD"}}})
        h = host(m)
        h.env.update(cloud)
        self.run_claude(h.launch_env("s", account="work"))
        self.assertTrue(self.recv.seen or owner_recv.seen, "claude came nowhere")
        self.assertEqual(self.recv.keys() | owner_recv.keys(), {"FAKE-MOD"})
        self.recv.seen.clear()
        env = host().launch_env("s", account="work")   # without the rule
        env.update(cloud, ANTHROPIC_BEDROCK_BASE_URL=self.recv.url)
        self.run_claude(env)
        self.premise("FAKE-AWS" in self.recv.keys(), "does not carry the cloud key to the Bedrock address")
        self.premises()

    def test_module_key_beats_the_saved_login_of_the_machine(self):
        home = tempfile.mkdtemp(prefix="modhooks-home-")
        os.makedirs(os.path.join(home, ".claude"))
        with open(os.path.join(home, ".claude", ".credentials.json"), "w") as f:
            json.dump({"claudeAiOauth": {"accessToken": "FAKE-SAVED", "refreshToken": "FAKE-REFRESH",
                                         "expiresAt": int(time.time() * 1000) + 3600_000,
                                         "scopes": ["user:inference", "user:profile"]}}, f)
        for add, keys in (({}, {"FAKE-SAVED"}), ({"ANTHROPIC_AUTH_TOKEN": "FAKE-MOD"}, {"FAKE-MOD"})):
            self.recv.seen.clear()
            env = {k: v for k, v in host().launch_env("s").items() if k not in AUTH}   # the machine's sign-in — by file only
            env.update(ANTHROPIC_BASE_URL=self.recv.url, **add)
            self.run_claude(env, home=home)
            if add:   # the module's key beats the machine's sign-in — the rule rests on this, strict
                self.assertEqual(self.recv.keys(), keys)
            else:
                self.premise(self.recv.keys() == keys, "does not take the sign-in from .credentials.json")
        self.premises()

    def test_without_the_rule_other_keys_would_go_to_the_module(self):
        # the environment before the rule: a module address on top of an inherited key or the pool's — claude carries someone else's key there too
        for account, inherited, add, leaked in (
                (None, "FAKE-OWNER", {"ANTHROPIC_AUTH_TOKEN": "FAKE-MOD"}, "FAKE-OWNER"),
                ("work", None, {}, "FAKE-POOL")):
            self.recv.seen.clear()
            h = host()
            if inherited:
                h.env["ANTHROPIC_API_KEY"] = inherited
            env = h.launch_env("s", account=account)
            env.update(ANTHROPIC_BASE_URL=self.recv.url, **add)
            self.assertTrue(leaked in env.values(), "the key did not reach the launch")   # our part of the control — strict
            self.run_claude(env)
            self.premise(leaked in self.recv.keys(), f"does not carry {leaked} to the module address")
        self.premises()


@unittest.skipUnless(accounts, "the built-in pool is cut out of this tree (LoginRuleWithRealClaude runs on it)")
class ControlsOfOtherClaude(unittest.TestCase):
    """A hub update runs the tests on its side and rolls back on red (18.10: from v1.0.1 the next release would not
    install — the machine's claude under ALL_PROXY went to the API bypassing the proxy). The same class with a claude
    that goes nowhere: the "without the rule" control is a skip with a reason, the check with the rule is still red."""

    def test_unmet_control_skips_and_the_rule_check_stays_strict(self):
        fake = script(os.path.join(tempfile.mkdtemp(prefix="modhooks-fake-"), "claude"),
                      "import sys\nif '--version' in sys.argv:\n    print('0.0.1 (stand)')\n")
        cls = "tests.test_modapi_hooks.LoginRuleWithRealClaude."
        r = subprocess.run([sys.executable, "-m", "unittest", "-v",
                            cls + "test_without_the_rule_other_keys_would_go_to_the_module",
                            cls + "test_claude_comes_to_the_module_address_with_the_module_key_only"],
                           cwd=ROOT, env={**os.environ, "HUB_REAL_CLAUDE": fake}, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
        self.assertRegex(r.stderr, r"test_without_the_rule_other_keys_would_go_to_the_module \(.*\) \.\.\. skipped "
                                   r"'the control without the rule did not work for claude 0\.0\.1")
        self.assertRegex(r.stderr,
                         r"test_claude_comes_to_the_module_address_with_the_module_key_only \(.*\) \.\.\. FAIL")


class SpawnWaitsForModules(unittest.TestCase):
    """After a hub restart sessions come up before the modules answer hello: a session launch waits for modules with
    launch.env or prompt (rights in the manifest) while they are not ready, no longer than modrun.LAUNCH_WAIT; then it
    goes without them and with module.error. If the module is ready, stopped, paused or without these rights, the
    launch does not wait."""

    def stand(self, caps=("launch.env",), clock=None):
        s = Stand(self, points=["launch.env"] if "launch.env" in caps else [], caps=list(caps), clock=clock)
        s.update(**{"launch.env": {"do": "ok", "answer": {"env": {"HUB_MOD_FAKE_X": "1"}}}})
        h = host(state_dir=tempfile.mkdtemp(prefix="modhooks-host-"))
        h.modules = s.hub.modules.mods
        self.spawned = []
        test = self

        class Host:
            kind, screen_dialogs = "stream", False
            tool_flags = staticmethod(lambda **kw: [])

            def spawn(self, name, **kw):
                test.spawned.append(h.launch_env(name))
                return "spawned"

            def alive(self, name):
                return False

            def info(self, name):
                return {"pid": 1}

            def poll(self):
                return []
        s.hub.host = Host()
        s.hub.modules.sync()
        return s

    def request(self, s, name="a"):
        s.hub.record("session.requested", name, {"cwd": s.state_dir, "session_id": f"s-{name}", "channel": "cli",
                                                 "owner": True})

    def test_spawn_waits_until_the_module_is_ready_and_gets_its_environment(self):
        s = self.stand()
        self.request(s)
        self.assertEqual(self.spawned, [])                 # the module is not up yet — the start is deferred
        self.assertIn("a", s.hub.waiting())
        s.launch()
        self.assertTrue(s.until(lambda: (s.hub.run_delayed(), self.spawned)[1], timeout=20))
        self.assertEqual(self.spawned[0].get("HUB_MOD_FAKE_X"), "1")
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(s.events("module.error"), [])

    def test_launch_file_of_the_previous_life_is_not_ready(self):
        # the hub is restarted: the "ready" record and the launch file are from the previous process, which is gone
        s = self.stand()
        s.launch()
        s.until_ready()
        p = s.procs[-1]
        kill(p.pid)
        p.wait(10)
        self.request(s)
        self.assertEqual(self.spawned, [])
        s.launch()
        self.assertTrue(s.until(lambda: (s.hub.run_delayed(), self.spawned)[1], timeout=20))
        self.assertEqual(self.spawned[0].get("HUB_MOD_FAKE_X"), "1")

    def test_module_that_never_answers_delays_the_spawn_at_most_the_wait(self):
        now = [time.time()]
        s = self.stand(clock=lambda: now[0])
        for name in ("a", "b", "c"):   # after a hub restart several come up at once
            self.request(s, name)
        self.assertEqual(self.spawned, [])
        now[0] += modrun.LAUNCH_WAIT - 1
        s.hub.run_delayed()
        self.assertEqual(self.spawned, [])
        now[0] += 2
        s.hub.run_delayed()
        self.assertEqual(len(self.spawned), 3)
        self.assertFalse([e for e in self.spawned if "HUB_MOD_FAKE_X" in e])
        # one failure per module launch, not per session: otherwise the module is paused
        errs = s.events("module.error")
        self.assertEqual([(e["data"]["module"], e["data"]["hook"]) for e in errs], [("fake", "launch")])
        self.assertEqual(s.hub.modules.mods.status("fake"), "ok")
        self.assertEqual(s.hub.waiting(), set())
        # a module that did not make it in this launch no longer holds up starts — until the process is launched anew
        self.request(s, "d")
        self.assertEqual(len(self.spawned), 4)
        # "did not make it" is not a module failure: even after three hub restarts within minutes it is not paused
        for i in range(3):
            s.hub.record("module.error", "fake", {"module": "fake", "hook": "launch", "kind": "late", "error": "x",
                                                  "launch": f"L{i}", "channel": "core", "owner": False})
        self.assertEqual(s.hub.modules.mods.status("fake"), "ok")

    def test_no_wait_for_a_stopped_module_or_one_without_launch_points(self):
        s = self.stand(caps=("doctor",))
        self.request(s)
        self.assertEqual(len(self.spawned), 1)
        s = self.stand()   # the tmux host does not call modules
        s.hub.host.kind = "tmux"
        self.request(s)
        self.assertEqual(len(self.spawned), 1)
        s = self.stand()
        s.hub.record("module.error", "fake", {"module": "fake", "hook": "hello", "kind": "hello", "error": "x",
                                              "channel": "core", "owner": False})
        self.request(s)
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(len(s.events("module.error")), 1)


def manifest(stand, name=None, **extra):
    """Add fields to the stand module's module.json and re-read the graph."""
    path = os.path.join(stand.root, name or stand.name, "module.json")
    with open(path) as f:
        man = json.load(f)
    man.update(extra)
    with open(path, "w") as f:
        json.dump(man, f)
    stand.hub.modules.reload(stand.cfg, {})


def hub_cli(stand, *args, session=None, stdin=None):
    """bin/hub in the stand's state folder (the saved state is as in a live hub, state.json)."""
    if os.path.exists(os.path.join(stand.state_dir, "journal.jsonl")):
        stand.hub.save_state()
    cfg = os.path.join(stand.state_dir, "hub.json")
    with open(cfg, "w") as f:
        json.dump(stand.cfg, f)
    env = {**os.environ, "HUB_STATE_DIR": stand.state_dir, "HUB_CLI_WAIT": "0", "HUB_CONFIG": cfg}
    env.pop("HUB_SESSION", None)
    if session:
        env["HUB_SESSION"] = session
    return subprocess.run([sys.executable, HUB, *args], env=env, capture_output=True, text=True, timeout=60,
                          **({"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}))


def held_for(stand, point):
    """How long the command waited for the silent module: from the module getting the call to now (the command is over).
    Not from the start of the command: on a loaded machine its own start (imports, claude, git) takes seconds, and it is
    not what the point's budget limits."""
    call = stand.calls(point)[0]   # the first call: the wait began with it
    return time.time() - call["at"]


class Cli(unittest.TestCase):
    def stand(self, budget=None, **ctl):
        s = Stand(self, points=["cli"], caps=["cli"], budget=budget)
        manifest(s, commands={"todo": "module tasks"})
        s.update(**ctl)
        s.launch()
        s.until_ready()
        return s

    def test_unknown_command_goes_to_the_module_that_declared_it(self):
        s = self.stand(cli={"do": "ok", "answer": {"out": "done\n", "err": "warning\n", "code": 3}})
        r = hub_cli(s, "todo", "add", "buy bread", session="worker")
        self.assertEqual((r.stdout, r.stderr, r.returncode), ("done\n", "warning\n", 3))
        body = s.calls("cli")[0]["body"]
        self.assertEqual((body["command"], body["args"], body["caller"]), ("todo", ["add", "buy bread"],
                                                                          {"session": "worker"}))
        self.assertEqual(hub_cli(s, "todo").returncode, 3)
        self.assertEqual(s.calls("cli")[1]["body"]["caller"], {"owner": True})

    def test_stdin_only_when_the_module_asks_for_it(self):
        # contract 2.8: a secret (the `hub accounts add` token) goes via stdin, not as an argument; the core reads stdin only
        # if the module answered {"stdin": true}, and calls it again with what it read; other commands do not wait for stdin
        s = self.stand(cli={"do": "ok", "answer": {"stdin": True}, "with_stdin": {"out": "taken\n"}})
        r = hub_cli(s, "todo", "add", "work", stdin="tok-secret\n")
        self.assertEqual((r.stdout, r.returncode), ("taken\n", 0), r.stderr)
        first, second = [c["body"] for c in s.calls("cli")]
        self.assertNotIn("stdin", first)
        self.assertEqual((second["stdin"], second["args"]), ("tok-secret\n", ["add", "work"]))
        self.assertNotIn("tok-secret", json.dumps(first))
        # asks for stdin the second time too — an error, not a loop
        s.update(cli={"do": "ok", "answer": {"stdin": True}})
        r = hub_cli(s, "todo", stdin="x")
        self.assertEqual(r.returncode, 1)
        self.assertIn("stdin", r.stderr)
        s = self.stand(cli={"do": "ok", "answer": {"out": "ok\n"}})   # does not ask — stdin is not read
        r = hub_cli(s, "todo")
        self.assertEqual(r.stdout, "ok\n")
        self.assertEqual(len(s.calls("cli")), 1)

    def test_silent_module_is_a_short_failure_not_a_hang(self):
        # the module's own budget (1 s), not the point's 10: the same refusal by the budget without waiting 10 s
        s = self.stand(budget={"cli": 1}, cli={"do": "silent"})
        r = hub_cli(s, "todo")
        self.assertLess(held_for(s, "cli"), 1 + 8)
        self.assertEqual(r.returncode, 1)
        self.assertIn("did not answer", r.stderr)
        self.assertTrue([x for x in s.spooled() if x["type"] == "module.error"])   # the failure goes as a fact into the core's spool

    def test_the_command_waits_the_modules_own_budget_or_else_the_points(self):
        # what the silent test no longer waits for: a module without a budget of its own gets the point's (at most 10 s)
        for budget, want in ((None, modrun.budget_of("cli")), ({"cli": 1}, 1)):
            s = self.stand(budget=budget, cli={"do": "ok", "answer": {"out": "x\n"}})
            got, real = [], modrun.post

            def post(port, secret, path, payload, seconds, **kw):
                got.append((path, seconds))
                return real(port, secret, path, payload, seconds, **kw)
            with mock.patch.object(modrun, "post", post):
                s.hub.modules.mods.call("fake", "cli", {"command": "todo", "args": [], "caller": {"owner": True}})
            self.assertEqual(got, [("/hook/cli", want)], budget)
        self.assertLessEqual(modrun.budget_of("cli"), 10)

    def test_core_command_is_never_the_modules_and_unknown_is_usage(self):
        s = self.stand()
        manifest(s, commands={"todo": "tasks", "status": "foreign"})
        s.hub.modules.sync()
        hub_cli(s, "status")
        self.assertEqual(s.calls("cli"), [])
        r = hub_cli(s, "nosuch")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(s.calls("cli"), [])

    def test_commands_field_is_checked_and_needs_the_cli_right(self):
        base = {"name": "m", "version": "1.0.0", "api": "^2.0", "run": ["x"]}
        self.assertEqual(modules.validate({**base, "capabilities": ["cli"], "commands": {"todo": "tasks"}}), [])
        self.assertTrue(modules.validate({**base, "commands": {"todo": "tasks"}}))            # without the cli right
        for bad in ({"Todo": "x"}, {"todo": 1}, ["todo"], {"t" * 41: "x"}):
            self.assertTrue(modules.validate({**base, "capabilities": ["cli"], "commands": bad}), bad)

    def test_caller_is_never_the_owner_from_a_plugin_or_module_process(self):
        from core import modcli
        with mock.patch.dict(os.environ, {"HUB_MODULE": "m"}):
            os.environ.pop("HUB_EXTENSION", None)
            self.assertEqual(modcli.caller(None), {"module": "m"})
            self.assertEqual(modcli.caller("x"), {"session": "x"})
        with mock.patch.dict(os.environ, {"HUB_EXTENSION": "p"}):
            self.assertEqual(modcli.caller(None), {"plugin": "p"})

    def test_caller_of_a_schedule_command_names_its_job(self):
        # a schedule command job runs as the owner without a session: a module that keeps items for a session (an
        # owner's reply goes to it) learns the job and finds its session, instead of an item no reply reaches
        from core import modcli
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_MODULE", "HUB_EXTENSION", "HUB_SCHEDULE_JOB")}
        with mock.patch.dict(os.environ, {**env, "HUB_SCHEDULE_JOB": "watch-print"}, clear=True):
            self.assertEqual(modcli.caller(None), {"owner": True, "schedule_job": "watch-print"})
            self.assertEqual(modcli.caller("x"), {"session": "x", "schedule_job": "watch-print"})
        # a plugin or module process is not the job's word: it gets no job, as bin/hub speaker() marks no fact for it
        with mock.patch.dict(os.environ, {**env, "HUB_SCHEDULE_JOB": "watch-print", "HUB_MODULE": "m"}, clear=True):
            self.assertEqual(modcli.caller(None), {"module": "m"})
        with mock.patch.dict(os.environ, {**env, "HUB_SCHEDULE_JOB": ""}, clear=True):
            self.assertEqual(modcli.caller(None), {"owner": True})
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(modcli.caller(None), {"owner": True})
            self.assertEqual(modcli.caller("x"), {"session": "x"})

    def test_help_lists_module_commands(self):
        s = self.stand()
        self.assertIn("module tasks", hub_cli(s, "--help").stdout)


class Doctor(unittest.TestCase):
    def test_without_modules(self):
        sd = tempfile.mkdtemp(prefix="modhooks-doc-")
        env = {**os.environ, "HUB_STATE_DIR": sd}
        env.pop("HUB_SESSION", None)
        r = subprocess.run([sys.executable, HUB, "doctor"], env=env, capture_output=True, text=True, timeout=60)
        self.assertIn("no modules", r.stdout)

    def test_module_checks_and_a_failing_one(self):
        s = Stand(self, points=["doctor"], caps=["doctor"])
        s.update(doctor={"do": "ok", "answer": [{"name": "base", "ok": True, "detail": "in place"},
                                                {"name": "key", "ok": False, "detail": "no key"}]})
        s.launch()
        s.until_ready()
        r = hub_cli(s, "doctor")
        self.assertIn("fake · base", r.stdout)
        self.assertIn("no key", r.stdout)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(s.calls("doctor")[0]["body"]["api"], version.MODULE_API)

    def test_silent_module_does_not_hold_the_doctor(self):
        s = Stand(self, points=["doctor"], caps=["doctor"])
        s.update(doctor={"do": "silent"})
        s.launch()
        s.until_ready()
        r = hub_cli(s, "doctor")
        self.assertLess(held_for(s, "doctor"), modrun.budget_of("doctor") + 8)
        self.assertIn("fake", r.stdout)
        self.assertIn("did not answer", r.stdout)

    def test_blocked_and_missing_program_are_named(self):
        s = Stand(self)
        manifest(s, run=["no-such-program-x", "main.py"])
        r = hub_cli(s, "doctor")
        self.assertIn("no-such-program-x", r.stdout)
        self.assertEqual(r.returncode, 1)

    def test_doctor_answer_shape(self):
        check = modrun.POINTS["doctor"]["check"]
        self.assertEqual(check([{"name": "a", "ok": True}], {"name": "m"}), [{"name": "a", "ok": True, "detail": ""}])
        for bad in ({}, [{"name": "a"}], [{"ok": True}], [1], [{"name": "a", "ok": "yes"}]):
            with self.assertRaises(modrun.BadResult, msg=bad):
                check(bad, {"name": "m"})


class Migrate(unittest.TestCase):
    def stand(self, budget=None):
        s = Stand(self, points=["migrate"], budget=budget)
        s.launch()
        s.until_ready()
        return s

    def relaunch(self, s, version):
        manifest(s, version=version)
        for p in s.procs:
            p.kill()
            p.wait()
        s.launch()

    def test_new_version_migrates_before_ready(self):
        s = self.stand()
        self.assertEqual(s.calls("migrate"), [])                     # the first install — no migration
        self.relaunch(s, "1.1.0")
        s.until_ready()
        self.assertEqual(s.calls("migrate")[0]["body"], {"from": "1.0.0", "to": "1.1.0"})
        self.assertEqual(s.rec()["version"], "1.1.0")

    def test_same_version_does_not_migrate(self):
        s = self.stand()
        for p in s.procs:
            p.kill()
            p.wait()
        s.launch()
        s.until_ready()
        self.assertEqual(s.calls("migrate"), [])

    def test_failed_migration_blocks_keeps_the_version_and_retries_next_load(self):
        s = self.stand()
        s.update(migrate={"do": "ok", "answer": {"error": "disk full"}})
        self.relaunch(s, "1.1.0")
        self.assertTrue(s.until(lambda: s.rec().get("state") == "blocked"), s.rec())
        self.assertEqual(s.rec()["version"], "1.0.0")
        self.assertNotIn("ready", s.rec())
        self.assertIn("disk full", s.rec()["reason"])
        self.assertEqual(len(s.events("module.ready")), 1)
        s.update(migrate={"do": "ok", "answer": {}})
        s.hub.record("core.started", data={"host": "stream"})        # a new boot — a new attempt
        self.relaunch(s, "1.1.0")
        s.until_ready()
        self.assertEqual(len(s.calls("migrate")), 2)
        self.assertEqual(s.rec()["version"], "1.1.0")

    def test_silent_migration_is_a_failure_in_budget(self):
        s = self.stand(budget={"migrate": 1})   # the module's own budget, not the point's 10 s: the same failure sooner
        s.update(migrate={"do": "silent"})
        self.relaunch(s, "1.1.0")
        self.assertTrue(s.until(lambda: s.rec().get("state") == "blocked", timeout=1 + 15), s.rec())

    def test_the_migration_waits_the_modules_own_budget_or_else_the_points(self):
        # what the silent test no longer waits for: a module without a budget of its own gets the point's (at most 10 s)
        for budget, want in ((None, modrun.budget_of("migrate")), ({"migrate": 1}, 1)):
            s = self.stand(budget=budget)
            got, real = [], modrun.post

            def post(port, secret, path, payload, seconds, **kw):
                if path != "/hook/migrate":
                    return real(port, secret, path, payload, seconds, **kw)
                got.append(seconds)
                return "timeout", "no answer"   # as the silent module, without waiting for it
            with mock.patch.object(modrun, "post", post):
                self.relaunch(s, "1.1.0")
                self.assertTrue(s.until(lambda: s.rec().get("state") == "blocked"), s.rec())
            self.assertEqual(got[:1], [want], budget)
        self.assertLessEqual(modrun.budget_of("migrate"), 10)


class HubmodAndExample(unittest.TestCase):
    """The hubmod helper: hello from module.json, the secret, points as functions; the examples/module-hello example module is built on it."""

    def stand(self):
        s = Stand(self, name="hello")
        shutil.rmtree(os.path.join(s.root, "hello"))
        shutil.copytree(EXAMPLE, os.path.join(s.root, "hello"))
        manifest(s, name="hello", run=[sys.executable, "main.py"])
        with mock.patch.dict(os.environ, {"HUB_REPO": ROOT, "PYTHONPATH": ROOT}):
            s.launch("hello")
        s.until_ready("hello")
        return s

    def test_example_answers_its_points(self):
        s = self.stand()
        self.assertEqual(sorted(s.rec("hello")["points"]), ["cli", "doctor", "launch.env"])
        mods = s.hub.modules.mods
        got = mods.call("hello", "launch.env", {"session": "maps"}, ctx={"env": []})
        self.assertEqual(got["env"], {"HUB_MOD_HELLO_GREETING": "hello, maps"})
        self.assertEqual(mods.call("hello", "doctor", {"api": "2.0"})[0]["ok"], True)
        out = mods.call("hello", "cli", {"command": "hello", "args": ["world"], "caller": {"owner": True}})
        self.assertEqual(out, {"out": "hello, world\n", "err": "", "code": 0})
        self.assertEqual(s.events("module.error"), [])

    def test_wrong_secret_is_refused(self):
        s = self.stand()
        run = modrun.run_of(s.state_dir, "hello")
        self.assertEqual(modrun.post(run["port"], "wrong", "/hook/doctor", {}, 2)[0], "status")

    def test_fact_goes_to_the_spool_as_the_module(self):
        sd = tempfile.mkdtemp(prefix="modhooks-fact-")
        with mock.patch.dict(os.environ, {"HUB_STATE_DIR": sd, "HUB_MODULE": "hello"}):
            hubmod.fact("owner.notify", {"text": "done"})
        spooled = []
        for n in os.listdir(os.path.join(sd, "spool")):
            with open(os.path.join(sd, "spool", n), encoding="utf-8") as f:
                spooled.append(json.load(f))
        self.assertEqual(spooled[0]["type"], "owner.notify")
        self.assertEqual({k: spooled[0]["data"][k] for k in ("text", "channel", "owner", "sender")},
                         {"text": "done", "channel": "plugin", "owner": False, "sender": "mod:hello"})
        # as `hub` from a module process (bin/hub OUTSIDER): a module does not answer session permissions on the owner's behalf
        from core import reducer
        st = reducer.empty()
        why = reducer.reduce(st, {"seq": 1, "ts": 1, "type": "permission.answered", "entity": "a",
                                  "data": spooled[0]["data"]})[2]
        self.assertIn("not a module process", why)


if __name__ == "__main__":
    unittest.main()
