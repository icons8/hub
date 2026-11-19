"""The Secrets module (spec d2345345): the hub keeps secrets in its own vault; the owner puts them in and copies them on
the page; an agent puts one into a form field of the agents' Chrome (`hub secret fill`) or into a command's environment
(`hub secret exec`) and never gets the value back. All values are made up; the vault lives in a temp folder with its key
in a temp settings folder."""
import tests  # noqa: F401 — keep the machine's settings out
import io
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import platforms

from modules.secrets import cli, engine, vault
from modules.testchrome import chrome as testchrome

CHROME = testchrome.find()   # a module that starts a browser: tools/test.py runs it with the browser tests

ORIGIN = "https://www.npmjs.com"
VALUE = "s3cr3t-pass-probe"
LOGIN = "user@example.com"


def need_lib():
    try:
        from core import cryptolib
        cryptolib.aesgcm()
        return None
    except Exception as e:  # noqa: BLE001
        return str(e)


LIB_WHY = need_lib()
needs_lib = unittest.skipIf(LIB_WHY, f"no cryptography for the vault: {LIB_WHY}")


def fake_keychain(case):
    """macOS: a Keychain of the test's own, one key per account as the login Keychain keeps it — not the Keychain of the
    machine running the tests (the tests' HOME has no login Keychain: `security add-generic-password` would hang there)."""
    if sys.platform != "darwin":
        return
    keys = {}

    def secret_key(account, folder, create=False, **kw):
        if account not in keys and create:
            keys[account] = os.urandom(32)
        return keys.get(account)
    k = mock.patch("platforms.current.secret_key", secret_key)
    k.start()
    case.addCleanup(k.stop)
    f = mock.patch("platforms.current.secret_key_file", lambda folder: os.path.join(folder, "secrets.key"))
    f.start()
    case.addCleanup(f.stop)


