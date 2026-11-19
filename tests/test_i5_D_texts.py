"""The owner-facing texts of modules/update and core/packaged come from the catalog: every call site passes all the params
of its text (a missing one would show `{name}` to the owner), and the counted restart reason keeps one Russian form."""
import tests  # noqa: F401 - ignore the machine's settings
import unittest

from core import i18n
from modules.update import private


class UpdateTexts(unittest.TestCase):
    PARAMS = {
        "update.follower_why": dict(old="abc1234", new="def5678"),
        "update.label_release": dict(name="v1.2.3"),
        "update.restart_rollback": dict(bad="v1.2.3", previous="v1.2.2"),
        "update.applied_version": dict(version="v1.2.3", notes="• a"),
        "update.applied_plain": dict(notes="• a"),
        "update.needs_module": dict(label="x", list="• y"),
        "update.tag_moved": dict(name="v1.2.3"),
        "update.not_descendant": {},
        "private.title": {},
        "private.no_ref": dict(ref="pm-v1.0.0"),
        "private.exit_code": dict(code=1),
        "private.tag_moved": dict(ref="pm-v1.0.0", sha="abc", pinned="def"),
        "private.installed_moved": dict(name="m", installed="abc", pinned="def"),
        "private.module_status": dict(name="m", status="error"),
        "private.unread_hours": dict(name="m", hours=6),
        "private.hello_missing": dict(name="m", minutes=5),
        "private.line_no_ref": dict(repo="o/r", ref="pm-v1.0.0"),
        "private.line_tag_moved": dict(why="w"),
        "private.line_read": dict(why="w"),
        "private.line_other": dict(why="w"),
        "private.local_edits": dict(files="a.py", repo="o/r"),
        "private.pool_down": {},
        "private.who_hub": dict(state="ready", id="k"),
        "private.who_record": dict(id="k"),
        "private.unreadable": dict(who="w"),
        "private.no_data": dict(who="w", name="m"),
        "private.installing": dict(name="m"),
        "private.coming_up": dict(name="m"),
        "private.login_not_ready": {},
        "private.unavailable": dict(why="w"),
        "private.unread_label": {},
        "private.state_done": {},
        "private.state_pending": {},
        "private.state_installing": {},
        "private.state_failed": {},
        "private.state_unread": {},
        "private.lines_head": dict(ref="pm-v1.0.0", url="u", state="s"),
        "private.forget_bad_key": {},
        "private.forget_missing": dict(id="k"),
        "core.packaged.how": {},
    }

    def test_every_text_is_filled_in_both_languages(self):
        for key, params in self.PARAMS.items():
            for lang in ("ru", "en"):
                with self.subTest(key=key, lang=lang):
                    text = i18n.tr(lang, key, **params)
                    self.assertNotEqual(text, key)
                    self.assertNotRegex(text, r"\{\w+\}")

    def test_counted_restart_reason_has_one_russian_form(self):
        forms = {i18n.tr("ru", "update.restart_applied", count=n).replace(str(n), "N") for n in (1, 2, 5, 21)}
        self.assertEqual(len(forms), 1)
        self.assertNotIn("{", forms.pop())
        self.assertNotEqual(i18n.tr("en", "update.restart_applied", count=1), i18n.tr("en", "update.restart_applied", count=3))

    def test_provider_texts_come_from_the_catalog(self):
        for provider in private.PROVIDER_TEXT:
            with self.subTest(provider=provider):
                self.assertNotEqual(private.provider_text(provider), private.PROVIDER_TEXT[provider])
        self.assertEqual(private.provider_text("something-new"), "something-new")

    def test_the_module_level_text_is_read_on_use(self):
        from core import packaged
        self.assertEqual(packaged.HOW, i18n.t("core.packaged.how"))
        i18n.use("ru")   # the language changed after the import: the text follows it
        try:
            self.assertEqual(packaged.HOW, i18n.tr("ru", "core.packaged.how"))
        finally:
            i18n.use("en")


if __name__ == "__main__":
    unittest.main()
