// The detailed session list: what a row shows comes from the snapshot only (out_head, since, state, cwd).
// No browser: the pure parts are rows.js, the rules of clean.css and the wiring in clean.js are read as text.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const DIR = path.join(import.meta.dirname, "..", "..", "modules", "web", "static", "clean");
const { AGO, agoText, flat, sessionFor, detailOf, liveLine, lastAt, byRecent } = await import(pathToFileURL(path.join(DIR, "rows.js")).href);
const css = fs.readFileSync(path.join(DIR, "clean.css"), "utf8");
const js = fs.readFileSync(path.join(DIR, "clean.js"), "utf8");
const NOW = 1_800_000_000;
// the page's words come from the catalogs (web.clean.ago_*); here another language's, to see they are the ones used
const WORDS = { now: "jetzt", m: (n) => `${n} Min.`, h: (n) => `${n} Std.`, d: (n) => `${n} T.` };

test("the preview is flat text: markup, images, list marks and line breaks are gone, one line, no trailing ellipsis", () => {
  assert.equal(flat("Done. **Board 81:35** is `outdated` — see [the doc](file:///a/b.md)."), "Done. Board 81:35 is outdated — see the doc.");
  assert.equal(flat("Ready. ![Overview](file:///a/b.png) Next: - first - second"), "Ready. Next: first second");
  assert.equal(flat("## Title\n\n> quote\n```js\nlet a = 1;\n```\ntail"), "Title quote js let a = 1; tail");
  assert.equal(flat("There was not enough room to read this thought to its very end and more…"), "There was not enough room to read this thought to its very end and more");
  assert.equal(flat("Pas assez de place pour lire cette pensée jusqu'au bout … "), "Pas assez de place pour lire cette pensée jusqu'au bout");
  assert.equal(flat("HANDOFF-DONE Done: the report is ready"), "Done: the report is ready", "the hand-off marker between sessions is not list text");
  assert.equal(flat("HANDOFF-ASK: Which branch?"), "Which branch?");
  assert.equal(flat("x".repeat(500)).length, 120);
  assert.equal(flat("   \n  "), "");
  assert.equal(flat(null), "");
  assert.equal(flat(undefined), "");
  assert.equal(flat("snake_case_name stays and a + b too"), "snake_case_name stays and a + b too");
});

test("time: a working session is \"now\", otherwise minutes, hours, days since the last change", () => {
  const at = (sec, state = "idle") => agoText({ state, since: NOW - sec }, NOW);
  assert.equal(at(5), "now");
  assert.equal(at(59), "now");
  assert.equal(at(60), "1m");
  assert.equal(at(14 * 60 + 20), "14m");
  assert.equal(at(3599), "59m");
  assert.equal(at(3600), "1h");
  assert.equal(at(86399), "23h");
  assert.equal(at(86400 * 5 + 99), "5d");
  assert.equal(at(7200, "busy"), "now", "since of a running turn is its start, not the last activity");
  assert.equal(at(7200, "starting"), "now");
  assert.equal(at(-30), "now", "a clock a little ahead of the core never gives a negative time");
  assert.equal(agoText({ state: "idle" }, NOW), "now", "no since: nothing to count");
});

test("time words: English by default (AGO), the page's words when given, the count passed to them", () => {
  assert.equal(AGO.now, "now");
  assert.deepEqual([AGO.m(3), AGO.h(4), AGO.d(5)], ["3m", "4h", "5d"]);
  assert.equal(agoText({ state: "idle", since: NOW - 180 }, NOW, AGO), agoText({ state: "idle", since: NOW - 180 }, NOW));
  assert.equal(agoText({ state: "idle", since: NOW - 180 }, NOW, WORDS), "3 Min.");
  assert.equal(agoText({ state: "idle", since: NOW - 3 * 3600 }, NOW, WORDS), "3 Std.");
  assert.equal(agoText({ state: "idle", since: NOW - 2 * 86400 }, NOW, WORDS), "2 T.");
  assert.equal(agoText({ state: "idle", since: NOW - 10 }, NOW, WORDS), "jetzt");
  assert.equal(agoText({ state: "busy", since: NOW - 7200 }, NOW, WORDS), "jetzt");
});

test("clean.js takes the time words from the catalogs (web.clean.ago_*) with English as the fallback", () => {
  assert.match(js, /import \{[^}]*\bAGO\b[^}]*\} from "\.\/rows\.js"/);
  assert.match(js, /now: T\("web\.clean\.ago_now", AGO\.now\)/);
  for (const k of ["m", "h", "d"]) assert.match(js, new RegExp(`${k}: \\(count\\) => T\\("web\\.clean\\.ago_${k}", AGO\\.${k}\\(count\\), \\{ count \\}\\)`));
  assert.match(js, /detailOf\(s, now, WORDS, liveLine\(s, /);
});

