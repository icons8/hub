"""The Russian owner's path at the core's own sites that pick the language themselves (bin/hub, core, hosts, platforms):
the tests speak English (tests/__init__.py HUB_LANG=en), so each of these places is run once more for a Russian owner
the way a real hub makes him one — hub.json `language`, the process language (i18n.use), the language a spawn carries —
and the text is checked against the Russian catalog, with the English twin on the same path so the test proves the
choice, not a constant. The source holds no Cyrillic: the expected text is `i18n.tr("ru", key, …)`."""
import tests  # noqa: F401 — keeps the machine's settings out
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import platforms
from core import busd, busstate, codexcli, i18n, journal, owner, reducer, schedule, spool
from hosts.streamjson import StreamHost
from platforms import windows
from tests.fakes import cut
from tests.test_ru_owner import ProcessLanguage

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(ROOT, "bin", "hub")
OWNER = {"channel": "cli", "owner": True}
DAY = 1791979200   # 2026-10-14 12:00 UTC: daytime, so a reminder is not moved to the morning


def ru(key, **params):
    return i18n.tr("ru", key, **params)


def en(key, **params):
    return i18n.tr("en", key, **params)


class TaskRecordCli(unittest.TestCase):
    """bin/hub `task note` / `task head`: the record and the journal fact are kept for the owner — in his language
    (hub.json), whoever writes the line; the session's own output stays English."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="ru-sites-rec-")
        self.addCleanup(platforms.current.rmtree, self.state)
        j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        j.append("session.requested", "hub", {"cwd": "/h", "session_id": "s-hub", "kind": "hub", **OWNER})
        for n in ("frontend", "frontend-ci"):
            j.append("session.requested", n, {"cwd": "/w", "session_id": f"s-{n}", **OWNER})
        ev = j.append("delegation.created", None, {"session": "frontend-ci", "for_session": "frontend",
                                                   "context": "fix the CI secrets", "channel": "session",
                                                   "owner": False, "sender": "frontend"})
        self.did = f"d{ev['seq']}"

    def hub(self, lang, *args, me=None):
        cfg = os.path.join(self.state, f"hub-{lang}.json")
        with open(cfg, "w") as f:
            json.dump({"update": {"enabled": False}, "language": lang}, f)
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_CLI_WAIT": "0", "HUB_CONFIG": cfg}
        env.pop("HUB_SESSION", None)
        env.pop("HUB_LANG", None)   # the owner's language comes from hub.json, as on a real hub
        if me:
            env["HUB_SESSION"] = me
        r = subprocess.run([sys.executable, HUB, *args], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        return r

    def record(self):
        with open(os.path.join(self.state, "tasks", f"{self.did}.md"), encoding="utf-8") as f:
            return f.read()

    def close(self):
        journal.Journal(os.path.join(self.state, "journal.jsonl")).append("delegation.done", self.did, dict(OWNER))

    def test_a_closed_task_and_the_owners_line_are_marked_in_his_language(self):
        self.close()
        self.hub("ru", "task", "note", self.did, "kept vault", me="frontend-ci")
        self.hub("ru", "task", "note", self.did, "checked")                       # the owner himself
        self.hub("en", "task", "note", self.did, "twin line", me="frontend-ci")
        self.hub("en", "task", "note", self.did, "twin owner")
        rec = self.record()
        self.assertIn(f"frontend-ci: {ru('cli.task.closed_mark')} kept vault", rec)
        self.assertIn(f"{ru('cli.task.note_by_owner')}: {ru('cli.task.closed_mark')} checked", rec)
        self.assertIn(f"frontend-ci: {en('cli.task.closed_mark')} twin line", rec)
        self.assertIn(f"{en('cli.task.note_by_owner')}: {en('cli.task.closed_mark')} twin owner", rec)

    def test_a_head_edit_lands_in_the_journal_in_the_owners_language(self):
        for lang, word in (("ru", "first"), ("en", "second")):
            out = self.hub(lang, "task", "show", self.did, me="frontend").stdout   # a session reads English
            sha = out.rsplit("fingerprint ", 1)[1].split()[0]
            self.hub(lang, "task", "head", self.did, "--sha", sha, word, "statement", me="frontend")
        heads = [e["data"]["text"] for _, e in spool.take(os.path.join(self.state, "spool"))
                 if e["type"] == "task.noted" and e["data"].get("head")]
        self.assertEqual(heads, [ru("cli.task.head_noted"), en("cli.task.head_noted")])


class BusChat(unittest.TestCase):
    """core/busd.py: the other hub's words to this owner get a title in his language (hub.json or the state)."""

    def state(self):
        st = reducer.empty()
        st["bus"] = busstate.empty()
        st["bus"].update(enabled=True, crypto_ready=True, me={"fp": "hb1-me", "label": "Me"})
        st["bus"]["contacts"]["peer"] = {"fp": "hb1-d", "label": "Hub of P.", "status": "known", "verified": True,
                                          "sign_pub": "S", "enc_pub": "E", "may_write": [], "auto_from": []}
        return st

    def title(self, cfg):
        recorded = []
        hub = types.SimpleNamespace(state=self.state(), config=cfg, record=lambda t, e, d: recorded.append(d))
        langs, real = [], busd._lang
        with mock.patch.object(busd, "_lang", lambda h: langs.append(real(h)) or langs[-1]):
            busd._after(hub, "core.bus_chat_received", {"contact": "peer", "status": "ok", "text": "Hello",
                                                        "for": "owner"})
        (data,) = recorded
        return data["title"], langs

    def test_the_chat_title_is_in_the_owners_language(self):
        title, langs = self.title({"language": "ru"})
        self.assertEqual(title, ru("core.bus.chat_title", label="Hub of P."))
        self.assertEqual(langs, ["ru"])   # the catalogs share the text: the choice is what is checked
        title, langs = self.title({})
        self.assertEqual(title, en("core.bus.chat_title", label="Hub of P."))
        self.assertEqual(langs, ["en"])


