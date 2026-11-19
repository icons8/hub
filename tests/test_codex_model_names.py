"""Codex model names in `hub session model` and `hub task run --model` on a hub entirely on Codex (hub.json "host":
"codex"). The host takes only `codex/<id>`, while the help and the refusals named only Claude's models: a session that
typed the bare id of a listed model was refused with "one of opus, sonnet…", and an executor raised with `--work risky`
got the hub's default workhorse model instead of the deep one, as opus is on Claude. Now a bare id of a listed model
becomes `codex/<id>`, an unknown one is refused with the form and real ids of the hub's Codex, and `--work risky` takes
the frontier family of the list (Astra). A Claude hub behaves as before."""
import tests  # noqa: F401 — do not see the machine's settings (~/.config/hub-core), see tests/__init__.py
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

import platforms
from core import catalog, delegations, i18n, journal, modelrule, reducer, spool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(ROOT, "bin", "hub")
OWN = {"channel": "cli", "owner": True}
# model/list as core/codexcli keeps it: Codex's order, its default first (the workhorse), the frontier one after it
LISTED = [{"id": "gpt-9-sol", "title": "GPT-9-Sol", "efforts": ["low", "medium", "high"], "effort": "low", "default": True},
          {"id": "gpt-9-astra", "title": "GPT-9-Astra", "efforts": ["low", "medium", "high"], "effort": "low",
           "default": False},
          {"id": "gpt-9-luna", "title": "GPT-9-Luna", "efforts": ["low", "medium"], "effort": "low", "default": False},
          {"id": "gpt-8-astra", "title": "GPT-8-Astra", "efforts": ["low", "high"], "effort": "low", "default": False}]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cx-names-")
        self.addCleanup(platforms.current.rmtree, self.tmp)
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state)
        j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        j.append("session.requested", "maps", {"cwd": self.tmp, "session_id": "s-maps", **OWN})
        j.append("session.started", "maps", {"session_id": "s-maps"})
        self.cfg = os.path.join(self.tmp, "hub.json")
        self.config({"host": "codex"})
        self.env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_CONFIG": self.cfg, "HUB_CLI_WAIT": "0",
                    "HUB_LANG": "en"}
        for k in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE", "HUB_WEB_PORT", "HUB_CODEX"):
            self.env.pop(k, None)

    def config(self, cfg):
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump(cfg, f)

    def listed(self, models=LISTED, age=0):
        with open(os.path.join(self.state, "codex-models.json"), "w", encoding="utf-8") as f:
            json.dump({"ts": time.time() - age, "models": models}, f)

    def hub(self, *args):
        return subprocess.run([sys.executable, HUB, *args], env=self.env, capture_output=True, text=True,
                              encoding="utf-8", timeout=30, stdin=subprocess.DEVNULL)

    def spooled(self):
        out = []
        for path, ev in spool.take(os.path.join(self.state, "spool")):
            os.unlink(path)
            out.append(ev)
        return out

    def refused(self, r):
        """Refused in the command itself: nonzero code, no traceback, nothing in the inbox; the line names the form and
        real ids of the hub's Codex, no Claude names."""
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertNotIn("Traceback", out)
        self.assertEqual(self.spooled(), [])
        self.assertIn("codex/<", out)
        self.assertIn("codex/gpt-9-sol", out)
        self.assertIn("codex/gpt-9-astra", out)
        self.assertNotIn("sonnet", out)
        return out