class Folders(unittest.TestCase):
    """A state folder and a settings folder of their own (the key goes into the settings folder on Linux)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="secrets-")
        self.addCleanup(platforms.current.rmtree, self.dir)
        self.state = os.path.join(self.dir, "state")
        self.conf = os.path.join(self.dir, "conf")
        os.makedirs(self.state)
        p = mock.patch.dict(os.environ, {"HUB_CONFIG_DIR": self.conf, "HUB_STATE_DIR": self.state})
        p.start()
        self.addCleanup(p.stop)
        fake_keychain(self)

    def put(self, name="npm", password=VALUE, login=LOGIN, sites=(ORIGIN,), commands=False, note="web sign-in"):
        return vault.save(self.state, name, password=password, login=login, sites=list(sites), commands=commands,
                          note=note)


@needs_lib
class Vault(Folders):
    def test_a_value_is_sealed_on_disk_and_comes_back_only_by_value(self):
        self.assertEqual(self.put(), ["new", "value", "login", "sites", "note"])
        self.assertEqual(vault.save(self.state, "tok", password=VALUE), ["new", "value"])
        with open(vault.path(self.state), encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn(VALUE, raw)
        self.assertNotIn(LOGIN, raw)
        self.assertEqual(vault.value(self.state, "npm", "password"), VALUE)
        self.assertEqual(vault.value(self.state, "npm", "username"), LOGIN)
        shown = vault.items(self.state)["npm"]
        self.assertEqual((shown["sites"], shown["login"], shown["commands"], shown["value"]), ([ORIGIN], True, False, True))
        self.assertNotIn(VALUE, json.dumps(shown))

    @unittest.skipIf(os.name == "nt", "modes of POSIX")
    def test_the_file_and_its_folder_are_the_owners_only(self):
        self.put()
        self.assertEqual(stat.S_IMODE(os.stat(vault.path(self.state)).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(vault.folder(self.state)).st_mode), 0o700)

    def test_the_key_is_not_in_the_state_folder(self):
        self.put()
        for root, _, files in os.walk(self.state):
            for name in files:
                with open(os.path.join(root, name), "rb") as f:
                    data = f.read()
                self.assertNotIn(vault.key(self.state), data, name)

    def test_a_settings_folder_inside_the_state_folder_gets_no_key(self):
        if sys.platform == "darwin":
            self.skipTest("the Keychain keeps the key on macOS")
        inside = os.path.join(self.state, "conf")
        with mock.patch.dict(os.environ, {"HUB_CONFIG_DIR": inside}):
            with self.assertRaises(vault.Error) as e:
                self.put()
        self.assertEqual(e.exception.key, "cli.secrets.key_inside")
        self.assertFalse(os.path.exists(os.path.join(inside, "secrets.key")))

    @unittest.skipIf(os.name == "nt", "a symlink needs rights on Windows")
    def test_a_symlink_into_the_state_folder_is_inside_too(self):
        if sys.platform == "darwin":
            self.skipTest("the Keychain keeps the key on macOS")
        real = os.path.join(self.state, "real-conf")
        os.makedirs(real)
        link = os.path.join(self.dir, "link-conf")
        os.symlink(real, link)
        with mock.patch.dict(os.environ, {"HUB_CONFIG_DIR": link}):
            with self.assertRaises(vault.Error) as e:
                self.put()
        self.assertEqual(e.exception.key, "cli.secrets.key_inside")

    def test_a_box_moved_under_another_name_does_not_open(self):
        self.put()
        with open(vault.path(self.state), encoding="utf-8") as f:
            data = json.load(f)
        data["items"]["other"] = dict(data["items"]["npm"])
        vault._write(self.state, data)
        with self.assertRaises(vault.Error) as e:
            vault.value(self.state, "other", "password")
        self.assertEqual(e.exception.key, "cli.secrets.lost")

    def test_an_empty_change_keeps_the_value_and_says_what_changed(self):
        self.put()
        self.assertEqual(vault.save(self.state, "npm", commands=True), ["commands"])
        self.assertEqual(vault.save(self.state, "npm", password=VALUE), [])
        self.assertEqual(vault.save(self.state, "npm", login=""), ["login"])
        self.assertFalse(vault.items(self.state)["npm"]["login"])
        self.assertEqual(vault.value(self.state, "npm", "password"), VALUE)

    def test_wrong_input_is_refused_without_the_value_in_the_error(self):
        for kw, key in (({"password": "abc"}, "cli.secrets.bad_value"), ({"password": "two\nlines"}, "cli.secrets.bad_value"),
                        ({"password": "x" * 5000}, "cli.secrets.bad_value"), ({"sites": ["javascript:alert(1)"]}, "cli.secrets.bad_site"),
                        ({"note": "n" * 300}, "cli.secrets.bad_note")):
            with self.assertRaises(vault.Error, msg=kw) as e:
                vault.save(self.state, "npm", **{"password": VALUE, **kw})
            self.assertEqual(e.exception.key, key)
            self.assertNotIn(VALUE, repr(e.exception) + str(e.exception.params))
        for name in ("../etc", "", "a b", "x" * 41):
            with self.assertRaises(vault.Error):
                vault.save(self.state, name, password=VALUE)

    def test_a_new_secret_needs_a_value(self):
        with self.assertRaises(vault.Error):
            vault.save(self.state, "npm", sites=[ORIGIN])

    def test_sites_are_origins_from_what_a_person_types(self):
        self.put(sites=["gitlab.example.com", "https://GitLab.example.com/users/sign_in", "http://203.0.113.5:8080/x"])
        self.assertEqual(vault.items(self.state)["npm"]["sites"],
                         ["https://gitlab.example.com", "http://203.0.113.5:8080"])

    def test_a_lost_key_keeps_names_and_sites_and_starts_over(self):
        self.put()
        if sys.platform == "darwin":
            self.skipTest("the fake Keychain of the test cannot lose its key")
        os.unlink(os.path.join(self.conf, "secrets.key"))
        self.assertEqual(vault.status(self.state), "lost")
        with self.assertRaises(vault.Error) as e:
            vault.value(self.state, "npm", "password")
        self.assertEqual(e.exception.key, "cli.secrets.lost")
        aside = vault.start_over(self.state)
        self.assertTrue(os.path.exists(aside))
        shown = vault.items(self.state)["npm"]
        self.assertEqual((shown["value"], shown["sites"]), (False, [ORIGIN]))
        self.assertEqual(vault.status(self.state), "ok")
        self.put()   # a new key, the value again
        self.assertEqual(vault.value(self.state, "npm", "password"), VALUE)

    def test_a_key_that_is_not_the_vaults_is_lost_too(self):
        self.put()
        if sys.platform == "darwin":
            self.skipTest("the fake Keychain of the test keeps its key")
        with open(os.path.join(self.conf, "secrets.key"), "w") as f:
            f.write(os.urandom(32).hex() + "\n")
        self.assertEqual(vault.status(self.state), "lost")
        with self.assertRaises(vault.Error):
            self.put("other", password="an0ther-value")
        vault.start_over(self.state)
        self.put()
        self.assertEqual(vault.value(self.state, "npm", "password"), VALUE)

    def test_with_the_key_lost_a_save_does_not_make_a_new_one_over_the_sealed_values(self):
        self.put()
        if sys.platform == "darwin":
            self.skipTest("the fake Keychain of the test cannot lose its key")
        os.unlink(os.path.join(self.conf, "secrets.key"))
        with self.assertRaises(vault.Error) as e:
            self.put("other", password="an0ther-value")
        self.assertEqual(e.exception.key, "cli.secrets.lost")
        self.assertFalse(os.path.exists(os.path.join(self.conf, "secrets.key")))
        self.assertEqual(vault.status(self.state), "lost")
        vault.save(self.state, "npm", note="still")   # what is shown may change
        self.assertEqual(vault.items(self.state)["npm"]["note"], "still")

    def test_after_start_over_a_login_alone_does_not_make_a_value(self):
        self.put()
        if sys.platform == "darwin":
            self.skipTest("the fake Keychain of the test cannot lose its key")
        os.unlink(os.path.join(self.conf, "secrets.key"))
        vault.start_over(self.state)
        with self.assertRaises(vault.Error) as e:
            vault.save(self.state, "npm", login="bob")
        self.assertEqual(e.exception.key, "cli.secrets.bad_value")
        self.assertFalse(vault.items(self.state)["npm"]["value"])
        vault.save(self.state, "npm", password=VALUE, login="bob")
        self.assertEqual(vault.value(self.state, "npm", "username"), "bob")

    def test_sites_in_the_form_the_browser_names_its_origin(self):
        self.assertEqual(vault.origin_of("bücher.example"), "https://xn--bcher-kva.example")
        self.assertEqual(vault.origin_of("http://[::1]:8080/x"), "http://[::1]:8080")
        self.assertEqual(engine.origin_of("https://xn--bcher-kva.example/login"), "https://xn--bcher-kva.example")
        self.assertEqual(engine.origin_of("http://[::1]:8080/login"), "http://[::1]:8080")
        for bad in ("about:blank", "chrome://newtab", "data:text/html,x", "javascript:alert(1)", "", None):
            self.assertEqual(engine.origin_of(bad), "", bad)

    def test_two_hubs_have_two_keys(self):
        from core import instance
        self.put()
        other = os.path.join(self.dir, "state2")
        os.makedirs(other)
        instance.write(other, {"name": "second"})   # a second hub on one machine is an instance with a name of its own
        first = vault.key(self.state)
        with mock.patch.dict(os.environ, {"HUB_CONFIG_DIR": os.path.join(self.dir, "conf2"), "HUB_STATE_DIR": other}):
            vault.save(other, "npm", password="an0ther-value")
            self.assertNotEqual(vault.key(other), first)
            self.assertEqual(vault.value(other, "npm", "password"), "an0ther-value")
        self.assertEqual(vault.value(self.state, "npm", "password"), VALUE)

    # spec d2441132: a session may change a value; the previous ones stay for the owner to return
    def test_a_changed_value_keeps_the_previous_one_and_who_set_each(self):
        self.put()
        by = {"who": "maps", "source": "/home/x/maps/.env", "key": "NPM_PASS"}
        self.assertEqual(vault.save(self.state, "npm", password="n3w-pass-probe", by=by), ["value"])
        shown = vault.items(self.state)["npm"]
        self.assertEqual(shown["by"], {**by, "ts": shown["changed"]})
        self.assertEqual([p["by"]["who"] for p in shown["prev"]], [""])
        self.assertEqual(vault.value(self.state, "npm", "password"), "n3w-pass-probe")
        raw = json.dumps(vault.items(self.state))
        self.assertNotIn("box", raw)
        self.assertNotIn(VALUE, raw)
        # return the owner's value; the session's goes into the history in its turn, so a return can be returned
        self.assertTrue(vault.restore(self.state, "npm", shown["prev"][0]["id"]))
        self.assertEqual(vault.value(self.state, "npm", "password"), VALUE)
        self.assertEqual(vault.value(self.state, "npm", "username"), LOGIN)
        shown = vault.items(self.state)["npm"]
        self.assertEqual((shown["by"]["who"], len(shown["by"]["restored"])), ("", 8))   # the owner's own, returned
        self.assertEqual([{k: v for k, v in p["by"].items() if k != "ts"} for p in shown["prev"]], [by])
        self.assertTrue(vault.restore(self.state, "npm", shown["prev"][0]["id"]))
        self.assertEqual(vault.value(self.state, "npm", "password"), "n3w-pass-probe")

    def test_three_previous_values_each_returns_its_own_also_within_one_second(self):
        import datetime
        now = datetime.datetime(2026, 11, 17, 9, 0, 0, tzinfo=datetime.timezone.utc)
        for i in range(5):
            vault.save(self.state, "tok", password=f"value-{i}-probe", now=now)
        prev = vault.items(self.state)["tok"]["prev"]
        self.assertEqual(len(prev), 3)
        self.assertEqual(len({p["id"] for p in prev}), 3)
        ids = [p["id"] for p in prev]   # newest first: values 3, 2, 1
        self.assertTrue(vault.restore(self.state, "tok", ids[2], now=now))
        self.assertEqual(vault.value(self.state, "tok", "password"), "value-1-probe")
        self.assertTrue(vault.restore(self.state, "tok", ids[1], now=now))
        self.assertEqual(vault.value(self.state, "tok", "password"), "value-2-probe")
        self.assertFalse(vault.restore(self.state, "tok", "nosuchid", now=now))
        self.assertFalse(vault.restore(self.state, "none", ids[0], now=now))

    def test_a_note_or_switch_is_no_new_version_a_login_is(self):
        self.put()
        vault.save(self.state, "npm", password="n3w-pass-probe", by={"who": "maps", "source": "stdin"})
        vault.save(self.state, "npm", note="other", commands=True)   # the owner's note: the value is still the session's
        shown = vault.items(self.state)["npm"]
        self.assertEqual((shown["by"]["who"], len(shown["prev"])), ("maps", 1))
        vault.remove(self.state, "npm")
        self.put()
        vault.save(self.state, "npm", note="other", commands=True)
        self.assertEqual(vault.items(self.state)["npm"]["prev"], [])
        vault.save(self.state, "npm", login="bob@example.com")
        self.assertEqual(len(vault.items(self.state)["npm"]["prev"]), 1)

    def test_a_returned_value_keeps_who_set_it_and_from_where(self):
        self.put()
        by = {"who": "maps", "source": "/home/x/maps/.env", "key": "NPM_PASS"}
        vault.save(self.state, "npm", password="n3w-pass-probe", by=by)
        vault.save(self.state, "npm", password="th1rd-pass-probe")
        session_s = vault.items(self.state)["npm"]["prev"][0]
        self.assertTrue(vault.restore(self.state, "npm", session_s["id"]))
        shown = vault.items(self.state)["npm"]
        self.assertEqual({k: v for k, v in shown["by"].items() if k not in ("ts", "restored")}, by)
        self.assertGreaterEqual(shown["by"]["ts"], session_s["changed"])   # current since the return
        self.assertEqual(vault.value(self.state, "npm", "password"), "n3w-pass-probe")

    def test_remove_and_start_over_take_the_history_too(self):
        self.put()
        vault.save(self.state, "npm", password="n3w-pass-probe")
        vault.remove(self.state, "npm")
        self.put()
        vault.save(self.state, "npm", password="n3w-pass-probe")
        self.assertEqual(len(vault.items(self.state)["npm"]["prev"]), 1)
        if sys.platform == "darwin":
            return   # the fake Keychain of the test cannot lose its key
        os.unlink(os.path.join(self.conf, "secrets.key"))
        vault.start_over(self.state)
        with open(vault.path(self.state), encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("history", raw)
        self.assertNotIn('"by"', raw)   # no "added by" under a secret that has no value any more
        self.assertEqual(vault.items(self.state)["npm"]["prev"], [])

    def test_a_vault_of_the_earlier_release_reads_and_an_odd_author_is_cut(self):
        self.put()
        with open(vault.path(self.state), encoding="utf-8") as f:
            data = json.load(f)
        data["items"]["npm"].pop("by", None)
        data["items"]["npm"].pop("history", None)
        vault._write(self.state, data)
        shown = vault.items(self.state)["npm"]
        self.assertEqual((shown["by"]["who"], shown["prev"]), ("", []))
        vault.save(self.state, "npm", password="n3w-pass-probe", by={"who": "maps", "source": "/" + "d/" * 400 + ".env"})
        source = vault.items(self.state)["npm"]["by"]["source"]
        self.assertLessEqual(len(source), vault.SOURCE_MAX)
        self.assertIn("…", source)
        self.assertTrue(source.endswith(".env"))
        data["items"]["npm"]["by"], data["items"]["npm"]["history"] = "odd", [{"box": 1}, "x"]
        vault._write(self.state, data)
        shown = vault.items(self.state)["npm"]
        self.assertEqual((shown["by"], shown["prev"]), ({"who": ""}, []))

    def test_without_the_library_the_vault_says_so(self):
        from core import cryptolib
        with mock.patch.object(cryptolib, "aesgcm", side_effect=cryptolib.Unavailable("no_lib", "no")):
            self.assertEqual(vault.status(self.state), "no_lib")
            with self.assertRaises(vault.Error) as e:
                self.put()
        self.assertEqual(e.exception.key, "cli.secrets.no_lib")

    def test_no_cryptographic_primitives_of_its_own(self):
        import ast
        for path in (vault.__file__, os.path.join(os.path.dirname(os.path.dirname(vault.__file__)), "..", "core",
                                                  "cryptolib.py")):
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                    for m in mods:
                        self.assertNotIn(m.split(".")[0], ("hmac", "random", "ctypes", "nacl", "Crypto"), (path, m))
                if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.LShift, ast.RShift, ast.BitXor)):
                    self.fail(f"{path}:{node.lineno}: bit arithmetic — a primitive of its own?")


class FakePage:
    """A tab of the agents' Chrome: the probe answers with its state; insert() is what the real one does in one call."""

    def __init__(self, url=ORIGIN + "/login", tag="INPUT", type="password", origin=None, focused=True, then=None):
        self.target = {"id": "p1", "type": "page", "url": url, "webSocketDebuggerUrl": "ws://x/p1"}
        self.state = {"o": origin or ORIGIN, "t": tag, "ty": type, "f": focused}
        self.then = then   # what the tab does between the probe and the insert (a site moving the focus)
        self.typed, self.closed, self.inserts = [], False, []

    def js(self, expr):
        return json.dumps(self.state)

    def insert(self, value, origin, types):
        if self.then:
            self.then(self)
        self.inserts.append((origin, tuple(types)))
        s = self.state
        if s["o"] != origin or not s["f"] or s["t"] != "INPUT" or s["ty"] not in types:
            return "moved"
        self.typed.append(value)
        return getattr(self, "answer", "ok")

    def close(self):
        self.closed = True


