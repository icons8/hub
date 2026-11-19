// Settings tabs (clean.js tabMarks): the core adds " ·" to a tab's name when there is something to know there ("Modules ·",
// "Plugins ·"). Clean shows it as a small dot in the warning color: the name stays a text node, the " ·" moves into
// span.clean-att (clean.css draws the dot and hides the sign; a screen reader still reads it). A tab without the mark is left
// alone, a second sweep does not add a second dot, and turning the theme off gives the core's text back.
// Browserless: clean.js loads on stub nodes, the sweep runs on hub-redraw.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import path from "node:path";

const DIR = path.join(import.meta.dirname, "..", "..", "modules", "web", "static", "clean");
const CLEAN = pathToFileURL(path.join(DIR, "clean.js")).href;

const text = (data) => ({ nodeType: 3, data, get textContent() { return this.data; } });
const span = () => {
  const s = { nodeType: 1, className: "", textContent: "", parent: null };
  s.replaceWith = (x) => { const p = s.parent, i = p.childNodes.indexOf(s); p.childNodes[i] = typeof x === "string" ? text(x) : x; };
  return s;
};
const tab = (label) => {
  const b = { nodeType: 1, childNodes: [text(label)] };
  Object.defineProperty(b, "firstChild", { get: () => b.childNodes[0] });
  Object.defineProperty(b, "textContent", { get: () => b.childNodes.map((n) => n.textContent).join("") });
  b.replaceChildren = (...xs) => { b.childNodes = xs; for (const x of xs) x.parent = b; };
  return b;
};

const node = (o = {}) => ({
  dataset: {}, style: {}, children: [], textContent: "",
  classList: { contains: () => false, toggle() {}, add() {}, remove() {} },
  querySelector: () => null, querySelectorAll: () => [], closest: () => null, matches: () => false,
  setAttribute() {}, removeAttribute() {}, append() {}, remove() {}, addEventListener() {},
  ...o,
});

async function scene(labels) {
  const tabs = labels.map(tab);
  let on = true;
  const root = node({ classList: { contains: (c) => c === "th-clean" && on, toggle() {}, add() {}, remove() {} } });
  const marks = () => tabs.flatMap((b) => b.childNodes.filter((n) => n.className === "clean-att"));
  const doc = {
    documentElement: root, body: node(), activeElement: null, readyState: "complete", head: node(),
    querySelector: () => null, getElementById: () => null, addEventListener() {},
    querySelectorAll: (q) => (q === "[data-settings-tab]" ? tabs : q === "[data-settings-tab] .clean-att" ? marks() : []),
    createElement: (tag) => (tag === "span" ? span() : node()),
    createTextNode: (d) => text(d),
  };
  const redraws = [], themes = [];
  Object.assign(globalThis, {
    document: doc, localStorage: { getItem: () => null, setItem() {} },
    addEventListener: (type, fn) => { if (type === "hub-redraw") redraws.push(fn); if (type === "hub-theme") themes.push(fn); }, removeEventListener() {},
    requestAnimationFrame: (f) => f(), innerWidth: 1440, innerHeight: 900,
    MutationObserver: class { observe() {} disconnect() {} },
    ResizeObserver: class { observe() {} disconnect() {} },
    getComputedStyle: () => ({ getPropertyValue: () => "" }), matchMedia: () => ({ matches: false, addEventListener() {} }),
    setInterval: () => 0, CSS: { escape: (x) => x },
  });
  const mod = await import(CLEAN + "?s=" + Math.random());
  await mod.default({ addStyle() {}, addSlot() {}, addOptions() {}, addOrder() {}, option: () => false, el: () => node(), t: (k) => k,
    snapshot: () => ({}), redraw() {}, locale: "en" });
  return {
    tabs,
    sweep: () => redraws.forEach((f) => f()),
    off: () => { on = false; themes.forEach((f) => f()); },
  };
}

test("a tab with the core's \" ·\" gets the name as text and the sign in span.clean-att", async () => {
  const s = await scene(["General", "Modules ·", "Plugins ·"]);
  s.sweep();
  assert.equal(s.tabs[0].childNodes.length, 1, "a tab without the mark is left alone");
  assert.equal(s.tabs[0].textContent, "General");
  for (const [b, name] of [[s.tabs[1], "Modules"], [s.tabs[2], "Plugins"]]) {
    assert.equal(b.childNodes.length, 2);
    assert.equal(b.childNodes[0].data, name);
    assert.equal(b.childNodes[1].className, "clean-att");
    assert.equal(b.textContent, name + " ·", "the sign stays readable");
  }
});

test("a second sweep keeps one dot", async () => {
  const s = await scene(["Modules ·"]);
  s.sweep(); s.sweep();
  assert.equal(s.tabs[0].childNodes.filter((n) => n.className === "clean-att").length, 1);
});

test("turning the theme off gives the core's text back", async () => {
  const s = await scene(["Modules ·"]);
  s.sweep();
  s.off();
  assert.equal(s.tabs[0].textContent, "Modules ·");
  assert.ok(!s.tabs[0].childNodes.some((n) => n.className === "clean-att"));
});

test("clean.css draws the dot in the warning color and hides the sign", () => {
  const css = readFileSync(path.join(DIR, "clean.css"), "utf8");
  const rule = /html\.th-clean \[data-settings-tab\] \.clean-att \{([^}]*)\}/.exec(css);
  assert.ok(rule, "a rule for the tab mark");
  assert.match(rule[1], /background: var\(--warning\)/);
  assert.match(rule[1], /font-size: 0/);
  assert.match(rule[1], /border-radius: 50%/);
});
