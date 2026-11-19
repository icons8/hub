"""Slice I5, group C: the owner-facing texts of core/modules.py, core/modrun.py, core/corewrite.py come from the catalog —
the text kept in the state follows the state's language, every call site passes all the placeholders of its text."""
import tests  # noqa: F401 — do not see the machine's settings
import re
import unittest

from core import corewrite, i18n, modrun, reducer


class State(unittest.TestCase):
    def fail(self, lang, kind):
        st = reducer.empty()
        if lang:
            st["ui_lang_default"] = lang
        st, actions, _ = reducer.reduce(st, {"seq": 1, "ts": 10, "type": "module.error", "entity": None,
                                             "data": {"module": "m", "hook": "hello", "kind": kind, "error": "boom"}})
        return st["modules"]["m"], [a for a in actions if a["do"] == "owner_notice"]

    def test_the_reason_and_the_notice_follow_the_language_of_the_state(self):
        for kind, key in (("start", "core.modrun.reason_start"), ("hello", "core.modrun.reason_hello"),
                          ("migrate", "core.modrun.reason_migrate")):
            for lang in ("ru", "en"):
                rec, notes = self.fail(lang, kind)
                self.assertEqual(rec["reason"], i18n.tr(lang, key, error="boom"))
                self.assertEqual(notes[0]["title"], i18n.tr(lang, "core.modrun.notice_title", name="m"))
                self.assertEqual(notes[0]["text"], i18n.tr(lang, "core.modrun.notice_blocked", name="m",
                                                           reason=rec["reason"]))
        self.assertNotEqual(self.fail("ru", "hello")[0]["reason"], self.fail("en", "hello")[0]["reason"])
        self.assertEqual(self.fail(None, "hello")[0]["reason"], i18n.tr("en", "core.modrun.reason_hello", error="boom"))

    def test_the_pause_notice_counts_the_failures(self):
        rec = {"state": "suspended", "suspensions": 1, "last": {"hook": "doctor", "error": "x"}}
        for lang in ("ru", "en"):
            text = modrun._notice("m", {}, rec, lang)
            self.assertNotRegex(text, r"\{\w+\}")
            self.assertIn(f"{modrun.FAILS}", text)
            self.assertIn(f"{modrun.WINDOW // 60}", text)
            self.assertIn(f"{modrun.PAUSE // 60}", text)
        self.assertIn("3 failures", modrun._notice("m", {}, rec, "en"))


class Texts(unittest.TestCase):
    PARAMS = {
        "core.corewrite.needs_consent": {"cap": "core.write"},
        "core.corewrite.fold_taken": {"kind": "a.*", "chosen": "m"},
        "core.corewrite.fold_clash": {"kind": "a.*", "a": "m1", "b": "m2"},
        "core.corewrite.warn_wrote": {"name": "m", "when": "01.01 10:00", "count": 1},
        "core.corewrite.warn_wrote_off": {"name": "m", "when": "01.01 10:00", "count": 5},
        "core.corewrite.warn_crashes": {"count": 2},
        "core.corewrite.warn_fold": {},
        "core.corewrite.touched_head": {"name": "m", "days": 7, "count": 3, "entities": 2},
        "core.corewrite.seen_gone": {"box": "sessions", "entity": "a", "seq": 4},
        "core.corewrite.seen_line": {"box": "sessions", "entity": "a", "seq": 4, "state": "idle"},
        "core.corewrite.seen_held": {"box": "messages", "entity": "m1", "seq": 4, "state": "held"},
        "core.corewrite.seen_busy": {"box": "sessions", "entity": "a", "seq": 4, "state": "busy"},
        "core.modules.json_unreadable": {"error": "e"},
        "core.modules.no_folder": {"path": "/p"},
        "core.modules.folder_unreadable": {"error": "e"},
        "core.modules.copy_of_builtin": {"name": "m", "dir": "/d"},
        "core.modules.same_name": {"name": "m", "dirs": "/a, /b"},
        "core.modules.not_enabled": {},
        "core.modules.off_by_default": {},
        "core.modules.needs_catalog": {},
        "core.modules.plugin_missing": {"plugins": "p"},
        "core.modules.cap_given_to": {"cap": "voice", "chosen": "m"},
        "core.modules.cap_pick": {"cap": "voice", "names": "a, b"},
        "core.modules.dep_missing": {"dep": "d", "range": ">=1"},
        "core.modules.dep_off": {"dep": "d", "range": ">=1"},
        "core.modules.dep_version": {"dep": "d", "range": ">=1", "found": "0.1"},
        "core.modules.dep_blocked": {"dep": "d"},
        "core.modules.cycle": {"loop": "a → b → a"},
        "core.modules.stopped_by_failures": {"hook": "h", "error": "e", "name": "m"},
        "core.modules.paused_until": {"time": "10:00", "hook": "h", "error": "e"},
        "core.modules.dev_note": {},
        "core.modrun.notice_title": {"name": "m"},
        "core.modrun.notice_error": {"name": "m", "hook": "h", "error": "e"},
        "core.modrun.notice_suspended": {"name": "m", "count": 3, "window": 5, "hook": "h", "error": "e", "pause": 10},
        "core.modrun.notice_blocked": {"name": "m", "reason": "r"},
        "core.modrun.reason_start": {"error": "e"},
        "core.modrun.reason_hello": {"error": "e"},
        "core.modrun.reason_migrate": {"error": "e"},
        "core.modrun.stopped_default": {"name": "m"},
    }

    def test_every_text_is_whole_in_both_languages(self):
        for key, params in self.PARAMS.items():
            for lang in ("en", "ru"):
                text = i18n.tr(lang, key, **params)
                self.assertNotEqual(text, key, (lang, key))
                self.assertNotRegex(text, r"\{\w+\}", (lang, key))
                for name, value in params.items():
                    if name != "count":
                        self.assertIn(str(value), text, (lang, key, name))

    def test_the_count_forms_of_the_warnings(self):
        self.assertIn("hub modules touched m", i18n.tr("ru", "core.corewrite.warn_wrote", name="m", when="x", count=1))
        self.assertNotEqual(i18n.tr("ru", "core.corewrite.warn_wrote", name="m", when="x", count=1),
                            i18n.tr("en", "core.corewrite.warn_wrote", name="m", when="x", count=1))
        self.assertIn("hub modules touched m", i18n.tr("ru", "core.corewrite.warn_wrote_off", name="m", when="x", count=2))
        self.assertIn("1 record)", i18n.tr("en", "core.corewrite.warn_wrote", name="m", when="x", count=1))
        self.assertIn("2 records)", i18n.tr("en", "core.corewrite.warn_wrote", name="m", when="x", count=2))

    def test_warnings_speak_the_process_language(self):
        st = {"core_written": {"crashes": 1, "modules": {"m": {"since": 1, "count": 1}}}}
        out = corewrite.warnings(st, {})
        self.assertEqual(len(out), 3)
        self.assertFalse([w for w in out if re.search(r"\{\w+\}", w)])
        self.assertEqual(out[1], i18n.t("core.corewrite.warn_crashes", count=1))


if __name__ == "__main__":
    unittest.main()
