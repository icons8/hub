// Voice recording keys in clean.js: Enter stops and sends, a double Esc cancels, a single Esc leaves the recording alone.
// No core or browser: clean.js loads on document stubs, and the key presses go into the one keydown listener on window
// (capture phase), the way the core would see them after the theme.
import test from "node:test";
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import path from "node:path";

const CLEAN = pathToFileURL(path.join(import.meta.dirname, "..", "..", "modules", "web", "static", "clean", "clean.js")).href;

// A node with the methods clean.js needs at load and in the key listener.
const node = (tag = "div", o = {}) => ({
  tag, dataset: {}, style: {}, children: [], attrs: {},
  classList: { contains: () => false, toggle() {}, add() {}, remove() {} },
  querySelector: () => null, querySelectorAll: () => [], closest: () => null, matches: () => false,
  setAttribute() {}, removeAttribute() {}, append() {}, remove() {}, addEventListener() {},
  ...o,
});

// The core's record strip as voice.js recStrip() builds it: the same children and classes, no button text.
// recording: dot, clock [data-rec-clock], spacer, round cancel x; hold: plus a hint before x; opening the mic: spinner,
// label, spacer, x; recognising (busy): spinner and label only.
const el = (tag, cls = "", attrs = {}) => ({ tag, cls: cls.split(" ").filter(Boolean), attrs });
const STRIPS = {
  recording: () => [el("span", "rec-dot"), el("span", "t", { "data-rec-clock": "1" }), el("span", "spacer"), el("button", "btn round quiet rec-cancel")],
  hold: () => [el("span", "rec-dot"), el("span", "t", { "data-rec-clock": "1" }), el("span", "spacer"), el("span", "hint"), el("button", "btn round quiet rec-cancel")],
  opening: () => [el("span", "spin-dot"), el("span", "t"), el("span", "spacer"), el("button", "btn round quiet rec-cancel")],
  busy: () => [el("span", "spin-dot"), el("span", "t")],
};
// A compound selector (tag, .class, [attr], [attr="v"]) and comma lists: enough for the queries inside the strip.
const matches = (n, sel) => sel.split(",").some((one) => {
  const parts = one.trim().match(/^[a-z]+|\.[\w-]+|\[[^\]]+\]/g) || [];
  if (parts.join("") !== one.trim()) throw new Error("unsupported selector in strip: " + one);
  return parts.every((p) => {
    if (p[0] === ".") return n.cls.includes(p.slice(1));
    if (p[0] === "[") { const [k, v] = p.slice(1, -1).split("="); return k in n.attrs && (v === undefined || n.attrs[k] === v.replace(/"/g, "")); }
    return n.tag === p;
  });
});

// The scene: whether a recording is on, which field has the focus, how many times stop was pressed.
async function scene({ theme = true, rec = "recording", focus = null, stopButton = true } = {}) {
  const s = { stops: 0, listeners: [], theme };
  const kids = rec ? STRIPS[rec]() : [];
  const strip = node("div", { querySelector: (q) => kids.find((k) => matches(k, q)) || null });
  const stop = node("button", { click: () => { s.stops++; } });
  const body = node("body");
  const root = node("html", { classList: { contains: (c) => c === "th-clean" && s.theme, toggle() {}, add() {}, remove() {} } });
  s.setFocus = (f) => { doc.activeElement = f; };
  const doc = {
    documentElement: root, body, activeElement: focus || body, readyState: "complete",
    querySelector: (q) => (q === "form.composer.rec .recstrip" ? (rec ? strip : null) : q === "form.composer.rec .bar > .btn.round.stop" ? (stopButton && rec && rec !== "busy" ? stop : null) : null),
    querySelectorAll: () => [], getElementById: () => null, createElement: (t) => node(t), addEventListener() {},
    head: node("head"),
  };
  Object.assign(globalThis, {
    document: doc, localStorage: { getItem: () => null, setItem() {} },
    addEventListener: (type, fn, opt) => { s.listeners.push({ type, fn, opt }); },
    removeEventListener() {}, requestAnimationFrame: (f) => f(), innerWidth: 1440, innerHeight: 900,
    MutationObserver: class { observe() {} disconnect() {} }, ResizeObserver: class { observe() {} disconnect() {} },
    getComputedStyle: () => ({ getPropertyValue: () => "" }), matchMedia: () => ({ matches: false, addEventListener() {} }),
    setInterval: () => 0, CSS: { escape: (x) => x },
  });
  const mod = await import(CLEAN + "?s=" + Math.random());
  await mod.default({ addStyle() {}, addSlot() {}, addOptions() {}, addOrder() {}, option: () => false, el: (t) => node(t), t: (k) => k, snapshot: () => ({}), redraw() {}, locale: "en" });
  const keys = s.listeners.filter((l) => l.type === "keydown");
  s.keys = keys;
  // a key press: returns what the theme did with it
  let t = 1000;
  s.press = (key, o = {}) => {
    t += o.after ?? 1000;
    const e = { key, timeStamp: t, repeat: false, isComposing: false, shiftKey: false, ctrlKey: false, metaKey: false, altKey: false, ...o,
      defaultPrevented: false, stopped: false, preventDefault() { this.defaultPrevented = true; }, stopImmediatePropagation() { this.stopped = true; } };
    for (const l of keys) l.fn(e);
    return e;
  };
  return s;
}

test("one permanent key listener, in the capture phase", async () => {
  const s = await scene();
  assert.equal(s.keys.length, 1);
  assert.equal(s.keys[0].opt, true);
});

test("Enter while recording presses stop once and swallows the key", async () => {
  const s = await scene();
  const e = s.press("Enter");
  assert.equal(s.stops, 1);
  assert.ok(e.defaultPrevented && e.stopped);
  const again = s.press("Enter", { after: 100 });   // a repeat while the strip has not changed yet
  assert.equal(s.stops, 1);
  assert.ok(!again.defaultPrevented);
});

test("Enter outside a recording, without the theme, while the mic opens and while recognising does nothing", async () => {
  for (const o of [{ rec: null }, { theme: false }, { rec: "opening" }, { rec: "busy" }]) {
    const s = await scene(o);
    const e = s.press("Enter");
    assert.equal(s.stops, 0, JSON.stringify(o));
    assert.ok(!e.defaultPrevented && !e.stopped, JSON.stringify(o));
  }
});

test("Enter with a modifier, a repeat or in another field is left alone", async () => {
  const s = await scene();
  for (const o of [{ shiftKey: true }, { ctrlKey: true }, { metaKey: true }, { altKey: true }, { repeat: true }, { isComposing: true }]) {
    const e = s.press("Enter", o);
    assert.ok(!e.defaultPrevented, JSON.stringify(o));
  }
  const other = node("input", { matches: (q) => q.includes("input") });
  s.setFocus(other);
  assert.ok(!s.press("Enter").defaultPrevented);
  const dlg = node("button", { matches: () => true, closest: (q) => (q.includes("dialog") ? {} : null) });
  s.setFocus(dlg);
  assert.ok(!s.press("Enter").defaultPrevented);
  assert.equal(s.stops, 0);
});

test("Enter in the conversation's input field and on the stop button works", async () => {
  for (const tag of ["textarea", "button"]) {
    const s = await scene();
    s.setFocus(node(tag, { matches: () => true }));   // matches both "a field or button" and "ours": the composer and the recording stop
    assert.ok(s.press("Enter").defaultPrevented, tag);
    assert.equal(s.stops, 1, tag);
  }
});

test("a single Esc does not reach the core, a pause between Escs resets the pair", async () => {
  const s = await scene();
  const a = s.press("Escape");
  assert.ok(a.stopped && !a.defaultPrevented);   // swallowed without preventDefault
  const b = s.press("Escape", { after: 1000 });
  assert.ok(b.stopped, "a second later: a single one again");
  const c = s.press("Escape", { after: 600 });
  assert.ok(c.stopped, "0.6 s later: not a pair");
});

test("a double Esc within 0.4 s passes the second Esc to the core; a third Esc right after is swallowed", async () => {
  const s = await scene();
  assert.ok(s.press("Escape").stopped);
  const second = s.press("Escape", { after: 300 });
  assert.ok(!second.stopped && !second.defaultPrevented, "the core gets the second Esc and cancels");
  const third = s.press("Escape", { after: 100 });
  assert.ok(third.stopped, "the cancel has gone out, the strip has not changed yet");
  const later = s.press("Escape", { after: 1500 });   // a new recording
  assert.ok(later.stopped);
  assert.ok(!s.press("Escape", { after: 200 }).stopped, "after a pause the pair works again");
});

test("holding Esc is not a second press", async () => {
  const s = await scene();
  s.press("Escape");
  const held = s.press("Escape", { after: 100, repeat: true });
  assert.ok(held.stopped);
});

test("Esc outside a recording, without the theme and while recognising goes to the core and the pages as before", async () => {
  for (const o of [{ rec: null }, { theme: false }, { rec: "busy" }]) {
    const s = await scene(o);
    const e = s.press("Escape");
    assert.ok(!e.stopped && !e.defaultPrevented, JSON.stringify(o));
    const e2 = s.press("Escape", { after: 100 });
    assert.ok(!e2.stopped, JSON.stringify(o));
  }
});

test("a single Esc is swallowed while the mic opens and while the record button is held", async () => {
  for (const rec of ["opening", "hold"]) {
    const s = await scene({ rec });
    assert.ok(s.press("Escape").stopped, rec);
    assert.ok(!s.press("Escape", { after: 200 }).stopped, rec + ": the second Esc goes to the core");
  }
});

test("Enter while the record button is held stops and sends", async () => {
  const s = await scene({ rec: "hold" });
  assert.ok(s.press("Enter").defaultPrevented);
  assert.equal(s.stops, 1);
});
