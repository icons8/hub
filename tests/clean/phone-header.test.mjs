// The session header on a phone (Clean): one row, and what does not fit goes into the "More" sheet. clean.js headFold gives the
// sheet a title (the session name) and turns the core's "Stop the executor" button, hidden in the row, into an item that clicks
// it; a disabled button ("Stopping…") becomes a disabled item. Browserless: clean.js loads on stub nodes and a redraw runs the
// sweep over a rendered header. The layout itself is read from clean.css as text.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { pathToFileURL } from "node:url";
import path from "node:path";

const dir = path.join(import.meta.dirname, "..", "..", "modules", "web", "static", "clean");
const CLEAN = pathToFileURL(path.join(dir, "clean.js")).href;
const css = fs.readFileSync(path.join(dir, "clean.css"), "utf8");
const rule = (sel) => {
  const out = [];
  for (let i = css.indexOf(sel + " {"); i >= 0; i = css.indexOf(sel + " {", i + 1)) {
    if (i === 0 || "\n ".includes(css[i - 1])) out.push(css.slice(i, css.indexOf("}", i)));
  }
  assert.ok(out.length, sel);
  return out.join("\n");
};

const node = (tag = "div", o = {}) => {
  const n = {
    tag, dataset: {}, style: {}, attrs: {}, kids: [], textContent: "", hidden: false, listeners: [],
    classList: { contains: () => false, toggle() {}, add() {}, remove() {} },
    querySelector: () => null, querySelectorAll: () => [], closest: () => null, matches: () => false,
    setAttribute(k, v) { this.attrs[k] = String(v); }, removeAttribute(k) { delete this.attrs[k]; },
    append(...xs) { this.kids.push(...xs); }, prepend(...xs) { this.kids.unshift(...xs); },
    after(...xs) { this.parent.kids.splice(this.parent.kids.indexOf(this) + 1, 0, ...xs); },
    remove() {}, addEventListener(type, fn) { if (type === "click") this.listeners.push(fn); },
    click() { for (const fn of this.listeners) fn({}); },
    ...o,
  };
  return n;
};

async function scene({ name = "hub-dev", stop = "Stop the executor", disabled = false, hidden = true } = {}) {
  const sep = node("div", { className: "menu-sep" });
  const core = node("button", { className: "menu-item", textContent: "Put to sleep" });
  const menu = node("div");
  menu.kids.push(core, sep);
  sep.parent = menu;
  menu.querySelector = (q) => (q === ".clean-fold-i" ? menu.kids.find((k) => /clean-fold-i/.test(k.className || "")) || null
    : q === ":scope > .menu-sep.head-fold-view" ? sep : null);
  let clicked = 0;
  const stopExec = stop == null ? null : node("button", { className: "btn danger stop-exec", textContent: stop, title: "Stops the executor", disabled, click: () => { clicked++; } });
  const h2 = node("h2", { textContent: ` ${name} ` });
  const head = node("div", {
    querySelector: (q) => ({ ".menu-wrap:not(.clean-tabs) > .menu": menu, "h2": h2, ":scope > .btn.stop-exec": stopExec })[q] ?? null,
  });
  const root = node("html", { classList: { contains: (c) => c === "th-clean", toggle() {}, add() {}, remove() {} } });
  const doc = {
    documentElement: root, body: node("body"), activeElement: null, readyState: "complete", head: node("head"),
    querySelector: (q) => (q === "main > .head" ? head : null), querySelectorAll: () => [],
    createElement: (x) => node(x), addEventListener() {}, getElementById: () => null,
  };
  const redraws = [];
  Object.assign(globalThis, {
    document: doc, localStorage: { getItem: () => null, setItem() {} },
    addEventListener: (type, fn) => { if (type === "hub-redraw") redraws.push(fn); }, removeEventListener() {},
    dispatchEvent() {}, KeyboardEvent: class { constructor(type, o) { Object.assign(this, { type }, o); } },
    requestAnimationFrame: (f) => f(), innerWidth: 390, innerHeight: 844,
    MutationObserver: class { observe() {} disconnect() {} }, ResizeObserver: class { observe() {} disconnect() {} },
    getComputedStyle: (n) => ({ getPropertyValue: () => "", display: n === stopExec && hidden ? "none" : "block" }),
    matchMedia: () => ({ matches: true, addEventListener() {} }), setInterval: () => 0, CSS: { escape: (x) => x },
  });
  globalThis.window = globalThis;
  const el = (tag, cls, text) => node(tag, { className: cls || "", textContent: text || "" });
  const mod = await import(CLEAN + "?s=" + Math.random());
  await mod.default({ addStyle() {}, addSlot() {}, addOptions() {}, addOrder() {}, option: () => false, el, t: (k) => k, snapshot: () => ({}), redraw() {}, locale: "en" });
  const sweep = () => redraws.forEach((f) => f());
  sweep();
  return { menu, sep, core, sweep, clicks: () => clicked };
}

test("the More sheet is titled with the session name", async () => {
  const s = await scene({ name: "hub-dev" });
  assert.equal(s.menu.dataset.cleanTitle, "hub-dev");
});