test("the row finds its session by the shown name, a child under its parent too, but never by a guess", () => {
  const list = [{ name: "maps-dev" }, { name: "maps-dev-api" }, { name: "x-api" }, { name: "docs-api" }];
  assert.equal(sessionFor("maps-dev", list).name, "maps-dev");
  assert.equal(sessionFor("maps-dev-api", list).name, "maps-dev-api");
  assert.equal(sessionFor("api", list), null, "two sessions end with -api: no guess");
  assert.equal(sessionFor("dev", list).name, "maps-dev");
  assert.equal(sessionFor("", list), null);
  assert.equal(sessionFor("nope", list), null);
});

test("what a row shows: the hub has the preview but no time, an empty chat has no second line, the full path goes to the tooltip", () => {
  const s = { name: "flash", kind: "work", state: "idle", since: NOW - 120, cwd: "/u/dev/p/.claude/worktrees/flash-fix", out_head: "Fixed **the flash** on reload…" };
  assert.deepEqual(detailOf(s, NOW, AGO), { prev: "Fixed the flash on reload", ago: "2m", cwd: s.cwd });
  assert.deepEqual(detailOf(s, NOW, WORDS), { prev: "Fixed the flash on reload", ago: "2 Min.", cwd: s.cwd });
  assert.equal(detailOf({ ...s, out_head: null, gist: { text: "The gist of the turn" } }, NOW, AGO).prev, "The gist of the turn", "an executor that never wrote to the owner: the gist of its turn");
  assert.deepEqual(detailOf({ ...s, out_head: null }, NOW, AGO), { prev: "", ago: "2m", cwd: s.cwd });
  assert.deepEqual(detailOf({ ...s, cwd: "", out_head: undefined }, NOW, AGO), { prev: "", ago: "2m", cwd: "" });
  const ans = { ...s, unread: true, owner_reply: 7, seen: 5, reply_head: "Opened the MR" };
  assert.equal(detailOf(ans, NOW, AGO).prev, "Opened the MR", "an unread answer to the owner: the answer, not a later result");
  assert.equal(detailOf({ ...ans, seen: 7 }, NOW, AGO).prev, "Fixed the flash on reload", "the answer read: the last result");
  assert.equal(detailOf({ ...ans, owner_out: 7 }, NOW, AGO).prev, "Fixed the flash on reload", "the answer is the last result: its own line");
  assert.deepEqual(detailOf({ ...s, kind: "hub" }, NOW, AGO), { prev: "Fixed the flash on reload", ago: "", cwd: "" });
  assert.equal(detailOf(null, NOW, AGO), null);
});

test("the second line: a working or waiting session shows the core caption, the rest — the start of the last result", () => {
  const s = { name: "flash", kind: "work", state: "idle", since: NOW - 120, out_head: "Fixed the flash", gist: { text: "gist" } };
  assert.equal(detailOf(s, NOW, AGO, "waiting: review of the branch").prev, "waiting: review of the branch");
  assert.equal(detailOf(s, NOW, AGO, "").prev, "Fixed the flash", "no caption given — as before");
  assert.equal(detailOf({ ...s, out_head: null, gist: null }, NOW, AGO, "Fixing **the feed**").prev, "Fixing the feed", "an executor without a result: the caption too");
  assert.equal(detailOf({ ...s, kind: "hub" }, NOW, AGO, "main session · working").prev, "main session · working");
  // liveLine: what the session says it does, else the core caption; "" for a session that does not work or wait itself
  const w = { name: "b", state: "busy", since: 100, status: "working", gist: { text: "Checking invoices", ts: 101 } };
  assert.equal(liveLine(w, "working · Billing"), "Checking invoices", "the gist of this turn");
  assert.equal(liveLine({ ...w, gist: { text: "old", ts: 99 } }, "working · Billing"), "working · Billing", "a gist of the previous turn: the caption");
  assert.equal(liveLine({ ...w, gist: null }, "working · Billing"), "working · Billing");
  assert.equal(liveLine({ ...w, state: "starting" }, "x"), "Checking invoices");
  assert.equal(liveLine({ ...w, status: "waiting" }, "waiting: a card"), "waiting: a card", "waits for the owner: the caption, not the gist");
  assert.equal(liveLine({ ...w, state: "idle", status: "idle", attention: { ask: "Deploy?" } }, "Deploy?"), "Deploy?",
    "the watchdog's \"reply?\": the action (the caption), not the result");
  assert.equal(liveLine({ ...w, state: "dialog" }, "waiting"), "waiting");
  assert.equal(liveLine({ name: "i", state: "idle", waiting: { text: "a reply" }, gist: { text: "g", ts: 5 }, since: 1 }, "waiting: a reply"), "waiting: a reply");
  assert.equal(liveLine({ name: "i", state: "idle", background: [{ id: "b1" }], since: 1 }, "running: build"), "running: build", "background work");
  assert.equal(liveLine({ name: "p", state: "idle", status: "idle", since: 1 }, "idle · executors: 1"), "", "a free parent that spins for its executors");
  assert.equal(liveLine({ name: "a", state: "asleep", waiting: { text: "a reply" } }, "x"), "", "asleep — the result, as the owner asked");
  assert.equal(liveLine(null, "x"), "");
  assert.equal(liveLine(w, ""), "Checking invoices");
});

