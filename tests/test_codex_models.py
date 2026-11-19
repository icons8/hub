"""Codex (experimental): models on a hub entirely on Codex (hub.json "host": "codex") — the executor
pairs of `--work normal|risky` differ, the page lists the models of the hub's Codex (`model/list`, no model call, no
sign-in), "Settings" accept `codex/…`, and a fresh hub knows `codex/…` before the daemon's first tick. A Claude hub
behaves as on main."""
import tests  # noqa: F401 — do not see the machine's settings (~/.config/hub-core), see tests/__init__.py
import os
import sys
import tempfile
import unittest

import platforms

from hosts.codex import CodexHost

HERE = os.path.dirname(__file__)
FAKE_CODEX = os.path.join(HERE, "fakes", "fake_codex.py")


class Temp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hubcxe-")
        self.addCleanup(platforms.current.rmtree, self.tmp)
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state)


class Pick(Temp):
    """The host on a hub entirely on Codex: a session's own effort holds also on the hub's model (the page's effort,
    `hub session effort`, the effort of an executor pair), and without one the hub's effort is used."""

    def host(self):
        return CodexHost(self.state, codex=(sys.executable, FAKE_CODEX), all_sessions=True, default_model="gpt-def",
                         default_effort="low")

    def test_own_effort_on_the_hubs_model(self):
        h = self.host()
        self.assertEqual(h.pick(None, "high"), ("gpt-def", "high"))
        self.assertEqual(h.pick(None, None), ("gpt-def", "low"))
        self.assertEqual(h.pick("sonnet", "xhigh"), ("gpt-def", "xhigh"))   # a Claude name: the hub's model
        self.assertEqual(h.pick("codex/gpt-x", None), ("gpt-x", "low"))
        self.assertEqual(h.pick("codex/gpt-x", "max"), ("gpt-x", "max"))

    def test_next_to_claude_nothing_changes(self):
        h = CodexHost(self.state, codex=(sys.executable, FAKE_CODEX))   # HUB_CODEX=1: only codex/… sessions are Codex
        self.assertEqual(h.pick("sonnet", "high"), (None, "high"))


CODEX = {"host": "codex", "codex": {"model": "gpt-def", "effort": "low"}}


class Pairs(Temp):
    """`--work normal|risky` on a hub entirely on Codex: the Codex host gets two different pairs (main: both became the
    hub's model and effort); "Settings" may name codex/… models; Claude names do not apply there."""

    def host_gets(self, work, cfg):
        from core import modelrule
        pin = modelrule.pin(work, cfg)
        h = CodexHost(self.state, codex=(sys.executable, FAKE_CODEX), all_sessions=True, default_model="gpt-def",
                      default_effort="low")
        return h.pick(pin.get("model"), pin.get("effort"))

    def test_normal_and_risky_differ_on_codex(self):
        self.assertEqual(self.host_gets("normal", CODEX), ("gpt-def", "medium"))
        self.assertEqual(self.host_gets("risky", CODEX), ("gpt-def", "high"))

    def test_settings_name_codex_models(self):
        cfg = {**CODEX, "executors": {"risky": {"model": "codex/gpt-big", "effort": "xhigh"},
                                      "normal": {"model": "opus", "effort": "low"}}}
        self.assertEqual(self.host_gets("risky", cfg), ("gpt-big", "xhigh"))
        self.assertEqual(self.host_gets("normal", cfg), ("gpt-def", "low"))   # a Claude name: the hub's model

    def test_an_agents_own_pair_and_the_economy(self):
        from core import modelrule
        cfg = {**CODEX, "executors": {"agents": {"maps": {"model": "sonnet", "effort": "high"}},
                                      "economy": {"effort": "low"}}}
        self.assertEqual(modelrule.own("maps", cfg), {"effort": "high"})
        self.assertEqual(modelrule.economy(CODEX), None)   # nothing named: no downgrade
        self.assertEqual(modelrule.economy(cfg), {"effort": "low"})
        cfg["executors"]["agents"]["maps"]["model"] = "codex/gpt-x"
        self.assertEqual(modelrule.own("maps", cfg), {"model": "codex/gpt-x", "effort": "high"})
        for bad in ("codex/", "codex/-x", "codex/a b", "claude-opus-5-5"):
            self.assertFalse(modelrule.model_ok(bad, CODEX), bad)

    def test_a_claude_hub_as_on_main(self):
        from core import modelrule
        self.assertEqual(modelrule.pin("normal", {}), {"model": "sonnet", "effort": "high"})
        self.assertEqual(modelrule.pin("risky", {}), {"model": "opus", "effort": "medium"})
        cfg = {"executors": {"normal": {"model": "codex/gpt-x"}}}
        self.assertEqual(modelrule.pin("normal", cfg)["model"], "sonnet")   # codex/… is not a model there
        self.assertEqual(modelrule.economy({}), {"model": "sonnet", "effort": "medium"})


