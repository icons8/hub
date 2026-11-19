""""Skills and MCP" of a session on the page (spec d2547143, modules/web/static/js/kit.js): from the session's "More"
menu, the set its process loaded and what it really used — with the executors' calls, a skill the model does not see in
its list, an MCP tool split by the argument that names what it opened, and a plugin that did not load. The owner was
sure the sessions used all their skills; the page showed only what the core granted."""
import tests  # noqa: F401
import json

from tests.test_web_e2e import PageInChrome

KIT = {"engine": "claude", "listed": {"in_prompt": 3, "total": 4, "tokens": 120},
       "skills": [{"name": "geo:tiles", "source": "plugin", "plugin": "geo", "in_prompt": True},
                  {"name": "geo:styles", "source": "plugin", "plugin": "geo", "in_prompt": True},
                  {"name": "release-notes", "source": "repo", "in_prompt": False,
                   "path": "/home/x/maps/.claude/skills/release-notes/SKILL.md"},
                  {"name": "code-review", "source": "built-in", "in_prompt": True}],
       "plugin_errors": [{"plugin": "geo-extra", "type": "path-not-found", "text": "Path not found: /home/x/plug/geo-extra"}],
       "mcp": [{"name": "atlas", "tools": 9}]}

ROWS = """(() => [...document.querySelectorAll('main .kit-row')].map((r) => ({
  key: r.dataset.key || '', text: r.innerText.replace(/\\s+/g, ' ').trim(), unused: r.classList.contains('unused') })))()"""


