// The ☰ menu and the header menus in Clean. The core's "Schedule" item carries one .tag node for a paused or an expired job,
// with no class for its state: clean.js navTags tells the two apart by the core's words (web.schedule.expired_tag) and marks
// the node, clean.css paints "paused" soft yellow and "expired" gray. Browserless: clean.js loads on stub nodes, a redraw
// runs the sweep over the rendered tags. The rest reads clean.css as text.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { pathToFileURL } from "node:url";
import path from "node:path";

const dir = path.join(import.meta.dirname, "..", "..", "modules", "web", "static", "clean");
const CLEAN = pathToFileURL(path.join(dir, "clean.js")).href;
const css = fs.readFileSync(path.join(dir, "clean.css"), "utf8").replace(/\r\n/g, "\n");   // a Windows checkout: CRLF
const rule = (sel) => {
  const out = [];
  for (let i = css.indexOf(sel + " {"); i >= 0; i = css.indexOf(sel + " {", i + 1)) {
    if (i === 0 || "\n ".includes(css[i - 1])) out.push(css.slice(i, css.indexOf("}", i)));
  }
  assert.ok(out.length, sel);
  return out.join("\n");
};

const CORE = { "web.schedule.paused": "paused", "web.schedule.expired_tag": "expired" };
const node = (tag = "div", o = {}) => ({
  tag, dataset: {}, style: {}, children: [], textContent: "",
  classList: { contains: () => false, toggle() {}, add() {}, remove() {} },
  querySelector: () => null, querySelectorAll: () => [], closest: () => null, matches: () => false,
  setAttribute() {}, removeAttribute() {}, append() {}, remove() {}, addEventListener() {},
  ...o,
});

async function scene(words) {
  const tags = words.map((w) => node("span", { textContent: w }));
  const root = node("html", { classList: { contains: (c) => c === "th-clean", toggle() {}, add() {}, remove() {} } });
  const doc = {
    documentElement: root, body: node("body"), activeElement: null, readyState: "complete", head: node("head"),
    querySelector: () => null, querySelectorAll: (q) => (q === "#nav-extra .srow.nav > .tag" ? tags : []),
    createElement: (x) => node(x), addEventListener() {}, getElementById: () => null,
  };
  const redraws = [];
  Object.assign(globalThis, {
    document: doc, localStorage: { getItem: () => null, setItem() {} },
    addEventListener: (type, fn) => { if (type === "hub-redraw") redraws.push(fn); }, removeEventListener() {},
    requestAnimationFrame: (f) => f(), innerWidth: 1440, innerHeight: 900,
    MutationObserver: class { observe() {} disconnect() {} }, ResizeObserver: class { observe() {} disconnect() {} },
    getComputedStyle: () => ({ getPropertyValue: () => "", display: "" }), matchMedia: () => ({ matches: false, addEventListener() {} }),
    setInterval: () => 0, CSS: { escape: (x) => x },
  });
  const t = (k) => (k in CORE ? CORE[k] : k);
  const mod = await import(CLEAN + "?s=" + Math.random());
  await mod.default({ addStyle() {}, addSlot() {}, addOptions() {}, addOrder() {}, option: () => false, el: (x) => node(x), t, snapshot: () => ({}), redraw() {}, locale: "en" });
  redraws.forEach((f) => f());
  return tags;
}

test("the Schedule tag in the ☰ menu is marked paused or ended by the core's words", async () => {
  const [paused, ended] = await scene([CORE["web.schedule.paused"], CORE["web.schedule.expired_tag"]]);
  assert.equal(paused.dataset.cleanTag, "paused");
  assert.equal(ended.dataset.cleanTag, "ended");
});

test("paused is a soft yellow badge, ended a gray one, both 12/16 gray words", () => {
  const tag = rule("html.th-clean .nav-pop .srow.nav > .tag:not(.soft)");
  assert.match(tag, /background: var\(--c-busy-bg\)/);
  assert.match(tag, /font: 400 var\(--c-fs-s\)\/var\(--c-lh-s\)/);
  assert.match(rule('html.th-clean .nav-pop .srow.nav > .tag[data-clean-tag="ended"]'), /background: var\(--c-fill\)/);
});

test("Settings in the ☰ menu: red dot and the words when there is no connection; ☰ itself does not turn blue", () => {
  assert.match(rule('html.th-clean #conn .glyph .dot[data-s="off"]'), /background: var\(--c-alert\)/);
  assert.match(rule('html.th-clean .nav-pop #conn:has(.dot[data-s="off"]) .lb'), /display: inline/);
  assert.match(rule('html.th-clean .navmenu[aria-current="true"]'), /color: var\(--c-t1\)/);
});

test("header menus: the stops audit on a soft red plate, an open More on the 5% fill, a paused schedule chip soft yellow", () => {
  assert.match(rule("html.th-clean .menu .menu-text.bad"), /background: var\(--c-alert-soft\)/);
  assert.match(rule('html.th-clean main > .head .menu-wrap > .menu-btn[aria-expanded="true"]'), /background: var\(--c-fill\)/);
  assert.match(rule("html.th-clean main > .head .chip.sched.warn"), /background: var\(--c-busy-bg\)/);
  assert.match(rule('html.th-clean main > .head:has(> .sched-title) .pill[data-s="dialog"]'), /background: var\(--c-fill\)/);
});

test("the / commands menu is 240 wide, its list scrolls inside and its hint wraps", () => {
  assert.match(rule("html.th-clean .cmd-menu"), /width: min\(240px/);
  assert.match(rule("html.th-clean .cmd-menu .cmd-rows"), /max-height: 164px/);
  assert.match(rule("html.th-clean .cmd-foot"), /white-space: normal/);
});

test("hover rules of these menus are only for a real pointer, and insets are logical", () => {
  for (const sel of [".navmenu:hover", ".cmd-row:hover", ".chip.sched.warn:hover"]) {
    const i = css.indexOf(sel + " {");
    assert.ok(i > 0, sel);
    assert.match(css.slice(0, i), /@media \(hover: hover\) and \(pointer: fine\) \{\n {2}html\.th-clean [^\n]*$/, sel);
  }
  assert.doesNotMatch(rule("html.th-clean main > .head .menu-wrap > .menu-btn.alert"), /(^|[\s;{])(left|right):/);
});
