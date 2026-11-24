"""Language data (core/langdata.py, i18n/data/*.json): the files load, every key the code names is there, a rule built
from no data matches nothing, the union over languages keeps order without repeats, and the earlier forms of the core's
texts never change (journals and state.json of every hub hold them)."""
import hashlib
import json
import os
import pathlib
import re
import unittest
from unittest import mock

from core import langdata

ROOT = pathlib.Path(__file__).resolve().parent.parent
# a call with literal names: legacy("key") or words|alt|merged("section", "key"); a name built in code (lessons) is not one
CALL = re.compile(r"langdata\.(legacy|words|alt|merged)\(\s*[\"']([a-z_]+)[\"'](?:\s*,\s*[\"']([a-z_]+)[\"'])?\s*\)")
# get(<lang literal or langdata.OLD>, "section", "key"[, default]): the default hides a missing key, so it is checked here
GET = re.compile(r"langdata\.get\(\s*(?:[\"']([a-z-]+)[\"']|langdata\.OLD)\s*,\s*[\"']([a-z_]+)[\"']\s*,\s*[\"']([a-z_]+)[\"']")
CODE = ("core", "modules", "hosts", "hooks", "bin", "platforms", "tools")

# sha256 of each earlier form as JSON (sort_keys): a new key adds its line, an existing one never changes — the code reads
# these texts back from old journals, so an edit is a loss of what every hub recorded
FROZEN = {
    "check_now": "cd40449099cc8888785dbbfa9d593f6e1cb2f52d4ada2156e64a5496b1c0bdfc",
    "decline_notice": "342fadbbe62ef185c0279c9023999118ed20ad31c271a0154edc0ef354af3394",
    "flake_line": "ce0a519357116179c46f0701b79b463b848e2a15ba1418b75dc0d11a7b87c88c",
    "flow_words": "564ffe97c69499afc897ca2300cea31d7c9d41ef72bf6bb89721c3670ec96ff9",
    "from": "2140ebc57dfddfc9f0dce6fc0bffcd746d8b01fbcb99b154cf4d2bb74f35a0a8",
    "fyi_for": "6f6228ed6f275f3234f0a2db244266ff710ac23f649da61a0e1216c2c8a8e04e",
    "from_named": "b430d862f0d2b694f52623e400ae3a151985d023bb01ee877ba6ab0dfef91c4d",
    "from_words": "d0f29577aeb968c688251fd985d3486be28e6355c1b7313407a6246efb83312e",
    "git_no_answer": "92e9c508dfc458122bfe8150a27bbcd5fa72b944808eea8d7b58e5e478c50172",
    "git_not_starting": "3dc35f4bf942d3eb463c6f153239400cdc70f94c7222827872ad342cb82f6686",
    "head_answer": "bfbbb83800f3fe576c221c2e735af44e4b6b79c9ca62cc13af8b59e100dfac61",
    "head_context": "81c7b999bd5194febfcd0ab36544abc72a98ebdcde02b782a6f7c2da627b46d4",
    "head_hub": "916874b0fc4be435df99fb77622cd5e5eba8069c591abbe6ddbbffcc7ee8ef89",
    "head_passed": "db7b20d46a11a4c97a835003097dd982e9f38161ec963830dde832d638e4dd3b",
    "head_task": "04a0f05d4623229b34d3a875b99ea85265b79ace3ba924b79b736269d62296f2",
    "head_voice": "558b9883b2836bb83ca7892387935fa12c887ea0b8d0e259b4915a8cf61f58b2",
    "head_words": "b272873d655b1139314eefb445fd73b8cd1144b21804d5826ee0bee80cf5c953",
    "hub_word": "29cbda41e06cdfeaeb0c74219b9f010daf5ac5f60a26f24e970773370fec7b9d",
    "job_line": "c83bfdbf78b0fa3b07eaeacf51ab25d968beffb02c8c96e4c4f96582f64ad567",
    "lang_codes": "fc343f858e182a9ee9d2e18f36fd6691718ae5a04ed05c221565392a8632233c",
    "lesson_hint": "52b0ce4f53c8ba63852d5d2a3091bb57ace24d7680e4e9a461650c9c0397aa5a",
    "mcp_refusals": "fc189142474a4b8c2b20dc9c3aa4fedbb940028756d612b9dc42c4be73181dab",
    "origin": "2c03d094cef603b1c91ac84a2a99895310a03b4282dc29bd189aa39fff9ae318",
    "plugright_unreadable": "58fe4917f948f898efcfd00b46dd850232caa19fe2cb0b954972b77b7c8d18e1",
    "post_by":"74b2ad3f1833bd8cd3fac317cece3c11481c341d6b803c97c7f20e4e7db90d24",
    "prefixes": "6091e72ea56dadb321caa14aa3c7fe0b9dd16f90860e01714f80e67efc1ffddb",
    "proof_root": "9ffb13275ec4d48add56d3cb5ec11820c0b82ada793ef25692a553aca9ead0f6",
    "quiet_sys": "541aedaed9b1380e4c4e9599b4e25b32398a6f2acfe83a19e4c4ee7915a615de",
    "record_task": "835ae65634a59d4384e422ceb227d1b9577c77a48164951ea47238dcf94c662f",
    "reply_head": "61aa34a7b8be70615f76681556e00f192be4903f700a5e575fb64db277b97913",
    "report_rule": "3c11619a888c2f814d4827659419a6d099c472bf3b508f0b5754085461106a5f",
    "restart_by": "94e2ceed51622fa390e68a7b8be4a6e23ce5296eb851595b2852fdf3dad9aae8",
    "schedule_handed": "4b1ad50cf506dcc78fd3e6198df71cbbd260e22cdf1d5d51e2a3608d4e93545a",
    "session_section": "1682cae12630d33de27ec86940528d4da3ec1a3089dedff8248af527e365331a",
    "wait": "de96e182f85a96a53d374038af602887319454944cbb136419e58285564508fa",
    "watch_session": "5c7a4e0c3252ca4e8c157883d1a33547359c2d3909c77f521628df3ee6db1056",
    "watch_state": "dbf841b33f7320918e76d692a24e031e316d497ea6420d43748f1b99450714b7",
    "who": "ea544a7c25bf952153aae10bcdf1f245462372532d3f612ace757cfc6d4b37bb",
    "yes_note": "c0a1f168285b4a4e143c39587de057b74ed1b4de7dd456b536c24ec23769824b",
    "dismissed": "6852e5820b4f12c4441fac339865187acc405345fa355f8e346df3fb3534be8e",
    "more_context": "35d4cb11e7c7759336fe977f1638afa4fe4390ded1e623f6aeaa07bca8d7c025",
    "nothing": "99f86efe1bd92779b83514a8b30544ce161eec8f4d37fb69f488b7a75802733a",
    "owner_denied": "75bdf70f6e99968f7c55e03edab21f1d3b04f111e2a4c4228f49c2b36f1ed418",
    "solo_auto": "eb0c278e1505de3d06576e3b342f6dcbacc5a557c8ee97db7fccd9f11e3e60bb",
    "task_record": "88078fafb42def45c7e09561862d75183269350310caf433ebfc810e45b4d26d",
    "package_chrome": "d84bea84814418ad433506fcaf93f9a069a2a3582ce4b94c55f21683c88f3014",
    "pool_golden": "219a2dc08ca700f622159ef074e3bf3aa428443d8b23ef05f90be1729f15449a",
}
# the same for every other value of every file (<lang>.<section>.<key>): what the core recognises in the owner's and the
# sessions' words. The tests take their Russian from this data, so a word dropped from it would fail nothing else: here
# an edit is red until its hash is updated on purpose
RECOGNISED = {
    "ru.comms.followup": "940aff8811c7f664fd9e438de7d2dd294717245200b715214cd86ef85b193f72",
    "ru.letters.fold": "44e9cf343daa66ac480517954ee250b77953b813e9226ab56d76a6dc0fa7610f",
    "ru.letters.own": "0d33dbe1eba51a9034683760fd6551743c70898971fe8fc2c12e9568e7cfe103",
    "ru.letters.own_not_south": "4500c4712912e18458e7e1c251e37aafe8fbb015e3a967808d04b25da34f33c1",
    "ru.letters.south": "139c6c2f56231b4fef195b6f6301a39f624c55ec408d00a0704857070b252836",
    "ru.keyboard.keys": "9c2161f95b1f16332eff50aa1f57f11d6736964d10e86009ec87b7e56b90ec49",
    "ru.letters.south_hard_sign": "690f459a30b94f300fe5a9b35abb995cd581a978ba1bad379c6bd7b09a58cf11",
    "ru.runner.lines": "9ed92abcfa8c35dbe3217267e317f728521a2e19c3564dbc3aeaf293ed369f3a",
    "ru.self_name.name": "6d5ee05ec656b8bf8a94af00d48cd39ea588dfdf97852042c5d8ebcc5414f0d3",
    "ru.translit.latin": "803422e3e8a755fd1cf1e5b53808407477a41c5e15efcf3e0174a4ae1ecfb04c",
    "ru.watch.doubt": "72e16fae2d3ff47ab69bf23cfa673f8b3bf2e3df706dc08529972f86d8549bc7",
    "ru.watch.hub_order": "c5bb887a64e65fcf8b7825810f27241a0d19d7004774285a9677db3af1d07161",
    "ru.watch.hub_service": "45d7094eaf00ca153a3295e7b9a911a4e91551b4f45083fa13343aa36f9985e3",
    "ru.watch.promise_if": "0fd5094654b0e638eb8750eb659d6bc8a5e28f4a5fc78b74a0308549320aec57",
    "ru.watch.promise_next": "17a144d4f8321825694a6059f7a77968f3defaac160a13b3f7f5cece76880f21",
    "ru.watch.promise_relay": "abe32204b587ca9e99cd258b7e1d6cdaea9a0d59b979c710ca3fdaafc9e32d84",
    "ru.watch.promise_verbs": "1963ddebbec0c25127adbf4b992054d4f2fd07ae83db18ef891aedf3da81963d",
    "ru.watch.recheck": "7d1728636c88cf932225b9532c1666ec0571fb8cdac128403092edb3c6a6dc72",
    "ru.watch.status_q": "4d5b886e049319be9d692e75d404184c62077dd2a9aac83da59f7ec34da27288",
    "ru.watch.surrender": "ecfb9844fae7b6a43aeab41132fa5b13b69711287a4e9b66919e0eb691532bf4",
    "ru.words.gate": "964e4ae2a5e0518f03951fb5e1b475cd6bdec91ef1f57486e211796e7f7b984b",
    "ru.words.hub": "dfd197379ef6686e58d833c0e7c18b166ed9dd31c4df0dcb3b500b22c29c6f5a",
    "ru.words.lessons_but": "0a3fbc1c1c4410191c9710b3679c29ac0db705b194f543be113b36e3c70fa9a2",
    "ru.words.lessons_for": "268340f0ef07a6e0673b7c9beff81e2e00bd1cdafa336233ba1c7042f1afba24",
    "ru.words.lessons_genitive": "0699dccd13897968c89faf55e8ae19061616a2c11c822202506a2ff4899d928e",
    "ru.words.lessons_head": "e9baae4e58fe34daf3183afebb7dd9c410c5eee7cecb7fadeaa960b8a8184d92",
    "ru.words.lessons_no": "b9f548e427617d728a67f4556fb192396fbb2d86094dc5f3948ac8211ef9218c",
    "ru.words.lessons_none": "c733114755abb32faf33a3844fa74d24b88b7d0a4c9cb798d71a5f5caadb2e9c",
    "ru.words.lessons_none_claude": "7d96e8ad0641979a1ef8f14b851f84f07eaa66138191407a4eb5bf1fe166f2b0",
    "ru.words.lessons_word": "3b2062fc3de88934c1a6d2234ce947a86fa8a3cb8698af9b8e2bd9e1afa7013c",
    "ru.words.no_data": "af1fdd5ad0933d26277a7c0a862dba0fc0bb221ba3b44eac575d53c211e80c65",
    "ru.words.people_stems": "184d6d6afcdf7266cbd22056e502ecd97912515a14d1d3df82f199aa9c10b540",
    "ru.words.recap_examples": "233c15201a1b2eb46bc6e8f893b75d44024102d6c22a77723be4274287378985",
    "ru.words.recommended": "a6ed4bb06b3118dab64b9de1771c80193f95cdb0582155659db56a33c3d329e8",
    "ru.words.topic_generic": "f375f93044e891988b111af14d85ac9e4b49a17861f8a009ae594fa8bda7722a",
    "ru.words.topic_label": "37e4f5d6e8de1c04635962dd7b2e15371ebb229b86df7a0809f07208d5dc40af",
    "ru.words.yes": "96e169334bb5796f47efd2441a68d9c1852bd1c5a01e95ccb69a7cdf6a87a1f9",
    "uk.words.people_stems": "1ef2a681430d9ae58299c61ed1588abaa3006b5d1021b1c71f8eed02219edb77",
    "uk.words.gate": "d1aec5ea8c1bdde8279ff02c884ac1e33427f0466768add67ab8350e63f6ea7b",
    "uk.letters.own": "2eb789e987d2fad5fd3a907c8c277c21b0cbda50c01bfa2773ffe8be934ce6df",
    "uk.self_name.name": "612d9ef2fb363823bfe553311d933c1115ed2902be1610a0c6f9e7909a659d29",
    "ru.secrets.labels": "4a54e59d56c171ee73610567a114875f047243d7ac595e82a719f28ec41c702f",
    "uk.secrets.labels": "cfbb29d6f48f1fd6bdc256855a66e5532b9e7c5be4a600ce5eec5290ba5d6d98",
    "de.secrets.labels": "aab149fb6efa92aa0f4a867d7c9dba5e0cde319bd0ba0ca2d9f6257da5ab4edd",
    "es.secrets.labels": "42d7b85c29e9c9b0a4b88fdeccdb2c542be038b0e9a9411bd4da34b4ea585bea",
    "fr.secrets.labels": "160f529f717e93ac37317aa24fa6168be53bab700969de7e5dc947b846890b35",
    "pt-BR.secrets.labels": "404b795f691dc3fe02208e1df5bbe6dcafeff0410b5237e91cf1762b205bd5d4",
    "tr.secrets.labels": "a0227e85efd8a2a0015565f9fe7ab48b5819cc0baf7900c37dc37ea1e5ca9025",
    "ar.secrets.labels": "c57ceeb06d8e55425959120a6455e76c05c49b0d63421010562e6381373967de",
    "hi.secrets.labels": "a4328a7b59d9aa3175720797427db7a3e22a27d0a2a7d372a4c6cb52605581f7",
    "ja.secrets.labels": "3baef7826692a11338ec31ab2418b9af3f586d6ad84ef2e8ce6c0a4e14016bb9",
    "ko.secrets.labels": "8bdd1bbbca9a4aa39750d18c369b89337dac3e3311ed7a8dcc0c8e51e19fd4f2",
    "zh-CN.secrets.labels": "3f5ddd2170b11647936cc6be3419b16f5ffd4687e4f71ff8c5b52ce698147c50",
}


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _code_calls():
    for top in CODE:
        for path in (ROOT / top).rglob("*"):
            if path.is_file() and (path.suffix == ".py" or path.parent.name == "bin"):
                text = path.read_text(encoding="utf-8", errors="replace")
                for m in CALL.finditer(text):
                    yield path.relative_to(ROOT).as_posix(), m.groups()


