"""Steps of a helper agent (Agent) go under its step, not in the session feed.

Claude Code in stream-json marks the agent's calls and text with the parent_tool_use_id field. The host keeps it in the
event (`parent`), and the session feed does not show them: they are visible through the «Agent steps» button on the Agent
step. Without the mark the same steps were mixed in with the session's steps and shown twice.
"""
import tests  # noqa: F401 — do not see the machine's settings
import tempfile
import unittest

from core import views


class Pipe:
    def write(self, _):
        pass

    def flush(self):
        pass


class Host(unittest.TestCase):
    def test_the_agents_steps_and_words_carry_their_parent(self):
        from hosts.streamjson import StreamHost, StreamProcess
        host = StreamHost(tempfile.mkdtemp(prefix="agsteps-"))
        popen = type("P", (), {"stdin": Pipe(), "poll": lambda self: None, "pid": 1})()
        host.procs["a"] = StreamProcess(popen)
        for msg in (
            {"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
                {"type": "tool_use", "id": "toolu_ag", "name": "Agent", "input": {"prompt": "take a look"}}]}},
            {"type": "assistant", "parent_tool_use_id": "toolu_ag", "message": {"content": [
                {"type": "text", "text": "looking"},
                {"type": "tool_use", "id": "toolu_b", "name": "Bash", "input": {"command": "ls"}}]}},
            {"type": "user", "parent_tool_use_id": "toolu_ag", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "toolu_b", "content": "a.txt"}]}},
            {"type": "assistant", "parent_tool_use_id": None, "message": {"content": [{"type": "text", "text": "ok"}]}},
        ):
            host.inbox.put(("a", msg))
        facts = [(k, d) for k, _, d in host.poll() if k in ("turn.tool", "turn.tool_result", "session.said")]
        self.assertEqual([(k, d.get("id") or d.get("text"), d.get("parent")) for k, d in facts], [
            ("turn.tool", "toolu_ag", None), ("session.said", "looking", "toolu_ag"), ("turn.tool", "toolu_b", "toolu_ag"),
            ("turn.tool_result", "toolu_b", "toolu_ag"), ("session.said", "ok", None)])
        self.assertNotIn("parent", facts[0][1])          # the session's own steps have no such field — the journal does not grow

    def test_the_agents_calls_do_not_change_the_sessions_conversation_length(self):
        from hosts.streamjson import StreamHost, StreamProcess
        host = StreamHost(tempfile.mkdtemp(prefix="agsteps-"))
        popen = type("P", (), {"stdin": Pipe(), "poll": lambda self: None, "pid": 1})()
        host.procs["a"] = StreamProcess(popen)
        for parent, read in ((None, 250000), ("toolu_ag", 20000)):
            host.inbox.put(("a", {"type": "assistant", "parent_tool_use_id": parent, "message": {
                "usage": {"input_tokens": 1, "cache_read_input_tokens": read}, "content": [{"type": "text", "text": "x"}]}}))
        host.inbox.put(("a", {"type": "result", "result": "done", "num_turns": 2}))
        res = [d for k, _, d in host.poll() if k == "turn.result"][0]
        self.assertEqual(res["context"], 250001)          # the length of the session's conversation, not its agent's


class Feed(unittest.TestCase):
    def test_the_session_feed_shows_the_agent_step_but_not_its_steps(self):
        r = views.Reader()
        raw = [("session.requested", {"session_id": "s", "cwd": "/w"}), ("session.started", {"session_id": "s"}),
               ("turn.tool", {"id": "toolu_ag", "name": "Agent", "input": {"prompt": "take a look"}}),
               ("session.said", {"text": "looking", "parent": "toolu_ag"}),
               ("turn.tool", {"id": "toolu_b", "name": "Bash", "input": {"command": "ls"}, "parent": "toolu_ag"}),
               ("turn.tool_result", {"id": "toolu_b", "text": "a.txt", "parent": "toolu_ag"}),
               ("turn.tool_result", {"id": "toolu_ag", "text": "done"}),
               ("session.said", {"text": "ok"})]
        for i, (kind, data) in enumerate(raw, 1):
            r.feed({"seq": i, "ts": i, "src": "test", "type": kind, "entity": "a", "data": data})
        items = [(i["kind"], i.get("name") or i.get("text")) for i in r.snapshot()["timelines"]["a"]
                 if i["kind"] in ("tool", "out")]
        self.assertEqual(items, [("tool", "Agent"), ("out", "ok")])
        (agent,) = [i for i in r.snapshot()["timelines"]["a"] if i["kind"] == "tool"]
        self.assertEqual(agent["status"], "done")


if __name__ == "__main__":
    unittest.main()