test("a hidden Stop the executor becomes an item after the core's separator, and its click clicks the core's button", async () => {
  const s = await scene();
  const i = s.menu.kids.indexOf(s.sep);
  const item = s.menu.kids[i + 1];
  assert.equal(item.textContent, "Stop the executor");
  assert.match(item.className, /\bmenu-item\b/);
  assert.equal(item.attrs.role, "menuitem");
  item.click();
  assert.equal(s.clicks(), 1);
  s.sweep();
  assert.equal(s.menu.kids.filter((k) => k.textContent === "Stop the executor").length, 1, "one item, not one per sweep");
});

test("a disabled Stop (Stopping…) is a disabled item that does nothing", async () => {
  const s = await scene({ stop: "Stopping…", disabled: true });
  const item = s.menu.kids.find((k) => k.textContent === "Stopping…");
  assert.equal(item.attrs["aria-disabled"], "true");
  item.click();
  assert.equal(s.clicks(), 0);
});

test("a Stop that is shown in the row is not copied into the menu", async () => {
  const s = await scene({ hidden: false });
  assert.ok(!s.menu.kids.some((k) => k.textContent === "Stop the executor"));
});

test("on a phone the header is one 52 row, the switches and Stop the executor hide only when More is there", () => {
  const head = rule("  html.th-clean main > .head");
  assert.match(head, /flex-wrap: nowrap; min-height: 52px/);
  // not by the Settings burger's own wrapper (.clean-tabs is a menu-wrap too): it would hide the section bar the fold measures
  assert.match(css, /html\.th-clean main > \.head:has\(> \.menu-wrap:not\(\.clean-tabs\)\) > :is\(\.seg, \.btn\.stop-exec\) \{ display: none; \}/);
  // the Settings header keeps its container: the section bar folds into ☰ by it (a bar that does not fold sticks out of a phone's width)
  assert.match(css, /html\.th-clean main > \.head:not\(:has\(> \.seg\.tabs\)\) \{ container: none; \}/);
  // no @container on a phone (container: none), so the core's fold items are shown by this block itself
  assert.match(rule("  html.th-clean main > .head .menu :is(.head-fold-view, .head-fold-board)"), /display: block/);
  assert.match(rule("  html.th-clean main > .head h2"), /min-width: min\(8ch, max-content\)/);
  assert.match(rule("  html.th-clean main > .head > .pill"), /text-overflow: ellipsis/);
});

test("on a phone menus are sheets from the bottom with a title and 44 items", () => {
  assert.match(rule("  html.th-clean :is(.gpop:not(.palette), main > .head .menu-wrap > .menu)"), /position: fixed; inset: auto 0 0 0 !important/);
  assert.match(rule("  html.th-clean main > .head .menu-wrap > .menu[data-clean-title]::before"), /content: attr\(data-clean-title\)/);
  assert.match(rule("  html.th-clean :is(.gpop-i, main > .head .menu-wrap > .menu .menu-item)"), /min-height: 44px/);
});

test("on a phone feed search takes the header row; Latest is a white 40 button", () => {
  assert.match(rule("  html.th-clean main > .fsearch"), /grid-row: 3/);
  assert.match(rule("  html.th-clean main:has(> .fsearch) > .head"), /visibility: hidden/);
  assert.match(rule("  html.th-clean main > .btn.jumpnew:not(.round)"), /width: 40px; height: 40px/);
});

test("feed search on every width is a gray 36 r8 field with the icon, the count, the arrows and × inside", () => {
  // top-level rules (no indent): the computer gets the same field as the phone, not the core's bordered input next to an outside icon
  const top = (sel) => { const i = css.indexOf("\n" + sel + " {"); assert.ok(i >= 0, sel); return css.slice(i, css.indexOf("}", i)); };
  assert.match(top("html.th-clean main > .fsearch .fs-row::before"), /border-radius: 8px; background: var\(--c-fill\)/);
  assert.match(top("html.th-clean main > .fsearch .fs-row"), /height: 36px/);
  assert.match(top("html.th-clean main > .fsearch .fs-row input"), /border: 0;.*background: none/);
  assert.match(top("html.th-clean main > .fsearch .fs-close"), /grid-column: 7/);
  // focus as the left panel's search: a white field with a darker thin border, not a gray line over the gray field
  assert.match(top("html.th-clean main > .fsearch .fs-row:has(input:focus)::before"), /background: var\(--background\); box-shadow: inset 0 0 0 1px var\(--c-line-strong\)/);
  // the row ends ↑ ↓ × word: the arrows are the 4th and 3rd from the end (counting 3 and 2 put "↑" outside the grid as text)
  assert.match(css, /\.fs-btn:nth-last-child\(4\) \{ grid-column: 5; \}/);
  assert.match(css, /\.fs-btn:nth-last-child\(3\) \{ grid-column: 6; \}/);
  assert.match(rule("  html.th-clean main > .fsearch .fs-cancel"), /width: auto/);
});

test("the phone rules use logical insets and keep the back arrow mirrored right-to-left", () => {
  const start = css.indexOf("/* ---- the header on a phone, as in apps");
  assert.ok(start > 0);
  assert.doesNotMatch(css.slice(start), /(^|[\s;{])(left|right|margin-left|margin-right|padding-left|padding-right):/m);
  assert.match(css, /html\[dir="rtl"\]\.th-clean main > \.head > \.btn\.back::before \{ transform: none; \}/);
});
