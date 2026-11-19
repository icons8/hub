"""An executor's model depends on the kind of work the session names: `--work normal|risky` (owner decision 2026-10-13:
sessions name the kind, the model comes from settings). The core takes the model and effort pair from Settings →
Executors; empty — no flag is passed, and claude goes to the models of its own endpoint (custom models via a custom
address). With a custom ANTHROPIC_BASE_URL the default is empty, otherwise normal — opus/medium, risky — opus/high. A
session without --work gets a refusal with the rule: without it the session never learns the rule (agent-dev run
2026-10-13)."""
import tests  # noqa: F401 — don't see the machine's settings
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

from core import appcode, i18n, journal, modelrule, spool
from modules.web import settings
from tests.fakes import cut

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")


def flat(text):
    return " ".join(text.split())


class Rule(unittest.TestCase):
    def setUp(self):
        env = unittest.mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("ANTHROPIC_BASE_URL", None)

    def test_defaults(self):
        self.assertEqual(modelrule.pin("normal", {}), {"model": "sonnet", "effort": "high"})
        self.assertEqual(modelrule.pin("risky", {}), {"model": "opus", "effort": "medium"})
        self.assertIsNone(modelrule.pin("cheap", {}))
        self.assertIsNone(modelrule.pin(None, {}))

    def test_the_pair_of_an_agent_wins_over_the_kind_and_ignores_what_is_unknown(self):
        cfg = {"executors": {"agents": {"maps": {"model": "opus", "effort": "high"}, "docs": {"model": "gpt-9"}}}}
        maps, docs, other = (modelrule.own(a, cfg) for a in ("maps", "docs", "other"))
        self.assertEqual(maps, {"model": "opus", "effort": "high"})
        self.assertEqual((docs, other), (None, None))                                      # nothing known / not set
        self.assertEqual(modelrule.pin("normal", cfg, maps), {"model": "opus", "effort": "high"})
        self.assertEqual(modelrule.pin(None, cfg, maps), {"model": "opus", "effort": "high"})
        self.assertEqual(modelrule.pin("normal", cfg, docs), {"model": "sonnet", "effort": "high"})   # unknown model: the kind
        self.assertEqual(modelrule.pin("normal", cfg, other), {"model": "sonnet", "effort": "high"})
        self.assertIsNone(modelrule.pin(None, cfg, other))
        self.assertEqual(modelrule.pin(None, cfg, other, {"model": "opus"}), {})            # the task names its pair: nothing to add
        self.assertEqual(modelrule.pin(None, cfg, maps, {"effort": "low"}), {"model": "opus", "effort": "high"})   # an own pair wins
        self.assertIsNone(modelrule.pin("risk", cfg, maps))                                # a typo is refused with a pair too

    def test_a_wrong_part_of_the_pair_of_an_agent_keeps_the_rest_from_the_kind(self):
        cfg = {"executors": {"agents": {"docs": {"model": "gpt-9", "effort": "max"}, "maps": {"effort": "nope", "model": "opus"}}}}
        docs, maps = modelrule.own("docs", cfg), modelrule.own("maps", cfg)
        self.assertEqual((docs, maps), ({"effort": "max"}, {"model": "opus"}))
        self.assertEqual(modelrule.pin("normal", cfg, docs), {"model": "sonnet", "effort": "max"})   # the model is not lost
        self.assertEqual(modelrule.pin("risky", cfg, maps), {"model": "opus", "effort": "medium"})
        self.assertEqual(modelrule.pin(None, cfg, docs), {"model": "sonnet", "effort": "max"})

    def test_agents_that_is_not_a_dictionary_is_nobodys_pair(self):
        for bad in (None, [], ["maps"], "maps", 5, True):
            for cfg in ({"executors": {"agents": bad}}, {"executors": bad}):
                self.assertIsNone(modelrule.own("maps", cfg), bad)
        self.assertEqual(modelrule.pin("normal", {"executors": {"agents": ["maps"]}}), {"model": "sonnet", "effort": "high"})

    def test_whose_executors_a_session_raises_is_the_lead_of_its_chain(self):
        # the key of the own pair: an executor's executor belongs to the agent (or the hub) on top of the chain; the
        # owner's executors are the hub's; a broken chain or a loop stops at the session itself
        ex = lambda up, **more: {"role": "executor", "raised_by": up, "state": "idle", **more}   # noqa: E731
        st = {"sessions": {"main-hub": {"kind": "hub", "role": "worker", "state": "idle"},
                           "maps": {"role": "agent", "state": "idle"}, "maps-x": ex("maps"),
                           "hub-x": ex("main-hub"), "hub-x-sub": ex("hub-x"), "mine": ex(None),
                           "lead": ex("main-hub", backlog="lead"), "lead-x": ex("lead"),
                           "self-x": ex("self-x"), "loop-a": ex("loop-b"), "loop-b": ex("loop-a"),
                           "orphan": ex("ghost"), "old": {"role": "agent", "state": "removed"}, "old-x": ex("old")}}
        of = lambda name: modelrule.agent_of(st, name)   # noqa: E731
        self.assertEqual([of(n) for n in ("maps", "maps-x", "main-hub", "hub-x", "hub-x-sub", "lead-x")],
                         ["maps", "maps", "main-hub", "main-hub", "main-hub", "lead"])
        self.assertEqual((of(None), of("mine")), ("main-hub", "main-hub"))              # the owner's: the hub's
        self.assertEqual([of(n) for n in ("self-x", "loop-a", "orphan", "old-x", "nobody")],
                         ["self-x", "loop-a", "orphan", "old-x", "nobody"])            # broken or looped: itself
        self.assertIsNone(modelrule.agent_of({"sessions": {}}, None))                    # no hub, the owner: nobody's

    def test_an_own_endpoint_gets_no_model_by_default(self):
        os.environ["ANTHROPIC_BASE_URL"] = "https://llm.example.com"
        self.assertEqual((modelrule.pin("normal", {}), modelrule.pin("risky", {})), ({}, {}))
        i18n.use("ru")   # the owner's terminal of a Russian hub
        self.addCleanup(i18n.use, "en")
        self.assertIn("(" + i18n.tr("ru", "core.modelrule.same") + ")", flat(modelrule.rule({})))
        self.assertNotEqual(i18n.tr("ru", "core.modelrule.same"), i18n.tr("en", "core.modelrule.same"))
        i18n.use("en")   # a session's process reads English
        self.assertIn("(" + i18n.tr("en", "core.modelrule.same") + ")", flat(modelrule.rule({})))
        # the owner set the pair themselves — that one is used
        self.assertEqual(modelrule.pin("risky", {"executors": {"risky": {"model": "sonnet", "effort": "high"}}}),
                         {"model": "sonnet", "effort": "high"})

    def test_empty_in_settings_means_do_not_pass_it(self):
        cfg = {"executors": {"normal": {"model": ""}, "risky": {"effort": "max", "model": "gpt-9"}}}
        self.assertEqual(modelrule.pin("normal", cfg), {"effort": "high"})
        self.assertEqual(modelrule.pin("risky", cfg), {"model": "opus", "effort": "max"})   # unknown — the default

    def test_the_rule_names_risk_first(self):
        i18n.use("en")   # a session's process reads English
        try:
            text = flat(modelrule.rule({}))
        finally:
            i18n.use("ru")
        self.assertTrue(text.startswith("--work risky — prod, money, clients, access"), text)
        self.assertIn("even an exact edit in them (opus, medium); --work normal — everything else (sonnet, high)", text)
        self.assertIn(i18n.t("core.modelrule.risky_when"), flat(modelrule.rule({})))

    def test_the_settings_tab_offers_both_pairs_and_empty(self):
        for work, (model, effort) in (("normal", ("sonnet", "high")), ("risky", ("opus", "medium"))):
            m, e = settings.KEYS[f"executors.{work}.model"], settings.KEYS[f"executors.{work}.effort"]
            self.assertEqual((m[3], e[3]), (model, effort))
            self.assertEqual(settings.validate(f"executors.{work}.model", ""), ("", None))
        self.assertIn("tested on Opus", settings.texts("en")[0]["executors_normal_model"][1])
        cfg = {}
        settings.put(cfg, "executors.risky.model", "")
        self.assertEqual(cfg, {"executors": {"risky": {"model": ""}}})                 # empty is stored, not deleted


