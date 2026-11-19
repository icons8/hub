"""Why the hub could not reach GitHub: the owner gets the real cause and a short thing to do (core/gitaccess.py), not
a raw git message, and the private modules notice no longer says "accept the invitation" for every cause (R1 findings)."""
import tests  # noqa: F401 — keep the machine's settings out
import json
import re
import unittest

from core import gitaccess, i18n, reducer
from modules.update import private
from tests.fakes.lang import legacy

# what the earlier core wrote when git hung or did not start (language data)
NO_ANSWER = legacy("git_no_answer").replace(r"\d+", "120")
NOT_STARTING = legacy("git_not_starting")
# what git prints (stderr, as the update check and the modules step see it), one per way the access can fail
MESSAGES = {
    "auth": [
        "git fetch origin: fatal: Authentication failed for 'https://github.com/acme/hub.git/'",
        "git fetch origin: fatal: could not read Username for 'https://github.com': No such device or address",
        "fatal: could not read Password for 'https://github.com': terminal prompts disabled",
        "remote: Invalid username or token. Password authentication is not supported for Git operations.",
        "fatal: unable to access 'https://github.com/acme/hub.git/': The requested URL returned error: 403",
    ],
    "ssh": [
        "git@github.com: Permission denied (publickey).\nfatal: Could not read from remote repository.",
        "git fetch origin: git@github.com: Permission denied (publickey).",
    ],
    "missing": [
        "fatal: repository 'https://github.com/acme/hub-private.git/' not found",
        "ERROR: Repository not found.\nfatal: Could not read from remote repository.",
        "fatal: unable to access 'https://github.com/acme/hub-private.git/': The requested URL returned error: 404",
    ],
    "network": [
        "fatal: unable to access 'https://github.com/acme/hub.git/': Could not resolve host: github.com",
        "ssh: Could not resolve hostname github.com: Temporary failure in name resolution",
        "fatal: unable to access 'https://github.com/acme/hub.git/': Failed to connect to github.com port 443: Connection timed out",
        f"git fetch origin: {NO_ANSWER}",
        "git fetch origin: no answer within 120 s",
        "ssh: connect to host github.com port 22: Operation timed out",
        "ssh: connect to host github.com port 22: No route to host",
        "fatal: unable to access 'https://github.com/acme/hub.git/': Could not resolve proxy: proxy.example.invalid",
    ],
}
# not a matter of access: no hint, the message stays as git worded it
OTHER = [
    "fatal: '/tmp/x/no-such.git' does not appear to be a git repository",
    f"{NOT_STARTING}: [Errno 2] No such file or directory: 'git'",
    "git does not start: [Errno 2] No such file or directory: 'git'",   # codestore.git now
    "fatal: refusing to merge unrelated histories",
    "", None,
]


def fold(*events):
    st = reducer.empty()
    for i, (kind, data) in enumerate(events, 1):
        st, _, why = reducer.reduce(st, {"seq": i, "ts": 100 + i, "type": kind, "entity": None,
                                         "data": {**data, "channel": "cli", "owner": True}})
        assert why is None, why
    return st


class Kinds(unittest.TestCase):
    def test_every_way_the_access_fails_has_its_kind(self):
        for want, texts in MESSAGES.items():
            for text in texts:
                with self.subTest(want=want, text=text):
                    self.assertEqual(gitaccess.kind(text), want)

    def test_other_trouble_has_no_kind_and_no_hint(self):
        for text in OTHER:
            with self.subTest(text=text):
                self.assertIsNone(gitaccess.kind(text))
                self.assertEqual(gitaccess.hint(text), "")

    def test_the_code_for_the_stats_report_is_short_and_has_no_text(self):
        """git that does not start / no sign-in / no network / otherwise no access — a code only (the report carries no texts)."""
        starts = (NOT_STARTING, "git does not start")
        cases = {"git": [f"{x}: [Errno 2] No such file or directory: 'git'" for x in starts],
                 "auth": [m for k in ("auth", "ssh") for m in MESSAGES[k]],
                 "network": MESSAGES["network"] + ["git: no answer within 120 s"],
                 "no-access": MESSAGES["missing"] + [t for t in OTHER if not str(t).startswith(starts)]}
        for want, texts in cases.items():
            for text in texts:
                with self.subTest(want=want, text=text):
                    self.assertEqual(gitaccess.code(text), want)

    def test_the_kind_ignores_case(self):
        self.assertEqual(gitaccess.kind("FATAL: AUTHENTICATION FAILED for 'x'"), "auth")

    def test_each_kind_has_a_short_hint_in_both_languages_and_the_two_differ(self):
        for kind, key in gitaccess.HINTS.items():
            ru, en = i18n.tr("ru", key), i18n.tr("en", key)
            with self.subTest(kind=kind):
                self.assertNotEqual(ru, key)
                self.assertNotEqual(en, key)
                self.assertNotEqual(ru, en)
                self.assertLess(len(ru), 220)
                self.assertLess(len(en), 220)
                self.assertRegex(ru, r"[\u0400-\u04ff]")   # Cyrillic letters
                self.assertNotRegex(en, r"[\u0400-\u04ff]")

    def test_hints_name_only_harmless_commands(self):
        # a command in a hint is something the owner will type: signing in and looking, never removing or forcing
        allowed = {"gh auth login", "gh auth setup-git", "gh auth status"}
        for key in gitaccess.HINTS.values():
            for lang in ("ru", "en"):
                text = i18n.tr(lang, key)
                self.assertLessEqual(set(re.findall(r"\bgh [a-z-]+(?: [a-z-]+)?", text)), allowed, (lang, key))
                self.assertNotRegex(text, r"--force|reset|\brm\b|sudo|chmod|git ")

    def test_the_hint_follows_the_process_language(self):
        text = "fatal: Authentication failed for 'https://github.com/acme/hub.git/'"
        try:
            i18n.use("en")
            self.assertIn("Sign in here", gitaccess.hint(text))
            i18n.use("ru")
            self.assertEqual(gitaccess.hint(text), i18n.tr("ru", gitaccess.HINTS["auth"]))
        finally:
            i18n.use("en")


