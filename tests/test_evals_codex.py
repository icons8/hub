"""Codex (experimental): evals/run.py on a hub on Codex — the temporary core is a hub on Codex with its own
CODEX_HOME inside the run folder, the session under test is CodexHost (a fake app-server here, no model). The sign-in is
the evals' own (<state>/codex-evals-home): a run gets a copy of its auth.json and gives the refreshed one back; the hub's
sign-in and the owner's ~/.codex are never read or touched. A case cut from a Codex thread resumes a copy of that thread;
a case cut from a Claude conversation is refused."""
import tests  # noqa: F401 — do not see the machine's settings (~/.config/hub-core), see tests/__init__.py
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import platforms
from unittest import mock

from core import journal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "evals"))
import codexrun  # noqa: E402
import run as evals  # noqa: E402

FAKE = os.path.join(ROOT, "tests", "fakes", "fake_codex.py")
THREAD = "01a1546c-0000-7000-8000-00000000abcd"


def line(type, payload):
    return {"timestamp": "2026-10-19T13:48:58.000Z", "type": type, "payload": payload}


def user(text):
    return line("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]})


def said(text):
    return line("response_item", {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]})


def turn():
    return line("event_msg", {"type": "task_started"})


def read(path):
    with open(path, "rb") as f:
        return f.read()


def rollout(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines))
    return path


THREAD_LINES = [line("session_meta", {"id": THREAD, "cwd": "/w"}),
                turn(), user("[hub:m1 from the owner] one"), said("ok"), line("event_msg", {"type": "task_complete"}),
                turn(), user("[hub:m2 from the owner] two"), said("ok"), line("event_msg", {"type": "task_complete"}),
                turn(), line("response_item", {"type": "message", "role": "developer", "content": []}),
                line("turn_context", {}), user("[hub:m3 from the owner] three"), said("did the wrong thing")]


