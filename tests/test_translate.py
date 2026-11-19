"""tools/translate.py: the model's translations are checked by script before they are written (placeholders, plural
forms of the language, what must stay verbatim, the script of the letters), written into the catalog and stamped; a key
that fails is asked again with the reason, and the budget stops new batches. The model itself is replaced here."""
import tests  # noqa: F401 — keep the machine's settings out
import importlib.util
import json
import os
import tempfile
import unittest
from unittest import mock

import platforms
from tests.fakes import exe

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("translate_tool", os.path.join(ROOT, "tools", "translate.py"))
tr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tr)


class Check(unittest.TestCase):
    def test_a_good_translation_passes(self):
        self.assertIsNone(tr.check("de", "Hello, {name}: run hub restart", "Hallo, {name}: hub restart ausführen"))
        self.assertIsNone(tr.check("de", {"one": "{count} file", "other": "{count} files"},
                                   {"one": "{count} Datei", "other": "{count} Dateien"}))

    def test_placeholders_stay_as_they_are(self):
        self.assertIn("placeholders", tr.check("fr", "Hello, {name}", "Bonjour, {nom}"))
        self.assertIn("placeholders", tr.check("fr", "Hello, {name}", "Bonjour"))
        self.assertIn("braces", tr.check("fr", "a {{literal}} {x}", "un literal {x}"))

    def test_the_plural_forms_of_the_language(self):
        en = {"one": "{count} file", "other": "{count} files"}
        self.assertIn("plural forms", tr.check("ar", en, {"one": "{count} ملف", "other": "{count} ملفات"}))
        ar = {f: "{count} ملف" for f in ("zero", "one", "two", "few", "many", "other")}
        self.assertIsNone(tr.check("ar", en, ar))
        self.assertIsNone(tr.check("ja", en, {"other": "{count} 件のファイル"}))
        self.assertIn("plural forms", tr.check("ja", en, {"one": "1 件", "other": "{count} 件"}))
        self.assertIn("plural forms", tr.check("de", en, "{count} Dateien"))
        self.assertIn("text", tr.check("de", "Files", {"one": "Datei", "other": "Dateien"}))

    def test_commands_flags_paths_urls_and_code_stay_verbatim(self):
        en = "run hub session wake {name} --work normal, see ~/.config/hub-core/hub.json and https://example.org/x."
        good = "führe hub session wake {name} --work normal aus, siehe ~/.config/hub-core/hub.json und https://example.org/x."
        self.assertIsNone(tr.check("de", en, good))
        for bad in (good.replace("hub session", "Hub-Sitzung"), good.replace("--work", "—work"),
                    good.replace("hub.json", "hub-json"), good.replace("https://example.org/x", "example.org")):
            self.assertIn("keep verbatim", tr.check("de", en, bad))
        self.assertIn("`hub gist`", tr.check("es", "say it with `hub gist`", "dilo con `hub resumen`"))
        # prose is translated: "a hub restart", "the hub version" are words, not commands
        self.assertIsNone(tr.check("es", "waiting for a hub restart", "esperando el reinicio del hub"))
        self.assertIsNone(tr.check("es", "the new hub version", "la nueva versión del hub"))
        # a metavariable in a usage line may be translated, an HTML tag may not
        self.assertIsNone(tr.check("es", "hub post <plugin> <text>", "hub post <plugin> <texto>"))
        self.assertIn("<b>", tr.check("es", "<b>Bold</b>", "Negrita"))

    def test_letters_of_another_script(self):
        cyrillic = "".join(chr(c) for c in (0x418, 0x43C, 0x44F))
        self.assertIn("CYRILLIC", tr.check("de", "Name", cyrillic))
        self.assertIsNone(tr.check("ja", "Name", "名前"))
        self.assertIn("HANGUL", tr.check("ja", "Name", "이름"))
        # a direction mark or a zero-width space is invisible in the catalog and in a review; the page isolates values itself
        self.assertIn("invisible", tr.check("ar", "--here works {x}", chr(0x200F) + "--here يعمل {x}"))
        self.assertIsNone(tr.check("hi", "Name", "न" + chr(0x200D) + "ाम"))   # a joiner is part of the script


