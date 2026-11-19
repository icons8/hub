"""The open flavor of the app (app spec §3.2, §3.3, slice A2b): the factory cuts the built-in login of accounts (with its
gateway and conduit), the reply voice and the stats report out of the tree, and the core works without them — sessions on
this machine's Claude Code login, a reply stays text, no report; a module with the capability takes the function over at
once (there is no built-in to compare it with). Every place asks core/cutout.present; here the cut is patched in (or named
in HUB_TEST_CUT for the hub command in its own process), and the factory's own run of the whole suite on the cut tree is
tested on a small repo. Nothing here imports the cut code: the file runs on the cut tree too."""
import tests  # noqa: F401 — hide the machine's settings
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from core import appcode, cutout, embed, journal, modrun, profiles, providers, restart
from core.daemon import Hub
from tests.fakes import cut as fakecut

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(REPO, "bin", "hub")
T0 = 1000000.0
LOGIN_POINTS = ["profile.list", "profile.decide", "profile.token", "profile.use"]
ALL = ("profile", "gateway", "conduit", "speech", "stats", "voice_cloud")


def cut(*names):
    """cutout.present with names (default: every function) cut out of the tree."""
    names = names or ALL
    return mock.patch.object(cutout, "present", lambda name, repo=None: name not in names)


class Fake:
    """Modules without processes: answer(point, question) -> the raw answer (None — silent), checked by modrun.POINTS."""

    def __init__(self, answer):
        self.answer, self.up, self.state, self.calls = answer, set(), {}, []

    def call(self, name, point, payload, ctx=None):
        self.calls.append((name, point))
        if name not in self.up or self.status(name) != "ok":
            return None
        raw = self.answer(point, payload)
        if raw is None:
            return None
        try:
            return modrun.POINTS[point]["check"](raw, {"name": name})
        except modrun.BadResult:
            return None

    def ready(self, name, wanted=None):
        return {"pid": 1, "launch": "L1"} if name in self.up and self.status(name) == "ok" else None

    def status(self, name):
        return self.state.get(name, "ok")


def login_answers(point, q):
    """A login module with one profile of its own token, ready to lead."""
    return {"profile.list": {"on": True, "switch_at": 0.95, "profiles": [{"name": "work"}],
                             "ready": {"tokens": "ok", "conduit": "off", "gateway": "off"}},
            "profile.decide": {},
            "profile.token": {"env": {"CLAUDE_CODE_OAUTH_TOKEN": "tok-work-0123456789"}},
            "profile.use": {"ok": True}}.get(point)


class Base(unittest.TestCase):
    def setUp(self):
        self.cfg = tempfile.mkdtemp(prefix="open-cfg-")
        env = {"HUB_CONFIG_DIR": self.cfg, "HUB_CONFIG": os.path.join(self.cfg, "hub.json"),
               "HUB_STATE_DIR": tempfile.mkdtemp(prefix="open-state-")}
        with open(env["HUB_CONFIG"], "w") as f:
            json.dump({"update": {"enabled": False}}, f)
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        self.wanted = {}
        p = mock.patch.object(modrun, "wants", lambda state_dir: self.wanted)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(providers, "local_edits", lambda repo, cfg: {})
        p.start()
        self.addCleanup(p.stop)
        p = cut()
        p.start()
        self.addCleanup(p.stop)

    def hub(self, answer=login_answers, config=None):
        h = Hub(tempfile.mkdtemp(prefix="open-"), types.SimpleNamespace(poll=lambda: []))
        h.execute = lambda a: None
        self.now = T0
        h.clock = lambda: self.now
        h.config = config or {}
        fake = Fake(answer)
        h.modules.mods = fake
        h.profiles = profiles.Profiles(h.state_dir, mods=fake, config=lambda: h.config, clock=lambda: h.clock(),
                                       cached=True, ttl=profiles.LIST_TTL, providers=lambda: h.state.get("providers"))
        h.providers = providers.Tick(h)
        h.profiles.shadow = lambda: h.providers.shadowing()
        h.profiles.observe = lambda *a: h.providers.observe(*a)
        self.h, self.fake = h, fake
        return h

    def up(self, name, caps, points):
        self.wanted[name] = {"capabilities": caps, "order": len(self.wanted), "version": "1.0.0"}
        self.fake.up.add(name)
        self.h.record("module.ready", name, {"module": name, "launch": "L1", "version": "1.0.0", "points": points,
                                             "channel": "core"})

    def tick(self, n=1):
        for _ in range(n):
            self.now += providers.EVERY
            with contextlib.redirect_stdout(io.StringIO()):
                self.h.providers_tick()
                self.h.switch_accounts()
                self.h.gateway_tick()

    def prov(self, cap):
        return (self.h.state.get("providers") or {}).get(cap) or {}

    def events(self, kind):
        return [e for e in journal.read_tail(os.path.join(self.h.state_dir, "journal.jsonl")) if e["type"] == kind]


