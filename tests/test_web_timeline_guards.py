"""Page timelines (timeline.js): a late reply, another server run, a session with the same name and a stuck fetch, on
synthetic snapshots and a fake fetch with a manual clock."""
import tests  # noqa: F401
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node is required for the timeline transport fixture")
class TimelineGuards(unittest.TestCase):
    def js(self, body):
        prelude = r'''
import assert from 'node:assert/strict';
import fs from 'node:fs';
let now=0, serial=0, timers=new Map(), calls=[], renders=0;
globalThis.setTimeout=(fn,ms=0)=>{const id=++serial;timers.set(id,{fn,at:now+ms});return id;};
globalThis.clearTimeout=id=>timers.delete(id);
Date.now=()=>now;
globalThis.setInterval=()=>0;globalThis.t=k=>k;   // backoff.js counts down on the page; here only its pauses are needed
const backoff=await import('data:text/javascript;base64,'+Buffer.from(fs.readFileSync('modules/web/static/js/backoff.js','utf8').replace(/import[^;]+;/g,'')).toString('base64'));
Object.assign(globalThis,backoff);   // timeline.js imports from backoff.js: the import is stripped below
globalThis.S={snap:{epoch:'one',sessions:[{name:'sample',session_id:'first'}],timeline_rev:{sample:1},timelines:{}},reading:()=>false,selected:'sample'};
globalThis.renderLive=()=>{renders++;};
globalThis.fetch=(url,options)=>new Promise((resolve,reject)=>calls.push({url,options,resolve,reject}));
const source=fs.readFileSync('modules/web/static/js/timeline.js','utf8').replace(/import[^;]+;/g,'');
const m=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const settle=async()=>{for(let i=0;i<30;i++)await Promise.resolve();};
const tick=async ms=>{now+=ms;for(const [id,t] of [...timers])if(t.at<=now){timers.delete(id);t.fn();}await settle();};
const data=(rev=1,extra={})=>({epoch:S.snap.epoch,rev,full:true,older:false,items:[{k:1,kind:'out',text:'synthetic-'+rev}],...extra});
const answer=async(index,body=data())=>{calls[index].resolve({ok:true,json:async()=>body});await settle();};
'''
        result = subprocess.run([NODE, "--input-type=module", "-e", prelude + body],
                                cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr[-4000:])

    def test_new_epoch_never_adopts_old_rows_or_accepts_a_late_response(self):
        self.js(r'''
await (async()=>{const p=m.sync('sample');await answer(0);await p;})();
assert.equal(m.loaded('sample'),true);
S.snap.timeline_rev.sample=2;const old=m.sync('sample');const obsolete=data(2);
S.snap={...S.snap,epoch:'two',timeline_rev:{sample:1},timelines:{}};m.adopt(S.snap);
assert.equal(S.snap.timelines.sample,undefined);assert.equal(m.loaded('sample'),false);
const current=m.sync('sample');await settle();assert.equal(calls.length,3);
await answer(1,obsolete);await old;assert.equal(S.snap.timelines.sample,undefined);
await answer(2,data(1));await current;assert.equal(S.snap.timelines.sample[0].text,'synthetic-1');
''')

    def test_same_name_replacement_and_removal_reject_old_cache_and_pending_reads(self):
        self.js(r'''
const old=m.sync('sample'),obsolete=data();
S.snap={...S.snap,sessions:[{name:'sample',session_id:'replacement'}]};m.adopt(S.snap);
const current=m.sync('sample');await settle();assert.equal(calls.length,2);
await answer(0,obsolete);await old;assert.equal(S.snap.timelines.sample,undefined);
await answer(1);await current;assert.equal(m.loaded('sample'),true);
S.snap.sessions=[];m.adopt(S.snap);assert.equal(S.snap.timelines.sample,undefined);assert.equal(m.loaded('sample'),false);
await m.sync('sample');assert.equal(calls.length,2);
''')

    def test_timeout_releases_a_hung_fetch_retries_after_growing_pauses_and_ignores_its_late_success(self):
        self.js(r'''
const old=m.sync('sample');await settle();await tick(15000);await old;
assert.equal(calls[0].options.signal.aborted,true);assert.equal(m.failed('sample'),true);
assert.equal(calls.length,1);assert.equal(timers.size,1);   // no retry before the pause (1 s) is over
await tick(500);assert.equal(calls.length,1);
await tick(500);await settle();assert.equal(calls.length,2);   // the retry; this one hangs too
await tick(15000);await settle();assert.equal(m.failed('sample'),true);
await tick(900);assert.equal(calls.length,2);   // the second pause is 1 s as well, the third is 2 s
await tick(100);await settle();assert.equal(calls.length,3);
await answer(2,data(2));await answer(0,data(1));await answer(1,data(1));
assert.equal(S.snap.timelines.sample[0].text,'synthetic-2');assert.equal(m.failed('sample'),false);assert.equal(timers.size,0);
''')

    def test_timeout_also_covers_a_hung_json_body(self):
        self.js(r'''
let body;const pending=m.sync('sample');calls[0].resolve({ok:true,json:()=>new Promise(resolve=>{body=resolve;})});await settle();
await tick(15000);await pending;assert.equal(m.failed('sample'),true);assert.equal(m.loaded('sample'),false);
body(data());await settle();assert.equal(S.snap.timelines.sample,undefined);assert.equal(timers.size,1);   // only the retry after the pause
''')

    def test_invalid_epoch_revision_rows_and_cold_delta_never_replace_a_valid_cache(self):
        self.js(r'''
let p=m.sync('sample');await answer(0);await p;
for(const invalid of [data(2,{epoch:'other'}),data(0),data(2,{items:null}),data(2,{full:false,items:[{k:'bad',kind:'out'}]})]){
 S.snap.timeline_rev.sample=2;m.retry('sample');await answer(calls.length-1,invalid);
 assert.equal(S.snap.timelines.sample[0].text,'synthetic-1');assert.equal(m.failed('sample'),true);
}
S.snap={...S.snap,epoch:'two'};m.adopt(S.snap);p=m.sync('sample');await answer(calls.length-1,data(2,{full:false}));await p;
assert.equal(m.loaded('sample'),false);assert.equal(S.snap.timelines.sample,undefined);
''')

    def test_response_must_reach_captured_revision_but_not_a_later_snapshot(self):
        self.js(r'''
let p=m.sync('sample');await answer(0);await p;
S.snap.timeline_rev.sample=2;p=m.sync('sample');
await answer(1,data(1,{items:[{k:1,kind:'out',text:'stale overwrite'}]}));await p;
assert.equal(m.failed('sample'),true);assert.equal(S.snap.timelines.sample[0].text,'synthetic-1');
m.retry('sample');S.snap.timeline_rev.sample=3;m.sync('sample');
await answer(2,data(2));
assert.equal(m.failed('sample'),false);assert.equal(S.snap.timelines.sample[0].text,'synthetic-2');
await tick(300);assert.equal(calls.length,4);await answer(3,data(3));
assert.equal(S.snap.timelines.sample[0].text,'synthetic-3');
''')

    def test_incremental_merge_reading_retention_deferred_sync_and_old_server_are_preserved(self):
        self.js(r'''
let p=m.sync('sample');await answer(0,data(1,{items:Array.from({length:400},(_,k)=>({k,kind:'out',text:'row'}))}));await p;
S.reading=()=>true;S.snap.timeline_rev.sample=2;p=m.sync('sample');m.sync('sample');
assert.match(calls[1].url,/since=1&epoch=one/);
await answer(1,data(2,{full:false,items:[{k:400,kind:'out',text:'next'}]}));await p;
assert.equal(S.snap.timelines.sample.length,401);await tick(300);assert.equal(calls.length,2);
S.snap={sessions:[{name:'sample'}],timelines:{sample:[{k:9,text:'legacy'}]}};m.adopt(S.snap);await m.sync('sample');
assert.equal(m.loaded('sample'),true);assert.equal(S.snap.timelines.sample[0].text,'legacy');assert.equal(calls.length,2);
''')

    # PR #425 review: the Fibonacci pause of a feed that did not load must not swallow a press, stall a loaded feed,
    # leave a timer behind or fetch a feed nobody looks at
    FAIL = "const fail=async i=>{calls[i].reject(new Error('down'));await settle();};\n"

    def test_a_press_during_a_failing_fetch_is_one_more_fetch_after_it(self):
        self.js(self.FAIL + r'''
m.sync('sample');await settle();m.retry('sample');assert.equal(calls.length,1);   // pressed while the fetch is on its way
await fail(0);assert.equal(m.failed('sample'),true);
await tick(300);assert.equal(calls.length,2);   // not the 1 s pause: the press goes right after the failed fetch
await answer(1);assert.equal(m.failed('sample'),false);assert.equal(timers.size,0);
''')

    def test_a_loaded_feed_that_failed_to_refresh_retries_with_every_hub_event(self):
        self.js(self.FAIL + r'''
let p=m.sync('sample');await answer(0);await p;
for(let i=1;i<=4;i++){S.snap.timeline_rev.sample=1+i;m.sync('sample');await settle();assert.equal(calls.length,i+1);await fail(i);}
assert.equal(m.failed('sample'),true);assert.equal(timers.size,0);   // no pause and no timer: the next event retries
S.snap.timeline_rev.sample=6;m.sync('sample');await answer(5,data(6));assert.equal(m.failed('sample'),false);
''')

    def test_the_pause_timer_is_one_and_only_for_the_feed_on_screen(self):
        self.js(self.FAIL + r'''
m.sync('sample');await settle();S.snap.timeline_rev.sample=2;m.sync('sample');   // a hub event during the fetch
await fail(0);assert.equal(timers.size,1);   // only the pause: the event waits it out, no second timer beside it
await tick(300);assert.equal(calls.length,1);await tick(700);assert.equal(calls.length,2);
await fail(1);S.selected='other';await tick(1000);assert.equal(calls.length,2);assert.equal(timers.size,0);   // not on screen
S.selected='sample';m.sync('sample');await settle();assert.equal(calls.length,3);   // opened again: the pause is over
''')

    def test_back_to_the_tab_tries_at_once_and_keeps_the_pauses_the_network_starts_them_over(self):
        self.js(self.FAIL + r'''
m.sync('sample');await settle();await fail(0);await tick(1000);await fail(1);await tick(1000);await fail(2);   // next pause 2 s
m.retryFailed(false);await settle();assert.equal(calls.length,4);await fail(3);
assert.equal(m.nextTry('sample'),now+3000);   // the count went on: 1, 1, 2, then 3 s
m.retryFailed(false);m.retryFailed(false);await settle();assert.equal(calls.length,5);   // focus + visibilitychange: one fetch
await fail(4);m.retryFailed(true);await settle();assert.equal(calls.length,6);await fail(5);
assert.equal(m.nextTry('sample'),now+1000);   // the network is back: the pauses start over
''')
