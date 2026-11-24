"""Interface languages (languages spec, slice C1): catalogs i18n/<language>.json, t() in Python and on the page, choice
of a person's language by the chain (hub.json → owner's language → hub before languages — Russian → browser → English),
pseudo-language xx for finding untranslated text, checks of catalog completeness and freshness, a ratchet on Russian
literals in texts for people."""
import tests  # noqa: F401 — keep the machine's settings out
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import platforms
from core import i18n, reducer
from tests import test_web
from tests.fakes import lang

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ru_catalog():
    """The Russian catalog as it lies on disk: what tr("ru", …) must give back (the source holds no Cyrillic)."""
    with open(os.path.join(ROOT, "i18n", "ru.json"), encoding="utf-8") as f:
        return json.load(f)


# Russian words of the fixture catalogs, from the language data: their meaning does not matter, their script does
YES, HUB = lang.word("words", "yes", 1).capitalize(), lang.legacy("hub_word").capitalize()
NAME, VERB = lang.data("self_name", "name"), lang.word("watch", "promise_verbs", 8)
LESSON, (LESSONS, OF_LESSONS) = lang.data("words", "lessons_word"), lang.words("words", "lessons_head")   # a noun's forms
# the words the state kept for the owner's language before the codes (core/forms.LANG_CODES)
OLD_WORD = {code: word for word, code in lang.legacy("lang_codes").items()}
_spec = importlib.util.spec_from_file_location("i18n_tool", os.path.join(ROOT, "tools", "i18n.py"))
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)

TOP = ("en", "ru", "zh-CN", "es", "hi", "pt-BR", "fr", "de", "ja", "ko", "ar", "tr", "uk")


def write(root, lang, cat):
    with open(os.path.join(root, f"{lang}.json"), "w", encoding="utf-8") as f:
        json.dump(cat, f, ensure_ascii=False)