class NobodyLeads(Base):
    """No module: nothing switches, sessions on this machine's login, no gateway, no "Accounts" tab."""

    def test_without_a_module_nothing_is_decided_and_nothing_is_imported(self):
        self.hub()
        with mock.patch.dict(sys.modules, {"modules.accounts": None, "modules.accounts.accounts": None,
                                           "modules.gateway": None, "modules.gateway.gateway": None}):
            self.tick(3)   # an import of the cut code would raise here
            self.assertIsNone(self.h.profiles.listing())
            self.assertIsNone(self.h.profiles.token("work"))
            self.assertFalse(self.h.profiles.gateway())
            self.assertEqual(self.h.profiles.gateway_summary(None, T0, {})[0], "off")
            self.assertEqual(self.h.profiles.gateway_summary(None, T0, {"gateway": {"enabled": True}})[0], "bad")
        self.assertEqual([e["type"] for e in journal.read_tail(os.path.join(self.h.state_dir, "journal.jsonl"))
                          if e["type"].startswith(("account.", "gateway.", "module.provider"))], [])
        self.assertEqual(self.h.state.get("providers") or {}, {})

    def test_a_spawn_does_not_wait_for_a_login_module_without_a_pin_and_reads_no_clock(self):
        self.hub()
        from modules.update import private
        clock = mock.Mock(return_value=T0)
        self.h.clock = clock
        asked = []   # wait_private swallows any exception of waiting(): a list, not self.fail
        with mock.patch.object(private, "load_pin", lambda repo=private.REPO: None), \
                mock.patch.object(private, "waiting", lambda *a, **k: asked.append(a)):
            self.assertFalse(self.h.wait_private({"do": "spawn", "session": "s1"}))
        self.assertEqual(asked, [])
        clock.assert_not_called()   # the journal of a hub without modules stays byte for byte
        with mock.patch.object(private, "load_pin", lambda repo=private.REPO: {"modules": {}}), \
                mock.patch.object(private, "waiting", lambda *a, **k: "the login module is coming up"):
            self.assertTrue(self.h.wait_private({"do": "spawn", "session": "s1"}))   # with a pin it waits, as before
        self.assertTrue(clock.called)

    def test_a_gateway_left_on_is_turned_off_when_nobody_leads_login(self):
        self.hub()
        self.h.record("gateway.changed", None, {"on": True, "ident": "x1", "channel": "core"})
        self.tick()
        got = self.events("gateway.changed")[-1]["data"]
        self.assertFalse(got["on"])
        self.assertFalse((self.h.state.get("gateway") or {}).get("on"))
        self.tick(2)
        self.assertEqual(len(self.events("gateway.changed")), 2)   # once, not every tick

    def test_hub_modules_names_nobody_not_the_built_in(self):
        lines = providers.lines({"profile": {"state": "builtin"}, "speech": {"state": "builtin"},
                                 "stats": {"state": "builtin"}})
        from core import i18n
        self.assertEqual(lines, [f"{i18n.t(providers.LABEL[c])}: {i18n.t(providers.NONE_LABEL[c])}"
                                 for c in providers.CAPS])
        with mock.patch.object(cutout, "present", lambda name, repo=None: True):
            self.assertIn(i18n.t("core.providers.state_builtin"), providers.lines({"profile": {"state": "builtin"}})[0])


class ModuleLeads(Base):
    """A ready module with the capability leads at once: no shadow and no warm-up (there is no built-in to compare)."""

    def test_a_login_module_leads_at_once_and_gives_the_sessions_its_login(self):
        self.hub()
        self.up("acc", ["profile"], LOGIN_POINTS)
        self.tick()
        self.assertEqual((self.prov("profile")["state"], self.prov("profile")["module"]), ("module", "acc"))
        self.assertTrue(self.events("module.provider")[-1]["data"]["bare"])
        self.assertEqual(self.h.profiles.listing()["profiles"][0]["name"], "work")
        self.assertEqual(self.h.profiles.token("work")["env"]["CLAUDE_CODE_OAUTH_TOKEN"], "tok-work-0123456789")

    def test_a_shadow_from_a_tree_with_the_built_in_hands_login_to_its_module(self):
        self.hub()
        self.up("acc", ["profile"], LOGIN_POINTS)
        self.h.record("module.provider", None, {"cap": "profile", "state": "shadow", "module": "acc", "why": "",
                                                "channel": "core"})
        self.tick()
        self.assertEqual(self.prov("profile")["state"], "module")

    def test_a_pause_after_failures_keeps_the_module_there_is_nobody_else(self):
        self.hub()
        self.up("acc", ["profile"], LOGIN_POINTS)
        self.tick()
        self.fake.state["acc"] = "suspended"
        self.tick(3)
        self.assertEqual(self.prov("profile")["state"], "module")
        self.wanted.pop("acc")   # removed — nobody leads it
        self.tick()
        self.assertEqual(self.prov("profile")["state"], "builtin")

    def test_voice_and_report_go_to_their_modules_and_failures_do_not_hold_them(self):
        self.hub(lambda p, q: [{"name": "x", "ok": True}] if p == "doctor" else None)
        self.up("vox", ["speech"], ["speech.say"])
        self.up("hstats", ["stats"], [])
        self.tick()
        self.assertEqual((self.prov("speech")["state"], self.prov("stats")["state"]), ("module", "module"))
        os.makedirs(os.path.join(self.h.state_dir, "stats"), exist_ok=True)
        with open(os.path.join(self.h.state_dir, "stats", "state.json"), "w") as f:
            json.dump({"fails": 5, "by": "hstats", "tried": self.now + 1}, f)
        for key in ("k1", "k2"):
            self.h.record("speech.said", None, {"module": "vox", "key": key, "ok": False, "why": "x"}, src="telegram")
        self.tick(2)
        self.assertEqual((self.prov("speech")["state"], self.prov("stats")["state"]), ("module", "module"))
        self.assertEqual([e["data"]["cap"] for e in self.events("module.provider") if e["data"]["state"] == "builtin"], [])
        self.assertNotIn("hold", self.prov("speech"))   # no flapping through "built-in" and back, no hold carried
        self.assertTrue(self.prov("speech")["bare"])

    def test_a_module_that_led_on_a_tree_with_the_built_in_is_marked_bare(self):
        self.hub(lambda p, q: None)
        self.up("vox", ["speech"], ["speech.say"])
        self.h.record("module.provider", None, {"cap": "speech", "state": "module", "module": "vox", "why": "",
                                                "channel": "core"})
        self.tick()
        self.assertTrue(self.prov("speech").get("bare"))
        for key in ("k1", "k2"):
            self.h.record("speech.said", None, {"module": "vox", "key": key, "ok": False, "why": "x"}, src="telegram")
        self.assertEqual(self.prov("speech")["state"], "module")

    @fakecut.needs("speech")   # the built-in voice comes back for real
    def test_the_mark_goes_when_the_built_in_code_is_back_and_a_named_module_gets_it(self):
        self.hub(lambda p, q: None, config={"modules": {"use": {"speech": "vox"}}})
        self.up("vox", ["speech"], ["speech.say"])
        self.h.record("module.provider", None, {"cap": "speech", "state": "module", "module": "vox", "why": "",
                                                "named": True, "channel": "core"})
        self.tick()
        self.assertTrue(self.prov("speech").get("bare"))   # named, on a tree without the built-in voice
        with mock.patch.object(cutout, "present", lambda name, repo=None: True):
            self.tick()
            self.assertNotIn("bare", self.prov("speech"))
            for key in ("k1", "k2"):
                self.h.record("speech.said", None, {"module": "vox", "key": key, "ok": False, "why": "x"},
                              src="telegram")
            self.assertEqual(self.prov("speech")["state"], "builtin")   # the built-in voice takes it back again

    def test_hub_modules_shows_no_hold_that_is_not_in_force(self):
        rec = {"speech": {"state": "module", "module": "vox", "hold": {"module": "vox", "version": "1", "kind": "fail"}}}
        self.assertNotIn("vox 1", providers.lines(rec)[0])
        with mock.patch.object(cutout, "present", lambda name, repo=None: True):
            self.assertIn("vox", providers.lines(rec)[0].split("—", 1)[-1])

    def test_the_fold_takes_the_bare_move_only_from_the_core(self):
        st = {"providers": {}}
        providers.apply(st, {"ts": 1}, {"cap": "profile", "state": "module", "module": "acc", "bare": True,
                                        "channel": "core"})
        self.assertEqual(st["providers"]["profile"]["state"], "module")
        from core.tools import Rejected
        with self.assertRaises(Rejected):   # without it a module comes to login through the shadow only
            providers.apply({"providers": {}}, {"ts": 1}, {"cap": "profile", "state": "module", "module": "acc",
                                                           "channel": "core"})