class Review(unittest.TestCase):
    """Review 2026-10-13."""
    def setUp(self):
        env = unittest.mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL"):
            os.environ.pop(k, None)
        os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="tiers-claude-")

    def test_an_own_endpoint_in_the_claude_settings_counts(self):
        # a custom address usually lives in the env of Claude Code's settings (catalog.user_env carries it to sessions), not in
        # the hub's environment: the page showed Sonnet/Opus while sessions spawned executors without flags
        with open(os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "settings.json"), "w") as f:
            json.dump({"env": {"ANTHROPIC_BASE_URL": "https://llm.example.com"}}, f)
        self.assertEqual(modelrule.pin("normal", {}), {})
        shown = {r["key"]: r["value"] for r in settings.view({}, {}) if r["key"].split(".")[1:2] in (["normal"], ["risky"])}
        self.assertEqual(set(shown.values()), {""})

    def test_full_model_ids_are_kept(self):
        cfg = {"executors": {"risky": {"model": "claude-opus-4-5"}}}
        self.assertEqual(modelrule.pin("risky", cfg)["model"], "claude-opus-4-5")
        self.assertEqual(modelrule.pin("normal", {"executors": ["x"]}), {"model": "sonnet", "effort": "high"})

    def test_the_hints_the_hub_follows_name_the_kind_of_work(self):
        # the core and the hub's texts suggested `hub route dN executor --name …` and `hub task run …` without --work — a
        # session gets a refusal on them
        root = os.path.join(os.path.dirname(BIN), "..")
        for rel in ("core/reducer.py", "core/delegations.py", "bin/hub", "core/tools.py", "hub-home/CLAUDE.md",
                    "plugins/hub-coordinator/skills/fleet/SKILL.md", "plugins/hub-base/skills/hub/SKILL.md"):
            with open(os.path.join(root, rel)) as f:
                lines = f.read().split("\n")
            for n, ln in enumerate(lines):
                if ("route" in ln and "executor" in ln and "--name" in " ".join(lines[n:n + 2])) or "hub task run <" in ln:
                    self.assertIn("--work", " ".join(lines[n:n + 2]), f"{rel}:{n + 1}")

    def test_a_full_model_id_shows_and_can_be_chosen_again(self):
        cfg = {"executors": {"risky": {"model": "claude-opus-4-5"}}}
        row = next(r for r in settings.view(cfg, {}) if r["key"] == "executors.risky.model")
        self.assertEqual(row["value"], "claude-opus-4-5")
        self.assertIn("claude-opus-4-5", row["choices"])
        self.assertEqual(settings.validate("executors.risky.model", "claude-opus-4-5"), ("claude-opus-4-5", None))
        self.assertIsNotNone(settings.validate("executors.risky.model", "gpt-9")[1])


