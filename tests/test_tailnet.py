"""The tailnet module (decision of 2026-09-30: Tailscale setup as a module too): the hub page and "Files" open from
the owner's Tailscale devices by the Mac's IP. The module writes tailnet.json (IP, link addresses, Host, owner) and
removes a stale `tailscale serve` export; pages admit from the tailnet only the Mac's owner (by whois of the address),
the tailnet also has other users."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import time
import os
import sys
import tempfile
import unittest

from modules.tailnet import access
from modules.tailnet.run import CHOSEN, Tailnet

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakes", "fake_tailscale.py")
STATUS = {"BackendState": "Running",
          "Self": {"DNSName": "mac.tail.ts.net.", "TailscaleIPs": ["100.9.9.9", "fd7a::1"], "UserID": 1},
          "User": {"1": {"LoginName": "owner@x"}, "2": {"LoginName": "guest@x"}}}
STALE = {"TCP": {"443": {"HTTPS": True}},
         "Web": {"mac.tail.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8790"},
                                                       "/f": {"Proxy": "http://127.0.0.1:8792"}}}}}


class QuietServer(unittest.TestCase):
    def test_closed_connection_is_not_logged_but_real_errors_are(self):
        import contextlib
        import io
        server = access.Server(("127.0.0.1", 0), None)
        try:
            for exc, logged in ((ConnectionResetError(54, "reset"), False), (BrokenPipeError(32, "pipe"), False),
                                (ValueError("a real one"), True)):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    try:
                        raise exc
                    except Exception:
                        server.handle_error(None, ("127.0.0.1", 1))
                self.assertEqual("Traceback" in err.getvalue(), logged, exc)
        finally:
            server.server_close()


class TailnetTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tailnet-")
        self.log = os.path.join(self.dir, "ts.log")
        self.fake_state = os.path.join(self.dir, "ts.json")
        self.write({"status": STATUS, "serve": STALE})
        os.environ.update(FAKE_TS_STATE=self.fake_state, FAKE_TS_LOG=self.log)
        os.environ.pop("SHLVL", None)  # as under launchd (hub-up): live case of 2026-09-30, "Tailscale GUI failed to start"
        self.cfg = {"tailnet": {"enabled": True}}   # off until turned on (DefaultOff)
        self.t = Tailnet(self.dir, config=lambda: self.cfg, cli=[sys.executable, FAKE])

    def write(self, state):
        json.dump(state, open(self.fake_state, "w"))

    def status(self):
        return json.load(open(os.path.join(self.dir, "tailnet.json")))

    def test_status_file_and_stale_serve_dropped(self):
        self.t.tick()
        self.assertEqual(json.load(open(self.fake_state))["serve"], {})
        st = self.status()
        self.assertTrue(st["ok"])
        self.assertEqual((st["owner"], st["ip"]), ("owner@x", "100.9.9.9"))
        self.assertEqual(st["urls"], {"web": "http://100.9.9.9:8787", "files": "http://100.9.9.9:8788"})
        self.assertEqual(set(st["hosts"]), {f"{h}:{p}" for h in ("100.9.9.9", "mac.tail.ts.net", "mac")
                                           for p in (8787, 8788)})

    def test_urls_by_the_name_survive_the_ip_changing(self):
        # Tailscale reinstalled on the Mac -> a new IP, the phone kept the old one from urls.web and lost the hub;
        # urls stays by IP (a phone with private DNS does not find the name), urls_dns is the same by the name
        self.t.tick()
        first = self.status()
        self.assertEqual(first["urls_dns"], {"web": "http://mac.tail.ts.net:8787", "files": "http://mac.tail.ts.net:8788"})
        moved = {**STATUS, "Self": {**STATUS["Self"], "TailscaleIPs": ["100.8.8.8"]}}
        self.write({"status": moved, "serve": {}})
        self.t.tick()
        again = self.status()
        self.assertEqual(again["urls_dns"], first["urls_dns"])
        self.assertEqual(again["urls"]["web"], "http://100.8.8.8:8787")

    def test_addresses_list_ip_and_name(self):
        self.t.tick()
        st = self.status()
        self.assertEqual(access.addresses(st), [{"kind": "ip", "url": "http://100.9.9.9:8787"},
                                                {"kind": "name", "url": "http://mac.tail.ts.net:8787"}])
        # the tailnet is off: nothing to keep
        self.assertEqual(access.addresses({"ok": False}), [])
        self.assertEqual(access.addresses({}), [])

    def test_no_name_address_without_a_node_name(self):
        # a node without a DNS name: no "http://:8787" for the client to keep
        self.write({"status": {**STATUS, "Self": {**STATUS["Self"], "DNSName": ""}}, "serve": {}})
        self.t.tick()
        st = self.status()
        self.assertEqual(st["urls_dns"], {})
        self.assertEqual(access.addresses(st), [{"kind": "ip", "url": "http://100.9.9.9:8787"}])

    def test_addresses_from_a_tailnet_file_of_another_shape_do_not_fail(self):
        # tailnet.json may be edited by hand: /api/about (and a client's hub discovery) must not answer 500
        self.assertEqual(access.addresses({"ok": True, "urls": ["x"], "urls_dns": "y"}), [])

    def test_foreign_serve_is_kept(self):
        mine = {"Web": {"mac.tail.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}}}}
        self.write({"status": STATUS, "serve": mine})
        self.t.tick()
        self.assertEqual(json.load(open(self.fake_state))["serve"], mine)

    def test_a_serve_setup_with_anything_besides_the_disabled_services_is_kept_whole(self):
        # `serve reset` drops the WHOLE setup: it goes only when every entry is a proxy to a disabled service on this
        # machine; a TCP forward, another host on 8790, a handler that is not a proxy, a funnel — all left as they are
        web = STALE["Web"]
        for what, theirs in {
            "a TCP forward next to it": {"TCP": {**STALE["TCP"], "5432": {"TCPForward": "127.0.0.1:5432"}}, "Web": web},
            "8790 of another machine": {"TCP": STALE["TCP"], "Web": {"mac.tail.ts.net:443": {"Handlers": {
                "/": {"Proxy": "http://203.0.113.5:8790"}}}}},
            "a folder served": {"TCP": STALE["TCP"], "Web": {"mac.tail.ts.net:443": {"Handlers": {
                **web["mac.tail.ts.net:443"]["Handlers"], "/docs": {"Path": "/srv/docs"}}}}},
            "a funnel": {**STALE, "AllowFunnel": {"mac.tail.ts.net:443": True}},
            "a service": {**STALE, "Services": {"svc:db": {"TCP": {"5432": {"TCPForward": "127.0.0.1:5432"}}}}},
        }.items():
            with self.subTest(what):
                self.write({"status": STATUS, "serve": theirs})
                self.t.tick()
                self.assertEqual(json.load(open(self.fake_state))["serve"], theirs)
                self.assertTrue(self.status()["ok"])
        # only the disabled services, by the name or by localhost: reset as before
        for target in ("http://localhost:8790", "http://127.0.0.1:8792/", "http://[::1]:8790", "http://0.0.0.0:8790"):
            with self.subTest(target):
                self.write({"status": STATUS, "serve": {"TCP": {"443": {"HTTPS": True}}, "Web": {
                    "mac.tail.ts.net:443": {"Handlers": {"/": {"Proxy": target}}}}}})
                self.t.tick()
                self.assertEqual(json.load(open(self.fake_state))["serve"], {})

    def test_tagged_node_has_no_owner_until_owners_are_set(self):
        # a tagged node has no human: its "user" is tagged-devices, shared by all tagged nodes in the tailnet,
        # and any other tagged node (a server, CI) passed the "owner" check
        tagged = {**STATUS, "Self": {**STATUS["Self"], "UserID": 3, "Tags": ["tag:server"]},
                  "User": {**STATUS["User"], "3": {"LoginName": "tagged-devices"}}}
        self.write({"status": tagged, "serve": {}})
        st = self.t.tick()
        self.assertFalse(st["ok"])
        self.assertEqual(st.get("owners") or [], [])
        self.assertIn("tailnet.owners", st["why"])
        self.cfg["tailnet"] = {"enabled": True, "owners":["owner@x", "second@x"]}
        st = self.t.tick()
        self.assertTrue(st["ok"])
        self.assertEqual(st["owners"], ["owner@x", "second@x"])

    def test_owners_setting_replaces_the_node_user(self):
        self.cfg["tailnet"] = {"enabled": True, "owners":["phone@x"]}
        self.assertEqual(self.t.tick()["owners"], ["phone@x"])

    def test_an_untagged_node_keeps_its_user_as_the_owner(self):
        # as it was: an untagged node — its user is the owner, nothing to configure
        st = self.t.tick()
        self.assertTrue(st["ok"])
        self.assertEqual((st["owners"], st["owner"]), (["owner@x"], "owner@x"))

    def test_the_modules_tab_shows_why_tailnet_is_off(self):
        # "Modules" read a problem field the module does not write: the cause was not shown
        from modules.web import modinfo
        self.write({"status": {**STATUS, "BackendState": "Stopped"}, "serve": {}})
        self.t.tick()
        (row,) = [m for m in modinfo.modules(self.dir, self.cfg, {"sessions": []}) if m["id"] == "tailnet"]
        self.assertIn("Tailscale is not connected", row["detail"])

    def test_tailnet_status_timeout_keeps_the_last_good_state(self):
        # a hung machine: `tailscale status` did not answer in time, tailnet.json became ok:false and the page answered
        # 403 to the owner's other devices until the next tick
        import subprocess
        from unittest import mock
        first = self.t.tick()
        self.assertTrue(first["ok"])
        with mock.patch("modules.tailnet.run.subprocess.run", side_effect=subprocess.TimeoutExpired("tailscale", 30)):
            st = self.t.tick()
        self.assertTrue(st["ok"])
        self.assertTrue(self.status()["ok"])

    def test_tailnet_status_timeout_drops_a_record_older_than_five_minutes(self):
        # the kept record is not rewritten, so it ages across timeouts: after 5 minutes the page stops letting the tailnet in
        import subprocess
        import time
        from unittest import mock
        first = self.t.tick()
        path = os.path.join(self.dir, "tailnet.json")
        for ts, kept in ((first["ts"], True), (time.time() - 301, False), (time.time() + 3600, False), (None, False)):
            rec = {**first, "ts": ts}
            if ts is None:
                rec.pop("ts")
            with open(path, "w") as f:
                json.dump(rec, f)
            with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("tailscale", 30)):
                st = self.t.tick()
            self.assertEqual(st["ok"], kept, ts)
            self.assertEqual(self.status()["ok"], kept, ts)
            if kept:
                self.assertEqual(self.status()["ts"], ts)   # not rewritten: the age does not restart

    def test_tailnet_status_timeout_does_not_keep_what_the_settings_no_longer_allow(self):
        # the owner removed a login from tailnet.owners (or moved the ports) while tailscale hangs: the old record would
        # keep admitting them for the rest of the 5 minutes
        import subprocess
        from unittest import mock
        self.cfg = {"tailnet": {"enabled": True, "owners":["owner@x", "guest@x"]}}
        self.assertEqual(self.t.tick()["owners"], ["owner@x", "guest@x"])
        hang = mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("tailscale", 30))
        with hang:
            self.assertTrue(self.t.tick()["ok"])   # the same settings: kept
            self.cfg = {"tailnet": {"enabled": True, "owners":["owner@x"]}}
            self.assertFalse(self.t.tick()["ok"])
        self.cfg = {"tailnet": {"enabled": True, "owners":["owner@x"]}}
        self.assertTrue(self.t.tick()["ok"])
        with hang:
            self.cfg = {"tailnet": {"enabled": True, "owners":["owner@x"], "expose": {"web": 9999}}}
            self.assertFalse(self.t.tick()["ok"])

    def test_serve_cleanup_timeout_does_not_discard_a_fresh_status(self):
        # `status` answered, only `serve status` hung: the new address is published, not the 5-minute-old record
        import subprocess
        from unittest import mock
        self.assertEqual(self.t.tick()["ip"], "100.9.9.9")
        self.write({"status": {**STATUS, "Self": {**STATUS["Self"], "TailscaleIPs": ["100.8.8.8"]}}, "serve": STALE})
        real = self.t.ts

        def ts(*args):
            if args[0] == "serve":
                raise subprocess.TimeoutExpired("tailscale", 30)
            return real(*args)
        with mock.patch.object(self.t, "ts", ts):
            st = self.t.tick()
        self.assertEqual((st["ok"], st["ip"]), (True, "100.8.8.8"))
        self.assertEqual(self.status()["ip"], "100.8.8.8")

    def test_tailnet_status_timeout_with_a_record_of_another_shape(self):
        import subprocess
        from unittest import mock
        for junk in ("[]", '"x"', "null", '{"ok": true}', '{"ok": true, "ts": "now"}'):
            with open(os.path.join(self.dir, "tailnet.json"), "w") as f:
                f.write(junk)
            with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("tailscale", 30)):
                self.assertFalse(self.t.tick()["ok"], junk)

    def test_tailnet_record_is_written_through_the_platform_replace(self):
        from unittest import mock
        import platforms
        with mock.patch.object(platforms.current, "replace", wraps=platforms.current.replace) as rep:
            self.t.tick()
        rep.assert_called_once()

    def test_tailnet_error_other_than_timeout_still_drops_the_record(self):
        # tailscaled is down (the CLI answers with an error): no reason to keep the addresses
        from unittest import mock
        self.assertTrue(self.t.tick()["ok"])
        with mock.patch("subprocess.run", side_effect=OSError("no such file")):
            self.assertFalse(self.t.tick()["ok"])
        self.assertFalse(self.status()["ok"])

    def test_down_and_disabled(self):
        self.write({"status": {**STATUS, "BackendState": "Stopped"}, "serve": {}})
        st = self.t.tick()
        self.assertFalse(st["ok"])
        self.assertIn("Tailscale", st["why"])
        self.cfg["tailnet"] = {"enabled": False}
        self.assertFalse(self.t.tick()["ok"])


class DefaultOff(unittest.TestCase):
    """The tailnet is off until `tailnet.enabled: true` (SECURITY.md: remote access is off by default). Earlier releases
    turned it on by themselves on a machine already in a tailnet; a hub where it worked keeps it after the update: the
    first tick writes `enabled: true` into hub.json, once."""

    OLD_WORKING = {"ok": True, "owners": ["owner@x"], "owner": "owner@x", "ip": "100.9.9.9", "name": "mac.tail.ts.net",
                   "urls": {"web": "http://100.9.9.9:8787"}, "hosts": ["100.9.9.9:8787"], "ts": 1.0}   # an earlier release's

    def setUp(self):
        from unittest import mock
        self.dir = tempfile.mkdtemp(prefix="tailnet-off-")
        self.fake_state = os.path.join(self.dir, "ts.json")
        self.log = os.path.join(self.dir, "ts.log")
        self.hub_json = os.path.join(self.dir, "conf", "hub.json")
        os.makedirs(os.path.dirname(self.hub_json))
        self.settings({"title": "maps"})
        self.connected(True)
        env = mock.patch.dict(os.environ, {"FAKE_TS_STATE": self.fake_state, "FAKE_TS_LOG": self.log,
                                           "HUB_CONFIG": self.hub_json, "SHLVL": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.t = Tailnet(self.dir, cli=[sys.executable, FAKE])

    def settings(self, cfg):
        with open(self.hub_json, "w") as f:
            json.dump(cfg, f)

    def saved(self):
        with open(self.hub_json, "rb") as f:
            return f.read()

    def connected(self, up):
        json.dump({"status": STATUS if up else {**STATUS, "BackendState": "Stopped"}, "serve": {}},
                  open(self.fake_state, "w"))

    def earlier(self, rec):
        if rec.get("ok"):   # an earlier release rewrote its working record every minute: fresh at the update
            rec = {**rec, "ts": time.time()}
        with open(os.path.join(self.dir, "tailnet.json"), "w") as f:
            json.dump(rec, f)

    def asked_tailscale(self):
        return os.path.exists(self.log) and os.path.getsize(self.log) > 0

    def test_a_new_hub_on_a_machine_in_a_tailnet_stays_off(self):
        before = self.saved()
        for _ in range(2):
            st = self.t.tick()
            self.assertFalse(st["ok"])
            self.assertTrue(st["off"])
            self.assertIn("tailnet.enabled: true", st["why"])   # why and how to turn it on, not silence
            self.assertNotIn("hosts", st)
        self.assertFalse(access.owner_peer(self.dir, "100.1.1.1", whois=lambda d, p: "owner@x"))
        self.assertFalse(self.asked_tailscale())   # off: Tailscale is not even asked
        self.assertEqual(self.saved(), before)     # nothing written for a new hub

    def test_an_earlier_hub_where_it_worked_keeps_it_on(self):
        self.settings({"title": "maps", "tailnet": {"owners": ["owner@x"]}})
        self.earlier(self.OLD_WORKING)
        st = self.t.tick()
        self.assertTrue(st["ok"])
        cfg = json.loads(self.saved())
        self.assertEqual(cfg, {"title": "maps", "tailnet": {"owners": ["owner@x"], "enabled": True}})
        once = self.saved()
        for _ in range(2):   # again: nothing changes
            self.assertTrue(self.t.tick()["ok"])
        self.assertEqual(self.saved(), once)

    def test_it_worked_but_tailscale_is_down_at_the_update(self):
        # the earlier record says it worked: on is kept even when Tailscale is disconnected at this moment
        self.earlier(self.OLD_WORKING)
        self.connected(False)
        st = self.t.tick()
        self.assertFalse(st["ok"])
        self.assertIn("Tailscale is not connected", st["why"])
        self.assertIs(json.loads(self.saved())["tailnet"]["enabled"], True)

    def test_an_earlier_record_that_was_down_but_tailscale_works_now(self):
        # the earlier release would have turned it on within this minute: the same as "it worked"
        self.earlier({"ok": False, "why": "Tailscale is not connected (Starting)", "ts": 1.0})
        self.assertTrue(self.t.tick()["ok"])
        self.assertIs(json.loads(self.saved())["tailnet"]["enabled"], True)

    def test_an_earlier_hub_without_tailscale_is_off_for_good(self):
        self.earlier({"ok": False, "why": "tailscale: no such file", "ts": 1.0})
        before = self.saved()
        missing = Tailnet(self.dir, cli=[os.path.join(self.dir, "no-tailscale")])
        self.assertTrue(missing.tick()["off"])
        for _ in range(2):   # Tailscale is installed later: the hub does not turn it on by itself any more
            st = self.t.tick()
            self.assertFalse(st["ok"])
            self.assertTrue(st["off"])
        self.assertEqual(self.saved(), before)

    def test_a_failing_tailscale_at_the_update_is_not_taken_for_none(self):
        # tailscaled down (the CLI answers with an error) or a garbled answer: Tailscale is here — decided once it works
        from unittest import mock
        self.earlier({"ok": False, "why": "tailscale: failed to connect to tailscaled", "ts": 1.0})
        for broken in (RuntimeError("failed to connect to local tailscaled"), ValueError("garbled")):
            with mock.patch.object(self.t, "ts", side_effect=broken):
                self.assertTrue(self.t.tick()["down"])
        self.assertFalse(os.path.exists(os.path.join(self.dir, CHOSEN)))
        self.assertTrue(self.t.tick()["ok"])
        self.assertIs(json.loads(self.saved())["tailnet"]["enabled"], True)

    def test_a_new_hub_with_a_broken_hub_json_does_not_turn_on_once_it_is_fixed(self):
        with open(self.hub_json, "w") as f:
            f.write("{")
        self.assertIn("hub.json", self.t.tick()["why"])
        self.settings({"title": "maps"})
        self.assertTrue(self.t.tick()["off"])
        self.assertEqual(json.loads(self.saved()), {"title": "maps"})

    def test_a_hub_that_was_down_long_before_the_update_still_carries_over(self):
        # the earlier release's working record is hours old (the computer was off): it worked there all the same
        with open(os.path.join(self.dir, "tailnet.json"), "w") as f:
            json.dump({**self.OLD_WORKING, "ts": 1.0}, f)
        # and right now Tailscale is not found (it is reinstalled at this very moment): the old record decides
        Tailnet(self.dir, cli=[os.path.join(self.dir, "no-tailscale")]).tick()
        self.assertIs(json.loads(self.saved())["tailnet"]["enabled"], True)

    def test_tailscale_away_at_the_update_is_decided_once_it_is_up(self):
        # a laptop away from its tailnet at the update: not decided until Tailscale answers as connected
        self.earlier({"ok": False, "why": "Tailscale is not connected (Stopped)", "ts": 1.0})
        self.connected(False)
        before = self.saved()
        for _ in range(2):
            st = self.t.tick()
            self.assertFalse(st["ok"])
            self.assertNotIn("off", st)
            self.assertIn("Tailscale is not connected", access.status_line(self.dir))
        self.assertEqual(self.saved(), before)
        self.connected(True)
        self.assertTrue(self.t.tick()["ok"])
        self.assertIs(json.loads(self.saved())["tailnet"]["enabled"], True)

    def test_an_owner_with_tailscale_and_nobody_to_let_in_is_off(self):
        # a tagged node with no tailnet.owners: it did not work under the earlier release either
        tagged = {**STATUS, "Self": {**STATUS["Self"], "UserID": 3, "Tags": ["tag:server"]},
                  "User": {**STATUS["User"], "3": {"LoginName": "tagged-devices"}}}
        json.dump({"status": tagged, "serve": {}}, open(self.fake_state, "w"))
        self.earlier({"ok": False, "owners": [], "why": "the tagged node has no owner", "ts": 1.0})
        self.assertTrue(self.t.tick()["off"])
        self.assertNotIn("tailnet", json.loads(self.saved()))

    def test_a_null_section_or_an_unreadable_record_still_carries_over(self):
        self.settings({"title": "maps", "tailnet": None})
        self.earlier(self.OLD_WORKING)
        self.assertTrue(self.t.tick()["ok"])
        self.assertEqual(json.loads(self.saved())["tailnet"], {"enabled": True})
        os.remove(os.path.join(self.dir, CHOSEN))
        self.settings({"title": "maps"})
        with open(os.path.join(self.dir, "tailnet.json"), "w") as f:
            f.write("{")   # cut off: not a new hub, Tailscale is asked
        self.assertTrue(self.t.tick()["ok"])
        self.assertIs(json.loads(self.saved())["tailnet"]["enabled"], True)

    def test_the_owners_choice_stands(self):
        for chosen in (False, "yes", 1, None):
            self.settings({"tailnet": {"enabled": chosen}})
            self.earlier(self.OLD_WORKING)
            before = self.saved()
            st = self.t.tick()
            self.assertFalse(st["ok"], chosen)
            self.assertTrue(st["off"], chosen)
            self.assertEqual(self.saved(), before, chosen)
        self.settings({"tailnet": {"enabled": True}})
        self.assertTrue(self.t.tick()["ok"])

    def test_turned_off_after_it_was_carried_over(self):
        self.earlier(self.OLD_WORKING)
        self.assertTrue(self.t.tick()["ok"])
        self.settings({"title": "maps", "tailnet": {"enabled": False}})
        st = self.t.tick()
        self.assertFalse(st["ok"])
        self.assertTrue(st["off"])

    def test_unwritable_settings_keep_it_working_and_try_again(self):
        import contextlib
        import io
        from unittest import mock
        from core import catalog
        self.earlier(self.OLD_WORKING)
        err = io.StringIO()
        with mock.patch.object(catalog, "write_settings", side_effect=OSError("read-only")), \
                contextlib.redirect_stderr(err):
            for _ in range(3):
                self.assertTrue(self.t.tick()["ok"])   # as before the update, not a lost phone
        self.assertEqual(err.getvalue().count("is not written"), 1)   # told once, not a line a minute
        self.assertNotIn("enabled", json.loads(self.saved()).get("tailnet", {}))
        self.assertTrue(self.t.tick()["ok"])   # writable again: carried over now
        self.assertIs(json.loads(self.saved())["tailnet"]["enabled"], True)

    def test_a_tailnet_section_of_another_shape_is_off_and_says_so(self):
        for junk in ("on", True, ["x"]):
            self.settings({"tailnet": junk})
            self.earlier(self.OLD_WORKING)
            before = self.saved()
            st = self.t.tick()
            self.assertFalse(st["ok"], junk)
            self.assertIn("tailnet is not an object", access.status_line(self.dir), junk)
            self.assertEqual(self.saved(), before, junk)

    def test_a_rollback_does_not_turn_it_on_for_good(self):
        # a new hub, off; the earlier release (a rollback) turns it on by itself again; forward again: still off
        self.assertTrue(self.t.tick()["off"])
        self.earlier(self.OLD_WORKING)
        before = self.saved()
        st = self.t.tick()
        self.assertTrue(st["off"])
        self.assertFalse(access.owner_peer(self.dir, "100.1.1.1", whois=lambda d, p: "owner@x"))
        self.assertEqual(self.saved(), before)

    def test_turned_off_while_it_is_carried_over(self):
        # the owner turns it off between the tick's read of the settings and its locked write: off stands
        t = Tailnet(self.dir, config=lambda: {"tailnet": {}}, cli=[sys.executable, FAKE])
        self.settings({"tailnet": {"enabled": False}})
        self.earlier(self.OLD_WORKING)
        before = self.saved()
        st = t.tick()
        self.assertTrue(st["off"])
        self.assertNotIn("hosts", st)
        self.assertEqual(self.saved(), before)

    def test_an_unreadable_hub_json_closes_the_tailnet_instead_of_stopping_the_module(self):
        self.settings({"tailnet": {"enabled": True}})
        self.assertTrue(self.t.tick()["ok"])
        with open(self.hub_json, "w") as f:
            f.write("{")
        st = self.t.tick()
        self.assertFalse(st["ok"])
        self.assertIn("hub.json", st["why"])
        self.assertFalse(access.owner_peer(self.dir, "100.1.1.1", whois=lambda d, p: "owner@x"))

    def test_a_failed_read_of_hub_json_keeps_the_record(self):
        # Windows: a read while the page replaces hub.json fails for a moment — not a closed tailnet until the next tick
        from unittest import mock
        from core import catalog
        self.settings({"tailnet": {"enabled": True}})
        first = self.t.tick()
        with mock.patch.object(catalog, "read_settings", side_effect=PermissionError("in use")):
            self.assertEqual(self.t.tick(), first)

    def test_an_unwritable_mark_is_told_once(self):
        import contextlib
        import io
        from unittest import mock
        err = io.StringIO()
        with mock.patch("builtins.open", side_effect=PermissionError("read-only")), contextlib.redirect_stderr(err):
            for _ in range(3):
                self.t.chosen()
        self.assertEqual(err.getvalue().count("is not written"), 1)

    def test_the_off_record_is_not_rewritten_every_minute(self):
        first = self.t.tick()
        self.assertEqual(self.t.tick()["ts"], first["ts"])

    def test_no_ipv4_is_a_reason_not_off(self):
        self.settings({"tailnet": {"enabled": True}})
        json.dump({"status": {**STATUS, "Self": {**STATUS["Self"], "TailscaleIPs": ["fd7a::1"]}}, "serve": {}},
                  open(self.fake_state, "w"))
        self.assertFalse(self.t.tick()["ok"])
        self.assertIn("no IPv4", access.status_line(self.dir))

    def test_hub_status_says_why_and_how(self):
        import subprocess
        self.t.tick()
        env = {**os.environ, "HUB_STATE_DIR": self.dir, "HUB_LANG": "en"}
        env.pop("HUB_SESSION", None)
        out = subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "..", "bin", "hub"), "status"],
                             capture_output=True, text=True, env=env).stdout
        self.assertIn("Tailscale: off", out)
        self.assertIn("Settings → Modules → Tailscale", out)
        self.settings({"tailnet": {"enabled": True}})
        self.assertTrue(self.t.tick()["ok"])
        self.assertIsNone(access.status_line(self.dir))   # working: no line

    def test_the_modules_card_has_a_switch(self):
        from modules.web import modinfo, settings
        self.t.tick()
        cfg = json.loads(self.saved())
        (row,) = [m for m in modinfo.modules(self.dir, cfg, {"sessions": []}, "en") if m["id"] == "tailnet"]
        self.assertEqual((row["state"], row["toggle"], row["enabled"]), ("off", True, False))
        self.assertIn("Enable", row["how"])
        self.assertEqual(row["detail"], "")   # "How to enable" says it; the reason is not repeated above it
        self.assertEqual(modinfo.TOGGLES["tailnet"], "tailnet.enabled")
        self.assertEqual(settings.validate("tailnet.enabled", True), (True, None))
        self.settings({"tailnet": {"enabled": True}})
        self.t.tick()
        (row,) = [m for m in modinfo.modules(self.dir, json.loads(self.saved()), {"sessions": []}, "en")
                  if m["id"] == "tailnet"]
        self.assertEqual((row["state"], row["enabled"]), ("on", True))
        # turned off, the record not yet rewritten: the switch shows the setting, so a press turns it on again
        (row,) = [m for m in modinfo.modules(self.dir, {"tailnet": {"enabled": False}}, {"sessions": []}, "en")
                  if m["id"] == "tailnet"]
        self.assertEqual((row["state"], row["enabled"]), ("off", False))


class CliPath(unittest.TestCase):
    """Where to look for tailscale: the hub may have started before Tailscale was installed — the process PATH is old,
    but the CLI is there."""

    def command(self, adapter, existing, env):
        from unittest import mock
        import platforms
        with mock.patch.object(platforms, "current", adapter), mock.patch("os.path.exists", lambda p: p in existing), \
                mock.patch.dict(os.environ, {"ProgramW6432": "", "ProgramFiles": "", **env}):   # not the machine folders
            return Tailnet("unused", config=lambda: {}).command()

    def test_windows_finds_the_cli_in_program_files_without_path(self):
        from platforms import windows
        exe = os.path.join("/progs", "Tailscale", "tailscale.exe")
        self.assertEqual(self.command(windows, {exe}, {"ProgramFiles": "/progs"}), [exe])

    def test_windows_32bit_python_looks_into_the_64bit_program_files(self):
        # for 32-bit Python ProgramFiles is "Program Files (x86)"; Tailscale installs only into the 64-bit folder
        from platforms import windows
        exe = os.path.join("/progs64", "Tailscale", "tailscale.exe")
        self.assertEqual(self.command(windows, {exe}, {"ProgramFiles": "/progs86", "ProgramW6432": "/progs64"}), [exe])

    def test_windows_without_the_file_falls_back_to_path(self):
        from platforms import windows
        self.assertEqual(self.command(windows, set(), {"ProgramFiles": "/progs"}), ["tailscale"])

    def test_mac_app_cli_is_used_when_present(self):
        from platforms import posix
        app = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
        self.assertEqual(self.command(posix, {app}, {}), [app])
        self.assertEqual(self.command(posix, set(), {}), ["tailscale"])

    def test_the_cli_from_settings_wins(self):
        self.assertEqual(Tailnet("unused", config=lambda: {"tailnet": {"cli": "mine"}}).command(), ["mine"])


class AccessTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="access-")
        json.dump({"ok": True, "ts": time.time(), "owner": "owner@x", "ip": "100.9.9.9", "hosts": ["100.9.9.9:8788"]},
                  open(os.path.join(self.dir, "tailnet.json"), "w"))
        self.logins = {"100.1.1.1": "owner@x", "100.2.2.2": "guest@x"}

    def ok(self, host, peer):
        return access.allowed(self.dir, host, peer, whois=lambda _, p: self.logins.get(p))

    def test_rules(self):
        self.assertTrue(self.ok("127.0.0.1:8788", "127.0.0.1"))
        self.assertTrue(self.ok("localhost:8788", "::1"))
        self.assertFalse(self.ok("evil.example:8788", "127.0.0.1"))  # DNS rebinding
        self.assertTrue(self.ok("100.9.9.9:8788", "100.1.1.1"))      # the owner's phone
        self.assertFalse(self.ok("100.9.9.9:8788", "100.2.2.2"))     # another tailnet user
        self.assertFalse(self.ok("100.9.9.9:8788", "100.3.3.3"))     # whois does not know it
        self.assertFalse(self.ok("127.0.0.1:8788", "100.1.1.1"))     # wrong Host
        self.assertFalse(self.ok("evil.example:8788", "100.1.1.1"))

    def test_owners_list(self):
        json.dump({"ok": True, "ts": time.time(),
                   "owners": ["owner@x", "guest@x"], "ip": "100.9.9.9", "hosts": ["100.9.9.9:8788"]},
                  open(os.path.join(self.dir, "tailnet.json"), "w"))
        self.assertTrue(self.ok("100.9.9.9:8788", "100.2.2.2"))
        self.assertFalse(self.ok("100.9.9.9:8788", "100.3.3.3"))

    def test_tagged_devices_is_never_an_owner(self):
        # a state file written by an earlier version on a tagged node
        json.dump({"ok": True, "ts": time.time(),
                   "owner": "tagged-devices", "ip": "100.9.9.9", "hosts": ["100.9.9.9:8788"]},
                  open(os.path.join(self.dir, "tailnet.json"), "w"))
        self.logins["100.4.4.4"] = "tagged-devices"
        self.assertFalse(self.ok("100.9.9.9:8788", "100.4.4.4"))

    def test_whois_of_a_tagged_peer_is_nobody(self):
        person = {"Node": {"Name": "phone.tail.ts.net."}, "UserProfile": {"LoginName": "owner@x"}}
        tagged = {"Node": {"Name": "ci.tail.ts.net.", "Tags": ["tag:ci"]},
                  "UserProfile": {"LoginName": "owner@x"}}   # whoever set the tag — the node is no longer his
        self.assertEqual(access.whois_login(person), "owner@x")
        self.assertIsNone(access.whois_login(tagged))
        self.assertIsNone(access.whois_login({"UserProfile": {"LoginName": "tagged-devices"}}))

    def whois_calls(self, *answers):
        """tailscale_whois over a clock of the test: answers[i] is what the i-th run of the CLI gives (an exception — raised);
        returns (ask(peer, at second), runs of the CLI)."""
        import types
        from unittest import mock
        runs, now = [], [1000.0]

        def run(argv, **kw):
            runs.append(argv)
            answer = answers[len(runs) - 1]
            if isinstance(answer, BaseException):
                raise answer
            return answer
        self.addCleanup(access._whois.clear)
        access._whois.clear()
        for patch in (mock.patch.object(access.subprocess, "run", run),
                      mock.patch.object(access, "time", types.SimpleNamespace(time=lambda: now[0]))):
            patch.start()
            self.addCleanup(patch.stop)

        def ask(peer, at):
            now[0] = 1000.0 + at
            return access.tailscale_whois(self.dir, peer)
        return ask, runs

    def test_whois_failure_is_asked_again_soon(self):
        # under load the CLI times out: the owner got 403 for the whole TTL, as if the address were nobody's
        import subprocess
        owner = subprocess.CompletedProcess([], 0, json.dumps({"UserProfile": {"LoginName": "owner@x"}}), "")
        ask, runs = self.whois_calls(subprocess.TimeoutExpired("tailscale", 10),
                                        subprocess.CompletedProcess([], 1, "", "no answer"), owner)
        self.assertIsNone(ask("100.1.1.1", 0))
        self.assertIsNone(ask("100.1.1.1", 1))            # not a run of the CLI per request
        self.assertEqual(len(runs), 1)
        self.assertIsNone(ask("100.1.1.1", 30))           # a nonzero exit is no answer either
        self.assertEqual(ask("100.1.1.1", 60), "owner@x")
        self.assertEqual(len(runs), 3)

    def test_requests_during_a_whois_wait_for_its_answer(self):
        # a page sends its requests in a bunch: while one whois runs (up to 10 s under load), the others must not each
        # start their own run of the CLI — they wait and take its answer
        import subprocess
        import threading
        import time as clock
        from unittest import mock
        owner = subprocess.CompletedProcess([], 0, json.dumps({"UserProfile": {"LoginName": "owner@x"}}), "")
        seen = []   # the peers whose run in flight a request looked up

        class Asking(dict):
            def get(self, key, default=None):
                seen.append(key)
                return super().get(key, default)
        got, second = [], []

        def during_the_first_run():
            # the second request comes while this run is going on; let the run end only once it either waits for
            # this run or has run the CLI itself
            t = threading.Thread(target=lambda: got.append(ask("100.3.3.3", 0)), daemon=True)
            t.start()
            second.append(t)
            deadline = clock.monotonic() + 10
            while len(seen) < 2 and len(runs) < 2 and clock.monotonic() < deadline:
                clock.sleep(0.01)
        ask, runs = self.whois_calls(owner, owner)
        fake = access.subprocess.run

        def run(argv, **kw):
            answer = fake(argv, **kw)
            if len(runs) == 1:
                during_the_first_run()
            return answer
        with mock.patch.object(access.subprocess, "run", run), mock.patch.object(access, "_asking", Asking(), create=True):
            self.assertEqual(ask("100.3.3.3", 0), "owner@x")
            second[0].join(10)
        self.assertEqual(got, ["owner@x"])
        self.assertEqual(len(runs), 1)

    def test_a_request_does_not_wait_for_a_stuck_run_forever(self):
        # the run in flight outlives its own timeout: the request waiting for it is refused (not known), not held
        import subprocess
        import threading
        from unittest import mock
        owner = subprocess.CompletedProcess([], 0, json.dumps({"UserProfile": {"LoginName": "owner@x"}}), "")
        got = []
        ask, runs = self.whois_calls(owner)
        fake = access.subprocess.run

        def run(argv, **kw):
            t = threading.Thread(target=lambda: got.append(ask("100.5.5.5", 0)), daemon=True)
            t.start()
            t.join(10)
            return fake(argv, **kw)
        with mock.patch.object(access.subprocess, "run", run), \
                mock.patch.object(access, "WHOIS_TIMEOUT", -4.95, create=True):   # the wait: 0.05 s
            self.assertEqual(ask("100.5.5.5", 0), "owner@x")
        self.assertEqual(got, [None])
        self.assertEqual(len(runs), 1)

    def test_whois_answer_is_kept(self):
        import subprocess
        guest = subprocess.CompletedProcess([], 0, json.dumps({"UserProfile": {"LoginName": "guest@x"}}), "")
        tagged = subprocess.CompletedProcess([], 0, json.dumps({"Node": {"Tags": ["tag:ci"]}}), "")
        ask, runs = self.whois_calls(guest, tagged, guest)
        self.assertEqual(ask("100.2.2.2", 0), "guest@x")
        self.assertEqual(ask("100.2.2.2", 200), "guest@x")
        self.assertIsNone(ask("100.4.4.4", 0))            # nobody's node: an answer, kept as long
        self.assertIsNone(ask("100.4.4.4", 200))
        self.assertEqual(len(runs), 2)
        self.assertEqual(ask("100.2.2.2", 400), "guest@x")  # past the TTL it is asked again
        self.assertEqual(len(runs), 3)

    def test_a_record_the_tailnet_part_stopped_rewriting_lets_nobody_in(self):
        # the tailnet part stopped (crashed, its lock held): its last working record admitted the tailnet for good, and
        # the switch turned off meanwhile changed nothing — a working record is rewritten every minute
        from unittest import mock
        path = os.path.join(self.dir, "tailnet.json")
        rec = {"ok": True, "owner": "owner@x", "ip": "100.9.9.9", "hosts": ["100.9.9.9:8788"], "ts": 1000.0}
        json.dump(rec, open(path, "w"))
        whois = lambda d, p: self.logins.get(p)   # noqa: E731
        for now, let_in in ((1000.0, True), (1000.0 + 479, True), (1000.0 + 481, False), (1000.0 - 30, True),
                            (1000.0 - 3600, False)):   # the clock set back an hour: not the record of now
            with mock.patch.object(access.time, "time", return_value=now):
                self.assertEqual(self.ok("100.9.9.9:8788", "100.1.1.1"), let_in, now)
                self.assertEqual(access.owner_peer(self.dir, "100.1.1.1", whois=whois), let_in, now)
                self.assertTrue(self.ok("127.0.0.1:8788", "127.0.0.1"))   # this machine is not the tailnet's
        for ts in (None, "now", True, 1e400, float("nan"), 10 ** 400, [1]):
            json.dump({**rec, "ts": ts} if ts is not None else {k: v for k, v in rec.items() if k != "ts"},
                      open(path, "w"))
            with mock.patch.object(access.time, "time", return_value=1000.0):
                self.assertFalse(self.ok("100.9.9.9:8788", "100.1.1.1"), ts)
                self.assertFalse(access.owner_peer(self.dir, "100.1.1.1", whois=whois), ts)

    def test_a_stale_record_reads_as_not_working_everywhere(self):
        # the card, `hub status`, the links and the board forwards read the same record the admission does
        from unittest import mock
        from modules.tailnet import run
        from modules.web import modinfo
        self.assertGreaterEqual(access.FRESH, run.STALE_AFTER + run.EVERY + run.CALLS * run.TIMEOUT + run.PAUSE)
        path = os.path.join(self.dir, "tailnet.json")
        json.dump({"ok": True, "ts": 1000.0, "owner": "owner@x", "ip": "100.9.9.9", "hosts": ["100.9.9.9:8788"],
                   "urls": {"web": "http://100.9.9.9:8787"}}, open(path, "w"))
        with mock.patch.object(access.time, "time", return_value=1000.0 + access.FRESH + 1):
            st = access.status(self.dir)
            self.assertEqual((st["ok"], st["stale"]), (False, True))
            self.assertEqual(access.addresses(st), [])
            self.assertIn("stopped updating", access.status_line(self.dir))
            (row,) = [m for m in modinfo.modules(self.dir, {"tailnet": {"enabled": True}}, {"sessions": []}, "en")
                      if m["id"] == "tailnet"]
            self.assertEqual(row["state"], "off")
            self.assertIn("stopped updating", row["detail"])
            (row,) = [m for m in modinfo.modules(self.dir, {}, {"sessions": []}, "en") if m["id"] == "tailnet"]
            self.assertIn("stopped updating", row["detail"])   # before the carry-over wrote the key, too
        # the frame-ancestors of "Files" drop the tailnet hosts once the record goes stale, the file itself unchanged
        with mock.patch.object(access.time, "time", return_value=1000.0):
            self.assertIn("http://100.9.9.9:8788", access.page_origins(self.dir, 8788))
        with mock.patch.object(access.time, "time", return_value=1000.0 + access.FRESH + 1):
            self.assertNotIn("http://100.9.9.9:8788", access.page_origins(self.dir, 8788))

    def test_tailnet_down_means_local_only(self):
        json.dump({"ok": False}, open(os.path.join(self.dir, "tailnet.json"), "w"))
        self.assertFalse(self.ok("100.9.9.9:8788", "100.1.1.1"))
        self.assertTrue(self.ok("127.0.0.1:8788", "127.0.0.1"))

    def test_page_origins_are_valid_csp_sources(self):
        # a host-source in CSP is a name or IPv4: the browser drops an IPv6 literal ([::1]) from frame-ancestors as an
        # error. A page opened on [::1] does not support embedding "Files"
        import re
        source = re.compile(r"http://[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*:\d+")
        origins = access.page_origins(self.dir, 8788)
        self.assertEqual([o for o in origins if not source.fullmatch(o)], [])
        self.assertIn("http://100.9.9.9:8788", origins)

    def test_page_origins_reread_the_file_only_when_it_changes(self):
        # "Files" asks for them on every response (and on every /raw chunk by Range): no file change — no read
        from unittest import mock
        path = os.path.join(self.dir, "tailnet.json")
        with mock.patch.object(access, "status", wraps=access.status) as reads:
            first = access.page_origins(self.dir, 8788)
            for _ in range(5):
                self.assertEqual(access.page_origins(self.dir, 8788), first)
            self.assertEqual(reads.call_count, 1)
            json.dump({"ok": True, "ts": time.time(),
                       "owner": "owner@x", "hosts": ["mac.tail.ts.net:8788"]}, open(path, "w"))
            st = os.stat(path)
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))   # another write time, as for a new write
            self.assertIn("http://mac.tail.ts.net:8788", access.page_origins(self.dir, 8788))
            self.assertEqual(reads.call_count, 2)
            os.remove(path)                                                   # no file — tailnet is not connected
            self.assertEqual(access.page_origins(self.dir, 8788), ["http://localhost:8788", "http://127.0.0.1:8788"])


class Log(unittest.TestCase):
    """The cause of a failure goes to the log when it changes, not once a minute (2026-10-13: without Tailscale — half
    of the hub-up.log lines)."""

    def test_same_reason_is_logged_once(self):
        import io
        from contextlib import redirect_stderr
        from modules.tailnet.run import report
        err, last = io.StringIO(), None
        with redirect_stderr(err):
            for st in ({"ok": False, "why": "no tailscale"}, {"ok": False, "why": "no tailscale"},
                       {"ok": True}, {"ok": True}, {"ok": False, "why": "no tailscale"},
                       {"ok": False, "why": "disabled"}):
                last = report(st, last)
        self.assertEqual(err.getvalue().splitlines(),
                         ["tailnet: no tailscale", "tailnet: working", "tailnet: no tailscale", "tailnet: disabled"])


class ForwardHostTest(unittest.TestCase):
    """A board forwarded into the tailnet (its server does not check Host) is admitted only under the node's own
    names: a site open on the owner's phone cannot reach it via DNS rebinding (sessions read the board's tasks)."""

    def setUp(self):
        import socket
        import threading
        import time
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from modules.tailnet.forward import Forward

        class Board(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"board " + self.headers.get("Host", "").encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        self.board = HTTPServer(("127.0.0.1", 0), Board)
        threading.Thread(target=self.board.serve_forever, daemon=True).start()
        self.addCleanup(self.board.server_close)
        self.addCleanup(self.board.shutdown)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        stop = threading.Event()
        self.addCleanup(stop.set)
        fw = Forward("127.0.0.1", self.port, self.board.server_address[1], allow=lambda peer: True, stop=stop,
                     hosts=lambda: {f"100.9.9.9:{self.port}", f"mac:{self.port}"})
        threading.Thread(target=fw.serve, daemon=True).start()
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.05)

    def get(self, host):
        import socket
        c = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        c.sendall(f"GET / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
        out = b""
        while True:
            chunk = c.recv(65536)
            if not chunk:
                break
            out += chunk
        c.close()
        return out.decode("latin-1")

    def test_own_names_pass(self):
        answer = self.get(f"100.9.9.9:{self.port}")
        self.assertIn(" 200 ", answer.split("\r\n")[0])
        self.assertTrue(answer.endswith(f"board 100.9.9.9:{self.port}"))
        self.assertIn(" 200 ", self.get(f"mac:{self.port}").split("\r\n")[0])

    def test_foreign_host_is_refused(self):
        for host in (f"evil.example:{self.port}", "100.9.9.9", f"127.0.0.1:{self.port}", ""):
            answer = self.get(host)
            self.assertNotIn("board", answer, host)
            self.assertIn(" 421 ", answer.split("\r\n")[0], host)

    def test_forwards_give_the_board_the_node_names(self):
        # Forwards (run.py) brings up the forward with the node names: IP, full name and short — with the board's port
        from unittest import mock
        from modules.tailnet import run
        d = tempfile.mkdtemp(prefix="fwd-")
        json.dump({"ok": True, "ts": time.time(), "ip": "100.9.9.9", "name": "mac.tail.ts.net", "owners": ["owner@x"]},
                  open(os.path.join(d, "tailnet.json"), "w"))
        json.dump({"6420": {}}, open(os.path.join(d, "boards.json"), "w"))
        made = []
        with mock.patch("modules.tailnet.forward.Forward", side_effect=lambda *a, **kw: made.append(kw) or mock.Mock()), \
                mock.patch("threading.Thread"):
            run.Forwards(d).reconcile()
        (kw,) = made
        self.assertEqual(kw["hosts"](), {"100.9.9.9:6420", "mac.tail.ts.net:6420", "mac:6420"})


if __name__ == "__main__":
    unittest.main()