class UpdateCheck(unittest.TestCase):
    """The update check failed at fetch: the core keeps the kind next to the git text, the page turns it into the hint."""

    def test_the_failed_check_carries_the_kind_for_every_way(self):
        for want, texts in MESSAGES.items():
            for text in texts:
                with self.subTest(want=want, text=text):
                    st = fold(("update.checked", {"result": "error", "error": text}))
                    self.assertEqual(st["update_check"]["hint"], want)
                    self.assertEqual(st["update_check"]["error"], text)   # git's own words stay

    def test_no_kind_for_other_errors_and_for_a_good_check(self):
        for text in OTHER:
            st = fold(("update.checked", {"result": "error", "error": text}))
            self.assertNotIn("hint", st["update_check"], text)
        st = fold(("update.checked", {"result": "current", "behind": 0}))
        self.assertNotIn("hint", st["update_check"])

    def test_a_newer_check_replaces_the_old_hint(self):
        st = fold(("update.checked", {"result": "error", "error": MESSAGES["auth"][0]}),
                  ("update.checked", {"result": "current", "behind": 0}))
        self.assertNotIn("hint", st["update_check"])


class PrivateModulesNotice(unittest.TestCase):
    PIN = {"repo": "acme/team-modules", "ref": "pm-v1.0.0"}

    def line(self, why, lang="en"):
        i18n.use(lang)
        self.addCleanup(i18n.use, "en")
        return private.notice_line("no-access", why, self.PIN)

    def test_each_cause_gets_its_own_advice(self):
        for want, texts in MESSAGES.items():
            for text in texts:
                for lang in ("ru", "en"):
                    with self.subTest(want=want, text=text, lang=lang):
                        line = self.line(text, lang)
                        self.assertIn(i18n.tr(lang, gitaccess.HINTS[want]), line)
                        self.assertIn("acme/team-modules", line)
                        # the advice of one cause is not the advice of another
                        for other in set(gitaccess.HINTS) - {want}:
                            self.assertNotIn(i18n.tr(lang, gitaccess.HINTS[other]), line)

    def test_the_invitation_is_named_only_when_the_repository_is_not_shown(self):
        for want, texts in MESSAGES.items():
            for text in texts:
                line = self.line(text)
                self.assertEqual("invitation" in line, want == "missing", (want, text, line))

    def test_an_unrecognised_cause_is_shown_as_git_worded_it(self):
        line = self.line("fatal: '/tmp/x/no-such.git' does not appear to be a git repository\nhint: more")
        self.assertIn("does not appear to be a git repository", line)
        self.assertNotIn("\n", line)
        self.assertNotIn("invitation", line)
        self.assertIn("Git said", line)
        self.assertIn(i18n.tr("ru", "web.updates.private_cause_other", why="fatal: no luck"),
                      self.line("fatal: no luck", "ru"))

    def test_a_long_text_is_cut_and_no_text_is_no_cause_line(self):
        self.assertLess(len(self.line("x " * 400)), 600)
        line = self.line("")
        self.assertIn("no access", line)
        self.assertNotIn("Git said", line)
        self.assertNotIn("  ", line)

    def test_other_reasons_keep_their_lines(self):
        self.assertIn("pm-v1.0.0", private.notice_line("no-ref", "no tag", self.PIN))
        self.assertIn("not installing", private.notice_line("tag-moved", "tag moved", self.PIN))
        self.assertEqual(private.notice_line("hello", "x: not ready", self.PIN), "module not installed: x: not ready")