class Files(unittest.TestCase):
    def test_every_language_file_loads_as_sections(self):
        for lang in langdata.LANGS:
            with self.subTest(lang=lang):
                data = langdata.load(lang)
                for section, part in data.items():
                    if not section.startswith("_"):
                        self.assertIsInstance(part, dict, section)

    def test_only_the_old_language_has_earlier_forms(self):
        for lang in langdata.LANGS:
            self.assertEqual("legacy" in langdata.load(lang), lang == langdata.OLD, lang)

    def test_every_key_the_code_names_is_in_the_data(self):
        seen = 0
        for path, (fn, a, b) in _code_calls():
            seen += 1
            with self.subTest(path=path, call=(fn, a, b)):
                if fn == "legacy":
                    self.assertIn(a, langdata.load(langdata.OLD)["legacy"])
                elif b:
                    self.assertTrue(any(langdata.get(lang, a, b) is not None for lang in langdata.LANGS), (a, b))
        self.assertGreater(seen, 40)
        gets = 0
        for top in CODE:
            for path in (ROOT / top).rglob("*.py"):
                for lang, section, key in GET.findall(path.read_text(encoding="utf-8", errors="replace")):
                    gets += 1
                    with self.subTest(path=path.name, get=(lang, section, key)):
                        self.assertIsNotNone(langdata.get(lang or langdata.OLD, section, key), (lang, section, key))
        self.assertTrue(gets)   # the pattern still finds the calls
        for k in ("head", "none", "none_claude", "word", "but", "no"):   # core/lessons.py builds these names
            self.assertTrue(langdata.words("words", "lessons_" + k), k)

    def test_every_regex_fragment_compiles_alone(self):
        for section in ("watch",):
            for lang in langdata.LANGS:
                for key, value in (langdata.load(lang).get(section) or {}).items():
                    with self.subTest(lang=lang, key=key):
                        re.compile(value)
        for lang in langdata.LANGS:   # tools/private_check.py builds its regex from these pairs
            for pair in (langdata.load(lang).get("runner") or {}).get("lines", []):
                with self.subTest(lang=lang, runner=pair):
                    self.assertTrue(isinstance(pair, list) and len(pair) == 2 and all(isinstance(w, str) and w for w in pair))
        for lang in langdata.LANGS:   # tools/comms_stats.py reads its follow-up phrases with the stdlib
            for value in (langdata.load(lang).get("comms") or {}).get("followup", []):
                with self.subTest(lang=lang, followup=value):
                    re.compile(value)
        for key in ("lessons_head", "lessons_none", "lessons_none_claude", "lessons_word", "lessons_but", "lessons_no",
                    "topic_label", "recommended", "no_data"):
            re.compile(langdata.alt("words", key))

    def test_the_keyboard_layouts_are_whole_and_one_letter_per_key(self):
        # the page's list search reads a query typed in the wrong layout through them (GET /api/keyboards, names.js)
        got = langdata.keyboards()
        self.assertEqual(set(got), {lang for lang in langdata.LANGS if "keyboard" in langdata.load(lang)})
        self.assertIn("ru", got)
        for lang, keys in got.items():
            with self.subTest(lang=lang):
                self.assertEqual(keys, langdata.get(lang, "keyboard", "keys"), "a pair the page would not get")
                self.assertEqual(set(keys), set("qwertyuiop[]asdfghjkl;'zxcvbnm,.`"))

    def test_a_broken_keyboard_layout_is_left_out(self):
        two = {"q": "\u0439", "w": "\u0439"}   # one letter on two keys: which key was meant is unknown
        data = {"ru": {"keyboard": {"keys": two}}, "uk": {"keyboard": {"keys": {"q": "qq", "w": "w", "e": "\u0443"}}},
                **{lang: {} for lang in langdata.LANGS if lang not in ("ru", "uk")}}
        with mock.patch.dict(langdata._cache, data, clear=True):
            self.assertEqual(langdata.keyboards(), {"uk": {"e": "\u0443"}})

    def test_the_files_are_utf8_without_a_bom(self):
        for lang in langdata.LANGS:
            raw = (ROOT / "i18n" / "data" / f"{lang}.json").read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), lang)
            json.loads(raw.decode("utf-8"))

    def test_earlier_forms_do_not_change(self):
        old = langdata.load(langdata.OLD)["legacy"]
        for key, digest in FROZEN.items():
            with self.subTest(key=key):
                self.assertEqual(_hash(old[key]), digest)
        self.assertEqual(sorted(FROZEN), sorted(old), "a new earlier form: add its hash to FROZEN")

    def test_recognised_words_change_only_on_purpose(self):
        got = {f"{lang}.{s}.{k}": _hash(v) for lang in langdata.LANGS for s, part in langdata.load(lang).items()
               if not s.startswith("_") and s != "legacy" for k, v in part.items()}
        self.assertEqual(got, RECOGNISED, "a recognised word changed: its rule's tests, then the hash in RECOGNISED")


