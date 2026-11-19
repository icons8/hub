// The board's own line in place of the board (boards.js: "Loading the board…" or the hub's error): Clean draws it as a title with a 24
// mark above it, three dots while loading and the error mark otherwise. clean.js boardHint tells the two apart by the core's own
// string (web.boards.loading) and sets data-clean-kind="error" on any other line. Browserless: clean.js loads on stub nodes, the
// sweep runs on a "hub-redraw", and the CSS is read as text.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const root = path.join(import.meta.dirname, "..", "..", "modules", "web", "static", "clean");
const CLEAN = pathToFileURL(path.join(root, "clean.js")).href;
const css = fs.readFileSync(path.join(root, "clean.css"), "utf8");
const LOADING = "Loading the board…";
const HINTS = ".boards-body:not(:has(> .board)) > p.hint";

const node = (tag = "div", o = {}) => ({
  tag, dataset: {}, style: {}, children: [], attrs: {}, textContent: "",
  classList: { contains: () => false, toggle() {}, add() {}, remove() {} },
  querySelector: () => null, querySelectorAll: () => [], closest: () => null, matches: () => false,
  setAttribute() {}, removeAttribute() {}, append() {}, remove() {}, addEventListener() {},
  ...o,
});

async function scene(lines) {
  const hints = lines.map((text) => node("p", { textContent: text }));
  const t = (k) => (k === "web.boards.loading" ? LOADING : k);
  const rootEl = node("html", { classList: { contains: (c) => c === "th-clean", toggle() {}, add() {}, remove() {} } });
  const doc = {
    documentElement: rootEl, body: node("body"), activeElement: null, readyState: "complete", head: node("head"),
    querySelector: () => null, querySelectorAll: (q) => (q === HINTS ? hints : []),
    createElement: (x) => node(x), addEventListener() {}, getElementById: () => null,
  };
  const redraws = [];
  Object.assign(globalThis, {
    document: doc, localStorage: { getItem: () => null, setItem() {} },
    addEventListener: (type, fn) => { if (type === "hub-redraw") redraws.push(fn); }, removeEventListener() {},
    requestAnimationFrame: (f) => f(), innerWidth: 1440, innerHeight: 900,
    MutationObserver: class { observe() {} disconnect() {} }, ResizeObserver: class { observe() {} disconnect() {} },
    getComputedStyle: () => ({ getPropertyValue: () => "" }), matchMedia: () => ({ matches: false, addEventListener() {} }),
    setInterval: () => 0, CSS: { escape: (x) => x },
  });
  const mod = await import(CLEAN + "?s=" + Math.random());
  await mod.default({ addStyle() {}, addSlot() {}, addOptions() {}, addOrder() {}, option: () => false, el: (x) => node(x), t, snapshot: () => ({}), redraw() {}, locale: "en" });
  redraws.forEach((f) => f());
  return hints;
}

test("the loading line is not an error; any other line is", async () => {
  const [loading, error] = await scene([LOADING, "No board at boards/x.json"]);
  assert.equal(loading.dataset.cleanKind || "", "");
  assert.equal(error.dataset.cleanKind, "error");
});

test("the line is drawn as a title with a 24 mark: three dots by default, the error mark on an error", () => {
  const sel = "html.th-clean .boards-body:not(:has(> .board)) > p.hint";
  assert.match(css, new RegExp(sel.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "::before \\{[^}]*url\\(\"more\\.svg\"\\)"));
  assert.match(css, /p\.hint\[data-clean-kind="error"\]::before \{[^}]*url\("failed\.svg"\)/);
  assert.ok(fs.existsSync(path.join(root, "failed.svg")));
});
