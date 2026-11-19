"""Replays of failures from tests/replays/*.json through the pure fold. No model and no tmux."""
import json
import pathlib
import re
import tempfile
import unittest

from core import langdata, reducer
from tests.fakes import tz

REPLAYS = pathlib.Path(__file__).parent / "replays"
# an earlier form of the core's text in a replay: ⟦legacy.<key>[.<subkey>]⟧ (core/langdata.legacy), so the files hold none;
# other ⟦…⟧ (the pseudo-language xx of core/i18n.py wraps text in them) are text, not placeholders
PLACEHOLDER = re.compile(r"⟦legacy(?:\.([^⟦⟧]*))?⟧")
# what is left after the expansion and still looks like a placeholder: a dotted name in brackets (a typo, another
# section) or an unclosed ⟦legacy. — an error, not text
BROKEN = re.compile(r"⟦\s*[A-Za-z_]+\.[\w.]*(?:⟧|$)|⟦legacy(?:\.|⟧)", re.M)


def _legacy(m):
    """The JSON-escaped value of one placeholder; an unknown key or not a string — KeyError."""
    path = (m[1] or "").split(".")
    try:
        v = langdata.legacy(path[0])
        for k in path[1:]:
            v = v[int(k)] if isinstance(v, list) else v[k] if isinstance(v, dict) else None
    except (KeyError, IndexError, ValueError, TypeError):
        v = None
    if not isinstance(v, str):
        raise KeyError(m[0])
    return json.dumps(v)[1:-1]


def load(path):
    """A replay file with its placeholders expanded; every reader of tests/replays/*.json goes through it."""
    text = PLACEHOLDER.sub(_legacy, pathlib.Path(path).read_text(encoding="utf-8"))
    bad = BROKEN.search(text)
    if bad:
        raise KeyError(f"{path}: a broken placeholder {bad[0]!r}")
    return json.loads(text)


def run(events):
    """Number the events, fold them; return (state, actions, number of rejected)."""
    state, actions, rejected = reducer.empty(), [], 0
    for seq, raw in enumerate(events, 1):
        event = {"seq": seq, "ts": raw["t"], "src": "test", "type": raw["type"],
                 "entity": raw.get("entity"), "data": raw.get("data") or {}}
        state, acts, why = reducer.reduce(state, event)
        actions += [a for a in acts if a["do"] != "task_note"]   # a trace in the task record is a file, not a host action
        rejected += why is not None
    return state, actions, rejected


def subset(actual, expected):
    return {k: actual.get(k) for k in expected}


class Replays(unittest.TestCase):
    def test_all(self):
        files = sorted(REPLAYS.glob("*.json"))
        self.assertGreater(len(files), 0)
        for path in files:
            case = load(path)
            with self.subTest(case=path.stem):
                if not tz.HAVE and any((e.get("data") or {}).get("timezone") not in (None, "UTC") for e in case["events"]):
                    self.skipTest("no timezone database (Windows without the tzdata package)")   # the owner's zone is unknowable here
                state, actions, rejected = run(case["events"])
                exp = case["expect"]
                for group in ("sessions", "messages", "delegations", "schedule"):
                    for key, fields in exp.get(group, {}).items():
                        self.assertIn(key, state[group], f"{group}/{key}")
                        self.assertEqual(subset(state[group][key], fields), fields, f"{group}/{key}")
                for key in exp.get("messages_absent", []):
                    self.assertNotIn(key, state["messages"], f"messages/{key}")
                for key in exp.get("schedule_absent", []):
                    self.assertNotIn(key, state["schedule"], f"schedule/{key}")
                if "owner" in exp:
                    self.assertEqual(subset(state["owner"], exp["owner"]), exp["owner"])
                if "actions" in exp:
                    got = [subset(a, e) for a, e in zip(actions, exp["actions"])]
                    self.assertEqual(len(actions), len(exp["actions"]), f"actions: {actions}")
                    self.assertEqual(got, exp["actions"])
                if "rejected" in exp:
                    self.assertEqual(rejected, exp["rejected"])

    def test_replay_is_deterministic_and_resumable(self):
        """hubd killed mid-delivery: replaying a journal prefix gives the same state, and the typing deadline is restored."""
        case = load(REPLAYS / "crash2_enter_lost_retry.json")
        events = case["events"][:5]  # the message is typed, no confirmation yet
        full, _, _ = run(events)
        again, _, _ = run(events)
        self.assertEqual(full, again)
        self.assertEqual(reducer.pending_timers(full), [(35, "m4", 1)])


class Load(unittest.TestCase):
    def load(self, value):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "case.json"
            path.write_text(json.dumps({"v": value}, ensure_ascii=False), encoding="utf-8")
            return load(path)["v"]

    def test_placeholder_is_the_earlier_form(self):
        old = langdata.legacy
        self.assertEqual(self.load("[hub:m5⟦legacy.origin.owner⟧] y"), f"[hub:m5{old('origin')['owner']}] y")
        self.assertEqual(self.load(" ⟦legacy.from⟧ w"), f" {old('from')} w")
        self.assertEqual(self.load("⟦legacy.quiet_sys.1⟧"), old("quiet_sys")[1])
        # a value with backslashes and quotes is escaped into the JSON string, not cut there
        self.assertEqual(self.load("⟦legacy.head_context⟧"), old("head_context"))
        self.assertEqual(self.load("⟦legacy.report_rule⟧"), old("report_rule"))

    def test_unknown_placeholder_fails(self):
        for bad in ("⟦legacy.nope⟧", "⟦legacy.origin.nope⟧", "⟦legacy.origin⟧", "⟦legacy.from.0⟧", "⟦legacy⟧",
                    "⟦legacy.quiet_sys.99⟧", "⟦legacy.from", "⟦legasy.origin.owner⟧", "⟦ Legacy.from⟧", "⟦words.yes⟧"):
            with self.subTest(bad=bad), self.assertRaises(KeyError):
                self.load(bad)

    def test_other_brackets_are_text(self):
        # the pseudo-language wraps English in the same brackets: a replay may record such output as it is
        for text in ("⟦Task from the owner⟧", "⟦legacy forms are kept⟧", "a ⟦ b ⟧ c"):
            self.assertEqual(self.load(text), text)


if __name__ == "__main__":
    unittest.main()