class Union(unittest.TestCase):
    def setUp(self):
        folds = mock.patch.dict(langdata._folds, {}, clear=True)   # the fold pairs are read once: per fake data
        folds.start()
        self.addCleanup(folds.stop)

    def fake(self, data):
        return mock.patch.dict(langdata._cache, data, clear=True)

    def test_values_are_copies(self):
        langdata.legacy("quiet_sys").append("x")
        langdata.get("ru", "words", "yes").append("x")
        self.assertNotIn("x", langdata.legacy("quiet_sys"))
        self.assertNotIn("x", langdata.get("ru", "words", "yes"))

    def test_fold_of_one_language(self):
        with self.fake({"ru": {"letters": {"fold": [["x", "y"]]}}, "uk": {"letters": {"fold": [["q", "w"]]}}}):
            self.assertEqual(langdata.fold("xq", "ru"), "yq")

    def test_words_join_languages_in_order_without_repeats(self):
        with self.fake({"ru": {"words": {"yes": ["a", "b"]}}, "uk": {"words": {"yes": ["b", "c"]}}}):
            self.assertEqual(langdata.words("words", "yes"), ["a", "b", "c"])
            self.assertEqual(langdata.alt("words", "yes"), "a|b|c")

    def test_a_string_value_is_one_item_and_no_value_is_nothing(self):
        with self.fake({"ru": {"words": {"x": "a|b"}}, "uk": {}}):
            self.assertEqual(langdata.words("words", "x"), ["a|b"])
            self.assertEqual(langdata.words("words", "missing"), [])
            self.assertEqual(langdata.words("missing", "x"), [])

    def test_an_alternation_of_nothing_matches_nothing(self):
        with self.fake({"ru": {}, "uk": {}}):
            rule = re.compile(langdata.alt("words", "missing"))
        for text in ("", "x", "any text at all"):
            self.assertIsNone(rule.search(text))

    def test_dicts_merge_the_earlier_language_first(self):
        with self.fake({"ru": {"translit": {"latin": {"a": "1", "b": "2"}}}, "uk": {"translit": {"latin": {"b": "9", "c": "3"}}}}):
            self.assertEqual(langdata.merged("translit", "latin"), {"a": "1", "b": "2", "c": "3"})

    def test_fold_applies_every_pair(self):
        with self.fake({"ru": {"letters": {"fold": [["x", "y"]]}}, "uk": {"letters": {"fold": [["q", "w"]]}}}):
            self.assertEqual(langdata.fold("xq-xq"), "yw-yw")

    def test_a_missing_file_or_earlier_form_fails_loudly(self):
        with self.fake({}), mock.patch.object(langdata, "ROOT", os.path.join(str(ROOT), "no-such-folder")):
            with self.assertRaises(OSError):
                langdata.load("ru")
        with self.fake({"ru": {"legacy": {}}}):
            with self.assertRaises(KeyError):
                langdata.legacy("origin")


if __name__ == "__main__":
    unittest.main()