class Parts(unittest.TestCase):
    def test_the_parts_of_cut_code_are_not_started_or_restarted(self):
        with cut():
            self.assertEqual(cutout.parts(["web", "gateway", "conduit", "stats", "update"]), ["web", "update"])
        with mock.patch.object(cutout, "present", lambda name, repo=None: True):
            self.assertEqual(cutout.parts(["gateway", "stats"]), ["gateway", "stats"])
        self.assertEqual(set(restart.PARTS) >= {"gateway", "conduit", "stats"}, cutout.present("gateway"))

    def test_the_gateway_and_conduit_go_with_the_pool_and_there_is_one_check(self):
        from modules.update import private
        with mock.patch.dict(os.environ, {"HUB_TEST_CUT": "profile"}):
            self.assertEqual([cutout.present(n) for n in ("gateway", "conduit")], [False, False])
            # only the pool's companions go with it: the voice and the report stay as the tree has them
            self.assertEqual([cutout.present(n) for n in ("speech", "stats")],
                             [embed.where(cutout.FILES[n], REPO) is not None for n in ("speech", "stats")])
            self.assertFalse(private.builtin_present("profile"))
            self.assertEqual(cutout.parts(["gateway", "conduit", "web", "stats"]),
                             ["web"] + (["stats"] if cutout.present("stats") else []))

    def test_the_code_cutout_asks_about_lies_in_an_embed_the_open_flavor_cuts(self):
        import fnmatch
        for name, path in cutout.FILES.items():
            self.assertNotEqual(embed.where(path, REPO), os.path.join(REPO, *path.split("/")), name)   # not the core's
            got = embed.where(path, REPO)
            if got:
                rel = os.path.relpath(got, REPO).replace(os.sep, "/")
                self.assertTrue(any(fnmatch.fnmatchcase(rel, g) for g in appcode.OPEN_CUT), name)
        for part in cutout.PARTS:   # their programs are the core's hub-part (bin/hub-<part>: a shim of the move only)
            self.assertTrue(f"bin/hub-{part}" in embed.SHIMS or not os.path.exists(os.path.join(REPO, "bin", f"hub-{part}")))

    def test_hub_up_names_only_the_parts_in_the_tree(self):
        code = ("import runpy, sys; sys.argv = ['hub-up', '--help'];\n"
                "g = runpy.run_path(%r, run_name='hub_up_probe'); print(sorted(g['PARTS']))" % os.path.join(REPO, "bin", "hub-up"))
        env = {k: v for k, v in os.environ.items() if k != "HUB_UP_PARTS"}
        got = subprocess.run([sys.executable, "-c", code], env={**env, "HUB_TEST_CUT": "gateway,conduit,stats"},
                             capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        self.assertIn("'hubd'", got.stdout, got.stderr[-600:])
        for part in ("gateway", "conduit", "stats"):
            self.assertNotIn(f"'{part}'", got.stdout)


class Voice(unittest.TestCase):
    def test_without_the_built_in_voice_and_a_module_the_reply_stays_text(self):
        with cut():
            sys.modules.pop("modules.voice.speech", None)
            try:
                with mock.patch.dict(sys.modules, {"modules.voice.speak": None}):
                    from modules.voice import speech
                    s = speech.Speech(tempfile.mkdtemp(prefix="open-voice-"), records=lambda: {}, providers=lambda: {})
                    self.assertIsNone(s.reply("text", {"voice_reply": {"enabled": True}}))
                    self.assertTrue(speech.enabled({"voice_reply": {"enabled": True}}))
                    self.assertTrue(issubclass(speech.SpeakError, RuntimeError))
            finally:
                sys.modules.pop("modules.voice.speech", None)


class Page(unittest.TestCase):
    def test_settings_have_no_voice_reply_or_stats_cards(self):
        from modules.web import modinfo
        with cut(), mock.patch.dict(sys.modules, {"modules.voice.speak": None, "modules.stats": None,
                                                  "modules.stats.stats": None}):
            got = modinfo.modules(tempfile.mkdtemp(prefix="open-page-"), {}, {"sessions": {}, "schedule": []})
        ids = {m["id"]: m for m in got}
        self.assertNotIn("stats", ids)
        self.assertIn("voice", ids)
        self.assertTrue(ids["voice_reply"]["toggle"])   # the switch is a voice module's too: it stays, without details
        with cut(), mock.patch.dict(sys.modules, {"modules.voice.speak": None, "modules.stats.stats": None}):
            got = modinfo.modules(tempfile.mkdtemp(prefix="open-page-"), {"voice_reply": {"enabled": True}},
                                  {"sessions": {}, "schedule": []})
        card = next(m for m in got if m["id"] == "voice_reply")
        self.assertEqual(card["state"], "part")   # no Telegram yet, and no voice module leads
        from core import i18n
        self.assertIn(i18n.t("core.providers.none_speech"), card["detail"])


def run_hub(*args, cut="profile,gateway,conduit,stats,speech"):
    state = tempfile.mkdtemp(prefix="open-hub-")
    env = {**os.environ, "HUB_STATE_DIR": state, "HUB_CLI_WAIT": "0", "PYTHONUTF8": "1", "HUB_LANG": "en",
           "HUB_TEST_CUT": cut}
    env.pop("HUB_SESSION", None)
    return subprocess.run([sys.executable, HUB, *args], env=env, capture_output=True, text=True, encoding="utf-8",
                          stdin=subprocess.DEVNULL, timeout=60)


class Command(unittest.TestCase):
    def test_cut_commands_say_whose_they_are_and_leave_the_help(self):
        for cmd, module in (("accounts", "accounts"), ("conduit", "accounts"), ("stats", "stats")):
            r = run_hub(cmd)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn(f"hub {cmd} is a command of the {module} module", r.stdout + r.stderr)
        r = run_hub("--help")
        self.assertEqual(r.returncode, 0, r.stderr[-600:])
        for cmd in ("accounts", "conduit", "stats"):
            self.assertNotIn(f"\n  {cmd} ", r.stdout)
        if cutout.present("profile"):   # on a tree that carries it, the help is there
            self.assertIn("\n  accounts ", run_hub("--help", cut="").stdout)

    def test_accounts_use_stays_the_cores(self):
        r = run_hub("accounts", "use", "work")
        self.assertNotIn("is a command of the", r.stdout + r.stderr)


class Repo(unittest.TestCase):
    """A small repo and tools/package/layer.py loaded from this tree."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="open-factory-")
        self.addCleanup(self.tmp.cleanup)
        spec = importlib.util.spec_from_file_location("hub_test_layer_open", os.path.join(REPO, "tools", "package", "layer.py"))
        self.layer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.layer)
        self.work = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.work)

        def g(*a):
            subprocess.run(["git", *a], cwd=self.work, check=True, capture_output=True, stdin=subprocess.DEVNULL)
        self.g = g
        g("init", "-q", "-b", "main")
        g("config", "user.email", "x@example.test")
        g("config", "user.name", "x")

    def tree(self, verdict):
        files = {"core/x.py": "X = 1\n", "embed/acme/embed.json": '{"embed": 1, "api": "1.0"}\n',
                 "embed/acme/modules/stats/stats.py": "S = 1\n", "embed/acme/modules/private.json": "{}\n",
                 "embed/acme/tests/test_stats.py": "T = 1\n", "tests/test_x.py": "T = 2\n",
                 "tools/test.py": ("import os, sys\nprint(sorted(os.listdir('tests')))\nprint(os.path.isdir('.git'))\n"
                                   f"print({verdict!r})\nsys.exit(0 if {verdict!r} == 'OK' else 1)\n")}
        for rel, text in files.items():
            os.makedirs(os.path.dirname(os.path.join(self.work, rel)), exist_ok=True)
            with open(os.path.join(self.work, rel), "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        self.g("add", "-A")
        self.g("commit", "-qm", "c")

    def write(self, rel, text):
        os.makedirs(os.path.dirname(os.path.join(self.work, rel)), exist_ok=True)
        with open(os.path.join(self.work, rel), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)


class Factory(Repo):
    """The factory's full run on the cut tree (§3.2): tools/package/layer.py open_tree + check_open on a small repo."""

    def test_the_cut_tree_carries_its_tests_without_those_of_the_cut_code(self):
        self.tree("OK")
        out = os.path.join(self.tmp.name, "open")
        self.layer.open_tree(self.work, "HEAD", out)
        self.assertTrue(os.path.isfile(os.path.join(out, "core", "x.py")))
        self.assertTrue(os.path.isfile(os.path.join(out, "tests", "test_x.py")))
        self.assertFalse(os.path.exists(os.path.join(out, "embed")))
        for gone in ("embed/acme/modules/stats/stats.py", "embed/acme/modules/private.json", "embed/acme/tests/test_stats.py"):
            self.assertFalse(os.path.exists(os.path.join(out, *gone.split("/"))), gone)
        listed = subprocess.run(["git", "ls-files"], cwd=out, capture_output=True, text=True, check=True).stdout.split()
        self.assertIn("core/x.py", listed)   # a clone of one commit: tests reading git see the cut tree

    def test_the_phrases_of_an_embed_go_with_it_and_the_cores_stay_as_they_are(self):
        self.tree("OK")
        core = {"en": {"core.both": "both", "web.x.y": "page"}, "ru": {"core.both": "B", "web.x.y": "P"}}
        for lang, cat in core.items():
            self.write(f"i18n/{lang}.json", json.dumps(cat))
            self.write(f"embed/acme/i18n/{lang}.json", json.dumps({"stats.sent": "sent"}))
        self.g("add", "-A")
        self.g("commit", "-qm", "catalogs")
        out = os.path.join(self.tmp.name, "open")
        self.layer.open_tree(self.work, "HEAD", out)
        for lang in ("en", "ru"):
            with open(os.path.join(out, "i18n", f"{lang}.json"), encoding="utf-8") as f:
                self.assertEqual(json.load(f), core[lang], lang)
        sheet = self.layer.build(self.work, "HEAD", os.path.join(self.tmp.name, "layer"), sys.executable, "open",
                                 keep=os.path.join(self.tmp.name, "seed"))
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "seed", "embed")))
        self.assertEqual(sheet["flavor"], "open")
        team = self.layer.build(self.work, "HEAD", os.path.join(self.tmp.name, "team"), sys.executable, "team",
                                keep=os.path.join(self.tmp.name, "team-seed"))
        top = os.path.join(self.tmp.name, "team-seed", "embed", "acme")
        self.assertTrue(os.path.isfile(os.path.join(top, "i18n", "ru.json")))   # the team's layer carries the embed
        self.assertTrue(os.path.isfile(os.path.join(top, "modules", "stats", "stats.py")))
        self.assertFalse(os.path.exists(os.path.join(top, "tests")))   # ...without its tests (appcode.EMBED_LEFT_OUT)
        self.assertEqual(team["flavor"], "team")

    def test_a_red_run_on_the_cut_tree_refuses_the_open_layer(self):
        self.tree("FAILED")
        with self.assertRaises(self.layer.Refused) as e:
            self.layer.check_open(self.work, "HEAD", os.path.join(self.tmp.name, "w"), sys.executable)
        self.assertIn("FAILED", str(e.exception))
        self.tree("OK")
        got = self.layer.check_open(self.work, "HEAD", os.path.join(self.tmp.name, "w2"), sys.executable)
        self.assertIn("['test_x.py']", got)
        self.assertIn("True", got)
        tested = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.work, capture_output=True, text=True).stdout.strip()
        self.g("commit", "-qm", "later", "--allow-empty")   # HEAD moved while the suite ran: the layer is the tested one
        sheet = self.layer.build(self.work, "HEAD", os.path.join(self.tmp.name, "out"), sys.executable, "open",
                                 tested=tested)
        self.assertEqual(sheet["commit"], tested)
        with self.assertRaises(self.layer.Refused):   # out of time — a refusal, not a traceback
            self.layer.check_open(self.work, "HEAD", os.path.join(self.tmp.name, "w3"), sys.executable,
                                  args=["--slow"], timeout=0.001)


