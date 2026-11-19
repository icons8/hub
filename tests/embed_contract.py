"""What the core expects of a slot's answers (core/embed.py, spec d2144426 §2.3): the shape of what it reads and what an
empty hub answers. An embed's tests take the mixin of each slot it fills, with the slot's module:

    class Contract(embed_contract.Stats, unittest.TestCase):
        module = stats

The names and parameters are core/embed.NAMES (check() holds them); here — only what a call gives back."""
import os
import socket
import tempfile

import platforms


class _Slot:
    module = None   # the slot's module, set by the test class

    def folder(self):
        top = tempfile.mkdtemp()
        self.addCleanup(platforms.current.rmtree, top)
        return top


class Profile(_Slot):
    def test_an_empty_pool(self):
        pool = self.module.load(os.path.join(self.folder(), "none.json"))
        self.assertIsInstance(pool, dict)
        self.assertIn("accounts", pool)
        self.assertIs(self.module.enabled(pool), False)

    def test_a_fingerprint_names_a_token_and_only_it(self):
        one = self.module.fingerprint("token-one")
        self.assertIsInstance(one, str)
        self.assertEqual(one, self.module.fingerprint("token-one"))
        self.assertNotEqual(one, self.module.fingerprint("token-two"))
        self.assertNotIn("token-one", one)

    def test_a_tally_counts_under_the_name(self):
        got = self.module.tally({}, "a", "ok", 0)
        self.assertIsInstance(got, dict)
        self.assertIn("a", got)


class Gateway(_Slot):
    def test_off_by_default(self):
        got = self.module.settings({})
        self.assertIs(got.get("enabled"), False)
        self.assertIsInstance(got.get("port"), int)

    def test_its_folder_is_in_the_state_and_an_empty_one_has_no_status(self):
        state = self.folder()
        self.assertEqual(os.path.dirname(os.path.abspath(self.module.folder(state))), os.path.abspath(state))
        self.assertIsNone(self.module.status_of(state))

    def test_a_port_nobody_listens_on_is_not_reachable(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.assertIs(self.module.reachable(port, 0.2), False)


class Conduit(_Slot):
    def test_it_has_a_main(self):
        self.assertTrue(callable(self.module.main))


class Speech(_Slot):
    def test_settings_and_what_is_missing(self):
        self.assertIsInstance(self.module.settings({}), dict)
        self.assertIsInstance(self.module.missing({}), list)
        self.assertTrue(issubclass(self.module.SpeakError, Exception))


class VoiceCloud(_Slot):
    def test_nothing_is_chosen_in_an_empty_hub_and_a_choice_holds(self):
        state = self.folder()
        self.assertIs(self.module.chosen(state), False)
        self.module.choose(state)
        self.assertIs(self.module.chosen(state), True)

    def test_its_row_and_its_summary_for_the_page(self):
        state = self.folder()
        row = self.module.row("en", state)
        self.assertIsInstance(row, dict)
        self.assertEqual((row.get("role"), row.get("cloud")), ("cloud", True))
        for key in ("id", "name", "note"):
            self.assertIsInstance(row.get(key), str, key)
        got = self.module.summary(state)
        self.assertLessEqual({"fallback", "on_text", "here", "last"}, set(got))


class Stats(_Slot):
    def test_off_by_default(self):
        got = self.module.settings({})
        self.assertIsInstance(got, dict)
        self.assertIs(got.get("enabled"), False)


class CodexPool(_Slot):
    def test_no_provider_no_pool(self):
        class Mods:
            def declared(self, point):
                return []
        pool = self.module.Pool(self.folder(), Mods(), lambda: {}, lambda: 0, log=lambda line: None)
        self.assertIsNone(pool.listing())
        self.assertEqual(pool.names()[0], None)
        self.assertIs(pool.silent(), False)
        name, why = pool.add()
        self.assertIsNone(name)
        self.assertIsInstance(why, str)

    def test_a_malformed_list_is_refused(self):
        with self.assertRaises(self.module.BadAnswer):
            self.module.check_list({"on": "yes"}, {})


SLOTS = {"profile": Profile, "gateway": Gateway, "conduit": Conduit, "speech": Speech, "voice_cloud": VoiceCloud,
         "stats": Stats, "codex_pool": CodexPool}