class CutThread(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hubevcx-")
        self.addCleanup(platforms.current.rmtree, self.tmp)

    def test_the_thread_is_cut_before_the_turn_that_took_the_message(self):
        src = rollout(os.path.join(self.tmp, "src", f"rollout-2026-10-19T13-48-58-{THREAD}.jsonl"), THREAD_LINES)
        home = os.path.join(self.tmp, "home")
        self.assertEqual(codexrun.thread_of(src), THREAD)
        self.assertEqual(codexrun.cut_rollout(src, "[hub:m3", home), (THREAD, 9))
        # where Codex looks for a thread by its id: sessions/YYYY/MM/DD under CODEX_HOME
        dst = os.path.join(home, "sessions", "2026", "10", "19", os.path.basename(src))
        with open(dst, encoding="utf-8") as f:
            kept = [json.loads(x) for x in f]
        self.assertEqual(kept, THREAD_LINES[:9])   # the third turn goes whole: the run feeds its message again

    def test_a_message_inserted_mid_turn_is_cut_right_before_it(self):
        # turn/steer: the message came into a running turn — its beginning (what the model already did) stays
        lines = THREAD_LINES[:4] + [user("[hub:m5 from the owner] stop")]
        src = rollout(os.path.join(self.tmp, f"rollout-2026-10-19T13-48-58-{THREAD}.jsonl"), lines)
        self.assertEqual(codexrun.cut_rollout(src, "[hub:m5", os.path.join(self.tmp, "home"))[1], 4)

    def test_the_marker_is_whole_and_must_be_there(self):
        lines = THREAD_LINES[:4] + [turn(), user("[hub:m30 from the owner] thirty"), said("ok"),
                                    turn(), user("[hub:m31 from the owner] as in [hub:m3 above"), said("ok"),
                                    turn(), user("[hub:m3 from the owner] three")]
        src = rollout(os.path.join(self.tmp, f"rollout-2026-10-19T13-48-58-{THREAD}.jsonl"), lines)
        # not at m30, not at a message quoting the label
        self.assertEqual(codexrun.cut_rollout(src, "[hub:m3", os.path.join(self.tmp, "home"))[1], 10)
        with self.assertRaises(SystemExit) as e:   # a wrong marker: not the whole thread with the answer under test
            codexrun.cut_rollout(src, "[hub:m9", os.path.join(self.tmp, "home"))
        self.assertIn("[hub:m9", str(e.exception))

    def test_a_claude_conversation_is_not_a_codex_thread(self):
        src = os.path.join(self.tmp, "claude.jsonl")
        with open(src, "w") as f:
            f.write(json.dumps({"type": "user", "message": {"content": "[hub:m1 from the owner] one"}}) + "\n")
        self.assertIsNone(codexrun.thread_of(src))


class Hub(unittest.TestCase):
    """A hub's state folder with three sign-ins: the hub's, the evals' and the owner's personal ~/.codex (HOME)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hubevcx-")
        self.addCleanup(platforms.current.rmtree, self.tmp)
        self.state = os.path.join(self.tmp, "state")
        self.home = os.path.join(self.tmp, "home")
        self.hub_auth = self.write(os.path.join(self.state, "codex-home", "auth.json"), {"who": "hub"})
        self.own_auth = self.write(os.path.join(self.home, ".codex", "auth.json"), {"who": "owner"})
        self.evals_auth = os.path.join(self.state, "codex-evals-home", "auth.json")

    def write(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f)
        return path

    def untouched(self):   # portable: the bytes are compared too; the time adds a rewrite with the same bytes
        """Bytes and mtime of the hub's and the owner's sign-ins and the listing of ~/.codex."""
        return {p: (read(p), os.stat(p).st_mtime_ns) for p in (self.hub_auth, self.own_auth)} | {
            "personal": sorted(os.listdir(os.path.join(self.home, ".codex"))),
            "hub-home": sorted(os.listdir(os.path.join(self.state, "codex-home")))}


@unittest.skipIf(os.name == "nt", "file permissions and the lock are POSIX")
class EvalsSignIn(Hub):
    def test_no_evals_sign_in_is_a_refusal_with_what_to_do(self):
        with self.assertRaises(SystemExit) as e:
            codexrun.Auth(self.state).__enter__()
        self.assertIn("--codex-login", str(e.exception))

    def test_two_runs_do_not_rotate_one_copy(self):
        self.write(self.evals_auth, {"who": "evals"})
        with codexrun.Auth(self.state):
            with self.assertRaises(SystemExit) as e:
                codexrun.Auth(self.state).__enter__()
        self.assertIn("another eval run", str(e.exception))
        with codexrun.Auth(self.state):   # the first one is over — the next one goes
            pass

    def test_a_copy_goes_in_and_the_refreshed_one_comes_back(self):
        self.write(self.evals_auth, {"who": "evals"})
        before = self.untouched()
        run = os.path.join(self.tmp, "run")
        with codexrun.Auth(self.state) as auth:
            auth.give(run)
            got = os.path.join(run, "state", "codex-home", "auth.json")
            self.assertEqual(os.stat(got).st_mode & 0o077, 0)
            self.write(got, {"who": "evals", "rotated": 1})   # the run's Codex refreshed its token
            auth.take(run)
        self.assertFalse(os.path.exists(got))   # no token copy stays in the run folder
        with open(self.evals_auth) as f:
            self.assertEqual(json.load(f), {"who": "evals", "rotated": 1})
        self.assertEqual(os.stat(self.evals_auth).st_mode & 0o077, 0)
        self.assertEqual(self.untouched(), before)

    def test_a_broken_copy_does_not_overwrite_the_sign_in(self):
        self.write(self.evals_auth, {"who": "evals"})
        run = os.path.join(self.tmp, "run")
        with codexrun.Auth(self.state) as auth:
            auth.give(run)
            with open(os.path.join(run, "state", "codex-home", "auth.json"), "w") as f:
                f.write("{")
            auth.take(run)
        with open(self.evals_auth) as f:
            self.assertEqual(json.load(f), {"who": "evals"})


@unittest.skipIf(os.name == "nt", "the fake wrapper is sh")
class WholeRunOnFakeCodex(Hub):
    def setUp(self):
        super().setUp()
        self.write(self.evals_auth, {"who": "evals"})
        self.write(os.path.join(self.home, ".claude", ".credentials.json"), {"who": "claude"})
        self.argv = os.path.join(self.tmp, "argv")
        self.work = os.path.join(self.tmp, "work")
        env = {"EVAL_CODEX": f"{sys.executable} {FAKE}", "FAKE_TURN_S": "0.1", "FAKE_ROTATE_AUTH": "1",
               "FAKE_ARGV_DIR": self.argv, "HOME": self.home, "HUB_STATE_DIR": self.state}
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)

    def run_case(self, case, user_home=None):
        before = self.untouched()
        run = os.path.join(self.work, "run")
        with codexrun.Auth(self.state) as auth:
            ok, results, events = evals.run_one(case, ROOT, run, codex={"model": "gpt-x", "effort": "low"}, auth=auth,
                                                user_home=user_home)
        self.assertEqual(self.untouched(), before)   # the hub's sign-in and the owner's ~/.codex: not read, not written
        with open(self.evals_auth) as f:
            self.assertGreaterEqual(json.load(f).get("rotated", 0), 1)   # the refreshed token is the evals' again
        home = os.path.join(run, "state", "codex-home")
        self.assertFalse(os.path.exists(os.path.join(run, "claude-config", ".credentials.json")))   # no claude in the run
        self.assertFalse(os.path.exists(os.path.join(home, "auth.json")))
        self.assertFalse(os.path.exists(os.path.join(home, "sessions")))   # thread copies are not kept
        with open(os.path.join(run, "hub.json")) as f:
            cfg = json.load(f)
        self.assertEqual((cfg["host"], cfg["codex"]), ("codex", {"model": "gpt-x", "effort": "low"}))
        started = []
        for p in glob.glob(os.path.join(self.argv, "codex-*.json")):
            with open(p) as f:
                got = json.load(f)["env"]
            started.append((got.get("CODEX_HOME"), got.get("HUB_STATE_DIR")))
        self.assertTrue(started)
        # every codex of the run — with the run's own CODEX_HOME and state, the core's own calls too (never the live hub's)
        self.assertEqual(set(started), {(home, os.path.join(run, "state"))})
        return ok, results, events

    def test_an_invented_history_runs_on_the_session_under_test_on_codex(self):
        case = {"id": "own", "real": ["w"], "timeout": 60, "settle": 1,
                "events": [{"type": "session.requested", "entity": "w", "data": {"cwd": "$work/w", "session_id": "sid-w",
                                                                                  "channel": "cli", "owner": True}}],
                "config": {"hub": {"enabled": False}, "timezone": "UTC"},
                "inject": [{"type": "message.received", "data": {"to": "w", "text": "hello", "channel": "web",
                                                                  "owner": True}}],
                "checks": [{"id": "answered", "type": "said", "re": "ok: .*hello"}]}
        ok, results, events = self.run_case(case)
        self.assertTrue(ok, results)
        result = next(e for e in events if e["type"] == "turn.result" and e["entity"] == "w")
        self.assertEqual(result["data"].get("engine"), "codex")

    def test_a_case_cut_from_a_codex_thread_continues_that_thread(self):
        src = os.path.join(self.tmp, "source.jsonl")
        j = journal.Journal(src)
        j.append("session.requested", "hub", {"cwd": self.tmp, "session_id": "sid-hub", "kind": "hub"}, ts=1)
        j.append("session.started", "hub", {"session_id": "sid-hub"}, ts=2)
        j.append("session.requested", "a", {"cwd": self.tmp, "session_id": "sid-a"}, ts=3)
        j.append("session.started", "a", {"session_id": "sid-a"}, ts=4)
        j.append("turn.started", "hub", {"session_id": "sid-hub"}, ts=5)   # it has a conversation: the core resumes it
        j.append("turn.result", "hub", {"session_id": "sid-hub", "result": "ok"}, ts=6)
        j.append("message.received", None, {"to": "hub", "text": "three", "channel": "web", "owner": True}, ts=7)
        thread = rollout(os.path.join(self.tmp, "hubcx", "sessions", "2026", "10", "19",
                                      f"rollout-2026-10-19T13-48-58-{THREAD}.jsonl"), THREAD_LINES)
        user_home = os.path.join(self.tmp, "uh")
        self.write(os.path.join(user_home, ".config", "hub-core", "hub.json"), {"host": "codex", "hub": {"enabled": False}})
        case = {"id": "cut", "journal": src, "cut_seq": 7, "real": ["hub"], "timeout": 60, "settle": 1,
                "transcripts": {"hub": {"path": thread, "marker": "[hub:m3"}},
                "inject": [{"type": "message.received", "data": {"to": "hub", "text": "hello", "channel": "web",
                                                                  "owner": True}}],
                "checks": [{"id": "resumed", "type": "said", "re": r"hello.*\(remembers 2\)$"}]}
        ok, results, events = self.run_case(case, user_home=user_home)
        self.assertTrue(ok, (results, [e["data"] for e in events if e["type"] in ("session.said", "session.started")]))   # two earlier messages of the thread, not the cut one, not a new thread
        cwds = set()
        for p in glob.glob(os.path.join(self.argv, "codex-hub-*.json")):
            with open(p) as f:
                cwds.add(os.path.realpath(json.load(f)["cwd"]))   # macOS: the run's TMPDIR is a link (/var -> /private/var)
        self.assertEqual(cwds, {os.path.realpath(os.path.join(self.work, "run", "hub-home"))})   # the hub folder of the run, as with claude

    def test_a_cut_thread_the_core_did_not_resume_is_na(self):
        # no turn before the cut: the core starts the session fresh and the cut thread is not continued — not a verdict
        src = os.path.join(self.tmp, "source.jsonl")
        j = journal.Journal(src)
        j.append("session.requested", "hub", {"cwd": self.tmp, "session_id": "sid-hub", "kind": "hub"}, ts=1)
        j.append("message.received", None, {"to": "hub", "text": "three", "channel": "web", "owner": True}, ts=2)
        thread = rollout(os.path.join(self.tmp, f"rollout-2026-10-19T13-48-58-{THREAD}.jsonl"), THREAD_LINES)
        user_home = os.path.join(self.tmp, "uh")
        self.write(os.path.join(user_home, ".config", "hub-core", "hub.json"), {"host": "codex", "hub": {"enabled": False}})
        case = {"id": "fresh", "journal": src, "cut_seq": 2, "real": ["hub"], "timeout": 60, "settle": 1,
                "transcripts": {"hub": {"path": thread, "marker": "[hub:m3"}},
                "inject": [{"type": "message.received", "data": {"to": "hub", "text": "hello", "channel": "web",
                                                                  "owner": True}}],
                "checks": [{"id": "said", "type": "said", "re": "hello"}]}
        ok, results, _ = self.run_case(case, user_home=user_home)
        self.assertIsNone(ok, results)
        self.assertIn("the cut thread was not continued", results[0][2])

    def test_a_case_cut_from_a_claude_conversation_is_refused(self):
        src = os.path.join(self.tmp, "source.jsonl")
        journal.Journal(src).append("session.requested", "a", {"cwd": "/w", "session_id": "s"}, ts=1)
        claude = os.path.join(self.tmp, "claude.jsonl")
        with open(claude, "w") as f:
            f.write(json.dumps({"type": "user", "message": {"content": "[hub:m1 from the owner] one"}}) + "\n")
        case = {"id": "cl", "journal": src, "cut_seq": 2, "real": ["a"],
                "transcripts": {"a": {"path": claude, "marker": "[hub:m1", "cwd": "/w"}}}
        user_home = os.path.join(self.tmp, "uh")
        self.write(os.path.join(user_home, ".config", "hub-core", "hub.json"), {"host": "codex"})
        with self.assertRaises(SystemExit) as e:
            evals.prepare(case, ROOT, os.path.join(self.work, "run"), user_home=user_home, codex={"model": "gpt-x"})
        self.assertIn("taken from a Claude conversation", str(e.exception))
        self.assertIn("build the case again", str(e.exception))


class LostSignIn(unittest.TestCase):
    def judge(self, host):
        run = tempfile.mkdtemp(prefix="hubevcx-")
        self.addCleanup(platforms.current.rmtree, run)
        events = [{"seq": 3, "type": "message.received", "entity": None, "data": {"to": "w", "eval_inject": True}},
                  {"seq": 4, "type": "codex.auth_lost", "entity": None, "data": {"why": "401 Unauthorized"}},
                  {"seq": 5, "type": "turn.result", "entity": "w", "data": {"is_error": True, "auth_lost": True}}]
        with open(os.path.join(run, "events.jsonl"), "w") as f:
            f.write("".join(json.dumps(e) + "\n" for e in events))
        with open(os.path.join(run, "driver.json"), "w") as f:
            json.dump({"finished": True}, f)
        with open(os.path.join(run, "case.json"), "w") as f:
            json.dump({"host": host} if host else {}, f)
        case = {"real": ["w"], "checks": [{"id": "quiet", "type": "no_event", "match": {"type": "tools.granted"}}]}
        return evals.judge(case, run)

    def test_a_run_without_a_codex_sign_in_is_not_a_pass(self):
        # a turn refused with 401: "did not do" checks would pass on silence — the run is n/a
        ok, results, _ = self.judge("codex")
        self.assertIsNone(ok)
        self.assertIn("Codex", results[0][2])

    def test_a_claude_run_is_judged_by_its_checks(self):
        self.assertTrue(self.judge(None)[0])


@unittest.skipIf(os.name == "nt", "the process group is POSIX")
class StuckRun(Hub):
    def test_a_run_over_its_time_is_na_and_leaves_no_process_behind(self):
        # the driver hangs: its group (codex app-servers among it) ends before the sign-in goes back — a leftover could
        # refresh the one-time token after take()
        self.write(self.evals_auth, {"who": "evals"})
        here = os.path.join(self.tmp, "evals")
        os.makedirs(here)
        pid = os.path.join(self.tmp, "child.pid")
        with open(os.path.join(here, "driver.py"), "w") as f:
            f.write("import subprocess, sys, time\n"
                    f"c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
                    f"open({pid!r}, 'w').write(str(c.pid))\n"
                    "time.sleep(300)\n")
        case = {"id": "stuck", "real": ["w"], "timeout": -119, "inject": [], "checks": [],
                "events": [{"type": "session.requested", "entity": "w", "data": {"cwd": "$work/w", "session_id": "s"}}]}
        run = os.path.join(self.tmp, "run")
        with mock.patch.object(evals, "HERE", here), codexrun.Auth(self.state) as auth:
            ok, results, _ = evals.run_one(case, ROOT, run, codex={"model": "gpt-x"}, auth=auth)
        self.assertIsNone(ok)
        self.left_nothing(results, pid, run)

    @unittest.skipIf(os.name == "nt", "no process groups")
    def test_without_waitid_a_run_still_works_and_leaves_no_process(self):
        # macOS before Python 3.13 has no os.waitid: a run of a Claude case there crashed (AttributeError) — a plain wait,
        # the group still ends; a driver that finished — its result as before
        saved = os.waitid
        del os.waitid
        self.addCleanup(setattr, os, "waitid", saved)
        self.test_a_run_over_its_time_is_na_and_leaves_no_process_behind()
        here = os.path.join(self.tmp, "evals")
        with open(os.path.join(here, "driver.py"), "w") as f:
            f.write("pass\n")
        p = subprocess.Popen([sys.executable, os.path.join(here, "driver.py")], start_new_session=True)
        self.assertTrue(evals.exited(p, 30))
        evals.end_group(p)
        self.assertEqual(p.returncode, 0)

    def left_nothing(self, results, pid, run):
        self.assertIn("did not fit into", results[0][2])
        with open(pid) as f:
            child = int(f.read())
        self.assertTrue(gone(child), child)
        self.assertFalse(os.path.exists(os.path.join(run, "state", "codex-home")))

    def test_a_refused_case_leaves_no_thread_copy(self):
        self.write(self.evals_auth, {"who": "evals"})
        src = os.path.join(self.tmp, "source.jsonl")
        journal.Journal(src).append("session.requested", "a", {"cwd": "/w", "session_id": "s"}, ts=1)
        good = rollout(os.path.join(self.tmp, f"rollout-2026-10-19T13-48-58-{THREAD}.jsonl"), THREAD_LINES)
        user_home = os.path.join(self.tmp, "uh")
        self.write(os.path.join(user_home, ".config", "hub-core", "hub.json"), {"host": "codex"})
        case = {"id": "two", "journal": src, "cut_seq": 2, "real": ["a"],
                "transcripts": {"a": {"path": good, "marker": "[hub:m3"}, "b": {"path": good, "marker": "[hub:m9"}}}
        run = os.path.join(self.tmp, "run")
        with self.assertRaises(SystemExit), codexrun.Auth(self.state) as auth:
            evals.run_one(case, ROOT, run, codex={"model": "gpt-x"}, auth=auth, user_home=user_home)
        self.assertFalse(os.path.exists(os.path.join(run, "state", "codex-home")))   # the first thread's copy too


def gone(pid, wait=10.0):
    """The process ended (no such pid, or a zombie waiting for its new parent)."""
    import time
    end = time.time() + wait
    while time.time() < end:
        try:
            with open(f"/proc/{pid}/stat") as f:
                if f.read().rsplit(")", 1)[1].split()[0] == "Z":
                    return True
        except OSError:
            if not os.path.exists(f"/proc/{pid}"):
                return True
        if not os.path.isdir("/proc"):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
        time.sleep(0.1)
    return False


class HostClaude(unittest.TestCase):
    def test_host_claude_on_a_codex_hub_runs_a_claude_hub(self):
        work = tempfile.mkdtemp(prefix="hubevcx-")
        self.addCleanup(platforms.current.rmtree, work)
        src = os.path.join(work, "source.jsonl")
        journal.Journal(src).append("session.requested", "a", {"cwd": "/w", "session_id": "s"}, ts=1)
        uh = os.path.join(work, "uh")
        os.makedirs(os.path.join(uh, ".config", "hub-core"))
        with open(os.path.join(uh, ".config", "hub-core", "hub.json"), "w") as f:
            json.dump({"host": "codex", "codex": {"model": "gpt-x"}}, f)
        run = os.path.join(work, "run")
        evals.prepare({"id": "c", "journal": src, "cut_seq": 2, "real": ["a"]}, ROOT, run, user_home=uh)
        with open(os.path.join(run, "hub.json")) as f:
            self.assertNotIn("host", json.load(f))


@unittest.skipIf(os.name == "nt", "the fake wrapper is sh")
class LoginCommand(Hub):
    def test_the_evals_sign_in_is_a_device_code_into_its_own_folder(self):
        bin = os.path.join(self.tmp, "codex")
        with open(bin, "w") as f:
            f.write(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
        os.chmod(bin, 0o755)
        replies = self.write(os.path.join(self.tmp, "replies.json"), {
            "account/read": [{"account": None}, {"account": {"type": "chatgpt", "email": "a@example.com",
                                                              "planType": "prolite"}}],
            "account/login/start": {"result": {"type": "chatgptDeviceCode", "loginId": "l1", "userCode": "ABCD-1234",
                                               "verificationUrl": "https://auth.example.com/codex/device"},
                                    "notes": [{"method": "account/login/completed", "after_s": 0.2,
                                               "params": {"success": True, "loginId": "l1"}}]}})
        before = self.untouched()
        argv = os.path.join(self.tmp, "argv")
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_CODEX_BIN": bin, "FAKE_CODEX_REPLIES": replies,
               "FAKE_ARGV_DIR": argv, "HOME": self.home, "HUB_CODEX_LOGIN_WAIT": "5", "HUB_SESSION": "maps"}
        r = subprocess.run([sys.executable, os.path.join(ROOT, "evals", "run.py"), "--codex-login"], capture_output=True,
                           text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("https://auth.example.com/codex/device", r.stdout)
        self.assertIn("ABCD-1234", r.stdout)
        self.assertIn("a@example.com", r.stdout)
        self.assertEqual(self.untouched(), before)
        homes = set()
        for p in glob.glob(os.path.join(argv, "codex-*.json")):
            with open(p) as f:
                homes.add(json.load(f)["env"].get("CODEX_HOME"))
        self.assertEqual(homes, {os.path.join(self.state, "codex-evals-home")})
        self.assertFalse(os.path.exists(os.path.join(self.state, "spool")))   # not the hub's sign-in: no codex.login fact


if __name__ == "__main__":
    unittest.main()
