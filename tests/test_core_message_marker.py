"""A core message whose id is not of the form mN / mNxK (MCP login — mNaK, MCP outside the hub — mNo, a duplicate of a
question — mNd, a resume after a limit — mNx5-<session>) is recognised by the hook from its marker, like any other:
otherwise the turn starts without a marker — the message is not delivered (four "already sent" attempts, then a
failure), and the feed shows it as "Typed in the terminal" with the raw marker [hub:mNaK ...] (2026-10-19: the hub
was waiting for an MCP login, seen since 2026-10-07 — 50 times in the journal)."""
import tests  # noqa: F401 — keep the machine's settings out
import unittest

from core import forms, reducer, views
from hooks import hub_hook
from tests.fakes import lang

OWNER = {"channel": "cli", "owner": True}
HUB_RU = lang.word("words", "hub")             # a hub named in Russian
TASK_RU = lang.legacy("record_task").lower()   # the task word of the earlier labels


class Marker(unittest.TestCase):
    def test_every_id_shape_the_core_makes_is_a_marker(self):
        for mid in ("m12", "m12x1", "m12x81", "m12a10", "m12o", "m12d", "m12x5-maps", "m12x5-maps.v2_x", "m12x5-Hub",
                    f"m12x5-{HUB_RU}-{HUB_RU}"):   # the hub name comes from hub.json, any value
            for origin in ("", lang.origin("core"), " from core", f" {lang.legacy('from')} maps, {TASK_RU} d7"):
                head = f"[hub:{mid}{origin}] text"
                with self.subTest(head=head):
                    kind, data = hub_hook.translate({"hook_event_name": "UserPromptSubmit", "prompt": head})
                    self.assertEqual(data["marker"], mid)
                    self.assertEqual([m.group(1) for m in forms.HEAD.finditer(head)], [mid])
        # joined lines (also) — by the same rule
        kind, data = hub_hook.translate({"hook_event_name": "UserPromptSubmit",
                                         "prompt": f"{lang.head('m5', kind='owner')} yes\n"
                                                   f"{lang.head('m12a3', kind='core')} login"})
        self.assertEqual((data["marker"], data["also"]), ("m5", ["m12a3"]))


class McpLoginNote(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.acts = reducer.empty(), 0, []
        self.ev("session.requested", "hub-core", cwd="/h", session_id="s-h", kind="hub", **OWNER)
        self.ev("session.started", "hub-core")
        self.ev("session.requested", "maps", cwd="/w/maps", session_id="s-maps", **OWNER)
        self.ev("session.started", "maps")

    def ev(self, type, entity=None, **data):
        self.seq += 1
        event = {"seq": self.seq, "ts": 1000 + self.seq, "type": type, "entity": entity, "data": data}
        self.st, acts, why = reducer.reduce(self.st, event)
        self.acts += acts
        return event

    def test_the_hub_turn_carries_the_mark_and_the_note_is_delivered(self):
        self.ev("session.info", "maps", mcp=["pay", "pay-live"], mcp_failed=[], mcp_auth=["pay", "pay-live"])
        typed = [a for a in self.acts if a["do"] == "type" and a["session"] == "hub-core"]
        self.assertEqual(len(typed), 1)                          # the second one waits in the queue
        mid = typed[0]["message"]
        self.assertNotRegex(mid, r"^m\d+$")                      # an id with a suffix — the very case
        self.ev("host.done", "hub-core", do="type", message=mid, attempt=1, result="sent")
        kind, data = hub_hook.translate({"hook_event_name": "UserPromptSubmit", "prompt": typed[0]["text"]})
        self.assertEqual(data["marker"], mid)
        started = self.ev(kind, "hub-core", **data)
        self.assertEqual(self.st["messages"][mid]["state"], "delivered")
        # the hub feed shows a core message row, not "Typed in the terminal" with a raw marker
        rows = [it for _, it in views._items(self.st, started)]
        self.assertNotIn("terminal", [it.get("kind") for it in rows])


if __name__ == "__main__":
    unittest.main()