class Cli(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="tiers-")
        self.cfg = os.path.join(self.state, "hub.json")
        with open(self.cfg, "w") as f:
            json.dump({"executors": {"risky": {"effort": "max"}}}, f)

    def hub(self, *args, me="devops", **env_extra):
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_CONFIG": self.cfg, "HUB_CLI_WAIT": "0"}
        for k in ("HUB_SESSION", "ANTHROPIC_BASE_URL"):
            env.pop(k, None)
        env.update(env_extra)
        if me:
            env["HUB_SESSION"] = me
        r = subprocess.run([sys.executable, BIN, *args], env=env, capture_output=True, text=True)
        return r, [ev for _, ev in spool.take(os.path.join(self.state, "spool"))]

    def test_a_session_names_the_kind_of_work(self):
        r, evs = self.hub("task", "run", "x", "delete old buckets", "--work", "risky")
        data = evs[-1]["data"]
        self.assertEqual((r.returncode, data["model"], data["effort"], data["task"]),
                         (0, "opus", "max", "delete old buckets"))
        r, evs = self.hub("route", "d7", "executor", "--name", "x", "--work", "normal")
        self.assertEqual((evs[-1]["data"]["model"], evs[-1]["data"]["effort"]), ("sonnet", "high"))
        r, evs = self.hub("task", "run", "y", "summary", "--work", "normal", ANTHROPIC_BASE_URL="https://llm.example.com")
        self.assertNotIn("model", evs[-1]["data"])                                     # a custom endpoint — no --model

    def test_an_agent_with_its_own_pair_gets_it_whatever_the_kind(self):
        # executors.agents.<agent>: the pair of that agent's executors, with or without --work
        with open(self.cfg, "w") as f:
            json.dump({"executors": {"agents": {"maps": {"model": "opus", "effort": "high"}}}}, f)
        for args in (["task", "run", "x", "MR"], ["task", "run", "x", "MR", "--work", "normal"]):
            r, evs = self.hub(*args, me="maps")
            self.assertEqual((r.returncode, evs[-1]["data"]["model"], evs[-1]["data"]["effort"]), (0, "opus", "high"))
        self.assertNotIn("economy", evs[-1]["data"])                                    # the limit saving does not lower it
        r, evs = self.hub("task", "run", "x", "summary", "--work", "normal")              # another agent: by the kind
        self.assertEqual((evs[-1]["data"]["model"], evs[-1]["data"]["effort"]), ("sonnet", "high"))
        self.assertIn("economy", evs[-1]["data"])
        sent = len(evs)
        r, evs = self.hub("task", "run", "x", "MR", "--work", "risk", me="maps")         # a typo is still refused
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(len(evs), sent)                                                # nothing reached the core

    def task_of(self, agent, **pair):
        """A task dN of the agent with the pair it already has (hub task new … --work …); returns its id."""
        j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        for n in ("hub", agent):
            j.append("session.requested", n, {"cwd": "/w", "session_id": f"s-{n}", "channel": "cli", "owner": True})
        j.append("delegation.created", None, {"session": agent, "context": "x", "for_session": "hub", **pair,
                                              "channel": "session", "owner": False, "sender": "hub"})
        return list(journal.load_state(self.state)["delegations"])[-1]

    def test_the_pair_of_the_agent_of_the_task_outranks_the_pair_the_task_already_has(self):
        # `hub route dN executor` is made by the hub (or the owner), not by the agent: the key is the agent of the task
        with open(self.cfg, "w") as f:
            json.dump({"executors": {"agents": {"maps": {"model": "opus", "effort": "high"}, "hub": {"model": "haiku"}}}}, f)
        mine = self.task_of("maps", model="sonnet", effort="high")
        other = self.task_of("devops", model="sonnet", effort="high")
        for who in ("hub", None):                                                          # the hub and the owner's terminal
            r, evs = self.hub("route", mine, "executor", "--name", "x", me=who)
            self.assertEqual((r.returncode, evs[-1]["data"]["model"], evs[-1]["data"]["effort"]), (0, "opus", "high"), who)
            r, evs = self.hub("route", other, "executor", "--name", "x", me=who)             # an agent without its own pair
            self.assertEqual((r.returncode, "model" in evs[-1]["data"]), (0, False), who)   # the task's pair stays
        self.assertNotIn("economy", self.hub("route", mine, "executor", "--name", "x", "--work", "normal")[1][-1]["data"])
        # routed by the message mN that carries the task (the core takes both): the same agent, the same pair
        msg = journal.load_state(self.state)["delegations"][mine]["message"]
        r, evs = self.hub("route", msg, "executor", "--name", "x", "--work", "normal", me="hub")
        self.assertEqual((r.returncode, evs[-1]["data"]["model"], evs[-1]["data"]["effort"]), (0, "opus", "high"))

    def test_a_partial_pair_of_an_agent_keeps_the_rest_from_the_pair_of_the_task(self):
        # a risky task (opus) routed with no --work to an agent whose pair sets only the effort: the model stays opus, not normal's sonnet
        with open(self.cfg, "w") as f:
            json.dump({"executors": {"agents": {"maps": {"effort": "max"}}}}, f)
        did = self.task_of("maps", model="opus", effort="medium")
        r, evs = self.hub("route", did, "executor", "--name", "x", me="hub")
        self.assertEqual((r.returncode, evs[-1]["data"]["model"], evs[-1]["data"]["effort"]), (0, "opus", "max"))
        r, evs = self.hub("route", did, "executor", "--name", "x", "--work", "normal", me="hub")   # a kind named — it is the base
        self.assertEqual((r.returncode, evs[-1]["data"]["model"], evs[-1]["data"]["effort"]), (0, "sonnet", "max"))

    def test_the_limit_saving_lowers_only_what_the_pair_of_the_agent_leaves_open(self):
        with open(self.cfg, "w") as f:
            json.dump({"executors": {"normal": {"model": "opus", "effort": "high"},
                                     "agents": {"maps": {"effort": "max"}, "docs": {"model": "opus"},
                                                "both": {"model": "opus", "effort": "low"}}}}, f)
        run = lambda me: self.hub("task", "run", "x", "MR", "--work", "normal", me=me)[1][-1]["data"]   # noqa: E731
        data = run("maps")                                                      # the effort is pinned: only the model may go down
        self.assertEqual((data["model"], data["effort"], data["economy"]), ("opus", "max", {"model": "sonnet"}))
        data = run("docs")                                                      # the model is pinned: only the effort
        self.assertEqual((data["model"], data["effort"], data["economy"]), ("opus", "high", {"effort": "medium"}))
        self.assertNotIn("economy", run("both"))                                # the whole pair is pinned: nothing is lowered
        self.assertEqual(run("devops")["economy"], {"model": "sonnet", "effort": "medium"})   # no own pair: as before

    def test_a_routed_partial_pair_keeps_the_saving_of_the_kind_the_task_has(self):
        # `hub route dN executor` names no --work: the kind (and with it the saving) is the one the task already has
        with open(self.cfg, "w") as f:
            json.dump({"executors": {"normal": {"model": "opus", "effort": "high"}, "agents": {"maps": {"effort": "max"}}}}, f)
        did = self.task_of("maps", model="opus", effort="high", work="normal", economy={"model": "sonnet", "effort": "medium"})
        data = self.hub("route", did, "executor", "--name", "x", me="hub")[1][-1]["data"]
        self.assertEqual((data["model"], data["effort"], data.get("work"), data.get("economy")),
                         ("opus", "max", "normal", {"model": "sonnet"}))              # the effort is pinned: only the model may go down
        risky = self.task_of("maps", model="opus", effort="max", work="risky")
        self.assertNotIn("economy", self.hub("route", risky, "executor", "--name", "x", me="hub")[1][-1]["data"])   # risky is never lowered

    def test_a_routed_task_with_an_explicit_pair_is_not_lowered_by_the_saving(self):
        # the saving is on a task only if it was raised in economy mode: a task with a pair of its own (no economy) keeps it
        did = self.task_of("maps", model="opus", effort="high", work="normal")
        data = self.hub("route", did, "executor", "--name", "x", me="hub")[1][-1]["data"]
        self.assertNotIn("economy", data)
        self.assertEqual((data.get("model"), data.get("effort")), (None, None))                  # the task's own pair stays
        data = self.hub("route", did, "executor", "--name", "x", "--work", "normal", me="hub")[1][-1]["data"]
        self.assertEqual(data.get("economy"), {"model": "sonnet", "effort": "medium"})           # --work asks for the kind: as before
        # by the message mN that carries the task, with no --work: the task's pair, not a refusal for «no --work»
        msg = journal.load_state(self.state)["delegations"][did]["message"]
        r, evs = self.hub("route", msg, "executor", "--name", "x", me="hub")
        self.assertEqual((r.returncode, evs[-1]["data"].get("model"), evs[-1]["data"].get("work")), (0, None, "normal"), r.stderr)

    def test_a_routed_task_of_a_kind_with_an_empty_pair_is_not_refused_for_no_work(self):
        # executors.normal is empty: the task has the kind but no pair; `hub route dN executor` takes the kind from the task
        with open(self.cfg, "w") as f:
            json.dump({"executors": {"normal": {"model": "", "effort": ""}}}, f)
        did = self.task_of("maps", work="normal")
        r, evs = self.hub("route", did, "executor", "--name", "x", me="hub")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((evs[-1]["data"].get("work"), "model" in evs[-1]["data"]), ("normal", False))
        r, evs = self.hub("route", self.task_of("maps"), "executor", "--name", "x", me="hub")   # no kind on the task: refused
        self.assertNotEqual(r.returncode, 0)

    def test_an_executor_raised_down_the_chain_or_by_the_owner_gets_the_pair_of_the_lead(self):
        # hub task run: the own pair is the one of the agent whose executor it is, not of the session typing the
        # command — the hub's executor raising a subtask, the owner from the terminal (the hub's executors)
        with open(self.cfg, "w") as f:
            json.dump({"executors": {"agents": {"main-hub": {"model": "haiku", "effort": "low"},
                                                "maps": {"model": "opus", "effort": "max"}}}}, f)
        j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        j.append("session.requested", "main-hub", {"cwd": "/h", "session_id": "s-h", "kind": "hub", "channel": "cli",
                                                   "owner": True})
        j.append("session.requested", "maps", {"cwd": "/w", "session_id": "s-m", "role": "agent", "channel": "cli",
                                               "owner": True})
        for name, up in (("hub-x", "main-hub"), ("hub-x-sub", "hub-x"), ("maps-x", "maps")):
            j.append("session.requested", name, {"cwd": "/w", "session_id": f"s-{name}", "executor": True,
                                                 "raised_by": up, "channel": "cli", "owner": True})
        pair = lambda me: (lambda d: (d.get("model"), d.get("effort")))(   # noqa: E731
            self.hub("task", "run", "x", "MR", "--work", "normal", me=me)[1][-1]["data"])
        self.assertEqual([pair(me) for me in ("main-hub", "hub-x", "hub-x-sub", None)], [("haiku", "low")] * 4)
        self.assertEqual((pair("maps"), pair("maps-x")), (("opus", "max"), ("opus", "max")))
        self.assertEqual(pair("devops"), ("sonnet", "high"))                             # no own pair: by the kind

    def test_without_it_the_session_is_refused_with_the_rule(self):
        for args in (["task", "run", "x", "summary"], ["route", "d7", "executor", "--name", "x"],
                     ["task", "run", "x", "summary", "--work", "cheap"], ["task", "run", "x", "y", "--model", "opus"]):
            r, evs = self.hub(*args)
            self.assertNotEqual(r.returncode, 0, args)
            self.assertEqual(evs, [], args)                                             # nothing reached the core
            self.assertIn("--work risky — prod", flat(r.stderr), args)
        r, evs = self.hub("task", "run", "x", "summary", me=None)                        # the owner from a terminal — as before
        self.assertEqual((r.returncode, evs[-1]["type"]), (0, "task.run_requested"))
        self.assertNotIn("model", evs[-1]["data"])

    def test_help_survives_a_broken_config(self):
        with open(self.cfg, "w") as f:
            f.write('{"executors": ')
        r = self.hub("task", "run", "--help", me=None)[0]
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("hub task run <", r.stdout)
        r = self.hub("task", "run", "x", "summary")[0]                                   # a refusal — with the rule, without a traceback
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("--work risky — prod", flat(r.stderr))

    def test_help_puts_the_rule_right_under_the_flags(self):
        out = self.hub("task", "run", "--help", me=None)[0].stdout.split("\n")
        i = next(n for n, ln in enumerate(out) if ln.startswith("hub task run <"))
        self.assertIn("--work normal|risky", out[i])
        self.assertIn("--work risky — " + i18n.t("core.modelrule.risky_when").split(",")[0],
                      flat(" ".join(out[i + 1:i + 4])))         # sessions read it with grep -A6
        root = os.path.join(os.path.dirname(BIN), "..")
        with open(os.path.join(root, "CHANGES.md")) as f:
            note = next((ln for ln in f if ln.startswith("- `hub task run` ") and "`hub route … executor`" in ln[:60]), None)
        if note is not None or not appcode.OPEN_CHANGES_FROM:   # the open tree's own history starts later (its cut)
            self.assertIn("--work risky", note or "")
        with open(os.path.join(root, "hub-home", "CLAUDE.md")) as f:
            self.assertIn("--work risky", f.read())