class SessionModel(Base):
    def test_a_bare_listed_id_becomes_codex_form(self):
        self.listed()
        r = self.hub("session", "model", "maps", "gpt-9-astra")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        (ev,) = self.spooled()
        self.assertEqual((ev["type"], ev["data"]["model"]), ("session.model_set", "codex/gpt-9-astra"))

    def test_the_codex_form_and_default_go_as_typed(self):
        self.listed()
        for typed in ("codex/gpt-9-luna", "default"):
            self.assertEqual(self.hub("session", "model", "maps", typed).returncode, 0, typed)
            (ev,) = self.spooled()
            self.assertEqual(ev["data"]["model"], typed)

    def test_an_unknown_id_is_refused_with_real_ids(self):
        self.listed()
        for typed in ("gpt-nope", "opus", "claude-opus-5-5"):
            out = self.refused(self.hub("session", "model", "maps", typed))
            self.assertIn(typed, out)

    def test_the_codex_form_checked_against_a_fresh_list(self):
        """A typo in the explicit form is refused while the list is fresh; a list older than a codex update may make
        it (codexcli.MODELS_MAX_AGE) lets an unlisted codex/<id> through — a new model."""
        self.listed()
        self.refused(self.hub("session", "model", "maps", "codex/gpt-9-astar"))
        self.listed(age=7 * 3600)
        for typed in ("codex/gpt-10-astra", "gpt-10-astra"):   # the bare id of a new model gets the form too
            self.assertEqual(self.hub("session", "model", "maps", typed).returncode, 0, typed)
            self.assertEqual(self.spooled()[0]["data"]["model"], "codex/gpt-10-astra", typed)
        self.refused(self.hub("session", "model", "maps", "opus"))   # a Claude name still never

    def test_a_malformed_codex_form_and_a_claude_name_without_the_list(self):
        """Nothing to compare with yet: the form itself is still checked, and on a hub entirely on Codex a Claude name
        never applies (the host would silently run the hub's model)."""
        for typed in ("codex/", "codex/a b", "codex/-x", "opus", "claude-opus-5-5"):
            r = self.hub("session", "model", "maps", typed)
            self.assertNotEqual(r.returncode, 0, typed)
            self.assertIn("codex/<id>", r.stdout + r.stderr, typed)
            self.assertEqual(self.spooled(), [], typed)

    def test_claude_names_next_to_codex(self):
        """HUB_CODEX=1: Claude's sessions next to Codex's — a Claude name goes as typed, a bare listed id gets the form."""
        self.listed()
        self.config({})
        self.env["HUB_CODEX"] = "1"
        self.assertEqual(self.hub("session", "model", "maps", "opus").returncode, 0)
        self.assertEqual(self.spooled()[0]["data"]["model"], "opus")
        self.assertEqual(self.hub("session", "model", "maps", "gpt-9-astra").returncode, 0)
        self.assertEqual(self.spooled()[0]["data"]["model"], "codex/gpt-9-astra")
        out = self.hub("session", "model", "maps", "gpt-nope")
        self.assertIn("(Claude: opus, sonnet", out.stdout + out.stderr)   # both engines' names there, apart

    def test_claude_names_next_to_codex_by_the_cores_mark(self):
        """HUB_CODEX=1 only in the service's environment: the terminal knows the Codex host by the core's codex.host."""
        self.listed()
        self.config({})
        journal.Journal(os.path.join(self.state, "journal.jsonl")).append(
            "codex.host", None, {"on": True, "channel": "core", "owner": False})
        self.assertEqual(self.hub("session", "model", "maps", "gpt-9-astra").returncode, 0)
        self.assertEqual(self.spooled()[0]["data"]["model"], "codex/gpt-9-astra")
        self.assertEqual(self.hub("session", "model", "maps", "opus").returncode, 0)
        self.assertEqual(self.spooled()[0]["data"]["model"], "opus")

    def test_the_hubs_own_model_counts_as_listed(self):
        """hub.json codex.model may name a model the list does not show yet (read before a codex update)."""
        self.listed()
        self.config({"host": "codex", "codex": {"model": "gpt-10-sol"}})
        self.assertEqual(self.hub("session", "model", "maps", "gpt-10-sol").returncode, 0)
        self.assertEqual(self.spooled()[0]["data"]["model"], "codex/gpt-10-sol")

    def test_before_the_list_is_read_the_core_decides(self):
        """No model/list yet: nothing to compare with — the fact goes as typed, and the core's refusal names the form."""
        r = self.hub("session", "model", "maps", "gpt-9-astra")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.spooled()[0]["data"]["model"], "gpt-9-astra")

    def test_a_claude_hub_as_before(self):
        self.listed()
        self.config({})
        for typed in ("gpt-9-astra", "opus"):
            self.assertEqual(self.hub("session", "model", "maps", typed).returncode, 0)
            self.assertEqual(self.spooled()[0]["data"]["model"], typed)