LISTED = [
    {"id": "gpt-a", "model": "gpt-a", "displayName": "GPT-A", "hidden": False, "isDefault": True,
     "defaultReasoningEffort": "medium",
     "supportedReasoningEfforts": [{"reasoningEffort": e} for e in ("low", "medium", "high", "xhigh", "max", "ultra")]},
    {"id": "gpt-old", "model": "gpt-old", "displayName": "GPT-Old", "hidden": False, "isDefault": False,
     "supportedReasoningEfforts": [{"reasoningEffort": e} for e in ("low", "medium", "high", "xhigh")]},
    {"id": "gpt-hidden", "model": "gpt-hidden", "hidden": True},
    {"id": "--bad", "model": "--bad"},
]


class Listing(Temp):
    """model/list of the hub's Codex — read without a model call or a sign-in, kept as a file."""

    class Rpc:
        def __init__(self, pages):
            self.pages, self.asked = list(pages), []

        def call(self, method, params):
            self.asked.append((method, params))
            return self.pages.pop(0)

    def test_pages_hidden_and_bad_names(self):
        from core import codexcli
        rpc = self.Rpc([{"data": LISTED[:1], "nextCursor": "c2"}, {"data": LISTED[1:], "nextCursor": None}])
        got = codexcli.fetch_models(self.state, rpc=rpc)
        self.assertEqual([m["id"] for m in got], ["gpt-a", "gpt-old"])
        self.assertEqual(rpc.asked, [("model/list", {}), ("model/list", {"cursor": "c2"})])
        self.assertEqual(got[0], {"id": "gpt-a", "title": "GPT-A", "default": True, "effort": "medium",
                                  "efforts": ["low", "medium", "high", "xhigh", "max", "ultra"]})
        listed, ts = codexcli.models(self.state)
        self.assertEqual(listed, got)
        self.assertGreater(ts, 0)

    def test_one_model_twice_on_a_page(self):
        from core import codexcli
        got = codexcli.fetch_models(self.state, rpc=self.Rpc([{"data": [LISTED[0], LISTED[0], LISTED[1]]}]))
        self.assertEqual([m["id"] for m in got], ["gpt-a", "gpt-old"])

    def test_an_empty_answer_keeps_the_list(self):
        from core import codexcli
        codexcli.fetch_models(self.state, rpc=self.Rpc([{"data": LISTED}]))
        self.assertEqual(codexcli.fetch_models(self.state, rpc=self.Rpc([{"data": []}])), [])
        self.assertEqual([m["id"] for m in codexcli.models(self.state)[0]], ["gpt-a", "gpt-old"])

    def test_no_file_or_a_broken_one(self):
        from core import codexcli
        self.assertEqual(codexcli.models(self.state), (None, None))
        with open(codexcli.models_path(self.state), "w") as f:
            f.write('{"ts": 1, "models": [{"id": "-x"}, "y"]}')
        self.assertEqual(codexcli.models(self.state), (None, None))

    @unittest.skipIf(os.name == "nt", "the fake's wrapper is sh")
    def test_through_the_app_server(self):
        import json
        from unittest import mock
        from core import codexcli
        wrapper, replies = os.path.join(self.tmp, "codex"), os.path.join(self.tmp, "replies.json")
        with open(wrapper, "w") as f:
            f.write(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CODEX}" "$@"\n')
        os.chmod(wrapper, 0o755)
        with open(replies, "w") as f:
            json.dump({"model/list": {"data": LISTED}}, f)
        with mock.patch.dict(os.environ, {"HUB_CODEX_BIN": wrapper, "FAKE_CODEX_REPLIES": replies}):
            got = codexcli.fetch_models(self.state)
        self.assertEqual([m["id"] for m in got], ["gpt-a", "gpt-old"])
        self.assertTrue(os.path.isdir(os.path.join(self.state, "codex-home")))   # the hub's Codex folder, not ~/.codex