class Catalogs(unittest.TestCase):
    def test_thirteen_catalogs_ru_and_en_required_all_ready(self):
        self.assertEqual(i18n.LANGS, TOP)
        self.assertEqual(sorted(i18n.available()), sorted(TOP))
        stage = json.load(open(os.path.join(i18n.ROOT, "stage.json"), encoding="utf-8"))
        self.assertEqual(sorted(stage["translated"]), ["en", "ru"])
        # every language is offered; Arabic writes right to left: the page mirrors (dir="rtl" from the server)
        self.assertEqual(sorted(i18n.ready()), sorted(TOP))
        self.assertEqual(i18n.known("ar"), "ar")
        self.assertNotEqual(i18n.tr("ar", "web.settings.tab.general"), i18n.tr("en", "web.settings.tab.general"))
        self.assertEqual([i18n.direction(x) for x in ("ar", "en", "ru")], ["rtl", "ltr", "ltr"])
        self.assertEqual(i18n.page_bundle("ar")["dir"], "rtl")
        self.assertIn('<html lang="ar" dir="rtl">', i18n.render_html('<html lang="ru"><body></body></html>', "ar"))

    def test_sync_file_has_only_language_codes_at_the_top(self):
        # `tools/i18n.py stamp --help` once wrote a stray "--help" entry
        with open(os.path.join(ROOT, "i18n", "sync.json")) as f:
            tops = list(json.load(f))
        self.assertTrue(all(re.fullmatch(r"[a-z]{2,3}(-[A-Za-z]+)?", k) for k in tops), tops)

    def test_catalogs_are_whole_and_fresh(self):
        # ru and en are complete and fresh; the others have no extra or stale keys, with the same substitutions and forms
        self.assertEqual(tool.problems(), [])

    def test_every_key_of_every_catalog_formats_with_its_placeholders_and_every_count(self):
        # the tests draw most strings in English: a form missing for a count, a substitution left in the text or an empty
        # text in another language would reach its people unseen (test_ru_owner does the same for Russian)
        en = i18n.catalog("en")
        bad = []
        for lang in TOP:
            need, _ = i18n.forms(lang)
            with open(os.path.join(i18n.ROOT, f"{lang}.json"), encoding="utf-8") as f:
                cat = json.load(f)
            for key, value in cat.items():
                names = i18n.placeholders(en.get(key, value))
                for n in (0, 1, 2, 5, 11, 21, 101, 1.5):
                    params = {name: f"<{name}>" for name in names}
                    params["count"] = n
                    clash = {k: params.pop(k) for k in ("lang", "key") if k in params}   # filled by the caller's format
                    got = i18n.format(i18n.tr(lang, key, **params), clash)
                    if got == key or any("{" + name + "}" in got for name in names) or not got.strip():
                        bad.append((lang, key, n, got))
                if isinstance(value, dict) and not need <= set(value):
                    bad.append((lang, key, "forms", sorted(need - set(value))))
        self.assertEqual(bad, [])

    def test_a_missing_key_a_wrong_placeholder_and_a_stale_translation_are_caught(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        cats = {"en": {"a.hello": "Hello, {name}", "a.files": {"one": "{count} file", "other": "{count} files"},
                       "a.bye": "Bye", "a.ok": "OK"},
                "ru": {"a.hello": HUB + ", {name}", "a.files": {"one": "{count} " + LESSON, "few": "{count} " + LESSONS,
                                                                "many": "{count} " + OF_LESSONS, "other": "{count} " + LESSONS},
                       "a.bye": NAME, "a.ok": YES},
                "de": {"a.hello": "Hallo, {name}", "a.bye": "Tschüss"}}
        for lang, cat in cats.items():
            write(root, lang, cat)
        tool.stamp("ru", root=root)
        tool.stamp("de", root=root)
        write(root, "stage", {"translated": ["en", "ru"], "ready": ["en", "ru", "de"]})
        self.assertEqual(tool.problems(root, langs=("en", "ru", "de")), [])
        # English changed after the translation: Russian is stale until it is retranslated and marked again
        cats["en"]["a.bye"] = "Goodbye"
        cats["ru"]["a.hello"] = HUB + ", {who}"
        cats["ru"]["a.files"] = {"one": "{count} " + LESSON, "other": "{count} " + LESSONS}
        del cats["ru"]["a.ok"]
        cats["ru"]["a.extra"] = NAME
        for lang, cat in cats.items():
            write(root, lang, cat)
        got = "\n".join(tool.problems(root, langs=("en", "ru", "de")))
        for want in ("ru: a.ok: missing", "ru: a.extra: not in English", "ru: a.hello: placeholders",
                     "ru: a.files: plural forms", "ru: a.bye: stale",
                     # an optional language may lack keys, but may not have stale ones (tools/i18n.py drop)
                     "de: a.bye: stale"):
            self.assertIn(want, got)
        self.assertNotIn("de: a.ok", got)
        self.assertEqual(tool.status("ru", root=root), [("a.bye", "Goodbye"), ("a.ok", "OK")])
        # stale keys of an optional language are dropped: it shows English until they are translated
        tool.drop_stale(root=root, langs=("de",))
        self.assertNotIn("a.bye", json.load(open(os.path.join(root, "de.json"), encoding="utf-8")))
        self.assertNotIn("de:", "\n".join(tool.problems(root, langs=("en", "ru", "de"))))

    def test_a_release_needs_every_ready_language_whole(self):
        # between releases a ready language may lack a phrase (it shows English); a release may not: check --release
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        write(root, "en", {"a.hello": "Hello", "a.bye": "Bye"})
        write(root, "ru", {"a.hello": HUB, "a.bye": NAME})
        write(root, "de", {"a.hello": "Hallo"})
        write(root, "fr", {})
        for lang in ("ru", "de"):
            tool.stamp(lang, root=root)
        write(root, "stage", {"translated": ["en", "ru"], "ready": ["en", "ru", "de"]})
        langs = ("en", "ru", "de", "fr")
        self.assertEqual(tool.problems(root, langs=langs), [])
        got = tool.problems(root, langs=langs, release=True)
        self.assertEqual([x.split(" — ")[0] for x in got], ["de: a.bye: missing"])   # fr is not offered: not needed
        self.assertIn("tools/translate.py de", got[0])

    def test_letters_of_another_script_are_caught(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        cats = {"en": {"a.pid": "Chrome is running (pid {pid})", "a.name": "Name"},
                "hi": {"a.pid": "Chrome चल रहा है (معرّف العملية {pid})", "a.name": "नाम"},
                "ru": {"a.pid": f"Chrome {VERB} (pid {{pid}})", "a.name": NAME}}
        for lang, cat in cats.items():
            write(root, lang, cat)
            tool.stamp(lang, root=root)
        write(root, "stage", {"translated": ["en", "ru"], "ready": ["en", "ru", "hi"]})
        self.assertEqual(tool.problems(root, langs=tuple(cats)), ["hi: a.pid: letters of another script (ARABIC)"])

    def test_every_key_the_code_asks_for_exists_in_english_and_russian(self):
        en, ru = i18n.catalog("en"), i18n.catalog("ru")
        used = tool.used_keys()
        self.assertGreater(len(used), 500, "almost no t() calls — the search is broken")
        self.assertEqual(sorted(k for k in used if k not in en), [])
        self.assertEqual(sorted(k for k in used if k not in ru), [])

    def test_page_keys_are_all_used(self):
        # page keys (web.*, files.*) are only those the code asks for: dead ones do not pile up in the catalogs of all languages
        used = tool.used_keys() | tool.DYNAMIC
        dead = sorted(k for k in i18n.catalog("en") if k.startswith(i18n.PAGE) and k not in used)
        self.assertEqual(dead, [])

    # short page texts that keep a closing full stop, with the reason: the text is read by a session or another
    # channel, or the full stop is a part of what is joined to it
    STOP_KEPT = {
        "web.srv.no_speech": "the Telegram bot says it too",
        "web.boards.run_text_one": "text sent to a session",
        "web.boards.run_text_many": "text sent to a session",
        "web.dock.par_short": "an abbreviation, not a sentence",
        "web.voicehelp.busy": "the hub command prints it too",
        "web.voicehelp.no_ffmpeg": "the hub command raises it too",
        "web.main.feed_stale": "a button follows it in the same line",
        "web.secrets.unlocked": "a link follows it in the same line",
        "web.modules.third_core_written": "the second sentence of one paragraph",
    }

    def test_a_short_page_text_does_not_end_with_a_full_stop(self):
        # labels, hints, statuses and short messages of the page (one or two short sentences, up to 140 characters) go
        # without the closing full stop; long texts, push and Telegram texts and the texts for sessions keep theirs
        stops = sorted(
            k for k, v in i18n.catalog("en").items()
            if k.startswith(i18n.PAGE) and isinstance(v, str) and len(v) <= 140 and v.endswith(".") and not v.endswith("..")
            and k not in self.STOP_KEPT and not k.startswith("web.push.") and not k.endswith("_prompt"))
        self.assertEqual(stops, [])

    def test_human_text_files_do_not_gain_russian_literals(self):
        # texts for a person go through t(): Russian strings in page code, Telegram or hub output only decrease
        # (i18n/pending.json); comments and docstrings do not count
        self.assertEqual(tool.ratchet(), [])

    def test_a_bundled_plugin_manifest_is_the_english_of_its_catalog_text(self):
        # the page shows the catalog text (web.<name>.manifest_*) for a bundled plugin; the manifest keeps the same English
        # for a reader of the repository and must not drift from it
        import glob
        en = i18n.catalog("en")
        mans = glob.glob(os.path.join(ROOT, "modules", "web", "bundled", "*", "hub-plugin.json"))
        self.assertTrue(mans)
        for path in mans:
            with open(path, encoding="utf-8") as f:
                man = json.load(f)
            for field in ("description", "why"):
                key = f"web.{man['name']}.manifest_{field}"
                if key in en:
                    self.assertEqual(man[field], en[key], f"{path}: {field} differs from {key}")

    def test_the_literal_scanner_skips_comments_and_sees_templates(self):
        w = HUB   # a Russian word: the scanner looks for the letters, not for what they say
        js = (f'// {w} "{w}"\nconst a = "{w}"; /* {w}\n «{w}» */ const b = `x ${{y ? "{w}" : "no"}} z`;\n'
              f'const r = /{w}/; const u = "http://x"; // {w}\nconst c = `${{a}}`; const d = \'ok\';\n')
        self.assertEqual(tool.js_literal_lines(js), {2, 3, 4})
        py = (f'"""{w}"""\nA = "{w}"  # {w}\n# {w} {w}\ndef f():\n    """{w} {w}"""\n'
              f'    return f"x {{1}} {w}"\n')
        self.assertEqual(tool.py_literal_lines(py), {2, 6})


class Translate(unittest.TestCase):
    def test_params_fallback_and_raw_key(self):
        self.assertEqual(i18n.tr("ru", "web.settings.tab.general"), ru_catalog()["web.settings.tab.general"])
        self.assertNotEqual(i18n.tr("ru", "web.settings.tab.general"), i18n.tr("en", "web.settings.tab.general"))
        self.assertEqual(i18n.tr("en", "web.settings.tab.general"), "Settings")
        self.assertEqual(i18n.tr("qq", "web.settings.tab.general"), "Settings")          # unknown language — English
        self.assertEqual(i18n.tr("ru", "no.such.key"), "no.such.key")             # the key itself: tests can see it
        self.assertEqual(i18n.format("{{a}} {b} {c}", {"b": 1}), "{a} 1 {c}")      # without a value — visible

    def test_a_broken_catalog_falls_back_to_english(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        shutil.copy(os.path.join(i18n.ROOT, "en.json"), root)
        shutil.copy(os.path.join(i18n.ROOT, "stage.json"), root)
        for lang, body in (("de", "{not json"), ("fr", "[1, 2]"), ("es", '{"web.settings.tab.general": 7}')):
            with open(os.path.join(root, lang + ".json"), "w", encoding="latin-1") as f:
                f.write(body)
        with mock.patch.object(i18n, "ROOT", root), mock.patch.dict(i18n._cache, clear=True):
            for lang in ("de", "fr", "es"):
                self.assertEqual(i18n.tr(lang, "web.settings.tab.general"), "Settings", lang)

    def test_a_running_process_keeps_a_phrase_whose_key_left_its_catalog(self):
        # 12.11: an update moved the pool's phrases from the core's catalog into an embed's; hubd, still on the previous
        # code until its restart a minute later, reread the core's catalog without them and told the owner
        # "conduit: core.accounts.limit_pct". A process keeps every phrase it has read (a new text of it is taken)
        root = tempfile.mkdtemp()
        self.addCleanup(platforms.current.rmtree, root)
        write(root, "en", {"a.kept": "kept", "a.moved": "limit {pct}%"})
        write(root, "stage", {"translated": ["en"], "ready": ["en"]})
        path = os.path.join(root, "en.json")
        now = [1000.0]
        with mock.patch.object(i18n, "ROOT", root), mock.patch.dict(i18n._cache, clear=True), \
                mock.patch.object(i18n.time, "monotonic", lambda: now[0]):
            self.assertEqual(i18n.tr("en", "a.moved", pct=97), "limit 97%")
            write(root, "en", {"a.kept": "kept, new text"})   # the update: the key is in another file now
            os.utime(path, ns=(10 ** 18, 10 ** 18))   # a new mtime even where the clock is coarse
            now[0] += i18n.FRESH
            self.assertEqual(i18n.tr("en", "a.kept"), "kept, new text")
            self.assertEqual(i18n.tr("en", "a.moved", pct=97), "limit 97%")
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"a.kept": "half wri')   # a file caught in the middle of the update's write
            os.utime(path, ns=(2 * 10 ** 18, 2 * 10 ** 18))
            now[0] += i18n.FRESH
            self.assertEqual(i18n.tr("en", "a.kept"), "kept, new text")

    def test_a_run_of_texts_resolves_the_language_and_merges_the_catalogs_once(self):
        # a snapshot of a hub with a long schedule asks for some 8 thousand texts in a row: every one used to resolve the
        # language again (stage.json, the folder listing) and check every catalog file — half of the snapshot's build.
        # Within FRESH the answer is the same; after it a changed catalog is read, as before
        root = tempfile.mkdtemp()
        self.addCleanup(platforms.current.rmtree, root)
        write(root, "en", {"a.k": "one"})
        write(root, "ru", {"a.k": "first"})
        write(root, "stage", {"translated": ["en", "ru"], "ready": ["en", "ru"]})
        now = [1000.0]
        with mock.patch.object(i18n, "ROOT", root), mock.patch.dict(i18n._cache, clear=True), \
                mock.patch.object(i18n.time, "monotonic", lambda: now[0]), \
                mock.patch.object(i18n, "ready", wraps=i18n.ready) as ready, \
                mock.patch.object(i18n, "_embedded", wraps=i18n._embedded) as embedded:
            self.assertEqual({i18n.tr("ru", "a.k") for _ in range(100)}, {"first"})
            self.assertEqual((ready.call_count, embedded.call_count), (1, 1))
            write(root, "ru", {"a.k": "second"})
            os.utime(os.path.join(root, "ru.json"), ns=(1, 1))   # a new mtime even where the clock is coarse
            now[0] += i18n.FRESH / 2
            self.assertEqual(i18n.tr("ru", "a.k"), "first")
            now[0] += i18n.FRESH
            self.assertEqual(i18n.tr("ru", "a.k"), "second")
            self.assertEqual(i18n.tr("qq", "a.k"), "one")   # an unknown language — English, as before

    def test_the_strings_of_a_text_are_never_older_than_the_stamp_of_its_page(self):
        # "Files" tags its page with stamp() and renders the page with tr(): a tag newer than the text it was sent with
        # keeps the old text in the browser (304) until the next catalog change. The language cache of tr() filled half a
        # FRESH after the catalog read must not outlive that read
        root = tempfile.mkdtemp()
        self.addCleanup(platforms.current.rmtree, root)
        write(root, "en", {"a.k": "one"})
        write(root, "ru", {"a.k": "first"})
        write(root, "stage", {"translated": ["en", "ru"], "ready": ["en", "ru"]})
        now = [1000.0]
        with mock.patch.object(i18n, "ROOT", root), mock.patch.dict(i18n._cache, clear=True), \
                mock.patch.object(i18n.time, "monotonic", lambda: now[0]):
            before = i18n.stamp("ru")
            now[0] += i18n.FRESH / 2
            self.assertEqual(i18n.tr("ru", "a.k"), "first")
            write(root, "ru", {"a.k": "second"})
            os.utime(os.path.join(root, "ru.json"), ns=(2 * 10 ** 18, 2 * 10 ** 18))   # newer than English, any clock
            now[0] += i18n.FRESH * 3 / 4
            tag, text = i18n.stamp("ru"), i18n.tr("ru", "a.k")
            self.assertEqual((tag == before, text), (False, "second"))

    def test_plural_forms(self):
        self.assertEqual([i18n.plural("ru", n) for n in (1, 2, 5, 11, 21, 22, 112)],
                         ["one", "few", "many", "many", "one", "few", "many"])
        self.assertEqual([i18n.plural("uk", n) for n in (1, 2, 5, 0)], ["one", "few", "many", "many"])
        self.assertEqual([i18n.plural("ar", n) for n in (0, 1, 2, 3, 11, 100)],
                         ["zero", "one", "two", "few", "many", "other"])
        self.assertEqual([i18n.plural("fr", n) for n in (0, 1, 2)], ["one", "one", "other"])
        self.assertEqual({i18n.plural(x, n) for x in ("ja", "ko", "zh-CN") for n in (0, 1, 2, 5)}, {"other"})
        self.assertEqual([i18n.plural("en", n) for n in (0, 1, 2)], ["other", "one", "other"])
        self.assertEqual(i18n.plural("ru", 1.5), "other")
        root = tempfile.mkdtemp()
        self.addCleanup(platforms.current.rmtree, root)
        write(root, "en", {"a.n": {"one": "{count} session", "other": "{count} sessions"}})
        write(root, "ru", {"a.n": {"one": "{count} " + LESSON, "few": "{count} " + LESSONS, "many": "{count} " + OF_LESSONS,
                                   "other": "{count} " + LESSONS}})
        write(root, "stage", {"translated": ["en", "ru"], "ready": ["en", "ru"]})
        with mock.patch.object(i18n, "ROOT", root), mock.patch.dict(i18n._cache, clear=True):
            self.assertEqual([i18n.tr("ru", "a.n", count=n) for n in (1, 3, 5)],
                             [f"1 {LESSON}", f"3 {LESSONS}", f"5 {OF_LESSONS}"])
            self.assertEqual([i18n.tr("en", "a.n", count=n) for n in (1, 3)], ["1 session", "3 sessions"])

    def test_language_from_the_browser(self):
        self.assertEqual(i18n.negotiate("ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"), "ru")
        self.assertEqual(i18n.negotiate("de-CH;q=0.5, ja;q=0.9"), "ja")
        self.assertEqual(i18n.negotiate("zh"), "zh-CN")
        self.assertEqual(i18n.negotiate("pt-PT"), "pt-BR")
        self.assertEqual(i18n.negotiate("uk-UA,uk;q=0.9,en;q=0.8"), "uk")
        self.assertEqual(i18n.negotiate("ar-EG,ar;q=0.9,fr;q=0.5"), "ar")
        self.assertEqual(i18n.negotiate("he-IL,fr;q=0.5"), "fr")   # a language without a catalog — the next one
        self.assertEqual(i18n.negotiate("xx-YY, *;q=0.1"), None)
        self.assertEqual(i18n.negotiate(""), None)
        self.assertEqual(i18n.negotiate("ru;q=0, de"), "de")

    def test_pseudo_language_wraps_every_english_text(self):
        # test language: every English string is wrapped in ⟦…⟧; visible text outside the brackets is a missed t()
        self.assertEqual(i18n.tr("xx", "web.settings.tab.general"), "⟦Settings⟧")
        self.assertEqual(i18n.known("xx"), "xx")
        self.assertNotIn("xx", i18n.ready())
        self.assertIsNone(i18n.negotiate("xx"))                     # the browser does not choose it — only hub.json
        bundle = i18n.page_bundle("xx")
        self.assertTrue(all(v.startswith("⟦") and v.endswith("⟧") for v in bundle["messages"].values()
                            if isinstance(v, str)))
        plural = next(v for v in bundle["messages"].values() if isinstance(v, dict))
        self.assertTrue(all(x.startswith("⟦") for x in plural.values()))


def old_hub(**owner):
    """State of a hub whose history predates the languages (the core set a mark at startup)."""
    st = reducer.empty()
    st["sessions"]["hub"] = {"name": "hub", "state": "idle"}
    st, _, _ = reducer.reduce(st, {"seq": 5, "ts": 1, "type": "core.started", "data": {"i18n": 1}})
    st["owner"].update(owner)
    return st


def new_hub(**owner):
    st, _, _ = reducer.reduce(reducer.empty(), {"seq": 1, "ts": 1, "type": "core.started", "data": {"i18n": 1}})
    st["owner"].update(owner)
    return st


class NoPin(unittest.TestCase):
    """Without the language pinned for tests (HUB_LANG=en in tests/__init__.py): the chain works as in a real hub."""

    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("HUB_LANG", None)


class Chain(NoPin):
    """Spec §4.2: hub.json language → owner's language (not Latin script, ready) → hub before languages — ru → browser
    (page only) → en. A language that is not ready, from any source, gives en."""

    def test_old_and_new_hub_marks(self):
        self.assertEqual(old_hub()["ui_lang_default"], "ru")
        self.assertEqual(new_hub()["ui_lang_default"], "")
        # hub journal from before the switch, folded from scratch: its launches without an i18n mark are also a hub before languages
        st, _, _ = reducer.reduce(reducer.empty(), {"seq": 1, "ts": 1, "type": "core.started", "data": {}})
        self.assertEqual(st["ui_lang_default"], "ru")
        # the mark is set once
        st, _, _ = reducer.reduce(new_hub(), {"seq": 9, "ts": 2, "type": "core.started", "data": {"i18n": 1}})
        self.assertEqual(st["ui_lang_default"], "")

    def test_rows_of_the_table(self):
        H = "en-US,en;q=0.9"
        rows = [
            # (settings, state, browser) → page, everything else (Telegram, hub to the owner, CLAUDE.md template)
            ({}, old_hub(), H, "ru", "ru"),                                       # old snapshot without a language
            ({}, old_hub(lang="latin", langs=["latin"] * 5), H, "ru", "ru"),     # Latin script is not a language
            ({}, old_hub(lang=OLD_WORD["en"], langs=[OLD_WORD["en"]]), H, "ru", "ru"),   # the former "English" = Latin script
            ({}, old_hub(lang="en"), H, "ru", "ru"),
            ({}, old_hub(lang="ru"), H, "ru", "ru"),
            ({}, old_hub(lang="uk"), "ru", "uk", "uk"),
            ({}, new_hub(), H, "en", "en"),
            ({}, new_hub(), "de-DE,de", "de", "en"),                              # the browser applies to the page only
            ({}, new_hub(lang="latin"), "fr", "fr", "en"),
            ({}, new_hub(lang="ru"), H, "ru", "ru"),
            ({}, new_hub(lang="ja"), H, "ja", "ja"),
            ({"language": "en"}, old_hub(lang="ru"), "ru", "en", "en"),           # manual choice
            ({"language": "pt-br"}, old_hub(), "ru", "pt-BR", "pt-BR"),
            ({"language": "ar"}, old_hub(), "ru", "ar", "ar"),
            ({}, new_hub(lang="ar"), "ru", "ar", "ar"),
            ({"language": "klingon"}, old_hub(), "ru", "en", "en"),
            ({"language": ""}, old_hub(), H, "ru", "ru"),                          # empty — same as unset
            ({"language": "xx"}, old_hub(), H, "xx", "xx"),                       # pseudo-language — from settings only
            ({}, None, None, "en", "en"),
        ]
        for cfg, st, header, page, other in rows:
            with self.subTest(cfg=cfg, owner=(st or {}).get("owner", {}).get("lang"), header=header):
                self.assertEqual(i18n.human(cfg, st, header, page=True), page)
                self.assertEqual(i18n.human(cfg, st), other)


    def test_unread_state_file_is_not_cached(self):
        # state.json is being replaced (journal.save_state: temp file + replace) — the read failed; the first answer is English,
        # but the language is not pinned: the next call reads again and takes the owner's language
        state = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, state, True)
        cfg = os.path.join(state, "hub.json")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({}, f)
        os.environ.update({"HUB_STATE_DIR": state, "HUB_CONFIG": cfg})
        before = i18n._current
        self.addCleanup(setattr, i18n, "_current", before)
        self.addCleanup(setattr, i18n, "_retry_at", None)
        i18n._current = None
        path = os.path.join(state, "state.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"seq": 1, "state": {"own')
        self.assertEqual(i18n.current(), "en")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"seq": 1, "state": new_hub(lang="ru")}, f)
        # again — not sooner than RETRY: t() calls current() for every string
        self.assertEqual(i18n.current(), "en")
        with mock.patch.object(i18n.time, "monotonic", return_value=time.monotonic() + i18n.RETRY + 1):
            self.assertEqual(i18n.current(), "ru")
        self.assertEqual(i18n._current, "ru")
        self.assertIsNone(i18n._retry_at)
        # no state file — the language is simply not set: the answer is cached as before
        os.remove(path)
        i18n._current = None
        self.assertEqual(i18n.current(), "en")
        self.assertEqual(i18n._current, "en")
        self.assertIsNone(i18n._retry_at)
        # hub.json is not JSON — not transient: cached; unreadable (OSError, mid-replace) — again after RETRY
        with open(cfg, "w", encoding="utf-8") as f:
            f.write("{")
        i18n._current = None
        self.assertEqual(i18n.current(), "en")
        self.assertIsNone(i18n._retry_at)
        from core import catalog
        i18n._current = None
        with mock.patch.object(catalog, "load_config", side_effect=PermissionError("busy")):
            self.assertEqual(i18n.current(), "en")
        self.assertIsNotNone(i18n._retry_at)


class SessionReadsEnglish(unittest.TestCase):
    """A process with HUB_SESSION is a session's step: it reads English whatever the owner's language (the
    hub's output in a session's steps); without it the owner's terminal gets the catalog of his language."""

    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        before = i18n._current
        self.addCleanup(setattr, i18n, "_current", before)

    def lang(self, **env):
        os.environ.pop("HUB_SESSION", None)
        os.environ.update(env)
        i18n._current = None
        return i18n.current()

    def test_session_gets_english_owner_gets_his_language(self):
        self.assertEqual(self.lang(HUB_LANG="ru"), "ru")
        self.assertEqual(self.lang(HUB_LANG="ru", HUB_SESSION="maps"), "en")
        self.assertEqual(self.lang(HUB_LANG="xx", HUB_SESSION="maps"), "en")
        self.assertEqual(self.lang(HUB_LANG="xx"), "xx")
        self.assertEqual(i18n.t("cli.send.empty", usage="u"), i18n.tr("xx", "cli.send.empty", usage="u"))
        self.lang(HUB_LANG="ru", HUB_SESSION="maps")
        self.assertEqual(i18n.t("cli.send.empty", usage="u"), i18n.tr("en", "cli.send.empty", usage="u"))
        # what is kept for the owner or shown to him (a fact, a caption on a picture) is in his language even when a
        # session runs the command
        self.addCleanup(setattr, i18n, "_owner", i18n._owner)
        i18n._owner = None
        self.assertEqual(i18n.owner(), "ru")
        self.assertEqual(self.lang(HUB_LANG="uk"), "uk")
        self.assertEqual(i18n.owner(), "uk")
        # a process that chose its language itself (the page server, the bot) keeps it
        i18n.use("ru")
        self.assertEqual(i18n.current(), "ru")


class Detector(NoPin):
    def test_scripts(self):
        # Cyrillic text from the language data: the alphabets, the letters only Russian has and those of South Slavic
        ru, uk, own = lang.LETTERS["ru"], lang.LETTERS["uk"], lang.data("letters", "own")
        shared = "".join(c for c in ru if c not in own)                                  # no Russian-only letter
        hard, = (c for c in own if c not in lang.data("letters", "own_not_south"))        # the hard sign
        rows = [(" ".join(lang.words("watch", "doubt")), "ru"),
                (uk, "uk"),
                ("Please make the release today", "latin"),
                ("Bitte mach heute das Release fertig", "latin"),
                ("今日リリースをお願いします、よろしくね", "ja"),
                ("请今天发布这个版本好吗谢谢", "zh-CN"),
                ("오늘 릴리스를 부탁합니다 감사합니다", "ko"),
                ("कृपया आज रिलीज़ कर दीजिए धन्यवाद", "hi"),
                ("من فضلك أصدر النسخة اليوم شكرا", "ar"),
                (ru[:11], None),                                                # too short to tell
                (f"{ru} — https://gitlab.example.com/group/project/-/merge_requests/12345", "ru"),   # the link is not speech
                (f"{shared[:5]}{hard}{shared[5:]}", "ru"),                      # hard sign before a vowel, no other Russian-only letter — still Russian
                (f"{shared} {lang.data('letters', 'south')}", None),            # Serbian — not Russian
                (f"{shared} {hard}{ru[1]}", None),                              # Bulgarian (the hard sign before a consonant) — same
                (f"{shared} {hard} ", "ru")]                                    # the hard sign letter itself — not Bulgarian
        for text, want in rows:
            with self.subTest(text=text):
                self.assertEqual(reducer.owner_lang(text), want)

    def test_voting_reads_old_words_and_latin_does_not_switch_a_russian_hub(self):
        st = old_hub(lang=OLD_WORD["ru"], langs=[OLD_WORD["ru"], OLD_WORD["ru"], OLD_WORD["en"]])
        reducer._note_lang(st, "Please check the release notes for today")
        self.assertEqual(st["owner"]["langs"], ["ru", "ru", "latin", "latin"])
        self.assertEqual(st["owner"]["lang"], "latin")   # a tie — the last one
        self.assertEqual(i18n.human({}, st, "en", page=True), "ru")
        reducer._note_lang(st, lang.LETTERS["ru"])   # now in Russian
        self.assertEqual(st["owner"]["lang"], "ru")
        # Latin script goes into the session rule as "English", as before languages (prompts change in slice C3 with evals)
        st["owner"]["lang"] = "latin"
        self.assertEqual(reducer.known_lang(st), "en")


class Page(test_web.WebTest):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("HUB_LANG", None)

    def config(self, language):
        cfg = os.path.join(self.state, "hub.json")
        with open(cfg, "w") as f:
            json.dump({"language": language} if language is not None else {}, f)
        env = mock.patch.dict(os.environ, {"HUB_CONFIG": cfg})
        env.start()
        self.addCleanup(env.stop)

    def html(self, accept=None):
        code, body = self.req("GET", "/", headers={"Accept-Language": accept} if accept else None)
        self.assertEqual(code, 200)
        return body.decode()

    def test_the_page_speaks_the_settings_language_or_the_browsers(self):
        self.config(None)
        self.assertIn('<html lang="ru" dir="ltr">', self.html("ru-RU,ru;q=0.9"))
        self.assertIn('<html lang="en" dir="ltr">', self.html())
        self.assertIn('<html lang="ar" dir="rtl">', self.html("ar-EG"))   # Arabic writes right to left
        self.config("de")
        self.assertIn('<html lang="de" dir="ltr">', self.html("ru"))

    def test_static_text_of_the_page_is_rendered_in_its_language(self):
        self.config(None)
        ru, en = self.html("ru"), self.html("en")
        self.assertIn(f">{ru_catalog()['web.hub.default_title']}</title>", ru.replace("<title>", ">"))
        self.assertNotRegex(re.sub(r"<!--.*?-->", "", en, flags=re.S), "[\u0410-\u044f\u0401\u0451]")
        # the page tag depends on the language: the browser will not take the Russian copy from cache for the English one
        tags = {self.etag(h) for h in ("ru", "en")}
        self.assertEqual(len(tags), 2)

    def etag(self, accept):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/", headers={"Host": f"127.0.0.1:{self.port}", "Accept-Language": accept})
        r = c.getresponse()
        r.read()
        c.close()
        return r.getheader("ETag")

    def test_the_page_gets_its_strings_and_only_the_missing_english(self):
        self.config(None)
        code, body = self.req("GET", "/i18n.json", headers={"Accept-Language": "ru"})
        self.assertEqual(code, 200)
        got = json.loads(body)
        self.assertEqual((got["lang"], got["dir"]), ("ru", "ltr"))
        self.assertEqual(got["messages"]["web.settings.tab.general"], ru_catalog()["web.settings.tab.general"])
        self.assertEqual(got["fallback"], {})                         # Russian is complete — English is not needed
        self.assertFalse([k for k in got["messages"] if not k.startswith(i18n.PAGE)])
        got = json.loads(self.req("GET", "/i18n.json", headers={"Accept-Language": "de"})[1])
        self.assertEqual(got["lang"], "de")
        self.assertTrue(set(got["fallback"]).isdisjoint(got["messages"]))
        self.assertEqual(set(got["fallback"]) | set(got["messages"]),
                         {k for k in i18n.catalog("en") if k.startswith(i18n.PAGE)})

    def test_the_page_gets_the_keyboard_layouts_of_the_language_data(self):
        # the list search reads a query typed in the wrong layout through them (static/js/names.js): the layout is language
        # data, not page code, so the page asks the server for it
        from core import langdata
        code, body = self.req("GET", "/api/keyboards")
        self.assertEqual(code, 200)
        got = json.loads(body)
        self.assertEqual(got, {"layouts": langdata.keyboards()})
        self.assertEqual(len(got["layouts"]["ru"]), 33)

    def test_the_language_does_not_wait_for_a_journal_reread(self):
        # a language change re-reads the feeds under the journal lock (tens of seconds on a large journal): the page, its
        # strings and text answers choose the language without this lock (a review finding)
        self.config("de")
        with self.web.tail.reading:
            self.assertIn('lang="de"', self.html("ru"))
            code, body = self.req("GET", "/i18n.json")
            self.assertEqual((code, json.loads(body)["lang"]), (200, "de"))

    def test_strings_of_another_language_on_request(self):
        # "Flow" in the app (iOS/Mac) labels the canvas in the app's language (hubFlow.setLang): ?lang= gives the strings of
        # that ready language; an unknown code gives the page language, as without it
        self.config("ru")
        got = json.loads(self.req("GET", "/i18n.json?lang=en-US")[1])
        self.assertEqual((got["lang"], got["messages"]["web.flow.hub"]), ("en", "Hub"))
        self.assertEqual(json.loads(self.req("GET", "/i18n.json?lang=de")[1])["lang"], "de")
        for odd in ("zz", "", "xx", "../ru"):
            self.assertEqual(json.loads(self.req("GET", f"/i18n.json?lang={odd}")[1])["lang"], "ru", odd)

    def test_an_old_hub_without_settings_stays_russian_in_an_english_browser(self):
        self.config(None)
        self.j.append("core.started", data={})   # journal of an earlier core: a launch without an i18n mark
        self.settle()
        self.assertIn('<html lang="ru"', self.html("en-US,en"))


@unittest.skipIf(os.name == "nt", "the package builder is sh")
class Package(unittest.TestCase):
    def test_the_deb_carries_the_catalogs(self):
        # the package carries the seed: the whole repo cloned on a release tag — it has what git has, no folder is named
        with open(os.path.join(ROOT, "tools", "package", "deb", "build.sh"), encoding="utf-8") as f:
            self.assertNotIn("archive HEAD", f.read())
        tracked = subprocess.run(["git", "-C", ROOT, "ls-files", "i18n"], capture_output=True, text=True).stdout
        for lang in ("en", "ru", "stage", "sync"):
            self.assertIn(f"i18n/{lang}.json", tracked)


def load_tests(loader, standard, pattern):
    """Page inherits WebTest for the server; WebTest's own tests live in test_web."""
    suite = unittest.TestSuite()
    for case in (Catalogs, Translate, Chain, SessionReadsEnglish, Detector, Package):
        suite.addTests(loader.loadTestsFromTestCase(case))
    suite.addTests(Page(n) for n in Page.__dict__ if n.startswith("test"))
    return suite


if __name__ == "__main__":
    unittest.main()
