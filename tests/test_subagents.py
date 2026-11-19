"""Steps of a helper agent (owner decision 2026-10-06: when an agent edits code, we do not see it at all): a session calls
Agent, it edits files, and for the hub that is one step. Claude Code writes the agent's conversation beside the session's
(<project>/<session_id>/subagents/agent-<id>.jsonl, with the Agent step's toolUseId in .meta.json): the page
reads it on click."""
import tests  # noqa: F401 — hide the machine's settings
import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest

from core import journal, subagents
from modules.web.server import Web
from hosts import worktree
from tests import winsafe


def line(kind, content, **extra):
    return json.dumps({"type": kind, "isSidechain": True, "message": {"role": kind, "content": content}, **extra})


class SubagentSteps(unittest.TestCase):
    def setUp(self):
        self.claude = tempfile.mkdtemp(prefix="sub-")
        self.addCleanup(shutil.rmtree, self.claude, True)
        self.cwd = "/w/proj.x"
        # the project folder as claude names it (on Windows with a drive: D--w-proj-x)
        self.dir = os.path.join(self.claude, "projects", worktree.slug(self.cwd), "sid-1", "subagents")
        os.makedirs(self.dir)

    def agent(self, aid, tool, lines, **meta):
        with open(os.path.join(self.dir, f"agent-{aid}.meta.json"), "w") as f:
            json.dump({"agentType": "implement", "description": "Fix", "toolUseId": tool, **meta}, f)
        with open(os.path.join(self.dir, f"agent-{aid}.jsonl"), "w") as f:
            f.write("\n".join(lines) + "\n")

    def test_steps_with_inputs_and_results(self):
        self.agent("a1", "toolu_A", [
            line("user", "Task: fix the font"),
            line("assistant", [{"type": "text", "text": "Fixing"},
                               {"type": "tool_use", "id": "t1", "name": "Edit",
                                "input": {"file_path": "/w/a.vue", "old_string": "x", "new_string": "y"}}]),
            line("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]),
            line("assistant", [{"type": "tool_use", "id": "t2", "name": "Agent", "input": {"description": "Review"}}]),
            line("user", [{"type": "tool_result", "tool_use_id": "t2", "is_error": True,
                           "content": [{"type": "text", "text": "crashed"}]}]),
            "not json",
        ])
        got = subagents.steps(self.claude, self.cwd, "sid-1", "toolu_A")
        self.assertEqual(got["agent"], {"type": "implement", "description": "Fix"})
        self.assertEqual([(i["kind"], i.get("name")) for i in got["items"]],
                         [("said", None), ("tool", "Edit"), ("tool", "Agent")])
        edit, nested = got["items"][1], got["items"][2]
        self.assertEqual((edit["id"], edit["input"]["new_string"], edit["result"], edit["status"]), ("t1", "y", "ok", "done"))
        self.assertEqual((nested["result"], nested["status"]), ("crashed", "error"))

    def test_a_long_reply_of_the_agent_is_whole(self):
        # its words reach the page whole, like a session's (journal.TEXT_MAX), not cut at a tool result's 4000
        text = "block " * 1500
        self.agent("a9", "toolu_L", [line("assistant", [{"type": "text", "text": text}])])
        self.assertEqual(subagents.steps(self.claude, self.cwd, "sid-1", "toolu_L")["items"], [{"kind": "said", "text": text}])

    def test_a_fork_shows_only_its_own_steps(self):
        # a fork (Claude Code 2.1.302 in -p, captured 2026-10-17): its conversation starts with a link to the parent's
        # conversation and
        # an inherited parent message (its text and Agent steps, including the fork call itself) and the replies to
        # them; these are not the fork's steps
        self.agent("a4", "toolu_F1", [
            json.dumps({"type": "fork-context-ref"}),
            line("assistant", [{"type": "text", "text": "Splitting into two parts"},
                               {"type": "tool_use", "id": "toolu_F1", "name": "Agent",
                                "input": {"description": "Part 1", "prompt": "x", "subagent_type": "fork"}},
                               {"type": "tool_use", "id": "toolu_F2", "name": "Agent",
                                "input": {"description": "Part 2", "prompt": "y", "subagent_type": "fork"}}]),
            line("user", [{"type": "tool_result", "tool_use_id": "toolu_F1",
                           "content": [{"type": "text", "text": "Fork started — processing in background"}]},
                          {"type": "tool_result", "tool_use_id": "toolu_F2",
                           "content": [{"type": "text", "text": "Fork started — processing in background"}]},
                          {"type": "text", "text": "<fork-boilerplate>…</fork-boilerplate>\n\nYour directive: x"}]),
            line("assistant", [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]),
            line("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "a.txt"}]),
            line("assistant", [{"type": "text", "text": "Part 1 is ready"}]),
        ], agentType="fork", isFork=True)
        got = subagents.steps(self.claude, self.cwd, "sid-1", "toolu_F1")
        self.assertEqual([(i["kind"], i.get("name") or i.get("text")) for i in got["items"]],
                         [("tool", "Bash"), ("said", "Part 1 is ready")])
        self.assertEqual(got["items"][0]["result"], "a.txt")
        # there is no stub reply to the fork call yet (the file is read while the fork writes it): its call is still not
        # a fork step
        self.agent("a5", "toolu_G1", [
            line("assistant", [{"type": "tool_use", "id": "toolu_G1", "name": "Agent", "input": {"subagent_type": "fork"}}]),
            line("assistant", [{"type": "text", "text": "Looking"}])], agentType="fork", isFork=True)
        self.assertEqual([i.get("text") for i in subagents.steps(self.claude, self.cwd, "sid-1", "toolu_G1")["items"]],
                         ["Looking"])

    def test_found_under_another_conversation_of_the_project(self):
        # the session's conversation continued with a new id (resume, fork): the agent lies under the old one, so search
        # the whole project
        self.agent("a2", "toolu_B", [line("assistant", [{"type": "text", "text": "x"}])])
        self.assertEqual(len(subagents.steps(self.claude, self.cwd, "sid-other", "toolu_B")["items"]), 1)

    def test_a_folder_reached_through_a_link_is_found_under_its_real_path(self):
        # claude names the project folder by the real path (/tmp -> /private/tmp on macOS), while the session may hold a
        # link
        base = tempfile.mkdtemp(prefix="sub-cwd-")
        self.addCleanup(shutil.rmtree, base, True)
        real, link = os.path.join(base, "real"), os.path.join(base, "link")
        os.makedirs(real)
        winsafe.symlink(self, real, link)
        self.dir = os.path.join(self.claude, "projects", worktree.slug(real), "sid-1", "subagents")
        os.makedirs(self.dir)
        self.agent("a3", "toolu_C", [line("assistant", [{"type": "text", "text": "x"}])])
        self.assertEqual(len(subagents.steps(self.claude, link, "sid-1", "toolu_C")["items"]), 1)

    def test_unknown_or_bad_ids(self):
        self.assertIsNone(subagents.steps(self.claude, self.cwd, "sid-1", "toolu_none"))
        self.assertIsNone(subagents.steps(self.claude, self.cwd, "../../etc", "toolu_A"))
        self.assertIsNone(subagents.steps(self.claude, self.cwd, "sid-1", "../x"))

    def test_long_inputs_are_clipped(self):
        big = "z" * 100000
        self.agent("a3", "toolu_C", [line("assistant", [{"type": "tool_use", "id": "t", "name": "Write",
                                                         "input": {"file_path": "/w/b", "content": big}}])])
        item = subagents.steps(self.claude, self.cwd, "sid-1", "toolu_C")["items"][0]
        self.assertLess(len(item["input"]["content"]), 30000)



class SubagentApi(unittest.TestCase):
    def setUp(self):   # the page on an empty journal; helpers are copied from tests/test_web.py, not inherited
        self.state = tempfile.mkdtemp(prefix="hubweb-")
        self.j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        self.web = Web(self.state)
        self.web.decide_wait = 0
        self.server = self.web.serve(port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.web.stop.set()
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.state, ignore_errors=True)

    def req(self, method, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, None, {"Host": f"127.0.0.1:{self.port}", "X-Hub": "1"})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def settle(self):
        self.web.tail.poll()

    def test_page_gets_steps_by_session_and_tool(self):
        claude = os.environ["CLAUDE_CONFIG_DIR"]
        d = os.path.join(claude, "projects", worktree.slug("/w/p"), "sid-a", "subagents")
        os.makedirs(d, exist_ok=True)
        self.addCleanup(shutil.rmtree, os.path.join(claude, "projects", worktree.slug("/w/p")), True)
        with open(os.path.join(d, "agent-x.meta.json"), "w") as f:
            json.dump({"agentType": "implement", "description": "Fix", "toolUseId": "toolu_Q"}, f)
        with open(os.path.join(d, "agent-x.jsonl"), "w") as f:
            f.write(line("assistant", [{"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": "/w/p/a"}}]) + "\n")
        self.j.append("session.requested", "a", {"session_id": "sid-a", "cwd": "/w/p"})
        self.settle()
        code, body = self.req("GET", "/api/subagent?session=a&tool=toolu_Q")
        self.assertEqual(code, 200)
        self.assertEqual([i["name"] for i in json.loads(body)["items"]], ["Edit"])
        self.assertEqual(self.req("GET", "/api/subagent?session=a&tool=toolu_none")[0], 404)
        self.assertEqual(self.req("GET", "/api/subagent?session=missing&tool=toolu_Q")[0], 404)

if __name__ == "__main__":
    unittest.main()