class FakeStore:
    def __init__(self, meta=None, error=None):
        self.meta_ = {"npm": {"name": "npm", "sites": [ORIGIN], "login": True, "commands": False, "note": "",
                              "value": True}} if meta is None else meta
        self.asked, self.error = [], error

    def meta(self, name):
        return self.meta_.get(name)

    def value(self, name, field):
        self.asked.append((name, field))
        if self.error:
            raise self.error
        return VALUE if field == "password" else LOGIN


def cfg(**over):
    return {"secrets": {"browser": "agents", **over}, "browsers": {"agents": {"kind": "remote", "url": "http://127.0.0.1:9230"}}}


class Case(unittest.TestCase):
    def make(self, pages=None, store=None, config=None):
        self.pages = pages if pages is not None else [FakePage()]
        self.store = store or FakeStore()
        self.facts = []
        by_ws = {p.target["webSocketDebuggerUrl"]: p for p in self.pages}
        self.engine = engine.Engine(lambda: config if config is not None else cfg(),
                                    lambda kind, entity, data: self.facts.append((kind, entity, data)), store=self.store,
                                    targets=lambda url: [p.target for p in self.pages], connect=lambda ws: by_ws[ws])
        return self.engine

    def fill(self, field="password", who="pkg-helper", item="npm"):
        return self.engine.fill(who, item, field)


class Fill(Case):
    def test_value_goes_into_the_focused_field_and_not_into_the_answer(self):
        self.make()
        code, out = self.fill()
        self.assertEqual(code, 200, out)
        self.assertEqual(self.pages[0].typed, [VALUE])
        self.assertEqual(self.store.asked, [("npm", "password")])
        self.assertNotIn(VALUE, json.dumps(out))
        self.assertTrue(self.pages[0].closed)

    def test_no_owner_confirmation_on_the_way(self):
        self.make()
        self.assertEqual(self.fill()[0], 200)
        self.assertEqual([k for k, _, _ in self.facts], ["secret.requested", "secret.filled"])

    def test_the_hand_out_is_a_fact_before_the_value_leaves(self):
        order = []
        self.make()
        self.engine.put = lambda kind, who, data: order.append(kind)
        page = self.pages[0]
        orig = page.insert
        page.insert = lambda *a: order.append("insert") or orig(*a)
        self.fill()
        self.assertEqual(order, ["secret.requested", "insert", "secret.filled"])

    def test_username_into_text_or_email(self):
        for ty in ("text", "email"):
            self.make([FakePage(type=ty)])
            self.assertEqual(self.fill("username")[0], 200)
            self.assertEqual(self.pages[0].typed, [LOGIN])

    def test_a_secret_into_a_text_field_never(self):
        self.make([FakePage(type="text")])
        code, out = self.fill()
        self.assertEqual((code, out["key"]), (403, "cli.secrets.cursor_password"))
        self.assertEqual(self.store.asked, [])

    def test_no_place_for_the_value_the_vault_is_not_asked(self):
        for page in (FakePage(url="https://www.npmjs.com.evil.example/login", origin="https://www.npmjs.com.evil.example"),
                     FakePage(origin="https://evil.example"), FakePage(tag="IFRAME"), FakePage(tag="BODY"),
                     FakePage(focused=False), FakePage(type="search")):
            self.make([page])
            code, _ = self.fill()
            self.assertEqual(code, 403, page.state)
            self.assertEqual((self.store.asked, page.typed), ([], []))
        self.make([])
        self.assertEqual(self.fill()[1]["key"], "cli.secrets.no_tab")

    def test_a_focus_moved_by_the_site_after_the_check_gets_nothing(self):
        def moved(page):
            page.state = {**page.state, "t": "IFRAME"}
        self.make([FakePage(then=moved)])
        code, out = self.fill()
        self.assertEqual((code, out["key"]), (403, "cli.secrets.moved"))
        self.assertEqual(self.pages[0].typed, [])
        self.assertEqual(self.facts[-1][0], "secret.denied")

    def test_a_site_changing_the_value_is_reported(self):
        page = FakePage()
        page.answer = "changed"
        self.make([page])
        code, out = self.fill()
        self.assertEqual((code, out["key"]), (200, "cli.secrets.filled_changed"))
        self.assertTrue(self.facts[-1][2]["changed"])

    def test_a_secret_without_sites_or_login(self):
        self.make(store=FakeStore({"tok": {"name": "tok", "sites": [], "login": False, "commands": True, "note": "",
                                           "value": True}}))
        self.assertEqual(self.fill(item="tok")[1]["key"], "cli.secrets.no_sites")
        self.assertEqual(self.fill("username", item="tok")[1]["key"], "cli.secrets.no_login")
        self.assertEqual(self.fill(item="none")[1]["key"], "cli.secrets.not_listed")
        self.make(store=FakeStore({"api": {"name": "api", "sites": [], "login": True, "commands": True, "note": "",
                                           "value": True}}))
        self.assertEqual(self.fill("username", item="api")[1]["key"], "cli.secrets.no_sites")
        self.assertEqual(self.store.asked, [])

    def test_a_vault_failure_is_a_502_without_a_value_and_nothing_is_typed(self):
        self.make(store=FakeStore(error=engine.Unavailable("cli.secrets.lost")))
        code, out = self.fill()
        self.assertEqual((code, out["key"]), (502, "cli.secrets.lost"))
        self.assertEqual(self.pages[0].typed, [])
        self.assertEqual([k for k, _, _ in self.facts], ["secret.requested", "secret.failed"])

    def test_on_unless_turned_off(self):
        self.make(config=cfg(enabled=False))
        self.assertEqual(self.fill()[1]["key"], "cli.secrets.off")
        self.assertTrue(engine.enabled({}))
        self.assertTrue(engine.enabled({"secrets": {"enabled": True}}))
        self.assertFalse(engine.enabled({"secrets": {"enabled": False}}))

    def test_journal_has_who_what_where_and_never_the_value(self):
        self.make()
        self.fill()
        kind, who, data = self.facts[-1]
        self.assertEqual((kind, who, data), ("secret.filled", "pkg-helper",
                                             {"item": "npm", "field": "password", "origin": ORIGIN}))
        self.assertNotIn(VALUE, json.dumps(self.facts))

    def test_the_core_knows_the_facts_and_keeps_them_in_the_journal(self):
        from core import machines
        for kind in ("secret.requested", "secret.denied", "secret.filled", "secret.failed", "secret.used",
                     "secret.saved", "secret.removed", "secret.copied"):
            self.assertIn(kind, machines.FACTS, kind)

    def test_browser_url_comes_from_the_hub_browsers_by_name(self):
        c = cfg()
        self.assertEqual(engine.browser_url(c), "http://127.0.0.1:9230")
        c["secrets"]["browser"] = "nope"
        self.assertIsNone(engine.browser_url(c))
        del c["secrets"]["browser"]
        self.assertEqual(engine.browser_url(c), "http://127.0.0.1:9230")     # without a name — "agents", then the first one

    def test_an_earlier_page_server_still_constructs_it(self):
        # the server of the release before keeps running on the new files for a while: Engine(config, put, {})
        e = engine.Engine(lambda: {}, lambda *a: None, {})
        self.assertIsInstance(e.store, engine.Vault)
        self.assertEqual(engine.items({"secrets": {"items": {"x": {}}}}), {})


