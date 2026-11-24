"""Kept drafts of the page (drafts.js): the field text and the "Reply" target in localStorage — old entries expire, a
sent draft is removed at once, two tabs do not wipe each other and take each other's changes. Node with a fake
storage and a manual clock."""
import tests  # noqa: F401
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node is required for the drafts fixture")
class KeptDrafts(unittest.TestCase):
    def js(self, body):
        prelude = r'''
import assert from 'node:assert/strict';
import fs from 'node:fs';
const DAY = 864e5;
let now = Date.UTC(2026, 0, 5, 12), serial = 0, timers = new Map(), tabs = [];
const store = new Map();
// one storage for every tab; a write from one tab is a "storage" event in the others, as in a browser
let writer = null;
const told = (key) => { for (const t of tabs) if (t !== writer) for (const fn of t.on.storage || []) fn({key}); };
globalThis.localStorage = {getItem: (k) => store.has(k) ? store.get(k) : null,
  setItem: (k, v) => { store.set(k, String(v)); told(k); }, removeItem: (k) => { store.delete(k); told(k); }};
globalThis.setTimeout = (fn, ms = 0) => { const id = ++serial; timers.set(id, {fn, at: now + ms, tab: writer}); return id; };
globalThis.clearTimeout = (id) => timers.delete(id);
Date.now = () => now;
globalThis.document = {addEventListener: () => {}, visibilityState: 'visible'};
const as = (t, fn) => { const was = writer; writer = t; try { return fn(); } finally { writer = was; } };
const tick = (ms) => { now += ms; for (const [id, t] of [...timers]) if (t.at <= now) { timers.delete(id); as(t.tab, t.fn); } };
const source = fs.readFileSync('modules/web/static/js/drafts.js', 'utf8').replace(/import[^;]+;/g, '');
// a tab: its own S, fields and redraw count; the page module is loaded anew over the same storage
const tab = async (cur = 'a') => {
  const t = {S: {cur: {name: cur}}, on: {}, boxes: new Map(), renders: 0};
  t.render = () => { t.renders++; };
  t.addEventListener = (e, fn) => { (t.on[e] ||= []).push(fn); };
  tabs.push(t);
  globalThis.__tab = t;
  t.m = await import('data:text/javascript;base64,' + Buffer.from(
    'const { S, boxes, render, addEventListener } = globalThis.__tab;\n' + source + `\n// tab ${tabs.length}`).toString('base64'));
  // the session field of thread k (dock.js names it on every draw) gets the text
  t.set = (k, v) => as(t, () => { t.m.keep(k); t.S.drafts[k] = v; });
  t.reply = (v) => as(t, () => { t.S.replyTarget = v; });
  return t;
};
const stored = (k) => JSON.parse(store.get(k) || '{}');
'''
        result = subprocess.run([NODE, "--input-type=module", "-e", prelude + body], cwd=ROOT, capture_output=True,
                                text=True, encoding="utf-8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr[-4000:])

    def test_prune_keeps_the_young_and_the_newest_and_merge_applies_removals(self):
        self.js(r"""
const {m} = await tab();
const all = {old: {at: now - 15 * DAY}, young: {at: now - DAY}, future: {at: now + 2 * DAY}, bad: {text: 'x'}, z: null};
assert.deepEqual(Object.keys(m.prune(all, now, m.DRAFT_TTL)), ['young']);
assert.deepEqual(Object.keys(m.prune({a: {at: now - 3}, b: {at: now - 1}, c: {at: now - 2}}, now, DAY, 2)), ['b', 'c']);
assert.deepEqual(m.prune('junk', now, DAY), {});
assert.deepEqual(m.prune(null, now, DAY), {});
assert.deepEqual(m.merge({a: {at: 1}, b: {at: 2}}, {a: null, c: {at: 3}}), {b: {at: 2}, c: {at: 3}});
""")

    def test_a_draft_is_written_after_a_pause_comes_back_after_a_reload_and_expires(self):
        self.js(r"""
let t = await tab();
t.set('a', 'half'); t.set('a', 'half a message [Image #1]');
assert.equal(store.has('hub.drafts'), false);            // typing waits for a pause
tick(500);
assert.equal(stored('hub.drafts').a.text, 'half a message [Image #1]');
t = await tab();
assert.equal(t.S.drafts.a, 'half a message [Image #1]'); // a reload brings it back, the mark included
now += 15 * DAY;
t = await tab();
assert.equal(t.S.drafts.a, undefined);                   // two weeks later it is gone
""")

    def test_a_sent_draft_is_removed_at_once_not_after_the_pause(self):
        self.js(r"""
let t = await tab();
t.set('a', 'goes out'); tick(500);
t.set('a', '');                                          // the composer clears the field before the send request
assert.equal(stored('hub.drafts').a, undefined);         // written at once: a reload right now must not send it again
t = await tab();
assert.equal(t.S.drafts.a, undefined);
""")

    def test_broken_storage_does_not_break_the_page(self):
        self.js(r"""
store.set('hub.drafts', '{not json'); store.set('hub.reply', '[1, "x", null]');
let t = await tab();
assert.deepEqual({...t.S.drafts}, {});
assert.equal(t.S.replyTarget, null);
store.set('hub.drafts', JSON.stringify({a: {at: now, text: 7}, b: 'x', c: {at: 'soon', text: 'c'}, d: {at: now, text: 'kept'}}));
store.set('hub.reply', JSON.stringify({a: {at: now, target: 'x'}, b: {at: now, target: {quote: 'no addressee'}}}));
t = await tab();
assert.deepEqual({...t.S.drafts}, {d: 'kept'});
assert.equal(t.S.replyTarget, null);
globalThis.localStorage.setItem = () => { throw new Error('full'); };
t.set('a', 'typed with a full storage'); tick(500);      // no storage: the draft lives in the tab as before
assert.equal(t.S.drafts.a, 'typed with a full storage');
""")

    def test_two_tabs_do_not_wipe_each_others_drafts(self):
        self.js(r"""
const one = await tab(), two = await tab();
one.set('a', 'from the first'); two.set('b', 'from the second');
tick(500);
const all = stored('hub.drafts');
assert.equal(all.a.text, 'from the first');
assert.equal(all.b.text, 'from the second');
two.set('b', '');                                        // the second tab sends its own: the first one's stays
assert.deepEqual(Object.keys(stored('hub.drafts')), ['a']);
""")

    def test_a_draft_sent_in_one_tab_does_not_come_back_from_another(self):
        # both tabs hold the same draft; one sends it. An edit in the other tab (of another thread) used to write its
        # whole old map and put the sent text back
        self.js(r"""
const one = await tab();
one.set('a', 'sent from the first tab'); tick(500);
const two = await tab();
let redrawn = 0;
two.boxes.set('a', {ta: {isConnected: true}, refresh: () => { redrawn++; }});
assert.equal(two.S.drafts.a, 'sent from the first tab');
one.set('a', '');                                        // sent
assert.equal(two.S.drafts.a, undefined);                 // the other tab's field is empty too
assert.equal(redrawn, 1);
two.set('b', 'something else'); tick(500);
assert.deepEqual(Object.keys(stored('hub.drafts')), ['b']);
const three = await tab();
assert.equal(three.S.drafts.a, undefined);               // and a reload does not bring the sent text back
""")

    def test_what_is_being_typed_here_is_not_replaced_by_another_tab(self):
        self.js(r"""
const one = await tab(), two = await tab();
one.set('a', 'older text from the other tab'); one.set('b', 'another thread');
tick(300);
two.set('a', 'typing here');                             // not written yet when the other tab's write arrives
tick(200);
assert.equal(stored('hub.drafts').a.text, 'older text from the other tab');
assert.equal(two.S.drafts.a, 'typing here');
assert.equal(two.S.drafts.b, 'another thread');          // the other thread's draft is taken
tick(400);
assert.equal(stored('hub.drafts').a.text, 'typing here');   // the later text is the one kept, in both tabs
assert.equal(one.S.drafts.a, 'typing here');
""")

    def test_the_reply_target_belongs_to_its_thread(self):
        self.js(r"""
let t = await tab('a');
t.reply({session: 'a', quote: 'the quote', view: 'a'});
t.S.cur = {name: 'b'};
assert.equal(t.S.replyTarget, null);                     // another thread has none
t.reply(null);                                           // and clearing there does not touch a's
t.S.cur = {name: 'a'};
assert.equal(t.S.replyTarget.quote, 'the quote');
t.S.cur = null;
assert.equal(t.S.replyTarget, null);                     // no thread on the screen (Settings)
t.reply(null);
tick(500);
t = await tab('a');
assert.equal(t.S.replyTarget.quote, 'the quote');        // after a reload too
const other = await tab('a');
t.reply(null);
assert.equal(stored('hub.reply').a, undefined);          // cleared — written at once
assert.equal(other.S.replyTarget, null);                 // and gone from the other tab, which redraws its field
assert.equal(other.renders, 1);
""")

    def test_a_page_that_is_going_away_keeps_nothing_more(self):
        # a reload aborts the send in flight; the composer takes it for a failure and puts the text back into the field.
        # Kept, the text would be in the field after the reload though the message most likely went out
        self.js(r"""
let t = await tab();
t.set('a', 'on its way'); tick(500);
t.set('a', '');                                          // sent: the field and the storage are cleared
for (const fn of t.on.pagehide) as(t, fn);               // the page reloads
t.set('a', 'on its way');                                // the aborted request "failed": the text is back in the dying page
tick(500);
assert.equal(stored('hub.drafts').a, undefined);
t = await tab();
assert.equal(t.S.drafts.a, undefined);                   // the new page has an empty field
t.set('b', 'typed before leaving');
for (const fn of t.on.pagehide) as(t, fn);               // what was typed up to that moment is written on the way out
assert.equal(stored('hub.drafts').b.text, 'typed before leaving');
for (const fn of t.on.pageshow) as(t, fn);               // back from the browser's page cache: it keeps drafts again
t.set('b', 'typed after coming back'); tick(500);
assert.equal(stored('hub.drafts').b.text, 'typed after coming back');
""")

    def test_the_id_of_a_send_with_an_unknown_outcome_is_kept_a_day(self):
        self.js(r"""
let t = await tab();
const rec = {sig: '["a","text"]', client_id: 'c-1'};
as(t, () => { t.m.keep('a'); t.m.keep('b'); t.m.unsent.set('a', rec); });
assert.equal(t.m.unsent.get('a'), rec);                  // the composer compares the record it saw by identity
tick(500);
t = await tab();
assert.deepEqual(t.m.unsent.get('a'), rec);              // after a reload the same text goes with the same id
as(t, () => t.m.unsent.delete('a'));
assert.equal(stored('hub.unsent').a, undefined);         // the send got its answer: forgotten at once
as(t, () => t.m.unsent.set('b', rec)); tick(500);
now += 25 * 3600e3;                                      // the core no longer remembers the id: neither does the page
t = await tab();
assert.equal(t.m.unsent.has('b'), false);
store.set('hub.unsent', JSON.stringify({a: {at: now, sig: 5}, b: {at: now, sig: 's', client_id: 'c-2', extra: 1}}));
t = await tab();
assert.deepEqual([...t.m.unsent], [['b', {sig: 's', client_id: 'c-2'}]]);
""")

    def test_only_session_fields_are_kept_a_plugin_field_stores_its_own_draft(self):
        self.js(r"""
let t = await tab();
as(t, () => { t.S.drafts['plugin:note'] = 'a plugin field'; t.m.unsent.set('plugin:note', {sig: 's', client_id: 'c'}); });
as(t, () => { t.S.drafts.a = 'put before the field was drawn'; });   // e.g. board cards handed to a session
tick(500);
assert.equal(store.has('hub.drafts'), false);
assert.equal(store.has('hub.unsent'), false);
assert.equal(t.S.drafts['plugin:note'], 'a plugin field');           // in the tab it works as before
as(t, () => t.m.keep('a'));                                          // the session field is drawn: what it holds is kept
tick(500);
assert.deepEqual(Object.keys(stored('hub.drafts')), ['a']);
t = await tab();
assert.equal(t.S.drafts.a, 'put before the field was drawn');
as(t, () => { delete t.S.drafts.a; });
assert.equal(stored('hub.drafts').a, undefined);
""")


    def test_another_tab_writing_a_draft_does_not_wipe_fields_that_are_not_kept(self):
        # a plugin's field, a board's draft and a linked hub's field live in S.drafts too and are never stored: the
        # storage event from another tab used to delete every in-memory draft missing from storage
        self.js(r"""
const one = await tab(), two = await tab();
as(two, () => { two.S.drafts['plugin:note'] = 'a plugin field'; two.S.drafts['board:x'] = 'a board draft'; });
two.set('b', 'kept here'); tick(500);
one.set('a', 'a session draft'); tick(500);              // the other tab writes: a "storage" event in this one
assert.equal(two.S.drafts['plugin:note'], 'a plugin field');
assert.equal(two.S.drafts['board:x'], 'a board draft');
assert.equal(two.S.drafts.a, 'a session draft');          // the other tab's draft is taken
assert.equal(two.S.drafts.b, 'kept here');
one.set('b', '');                                         // a kept draft sent in the other tab still leaves this one
assert.equal(two.S.drafts.b, undefined);
assert.equal(two.S.drafts['plugin:note'], 'a plugin field');
""")

    def test_a_sent_draft_is_removed_even_when_the_storage_is_full(self):
        self.js(r"""
let t = await tab();
t.set('a', 'goes out'); tick(500);
const set = globalThis.localStorage.setItem;               // full: a longer value does not fit, a shorter one does
globalThis.localStorage.setItem = (k, v) => { if (String(v).length > (store.get(k) || '').length) throw new Error('full'); set(k, v); };
t.set('b', 'a long text that does not fit any more');      // waits for the pause
t.set('a', '');                                            // sent: written at once together with b, which does not fit
assert.equal(stored('hub.drafts').a, undefined);           // the removal went anyway
t = await tab();
assert.equal(t.S.drafts.a, undefined);                     // a reload does not bring the sent text back
""")

    def test_a_long_quote_is_stored_cut(self):
        self.js(r"""
let t = await tab('a');
const quote = 'x'.repeat(50000);
t.reply({session: 'a', quote, view: 'a'});
assert.equal(t.S.replyTarget.quote, quote);                // this tab keeps the quote as chosen
tick(500);
assert.equal(stored('hub.reply').a.target.quote, 'x'.repeat(4000) + '…');
t = await tab('a');
assert.equal(t.S.replyTarget.quote.length, 4001);
""")

    def test_a_kept_reply_target_to_a_session_that_is_gone_is_dropped(self):
        self.js(r"""
let t = await tab('a');
t.reply({session: 'old-name', quote: 'to a session renamed later', view: 'a'});
tick(500);
t = await tab('a');
assert.equal(t.S.replyTarget.quote, 'to a session renamed later');   // no snapshot yet: nothing to check against
t.S.snap = {sessions: [{name: 'a'}, {name: 'new-name'}]};
assert.equal(t.S.replyTarget, null);                                 // the addressee is no longer there
assert.equal(stored('hub.reply').a, undefined);
t.reply({session: 'gone-too', quote: 'chosen here', view: 'a'});
assert.equal(t.S.replyTarget.quote, 'chosen here');                  // chosen in this tab: as before, not checked
t.reply({session: 'b', quote: 'in a feed not on the list', view: 'z'});
tick(500);
t = await tab('z');
t.S.snap = {sessions: [{name: 'a'}]};
assert.equal(t.S.replyTarget.quote, 'in a feed not on the list');    // a feed that is not a session's: left as it is
""")


if __name__ == "__main__":
    unittest.main()
