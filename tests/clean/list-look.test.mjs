// The session list in Clean: the executors badge carries no number unless "Counts in the list" is on (a 16 square with the
// chevron in its middle) and no dots ("waiting" is a soft red plate, "new" the badge at full strength); the group count, when
// shown, is a quiet gray badge; the watchdog's "reply?" is a quiet badge with a cross; a long group title gets an ellipsis; the
// drop line of a dragged row is in the text color; the "External hubs" section has its own gap and a compass tile per row; a
// phone shows no list scroll bar; the ☰ menu opened with a pointer shows no focus ring. clean.css and clean.js are read as text.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

const DIR = path.join(import.meta.dirname, "..", "..", "modules", "web", "static", "clean");
const css = fs.readFileSync(path.join(DIR, "clean.css"), "utf8").replace(/\r\n/g, "\n");   // a Windows checkout: CRLF
const js = fs.readFileSync(path.join(DIR, "clean.js"), "utf8").replace(/\r\n/g, "\n");
const bare = css.replace(/\/\*[\s\S]*?\*\//g, "");
// the top-level selectors of a selector list (a comma inside :is() / :has() does not split)
const parts = (list) => {
  const out = [];
  let depth = 0, from = 0;
  for (let i = 0; i < list.length; i++) {
    if (list[i] === "(") depth++;
    else if (list[i] === ")") depth--;
    else if (list[i] === "," && !depth) { out.push(list.slice(from, i)); from = i + 1; }
  }
  return [...out, list.slice(from)].map((x) => x.trim());
};
const rules = (sel) => [...bare.matchAll(/(^|\n)\s*([^{}\n@][^{}]*)\{([^}]*)\}/g)]
  .filter((m) => parts(m[2]).includes(sel)).map((m) => m[3]).join("\n");
const OFF = "html.th-clean:not(.th-clean-counts)";

test("the executors badge: no number by default, the twin is empty; with the option the number is back", () => {
  assert.match(rules(OFF + " .fork-toggle .n"), /display: none/);
  assert.match(rules("html.th-clean li.has-forks > .srow::after"), /content: ""/);
  assert.match(rules("html.th-clean.th-clean-counts li.has-forks > .srow::after"), /content: attr\(data-clean-fw\)/);
  assert.match(js, /const forkCounts = \(\) => \{\n    if \(!document\.documentElement\.classList\.contains\("th-clean-counts"\)\) return;/);
});

test("without the number the chevron sits in the middle of a 16 square", () => {
  assert.match(rules(OFF + " .fork-toggle"), /padding: 0 3px; justify-content: center/);
  assert.match(rules(OFF + " .fork-toggle .chev"), /translate: -\.33px 0/);
  assert.match(rules(OFF + ' .fork-toggle .chev[data-down="1"]'), /translate: 0 -\.33px/);
});

test("the executors badge has no dots: waiting is a soft red plate, new is the badge at full strength", () => {
  assert.match(rules("html.th-clean .fork-toggle .fdot"), /display: none/);
  const wants = rules("html.th-clean .fork-toggle:is(.wants, :has(.fdot.wants))");
  assert.match(wants, /background: var\(--c-alert-soft\)/);
  assert.match(wants, /color: var\(--destructive\)/);
  assert.match(rules("html.th-clean .fork-toggle:has(.fdot:not(.wants))"), /opacity: 1/);
  assert.match(css, /@media \(hover: hover\) and \(pointer: fine\) \{\n  html\.th-clean \.fork-toggle:is\(\.wants, :has\(\.fdot\.wants\)\):hover \{ background: var\(--c-alert-soft-hover\); \}/);
});

test("the group count (with the option) is a quiet gray badge", () => {
  const g = rules("html.th-clean .gcount");
  assert.match(g, /background: var\(--c-fill\)/);
  assert.match(g, /color: var\(--c-t3\)/);
  assert.doesNotMatch(g, /opacity/);
});

test("the \"reply?\" tag is a quiet badge with a cross that takes it off", () => {
  const t = rules("html.th-clean .srow > .tag.soft");
  assert.match(t, /height: calc\(16px \* var\(--c-rel\)\)/);
  assert.match(t, /background: var\(--c-badge\)/);
  assert.match(t, /border: 0/);
  assert.match(rules("html.th-clean .srow > .tag.soft.dismiss::after"), /mask: url\("close\.svg"\)/);
  assert.match(css, /@media \(hover: hover\) and \(pointer: fine\) \{\n  html\.th-clean \.srow > \.tag\.soft\.dismiss:hover \{ color: var\(--c-t1\); text-decoration: none; \}/);
});

test("a long group title gets an ellipsis; the drop line is in the text color", () => {
  assert.match(rules("html.th-clean .sgroup-h .gt"), /text-overflow: ellipsis/);
  assert.match(rules("html.th-clean .drop-line"), /background: var\(--foreground\)/);
  assert.match(rules("html.th-clean .sgroup.drop-target"), /var\(--c-line-strong\)/);
});

test("\"External hubs\": a section 24 apart, a compass tile on each row", () => {
  assert.match(rules("html.th-clean #list > .bus-group"), /margin-top: 24px/);
  assert.match(rules("html.th-clean.th-clean-av #list .bus-srow .nm::before"), /url\("compass\.svg"\)/);
  assert.ok(fs.existsSync(path.join(DIR, "compass.svg")));
});

test("a phone shows no list scroll bar; a pointer-opened ☰ shows no focus ring until a key", () => {
  assert.match(css, /@media \(max-width: 760px\) \{\n  html\.th-clean \.app > aside #list \{ scrollbar-width: none; \}\n  html\.th-clean \.app > aside #list::-webkit-scrollbar \{ display: none; \}/);
  assert.match(rules("html.th-clean #nav-extra[data-clean-ptr] :focus-visible"), /outline: 0/);
  assert.match(js, /document\.addEventListener\("pointerdown", navPtr, true\);/);
  assert.match(js, /document\.addEventListener\("keydown", navPtr, true\);/);
});