class Snapshot(Temp):
    """The lists of /model and /effort in the page snapshot (commands): a Codex hub — its Codex models."""

    def web(self):
        from modules.web.server import Web
        w = Web(self.state)
        self.addCleanup(w.stop.set)
        return w

    def test_a_codex_hub_lists_its_models(self):
        from unittest import mock
        from core import codexcli
        codexcli.fetch_models(self.state, rpc=Listing.Rpc([{"data": LISTED}]))
        w = self.web()
        with mock.patch.object(codexcli, "fetch_models") as fetch:
            got = w.model_lists({"host": "codex", "codex": {"model": "gpt-old"}})
        fetch.assert_not_called()   # fresh: not read again
        self.assertEqual(got["models"], ["codex/gpt-a", "codex/gpt-old", "default"])
        self.assertEqual(got["titles"], {"codex/gpt-a": "GPT-A", "codex/gpt-old": "GPT-Old"})
        self.assertEqual(got["model_efforts"], {"codex/gpt-a": ["low", "medium", "high", "xhigh", "max"],
                                                "codex/gpt-old": ["low", "medium", "high", "xhigh"],
                                                "default": ["low", "medium", "high", "xhigh"]})   # the hub's model
        self.assertEqual(got["efforts"], ["low", "medium", "high", "xhigh", "max"])

    def test_before_the_list_is_read(self):
        import threading
        from unittest import mock
        from core import codexcli
        w = self.web()
        go = threading.Event()
        with mock.patch.object(codexcli, "fetch_models", side_effect=lambda *a: go.wait(5) and []) as fetch:
            got = w.model_lists({"host": "codex", "codex": {"model": "gpt-def"}})
            w.model_lists({"host": "codex"})   # a second snapshot while the reading is under way: no second one
            self.called(fetch)
            go.set()
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(got["models"], ["codex/gpt-def", "default"])
        self.assertEqual(got["model_efforts"], {})

    def called(self, fetch):
        import time
        end = time.time() + 10
        while not fetch.called and time.time() < end:
            time.sleep(0.01)

    def test_the_hubs_own_model_and_its_efforts(self):
        # codex.model not in model/list (hidden, gone): it stays listed, and its efforts are unknown — every level, not
        # those of another (isDefault) model; a model whose levels are all unknown to the core has no entry
        from unittest import mock
        from core import codexcli
        odd = {"model": "gpt-odd", "supportedReasoningEfforts": [{"reasoningEffort": "minimal"}]}
        codexcli.fetch_models(self.state, rpc=Listing.Rpc([{"data": [*LISTED, odd]}]))
        w = self.web()
        with mock.patch.object(codexcli, "fetch_models"):
            got = w.model_lists({"host": "codex", "codex": {"model": "gpt-hidden"}})
            fresh = w.model_lists({"host": "codex"})
        self.assertEqual(got["models"], ["codex/gpt-a", "codex/gpt-old", "codex/gpt-odd", "codex/gpt-hidden", "default"])
        self.assertNotIn("default", got["model_efforts"])
        self.assertNotIn("codex/gpt-odd", got["model_efforts"])
        self.assertEqual(fresh["model_efforts"]["default"], ["low", "medium", "high", "xhigh", "max"])   # isDefault
        self.assertTrue(got["codex"])

    def test_a_failed_reading_backs_off(self):
        import time
        from unittest import mock
        from core import codexcli
        w = self.web()
        waits = []
        with mock.patch.object(codexcli, "fetch_models", side_effect=[AttributeError("x"), [], AttributeError("x")]):
            for _ in range(3):
                w.read_codex_models_now()
                waits.append(round(w.codex_models_next - time.time()))
        self.assertEqual(waits, [300, 600, 1200])   # an empty answer counts as a failure too
        with mock.patch.object(codexcli, "fetch_models", return_value=[{"id": "gpt-a"}]):
            w.read_codex_models_now()
        self.assertLessEqual(w.codex_models_next, time.time())   # read: the file's age decides, no wait left over
        with mock.patch.object(codexcli, "fetch_models", side_effect=OSError("x")):
            w.read_codex_models_now()
        self.assertEqual(round(w.codex_models_next - time.time()), 300)

    def test_one_reading_at_a_time(self):
        import threading
        from unittest import mock
        from core import codexcli
        w = self.web()
        go = threading.Event()
        with mock.patch.object(codexcli, "fetch_models", side_effect=lambda *a: go.wait(5) and []) as fetch:
            w.read_codex_models()
            w.read_codex_models()
            self.called(fetch)
            go.set()
        self.assertEqual(fetch.call_count, 1)

    def test_a_claude_hub_as_on_main(self):
        from unittest import mock
        from core import catalog, codexcli
        w = self.web()
        with mock.patch.object(codexcli, "fetch_models") as fetch:
            got = w.model_lists({})
        fetch.assert_not_called()
        self.assertEqual(got, {"models": [*catalog.MODELS, "default"], "efforts": list(catalog.EFFORTS)})