@cut.needs("codex_pool")
class CodexUse(ProcessLanguage, unittest.TestCase):
    """`hub codex use` (the Codex pool's command, core/codexslot.py): the switch's reason goes into the journal for the
    owner, in his language."""

    def setUp(self):
        from modules.accounts import codexpool
        self.state = os.path.realpath(tempfile.mkdtemp(prefix="ru-sites-cxp-"))
        self.addCleanup(platforms.current.rmtree, self.state)
        hub, b = codexcli.home_of(self.state), os.path.join(self.state, "moddata", "accounts", "codex", "codex-2")
        for d in (hub, b):
            os.makedirs(d, mode=0o700, exist_ok=True)
            os.chmod(d, 0o700)
        st = {"codex": {"pool": {"on": True, "profiles": [{"name": "codex-1", "home": hub, "hub": True},
                                                          {"name": "codex-2", "home": b}]},
                        "account": {"active": "codex-1"}}, "sessions": {}}
        self.enterContext(mock.patch.object(journal, "load_state", lambda state_dir: st))
        self.enterContext(mock.patch.object(codexpool.Pool, "use", lambda pool, acc, name, force, now: None))

    def why(self, lang):
        self.speak(lang)
        put = []
        with contextlib.redirect_stdout(io.StringIO()):
            code = codexcli.main(["use", "codex-2"], self.state, put=lambda *a: put.append(a) or len(put))
        self.assertEqual(code, 0)
        ((kind, _, data),) = put
        self.assertEqual(kind, "codex.switched")
        return data["why"]

    def test_the_manual_switch_reason_is_in_the_owners_language(self):
        self.assertEqual(self.why("ru"), ru("cli.codex.picked_manually"))
        self.assertEqual(self.why("en"), en("cli.codex.picked_manually"))


def form(*questions):
    return {"request_id": "r1", "tool": "AskUserQuestion", "since": DAY,
            "input": {"questions": [{"question": q, **({"header": h} if h is not None else {})} for q, h in questions]}}