class Run(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="translate-")
        self.addCleanup(platforms.current.rmtree, self.root)
        self.write("en", {"a.hi": "Hello, {name}", "a.n": {"one": "{count} file", "other": "{count} files"},
                          "a.bad": "Bye", "glossary.session": "session"})
        self.write("ru", {})
        self.write("de", {})
        self.write("stage", {"translated": ["en", "ru"], "ready": ["en", "ru", "de"]})
        patch = mock.patch.object(tr, "CATALOGS", self.root)
        patch.start()
        self.addCleanup(patch.stop)
        self.calls = []

    def write(self, name, data):
        with open(os.path.join(self.root, f"{name}.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    def read(self, name):
        with open(os.path.join(self.root, f"{name}.json"), encoding="utf-8") as f:
            return json.load(f)

    def model(self, answers, usd=0.5):
        """A fake ask(): each call takes the next answer table {key: value}; the call is recorded."""
        def ask(system, items, schema, model, effort="low"):
            table = answers[min(len(self.calls), len(answers) - 1)]
            self.calls.append((system, items))
            out = []
            for x in items:
                value = table.get(x["k"])
                if value is not None:
                    out.append({"i": x["i"], ("forms" if isinstance(value, dict) else "text"): value})
            return {"items": out}, usd
        return mock.patch.object(tr, "ask", ask)

    def test_checked_translations_are_written_and_stamped_and_a_failing_key_asked_again(self):
        first = {"a.hi": "Hallo, {name}", "a.n": {"one": "{count} Datei", "other": "{count} Dateien"},
                 "a.bad": "Tschüss {extra}"}
        second = {"a.bad": "Tschüss"}
        with self.model([first, second]):
            left = tr.translate("de", ["a.hi", "a.n", "a.bad"], tr.Spend(), jobs=1, log=lambda *_: None)
        self.assertEqual(left, {})
        de = self.read("de")
        self.assertEqual(de, {"a.bad": "Tschüss", "a.hi": "Hallo, {name}",
                              "a.n": {"one": "{count} Datei", "other": "{count} Dateien"}})
        self.assertEqual(tr.tool.status("de", root=self.root), [("glossary.session", "session")])
        # the second call asked only the failed key, with the reason
        self.assertEqual([x["k"] for x in self.calls[1][1]], ["a.bad"])
        self.assertIn("placeholders", self.calls[1][1][0]["problem"])

    def test_a_key_failing_every_try_stays_missing_with_its_reason(self):
        with self.model([{"a.hi": "Hallo"}]):
            left = tr.translate("de", ["a.hi"], tr.Spend(), jobs=1, log=lambda *_: None)
        self.assertEqual(list(left), ["a.hi"])
        self.assertIn("placeholders", left["a.hi"])
        self.assertEqual(len(self.calls), tr.TRIES)
        self.assertEqual(self.read("de"), {})

    def test_the_budget_stops_new_batches(self):
        spend = tr.Spend(budget=0.5, ledger=os.path.join(self.root, "ledger", "spend.jsonl"))
        with self.model([{"a.hi": "Hallo, {name}", "a.bad": "Tschüss"}]), mock.patch.object(tr, "BATCH_KEYS", 1):
            left = tr.translate("de", ["a.hi", "a.bad"], spend, jobs=1, log=lambda *_: None)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(list(left), ["a.bad"])
        with open(os.path.join(self.root, "ledger", "spend.jsonl")) as f:
            self.assertEqual([json.loads(x)["usd"] for x in f], [0.5])

    def test_the_prompt_carries_the_forms_of_the_language_and_the_glossary(self):
        self.write("de", {"glossary.session": "Sitzung"})
        tr.tool.stamp("de", root=self.root)
        system = tr.prompt("ar")
        self.assertIn("few, many, one, other, two, zero", system)
        self.assertIn("session → (choose one and keep it)", system)
        self.assertIn("session → Sitzung", tr.prompt("de"))
        # choosing the terms themselves, the prompt does not show the earlier choice
        self.assertIn("session → (choose one and keep it)", tr.prompt("de", fresh=True))

    def reviewer(self, problems, usd=0.5):
        """A fake ask() for the reviewer: {key: problem}; the call is recorded."""
        def ask(system, items, schema, model, effort="low"):
            self.calls.append((system, items))
            return {"items": [{"k": x["k"], "problem": problems[x["k"]]} for x in items if x["k"] in problems]}, usd
        return mock.patch.object(tr, "ask", ask)

    def test_the_review_keeps_real_defects_and_drops_its_own_no_defect_remarks(self):
        self.write("de", {"a.hi": "Hallo, {name}", "a.bad": "Tschüss", "glossary.session": "Sitzung"})
        problems = {"a.hi": "OK button label is left in English", "a.bad": "Fine per glossary; no defect."}
        with self.reviewer(problems):
            found = tr.review("de", tr.Spend(), log=lambda *_: None)
        self.assertEqual(found, {"a.hi": "OK button label is left in English"})
        self.assertEqual(sorted(x["k"] for x in self.calls[0][1]), ["a.bad", "a.hi"])   # the terms are not reviewed
        with self.reviewer(problems):
            self.assertEqual(tr.review("de", tr.Spend(), keys=[], log=lambda *_: None), {})   # nothing asked, not all
        self.assertEqual(len(self.calls), 1)

    def test_the_review_budget_is_seen_between_batches(self):
        self.write("de", {"a.hi": "Hallo, {name}", "a.bad": "Tschüss"})
        with self.reviewer({}), mock.patch.object(tr, "REVIEW_KEYS", 1):
            tr.review("de", tr.Spend(budget=0.5), jobs=1, log=lambda *_: None)
        self.assertEqual(len(self.calls), 1)

    def test_a_sample_is_stable_and_half_short_page_texts(self):
        en = {f"web.b{i}": "Go" for i in range(10)} | {f"core.t{i}": "A longer text of the core" * 2 for i in range(10)}
        self.write("en", en)
        self.write("de", {k: "x" for k in en})
        got = tr.sample("de", 6)
        self.assertEqual(got, tr.sample("de", 6))
        self.assertEqual(len(got), 6)
        self.assertGreaterEqual(sum(k.startswith("web.") for k in got), 3)

    def test_no_live_model_in_tests(self):
        with mock.patch.dict(os.environ, {"HUB_NO_MODEL": "1"}):
            with self.assertRaises(RuntimeError):
                tr.ask("x", [], {}, tr.MODEL)


class ClaudeEnv(unittest.TestCase):
    """In a hub session realclaude.find() bypasses the wrapper that puts the gateway stub in, so ask() puts it itself."""
    FAKE = ('import json, os, sys\nsys.stdin.read()\n'
            'print(json.dumps({"structured_output": {"token": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")}}))\n')

    def run_ask(self, env):
        with tempfile.TemporaryDirectory() as d:
            fake = exe.script(os.path.join(d, "claude"), self.FAKE)
            from core import realclaude
            with mock.patch.object(realclaude, "find", lambda: fake), \
                    mock.patch.dict(os.environ, env, clear=False):
                for k in ("HUB_NO_MODEL", "CLAUDE_CODE_OAUTH_TOKEN", "HUB_GATEWAY_STUB"):
                    if k not in env:
                        os.environ.pop(k, None)
                return tr.ask("x", [], {}, tr.MODEL)[0]["token"]

    def test_the_gateway_stub_becomes_the_token_of_the_called_claude(self):
        self.assertEqual(self.run_ask({"HUB_GATEWAY_STUB": "stub-value"}), "stub-value")

    def test_an_own_token_is_kept(self):
        self.assertEqual(self.run_ask({"HUB_GATEWAY_STUB": "stub-value", "CLAUDE_CODE_OAUTH_TOKEN": "mine"}), "mine")

    def test_without_a_stub_nothing_is_added(self):
        self.assertIsNone(self.run_ask({}))


if __name__ == "__main__":
    unittest.main()
