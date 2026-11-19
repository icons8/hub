"""`hub remote cloudflare setup|remove|rotate` (modules/cloudflare/setup.py) and cloudflared as a part of the hub
(modules/cloudflare/cloudflared.py): the hub makes its own tunnel, DNS name and Access application in the owner's
Cloudflare account by their API token, keeps only the tunnel's token, undoes a half-made setup, and runs cloudflared
as the cloudflare part's child. Cloudflare here is a fake API server on 127.0.0.1 answering as its documentation
says; cloudflared is a fake program — nothing reaches Cloudflare or GitHub."""
import tests  # noqa: F401 — so the machine's settings are not seen
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import platforms
from core import fetch, i18n
from modules.cloudflare import api as cfapi, cli, cloudflared, remote, setup
from modules.cloudflare.run import Cloudflare
from tests.fakes import exe

GOOD = "cf-api-" + "k" * 33   # the owner's API token (made up)
ACCOUNT = "0123456789abcdef0123456789abcdef"
ZONE = "fedcba9876543210fedcba9876543210"
HUB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")


class FakeCloudflare:
    """The parts of the Cloudflare API v4 setup uses, with what each call created; `fail` — (method, path regex) ->
    HTTP status for the matching calls; `zones`, `team` — what the account has."""

    def __init__(self):
        self.calls, self.made, self.fail, self.lock = [], {}, {}, threading.Lock()
        self.zones = [{"id": ZONE, "name": "example.test", "status": "active", "account": {"id": ACCOUNT, "name": "x"}}]
        self.team = "myteam.cloudflareaccess.com"
        self.taken = set()          # DNS names already in the zone
        self.no_token_in_create = False
        self.configs = {}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def handle_one(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                u = urlsplit(self.path)
                status, result = fake.answer(self.command, u.path.removeprefix("/client/v4"), parse_qs(u.query), body,
                                             self.headers.get("Authorization"))
                out = json.dumps({"success": status < 300, "errors": [] if status < 300 else result,
                                  "messages": [], "result": result if status < 300 else None}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = handle_one

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/client/v4"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def alive(self):
        """What is still in the account: [(kind, id)]."""
        return sorted((k, i) for k, items in self.made.items() for i in items)

    def new(self, kind, **extra):
        rid = str(uuid.uuid4()) if kind == "tunnel" else uuid.uuid4().hex
        self.made.setdefault(kind, {})[rid] = extra
        return {"id": rid, **extra}

    def answer(self, method, path, query, body, auth):
        with self.lock:
            self.calls.append((method, path, body))
            if auth != f"Bearer {GOOD}":
                return 403, [{"code": 10000, "message": "Authentication error"}]
            for (m, pattern), status in self.fail.items():
                if m == method and re.search(pattern, path):
                    return status, [{"code": 1000, "message": f"made to fail: {method} {path}"}]
            return self.route(method, path, query, body)

    def route(self, method, path, query, body):
        acc = f"/accounts/{ACCOUNT}"
        if method == "GET" and path == "/zones":
            return 200, self.zones if query.get("page") == ["1"] else []
        if method == "GET" and path == f"{acc}/access/organizations":
            return (200, {"auth_domain": self.team, "name": "myteam"}) if self.team else \
                (404, [{"code": 7003, "message": "organization not found"}])
        if method == "GET" and path == f"/zones/{ZONE}/dns_records":
            name = query.get("name", [""])[0]
            hit = [r for r in self.made.get("dns", {}).values() if r["name"] == name] or \
                ([{"name": name}] if name in self.taken else [])
            return 200, hit
        if method == "POST" and path == f"{acc}/access/policies":
            return 200, self.new("policy", decision=body["decision"], include=body["include"])
        if method == "POST" and path == f"{acc}/access/service_tokens":
            return 200, self.new("service_token", client_id=uuid.uuid4().hex + ".access",
                                 client_secret=uuid.uuid4().hex * 2, name=body["name"])
        if method == "POST" and path == f"{acc}/access/apps":
            pids = [p["id"] for p in body["policies"]]
            if any(p not in self.made.get("policy", {}) for p in pids):
                return 400, [{"code": 12130, "message": "policy not found"}]
            return 200, self.new("app", aud=uuid.uuid4().hex * 2, domain=body["domain"], policies=pids,
                                 session_duration=body["session_duration"])
        if method == "POST" and path == f"{acc}/cfd_tunnel":
            got = self.new("tunnel", name=body["name"])
            got["token"] = None if self.no_token_in_create else "eyJhIjoi" + uuid.uuid4().hex * 3
            self.made["tunnel"][got["id"]]["token"] = got["token"] or "eyJhIjoi" + uuid.uuid4().hex * 3
            return 200, got
        m = re.fullmatch(rf"{acc}/cfd_tunnel/([0-9a-f-]+)(/configurations|/token|/connections)?", path)
        if m:
            tid, what = m.groups()
            if tid not in self.made.get("tunnel", {}):
                return 404, [{"code": 1003, "message": "tunnel not found"}]
            if method == "PUT" and what == "/configurations":
                self.configs[tid] = body["config"]
                return 200, {"tunnel_id": tid, "config": body["config"]}
            if method == "GET" and what == "/token":
                return 200, self.made["tunnel"][tid]["token"]
            if method == "DELETE" and what == "/connections":
                return 200, None
            if method == "PATCH" and what is None:
                self.made["tunnel"][tid]["token"] = "eyJhIjoi" + hashlib.sha256(body["tunnel_secret"].encode()).hexdigest()
                return 200, {"id": tid}
            if method == "DELETE" and what is None:
                del self.made["tunnel"][tid]
                return 200, {"id": tid}
        if method == "POST" and path == f"/zones/{ZONE}/dns_records":
            return 200, self.new("dns", name=body["name"], type=body["type"], content=body["content"],
                                 proxied=body["proxied"])
        m = re.fullmatch(rf"{acc}/access/service_tokens/([0-9a-f]+)/rotate", path)
        if m and method == "POST" and m.group(1) in self.made.get("service_token", {}):
            self.made["service_token"][m.group(1)]["client_secret"] = "new" + uuid.uuid4().hex
            return 200, {"id": m.group(1), "client_secret": self.made["service_token"][m.group(1)]["client_secret"]}
        kinds = {f"/zones/{ZONE}/dns_records/": "dns", f"{acc}/access/apps/": "app",
                 f"{acc}/access/policies/": "policy", f"{acc}/access/service_tokens/": "service_token"}
        for prefix, kind in kinds.items():
            if method == "DELETE" and path.startswith(prefix):
                rid = path[len(prefix):]
                if rid not in self.made.get(kind, {}):
                    return 404, [{"code": 1004, "message": f"{kind} not found"}]
                if kind == "policy" and any(rid in a["policies"] for a in self.made.get("app", {}).values()):
                    return 400, [{"code": 12131, "message": "policy is used by an application"}]
                del self.made[kind][rid]
                return 200, {"id": rid}
        return 404, [{"code": 7000, "message": f"no route {method} {path}"}]


class Ui:
    """The owner at a terminal, as a test: answers to questions in turn, the token."""

    def __init__(self, token=GOOD, answers=(), tty=False):
        self.token, self.answers, self.tty, self.said = token, list(answers), tty, []

    def say(self, text):
        self.said.append(text)

    def ask(self, prompt):
        self.said.append("<ask>")
        return self.answers.pop(0) if self.answers else None

    def secret(self, prompt):
        self.said.append("<secret>")
        return self.token

    def text(self):
        return "\n".join(self.said)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cf-setup-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.conf = tempfile.mkdtemp(prefix="cf-setup-conf-")
        self.addCleanup(shutil.rmtree, self.conf, True)
        self.cf = FakeCloudflare()
        self.addCleanup(self.cf.close)
        env = mock.patch.dict(os.environ, {"HUB_CONFIG": os.path.join(self.conf, "hub.json"),
                                           "HUB_CLOUDFLARE_API": self.cf.base, "HUB_WEB_PORT": "8790"})
        env.start()
        self.addCleanup(env.stop)
        self.binary = os.path.join(self.dir, "cloudflared-fake")

    def ensure(self, state_dir, say=None):
        return self.binary

    def setup_(self, *argv, ui=None):
        ui = ui or Ui()
        try:
            code = setup.run_setup(self.dir, list(argv), ui, ensure=self.ensure)
        except setup.Stop as e:
            ui.say(str(e))
            code = 1
        return code, ui

    def settings(self):
        return remote.load_settings(self.dir)

    def every_file(self):
        for root in (self.dir, self.conf):
            for folder, _, files in os.walk(root):
                for name in files:
                    with open(os.path.join(folder, name), "rb") as f:
                        yield os.path.join(folder, name), f.read()


class Setup(Base):
    def test_one_command_makes_the_tunnel_name_and_access_and_keeps_no_api_token(self):
        code, ui = self.setup_("--email", "Me@Example.test")
        self.assertEqual(code, 0, ui.text())
        s = self.settings()
        host = urlsplit(s["url"]).hostname
        self.assertRegex(host, r"^hub-[a-z0-9]{8}\.example\.test$")
        norm, missing = remote.normalized(s)
        self.assertEqual(missing, [])
        self.assertTrue(norm["enabled"])
        self.assertEqual(norm["team"], "myteam.cloudflareaccess.com")
        self.assertEqual(norm["emails"], ["me@example.test"])
        self.assertEqual(norm["service_tokens"], [])
        [(app_id, app)] = self.cf.made["app"].items()
        self.assertEqual((app["domain"], app["session_duration"], s["aud"]), (host, "24h", [app["aud"]]))
        [(policy_id, policy)] = self.cf.made["policy"].items()
        self.assertEqual((policy["decision"], policy["include"]), ("allow", [{"email": {"email": "Me@Example.test"}}]))
        self.assertEqual(app["policies"], [policy_id])
        [(tunnel_id, tunnel)] = self.cf.made["tunnel"].items()
        [dns] = self.cf.made["dns"].values()
        self.assertEqual((dns["name"], dns["type"], dns["content"], dns["proxied"]),
                         (host, "CNAME", f"{tunnel_id}.cfargotunnel.com", True))
        ingress = self.cf.configs[tunnel_id]["ingress"]
        self.assertEqual(ingress[0]["hostname"], host)
        self.assertEqual(ingress[0]["service"], "http://127.0.0.1:8790")   # this hub's page port, Host left as is
        self.assertNotIn("httpHostHeader", ingress[0].get("originRequest", {}))
        self.assertEqual(ingress[0]["originRequest"]["access"],
                         {"required": True, "teamName": "myteam", "audTag": [app["aud"]]})
        self.assertEqual(ingress[-1], {"service": "http_status:404"})
        m = s["managed"]
        self.assertEqual((m["tunnel"], m["app"], m["policies"], m["account"], m["zone"], m["host"]),
                         (tunnel_id, app_id, [policy_id], ACCOUNT, ZONE, host))
        kept = setup.load_secrets(self.dir)
        self.assertEqual(kept["tunnel_token"], tunnel["token"])
        if os.name != "nt":
            for path in (setup.secrets_path(self.dir), remote.settings_path(self.dir)):
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600, path)
        for path, data in self.every_file():
            self.assertNotIn(GOOD.encode(), data, path)
        self.assertTrue(os.path.exists(os.path.join(self.dir, remote.REFRESH)))   # the part wakes up at once
        self.assertIn(f"https://{host}", ui.text())

    def test_a_service_token_for_a_native_client(self):
        code, ui = self.setup_("--email", "me@example.test", "--service-token")
        self.assertEqual(code, 0, ui.text())
        [(tid, token)] = self.cf.made["service_token"].items()
        self.assertEqual(self.settings()["service_tokens"], [token["client_id"]])
        policies = {p["decision"]: p for p in self.cf.made["policy"].values()}
        self.assertEqual(policies["non_identity"]["include"], [{"service_token": {"token_id": tid}}])   # Service Auth
        self.assertEqual(sorted(policies), ["allow", "non_identity"])
        self.assertEqual(setup.load_secrets(self.dir)["service_secret"], token["client_secret"])
        self.assertNotIn(token["client_secret"], ui.text())   # not a terminal: the path, not the secret
        self.assertIn(setup.secrets_path(self.dir), ui.text())
        self.assertEqual(setup.run_remove(self.dir, Ui()), 0)
        code, ui = self.setup_("--email", "me@example.test", "--service-token", ui=Ui(tty=True))   # a terminal: shown once
        self.assertEqual(code, 0, ui.text())
        [token] = self.cf.made["service_token"].values()
        self.assertIn(token["client_secret"], ui.text())

    def test_the_page_port_the_part_saw_wins(self):
        with open(os.path.join(self.dir, remote.STATUS), "w") as f:
            json.dump({"page_port": 8801}, f)
        self.assertEqual(self.setup_("--email", "me@example.test")[0], 0)
        [config] = self.cf.configs.values()
        self.assertEqual(config["ingress"][0]["service"], "http://127.0.0.1:8801")
        self.assertEqual(self.settings()["managed"]["port"], 8801)

    def test_a_failure_at_any_step_undoes_what_the_run_made_and_a_repeat_is_safe(self):
        steps = [("POST", r"/access/policies$"), ("POST", r"/access/service_tokens$"), ("POST", r"/access/apps$"),
                 ("POST", r"/cfd_tunnel$"), ("PUT", r"/configurations$"), ("GET", r"/cfd_tunnel/.*/token$"),
                 ("POST", r"/dns_records$")]
        for step in steps:
            with self.subTest(step):
                self.cf.made, self.cf.fail = {}, {step: 500}
                self.cf.no_token_in_create = step[0] == "GET"
                code, ui = self.setup_("--email", "me@example.test", "--service-token")
                self.assertEqual(code, 1, ui.text())
                self.assertEqual(self.cf.alive(), [], ui.text())
                self.assertNotIn("managed", self.settings())
                self.assertNotIn("orphans", self.settings())
                self.assertFalse(self.settings().get("enabled"))
                self.assertFalse(os.path.exists(setup.secrets_path(self.dir)))
                if step != steps[0]:   # the first step made nothing to undo
                    self.assertIn(i18n.t("cloudflare.setup.undone"), ui.text())
        self.cf.fail, self.cf.made = {}, {}
        code, ui = self.setup_("--email", "me@example.test")
        self.assertEqual(code, 0, ui.text())

    def test_ctrl_c_in_the_middle_undoes_and_a_killed_run_leaves_a_record(self):
        seen = []

        class Api(cfapi.Api):
            def call(api, method, path, body=None, query=None):
                if method == "POST" and path.endswith("/cfd_tunnel"):
                    seen.append([x["kind"] for x in self.settings().get("orphans") or []])
                    raise KeyboardInterrupt
                return super().call(method, path, body, query)
        ui = Ui()
        with self.assertRaises(KeyboardInterrupt):
            setup.run_setup(self.dir, ["--email", "me@example.test"], ui, api_for=Api, ensure=self.ensure)
        self.assertIn(i18n.t("cloudflare.setup.undone"), ui.text())   # the owner hears what is left before the interrupt
        self.assertIn(i18n.t("cloudflare.setup.interrupted"), ui.text())
        self.assertEqual(seen, [["policy", "app"]])   # written down before the next step: a kill leaves a record
        self.assertEqual(self.cf.alive(), [])
        self.assertNotIn("orphans", self.settings())

    def test_the_owners_own_tunnel_is_kept_and_comes_back_on_remove(self):
        own = {"enabled": True, "team": "own", "aud": ["b" * 64], "url": "https://mine.example.test",
               "emails": ["own@example.test"], "metrics": "127.0.0.1:20241"}
        remote.save_settings(self.dir, dict(own))
        code, ui = self.setup_("--email", "me@example.test")
        self.assertEqual(code, 0, ui.text())
        self.assertIn("https://mine.example.test", ui.text())
        self.assertEqual(setup.run_remove(self.dir, Ui()), 0)
        self.assertEqual(self.settings(), {**own, "enabled": False})

    def test_saving_the_result_failing_undoes_too(self):
        with mock.patch.object(setup, "save_secrets", side_effect=OSError("disk full")):
            code, ui = self.setup_("--email", "me@example.test")
        self.assertEqual(code, 1)
        self.assertEqual(self.cf.alive(), [])

    def test_what_could_not_be_undone_is_kept_by_id_and_remove_deletes_it(self):
        self.cf.fail = {("POST", r"/dns_records$"): 500, ("DELETE", r"/cfd_tunnel/[0-9a-f-]+$"): 500}
        code, ui = self.setup_("--email", "me@example.test")
        self.assertEqual(code, 1)
        self.assertEqual([k for k, _ in self.cf.alive()], ["tunnel"])
        [orphan] = self.settings()["orphans"]
        self.assertEqual((orphan["kind"], orphan["in"]), ("tunnel", ACCOUNT))
        self.assertIn("hub remote cloudflare remove", ui.text())
        self.cf.fail = {}
        self.assertEqual(setup.run_remove(self.dir, Ui()), 0)
        self.assertEqual(self.cf.alive(), [])
        self.assertNotIn("orphans", self.settings())

    def test_refusals_name_the_permission_the_step_needs(self):
        code, ui = self.setup_("--email", "me@example.test", ui=Ui(token="wrong-token"))
        self.assertEqual(code, 1)
        self.assertIn(setup.PERMISSIONS["zones"], ui.text())
        self.cf.fail = {("POST", r"/cfd_tunnel$"): 403}
        code, ui = self.setup_("--email", "me@example.test")
        self.assertIn(setup.PERMISSIONS["tunnel"], ui.text())
        self.assertEqual(self.cf.alive(), [])

    def test_without_a_zero_trust_team_nothing_is_made(self):
        self.cf.team = None
        code, ui = self.setup_("--email", "me@example.test")
        self.assertEqual(code, 1)
        self.assertIn(setup.ZERO_TRUST, ui.text())
        self.assertEqual([c for c in self.cf.calls if c[0] != "GET"], [])

    def test_several_domains_are_a_choice(self):
        self.cf.zones.append({"id": "a" * 32, "name": "other.test", "status": "active", "account": {"id": ACCOUNT}})
        code, ui = self.setup_("--email", "me@example.test")   # no terminal, no --domain
        self.assertEqual(code, 1)
        self.assertIn("other.test", ui.text())
        code, ui = self.setup_("--email", "me@example.test", ui=Ui(answers=["2"]))   # sorted: example, other
        self.assertEqual(code, 1, ui.text())   # other.test is not the fake's zone for writes: refused, undone
        self.assertEqual(self.cf.alive(), [])
        code, ui = self.setup_("--email", "me@example.test", "--domain", "Example.test.")
        self.assertEqual(code, 0, ui.text())

    def test_the_email_is_asked_before_the_token(self):
        code, ui = self.setup_(ui=Ui(answers=["me@example.test, two@example.test"]))
        self.assertEqual(code, 0, ui.text())
        self.assertEqual(self.settings()["emails"], ["me@example.test", "two@example.test"])
        self.assertLess(ui.said.index("<ask>"), ui.said.index("<secret>"))   # the token last, held for the shortest time

    def test_no_email_no_binary_or_a_set_up_hub_ask_nothing_of_cloudflare(self):
        code, ui = self.setup_()
        self.assertEqual(code, 1)
        self.assertIn("--email", ui.text())
        with mock.patch.object(self, "ensure", side_effect=fetch.Failed("checksum", "bad file")):
            code, ui = self.setup_("--email", "me@example.test")
        self.assertEqual(code, 1)
        self.assertIn("bad file", ui.text())
        self.assertEqual(self.cf.calls, [])
        self.assertEqual(self.setup_("--email", "me@example.test")[0], 0)
        code, ui = self.setup_("--email", "me@example.test")
        self.assertEqual(code, 1)
        self.assertIn("remove", ui.text())
        self.assertEqual(len(self.cf.made["tunnel"]), 1)

    def test_a_taken_name_is_not_reused(self):
        names = iter("aaaaaaaa" + "bbbbbbbb")
        self.cf.taken = {"hub-aaaaaaaa.example.test"}
        code = setup.run_setup(self.dir, ["--email", "me@example.test"], Ui(), ensure=self.ensure,
                               rand=lambda chars: next(names))
        self.assertEqual(code, 0)
        self.assertEqual(self.settings()["url"], "https://hub-bbbbbbbb.example.test")

    def test_bad_answers_from_the_api_are_not_used_in_paths(self):
        orig = self.cf.new

        def odd(kind, **extra):
            got = orig(kind, **extra)
            return {**got, "id": "../../zones"} if kind == "app" else got
        self.cf.new = odd
        code, ui = self.setup_("--email", "me@example.test")
        self.assertEqual(code, 1)
        self.assertFalse([c for c in self.cf.calls if ".." in c[1]])


class Remove(Base):
    def test_remove_deletes_only_what_setup_made_and_a_second_time_is_safe(self):
        self.assertEqual(self.setup_("--email", "me@example.test", "--service-token")[0], 0)
        self.cf.new("tunnel", name="someone-else")   # not the hub's: stays
        self.cf.calls.clear()
        ui = Ui()
        self.assertEqual(setup.run_remove(self.dir, ui), 0, ui.text())
        self.assertEqual([k for k, _ in self.cf.alive()], ["tunnel"])
        deletes = [p for m, p, _ in self.cf.calls if m == "DELETE"]
        connections = next(i for i, p in enumerate(deletes) if p.endswith("/connections"))
        self.assertTrue(deletes[connections + 1].endswith(deletes[connections].removesuffix("/connections")))
        self.assertLess(next(i for i, p in enumerate(deletes) if "/access/apps/" in p),
                        next(i for i, p in enumerate(deletes) if "/access/policies/" in p))
        s = self.settings()
        self.assertFalse(s["enabled"])
        self.assertNotIn("managed", s)
        self.assertNotIn("url", s)
        self.assertFalse(os.path.exists(setup.secrets_path(self.dir)))
        ui = Ui()
        self.assertEqual(setup.run_remove(self.dir, ui), 0)
        self.assertIn(i18n.t("cloudflare.setup.nothing"), ui.text())

    def test_already_deleted_in_the_dashboard_counts_as_deleted(self):
        self.assertEqual(self.setup_("--email", "me@example.test")[0], 0)
        self.cf.made["dns"].clear()
        self.assertEqual(setup.run_remove(self.dir, Ui()), 0)
        self.assertEqual(self.cf.alive(), [])

    def test_remove_asks_the_token_before_turning_anything_off(self):
        self.assertEqual(self.setup_("--email", "me@example.test")[0], 0)
        self.assertEqual(setup.main("remove", [], self.dir, ui=Ui(token="")), 1)   # changed their mind at the prompt
        self.assertTrue(self.settings()["enabled"])
        self.assertEqual(setup.main("remove", ["--orphans"], self.dir, ui=Ui()), 1)   # no such option
        self.assertTrue(self.settings()["enabled"])
        waited = []
        with open(os.path.join(self.dir, cloudflared.PIDFILE), "w") as f:
            json.dump({"pid": 0}, f)   # the part runs the hub's cloudflared

        class Seen(Ui):
            def secret(ui, prompt):
                waited.append(("token", self.settings()["enabled"]))
                return GOOD
        self.assertEqual(setup.run_remove(self.dir, Seen(), wait=lambda d: waited.append(("wait", self.settings()["enabled"]))), 0)
        self.assertEqual(waited, [("token", True), ("wait", False)])   # off, then wait for cloudflared, then delete

    def test_a_wrong_token_on_remove_keeps_everything(self):
        self.assertEqual(self.setup_("--email", "me@example.test")[0], 0)
        self.assertEqual(setup.run_remove(self.dir, Ui(token="wrong-token")), 1)
        s = self.settings()
        self.assertFalse(s["enabled"])           # the tunnel is stopped even though the token was wrong
        self.assertIn("managed", s)              # and nothing is forgotten: remove again
        self.assertEqual(setup.run_remove(self.dir, Ui()), 0)
        self.assertEqual(self.cf.alive(), [])


class Rotate(Base):
    def test_rotate_gives_the_tunnel_a_new_token(self):
        self.assertEqual(self.setup_("--email", "me@example.test", "--service-token")[0], 0)
        before = setup.load_secrets(self.dir)
        self.assertEqual(setup.run_rotate(self.dir, [], Ui()), 0)
        after = setup.load_secrets(self.dir)
        self.assertNotEqual(after["tunnel_token"], before["tunnel_token"])
        self.assertEqual(after["service_secret"], before["service_secret"])
        [tunnel] = self.cf.made["tunnel"].values()
        self.assertEqual(after["tunnel_token"], tunnel["token"])
        self.assertEqual(setup.run_rotate(self.dir, ["--service-token"], Ui()), 0)
        [token] = self.cf.made["service_token"].values()
        self.assertEqual(setup.load_secrets(self.dir)["service_secret"], token["client_secret"])
        for path, data in self.every_file():
            self.assertNotIn(GOOD.encode(), data, path)

    def test_a_failure_after_the_secret_changed_says_to_rotate_again(self):
        self.assertEqual(self.setup_("--email", "me@example.test")[0], 0)
        self.cf.fail = {("GET", r"/token$"): 500}
        with self.assertRaises(setup.Stop) as got:
            setup.run_rotate(self.dir, [], Ui())
        self.assertIn("hub remote cloudflare rotate", str(got.exception))
        self.assertIn(i18n.t("cloudflare.setup.rotate_half", why="X").split("(")[0], str(got.exception))

    def test_a_failed_service_secret_says_the_tunnel_part_is_done(self):
        self.assertEqual(self.setup_("--email", "me@example.test", "--service-token")[0], 0)
        self.cf.fail = {("POST", r"/rotate$"): 500}
        before = setup.load_secrets(self.dir)
        with self.assertRaises(setup.Stop) as got:
            setup.run_rotate(self.dir, ["--service-token"], Ui())
        self.assertIn("rotate --service-token", str(got.exception))
        self.assertNotEqual(setup.load_secrets(self.dir)["tunnel_token"], before["tunnel_token"])

    def test_rotate_without_setup_or_service_token(self):
        with self.assertRaises(setup.Stop):
            setup.run_rotate(self.dir, [], Ui())
        self.assertEqual(self.setup_("--email", "me@example.test")[0], 0)
        with self.assertRaises(setup.Stop):
            setup.run_rotate(self.dir, ["--service-token"], Ui())
        self.assertEqual([c for c in self.cf.calls if c[0] == "PATCH"], [])


class Copy(Base):
    """A copy of the hub (another state folder with the settings copied) neither runs, changes nor deletes the tunnel."""

    def test_a_copy_only_forgets(self):
        self.assertEqual(self.setup_("--email", "me@example.test", "--service-token")[0], 0)
        copy_state = tempfile.mkdtemp(prefix="cf-copy-")
        self.addCleanup(shutil.rmtree, copy_state, True)
        copy_conf = tempfile.mkdtemp(prefix="cf-copy-conf-")
        self.addCleanup(shutil.rmtree, copy_conf, True)
        for name in (remote.SETTINGS, setup.SECRETS):
            shutil.copy(os.path.join(self.conf, name), os.path.join(copy_conf, name))
        before = self.cf.alive()
        self.cf.calls.clear()
        with mock.patch.dict(os.environ, {"HUB_CONFIG": os.path.join(copy_conf, "hub.json")}):
            self.assertTrue(setup.copied(copy_state))
            self.assertEqual(setup.load_secrets(copy_state), {})
            with self.assertRaises(setup.Stop):
                setup.run_rotate(copy_state, [], Ui())
            with self.assertRaises(setup.Stop):
                setup.run_setup(copy_state, ["--email", "me@example.test"], Ui(), ensure=self.ensure)
            ui = Ui()
            self.assertEqual(setup.run_remove(copy_state, ui), 0)
            self.assertIn(i18n.t("cloudflare.setup.forgot_copy"), ui.text())
            self.assertNotIn("managed", remote.load_settings(copy_state))
            self.assertEqual(setup.load_secrets(copy_state), {})   # the other hub's token is never this one's
            self.assertFalse(setup.copied(copy_state))   # now an ordinary hub: its own setup may run
        self.assertEqual(self.cf.calls, [])
        self.assertEqual(self.cf.alive(), before)
        self.assertIn("managed", self.settings())   # the original is untouched


class Token(unittest.TestCase):
    def test_the_api_token_goes_to_cloudflare_or_to_this_machine_only(self):
        for own, base in (("https://api.example.test/client/v4", cfapi.BASE), ("", cfapi.BASE),
                          ("http://127.0.0.1:5/client/v4", "http://127.0.0.1:5/client/v4")):
            with mock.patch.dict(os.environ, {"HUB_CLOUDFLARE_API": own}):
                self.assertEqual(cfapi.Api("x").base, base, own)
        self.assertNotIn("secret-value", repr(cfapi.Api("secret-value")))

    def test_a_redirect_is_not_followed(self):
        class Moved(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:9/elsewhere")
                self.send_header("Content-Length", "0")
                self.end_headers()
        server = ThreadingHTTPServer(("127.0.0.1", 0), Moved)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with mock.patch.dict(os.environ, {"HUB_CLOUDFLARE_API": f"http://127.0.0.1:{server.server_address[1]}"}):
            with self.assertRaises(cfapi.Failed) as got:
                cfapi.Api("x").call("GET", "/zones")
        self.assertEqual(got.exception.status, 302)

    def test_the_tunnel_token_is_masked_in_the_journal(self):
        from core import redact
        token = "eyJhIjoi" + "MDEyMzQ1Njc4OWFiY2RlZiIsInQiOiIx" * 3
        self.assertNotIn(token, redact.mask(f"TUNNEL_TOKEN={token}"))


class Command(Base):
    def hub(self, *args, me=None):
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE")}
        env.update({"HUB_STATE_DIR": self.dir, "HUB_CLI_WAIT": "0", **({"HUB_SESSION": me} if me else {})})
        return subprocess.run([sys.executable, HUB, "remote", "cloudflare", *args], env=env, capture_output=True,
                              text=True, encoding="utf-8", timeout=60, input="")

    def test_a_session_never_handles_the_api_token(self):
        for args in (("setup", "--email", "me@example.test"), ("remove",), ("rotate",)):
            got = self.hub(*args, me="maps")
            self.assertNotEqual(got.returncode, 0, args)
            self.assertIn(i18n.tr("en", "cloudflare.setup.only_terminal"), got.stderr, args)   # a session reads English
        self.assertEqual(self.cf.calls, [])

    def test_in_the_owner_terminal_remove_with_nothing_made(self):
        got = self.hub("remove")
        self.assertEqual(got.returncode, 0, got.stderr + got.stdout)
        self.assertIn(i18n.t("cloudflare.setup.nothing"), got.stdout)

    def test_status_names_the_tunnel_the_hub_made(self):
        self.assertEqual(self.setup_("--email", "me@example.test", "--service-token")[0], 0)
        out = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch.object(cli, "reach", return_value=("ok", "Access")):
            self.assertEqual(cli.main(["status"], self.dir), 0)
        m = self.settings()["managed"]
        self.assertIn(m["name"], out.getvalue())
        self.assertIn(setup.secrets_path(self.dir), out.getvalue())


FAKE_CLOUDFLARED = r'''
import http.server, json, os, sys, threading
args = sys.argv[1:]
port = int(args[args.index("--metrics") + 1].split(":")[1])
with open(os.environ["FAKE_CFD_LOG"], "a") as f:
    f.write(json.dumps({"pid": os.getpid(), "args": args, "token": os.environ.get("TUNNEL_TOKEN")}) + "\n")
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def do_GET(self):
        self.send_response(200 if self.path == "/ready" else 404)
        self.end_headers()
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
'''


class Connector(unittest.TestCase):
    """cloudflared as the cloudflare part's child: the token in the environment, its own metrics port, one connector."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cf-connector-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.log = os.path.join(self.dir, "fake.log")
        self.binary = exe.script(os.path.join(self.dir, "cloudflared"), FAKE_CLOUDFLARED)
        env = mock.patch.dict(os.environ, {"FAKE_CFD_LOG": self.log})
        env.start()
        self.addCleanup(env.stop)
        self.now = 0.0

    def runs(self):
        try:
            with open(self.log) as f:
                return [json.loads(x) for x in f if x.strip()]
        except OSError:
            return []

    def connector(self):
        c = cloudflared.Connector(self.dir, clock=lambda: self.now)
        self.addCleanup(c.stop)
        return c

    def until(self, check, seconds=20):
        end = time.time() + seconds
        while time.time() < end:
            if check():
                return True
            time.sleep(0.1)
        return False

    def test_runs_with_the_token_in_the_environment_and_its_own_metrics(self):
        c = self.connector()
        self.assertTrue(c.want(self.binary, "tok-1"))
        self.assertTrue(self.until(lambda: self.runs()))
        [run] = self.runs()
        self.assertEqual(run["token"], "tok-1")
        self.assertNotIn("tok-1", " ".join(run["args"]))
        self.assertEqual(run["args"][:3], ["tunnel", "--no-autoupdate", "--management-diagnostics=false"])
        self.assertEqual(run["args"][-1], "run")
        self.assertEqual(c.metrics, run["args"][run["args"].index("--metrics") + 1])
        self.assertRegex(c.metrics, r"^127\.0\.0\.1:\d+$")
        self.assertTrue(self.until(lambda: __import__("modules.cloudflare.run", fromlist=["ready"]).ready(c.metrics)))
        c.want(self.binary, "tok-1")   # the same token: nothing restarts
        self.assertEqual(len(self.runs()), 1)
        c.want(self.binary, "tok-2")   # rotate: the old one stops, the new one starts
        self.assertTrue(self.until(lambda: len(self.runs()) == 2))
        self.assertEqual(self.runs()[1]["token"], "tok-2")
        self.assertTrue(self.until(lambda: not platforms.current.alive(self.runs()[0]["pid"])))
        c.stop()
        self.assertFalse(c.running())
        self.assertTrue(self.until(lambda: not platforms.current.alive(self.runs()[1]["pid"])))
        self.assertFalse(os.path.exists(os.path.join(self.dir, cloudflared.PIDFILE)))

    def test_an_exit_is_started_again_with_a_backoff(self):
        c = self.connector()
        c.want(self.binary, "tok")
        self.assertTrue(self.until(lambda: self.runs()))
        c.proc.kill()
        c.proc.wait(10)
        self.assertFalse(c.want(self.binary, "tok"))   # not at once
        self.now += cloudflared.BACKOFF[0] + 1
        self.assertTrue(c.want(self.binary, "tok"))
        self.assertTrue(self.until(lambda: len(self.runs()) == 2))

    @unittest.skipIf(os.name == "nt", "the pid file path on POSIX; Windows ends the child with the part's job")
    def test_a_child_left_by_a_killed_part_is_stopped_by_the_next(self):
        first = cloudflared.Connector(self.dir)
        first.want(self.binary, "tok")
        self.assertTrue(self.until(lambda: self.runs()))
        pid = first.proc.pid
        second = self.connector()   # the part started again; the first died without stop()
        self.assertTrue(self.until(lambda: first.proc.poll() is not None))
        self.assertIsNone(second.proc)
        self.assertFalse(platforms.current.alive(pid))
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)
        with open(os.path.join(self.dir, cloudflared.PIDFILE), "w") as f:
            json.dump({"pid": sleeper.pid}, f)   # the pid now belongs to another program: left alone
        cloudflared.Connector(self.dir)
        with self.assertRaises(subprocess.TimeoutExpired):
            sleeper.wait(2)

    @unittest.skipIf(os.name == "nt", "the pid file path on POSIX")
    def test_a_pid_file_of_another_hub_is_left_alone(self):
        first = self.connector()
        first.want(self.binary, "tok")
        self.assertTrue(self.until(lambda: self.runs()))
        other = tempfile.mkdtemp(prefix="cf-other-")
        self.addCleanup(shutil.rmtree, other, True)
        shutil.copy(os.path.join(self.dir, cloudflared.PIDFILE), os.path.join(other, cloudflared.PIDFILE))
        cloudflared.Connector(other)   # a copy of the hub, started on the same machine
        with self.assertRaises(subprocess.TimeoutExpired):
            first.proc.wait(2)

    def test_the_log_starts_over_under_a_long_run(self):
        c = self.connector()
        os.makedirs(os.path.dirname(c.log()), exist_ok=True)
        with open(c.log(), "wb") as f:
            f.write(b"x" * 100)
        with mock.patch.object(cloudflared, "LOG_MAX", 10):
            c.want(self.binary, "tok")
        self.assertLess(os.path.getsize(c.log()), 100)


class Part(Base):
    """The cloudflare part (run.py) runs the hub's own cloudflared while the provider is on, never in a copy of the hub."""

    class FakeConnector:
        def __init__(self):
            self.wanted, self.stopped, self.metrics = [], 0, "127.0.0.1:1"

        def want(self, binary, token):
            self.wanted.append((binary, token))
            return True

        def running(self):
            return bool(self.wanted)

        def stop(self):
            self.stopped += 1

    def part(self):
        self.conn = self.FakeConnector()
        return Cloudflare(self.dir, fetch=lambda url: {"keys": []}, ready=lambda addr: addr == "127.0.0.1:1",
                          clock=time.time, connector=self.conn)

    def test_runs_while_on_stops_when_off(self):
        open(self.binary, "w").close()
        self.assertEqual(self.setup_("--email", "me@example.test")[0], 0)
        cf = self.part()
        cf.keep()
        self.assertEqual(self.conn.wanted, [(self.binary, setup.load_secrets(self.dir)["tunnel_token"])])
        self.assertEqual(cf.tick()["tunnel"], "up")
        self.assertEqual(cf.tick()["page_port"], 8790)
        cli.switch(self.dir, False)
        cf.keep()
        self.assertEqual(self.conn.stopped, 1)

    def test_a_copy_of_the_hub_does_not_run_the_tunnel(self):
        open(self.binary, "w").close()
        self.assertEqual(self.setup_("--email", "me@example.test")[0], 0)
        with open(setup.secrets_path(self.dir), encoding="utf-8") as f:
            kept = json.load(f)
        kept["state_dir"] = "/elsewhere/state"   # the files were copied with the settings folder to another hub
        with open(setup.secrets_path(self.dir), "w") as f:
            json.dump(kept, f)
        raw = self.settings()
        raw["managed"]["state_dir"] = "/elsewhere/state"
        remote.save_settings(self.dir, raw)
        cf = self.part()
        cf.keep()
        self.assertEqual(self.conn.wanted, [])
        self.assertEqual(self.conn.stopped, 1)
        st = cf.tick()
        self.assertEqual(st["held"], {"key": "cloudflare.why_copy"})
        rows = dict((w, (lvl, d)) for lvl, w, d in cli.doctor_rows(self.dir, reach=lambda u: ("ok", "")))
        self.assertEqual(rows["cloudflare · tunnel"][0], "fail")
        self.assertEqual(rows["cloudflare · tunnel"][1], i18n.t("cloudflare.why_copy"))

    def test_the_owners_own_tunnel_is_not_run_by_the_hub(self):
        remote.save_settings(self.dir, {"enabled": True, "team": "myteam", "aud": ["a" * 64],
                                        "emails": ["me@example.test"], "url": "https://hub.example.test"})
        cf = self.part()
        cf.keep()
        self.assertEqual(self.conn.wanted, [])
        self.assertEqual(cf.tick()["tunnel"], "unknown")


class Download(unittest.TestCase):
    """The pinned cloudflared build: fetched once by its sha256 and size, nothing else is put in place."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cf-download-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.files = {}
        files = self.files

        class Serve(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = files.get(self.path)
                self.send_response(200 if body is not None else 404)
                self.send_header("Content-Length", str(len(body or b"")))
                self.end_headers()
                self.wfile.write(body or b"")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Serve)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        env = mock.patch.dict(os.environ, {cloudflared.ROOT_ENV: f"http://127.0.0.1:{self.server.server_address[1]}"})
        env.start()
        self.addCleanup(env.stop)

    def pin(self, name, body):
        self.files[f"/{cloudflared.VERSION}/{name}"] = body
        return mock.patch.dict(cloudflared.FILES, {cloudflared.system(): (name, hashlib.sha256(body).hexdigest(),
                                                                          len(body))})

    def test_the_pinned_file_is_put_in_place_once(self):
        with self.pin("cloudflared-x", b"#!/bin/sh\necho fake\n"):
            got = cloudflared.ensure(self.dir)
            self.assertEqual(got, cloudflared.path(self.dir))
            with open(got, "rb") as f:
                self.assertEqual(f.read(), b"#!/bin/sh\necho fake\n")
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(os.stat(got).st_mode), 0o700)
            self.files.clear()   # not fetched again
            self.assertEqual(cloudflared.ensure(self.dir), got)

    def test_a_file_that_does_not_match_is_not_put_in_place(self):
        with self.pin("cloudflared-x", b"the real one"):
            self.files[f"/{cloudflared.VERSION}/cloudflared-x"] = b"the evil one"
            with self.assertRaises(fetch.Failed):
                cloudflared.ensure(self.dir)
        self.assertFalse(os.path.exists(cloudflared.path(self.dir)))

    def test_the_macos_archive_gives_only_its_cloudflared(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, data in (("../evil", b"x"), ("cloudflared", b"binary")):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        with self.pin("cloudflared-darwin-x.tgz", buf.getvalue()):
            got = cloudflared.ensure(self.dir)
        with open(got, "rb") as f:
            self.assertEqual(f.read(), b"binary")
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.dir), "evil")))

    def test_no_pinned_build_uses_the_one_on_path_or_says_how_to_install(self):
        with mock.patch.object(cloudflared, "system", return_value=("plan9", "mips")):
            with mock.patch("shutil.which", return_value="/opt/x/cloudflared"):
                self.assertEqual(cloudflared.ensure(self.dir), "/opt/x/cloudflared")
            with mock.patch("shutil.which", return_value=None), self.assertRaises(cloudflared.Missing):
                cloudflared.ensure(self.dir)

    def test_every_pin_is_a_release_file_with_a_sha256(self):
        for key, (name, sha, size) in cloudflared.FILES.items():
            self.assertRegex(sha, r"^[0-9a-f]{64}$", key)
            self.assertTrue(name.startswith("cloudflared-") and size > 10 << 20, key)


if __name__ == "__main__":
    unittest.main()