class TaskRunModel(Base):
    def run_with(self, *flags):
        return self.hub("task", "run", "tiles-dark", "add a dark tile theme", "--repo", self.tmp, *flags)

    def test_a_bare_listed_id_becomes_codex_form(self):
        self.listed()
        r = self.run_with("--work", "risky", "--model", "gpt-9-luna")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        (ev,) = self.spooled()
        self.assertEqual((ev["type"], ev["data"]["model"]), ("task.run_requested", "codex/gpt-9-luna"))

    def test_an_unknown_id_is_refused_with_real_ids(self):
        self.listed()
        self.refused(self.run_with("--work", "risky", "--model", "gpt-nope"))

    def test_risky_takes_the_frontier_model_of_the_list(self):
        """On Claude risky is opus (the deep family), not the session's sonnet; on Codex — the first listed Astra, not
        the default workhorse Sol."""
        self.listed()
        r = self.run_with("--work", "risky")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        data = self.spooled()[0]["data"]
        self.assertEqual((data.get("model"), data.get("effort")), ("codex/gpt-9-astra", "high"))
        self.assertEqual(self.run_with("--work", "normal").returncode, 0)
        data = self.spooled()[0]["data"]
        self.assertEqual((data.get("model"), data.get("effort")), (None, "medium"))   # the hub's own model


class Pairs(unittest.TestCase):
    CODEX = {"host": "codex"}

    def test_risky_on_codex(self):
        ids = [m["id"] for m in LISTED]
        self.assertEqual(modelrule.DEFAULTS(self.CODEX, ids)["risky"], ("codex/gpt-9-astra", "high"))
        self.assertEqual(modelrule.DEFAULTS(self.CODEX, ids)["normal"], ("", "medium"))
        self.assertEqual(modelrule.DEFAULTS(self.CODEX, ["gpt-9-sol"])["risky"], ("", "high"))   # no Astra listed
        self.assertEqual(modelrule.DEFAULTS(self.CODEX, [])["risky"], ("", "high"))   # not read yet
        self.assertEqual(modelrule.deep(["gpt-9-sol", "gpt-9-astra-mini", "gpt-9-astra"]), "gpt-9-astra")
        self.assertEqual(modelrule.deep(["gpt-9-sol", "gpt-9-astra-codex"]), "gpt-9-astra-codex")
        cfg = {**self.CODEX, "executors": {"risky": {"model": "codex/gpt-9-luna"}}}
        self.assertEqual(modelrule.pair("risky", cfg), ("codex/gpt-9-luna", "high"))   # the settings win

    def test_a_claude_hub_as_before(self):
        self.assertEqual(modelrule.DEFAULTS({}, ["gpt-9-astra"])["risky"], ("opus", "medium"))


class CoreRefusals(unittest.TestCase):
    """What the core itself says (a fact without the command's check: an old command, a page, a module): on a hub with
    the Codex host the refusal names the codex/<id> form; on a Claude hub — as before."""

    def setUp(self):
        self.st, self.seq = reducer.empty(), 0

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 100 + self.seq, "type": type,
                                                   "entity": entity, "data": data})
        return why

    def test_session_model(self):
        self.ev("session.requested", "maps", cwd="/m", session_id="s-m", **OWN)
        self.ev("session.started", "maps")
        why = self.ev("session.model_set", "maps", model="gpt-9-astra", **OWN)
        self.assertIn("sonnet", why)                                   # a Claude hub: as before
        self.ev("codex.host", None, on=True, whole=True, channel="core", owner=False)
        self.assertTrue(catalog.codex_state(self.st))
        why = self.ev("session.model_set", "maps", model="gpt-9-astra", **OWN)
        self.assertIn("codex/<", str(why))
        self.assertIsNone(self.ev("session.model_set", "maps", model="codex/gpt-9-astra", **OWN))

    def test_task_pin(self):
        with self.assertRaises(reducer.Rejected) as got:
            delegations.pinned({"model": "gpt-9-astra"}, codex=True)
        self.assertIn("codex/<", str(got.exception))
        with self.assertRaises(reducer.Rejected) as got:
            delegations.pinned({"model": "gpt-9-astra"})
        self.assertIn("claude-", str(got.exception))
        self.assertEqual(delegations.pinned({"model": "codex/gpt-9-astra"}, codex=True), {"model": "codex/gpt-9-astra"})


class Texts(unittest.TestCase):
    def test_every_new_key_is_in_english_and_russian(self):
        for key in ("cli.model.codex_unknown", "core.reducer.model_bad_codex", "core.delegations.bad_model_codex"):
            for lang in ("en", "ru"):
                self.assertNotEqual(i18n.tr(lang, key, model="x", examples="y"), key, (lang, key))


if __name__ == "__main__":
    unittest.main()