test("the preview is regular sans in the preview token, one line with an ellipsis; the token keeps 4.5:1 or more", () => {
  const rule = /\[data-clean-prev\]::after \{ content: attr\(data-clean-prev\)[^}]*\}/.exec(css)?.[0] || "";
  assert.ok(rule, "the preview rule exists");
  assert.match(rule, /font: 400 var\(--c-fs-s\)\/var\(--c-lh-s\) var\(--font\)/);
  assert.match(rule, /color: var\(--c-preview\)/);
  assert.match(rule, /white-space: nowrap/);
  assert.match(rule, /text-overflow: ellipsis/);
  assert.doesNotMatch(rule, /mono|font-code/);
  // token: light 55%, dark 50%; contrast against the card (#fff / #1c1c1f) and the list (#f1f1f3 / #121214)
  const m = /--c-preview: light-dark\(rgb\(0 0 0 \/ ([\d.]+)\), rgb\(255 255 255 \/ ([\d.]+)\)\)/.exec(css);
  assert.ok(m, "token --c-preview exists");
  const lin = (c) => ((c /= 255) <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  const lum = (r, g, b) => 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
  const ratio = (a, b) => (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
  const on = (fg, a, bg) => fg.map((c, i) => c * a + bg[i] * (1 - a));
  for (const bg of [[255, 255, 255], [241, 241, 243]]) assert.ok(ratio(lum(...on([0, 0, 0], +m[1], bg)), lum(...bg)) >= 4.5, "light " + bg);
  for (const bg of [[28, 28, 31], [18, 18, 20]]) assert.ok(ratio(lum(...on([255, 255, 255], +m[2], bg)), lum(...bg)) >= 4.5, "dark " + bg);
});

test("the detailed rules live behind the setting class and use no bare :hover", () => {
  const rules = css.split("\n").filter((l) => l.includes("th-clean-detail"));
  assert.ok(rules.length > 5);
  for (const l of rules) {
    assert.match(l, /html\.th-clean\.th-clean-detail/, "every rule needs both classes: the theme and the setting");
    assert.doesNotMatch(l, /:hover/, "hover of a row stays the compact one, inside its media query");
  }
});

test("last message: the later of the state change and the last result for the owner", () => {
  assert.equal(lastAt({ since: 100, out_head_ts: 90 }), 100, "a turn the owner started or the moment it fell asleep");
  assert.equal(lastAt({ since: 100, out_head_ts: 130 }), 130, "a result that arrived while the state did not change");
  assert.equal(lastAt({ since: 100 }), 100);
  assert.equal(lastAt({}), 0);
  assert.equal(lastAt(null), 0);
});

test("sort by last message: the freshest chat on top, the hub stays first, ties keep the given order, the input is not touched", () => {
  const rows = [{ name: "a", since: 10 }, { name: "hub", kind: "hub", since: 1 }, { name: "b", since: 30 }, { name: "c", since: 5, out_head_ts: 30 }, { name: "d" }];
  const copy = rows.slice();
  assert.deepEqual(byRecent(rows).map((r) => r.name), ["hub", "b", "c", "a", "d"]);
  assert.deepEqual(rows, copy);
  assert.deepEqual(byRecent([]), []);
});

test("sort by last message: the option is off by default (owner, 10.11), the order goes through the core hook, nothing but the list is anchored", () => {
  assert.match(js, /key: "recent", on: false/);
  assert.match(js, /hub\.addOrder\(/);
  assert.match(css, /html\.th-clean\.th-clean-recent #list \{ overflow-anchor: none; \}/);
});

test("time in a row is the time the list is sorted by: a result that arrived without a state change is \"now\", not the old since", () => {
  assert.equal(agoText({ state: "idle", since: NOW - 20 * 86400, out_head_ts: NOW - 30 }, NOW), "now");
  assert.equal(agoText({ state: "idle", since: NOW - 3 * 3600, out_head_ts: NOW - 7200 }, NOW), "2h", "the later of the two wins");
  assert.equal(agoText({ state: "idle", since: NOW - 3600, out_head_ts: NOW - 7200 }, NOW), "1h", "an older result does not make it older");
  assert.equal(agoText({ state: "idle", since: NOW - 3 * 3600, out_head_ts: NOW - 60 }, NOW), "1m");
});
