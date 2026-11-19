// Clean while typing: the core rewrites the field height (style) and the form's class on every key. The theme must not make the
// browser restyle or lay out the whole page for that: no :has() over all of main or the aside, no [style] inside a :has(); the
// composer row (fit.js) is not measured again for a class written over with the same value; the rows of the list find their
// session by an index of the snapshot's sessions, not a scan per row. Browserless: rows.js and fit.js run on stubs, the rest
// is read as text.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const DIR = path.join(import.meta.dirname, "..", "..", "modules", "web", "static", "clean");
const css = fs.readFileSync(path.join(DIR, "clean.css"), "utf8");
const js = fs.readFileSync(path.join(DIR, "clean.js"), "utf8");
const { sessionFor, sessionIndex } = await import(pathToFileURL(path.join(DIR, "rows.js")).href);
const { mountFit } = await import(pathToFileURL(path.join(DIR, "fit.js")).href);

// the selectors of all rules, comments dropped
const selectors = css.replace(/\/\*[\s\S]*?\*\//g, "").split("}").map((r) => r.split("{")).filter((p) => p.length > 1).map((p) => p.at(-2).trim());

test("no :has() hangs on the whole main or aside: any change inside them would re-check it and restyle all of it", () => {
  const wide = selectors.filter((s) => /(^|[\s>~+(,])(main|aside)(\.[\w-]+)*:has\((?!\s*>)/.test(s));
  assert.deepEqual(wide, [], "a :has on main/aside looks only at a direct child (main:has(> .fsearch)), not deep into it");
});

test("no [style] inside a :has(): the core writes style on every key", () => {
  assert.deepEqual(selectors.filter((s) => /:has\([^{]*\[style/.test(s)), []);
  assert.match(css, /main > \.head > \.btn:not\(\.quiet, \.stop-exec\):has\(> svg\.icon > use\[href\$="#i-stop"\], > \.icon\.kit-img\) \{ display: none; \}/,
    "the header stop is still found: the core's icon or the theme's own (kit-img)");
});

test("the rules moved off main/aside keep their meaning: the dock, the search row", () => {
  assert.match(css, /html\.th-clean aside:has\(> :where\(\.search-row\) #filter:not\(:placeholder-shown\)\) #list \.g-tools \{ display: none; \}/);
  assert.match(css, /html\.th-clean main > \.btn\.jumpnew:has\(~ \.dock \.menu-btn\[aria-expanded="true"\]\) \{ opacity: 0;/);
  assert.match(css, /html\.th-clean main > \.feed:has\(~ \.dock > :where\(\.dock-inner\) > form\.composer\) \{ padding-bottom: 56px; \}/);
});

test("the index of the sessions finds every name exactly as sessionFor does", () => {
  const list = ["maps-dev", "maps-dev-api", "web-api", "hub-core", "a-b-c", "c", "x--y", "dup", "dup"].map((name, i) => ({ name, i }));
  const ix = sessionIndex(list);
  for (const name of ["maps-dev", "maps-dev-api", "api", "dev", "dev-api", "core", "b-c", "c", "y", "-y", "dup", "", "nope", "maps"]) {
    assert.equal(ix.find(name), sessionFor(name, list), name);
  }
  assert.equal(ix.byName.get("dup").i, 7, "the first of two sessions with one name, as Array.find");
  assert.equal(sessionIndex([]).find("x"), null);
});

test("the rows of the list ask the index, built once per sessions array, not a scan of all sessions per row", () => {
  assert.match(js, /import \{[^}]*\bsessionIndex\b[^}]*\} from "\.\/rows\.js"/);
  assert.match(js, /const indexes = new WeakMap\(\);/);
  assert.doesNotMatch(js, /sessions \|\| \[\]\)\.(find|map)\(/, "no per-call scan or map of the snapshot's sessions");
  assert.doesNotMatch(js, /\bsessionFor\(/);
});

// fit.js on stubs: one .dock-inner, observers that only record, frames that only queue
function stage() {
  const frames = [], observers = [];
  const attrs = { class: "composer" };
  const form = { getAttribute: (k) => attrs[k] ?? null };
  const dock = { classList: { remove() {}, add() {}, contains: () => false }, style: { setProperty() {}, removeProperty() {} }, dataset: {}, querySelector: () => null };
  globalThis.document = { querySelector: (s) => (s === ".dock-inner" ? dock : null), fonts: null };
  globalThis.requestAnimationFrame = (f) => { frames.push(f); return frames.length; };
  globalThis.ResizeObserver = class { observe() {} disconnect() {} };
  globalThis.MutationObserver = class {
    constructor(cb) { this.cb = cb; observers.push(this); }
    observe(_, opts) { this.opts = opts; }
    disconnect() {}
  };
  const fit = mountFit(() => true);
  fit.hook();
  frames.splice(0).forEach((f) => f());   // the first measure on hook (no CSS here: it just clears) — not what is checked
  return { frames, mo: observers.at(-1), form, attrs, dock };
}

test("fit.js: a class written over with the same value does not measure the row again; a real change does", (t) => {
  const keep = { document: globalThis.document, raf: globalThis.requestAnimationFrame, RO: globalThis.ResizeObserver, MO: globalThis.MutationObserver };
  t.after(() => Object.assign(globalThis, { document: keep.document, requestAnimationFrame: keep.raf, ResizeObserver: keep.RO, MutationObserver: keep.MO }));
  const { frames, mo, form, attrs } = stage();
  assert.equal(mo.opts.attributeOldValue, true, "the old value is recorded to compare with");
  mo.cb([{ type: "attributes", target: form, attributeName: "class", oldValue: "composer" }]);
  assert.equal(frames.length, 0, "the same class again: nothing to measure");
  attrs.class = "composer has-text";
  mo.cb([{ type: "attributes", target: form, attributeName: "class", oldValue: "composer" }]);
  assert.equal(frames.length, 1, "the class changed: the row is measured");
});