class Push(ProcessLanguage, unittest.TestCase):
    """core/owner.py push_text through alerts(): the push texts in the process language (hubd's push module)."""

    def asked(self, req, v=0):
        st = reducer.empty()
        st["sessions"]["maps"] = {"name": "maps", "state": "idle", "since": 1, "pending": [req]}
        if v:
            st["owner"]["reminders"]["q:maps:r1"] = v
        (a,) = owner.alerts(st, DAY + 100)
        return a

    def notice(self, data):
        st, _, why = reducer.reduce(reducer.empty(), {"seq": 1, "ts": 1000, "type": "owner.notify", "entity": None,
                                                      "src": "module", "data": data})
        self.assertIsNone(why)
        (a,) = owner.alerts(st, 1100)
        return a

    def tool(self):
        st = reducer.empty()
        st["sessions"]["maps"] = {"name": "maps", "state": "idle", "since": 1, "requests": [
            {"tool": "browser:lobo", "state": "needs-owner", "since": 1000, "why": "check the login"}]}
        (a,) = owner.alerts(st, 1100)
        return a

    MOD = {"channel": "plugin", "owner": False, "sender": "mod:shop"}

    def check(self, lang):
        self.speak(lang)
        t = lambda key, **kw: i18n.tr(lang, key, **kw)  # noqa: E731
        a = self.asked(form(("Summary:", "Summary")))
        self.assertEqual((a["title"], a["body"]), (t("web.push.waits", session="maps"),) * 2)
        a = self.asked(form(("Publish the issue?", "Issue"), ("Write the post?", "Post"), ("To whom?", "Addressee")))
        self.assertEqual(a["body"], "Publish the issue? " + t("web.push.more", count=2))
        a = self.asked({"request_id": "r1", "tool": "Bash", "since": DAY, "input": {"command": "git push"}})
        self.assertEqual((a["title"], a["body"]), (t("web.push.allow", tool="Bash"), "git push"))
        a = self.asked(form(("Confirm?", "Check-in")), v=1)
        self.assertEqual(a["body"], t("web.push.reminder", time=t("web.push.minutes", count=30), body="Confirm?"))
        a = self.asked(form(("Confirm?", "Check-in")), v=2)
        self.assertEqual(a["body"], t("web.push.reminder", time=t("web.push.hours", count=3), body="Confirm?"))
        self.assertEqual(self.tool()["title"], t("web.push.connect", tool="browser:lobo"))
        a = self.notice({"text": "No login, no order.", "topic": "Login", "urgency": "now", "due": "16:00", **self.MOD})
        self.assertEqual(a["body"], t("web.push.now_due", due="16:00") + " No login, no order.")
        a = self.notice({"text": "Valid until Friday.", "urgency": "later", **self.MOD})
        self.assertEqual(a["body"], t("web.push.later") + " Valid until Friday.")
        n = {"id": "n:1", "sender": "mod:shop", "since": 1, "at": 1, "v": 1, "title": "", "text": "x", "urgent": False,
             "urgency": "now", "due": 5e18}
        self.assertEqual(owner.push_text({"kind": "notice", "notice": n})["body"], t("web.push.now") + " x")

    def test_russian(self):
        self.check("ru")

    def test_english_twin(self):
        self.check("en")


class CronWords(ProcessLanguage, unittest.TestCase):
    """core/schedule.py human_cron through when_text: "when" in words in the process language (views, `hub schedule`)."""

    def cases(self, t):
        every, weekdays = t("core.schedule.day.every"), t("core.schedule.day.weekdays")
        return {
            "0 9 1 * *": t("core.schedule.cron_raw", expr="0 9 1 * *"),
            "*/5 * * * *": t("core.schedule.every_min", n=5),
            "0 */2 * * *": t("core.schedule.every_hours", n=2),
            "0 * * * *": t("core.schedule.hourly"),
            "0 9 * * *": t("core.schedule.day_at", day=every, times="09:00"),
            "0 9,18 * * 1-5": t("core.schedule.day_at", day=weekdays,
                                times=t("core.schedule.and", a="09:00", b="18:00")),
            "0 10 * * 0,6": t("core.schedule.day_at", day=t("core.schedule.day.weekends"), times="10:00"),
            "30 8 * * 1": t("core.schedule.day_at", day=t("core.schedule.day.1"), times="08:30"),
            "0 9-18 * * 3": t("core.schedule.day_hours", day=t("core.schedule.day.3"), a=9, b=18),
        }

    def check(self, lang):
        self.speak(lang)
        t = lambda key, **kw: i18n.tr(lang, key, **kw)  # noqa: E731
        for cron, words in self.cases(t).items():
            job = {"kind": "cron", "cron": cron, "until": None}
            self.assertEqual(schedule.when_text(job, "UTC"), words + " " + t("core.schedule.indefinitely"), cron)

    def test_russian(self):
        self.check("ru")

    def test_english_twin(self):
        self.check("en")


class AgentFileWithoutProfile(unittest.TestCase):
    """hosts/streamjson.py: a new agent's CLAUDE.md without a profile says so in the person's language (the spawn's)."""

    def made(self, lang):
        state = tempfile.mkdtemp(prefix="ru-sites-agentfile-")
        self.addCleanup(platforms.current.rmtree, state)
        path = StreamHost(state, claude=(sys.executable,)).agent_file("s-dev", name="devops", lang=lang)
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_the_missing_profile_line_is_in_the_spawns_language(self):
        self.assertIn("\n" + ru("agent_file.no_profile") + "\n", self.made("ru"))
        self.assertIn("\n" + en("agent_file.no_profile") + "\n", self.made("en"))


class WindowsClaudeTooOld(ProcessLanguage, unittest.TestCase):
    """platforms/windows.py claude_problem: the text goes into the journal for the owner — in his language."""

    def test_the_old_claude_code_problem_is_in_the_owners_language(self):
        need = ".".join(map(str, windows.HOOK_EXEC_FORM))
        for lang in ("ru", "en"):
            self.speak(lang)
            self.assertEqual(windows.claude_problem("2.1.100"),
                             i18n.tr(lang, "cli.platform.claude_too_old", version="2.1.100", need=need))
        self.assertIsNone(windows.claude_problem(need))


if __name__ == "__main__":
    unittest.main()