class Mask(unittest.TestCase):
    def run_mask(self, values, pieces):
        m = cli.Mask(values)
        return b"".join(m.feed(p) for p in pieces) + m.close()

    def test_a_value_is_replaced_also_across_pieces(self):
        text = f"token={VALUE}; again {VALUE}!".encode()
        for size in (1, 2, 3, 7, len(text)):
            pieces = [text[i:i + size] for i in range(0, len(text), size)]
            self.assertEqual(self.run_mask([("npm", VALUE)], pieces), b"token=[secret:npm]; again [secret:npm]!", size)

    def test_json_escaped_form_is_masked(self):
        v = 'q"uo\\te-' + "ü"
        for shown in (json.dumps({"t": v}), json.dumps({"t": v}, ensure_ascii=False)):
            out = self.run_mask([("x", v)], [shown.encode()])
            self.assertNotIn(v.encode(), out)
            self.assertIn(b"[secret:x]", out)

    def test_output_that_cannot_begin_a_value_is_not_held(self):
        m = cli.Mask([("tok", "zq9-" + "x" * 28)])
        self.assertEqual(m.feed(b"Continue? [y/N] "), b"Continue? [y/N] ")
        self.assertEqual(m.feed(b"key zq9-xx"), b"key ")   # might be the value: held until the next piece
        self.assertEqual(m.feed(b"!") + m.close(), b"zq9-xx!")

    def test_a_longer_value_that_begins_with_a_shorter_one_is_masked_whole_across_pieces(self):
        values = [("short", "abcd1234"), ("long", "abcd1234XYZ9")]
        text = b"key abcd1234XYZ9 and abcd1234 end"
        for size in range(1, len(text) + 1):
            pieces = [text[i:i + size] for i in range(0, len(text), size)]
            self.assertEqual(self.run_mask(values, pieces), b"key [secret:long] and [secret:short] end", size)
        self.assertEqual(self.run_mask(values, [b"tail abcd1234"]), b"tail [secret:short]")   # the end: no longer one
        values = [("short", "abcd1234"), ("long", "XXabcd1234YY")]   # the shorter one inside the longer one
        text = b"key XXabcd1234YY and abcd1234 end"
        for size in range(1, len(text) + 1):
            pieces = [text[i:i + size] for i in range(0, len(text), size)]
            self.assertEqual(self.run_mask(values, pieces), b"key [secret:long] and [secret:short] end", size)

    def test_a_value_inside_its_own_mark_does_not_loop(self):
        self.assertEqual(self.run_mask([("secret", "secret")], [b"a secret b", b" secret"]),
                         b"a [secret:secret] b [secret:secret]")


@needs_lib
class Cli(Folders):
    def run_cli(self, argv, config=None, answer=(200, {"ok": True, "text": "filled in"})):
        from contextlib import redirect_stdout
        sent, facts, buf = [], [], io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(argv, config if config is not None else {}, "pkg-helper",
                            lambda body: sent.append(body) or answer, state=self.state,
                            put=lambda kind, who, data, **kw: facts.append((kind, who, data)))
        return code, buf.getvalue(), sent, facts

    def test_fill_sends_the_session_item_and_field_and_prints_the_text(self):
        code, out, sent, _ = self.run_cli(["fill", "npm"])
        self.assertEqual((code, sent), (0, [{"session": "pkg-helper", "item": "npm", "field": "password"}]))
        self.assertIn("filled in", out)
        self.assertEqual(self.run_cli(["fill", "npm", "username"])[2][0]["field"], "username")

    def test_refusal_is_exit_code_2_with_the_reason(self):
        code, out, _, _ = self.run_cli(["fill", "npm"], answer=(403, {"error": "no tab"}))
        self.assertEqual(code, 2)
        self.assertIn("no tab", out)

    def test_list_shows_names_sites_and_notes_but_no_values(self):
        self.put()
        self.put("tok", login="", sites=(), commands=True, note="API")
        code, out, sent, _ = self.run_cli(["list"])
        self.assertEqual((code, sent), (0, []))
        self.assertIn("npm", out)
        self.assertIn(ORIGIN, out)
        self.assertIn("tok", out)
        self.assertNotIn(VALUE, out)
        self.assertNotIn(LOGIN, out)

    def test_off_only_when_turned_off(self):
        code, out, _, _ = self.run_cli(["list"], {"secrets": {"enabled": False}})
        self.assertEqual(code, 0)
        self.assertIn("off", out)
        self.assertEqual(self.run_cli(["list"])[0], 0)

    def test_wrong_usage_is_exit_code_2(self):
        for argv in ([], ["fill"], ["fill", "npm", "otp"], ["get", "npm", "password"], ["exec"],
                     ["exec", "--env", "T=npm"], ["exec", "--env", "T=npm", "--"], ["exec", "--env", "1T=npm", "--", "x"],
                     ["exec", "--env", "T=npm:pass", "--", "x"], ["exec", "--env", "T=../x", "--", "x"]):
            self.assertEqual(self.run_cli(argv)[0], 2, argv)

    def exec_python(self, code_text, name="tok"):
        out, err, facts = io.BytesIO(), io.BytesIO(), []
        o, e = (io.TextIOWrapper(b, encoding="utf-8", write_through=True) for b in (out, err))
        self.addCleanup(lambda: (o.detach(), e.detach()))   # the wrappers would close the buffers when collected
        with mock.patch.object(sys, "stdout", o), mock.patch.object(sys, "stderr", e):
            code = cli.run_with(["--env", f"TOKEN={name}", "--", sys.executable, "-c", code_text], self.state,
                                "pkg-helper", lambda kind, who, data, **kw: facts.append((kind, who, data)))
        return code, out.getvalue(), err.getvalue(), facts

    def test_exec_gives_the_value_in_the_environment_and_masks_it_in_the_output(self):
        self.put("tok", login="", sites=(), commands=True)
        code, out, err, facts = self.exec_python(
            "import os,sys,json; v=os.environ['TOKEN']; print('got', v); print(json.dumps({'t': v}));"
            "sys.stderr.write('err '+v+'\\n'); sys.exit(3 if v else 4)")
        self.assertEqual(code, 3)
        self.assertNotIn(VALUE.encode(), out + err)
        self.assertIn(b"got [secret:tok]", out)
        self.assertIn(b"err [secret:tok]", err)
        self.assertEqual([k for k, _, _ in facts], ["secret.requested", "secret.used"])
        self.assertEqual(facts[1][2], {"item": "tok", "field": "password", "env": "TOKEN",
                                       "program": os.path.basename(sys.executable), "code": 3})
        self.assertNotIn(VALUE, json.dumps(facts))

    def test_exec_without_the_owners_switch_is_refused_before_anything(self):
        self.put()
        code, out, err, facts = self.exec_python("print('ran')", name="npm")
        self.assertEqual((code, out, facts), (2, b"", []))
        self.assertIn(ORIGIN.encode(), err)

    def test_exec_gives_a_login_unmasked_it_is_no_secret(self):
        self.put("tok", login="ab", sites=(), commands=True)
        code, out, err, facts = self.exec_python("import os; print('user', os.environ['TOKEN'], 'about cabs')", name="tok:login")
        self.assertEqual((code, out.strip()), (0, b"user ab about cabs"))

    def test_exec_a_failure_after_the_hand_out_is_a_fact(self):
        self.put("tok", login="", sites=(), commands=True)
        with mock.patch.object(vault, "value", side_effect=vault.Error("cli.secrets.lost")):
            code, out, err, facts = self.exec_python("print('ran')")
        self.assertEqual((code, out), (2, b""))
        self.assertEqual([k for k, _, _ in facts], ["secret.requested", "secret.failed"])

    def test_exec_ends_its_command_and_records_the_outcome_when_the_reader_goes_away(self):
        self.put("tok", login="", sites=(), commands=True)
        facts = []
        with mock.patch.object(cli, "_pump", side_effect=BrokenPipeError):
            with self.assertRaises(BrokenPipeError):
                cli.run_with(["--env", "T=tok", "--", sys.executable, "-c", "import time; time.sleep(30)"], self.state,
                             "x", lambda kind, who, data, **kw: facts.append((kind, data)))
        self.assertEqual([k for k, _ in facts], ["secret.requested", "secret.used"])
        self.assertNotEqual(facts[1][1]["code"], 0)

    def test_two_creators_of_the_key_end_with_one(self):
        if sys.platform == "darwin":
            self.skipTest("the Keychain keeps the key on macOS")
        import platforms as p
        theirs = os.urandom(32)
        real_link = os.link

        def late(src, dst):   # another process put its key in between our look and our link
            with open(dst, "w") as f:
                f.write(theirs.hex() + "\n")
            return real_link(src, dst)
        os.makedirs(self.conf, exist_ok=True)
        with mock.patch.object(os, "link", late):
            self.assertEqual(p.current.secret_key("main", self.conf, True), theirs)
        self.assertEqual(p.current.secret_key("main", self.conf, False), theirs)
        self.assertEqual([f for f in os.listdir(self.conf) if f.startswith("secrets.key.")], [])

    def test_exec_of_a_missing_program(self):
        self.put("tok", login="", sites=(), commands=True)
        facts, err = [], io.BytesIO()
        e = io.TextIOWrapper(err, encoding="utf-8", write_through=True)
        self.addCleanup(e.detach)
        with mock.patch.object(sys, "stderr", e):
            code = cli.run_with(["--env", "T=tok", "--", os.path.join(self.dir, "no-such-program")], self.state, "x",
                                lambda kind, who, data, **kw: facts.append(kind))
        self.assertEqual((code, facts), (127, ["secret.requested", "secret.used"]))


