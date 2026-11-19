"""The owner-facing texts of core/version.py, plugrights.py, modlaunch.py, modclaims.py go through the catalog: every
call site passes every parameter of its text (a missing one would print `{name}` to the owner), in both languages."""
import tests  # noqa: F401 — do not see the machine's settings
import unittest

from core import i18n, modclaims, modlaunch, plugrights, version


def whole(case, text):
    case.assertIsInstance(text, str)
    case.assertTrue(text and "{" not in text and "}" not in text and not text.startswith("core."), text)


class Texts(unittest.TestCase):
    def check(self, make):
        for lang in ("ru", "en"):
            i18n.use(lang)
            try:
                make(lambda text: whole(self, text))
            finally:
                i18n.use("ru")

    def test_version_texts(self):
        def make(ok):
            for want in (None, "x", "^0.1", "^9.0", "^1.99"):
                ok(version.compat(want)[1] or "-")
            ok(version.deprecated(["hub market"])[0])
            ok(version.module_api("???")[1])
            ok(version.module_api("^1.0")[1])
            ok(version.module_api("^2.99")[1])
        self.check(make)

    def test_launch_refusals(self):
        self.check(lambda ok: [ok(modlaunch.refusal(why, ["a", "b"])) for why in ("second", "key")])

    def test_labels_are_looked_up_at_use_and_keep_the_mapping_shape(self):
        self.assertEqual(set(plugrights.LABELS), set(plugrights.KINDS))
        self.check(lambda ok: [ok(plugrights.LABELS[k]) for k in plugrights.KINDS])
        self.assertEqual(plugrights.LABELS.get("nope", "nope"), "nope")
        self.assertEqual({**plugrights.LABELS}["page"], plugrights.LABELS["page"])

    def test_claims_texts(self):
        def found(name, **man):
            return {"name": name, "manifest": {"http": {"paths": ["/api/files", "/api/send"]},
                                               "snapshot": {"keys": ["todo", "voice"], "features": ["push"]}, **man},
                    "state": "ok"}
        plan = [found("a"), found("b")]

        def make(ok):
            for use in ({}, {"paths": {"/api/files": "a"}, "keys": {"todo": "b"}}):
                for lines in modclaims.resolve(plan, {"modules": {"use": use}})[1].values():
                    for line in lines:
                        ok(line)
        self.check(make)


if __name__ == "__main__":
    unittest.main()
