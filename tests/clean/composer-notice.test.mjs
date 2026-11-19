// The composer and the plates above it in Clean. clean.js: the composer's message (p.error-text) is an error by default, a line
// that only asks to wait for an upload or says the microphone is allowed gets data-clean-kind "wait" / "ok" (told by the core's
// own strings); "Unread" and "Not delivered" rows and a "Not removed" queue row move onto the queue plate, the last two as errors;
// the feed-end fade variable is written on .dock, not on main (the whole feed inherited it). clean.css: the message is a plate in
// the field, the "To:" line is gone. Browserless: clean.js loads on stub nodes, the sweep runs on a "hub-redraw".
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const root = path.join(import.meta.dirname, "..", "..", "modules", "web", "static", "clean");
const CLEAN = pathToFileURL(path.join(root, "clean.js")).href;
const css = fs.readFileSync(path.join(root, "clean.css"), "utf8");
const CAT = {
  "web.composer.wait_upload": "Wait for the files to upload",
  "web.voice.mic_allowed_hold": "Microphone allowed. Hold the button to record",
  "web.strip.not_removed": "Not removed: {text}",
};

const node = (tag = "div", o = {}) => ({
  tag, dataset: {}, style: {}, children: [], attrs: {}, textContent: "",
  classList: { contains: () => false, toggle() {}, add() {}, remove() {} },
  querySelector: () => null, querySelectorAll: () => [], closest: () => null, matches: () => false,
  setAttribute() {}, removeAttribute() {}, append() {}, remove() {}, addEventListener() {},
  ...o,
});
const styleOf = () => { const v = {}; return { v, getPropertyValue: (k) => v[k] ?? "", setProperty: (k, x) => { v[k] = x; } }; };

async function scene({ notices = [], rows = [] } = {}) {
  const ps = notices.map((text) => node("p", { textContent: text }));
  const strip = rows.map(([k, text]) => node("div", { dataset: { k }, querySelector: (q) => (q === ":scope > .t" ? { textContent: text } : null) }));
  const moved = [];
  let plate = null;
  const host = node("div", {
    querySelectorAll: (q) => (q.startsWith(":scope > .strip > .strip-row:is(") ? strip.filter((r) => q.includes(`[data-k=${r.dataset.k}]`)) : []),
    querySelector: (q) => (q === ":scope > .clean-queue" ? plate : null),
    append: (x) => { plate = x; },
  });
  const dock = node("div", { style: styleOf() }), main = node("main", { style: styleOf(), querySelector: (q) => (q === ":scope > .dock" ? dock : null) });
  const feed = node("div", { parentElement: main, scrollHeight: 1000, clientHeight: 400, scrollTop: 600 });
  const rootEl = node("html", { classList: { contains: (c) => c === "th-clean", toggle() {}, add() {}, remove() {} } });
  const doc = {
    documentElement: rootEl, body: node("body"), activeElement: null, readyState: "complete", head: node("head"),
    querySelector: (q) => (q === "main > .feed" ? feed : null),
    querySelectorAll: (q) => (q === "form.composer > p.error-text" ? ps : q === ".strip-host" ? [host] : []),
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
  const t = (k) => CAT[k] ?? k;
  const el = (x, cls) => node(x, { className: cls, append: (r) => moved.push(r) });
  const mod = await import(CLEAN + "?s=" + Math.random());
  await mod.default({ addStyle() {}, addSlot() {}, addOptions() {}, addOrder() {}, option: () => false, el, t, snapshot: () => ({}), redraw() {}, locale: "en" });
  redraws.forEach((f) => f());
  return { ps, strip, moved, dock, main };
}

test("the composer's message: an error by default, wait and ok by the core's own words", async () => {
  const { ps } = await scene({ notices: ["Could not send: no connection", CAT["web.composer.wait_upload"] + ".", CAT["web.voice.mic_allowed_hold"], ""] });
  assert.deepEqual(ps.map((p) => p.dataset.cleanKind || ""), ["", "wait", "ok", ""]);
});

test("Unread and Not delivered rows go onto the queue plate; Not delivered and Not removed are errors", async () => {
  const { strip, moved } = await scene({ rows: [["unread", "Step runs longer than a minute"], ["fail", "Not delivered: hi"], ["queue", "Not removed: hi"], ["queue", "Plain"]] });
  assert.equal(new Set(moved).size, 4);
  assert.deepEqual(strip.map((r) => r.dataset.clean), ["unread", "fail", "fail", "state"]);
});

test("the feed-end fade variable lives on .dock, not on main", async () => {
  const { dock, main } = await scene();
  assert.equal(dock.style.v["--clean-end"], "0");
  assert.equal(main.style.v["--clean-end"], undefined);
});

test("the message is a plate in the field, not a card over it; a send r8", () => {
  assert.match(css, /form\.composer > p\.error-text:not\(:empty\) \{ order: -1; position: static;[^}]*background: var\(--c-alert-soft\)/);
  assert.match(css, /p\.error-text:is\(\[data-clean-kind="wait"\], \[data-clean-kind="ok"\]\) \{ background: var\(--c-fill\); \}/);
  // the "To" line stays (the core's cross on its chip takes the addressee off — a decision recorded in the page's own checks)
  assert.doesNotMatch(css, /\.composer \.to-line \{ display: none/);
  assert.match(css, /p\.error-text::before \{[^}]*mask: url\("failed\.svg"\)/);   // one error glyph in the theme
  assert.ok(fs.existsSync(path.join(root, "failed.svg")));
});