@cut.needs("gateway")
class Gateway(unittest.TestCase):
    """The accounts gateway points sessions' ANTHROPIC_BASE_URL at itself (modules/gateway): that is not the person's own
    endpoint — normal executors went to the session's model (opus) instead of sonnet/high. A custom address behind the
    gateway is still custom."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="tiers-gw-")
        self.addCleanup(self.tmp.cleanup)
        self.state, self.claude = (os.path.join(self.tmp.name, d) for d in ("state", "claude"))
        os.makedirs(self.claude)
        os.makedirs(self.state)
        self.cfg = os.path.join(self.state, "hub.json")
        with open(self.cfg, "w") as f:
            json.dump({}, f)

    def session(self, own=None, **extra):
        """A session's environment on the gateway — as gateway.session_env sets it (own — the person's own address before the gateway)."""
        from modules.gateway import gateway as gw
        env = {"HUB_STATE_DIR": self.state, "CLAUDE_CONFIG_DIR": self.claude, **({"ANTHROPIC_BASE_URL": own} if own else {})}
        return {**gw.session_env(env, "http://127.0.0.1:4321", "s", stub=True), **extra}

    def pins(self, env):
        with unittest.mock.patch.dict(os.environ, env):
            for k in ("ANTHROPIC_MODEL", "ANTHROPIC_CUSTOM_HEADERS", "HUB_GATEWAY_UPSTREAM", "ANTHROPIC_BASE_URL"):
                if k not in env:
                    os.environ.pop(k, None)
            return modelrule.pin("normal", {}), modelrule.pin("risky", {})

    def test_the_gateway_to_the_api_is_not_an_own_endpoint(self):
        self.assertEqual(self.pins(self.session()), ({"model": "sonnet", "effort": "high"},
                                                     {"model": "opus", "effort": "medium"}))

    def test_an_own_endpoint_behind_the_gateway_still_counts(self):
        self.assertEqual(self.pins(self.session(own="https://llm.example.com")), ({}, {}))   # the gateway sends to its own address
        self.assertEqual(self.pins(self.session(ANTHROPIC_MODEL="my-model")), ({}, {}))     # a custom model
        env = self.session()
        del env["HUB_GATEWAY_UPSTREAM"]                                                     # a session of an older core
        self.assertEqual(self.pins(env), ({}, {}))
        with open(os.path.join(self.claude, "settings.json"), "w") as f:                  # a custom address in claude's settings
            json.dump({"env": {"ANTHROPIC_BASE_URL": "https://llm.example.com"}}, f)
        self.assertEqual(self.pins(self.session(own="https://llm.example.com")), ({}, {}))   # as at spawn: user_env
        self.assertEqual(self.pins(self.session()), ({}, {}))                               # and overrode the gateway address

    def test_a_session_on_the_gateway_starts_a_normal_executor_on_sonnet(self):
        env = {**os.environ, **self.session(), "HUB_CONFIG": self.cfg, "HUB_CLI_WAIT": "0", "HUB_SESSION": "devops"}
        env.pop("ANTHROPIC_MODEL", None)
        subprocess.run([sys.executable, BIN, "task", "run", "x", "summary", "--work", "normal"], env=env,
                       capture_output=True, text=True, check=True)
        data = [ev for _, ev in spool.take(os.path.join(self.state, "spool"))][-1]["data"]
        self.assertEqual((data["model"], data["effort"]), ("sonnet", "high"))


if __name__ == "__main__":
    unittest.main()