class KitPage(PageInChrome):
    def stand(self):
        self.ev("session.requested", "maps", session_id="s-maps", cwd="/w", channel="cli", owner=True, role="agent")
        self.ev("session.started", "maps", session_id="s-maps", source="startup")
        self.ev("session.kit", "maps", **KIT)
        self.ev("session.requested", "maps-3", session_id="s-maps-3", cwd="/w", channel="cli", owner=True,
                executor=True, raised_by="maps")
        self.ev("session.started", "maps-3", session_id="s-maps-3", source="startup")
        self.ev("session.kit", "maps-3", **{**KIT, "skills": KIT["skills"][:1]})
        for n, (s, name, inp) in enumerate([("maps", "Skill", {"skill": "geo:tiles"}),
                                            ("maps-3", "Skill", {"skill": "geo:tiles"}),
                                            ("maps-3", "Skill", {"skill": "geo:tiles"}),
                                            ("maps", "mcp__atlas__skill_reference", {"name": "atlas-tiles"}),
                                            ("maps", "mcp__atlas__skill_reference", {"name": "atlas-styles"})]):
            self.ev("turn.tool", s, id=f"t{n}", name=name, input=inp)
            self.ev("turn.tool_result", s, id=f"t{n}", error=False, text="ok")

    def open(self, *more):
        item = "[...document.querySelectorAll('[role=menuitem]')].find((x) => x.textContent.trim() === 'Skills and MCP')"
        return self.page({"go": f"http://127.0.0.1:{self.port}/#/s/maps"},
                         {"until": "!!document.querySelector('main .head .menu-btn')", "ms": 10000},
                         {"eval": "document.querySelector('main .head .menu-btn').click(), 1"},
                         {"eval": f"(() => {{ const b = {item}; if (b) b.click(); return !!b; }})()"},
                         {"until": "location.hash === '#/kit/maps' && !!document.querySelector('main .kit-row')", "ms": 10000},
                         *more, {"eval": ROWS})

    def test_the_set_and_the_uses_from_the_more_menu(self):
        self.stand()
        got = self.open()
        self.assertTrue(got[3]["eval"])
        rows = {r["key"]: r for r in got[-1]["eval"]}
        self.assertIn("1 · 2", rows["geo:tiles"]["text"])                   # itself · its executors
        self.assertFalse(rows["geo:tiles"]["unused"])
        self.assertIn("Plugin geo", rows["geo:tiles"]["text"])
        self.assertIn("Repo maps", rows["release-notes"]["text"])           # a repo skill says whose repo, not just "Repo"
        self.assertTrue(rows["geo:styles"]["unused"])
        self.assertIn("Not in the model's list", rows["release-notes"]["text"])
        self.assertIn("Not in the session now", rows["atlas"]["text"])     # the core has no live status of it
        self.assertIn("name=atlas-tiles ×1 · name=atlas-styles ×1", rows["skill_reference"]["text"])
        failed = [r for r in got[-1]["eval"] if "geo-extra" in r["text"]]
        self.assertTrue(failed and "did not load" in failed[0]["text"], got[-1]["eval"])

    def test_the_lists_tokens_and_the_periods_conversations_and_turns_stand_beside_the_counts(self):
        self.stand()
        for s in ("maps", "maps", "maps-3"):
            self.ev("turn.started", s, prompt_head="redo the tiles")
        got = self.open({"eval": "document.querySelector('main .kit-notes').innerText"})
        notes = got[-2]["eval"]
        self.assertIn("3 of 4 skills in the model's list · 120 tokens of the context", notes)
        self.assertIn("In the period: conversations 1, turns 2; executors: conversations 1, turns 1", notes)

    def test_a_server_that_does_not_say_the_volume_shows_the_page_as_before(self):
        self.stand()
        body = json.dumps({"session": "maps", "days": 7, "complete": True, "period_ready": True, "read_from": 1.0,
                           "kit": {**KIT, "listed": {"in_prompt": 3, "total": 4}, "at": 1.78e9}, "mcp_status": {},
                           "used": {"own": {}, "executors": {}, "executors_count": 0}})   # no tokens, no volume
        got = self.mocked(f"Promise.resolve(new Response({json.dumps(body)}, {{status: 200}}))",
                          {"until": "!!document.querySelector('main .kit-row')", "ms": 10000},
                          {"eval": "document.querySelector('main .kit-notes').innerText"})
        self.assertIn("3 of 4 skills in the model's list", got[-1]["eval"])
        self.assertNotIn("tokens", got[-1]["eval"])
        self.assertNotIn("In the period", got[-1]["eval"])

    def test_an_executor_sees_what_its_lead_has_and_it_does_not(self):
        self.stand()
        got = self.page({"go": f"http://127.0.0.1:{self.port}/#/kit/maps-3"},
                        {"until": "!!document.querySelector('main .kit-row')"},
                        {"eval": "document.querySelector('main .kit-body').innerText"})
        self.assertIn("Its lead maps has skills this session does not: geo:styles, release-notes",
                      got[-1]["eval"], json.dumps(got[-1])[:600])

    def mocked(self, js, *more):
        """The page with /api/session/kit answered by `js` (a function body: the request count n, the real fetch)."""
        init = ("(() => { const real = window.fetch.bind(window); let n = 0; window.fetch = (u, o) => "
                "String(u).includes('/api/session/kit') ? (n++, (" + js + ")) : real(u, o); })()")
        return self.page({"go": "about:blank"}, {"go": f"http://127.0.0.1:{self.port}/#/kit/maps", "init": init},
                         {"until": "!!document.querySelector('main .kit-body')", "ms": 10000}, *more)

    def test_a_hub_still_reading_its_journal_is_loading_not_a_failure(self):
        self.stand()
        body = json.dumps({"session": "maps", "days": 7, "complete": True, "period_ready": True, "read_from": 1.0,
                           "kit": {**KIT, "at": 1.78e9}, "mcp_status": {}, "used": {"own": {}, "executors": {}, "executors_count": 0}})
        got = self.mocked("n === 1 ? Promise.resolve(new Response('{}', {status: 503})) : "
                          f"Promise.resolve(new Response({json.dumps(body)}, {{status: 200}}))",
                          {"eval": "document.querySelector('main .kit-body').innerText"},
                          {"until": "!!document.querySelector('main .kit-row')", "ms": 10000},
                          {"eval": "document.querySelector('main .kit-body').innerText"})
        self.assertNotIn("Could not load", got[-3]["eval"])                # the 503 is "loading", not an error
        self.assertTrue(got[-2]["until"], got[-3]["eval"])                  # and it is asked again by itself

    def test_the_history_still_being_read_says_a_dash_not_that_nothing_was_found(self):
        self.stand()
        old = {"skill:release-notes": {"tool": 0, "command": 0, "file": 0, "failed": 0, "unknown": 0, "last": None},
               "skill:retired-one": {"tool": 0, "command": 0, "file": 0, "failed": 0, "unknown": 0, "last": 1.7e9}}
        body = json.dumps({"session": "maps", "days": 7, "complete": False, "period_ready": True, "read_from": 1.78e9,
                           "kit": {**KIT, "at": 1.78e9}, "mcp_status": {}, "used": {"own": old, "executors": {}, "executors_count": 0}})
        got = self.mocked(f"Promise.resolve(new Response({json.dumps(body)}, {{status: 200}}))",
                          {"until": "!!document.querySelector('main .kit-row')", "ms": 10000}, {"eval": ROWS})
        rows = {r["key"]: r for r in got[-1]["eval"]}
        self.assertNotIn("Not since", rows["geo:styles"]["text"])
        self.assertNotIn("Never", rows["geo:styles"]["text"])               # not "never": older journals are not read yet
        self.assertTrue(rows["geo:styles"]["text"].endswith("—"), rows["geo:styles"]["text"])
        folded = self.page({"eval": "[...document.querySelectorAll('main .kit-gone .kit-row')].map((r) => r.dataset.key)"})
        self.assertEqual(folded[-1]["eval"], ["retired-one"])                # not in the set, not called this period: apart

    def test_the_mcp_status_of_a_sleeping_session_is_its_last_run(self):
        self.stand()
        self.ev("session.info", "maps-3", mcp=["atlas"], mcp_connected=["atlas"])
        self.ev("session.close_requested", "maps-3", retire=True)
        self.ev("session.exited", "maps-3", why="sleep")
        got = self.page({"go": f"http://127.0.0.1:{self.port}/#/kit/maps-3"},
                        {"until": "!!document.querySelector('main .kit-row')", "ms": 10000},
                        {"eval": ROWS})
        rows = {r["key"]: r for r in got[-1]["eval"]}
        self.assertIn("Connected (last run)", rows["atlas"]["text"])


def load_tests(loader, standard, pattern):   # only its own tests, not the ones inherited from the page
    import unittest
    return unittest.TestSuite(KitPage(n) for n in KitPage.__dict__ if n.startswith("test_"))