class PrivateModulesStep(unittest.TestCase):
    """The step itself (a repository on disk, git's refusal replaced): the owner hears the cause, and hears again when it changes."""

    def setUp(self):
        from tests.test_private_modules import Fixture
        self.f = Fixture(self)
        self.addCleanup(i18n.use, "en")

    def refuse(self, text):
        return lambda url, ref: ("no-access", None, text)

    def notes(self):
        return [e["data"]["text"] for e in self.f.events("owner.notify")]

    def test_each_cause_reaches_the_owner_in_its_own_words(self):
        for want, texts in MESSAGES.items():
            f = self.f.__class__(self)
            got = f.ensure(access=self.refuse(texts[0])).run()
            self.assertFalse(got["access"]["ok"])
            (note,) = [e["data"]["text"] for e in f.events("owner.notify")]
            self.assertIn(i18n.t(gitaccess.HINTS[want]), note, want)
            self.assertEqual("invitation" in note, want == "missing", (want, note))

    def test_the_kind_refines_the_code_of_the_step_it_does_not_replace_it(self):
        # one failure, one reason code (the step result and the stats report): the kind of the git failure only words
        # the line and the page text for the code "no-access", so the notice, the fact and the report agree
        for want, texts in MESSAGES.items():
            f = self.f.__class__(self)
            got = f.ensure(access=self.refuse(texts[0])).run()
            self.assertEqual(got["code"], "no-access", want)
            self.assertEqual({m["code"] for m in got["modules"].values()}, {"no-access"}, want)
            evs = f.events()
            (fact,) = [e["data"] for e in evs if e["type"] == "update.modules"]
            st = fold(("update.modules", fact))["private_modules"]
            self.assertEqual(st["hint"], want)
            (note,) = [e["data"]["text"] for e in evs if e["type"] == "owner.notify"]
            self.assertIn(i18n.t(gitaccess.HINTS[want]), note)
            from modules.update import private
            report = private.report(f.state, f.cfg(), repo=f.core)
            self.assertEqual((report["code"], {m["code"] for m in report["modules"].values()}), ("no-access", {"no-access"}))
            self.assertNotIn("github.com", json.dumps(report))   # the report stays without git's words, only the closed codes

    def test_a_changed_cause_is_told_again_the_same_cause_is_not(self):
        # the first refusal is the network (the hub started offline), the next ones are a revoked token: the owner must
        # not stay with "the hub will try again by itself"
        self.f.ensure(access=self.refuse(MESSAGES["network"][0])).run()
        (first,) = self.notes()
        self.assertIn(i18n.t(gitaccess.HINTS["network"]), first)
        self.f.ensure(access=self.refuse(MESSAGES["network"][2])).run()
        self.assertEqual(self.notes(), [])                                   # another network message: nothing new
        self.f.ensure(access=self.refuse(MESSAGES["auth"][0])).run()
        (second,) = self.notes()
        self.assertIn("gh auth login", second)
        self.f.ensure(access=self.refuse(MESSAGES["auth"][1])).run()
        self.assertEqual(self.notes(), [])


class PrivateModulesFact(unittest.TestCase):
    """update.modules: without access the core keeps the kind of the failure (the page: 'About the hub')."""

    def fact(self, **kw):
        return fold(("update.modules", {"ok": False, "missing": ["stats"], "modules": {"stats": "missing"},
                                        "ref": "pm-v1.0.0", **kw}))["private_modules"]

    def test_no_access_carries_the_kind_for_every_way(self):
        for want, texts in MESSAGES.items():
            for text in texts:
                with self.subTest(want=want, text=text):
                    got = self.fact(access=False, why=text)
                    self.assertEqual(got["hint"], want)
                    self.assertEqual(got["why"], text)

    def test_other_trouble_and_access_that_works_carry_none(self):
        self.assertNotIn("hint", self.fact(access=False, why="fatal: not a git repository"))
        # there is access, a module failed to install, and its output mentions authentication: not an access matter
        self.assertNotIn("hint", self.fact(access=True, why="stats: Authentication failed for the module's own service"))
        self.assertNotIn("hint", fold(("update.modules", {"ok": True, "access": True, "why": "", "modules": {}}))
                         ["private_modules"])


if __name__ == "__main__":
    unittest.main()