class Settings(Temp):
    """"Settings → Executors" on a hub entirely on Codex: the model rows offer and accept codex/… models; the
    session model and effort rows of Claude are not shown there (the hub's Codex model is "codex.model")."""

    def test_executor_rows_take_codex_models(self):
        from modules.web import settings
        self.assertEqual(settings.validate("executors.risky.model", "codex/gpt-a", "en", CODEX), ("codex/gpt-a", None))
        self.assertEqual(settings.validate("executors.agents.maps.model", "codex/gpt-a", "en", CODEX)[0], "codex/gpt-a")
        self.assertIsNone(settings.validate("executors.risky.model", "codex/a b", "en", CODEX)[0])
        self.assertIsNone(settings.validate("executors.risky.model", "opus", "en", CODEX)[0])   # not a model there

    def test_a_claude_hub_as_on_main(self):
        from modules.web import settings
        self.assertIsNone(settings.validate("executors.risky.model", "codex/gpt-a", "en", {})[0])
        self.assertIsNone(settings.validate("executors.risky.model", "codex/gpt-a", "en")[0])
        self.assertEqual(settings.validate("executors.risky.model", "opus", "en", {}), ("opus", None))
        rows = {r["key"]: r for r in settings.view({}, {}, "en", agents=["maps"])}
        self.assertEqual(rows["executors.normal.model"]["value"], "sonnet")
        self.assertIn("opus", rows["executors.normal.model"]["choices"])
        self.assertIn("model", rows)

    def test_the_page_writes_a_codex_model(self):
        import json
        from unittest import mock
        from modules.web.server import Web
        conf = os.path.join(self.tmp, "hub.json")
        for cfg, code in ((CODEX, 200), ({}, 400)):
            with open(conf, "w") as f:
                json.dump(cfg, f)
            with mock.patch.dict(os.environ, {"HUB_CONFIG": conf}):
                w = Web(self.state)
                self.addCleanup(w.stop.set)
                self.assertEqual(w.settings_set("executors.risky.model", "codex/gpt-a")[0], code, cfg)
                with open(conf) as f:
                    self.assertEqual(json.load(f).get("executors", {}).get("risky", {}).get("model"),
                                     "codex/gpt-a" if code == 200 else None)

    def test_the_rows_on_a_codex_hub(self):
        from modules.web import settings
        rows = {r["key"]: r for r in settings.view(CODEX, {}, "en", agents=["maps"], codex_models=["gpt-a", "gpt-b"])}
        for key in ("executors.normal.model", "executors.economy.model", "executors.agents.maps.model"):
            self.assertEqual(rows[key]["choices"], ["", "codex/gpt-a", "codex/gpt-b"], key)
        self.assertEqual((rows["executors.normal.model"]["value"], rows["executors.normal.effort"]["value"]),
                         ("", "medium"))
        self.assertEqual((rows["executors.risky.model"]["value"], rows["executors.risky.effort"]["value"]),
                         ("", "high"))
        self.assertEqual(rows["executors.economy.model"]["value"], "")
        self.assertNotIn("model", rows)
        self.assertNotIn("effort", rows)
        # a Claude model left from before does not apply here: the row shows what is in effect (the hub's model)
        stale = {**CODEX, "executors": {"risky": {"model": "opus"}, "agents": {"maps": {"model": "sonnet"}}}}
        rows = {r["key"]: r for r in settings.view(stale, {}, "en", agents=["maps"], codex_models=["gpt-a"])}
        self.assertEqual((rows["executors.risky.model"]["value"], rows["executors.agents.maps.model"]["value"]), ("", ""))
        self.assertNotIn("opus", rows["executors.risky.model"]["choices"])
        held = {**CODEX, "executors": {"normal": {"model": "codex/gpt-gone"}}}   # set before, no longer listed: shown
        rows = {r["key"]: r for r in settings.view(held, {}, "en", codex_models=["gpt-a"])}
        self.assertEqual(rows["executors.normal.model"]["choices"], ["", "codex/gpt-a", "codex/gpt-gone"])