class Exit(Repo):
    """What goes out as the open flavor: the published snapshot, cut, checked by the guard over the whole tree against
    .open-deny of the main copy (strings only the team's code carries: its servers, its repositories)."""

    def test_the_open_tree_leaves_out_what_is_never_published(self):
        self.tree("OK")
        self.write("docs/notes.md", "n\n")
        self.write("evals/locale/ru/case.json", "{}\n")
        self.write("evals/prompts/case.json", "{}\n")
        self.g("add", "-A")
        self.g("commit", "-qm", "docs")
        out = os.path.join(self.tmp.name, "open")
        self.layer.open_tree(self.work, "HEAD", out)
        self.assertFalse(os.path.exists(os.path.join(out, "docs")))
        self.assertFalse(os.path.exists(os.path.join(out, "evals", "locale")))
        self.assertTrue(os.path.isfile(os.path.join(out, "evals", "prompts", "case.json")))

    def test_publish_leaves_out_the_same(self):
        with open(os.path.join(REPO, "tools", "publish.sh"), encoding="utf-8") as f:
            line = next(x for x in f if x.startswith('rm -rf "$TMP/pub/'))
        dropped = {w.strip('"').split("$TMP/pub/", 1)[1] for w in line.split("#")[0].split()[2:]}
        self.assertEqual(dropped, {g[:-2] for g in appcode.UNPUBLISHED})

    def test_the_guard_reads_tests_and_paths_whatever_the_case(self):
        self.tree("OK")
        self.write("tests/test_y.py", 'URL = "https://Reports.Example.Test/v1"\n')
        self.write("tools/reports.example.test.txt", "x\n")
        self.write("tools/Reports.Example.Test/a.txt", "x\n")
        self.g("add", "-A")
        self.g("commit", "-qm", "leak")
        out = os.path.join(self.tmp.name, "open")
        self.layer.open_tree(self.work, "HEAD", out)
        self.assertEqual(appcode.open_guard(out, ["reports.example.TEST"], tests=True),
                         ["tests/test_y.py: carries a forbidden string",
                          "tools/Reports.Example.Test/: its path carries a forbidden string",
                          "tools/Reports.Example.Test/a.txt: its path carries a forbidden string",
                          "tools/reports.example.test.txt: its path carries a forbidden string"])
        other = os.path.join(self.tmp.name, "other")
        os.makedirs(other)
        with open(os.path.join(other, "z.py"), "w", encoding="utf-8") as f:
            f.write('"\u00c4rzte.example"\n')   # a letter outside ASCII is matched as written
        self.assertEqual(appcode.open_guard(other, ["\u00c4rzte.example"]), ["z.py: carries a forbidden string"])
        if hasattr(os, "symlink") and os.name != "nt":   # a link is its text, even one that leads nowhere
            os.symlink("/srv/reports.example.test/x", os.path.join(other, "gone"))
            target = os.path.join(self.tmp.name, "reports.example.test")   # a link to a folder that is there
            os.makedirs(target)
            os.symlink(target, os.path.join(other, "dir"))
            self.assertEqual(appcode.open_guard(other, ["REPORTS.example.test"]),
                             ["dir/: its link carries a forbidden string", "gone: carries a forbidden string"])
        self.assertEqual(appcode.open_guard(out, [], tests=True), [])

    def test_an_allow_line_of_a_forbidden_string_leaves_the_open_tree(self):
        # tools/publish-allow.txt lets the working repository's snapshot name the team's servers and repositories; the
        # open tree has nothing such a line allows (the code naming it is cut), so the line goes, the rest stays
        self.tree("OK")
        self.write("tools/publish-allow.txt", "# allowed\ngithub.com/someone/tool\nreports.example.test\n"
                                              "team/Reports.Example.Test-modules\n")
        self.g("add", "-A")
        self.g("commit", "-qm", "allow")
        out = os.path.join(self.tmp.name, "open")
        self.layer.open_tree(self.work, "HEAD", out, forbid=["REPORTS.example.test"])
        with open(os.path.join(out, "tools", "publish-allow.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "# allowed\ngithub.com/someone/tool\n")
        self.assertEqual(appcode.open_guard(out, ["REPORTS.example.test"], tests=True), [])
        kept = os.path.join(self.tmp.name, "kept")   # no forbidden strings given: nothing to drop
        self.layer.open_tree(self.work, "HEAD", kept)
        with open(os.path.join(kept, "tools", "publish-allow.txt"), encoding="utf-8") as f:
            self.assertIn("reports.example.test", f.read())

    def test_the_open_history_starts_on_its_own_day(self):
        self.tree("OK")
        head = "# What's new\n\nNewest first.\n\n"
        self.write("CHANGES.md", head + "## 2026-03-02\n- new\n\n## 2026-03-01\n- opened\n\n"
                                        "## 2026-02-28 (a fix)\n- before, thanks: @someone (PR #7)\n")
        self.g("add", "-A")
        self.g("commit", "-qm", "changes")
        whole = os.path.join(self.tmp.name, "whole")
        with mock.patch.object(appcode, "OPEN_CHANGES_FROM", None):   # not opened yet: the file as it is
            self.layer.open_tree(self.work, "HEAD", whole)
        with open(os.path.join(whole, "CHANGES.md"), encoding="utf-8") as f:
            self.assertIn("thanks: @someone (PR #7)", f.read())   # its thanks as they are too
        out = os.path.join(self.tmp.name, "open")
        with mock.patch.object(appcode, "OPEN_CHANGES_FROM", "2026-03-01"):
            self.layer.open_tree(self.work, "HEAD", out)
        with open(os.path.join(out, "CHANGES.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), head + "## 2026-03-02\n- new\n\n## 2026-03-01\n- opened\n")
        tree = os.path.join(self.tmp.name, "layer-tree")   # the layer's cut does the same (its CHANGES.md is "what's new")
        os.makedirs(tree)
        with open(os.path.join(tree, "CHANGES.md"), "w", encoding="utf-8", newline="\n") as f:
            f.write(head + "## 2026-02-28\n- before\n\n## 2026-02-27 (a fix)\n- older\n")
        with mock.patch.object(appcode, "OPEN_CHANGES_FROM", "2026-03-01"):
            self.layer.cut_open(tree)
        with open(os.path.join(tree, "CHANGES.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), head.rstrip("\n") + "\n")   # the header alone, one line end

    def test_the_open_thanks_name_the_person_not_the_pull_request(self):
        # owner 11.11: the open CHANGES.md thanks "@login" — the number is of a pull request of the working repository,
        # which the open one does not have; the working CHANGES.md keeps it
        self.tree("OK")
        text = ("# What's new\n\nAn item from someone else's PR ends with thanks: \"— thanks: @login (PR #N)\".\n\n"
                "## 2026-03-01\n- one — thanks: @some-one (PR #7)\n- two, wrapped — thanks: @other\n  (PR #8)\n"
                "- three — thanks: @a, @b (PR #9, #10)\n- four — thanks: @a (PR #11, partly)\n"
                "- five — thanks: @a (PR #12 of the modules)\n- six — thanks: @a (issue #3)\n- seven (PR #13)\n"
                "- eight — thanks: @a (PR #14 and #15)\n- nine — thanks: @a (PR #16, the modular idea)\n")
        self.write("CHANGES.md", text)
        self.g("add", "-A")
        self.g("commit", "-qm", "changes")
        out = os.path.join(self.tmp.name, "open")
        with mock.patch.object(appcode, "OPEN_CHANGES_FROM", "2026-03-01"):
            self.layer.open_tree(self.work, "HEAD", out)
        with open(os.path.join(out, "CHANGES.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "# What's new\n\nAn item from someone else's PR ends with thanks: \"— thanks: "
                             "@login\".\n\n## 2026-03-01\n- one — thanks: @some-one\n- two, wrapped — thanks: @other\n"
                             "- three — thanks: @a, @b\n- four — thanks: @a (partly)\n- five — thanks: @a\n"
                             "- six — thanks: @a (issue #3)\n- seven (PR #13)\n- eight — thanks: @a\n"
                             "- nine — thanks: @a (the modular idea)\n")
        with open(os.path.join(self.work, "CHANGES.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), text)

    def test_the_open_tree_credits_every_contributor_and_its_guard_spares_only_the_credits(self):
        # owner 17.11: the author line and the contributors of the whole history (the open CHANGES.md keeps only its
        # own), built before the cut; the guard spares the author's login only in the credits of README.md and
        # CONTRIBUTORS.md
        from tools import publish_allow
        self.tree("OK")
        self.write("README.md", "# Hub\n\nCreated by Alice Smith ([@alice](https://github.com/alice))\n\n## Use\n\nRun it.\n")
        self.write("CHANGES.md", "# What's new\n\n## 2026-03-02\n- new — thanks: @carol (PR #9)\n\n"
                                 "## 2026-02-28\n- old — thanks: @bob (PR #7)\n- older — thanks: @bob, @carol\n  (PR #8)\n")
        self.write("notes.md", "ask alice\n")
        self.g("add", "-A")
        self.g("commit", "-qm", "credits")
        out = os.path.join(self.tmp.name, "open")
        with mock.patch.object(appcode, "OPEN_CHANGES_FROM", "2026-03-01"):
            self.layer.open_tree(self.work, "HEAD", out)
        with open(os.path.join(out, "CONTRIBUTORS.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("Created by Alice Smith ([@alice](https://github.com/alice))", text)
        self.assertIn("- [@bob](https://github.com/bob): 2 pull requests\n"   # a tie: by the login
                      "- [@carol](https://github.com/carol): 2 pull requests\n", text)
        with open(os.path.join(out, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        self.assertTrue(readme.startswith("# Hub\n\nCreated by Alice Smith"), readme)
        self.assertIn("## Use\n\nRun it.\n\n## Contributors\n", readme)
        self.assertIn("- [@bob](https://github.com/bob): 2 pull requests", readme)
        with open(os.path.join(out, "CHANGES.md"), encoding="utf-8") as f:
            self.assertNotIn("@bob", f.read())   # the cut history, as before
        self.assertEqual(appcode.open_guard(out, ["ALICE"], tests=True, spare=publish_allow.credit_file),
                         ["notes.md: carries a forbidden string"])
        self.assertEqual(appcode.open_guard(out, ["ALICE"], tests=True),
                         ["CONTRIBUTORS.md: carries a forbidden string", "README.md: carries a forbidden string",
                          "notes.md: carries a forbidden string"])
        with open(os.path.join(out, "README.md"), "a", encoding="utf-8") as f:
            f.write("Alice Smith, again\n")   # the name outside the author line counts there too
        self.assertEqual(appcode.open_guard(out, ["ALICE"], tests=True, spare=publish_allow.credit_file),
                         ["README.md: carries a forbidden string", "notes.md: carries a forbidden string"])

    def test_the_open_tree_has_the_open_catalog(self):
        # the team's catalog is named only by its embed's modules/private.json, which the open flavor cuts: the open
        # tree's official catalog is the open core's own (core/marketplaces.source_of)
        from core import marketplaces
        self.tree("OK")
        self.write("embed/acme/modules/private.json", '{"repo": "team/modules"}\n')
        self.g("add", "-A")
        self.g("commit", "-qm", "pin")
        self.assertEqual(marketplaces.source_of(self.work), "team/modules")
        out = os.path.join(self.tmp.name, "open")
        self.layer.open_tree(self.work, "HEAD", out)
        self.assertFalse(os.path.exists(os.path.join(out, "embed")))
        self.assertEqual(marketplaces.source_of(out), marketplaces.OPEN_SOURCE)
        self.assertEqual(marketplaces.OPEN_SOURCE, "icons8/agents-hub-plugins")

    def test_a_forbidden_string_refuses_before_the_suite_runs(self):
        self.tree("OK")
        self.write("tests/test_y.py", '"reports.example.test"\n')
        self.g("add", "-A")
        self.g("commit", "-qm", "leak")
        with self.assertRaises(self.layer.Refused) as e:
            self.layer.check_open(self.work, "HEAD", os.path.join(self.tmp.name, "w"), sys.executable,
                                  forbid=["Reports.Example.Test"])
        self.assertIn("tests/test_y.py: carries a forbidden string", str(e.exception))
        self.assertNotIn("test_x.py", str(e.exception))   # the suite never ran

    def test_no_deny_file_no_open_release(self):
        self.tree("OK")
        for text in (None, "", "# only a comment\n\n"):
            path = os.path.join(self.work, appcode.DENY)
            if text is not None:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
            with self.assertRaises(self.layer.Refused) as e:
                self.layer.deny_of(self.work)
            self.assertIn(appcode.DENY, str(e.exception))
        with open(os.path.join(self.work, appcode.DENY), "w", encoding="utf-8") as f:
            f.write("# servers\nreports.example.test\n\n  repo.example.test  \n")
        self.assertEqual(self.layer.deny_of(self.work), ["reports.example.test", "repo.example.test"])

    def test_a_worktree_adds_its_own_strings_to_the_main_copy(self):
        self.tree("OK")
        with open(os.path.join(self.work, appcode.DENY), "w", encoding="utf-8") as f:
            f.write("reports.example.test\n")
        wt = os.path.join(self.tmp.name, "wt")
        self.g("worktree", "add", "-q", wt)
        self.assertEqual(self.layer.deny_of(wt), ["reports.example.test"])   # a worktree has no copy of the file
        with open(os.path.join(wt, appcode.DENY), "w", encoding="utf-8") as f:
            f.write("repo.example.test\n")
        self.assertEqual(self.layer.deny_of(wt), ["reports.example.test", "repo.example.test"])

    def test_the_command_refuses_an_open_layer_without_the_deny_file(self):
        self.tree("OK")
        out = os.path.join(self.tmp.name, "layer")
        args = ["--repo", self.work, "--ref", "HEAD", "--out", out, "--python", sys.executable, "--flavor", "open"]
        deny_of = self.layer.deny_of   # the main copy is the small repo's, not this tree's
        with mock.patch.object(self.layer, "deny_of", lambda repo=None: deny_of(repo or self.work)):
            with self.assertRaises(SystemExit) as e:
                self.layer.main(args)
            self.assertIn(appcode.DENY, str(e.exception.code))
            self.g("rm", "-rq", "embed")   # its small embed keeps no Embed API: the team build would refuse it first
            self.g("commit", "-qm", "no embed")
            self.layer.main(args[:-2])   # the team flavor does not need it
            with open(os.path.join(self.work, appcode.DENY), "w", encoding="utf-8") as f:
                f.write("reports.example.test\n")
            with contextlib.redirect_stdout(io.StringIO()):
                self.layer.main(args)
            self.write("core/y.py", '"REPORTS.example.test"\n')
            self.g("add", "-A")
            self.g("commit", "-qm", "leak")
            with self.assertRaises(SystemExit) as e:
                self.layer.main(args)
            self.assertIn("core/y.py: carries a forbidden string", str(e.exception.code))


class MacLauncher(Repo):
    """The Mac launcher bundle: the team's is signed with the company's Developer ID; the open flavor carries instead
    "Hub by Icons8.app", built here from the same launcher.c and signed ad hoc (appcode.OPEN_SWAP) — the open tree has
    no company's certificate, a team build stays as it was."""
    TEAM = "deploy/mac/Agents Hub.app/Contents/MacOS/Agents Hub"
    OWN = "deploy/mac/open/Hub by Icons8.app/Contents/MacOS/Hub by Icons8"
    PLACED = "deploy/mac/Hub by Icons8.app/Contents/MacOS/Hub by Icons8"
    FAT = b"\xca\xfe\xba\xbe\x00\x00\x00\x02"   # a universal Mach-O of two architectures
    SIGNED = FAT + b" launcher; signed: Authority=" + appcode.SIGNED + b": Example Ltd (ABCDE12345)\n"
    AD_HOC = FAT + b" launcher; Signature=adhoc\n"

    def put(self, rel, data):
        path = os.path.join(self.work, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)

    def bundles(self):
        self.tree("OK")
        self.put(self.TEAM, self.SIGNED)
        self.put(self.OWN, self.AD_HOC)
        self.put("deploy/mac/launcher.c", b"int main(void) { return 0; }\n")
        for rel in (self.TEAM, self.OWN):   # programs: 0755 in git
            os.chmod(os.path.join(self.work, *rel.split("/")), 0o755)
        self.g("add", "-A")
        self.g("update-index", "--chmod=+x", self.TEAM, self.OWN)   # a disk without the bit (Windows) gives git none
        self.g("commit", "-qm", "bundles")

    def read(self, root, rel):
        with open(os.path.join(root, *rel.split("/")), "rb") as f:
            return f.read()

    def test_the_open_tree_carries_the_ad_hoc_bundle_in_place_of_the_teams(self):
        self.bundles()
        out = os.path.join(self.tmp.name, "open")
        self.layer.open_tree(self.work, "HEAD", out)
        self.assertFalse(os.path.exists(os.path.join(out, "deploy", "mac", "Agents Hub.app")))
        self.assertFalse(os.path.exists(os.path.join(out, "deploy", "mac", "open")))   # moved, not copied
        self.assertEqual(self.read(out, self.PLACED), self.AD_HOC)
        self.assertTrue(os.path.isfile(os.path.join(out, "deploy", "mac", "launcher.c")))
        self.assertEqual(appcode.open_guard(out, tests=True), [])
        again = os.path.join(self.tmp.name, "again")   # a tree cut before (a hub's own build): the bundle stays
        shutil.copytree(out, again)
        self.layer.cut_open(again)
        self.assertEqual(self.read(again, self.PLACED), self.AD_HOC)
        sheet = self.layer.build(self.work, "HEAD", os.path.join(self.tmp.name, "layer"), sys.executable, "open",
                                 keep=os.path.join(self.tmp.name, "seed"))
        self.assertEqual(self.read(os.path.join(self.tmp.name, "seed"), self.PLACED), self.AD_HOC)
        self.assertEqual(sheet["flavor"], "open")
        with open(os.path.join(self.tmp.name, "seed", appcode.MANIFEST), encoding="utf-8") as f:
            files = json.load(f)["files"]
        self.assertTrue(files[self.PLACED].get("x"), files[self.PLACED])   # still a program: launchd runs it
        if os.name != "nt":
            self.assertTrue(os.stat(os.path.join(self.tmp.name, "seed", *self.PLACED.split("/"))).st_mode & 0o100)

    def test_a_team_build_keeps_the_teams_bundle_as_it_was(self):
        self.bundles()
        self.layer.build(self.work, "HEAD", os.path.join(self.tmp.name, "team"), sys.executable, "team",
                         keep=os.path.join(self.tmp.name, "seed"))
        seed = os.path.join(self.tmp.name, "seed")
        self.assertEqual(self.read(seed, self.TEAM), self.SIGNED)
        with open(os.path.join(seed, appcode.MANIFEST), encoding="utf-8") as f:
            self.assertTrue(json.load(f)["files"][self.TEAM].get("x"))
        self.assertFalse(os.path.exists(os.path.join(seed, "deploy", "mac", "open")))   # appcode.LEFT_OUT
        self.assertFalse(os.path.exists(os.path.join(seed, "deploy", "mac", "Hub by Icons8.app")))

    def test_the_guard_refuses_the_teams_bundle_or_its_signature_in_the_open_tree(self):
        for rel, data, why in ((self.TEAM, self.AD_HOC, "cut from the open flavor"),      # the cut undone
                               (self.OWN, self.AD_HOC, "cut from the open flavor"),       # the swap undone
                               (self.PLACED, self.SIGNED, "signed with a Developer ID")):  # the team's bytes put back
            with self.subTest(rel):
                root = tempfile.mkdtemp(dir=self.tmp.name)
                path = os.path.join(root, *rel.split("/"))
                os.makedirs(os.path.dirname(path))
                with open(path, "wb") as f:
                    f.write(data)
                self.assertEqual(appcode.open_guard(root), [f"{rel}: {why}" + (
                    " (the open flavor's are signed ad hoc)" if why.startswith("signed") else "")])
        loose = tempfile.mkdtemp(dir=self.tmp.name)   # a signed program or image outside a bundle too
        for name in ("helper", "Hub.dmg"):
            with open(os.path.join(loose, name), "wb") as f:
                f.write(self.SIGNED if name == "helper" else b"koly " + self.SIGNED)
        self.assertEqual(appcode.open_guard(loose), [f"{n}: signed with a Developer ID (the open flavor's are signed ad hoc)"
                                                     for n in ("Hub.dmg", "helper")])
        notes = tempfile.mkdtemp(dir=self.tmp.name)   # text naming a Developer ID outside a bundle is no signature
        with open(os.path.join(notes, "app.py"), "wb") as f:
            f.write(b'"app.py build <' + appcode.SIGNED + b': ...>"\n')
        with open(os.path.join(notes, "A.class"), "wb") as f:   # nor a Java class (CAFEBABE, then its version)
            f.write(b"\xca\xfe\xba\xbe\x00\x00\x00\x34 " + appcode.SIGNED)
        self.assertEqual(appcode.open_guard(notes), [])

    def test_the_open_tree_of_this_one_has_no_company_certificate(self):
        # this tree's files as the open flavor cuts them (OPEN_CUT, OPEN_SWAP): not one carries the company's
        # Developer ID, and its Mac launcher is the ad-hoc one; in the open tree itself the cut finds nothing to do
        listed = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=REPO,
                                capture_output=True, text=True, check=True).stdout.split("\0")
        import fnmatch
        cut = appcode.OPEN_CUT + tuple(src + "/*" for src, _ in appcode.OPEN_SWAP)
        company = appcode.SIGNED + b": Icons8"
        for rel in filter(None, listed):
            path = os.path.join(REPO, *rel.split("/"))
            if not os.path.isfile(path):
                continue
            with open(path, "rb") as f:
                data = f.read()
            if company in data:
                self.assertTrue(any(fnmatch.fnmatchcase(rel, g) for g in cut), rel)
        mac = os.path.join(self.tmp.name, "mac")
        shutil.copytree(os.path.join(REPO, "deploy", "mac"), os.path.join(mac, "deploy", "mac"), symlinks=True)
        self.layer.leave_out(mac, appcode.OPEN_CUT)
        self.layer.swap_open(mac)
        self.assertEqual(appcode.open_guard(mac), [])
        exe = os.path.join(mac, "deploy", "mac", "Hub by Icons8.app", "Contents", "MacOS", "Hub by Icons8")
        with open(exe, "rb") as f:
            self.assertEqual(f.read(4), b"\xca\xfe\xba\xbe")   # a universal Mach-O (tests/test_mac_app.py OpenBundle)
        self.assertEqual([n for n in os.listdir(os.path.join(mac, "deploy", "mac")) if n.endswith(".app") or n == "open"],
                         ["Hub by Icons8.app"])


class Lists(unittest.TestCase):
    """A company's code is never the core's own: it lies in its embed (core/embed.py), and the open flavor is the tree
    without embed/ (spec d2144426) — nothing to list, nothing to forget."""

    def test_no_slot_code_and_no_pin_in_the_cores_own_folders(self):
        for name, path in cutout.FILES.items():   # a shim of the move (embed.SHIMS) leads into the embed, it is no code
            self.assertTrue(path in embed.SHIMS or not os.path.isfile(os.path.join(REPO, *path.split("/"))), name)
        self.assertFalse(os.path.isfile(os.path.join(REPO, "modules", "private.json")))

    def test_the_open_cut_is_the_embeds(self):
        # ...and the team's Mac launcher, signed with the company's Developer ID: the open one takes its place (OPEN_SWAP)
        # ...and deliver/, the team build's delivery server (the open build is given out by pages)
        self.assertEqual(appcode.OPEN_CUT, ("embed/*", "EMBED.json", *embed.SHIMS, "deploy/mac/Agents Hub.app/*", "deliver/*"))
        self.assertEqual(appcode.OPEN_SWAP, (("deploy/mac/open/Hub by Icons8.app", "deploy/mac/Hub by Icons8.app"),))

    def test_the_core_does_not_name_the_report_server(self):
        from core import catalog
        self.assertNotIn("url", catalog.DEFAULTS["stats"])   # modules/stats keeps its own address (cut with it)


class Compat(unittest.TestCase):
    def test_a_tree_without_the_gateway_has_no_gateway_line(self):
        spec = importlib.util.spec_from_file_location("hub_test_compat_open", os.path.join(REPO, "tools", "compat.py"))
        compat = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(compat)
        with cut():
            self.assertIsNone(compat.gateway("/nonexistent/claude"))


if __name__ == "__main__":
    unittest.main()
