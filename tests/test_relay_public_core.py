"""A hub of the public install links at once (spec public relay §3.1): the open build's pages carry the public relay's
line (`v1/relay.json`, written by every release: tools/package/pages.py --relay), the core reads it from the hub's own
install source when «Link with a hub» finds no relay, and the page names that relay before the press. The team build's
hubs (a key in layers/deliver.json) and the hubs from git (no deliver.json) stay as they were: no line is looked for.

No network: the pages are a server of this machine (http to this machine is what a stand's source may be)."""
import tests  # noqa: F401 — keeps the machine's settings out
import base64
import http.server
import importlib.util
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

import platforms
from core import busflow, busrelay

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = base64.b64encode(os.urandom(32)).decode()


def line(url="https://relay.example.test", key=KEY, exp=4_000_000_000):
    payload = json.dumps({"v": 1, "relay": url, "key": key, "id": os.urandom(16).hex(), "by": "operator", "exp": exp},
                         separators=(",", ":")).encode()
    return busrelay.invite_text(payload, "-----BEGIN SSH SIGNATURE-----\nAAAA\n-----END SSH SIGNATURE-----\n")


class Pages:
    """The open build's pages on this machine: what each path answers is in `files` ({path: (status, body, headers)})."""

    def __init__(self):
        self.files, self.asked = {}, []
        pages = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                pages.asked.append(self.path)
                status, body, headers = pages.files.get(self.path, (404, b"", {}))
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class PublicLine(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hub-pr-")
        self.addCleanup(platforms.current.rmtree, self.state)
        os.makedirs(os.path.join(self.state, "layers"))
        self.pages = Pages()
        self.addCleanup(self.pages.stop)
        busflow._public.clear()
        self.addCleanup(busflow._public.clear)

    def source(self, **got):
        with open(os.path.join(self.state, "layers", "deliver.json"), "w", encoding="utf-8") as f:
            json.dump({"url": self.pages.url, **got}, f)

    def serve(self, text, status=200):
        self.pages.files["/v1/relay.json"] = (status, json.dumps({"invite": text}).encode(), {})

    def test_the_open_builds_pages_name_the_relay(self):
        self.source()
        good = line()
        self.serve(good)
        self.assertEqual(busflow.public_line(self.state), good)
        self.assertEqual(busflow.public_relay(self.state), "https://relay.example.test")
        busflow.public_line(self.state)
        self.assertEqual(self.pages.asked, ["/v1/relay.json"])                     # asked once, then kept

    def test_no_line_where_there_is_none(self):
        self.serve(line())
        self.assertEqual(busflow.public_relay_now(self.state), (None, False))     # decided at once: no thread
        busflow._public.clear()
        self.assertIsNone(busflow.public_line(self.state))                        # from git: no install source
        busflow._public.clear()
        self.source(key="k" * 32)                                                  # the team build: its key
        self.assertIsNone(busflow.public_line(self.state))
        self.assertEqual(self.pages.asked, [])                                     # nothing was asked of anyone

    def test_a_missing_or_wrong_file_is_no_line(self):
        self.source()
        for files in ({}, {"/v1/relay.json": (200, b"not json", {})},
                      {"/v1/relay.json": (200, json.dumps({"invite": "hub-link-1:abc"}).encode(), {})},
                      {"/v1/relay.json": (200, json.dumps({"invite": line(url="http://relay.example.test")}).encode(),
                                          {})},
                      {"/v1/relay.json": (302, b"", {"Location": "https://elsewhere.example.test/relay.json"})}):
            busflow._public.clear()
            self.pages.files = files
            self.assertIsNone(busflow.public_line(self.state), files)

    def flow(self, joined=None):
        flow = busflow.Flow(put=None, current=lambda: {"bus": {}}, state_dir=self.state)
        seen = []

        def join(self_, text, b=None, keys=None):
            seen.append(text)
            return joined or busflow.Done(url="u", by="o")
        return flow, seen, mock.patch.object(busflow.Flow, "relay_join", join)

    def test_link_without_a_relay_takes_the_public_line(self):
        self.source()
        good = line()
        self.serve(good)
        flow, seen, joining = self.flow()
        with joining:
            got = flow.relay_step({}, None, object(), True, busflow.Done(fp="f"))
        self.assertTrue(got.ok)
        self.assertEqual(seen, [good])

    def test_a_hub_on_a_relay_given_an_invitation_or_joining_by_a_link_looks_for_no_line(self):
        self.source()
        self.serve(line())
        flow, seen, joining = self.flow()
        own = line(url="https://own.example.test")
        with joining:
            self.assertTrue(flow.relay_step({}, own, object(), True, busflow.Done()).ok)          # pasted: that one
            self.assertTrue(flow.relay_step({"relay": {"url": "u", "key": KEY}}, None, object(), True,
                                            busflow.Done()).ok)                                     # on a relay
            self.assertTrue(flow.relay_step({}, None, object(), False, busflow.Done()).ok)          # B by a link
            # no library in this process (the core's has it): no install here, the paste as before
            self.assertEqual(flow.relay_step({}, None, None, True, busflow.Done()).code, "no_relay")
        self.assertEqual(seen, [own])
        self.assertEqual(self.pages.asked, [])

    def test_a_line_the_relay_refused_is_read_anew(self):
        self.source()
        self.serve(line())
        flow, seen, joining = self.flow(joined=busflow.Done("bad_invite", "used"))
        with joining:
            self.assertEqual(flow.relay_step({}, None, object(), True, busflow.Done()).code, "bad_invite")
            flow.relay_step({}, None, object(), True, busflow.Done())
        self.assertEqual(self.pages.asked, ["/v1/relay.json", "/v1/relay.json"])

    def test_the_page_and_the_terminal_name_the_relay_before_the_link(self):
        from modules.web.busweb import BusWeb
        from core import buscli
        self.source()
        self.serve(line())
        web = mock.Mock()
        web.t = lambda key, **_: key
        web.state_dir = self.state
        page = BusWeb(web)
        with mock.patch.object(BusWeb, "state", return_value={"bus": {"enabled": True, "name": "Hub"}, "sessions": {}}):
            status, got = page.act({"op": "view"})                                 # the network is not waited for
            self.assertEqual((status, got["public_relay"], got["public_relay_reading"]), (200, None, True))
            for _ in range(200):
                if not busflow._public_reading:
                    break
                threading.Event().wait(0.05)
            status, got = page.act({"op": "view"})
        self.assertEqual((status, got["public_relay"], got["public_relay_reading"]),
                         (200, "https://relay.example.test", False))
        on = {"bus": {"enabled": True, "name": "Hub", "relay": {"url": "https://own.example.test", "key": KEY}},
              "sessions": {}}
        with mock.patch.object(BusWeb, "state", return_value=on):
            self.assertIsNone(page.act({"op": "view"})[1]["public_relay"])        # on a relay: nothing to say
        busflow._public.clear()
        self.pages.asked.clear()
        with mock.patch.object(BusWeb, "state", return_value={"bus": {"name": "Hub"}, "sessions": {}}):
            self.assertEqual(page.act({"op": "view"})[1]["public_relay"], None)   # the bus off, no «Link» pressed:
            self.assertEqual(self.pages.asked, [])                                 # nothing goes out
            self.assertTrue(page.act({"op": "view", "consent": True})[1]["public_relay_reading"])   # pressed: read
        cli = buscli.Cli(None, lambda: {"bus": {"enabled": True, "name": "Hub"}}, self.state, None, None, {})
        cli.owner = lambda *a, **k: None
        err = []
        with mock.patch.object(busflow.Flow, "link", lambda self, **k: busflow.Done("no_relay", "x")), \
                mock.patch.object(busflow.Flow, "has_library", lambda self: True), \
                mock.patch("sys.stderr", mock.Mock(write=err.append)), mock.patch("builtins.print",
                                                                                  lambda *a, **k: err.append(" ".join(map(str, a)))):
            cli.link_cmd([])
        self.assertTrue(any("https://relay.example.test" in e for e in err), err)


class PagesRelay(unittest.TestCase):
    """Every release of the open build writes the line, or says it has none: pages.py rebuilds its folder whole, a
    release without the flag would drop the file the hubs read."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location("hub_test_pages_r", os.path.join(ROOT, "tools", "package",
                                                                                         "pages.py"))
        self.pages = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.pages)
        self.dir = tempfile.mkdtemp(prefix="hub-pr-pages-")
        self.addCleanup(platforms.current.rmtree, self.dir)

    def test_the_relay_file_is_checked_and_written(self):
        path = os.path.join(self.dir, "relay.txt")
        good = line()
        with open(path, "w", encoding="utf-8") as f:
            f.write(good + "\n")
        self.assertEqual(self.pages.relay_doc(path), {"invite": good})
        self.assertIsNone(self.pages.relay_doc("none"))
        for bad in ("hub-link-1:abc", line(url="http://relay.example.test"), "", good.partition(".")[0],
                    line(exp=int(__import__("time").time()) + 7 * 86400)):   # no signature; a one-time invitation's term
            with open(path, "w", encoding="utf-8") as f:
                f.write(bad)
            with self.assertRaises(self.pages.Refused):
                self.pages.relay_doc(path)
        with self.assertRaises(self.pages.Refused):
            self.pages.relay_doc(os.path.join(self.dir, "missing.txt"))

    def test_without_the_flag_the_pages_are_refused(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit) as e:
            self.pages.main([self.dir, "--out", self.dir, "--base", "https://acme.example.test/hub",
                             "--assets", "https://github.com/acme/hub/releases/download/v1"])
        self.assertEqual(e.exception.code, 2)

    def test_the_release_plan_names_the_flag(self):
        spec = importlib.util.spec_from_file_location("hub_test_factory_r", os.path.join(ROOT, "tools", "package",
                                                                                           "factory.py"))
        factory = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(factory)
        rows = factory.plan_open("https://acme.example.test/hub", "acme/hub", "v1.2.3", self.dir, relay="relay.txt")
        self.assertIn("--relay relay.txt", rows[0])


if __name__ == "__main__":
    unittest.main()


from tests import test_hubbus_l1  # noqa: E402 — the module, not its class: a class here would run its tests again
from tests.test_hubbus import need_lib, need_ssh  # noqa: E402


@need_lib
@need_ssh
class PublicInstallLinksAtOnce(unittest.TestCase):
    """The whole path on two clean hubs and the relay of this tree, the relay open to everyone (`server.py open`, slice
    R): A, installed from pages that name the relay, makes a link with nothing pasted; B pastes the link — linked. The
    stand's methods, not its tests (tests/test_hubbus_l1.TwoCommandsThroughTheRelay)."""
    # the stand's methods by name: a TestCase class kept in this module's names would run its tests here again
    setUp = test_hubbus_l1.TwoCommandsThroughTheRelay.setUp
    transport = test_hubbus_l1.TwoCommandsThroughTheRelay.transport
    settle = test_hubbus_l1.TwoCommandsThroughTheRelay.settle
    cli = test_hubbus_l1.TwoCommandsThroughTheRelay.cli

    def test_a_hub_of_the_public_install_links_without_pasting_a_relay(self):
        code, line, _ = self.relay.cmd("open")
        if code != 0:
            self.skipTest("the relay of this tree has no open line (relay/server.py open, slice R)")
        pages = Pages()
        self.addCleanup(pages.stop)
        pages.files["/v1/relay.json"] = (200, json.dumps({"invite": line}).encode(), {})
        os.makedirs(os.path.join(self.a.h.state_dir, "layers"), exist_ok=True)
        with open(os.path.join(self.a.h.state_dir, "layers", "deliver.json"), "w", encoding="utf-8") as f:
            json.dump({"url": pages.url}, f)
        busflow._public.clear()
        self.addCleanup(busflow._public.clear)
        link, err = self.cli(self.a, ["link", "--yes"])                          # «Link» on a fresh hub
        self.assertEqual(busflow.kind_of(link), "link")
        self.assertIn(self.relay.url, err)                                         # the relay named before
        self.assertEqual(self.a.bus["relay"]["url"], self.relay.url)
        self.cli(self.b, ["join", "-", "--yes"], stdin=link + "\n")               # B: the link alone
        self.settle()
        (ca,), (cb,) = self.a.bus["contacts"].values(), self.b.bus["contacts"].values()
        self.assertEqual((ca["status"], cb["status"]), ("known", "known"))
        self.assertEqual(self.b.bus["relay"]["url"], self.relay.url)
        self.assertEqual(pages.asked, ["/v1/relay.json"])                          # read once, by A