class FreshHub(Temp):
    """A fresh hub on Codex: the fold knows codex/… models from the core's codex.host mark; it is written at the start,
    before the mailbox is read — a model or an executor pair with codex/… left in the mailbox is not refused before the
    daemon's first tick. A Claude hub writes no mark, as on main."""

    def hub(self, host):
        from unittest import mock
        from core.daemon import Hub
        conf = os.path.join(self.tmp, "hub.json")
        with open(conf, "w") as f:
            f.write("{}")
        with mock.patch.dict(os.environ, {"HUB_CONFIG": conf}):
            os.environ.pop("HUB_CODEX", None)
            return Hub(self.state, host, registry_dir=os.path.join(self.state, "registry"))

    def test_codex_models_are_known_before_the_first_tick(self):
        from core import journal, spool
        box = os.path.join(self.state, "spool")
        spool.put(box, "session.requested", "maps", {"cwd": self.state, "session_id": "s-m", "channel": "cli",
                                                    "owner": True})
        spool.put(box, "session.model_set", "maps", {"model": "codex/gpt-x", "channel": "cli", "owner": True})
        h = self.hub(CodexHost(self.state, codex=(sys.executable, FAKE_CODEX), all_sessions=True,
                               default_model="gpt-def", env={"FAKE_TURN_S": "0.2"}))
        self.addCleanup(h.host.kill_server)
        h.start()
        rejected = [e for e in journal.read(h.journal.path) if e["type"] == "core.rejected"]
        self.assertEqual(rejected, [])
        self.assertEqual(h.state["sessions"]["maps"].get("model"), "codex/gpt-x")
        self.assertTrue(h.state["codex"]["host"])

    def test_a_claude_hub_writes_no_mark(self):
        import types
        from core import journal
        h = self.hub(types.SimpleNamespace())
        h.codex_mark()
        h.codex_tick()
        self.assertEqual([e for e in journal.read(h.journal.path) if e["type"].startswith("codex.")], [])
        self.assertNotIn("codex", h.state)


if __name__ == "__main__":
    unittest.main()
