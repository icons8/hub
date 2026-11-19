"""Name search must discover a matching child even when its parent does not match."""
import tests  # noqa: F401
import json
import os
import time

from tests.test_web_e2e import PageInChrome


class ChildSearch(PageInChrome):
    def fleet(self):
        # one moment for all: rows equally fresh keep this order (the freshest is on top, and Windows writes the journal
        # a few ms apart — needle-leaf was on top there, and Enter opened it)
        at = time.time()
        for name, extra in (("project", {"role": "agent"}),
                            ("needle-worker", {"executor": True, "raised_by": "project"}),
                            ("needle-leaf", {"executor": True, "raised_by": "needle-worker"}),
                            ("unrelated", {"executor": True, "raised_by": "project"})):
            self.j.append("session.requested", name, dict(session_id="s-" + name, cwd="/w", channel="cli", owner=True,
                                                         **extra), ts=at)
            self.j.append("session.started", name, {"session_id": "s-" + name}, ts=at)

    def query(self, value):
        # the list is rebuilt after a typing pause (sidebar.js FILTER_WAIT): the step types the value and waits 400 ms
        v = json.dumps(value)
        return {"until": "(() => { const f = document.getElementById('filter'); if (window.__qv !== " + v + ") {"
                " f.value = " + v + "; f.dispatchEvent(new Event('input')); window.__qv = " + v + ";"
                " window.__qt = performance.now(); return false; } return performance.now() - window.__qt > 400; })()"}

    def test_child_and_nested_child_are_discoverable_in_a_collapsed_group_and_enter_opens_a_match(self):
        self.fleet()
        got = self.page({"local": {"hub.closedGroups": '["agents"]'}},
                        {"go": f"http://127.0.0.1:{self.port}/#/sessions"},
                        {"until": "document.querySelector('#list [data-g=agents] .sgroup-h')"},
                        self.query("NEEDLE"),
                        {"eval": "[...document.querySelectorAll('#list .srow .nm')].map(n => n.textContent.trim().split(/\\s+/).pop())"},
                        {"eval": "document.querySelector('#list [data-g=agents] .sgroup-h').getAttribute('aria-expanded')"},
                        {"eval": "document.getElementById('filter').dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', bubbles:true})); 1"},
                        {"until": "location.hash === '#/s/needle-worker'"},
                        self.query(""),
                        {"eval": "[document.querySelector('#list [data-g=agents] .sgroup-h').getAttribute('aria-expanded'), localStorage.getItem('hub.closedGroups')]"})
        self.assertEqual(set(got[4]["eval"]), {"project", "needle-worker", "needle-leaf"})
        self.assertEqual(got[5]["eval"], "true")
        self.assertTrue(got[7]["until"])
        self.assertEqual(got[9]["eval"], ["false", '["agents"]'])

    def test_a_group_remembered_by_its_title_stays_collapsed(self):
        # hub.closedGroups used to hold the titles of the default groups: the page reads them from the catalog now
        from core import i18n
        self.fleet()
        got = self.page({"local": {"hub.closedGroups": json.dumps([i18n.tr("en", "web.sidebar.group_agents")])}},
                        {"go": f"http://127.0.0.1:{self.port}/#/sessions"},
                        {"until": "document.querySelector('#list [data-g=agents] .sgroup-h')"},
                        {"eval": "document.querySelector('#list [data-g=agents] .sgroup-h').getAttribute('aria-expanded')"})
        self.assertEqual(got[-1]["eval"], "false")

    def test_filter_overrides_active_and_fork_disclosure_without_persisting_them(self):
        self.fleet()
        got = self.page({"local": {"hub.sidebar": "active"}},
                        {"go": f"http://127.0.0.1:{self.port}/#/sessions"},
                        # the fleet is drawn: the tab's count (in Classic it is in the tooltip, the tab shows the word)
                        {"until": "(document.querySelector('#active-bar button')?.title || '').endsWith(' · 1')"},
                        self.query("needle-leaf"),
                        {"eval": "[...document.querySelectorAll('#list .srow .nm')].map(n => n.textContent.trim().split(/\\s+/).pop())"},
                        {"eval": "(() => { import('/static/js/state.js').then(m => window.searchState = m); return true; })()"},
                        {"until": "!!window.searchState"},
                        {"eval": "(() => { const {S,render} = window.searchState; S.snap.sessions = S.snap.sessions.filter(s => s.name !== 'needle-leaf'); render(); return {matches: [...document.querySelectorAll('#list .srow .nm')].map(n => n.textContent.trim().split(/\\s+/).pop())}; })()"},
                        self.query(""),
                        {"eval": "localStorage.getItem('hub.sidebar')"})
        self.assertTrue(got[2]["until"])
        self.assertEqual(set(got[4]["eval"]), {"project", "needle-leaf"})
        self.assertEqual(got[7]["eval"], {"matches": []})
        self.assertEqual(got[9]["eval"], "active")

    def test_filter_restores_closed_forks_and_handles_a_resting_parent(self):
        self.fleet()
        got = self.page({"go": f"http://127.0.0.1:{self.port}/#/sessions"},
                        {"until": "document.querySelector('#list .fork-toggle')"},
                        {"eval": "document.querySelector('#list .fork-toggle').click(); document.querySelector('#list .fork-toggle').click(); 1"},
                        self.query("needle-leaf"),
                        {"eval": "document.querySelector('#list .fork-toggle').getAttribute('aria-expanded')"})
        self.assertEqual(got[-1]["eval"], "true")
        shots = os.environ.get("HUB_SIDEBAR_SEARCH_SHOTS") or self.state
        os.makedirs(shots, exist_ok=True)
        for width in (390, 1280):
            self.page({"shot": os.path.join(shots, f"child-search-{width}.png"), "width": width})
        got = self.page(self.query(""),
                        {"eval": "[document.querySelector('#list .fork-toggle').getAttribute('aria-expanded'), !!document.querySelector('#list li.forks')]"},
                        {"eval": "import('/static/js/state.js').then(m => window.searchState = m); 1"},
                        {"until": "!!window.searchState"},
                        {"eval": "(() => { const {S,render} = window.searchState; const p = S.snap.sessions.find(s => s.name === 'project'); Object.assign(p, {role:'executor',backlog:'tasks',state:'asleep',status:'asleep'}); render(); return 1; })()"},
                        self.query("needle-leaf"),
                        {"eval": "[...document.querySelectorAll('#list .resting .srow .nm')].map(n => n.textContent.trim().split(/\\s+/).pop())"})
        self.assertEqual(got[1]["eval"], ["false", False])
        self.assertEqual(set(got[6]["eval"]), {"project", "needle-leaf"})

    def test_name_prefix_match_comes_first_inside_its_group_and_groups_keep_their_order(self):
        # a name that starts with the typed text goes first, an exact name above it, but only inside its own group: the order of the
        # groups does not change while typing (the list must not jump from letter to letter); Enter opens the top row of the list
        for name, extra in (("web-design", {"role": "agent"}), ("notes-bot", {"role": "agent"}),
                            ("old-design", {}), ("design-notes", {}), ("notes", {})):
            self.ev("session.requested", name, session_id="s-" + name, cwd="/w", channel="cli", owner=True, **extra)
            self.ev("session.started", name, session_id="s-" + name)
        names = "[...document.querySelectorAll('#list .srow .nm')].map(n => n.textContent.trim().split(/\\s+/).pop())"
        got = self.page({"go": f"http://127.0.0.1:{self.port}/#/sessions"},
                        {"until": "document.querySelector('#list [data-g=agents] .sgroup-h')"},
                        self.query("desi"),
                        {"until": "!document.querySelector('#list li[data-s=\"notes\"]')"},
                        {"eval": names},
                        {"eval": "document.getElementById('filter').dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', bubbles:true})); 1"},
                        {"until": "location.hash === '#/s/web-design'", "ms": 3000},
                        self.query("notes"),
                        {"until": "!document.querySelector('#list li[data-s=\"web-design\"]')"},
                        {"eval": names})
        # the agents group stays above the threads; inside the threads the name that starts with the text is first
        self.assertEqual(got[4]["eval"], ["web-design", "design-notes", "old-design"])
        self.assertTrue(got[6]["until"])
        self.assertEqual(got[9]["eval"], ["notes-bot", "notes", "design-notes"])

    def test_under_a_parent_the_better_match_goes_first_and_equal_ones_the_freshest_first(self):
        # under one parent the executor whose name starts with the typed text is above the one matched in the middle;
        # equal matches keep the usual order, the freshest on top; Enter opens the top row of the list (the parent here)
        import time
        now = time.time()
        for k, (name, extra) in enumerate((("old-design", {}), ("design-old", {"executor": True, "raised_by": "old-design"}),
                                           ("design-new", {"executor": True, "raised_by": "old-design"}),
                                           ("redesign-x", {"executor": True, "raised_by": "old-design"}))):   # the middle match is the freshest
            self.j.append("session.requested", name, dict(session_id="s-" + name, cwd="/w", channel="cli", owner=True, **extra), ts=now - 100 + k * 10)
            self.j.append("session.started", name, dict(session_id="s-" + name), ts=now - 100 + k * 10)
        got = self.page({"go": f"http://127.0.0.1:{self.port}/#/sessions"},
                        {"until": "document.querySelector('#list li[data-s=\"old-design\"]')"},
                        self.query("desi"),
                        {"until": "document.querySelectorAll('#list .srow[data-search-match]').length > 0"},
                        {"eval": "[...document.querySelectorAll('#list .srow .nm')].map(n => n.textContent.trim().split(/\\s+/).pop())"},
                        {"eval": "document.getElementById('filter').dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', bubbles:true})); 1"},
                        {"until": "location.hash === '#/s/old-design'", "ms": 3000})
        self.assertEqual(got[4]["eval"], ["old-design", "design-new", "design-old", "redesign-x"])
        self.assertTrue(got[6]["until"])


def load_tests(loader, standard, pattern):
    import unittest
    return unittest.TestSuite(ChildSearch(n) for n in ChildSearch.__dict__ if n.startswith("test_"))