class Add(Folders):
    """`hub secret add` (spec d2441132): the value goes from a file or stdin to the page process, never to the output."""
    ENV = ("# deploy\nexport DB_URL=postgres://db.example/app\nGITLAB_TOKEN = glpat-probe-1234 # read_api\n"
           "QUOTED=\"a \\\"q\\\" b\\\\c\"\nSINGLE='lit\\n $x'\nDUP=first\nDUP=last-probe\n")

    def file(self, text, name=".env", raw=None):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as f:
            f.write(raw if raw is not None else text.encode("utf-8"))
        return path

    def run_add(self, argv, answer=(200, {"ok": True, "new": True, "changed": ["value"]}), stdin=b"", config=None):
        from contextlib import redirect_stderr, redirect_stdout
        sent, out, err = [], io.StringIO(), io.StringIO()
        fake_in = io.TextIOWrapper(io.BytesIO(stdin), encoding="utf-8")
        with redirect_stdout(out), redirect_stderr(err), mock.patch.object(sys, "stdin", fake_in):
            code = cli.main(["add", *argv], config if config is not None else {}, "maps",
                            lambda body, **kw: sent.append((body, kw)) or answer, state=self.state)
        return code, out.getvalue() + err.getvalue(), sent

    def picked(self, text, key):
        code, out, sent = self.run_add(["tok", "--from-file", self.file(text), *(["--key", key] if key else [])])
        self.assertEqual(code, 0, out)
        return sent[0][0]["password"]

    def test_a_key_of_a_dotenv_file_goes_to_the_page_and_never_to_the_output(self):
        path = self.file(self.ENV)
        code, out, sent = self.run_add(["gitlab-token", "--from-file", path, "--key", "GITLAB_TOKEN", "--commands",
                                        "--site", "gitlab.example.com", "--note", "API"])
        self.assertEqual(code, 0, out)
        body, kw = sent[0]
        self.assertEqual(kw, {"path": "/api/secrets/add", "timeout": cli.ADD_TIMEOUT})
        self.assertEqual(body, {"session": "maps", "name": "gitlab-token", "password": "glpat-probe-1234",
                                "source": os.path.realpath(path), "key": "GITLAB_TOKEN", "commands": True,
                                "sites": ["gitlab.example.com"], "note": "API"})
        self.assertIn("gitlab-token", out)
        self.assertNotIn("glpat-probe", out)

    def test_dotenv_lines_as_shells_and_dotenv_read_them(self):
        self.assertEqual(self.picked(self.ENV, "QUOTED"), 'a "q" b\\c')
        self.assertEqual(self.picked(self.ENV, "SINGLE"), "lit\\n $x")
        self.assertEqual(self.picked(self.ENV, "DUP"), "last-probe")
        self.assertEqual(self.picked(self.ENV, "DB_URL"), "postgres://db.example/app")
        self.assertEqual(self.picked("﻿A=one-probe\r\nB=two-probe\r\n", "B"), "two-probe")

    def test_without_a_key_a_one_line_file_is_the_value_as_it_is(self):
        self.assertEqual(self.picked("abcd=efgh==\n", None), "abcd=efgh==")
        self.assertEqual(self.picked("  spaced # token\r\n", None), "  spaced # token")
        code, out, _ = self.run_add(["tok", "--from-file", self.file("TOKEN=abc-probe\n")])
        self.assertEqual(code, 0)
        self.assertIn(cli.i18n.t("cli.secrets.looks_env"), out)   # NAME=value: the answer says how to take the value
        self.assertNotIn("abc-probe", out)
        # base64 with its padding looks like NAME=value too: the part before "=" is the token, it is never said
        code, out, sent = self.run_add(["tok", "--from-file", self.file("c2VjcmV0LXRva2VuLXZhbHVl==\n")])
        self.assertEqual((code, sent[0][0]["password"]), (0, "c2VjcmV0LXRva2VuLXZhbHVl=="))
        self.assertNotIn("c2VjcmV0", out)

    def test_several_lines_without_a_key_name_the_keys_not_the_values(self):
        code, out, sent = self.run_add(["tok", "--from-file", self.file(self.ENV)])
        self.assertEqual((code, sent), (2, []))
        for k in ("DB_URL", "GITLAB_TOKEN", "QUOTED", "DUP"):
            self.assertIn(k, out)
        for v in ("glpat-probe", "last-probe", "postgres://"):
            self.assertNotIn(v, out)

    def test_lines_of_tokens_are_never_named_as_keys(self):
        # base64 lines with padding match NAME=value: the refusal names capital .env names only
        code, out, sent = self.run_add(["tok", "--from-file", self.file("dG9rZW4tb25l==\nZm9vYmFyYmF6==\nAPI_KEY=x-probe\n")])
        self.assertEqual((code, sent), (2, []))
        self.assertIn("API_KEY", out)
        self.assertNotIn("dG9rZW4", out)
        self.assertNotIn("Zm9vYmFy", out)
        code, out, sent = self.run_add(["tok", "--from-file", self.file("dG9rZW4tb25l==\nZm9vYmFyYmF6==\n"), "--key", "X"])
        self.assertEqual((code, sent), (2, []))
        self.assertNotIn("dG9rZW4", out)
        # base32 and capital hex keys with their padding are capitals too: a line with no value after "=" is no NAME line
        for text in ("JBSWY3DPEHPK3PX=\nABCDEF0123456789ABCDEF==\n", "JBSWY3DPEHPK3PX=\n"):
            code, out, sent = self.run_add(["tok", "--from-file", self.file(text), *(["--key", "X"] if "\n" in text[:-1] else [])])
            self.assertNotIn("JBSWY3", out, text)
            self.assertNotIn("ABCDEF0123", out, text)
        code, out, sent = self.run_add(["tok", "--from-file", self.file("JBSWY3DPEHPK3PX=\n")])
        self.assertEqual((code, sent[0][0]["password"]), (0, "JBSWY3DPEHPK3PX="))
        self.assertNotIn(cli.i18n.t("cli.secrets.looks_env"), out)   # no advice to pass the token's start as --key
        # names in small letters are not named either: the answer says "one line", not an empty list
        code, out, _ = self.run_add(["tok", "--from-file", self.file("db_user=a-probe\ndb_pass=b-probe\n")])
        self.assertEqual(code, 2)
        self.assertNotIn("(+", out)
        self.assertNotIn("probe", out.replace(self.dir, ""))

    def test_json_fields_by_key_and_the_login_from_the_same_file(self):
        path = self.file(json.dumps({"token": "json-probe-1", "user": "bot@example.com", "deep": {"a": "b"}}), "x.json")
        code, out, sent = self.run_add(["tok", "--from-file", path, "--key", "token", "--login-key", "user"])
        self.assertEqual((code, sent[0][0]["password"], sent[0][0]["login"]), (0, "json-probe-1", "bot@example.com"), out)
        code, out, sent = self.run_add(["tok", "--from-file", path, "--key", "deep"])
        self.assertEqual((code, sent), (2, []))
        code, out, sent = self.run_add(["tok", "--from-file", path, "--key", "missing"])
        self.assertEqual((code, sent), (2, []))
        self.assertIn("token", out)
        self.assertNotIn("json-probe", out)

    def test_stdin(self):
        code, out, sent = self.run_add(["tok", "--from-file", "-"], stdin=b"piped-probe-99\n")
        self.assertEqual((code, sent[0][0]["password"], sent[0][0]["source"]), (0, "piped-probe-99", "stdin"), out)
        code, out, sent = self.run_add(["tok", "--from-file", "-", "--key", "B"], stdin=b"A=1\nB=env-probe-2\n")
        self.assertEqual((sent[0][0]["password"], sent[0][0]["source"], sent[0][0]["key"]), ("env-probe-2", "stdin", "B"))

    def test_what_cannot_be_read_is_refused_without_its_content(self):
        big = self.file("", "big", raw=b"x" * (cli.FILE_MAX + 1))
        cases = [self.file("A=\"multi\\nline-probe\"\n"), self.file("", "cp", raw="caf\u00e9-probe".encode("latin-1")),
                 big, self.dir, os.path.join(self.dir, "missing"), self.file("one-probe\ntwo-probe\n")]
        for path in cases:
            key = ["--key", "A"] if path == cases[0] else []
            code, out, sent = self.run_add(["tok", "--from-file", path, *key])
            self.assertEqual((code, sent), (2, []), (path, out))
            self.assertNotIn("probe", out.replace(self.dir, ""), path)
            self.assertNotIn("xxxx", out, path)

    def test_the_answers_of_the_page(self):
        path = self.file("tok-probe-5\n")
        code, out, _ = self.run_add(["tok", "--from-file", path], answer=(
            200, {"ok": True, "new": False, "changed": ["value"], "was": {"who": "", "changed": "2026-11-16T09:00:00Z"}}))
        self.assertEqual(code, 0)
        self.assertIn("2026-11-16", out)
        self.assertEqual(self.run_add(["tok", "--from-file", path], answer=(200, {"ok": True, "new": False,
                                                                                 "changed": []}))[0], 0)
        code, out, _ = self.run_add(["tok", "--from-file", path, "--commands"], answer=(
            409, {"error": "x", "key": "cli.secrets.add_owner_parts", "params": {"name": "tok"}}))
        self.assertEqual(code, 2)
        self.assertIn("--commands", out)
        for answer in ((404, {"error": "no such action"}), (401, {"error": "sign in"})):   # a page of an earlier release
            code, out, _ = self.run_add(["tok", "--from-file", path], answer=answer)
            self.assertEqual(code, 2)
            self.assertIn(cli.i18n.t("cli.secrets.page_old"), out)
        self.assertEqual(self.run_add(["tok", "--from-file", path], config={"secrets": {"enabled": False}})[0], 2)

    def test_a_page_too_slow_to_answer_is_not_said_to_have_refused(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self.addCleanup(srv.close)
        with mock.patch("core.runtime.state_dir", return_value=self.state):
            code, out = cli.post(srv.getsockname()[1])({"name": "tok"}, path="/api/secrets/add", timeout=0.5)
        self.assertEqual((code, out["key"]), (504, "cli.secrets.page_slow"))

    def test_wrong_lines_are_usage(self):
        path = self.file("tok-probe-5\n")
        for argv in ([], ["tok"], ["tok", "--from-file"], ["tok", "--key", "A"], ["tok", "--from-file", path, "--odd"],
                     ["tok", "--from-file", path, "--login", "a", "--login-key", "B"], ["../x", "--from-file", path]):
            code, out, sent = self.run_add(argv)
            self.assertEqual((code, sent), (2, []), argv)


class Wire(unittest.TestCase):
    """The page: POST /api/secrets/fill → engine; `hub secret` is a thin client."""

    def setUp(self):
        from tests import test_web
        self.h = test_web.WebTest("test_page_and_snapshot")
        self.h.setUp()
        self.calls = []
        outer = self

        class Fake:
            def fill(self, who, item, field):
                outer.calls.append((who, item, field))
                return 200, {"ok": True, "text": "filled in"}
        self.h.web.secrets = Fake()

    def tearDown(self):
        self.h.tearDown()

    def test_fill_goes_to_the_engine_with_the_session_name(self):
        code, data = self.h.req("POST", "/api/secrets/fill", {"session": "pkg-helper", "item": "npm", "field": "password"})
        self.assertEqual((code, json.loads(data)["text"]), (200, "filled in"))
        self.assertEqual(self.calls, [("pkg-helper", "npm", "password")])

    def test_bad_body_and_odd_names_do_not_reach_the_engine(self):
        for body in ({}, {"item": 1, "field": "password"}, {"item": "npm", "field": ["x"]}, {"item": "../etc", "field": "password"},
                     {"item": "npm", "field": "password", "session": "x" * 200}):
            code, _ = self.h.req("POST", "/api/secrets/fill", body)
            self.assertEqual(code, 400, body)
        self.assertEqual(self.calls, [])

    def test_without_the_page_header_it_is_refused_like_every_action(self):
        code, _ = self.h.req("POST", "/api/secrets/fill", {"item": "npm", "field": "password"}, headers={"X-Hub": "0"})
        self.assertEqual(code, 403)
        self.assertEqual(self.calls, [])


@needs_lib
class Tab(unittest.TestCase):
    """Settings -> Secrets: GET /api/secrets and its actions, through the page server."""

    def setUp(self):
        from tests import test_web
        self.conf = tempfile.mkdtemp(prefix="secrets-conf-")
        self.addCleanup(platforms.current.rmtree, self.conf)
        p = mock.patch.dict(os.environ, {"HUB_CONFIG_DIR": self.conf})
        p.start()
        self.addCleanup(p.stop)
        fake_keychain(self)
        self.h = test_web.WebTest("test_page_and_snapshot")
        self.h.setUp()
        self.addCleanup(self.h.tearDown)

    def get(self):
        code, data = self.h.req("GET", "/api/secrets")
        self.assertEqual(code, 200, data)
        return json.loads(data)

    def facts(self):
        return [(e["type"], e.get("data") or {}) for e in self.h.spooled()]

    def add(self, **over):
        body = {"name": "npm", "password": VALUE, "login": LOGIN, "sites": ["www.npmjs.com"], "commands": False,
                "note": "web", "new": True, **over}
        return self.h.req("POST", "/api/secrets/save", body)

    def test_add_list_copy_remove_and_the_value_only_in_copy(self):
        self.assertEqual(self.get()["items"], [])
        self.assertEqual(self.add()[0], 200)
        got = self.get()
        self.assertEqual([i["name"] for i in got["items"]], ["npm"])
        self.assertEqual(got["items"][0]["sites"], [ORIGIN])
        self.assertTrue(got["values"])
        self.assertIsInstance(got["unlocked"], list)   # the hubs without a page password (core/pagepass.unprotected)
        self.assertNotIn(VALUE, json.dumps(got))
        self.assertNotIn(LOGIN, json.dumps(got))
        code, data = self.h.req("POST", "/api/secrets/copy", {"name": "npm", "field": "password"})
        self.assertEqual((code, json.loads(data)["value"]), (200, VALUE))
        code, data = self.h.req("POST", "/api/secrets/copy", {"name": "npm", "field": "username"})
        self.assertEqual(json.loads(data)["value"], LOGIN)
        self.assertEqual(self.h.req("POST", "/api/secrets/remove", {"name": "npm"})[0], 200)
        self.assertEqual(self.get()["items"], [])
        facts = self.facts()
        self.assertEqual([k for k, _ in facts], ["secret.saved", "secret.copied", "secret.copied", "secret.removed"])
        self.assertNotIn(VALUE, json.dumps(facts))
        self.assertNotIn(LOGIN, json.dumps(facts))

    def test_a_second_add_under_the_same_name_does_not_overwrite(self):
        self.add()
        code, _ = self.add(password="an0ther-value")
        self.assertEqual(code, 409)
        self.assertEqual(json.loads(self.h.req("POST", "/api/secrets/copy", {"name": "npm"})[1])["value"], VALUE)

    def test_a_change_without_a_value_keeps_it(self):
        self.add()
        self.assertEqual(self.h.req("POST", "/api/secrets/save", {"name": "npm", "commands": True, "sites": []})[0], 200)
        item = self.get()["items"][0]
        self.assertEqual((item["commands"], item["sites"]), (True, []))
        self.assertEqual(json.loads(self.h.req("POST", "/api/secrets/copy", {"name": "npm"})[1])["value"], VALUE)

    def test_bad_input_is_a_400_with_the_reason_and_no_value(self):
        code, data = self.add(password="ab")
        self.assertEqual(code, 400)
        self.assertNotIn("ab\"", data.decode())
        self.assertEqual(self.add(sites=["javascript:alert(1)"])[0], 400)
        self.assertEqual(self.h.req("POST", "/api/secrets/copy", {"name": "none"})[0], 400)

    def test_values_only_from_this_machine_or_the_tailnet(self):
        from modules.web import secretsweb
        self.add()
        for path, ok in (("tailnet", True), ("remote", False), ("some-provider", False)):
            with mock.patch.object(secretsweb, "via", return_value=path):
                self.assertEqual(self.get()["values"], ok, path)
                self.assertEqual(self.h.req("POST", "/api/secrets/copy", {"name": "npm"})[0], 200 if ok else 403, path)

    def test_through_cloudflare_no_value_travels_either_way(self):
        from modules.web import secretsweb
        self.add()
        with mock.patch.object(secretsweb, "via", return_value="cloudflare"):
            got = self.get()
            self.assertFalse(got["values"])
            self.assertEqual(self.h.req("POST", "/api/secrets/copy", {"name": "npm"})[0], 403)
            self.assertEqual(self.add(name="other")[0], 403)
            # sites, notes and removal work there
            self.assertEqual(self.h.req("POST", "/api/secrets/save", {"name": "npm", "note": "x"})[0], 200)
            self.assertEqual(self.h.req("POST", "/api/secrets/remove", {"name": "npm"})[0], 200)

    def test_start_over_only_when_the_key_is_lost(self):
        self.add()
        self.assertEqual(self.h.req("POST", "/api/secrets/start-over", {})[0], 409)
        self.assertEqual(json.loads(self.h.req("POST", "/api/secrets/copy", {"name": "npm"})[1])["value"], VALUE)

    def test_without_the_page_header_nothing(self):
        self.add()
        code, _ = self.h.req("POST", "/api/secrets/copy", {"name": "npm"}, headers={"X-Hub": "0"})
        self.assertEqual(code, 403)

    # spec d2441132: `hub secret add` — a session puts a value it took from a file into the vault
    def session_add(self, **over):
        body = {"session": "maps", "name": "tok", "password": VALUE, "source": "/home/x/maps/.env", "key": "TOK", **over}
        code, data = self.h.req("POST", "/api/secrets/add", body)
        return code, json.loads(data)

    def test_a_session_adds_a_secret_and_the_tab_names_it_and_the_file(self):
        code, out = self.session_add(sites=["gitlab.example.com"], commands=True, note="API")
        self.assertEqual((code, out["new"]), (200, True), out)
        item = self.get()["items"][0]
        self.assertEqual((item["name"], item["sites"], item["commands"]), ("tok", ["https://gitlab.example.com"], True))
        self.assertEqual(item["by"], {"who": "maps", "source": "/home/x/maps/.env", "key": "TOK", "ts": item["changed"]})
        facts = self.h.spooled()
        self.assertEqual([(e["type"], e.get("entity")) for e in facts], [("secret.saved", "maps")])
        self.assertEqual((facts[0]["data"]["by"], facts[0]["data"]["source"], facts[0]["data"]["key"],
                          facts[0]["data"]["owner"]), ("session", "/home/x/maps/.env", "TOK", False))
        self.assertNotIn(VALUE, json.dumps(facts) + json.dumps(out) + json.dumps(self.get()))
        code, out = self.session_add(session="", password="an0ther-value")   # the owner's terminal: no session
        self.assertEqual(self.get()["items"][0]["by"]["who"], ":terminal")

    def test_a_session_changes_only_the_value_of_an_existing_one_and_the_owner_returns_it(self):
        self.add(name="tok")
        code, out = self.session_add(password="n3w-value-probe", sites=["evil.example"])
        self.assertEqual((code, out["key"]), (409, "cli.secrets.add_owner_parts"))
        self.assertEqual(self.session_add(password="n3w-value-probe", commands=True)[0], 409)
        self.h.spooled()
        code, out = self.session_add(password="n3w-value-probe")
        self.assertEqual((code, out["new"], out["changed"], out["was"]["who"]), (200, False, ["value"], ""), out)
        item = self.get()["items"][0]
        self.assertEqual((item["sites"], item["commands"]), ([ORIGIN], False))   # the owner's, as they were
        self.assertEqual(json.loads(self.h.req("POST", "/api/secrets/copy", {"name": "tok"})[1])["value"], "n3w-value-probe")
        self.h.spooled()
        code, data = self.h.req("POST", "/api/secrets/restore", {"name": "tok", "id": item["prev"][0]["id"]})
        self.assertEqual(code, 200, data)
        self.assertEqual(json.loads(self.h.req("POST", "/api/secrets/copy", {"name": "tok"})[1])["value"], VALUE)
        facts = self.h.spooled()
        self.assertEqual([(e["type"], e.get("entity"), e["data"].get("restored")) for e in facts][:1],
                         [("secret.saved", None, item["prev"][0]["id"])])
        self.assertEqual(self.h.req("POST", "/api/secrets/restore", {"name": "tok", "id": "00000000"})[0], 404)

    def test_the_same_value_changes_nothing_and_writes_no_fact(self):
        self.session_add()
        self.h.spooled()
        code, out = self.session_add()
        self.assertEqual((code, out["changed"]), (200, []))
        self.assertEqual(self.h.spooled(), [])

    def test_a_session_add_is_refused_off_remote_and_with_odd_input(self):
        from modules.web import secretsweb
        with mock.patch.object(secretsweb, "via", return_value="cloudflare"):
            code, out = self.session_add()
        self.assertEqual((code, out["key"]), (403, "web.secrets.no_values_remote"))   # not "a page of an earlier release"
        with mock.patch.object(self.h.web, "config", return_value={"secrets": {"enabled": False}}):
            code, out = self.session_add()
        self.assertEqual((code, out["key"]), (403, "cli.secrets.off"))
        for over in ({"session": "../x"}, {"session": 1}, {"password": "ab"}, {"password": None}, {"source": 7},
                     {"name": "a b"}, {"key": ""}, {"key": "k" * 65}, {"key": "A\nB"}):
            code, out = self.session_add(**over)
            self.assertEqual(code, 400, over)
            self.assertNotIn(VALUE, json.dumps(out))
        self.assertEqual(self.get()["items"], [])

    def test_the_owner_returns_a_value_also_through_cloudflare_it_does_not_travel(self):
        from modules.web import secretsweb
        self.add(name="tok")
        self.session_add(password="n3w-value-probe")
        prev = self.get()["items"][0]["prev"][0]["id"]
        with mock.patch.object(secretsweb, "via", return_value="cloudflare"):
            code, data = self.h.req("POST", "/api/secrets/restore", {"name": "tok", "id": prev})
        self.assertEqual(code, 200, data)
        self.assertNotIn(VALUE, data.decode())

    def test_the_session_s_feed_says_what_it_put_in_and_from_where(self):
        from core import i18n, reducer, views
        st = reducer.empty()
        st["sessions"]["maps"] = {}
        rows = []
        for changed in (["new", "value"], ["value"]):
            ev = {"seq": 1, "ts": "2026-11-17T09:00:00Z", "type": "secret.saved", "entity": "maps",
                  "data": {"item": "tok", "changed": changed, "by": "session", "source": "/home/x/maps/.env", "key": "TOK"}}
            rows += [(s, r["text"]) for s, r in views._items(st, ev)]
        self.assertEqual(rows, [("maps", i18n.t("core.views.secret.added", item="tok", source="/home/x/maps/.env (TOK)")),
                                ("maps", i18n.t("core.views.secret.changed", item="tok", source="/home/x/maps/.env (TOK)"))])
        self.assertNotIn("core.views", rows[0][1])   # the catalog has the text
        owners = {"seq": 2, "ts": "2026-11-17T09:00:00Z", "type": "secret.saved", "entity": None,
                  "data": {"item": "tok", "changed": ["value"]}}
        self.assertEqual(views._items(st, owners), [])

    def test_the_last_use_comes_from_the_fold(self):
        from core import reducer
        st = reducer.empty()
        for seq, (kind, data) in enumerate((("secret.requested", {"item": "npm", "field": "password", "origin": ORIGIN}),
                                            ("secret.filled", {"item": "npm", "field": "password", "origin": ORIGIN}),
                                            ("secret.used", {"item": "tok", "field": "password", "env": "T",
                                                             "program": "glab", "code": 0}),
                                            ("secret.requested", {"item": "key", "field": "password", "env": "T",
                                                                  "program": "curl"}),
                                            ("secret.removed", {"item": "gone"})), 1):
            st, _, why = reducer.reduce(st, {"seq": seq, "type": kind, "entity": "pkg-helper",
                                             "ts": f"2026-11-16T09:0{seq}:00Z", "data": data})
            self.assertIsNone(why, kind)
        last = st["secrets_last"]
        self.assertEqual((last["npm"]["how"], last["npm"]["outcome"], last["npm"]["where"]), ("fill", "ok", ORIGIN))
        self.assertEqual((last["tok"]["how"], last["tok"]["outcome"], last["tok"]["where"]), ("exec", "ok", "glab"))
        self.assertEqual(last["key"]["outcome"], "pending")
        st, _, _ = reducer.reduce(st, {"seq": 8, "type": "secret.denied", "entity": "pkg-helper", "ts": "2026-11-16T09:08:00Z",
                                       "data": {"item": "key", "field": "password", "why": "moved"}})
        self.assertEqual(st["secrets_last"]["key"]["outcome"], "failed")
        st, _, _ = reducer.reduce(st, {"seq": 10, "type": "secret.denied", "entity": "other", "ts": "2026-11-16T09:10:00Z",
                                       "data": {"item": "tok", "field": "password", "why": "no tab"}})
        self.assertEqual(st["secrets_last"]["tok"]["outcome"], "ok")   # a refusal before a hand-out is no use
        st, _, _ = reducer.reduce(st, {"seq": 9, "type": "secret.removed", "ts": "2026-11-16T09:09:00Z",
                                       "data": {"item": "npm"}})
        self.assertNotIn("npm", st["secrets_last"])
        self.assertNotIn(VALUE, json.dumps(st["secrets_last"]))


@needs_lib
class SessionRule(Folders):
    """The SECRETS part of a session's system prompt: only on a hub whose vault holds a secret, and not when turned off."""

    def prompt(self, config=None):
        from hosts import streamjson
        path = os.path.join(self.state, "hub.json")
        with open(path, "w") as f:
            json.dump(config or {}, f)
        with mock.patch.dict(os.environ, {"HUB_CONFIG": path}):
            h = streamjson.StreamHost(self.state, claude=(sys.executable, "-c", "pass"))   # only the command line is built
            cmd = h.command("w", "s-1")
            with open(cmd[cmd.index("--append-system-prompt-file") + 1], encoding="utf-8") as f:
                return f.read()

    def test_only_with_a_secret_kept_and_the_feature_on(self):
        from hosts import streamjson
        self.assertNotIn(streamjson.SECRETS, self.prompt())
        self.put()
        self.assertIn(streamjson.SECRETS, self.prompt())
        self.assertNotIn(streamjson.SECRETS, self.prompt({"secrets": {"enabled": False}}))

    def test_the_card_rule_offers_secrets_only_where_the_part_is(self):
        # the feature's switch, not the vault: an empty vault is where the first password is added, so the option stays
        # there, though the SECRETS part is not in the prompt yet; off — no Secrets option, nor `hub secret list`
        from hosts import streamjson
        text = self.prompt()
        self.assertIn(streamjson.CARDS, text)
        self.assertNotIn(streamjson.CARDS_NO_SECRETS, text)
        self.assertNotIn(streamjson.SECRETS, text)
        self.put()
        text = self.prompt()
        self.assertIn(streamjson.CARDS, text)
        self.assertNotIn(streamjson.CARDS_NO_SECRETS, text)
        text = self.prompt({"secrets": {"enabled": False}})
        self.assertIn(streamjson.CARDS_NO_SECRETS, text)
        self.assertNotIn("Settings → Secrets", text)
        self.assertNotIn("hub secret", text)

    def test_a_broken_vault_is_no_part_and_no_failed_launch(self):
        from hosts import streamjson
        os.makedirs(vault.folder(self.state))
        with open(vault.path(self.state), "w") as f:
            f.write("not json")
        self.assertNotIn(streamjson.SECRETS, self.prompt())


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


FORM = b"""<!doctype html><meta charset="utf-8"><title>sign in</title>
<form><input id="user" type="email"><input id="pass" type="password"><iframe srcdoc="<input id=inner>"></iframe></form>
<script>
window.seen = [];
document.getElementById("pass").addEventListener("input", (e) => window.seen.push(e.target.value.length));
</script>"""


class _Form(threading.Thread):
    def __init__(self):
        import http.server

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(FORM)

            def log_message(self, *a):
                pass
        self.server = http.server.HTTPServer(("127.0.0.1", 0), H)
        super().__init__(target=self.server.serve_forever, daemon=True)


class InChrome(unittest.TestCase):
    """The one call in the tab on a real headless Chrome: into the checked element only, events reach the site."""

    def setUp(self):
        chrome = CHROME
        if not chrome:
            self.skipTest(testchrome.INSTALL)
        self.form = _Form()
        self.form.start()
        self.addCleanup(self.form.server.shutdown)
        self.origin = f"http://127.0.0.1:{self.form.server.server_address[1]}"
        self.port = free_port()
        self.profile = tempfile.mkdtemp(prefix="secrets-chrome-")
        root = ["--no-sandbox"] if hasattr(os, "geteuid") and os.geteuid() == 0 else []
        self.chrome = subprocess.Popen([chrome, "--headless=new", *testchrome.NO_AUDIO, *root,
                                        "--remote-debugging-address=127.0.0.1", f"--remote-debugging-port={self.port}",
                                        f"--user-data-dir={self.profile}", "--no-first-run", "--no-default-browser-check",
                                        "--disable-component-update", "--disable-background-networking",
                                        self.origin + "/login"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(self.stop)
        from modules.browser import browsers
        end = time.time() + 60
        while True:
            try:
                tabs = [t for t in browsers.tabs(f"http://127.0.0.1:{self.port}") if t.get("type") == "page"
                        and t.get("url", "").startswith(self.origin)]
                if tabs:
                    break
            except OSError:
                pass
            if self.chrome.poll() is not None or time.time() > end:
                self.skipTest("Chrome did not come up")
            time.sleep(0.2)
        self.tab = engine.Session(tabs[0]["webSocketDebuggerUrl"])
        self.addCleanup(self.tab.close)
        self.tab.call("Emulation.setFocusEmulationEnabled", {"enabled": True})
        end = time.time() + 20
        # /json/list names the address while the document may still be about:blank
        while not self.tab.js(f"location.origin === {json.dumps(self.origin)} && document.readyState === 'complete' "
                              "&& !!document.getElementById('pass')"):
            if time.time() > end:
                self.fail("the form did not load")
            time.sleep(0.1)

    def stop(self):
        testchrome.stop(self.chrome, profile=self.profile)
        platforms.current.rmtree(self.profile)

    def test_the_value_goes_into_the_focused_password_field_and_the_site_sees_it(self):
        self.tab.js("document.getElementById('pass').focus()")
        self.assertEqual(self.tab.insert(VALUE, self.origin, ("password",)), "ok")
        self.assertEqual(self.tab.js("document.getElementById('pass').value"), VALUE)
        self.assertEqual(self.tab.js("window.seen"), [len(VALUE)])
        self.assertEqual(self.tab.js("document.getElementById('user').value"), "")
        # the page's scripts do not see the variables of the isolated world
        self.assertIsNone(self.tab.js("typeof v === 'undefined' ? null : v"))

    def test_a_wrong_field_type_or_another_origin_gets_nothing(self):
        self.tab.js("document.getElementById('user').focus()")
        self.assertEqual(self.tab.insert(VALUE, self.origin, ("password",)), "moved")
        self.tab.js("document.getElementById('pass').focus()")
        self.assertEqual(self.tab.insert(VALUE, "https://evil.example", ("password",)), "moved")
        self.tab.js("document.querySelector('iframe').focus()")
        self.assertEqual(self.tab.insert(VALUE, self.origin, ("password",)), "moved")
        self.assertEqual(self.tab.js("document.getElementById('pass').value + document.getElementById('user').value"), "")

    def test_a_value_with_quotes_emoji_and_line_separators_comes_through_whole(self):
        v = "a'b\"c\\d</script>\u2028\U0001F511x"
        self.tab.js("document.getElementById('pass').focus()")
        self.assertEqual(self.tab.insert(v, self.origin, ("password",)), "ok")
        self.assertEqual(self.tab.js("document.getElementById('pass').value"), v)


if __name__ == "__main__":
    unittest.main()
