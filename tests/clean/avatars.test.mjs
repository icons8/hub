// "Chat avatars" of the session list (Settings -> Look): on by default, two sizes by level (not by type), directions are the
// same size as specialists. A row shows a character (clean/faces.js): of its icon, else of whoever raised it, else one by the
// name; an emoji of the owner's own stays an emoji; the Hub without an icon and the "Hub"/"Core" bylines show the hub's
// icon; a direction is a compass tile.
// Browserless: the rules of clean.css are read as text, and clean.js runs on stub nodes for rowFaces and the bylines.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const DIR = path.join(import.meta.dirname, "..", "..", "modules", "web", "static", "clean");
const CLEAN = pathToFileURL(path.join(DIR, "clean.js")).href;
const css = fs.readFileSync(path.join(DIR, "clean.css"), "utf8"), js = fs.readFileSync(path.join(DIR, "clean.js"), "utf8");
const block = css.slice(css.indexOf('"Chat avatars"'));
const rules = block.split("\n").filter((l) => /^html\./.test(l));

test("the setting is on by default and behind the theme class", () => {
  assert.ok(css.indexOf('"Chat avatars"') > 0, "the section of the setting is in clean.css");
  assert.ok(rules.length > 10);
  assert.match(js, /key: "avatars", on: true/);
  for (const l of rules) assert.match(l, /^html\.th-clean\./, "a rule outside the theme class: " + l);
});

test("avatar sizes: two per level, 16/12 in the compact list, 32/24 in the detailed one", () => {
  const sizes = (re) => rules.filter((l) => re.test(l)).map((l) => /--av:\s*(\d+)px/.exec(l)?.[1]).filter(Boolean);
  assert.deepEqual(sizes(/^html\.th-clean\.th-clean-av #list (\.forks )?\.srow\[data-clean-k\] \{/), ["16", "12"]);
  assert.deepEqual(sizes(/th-clean-detail\.th-clean-av #list (\.forks )?\.srow\[data-clean-k\] \{/), ["32", "24"]);
});

test("a direction takes the size of the level, not a size of its own", () => {
  assert.ok(!rules.some((l) => /data-clean-k="dir"\][^{]*\{[^}]*--av:/.test(l)));
});

test("the core's avatar node is a transparent click area over the picture for the icon picker (not for directions)", () => {
  assert.match(block, /\.srow\[data-clean-k\] \.nm > \.avatar \{ display: block; z-index: 1; background: none !important; cursor: pointer;\s*font-size: calc\(var\(--av\) \* 0\.7\);/);
  assert.ok(rules.some((l) => /\.srow:is\(\[style\*="--clean-face"\], \[data-clean-face\]\) \.nm > \.avatar \{ font-size: 0; color: transparent;/.test(l)), "over a character the emoji is not seen");
  assert.ok(rules.some((l) => /data-clean-k="dir"\] \.nm > \.avatar \{ display: none/.test(l)));
  // the frame and the emoji share one box, placed from the line start
  assert.match(block, /\.srow\[data-clean-k\] > \.txt::before, html\.th-clean\.th-clean-av #list \.srow\[data-clean-k\] \.nm > \.avatar \{\s*[^}]*inset-inline-start: var\(--av-left\)/);
  assert.match(block, /\.srow\[data-clean-k\] > \.txt \{[^}]*padding-inline-start: calc\(var\(--av\) \+ var\(--av-gap\) \+ var\(--av-left\)\)/);
});

test("the frame shows the row's character, --clean-face", () => {
  assert.match(block, /\.srow\[data-clean-k\] > \.txt::before \{ background: var\(--clean-face, var\(--c-fill\)\) center \/ cover;/);
});

test("the Hub is a first-level row: the size of a specialist, the same ring, the hub's icon when it has none of its own", () => {
  assert.ok(!rules.some((l) => /data-clean-k="hub"\][^{]*\{[^}]*--av:/.test(l)));
  assert.match(js, /hubRow \? "hub"/);
  // the ring is drawn by the picture rule that every typed row shares (.txt::before)
  assert.match(block, /\.srow\[data-clean-k\] > \.txt::before \{[^}]*box-shadow: inset 0 0 0 1px/);
  // without an icon of its own the Hub shows the hub's icon, not an empty tile; the emoji node under it is not seen
  assert.ok(rules.some((l) => /\.srow\[data-clean-face\] > \.txt::before \{ background-image: url\("\/static\/favicon\/tab\.svg"\); \}/.test(l)));
  assert.ok(rules.some((l) => /\.srow:is\(\[style\*="--clean-face"\], \[data-clean-face\]\) \.nm > \.avatar \{ font-size: 0; color: transparent; \}/.test(l)));
  assert.match(js, /put\(r, "cleanFace", hubRow && !s\.icon \? "1" : ""\)/);
});

test("the characters are png files of the theme (an older page server serves png, not jpg), every one of them is there", async () => {
  const { FACES, PICTURES, DIR: WEB, bare } = await import(pathToFileURL(path.join(DIR, "faces.js")).href);
  assert.equal(WEB, "/static/clean/a/");
  const files = fs.readdirSync(path.join(DIR, "a")).sort();
  assert.equal(files.length, 24);
  assert.ok(files.every((f) => f.endsWith(".png")), "png only");
  assert.deepEqual(PICTURES.map((u) => u.slice(WEB.length)).sort(), files, "the ordered list is every picture, once");
  assert.deepEqual([...Object.values(FACES)].sort(), files, "one emoji to a picture");
  for (const e of Object.keys(FACES)) assert.equal(e, bare(e), "the keys are the bare form: " + e);
  const server = fs.readFileSync(path.join(DIR, "..", "..", "server.py"), "utf8");
  assert.doesNotMatch(/STATIC_TYPES = \{[^}]*\}/.exec(server)[0], /\.jpg/, "the page server needs no new type for them");
});

test("the character of an emoji does not depend on U+FE0F; the page's theme kit has both forms", async () => {
  const { faceOf, kitFaces, bare } = await import(pathToFileURL(path.join(DIR, "faces.js")).href);
  for (const e of ["\u{1F6E0}", "\u{1F6E0}\uFE0F", "\u2699", "\u2699\uFE0F", "\u270D\uFE0F", "\u{1F58C}"]) assert.ok(faceOf(e), e);
  assert.equal(faceOf("\u{1F6E0}\uFE0F"), faceOf("\u{1F6E0}"));
  const kit = kitFaces();
  for (const e of ["\u{1F6E0}", "\u{1F6E0}\uFE0F", "\u2699", "\u2699\uFE0F"]) assert.equal(kit[e], faceOf(e), e);
  assert.equal(bare("\u2699\uFE0F"), "\u2699");
  assert.equal(faceOf("\u{1F4E1}"), "", "an emoji with no character of its own (the antenna shared one with the ant)");
  assert.equal(new Set(Object.values(kit)).size, 24);
});

test("characterOf: the icon's character, else the raiser's, else one by the name; an emoji of one's own stays an emoji", async () => {
  const { characterOf, faceByName, faceOf } = await import(pathToFileURL(path.join(DIR, "faces.js")).href);
  const by = (list) => new Map(list.map((s) => [s.name, s]));
  const list = [{ name: "w", icon: "\u{1F3A8}" }, { name: "w-fix", raised_by: "w" }, { name: "w-fix-2", raised_by: "w-fix" },
    { name: "fox", icon: "\u{1F98A}" }, { name: "fox-fix", raised_by: "fox" }, { name: "plain" }, { name: "plain-fix", raised_by: "plain" },
    { name: "a", raised_by: "b" }, { name: "b", raised_by: "a" }];
  const m = by(list), of = (n) => characterOf(m.get(n), m);
  assert.equal(of("w"), faceOf("\u{1F3A8}"));
  assert.equal(of("w-fix"), faceOf("\u{1F3A8}"), "an executor takes the raiser's character");
  assert.equal(of("w-fix-2"), faceOf("\u{1F3A8}"), "down the chain");
  assert.equal(of("fox"), "", "an emoji of one's own is not replaced by a character");
  assert.equal(of("fox-fix"), "", "nor the executor's of whoever raised it");
  assert.equal(of("plain"), faceByName("plain"));
  assert.equal(of("plain-fix"), faceByName("plain"), "no icon down the chain: one by the name of the last in it");
  assert.equal(typeof of("a"), "string", "a cycle ends");
});

// a picture request answered as the script says: true — loaded, false — failed
const images = (outcomes) => {
  const asked = [];
  globalThis.Image = class { set src(v) { asked.push(v); const ok = outcomes.shift(); setTimeout(() => (ok ? this.onload() : this.onerror()), 0); } };
  return asked;
};
// made-up time: the timers set while it runs go off in their order at once, nothing is waited for (a slow machine — winstand —
// let the real ones run past the end of a test, and a 100 s ask of a missing picture held node --test to its time limit)
const later = (ms, run) => {
  const real = globalThis.setTimeout;
  const due = [];
  let now = 0;
  globalThis.setTimeout = (fn, wait = 0) => { due.push({ at: now + wait, fn }); return due.length; };
  try {
    run();
    for (;;) {
      due.sort((a, b) => a.at - b.at);
      if (!due.length || due[0].at > ms) break;
      const t = due.shift();
      now = t.at;
      t.fn();
    }
  } finally { globalThis.setTimeout = real; delete globalThis.Image; }
};

test("the characters did not load: no picture is drawn, the emoji and the letters stay", async () => {
  const f = await import(pathToFileURL(path.join(DIR, "faces.js")).href + "?missing");
  assert.ok(f.loaded() && f.faceOf("\u{1F3A8}") && f.faceByName("x") && Object.keys(f.kitFaces()).length);
  const asked = images([false, false, false, false]);
  let told = 0;
  later(60, () => f.probe(() => told++, [5, 5], 100000));   // the next ask of a missing picture, at 100 s, is not waited for
  assert.equal(asked.length, 3, "asked again twice before it is believed");
  assert.equal(told, 1);
  assert.equal(f.loaded(), false);
  assert.equal(f.faceOf("\u{1F3A8}"), "");
  assert.equal(f.faceByName("x"), "");
  assert.deepEqual(f.kitFaces(), {});
  assert.equal(f.characterOf({ name: "x" }, new Map()), "");
});

test("one failed request does not take the characters away for the life of the page", async () => {
  const f = await import(pathToFileURL(path.join(DIR, "faces.js")).href + "?blip");
  const asked = images([false, true]);
  let told = 0;
  later(60, () => f.probe(() => told++, [5, 5], 100000));
  assert.equal(asked.length, 2);
  assert.equal(told, 0, "the page was never told they were gone");
  assert.equal(f.loaded(), true);
  assert.ok(f.faceOf("\u{1F3A8}"));
});

test("the characters come back when a picture loads after they were missing", async () => {
  const f = await import(pathToFileURL(path.join(DIR, "faces.js")).href + "?back");
  const asked = images([false, false, false, false, true]);
  let told = 0;
  later(150, () => f.probe(() => told++, [5, 5], 20));
  assert.equal(asked.length, 5, "it keeps asking while they are missing");
  assert.equal(told, 2, "gone, then back");
  assert.equal(f.loaded(), true);
  assert.ok(f.faceOf("\u{1F3A8}") && Object.keys(f.kitFaces()).length);
});

test("the Hub and Core bylines in the feed show the hub's icon, the other authors a character", () => {
  assert.match(css, /\.byline:is\(\[data-clean="hub"\], \[data-clean="core"\]\) \.av \{ background: url\("\/static\/favicon\/tab\.svg"\) center \/ cover; \}/);
  assert.match(css, /\.byline \.av\[data-clean-av\] \{ background: var\(--clean-av\) center \/ cover; \}/);
  assert.ok(!fs.existsSync(path.join(DIR, "avatar-hub.png")) && !fs.existsSync(path.join(DIR, "avatar-core.png")), "the hub has one icon");
});

test("the icon picker: characters first, large; the search, the owner's own emoji field and the rest of the page's list stay", () => {
  assert.match(css, /\.gpop\.palette > \.pal-grid \{ grid-template-columns: repeat\(4, minmax\(0, 1fr\)\);[^}]*scrollbar-gutter: stable;/);   // room for a classic scrollbar
  // nothing of the core's picker is hidden: the emoji without a character are in the grid, the own field is under it
  assert.doesNotMatch(css, /\.pal-own\)? \{ display: none/);
  assert.doesNotMatch(css, /\.pal-find\) \{ display: none/);
  assert.doesNotMatch(css, /pal-e:not\(:has\(> \.kit-img\)\) \{ display: none/);
  assert.match(css, /\.gpop\.palette > \.gpop-t \{ display: none; \}/);
  const themes = fs.readFileSync(path.join(DIR, "..", "js", "themes.js"), "utf8");
  assert.match(themes, /kit\.avatars = \{ \.\.\.kitFaces\(\),/);   // kit.avatars of the built-in Clean
});

test("the old 24px hub picture is only for the setting that is off; the row height does not hang on our attributes", () => {
  const old = css.split("\n").filter((l) => /sgroup\.bare/.test(l) && /\.avatar|data-clean-d\]:has\(\.nm > \.avatar\)/.test(l));
  assert.ok(old.length >= 3);
  for (const l of old) assert.match(l, /:not\(\.th-clean-av\)/, l);
  // that picture is the hub's icon too
  assert.match(css, /html\.th-clean:not\(\.th-clean-av\) \.list \.sgroup\.bare \.srow \.nm > \.avatar \{[^}]*background: url\("\/static\/favicon\/tab\.svg"\)/);
  // the core erases our attributes on every snapshot: the Hub row must keep its 44 without data-clean-k, or the scroll is cut (keepList)
  assert.ok(rules.some((l) => /\.sgroup\.bare \.srow:not\(\[data-clean-d\], \.forks \.srow\) \{ min-height: 44px/.test(l)));
});

test("a direction is a tile with the compass", () => {
  assert.match(block, /\.srow\[data-clean-tile\] \.nm::before \{[^}]*mask: url\("compass\.svg"\) center \/ contain no-repeat;/);
  assert.match(block, /\.srow\[data-clean-tile\] \.nm::before \{[^}]*inset-inline-start:/);
  assert.ok(fs.existsSync(path.join(DIR, "compass.svg")));
  assert.match(js, /put\(r, "cleanTile"/);
});

test("the list observer watches every attribute rowFaces puts (the core erases them on every snapshot)", () => {
  const filter = /attributeFilter: \[([^\]]*)\]/.exec(js.slice(js.indexOf("const keepList")))?.[1] || "";
  for (const a of ["data-clean-k", "data-clean-tile", "style"]) assert.ok(filter.includes(`"${a}"`), a);   // --clean-face is in style
});

// --- rowFaces and the bylines on stub nodes ---

const node = (tag = "div", o = {}) => ({
  tag, dataset: {}, children: [], attrs: {}, textContent: "",
  style: { props: {}, setProperty(k, v) { this.props[k] = v; }, getPropertyValue(k) { return this.props[k] || ""; }, removeProperty(k) { delete this.props[k]; } },
  classList: { contains: () => false, toggle() {}, add() {}, remove() {} },
  querySelector: () => null, querySelectorAll: () => [], closest: () => null, matches: () => false,
  setAttribute() {}, removeAttribute() {}, append() {}, remove() {}, addEventListener() {}, hasAttribute: () => false,
  ...o,
});

// A list row as the core draws it: .txt > .nm with the name as a text node, the emoji node .nm > .avatar when the session has an icon.
const row = (name, { emoji = null, fork = false } = {}) => {
  const txt = node("div");
  const avatar = emoji ? node("button", { textContent: emoji }) : null;
  const nm = node("span", { childNodes: [...(avatar ? [avatar] : []), { nodeType: 3, nodeValue: name }] });
  return node("div", {
    txt, querySelector: (q) => (q === ":scope > .txt" ? txt : q === ".nm" ? nm : q === ".nm > .avatar" ? avatar : null),
    closest: (q) => (q === "li.forks" && fork ? {} : null),
  });
};

const byline = (who) => {
  const b = node("b", { textContent: who });
  return node("div", { querySelector: (q) => (q === ":scope > b" ? b : null) });
};

async function scene({ sessions, rows, bylines = [], avs = [], hubRow = null, avatars = true }) {
  const cls = new Set(["th-clean"]);
  const root = node("html", { classList: { contains: (c) => cls.has(c), toggle: (c, on) => (on ? cls.add(c) : cls.delete(c)), add: (c) => cls.add(c), remove: (c) => cls.delete(c) } });
  const doc = {
    documentElement: root, body: node("body"), activeElement: null, readyState: "complete", head: node("head"),
    querySelector: (q) => (q === "#list .sgroup.bare .srow" ? hubRow : null),
    querySelectorAll: (q) => (q === "#list .srow:not(.nav, .queued)" ? rows : q === ".byline:not([data-clean])" ? bylines.filter((b) => !("clean" in b.dataset)) : q === ".byline .av" ? avs : []),
    getElementById: () => null, createElement: (x) => node(x), addEventListener() {},
  };
  Object.assign(globalThis, {
    document: doc, localStorage: { getItem: () => null, setItem() {} },
    addEventListener() {}, removeEventListener() {}, requestAnimationFrame: (f) => f(), innerWidth: 1440, innerHeight: 900,
    MutationObserver: class { observe() {} disconnect() {} }, ResizeObserver: class { observe() {} disconnect() {} },
    getComputedStyle: () => ({ getPropertyValue: () => "" }), matchMedia: () => ({ matches: false, addEventListener() {} }),
    setInterval: () => 0, CSS: { escape: (x) => x },
  });
  const words = { "web.who.hub": "Hub", "web.who.core": "Core", "web.who.owner": "Owner" };
  const mod = await import(CLEAN + "?s=" + Math.random());
  await mod.default({
    addStyle() {}, addSlot() {}, addOptions() {}, addOrder() {}, option: (k) => k === "avatars" && avatars, el: (x) => node(x),
    t: (k) => words[k] ?? k, snapshot: () => ({ sessions, directions: ["roadmap"] }), redraw() {}, locale: "en",
  });
  return { cls };
}

const SESSIONS = [
  { name: "hub", kind: "hub" },
  { name: "writer", role: "agent", icon: "\u{1F3A8}" },
  { name: "tester", role: "agent" },
  { name: "roadmap", icon: "\u{1F9ED}" },
  { name: "writer-fix", raised_by: "writer-x" },
  { name: "émile" },
];

const face = (r) => r.style.props["--clean-face"];
const pic = (f) => `url("/static/clean/a/${f}")`;

test("rowFaces: the row type and the picture from the snapshot", async () => {
  const { faceByName } = await import(pathToFileURL(path.join(DIR, "faces.js")).href);
  const hubRow = row("Hub");
  const writer = row("writer", { emoji: "\u{1F3A8}" }), tester = row("tester"), dir = row("roadmap", { emoji: "\u{1F9ED}" });
  const exec = row("fix", { fork: true }), other = row("émile"), fox = row("fox", { emoji: "\u{1F98A}" }), unknown = row("nobody");
  const sessions = [...SESSIONS, { name: "fox", icon: "\u{1F98A}" }];
  await scene({ sessions, rows: [hubRow, writer, tester, dir, exec, other, fox, unknown], hubRow });

  assert.equal(hubRow.dataset.cleanK, "hub", "the first row is the hub, by its own label");
  assert.equal(face(hubRow), undefined, "a hub without an icon: the hub's icon (CSS, data-clean-face)");
  assert.equal(hubRow.dataset.cleanFace, "1");
  assert.equal(writer.dataset.cleanK, "agent");
  assert.equal(face(writer), pic("fuzzy-lime-star.png"), "the character of the session's icon");
  assert.equal(tester.dataset.cleanK, "agent");
  assert.equal(face(tester), `url("${faceByName("tester")}")`, "no icon: a character by the name");
  assert.equal(dir.dataset.cleanK, "dir");
  assert.equal(dir.dataset.cleanTile, "1");
  assert.equal(face(dir), undefined, "a direction is a tile, not a character");
  assert.equal(exec.dataset.cleanK, "exec", "an executor under its parent, found by its short name");
  assert.equal(face(exec), `url("${faceByName("writer-fix")}")`, "no icon and its raiser is gone: by its own full name");
  assert.equal(other.dataset.cleanK, "other");
  assert.equal(face(fox), undefined, "an emoji of one's own with no character stays an emoji: no picture on the row");
  assert.equal(fox.txt.dataset.cleanLetter, undefined);
  assert.equal(unknown.dataset.cleanK, undefined, "a row with no session in the snapshot gets nothing");
  for (const r of [writer, tester, exec, other]) assert.equal(r.dataset.cleanTile, undefined);
});

test("rowFaces: an executor without an icon shows the character of whoever raised it", async () => {
  const exec = row("fix", { fork: true });
  await scene({ sessions: [{ name: "writer", role: "agent", icon: "\u{1F3A8}" }, { name: "writer-fix", raised_by: "writer" }], rows: [exec] });
  assert.equal(face(exec), pic("fuzzy-lime-star.png"));
});

test("rowFaces: a hub with a character of its own shows it, not the hub's picture", async () => {
  const hubRow = row("Hub", { emoji: "\u{1F916}" });
  await scene({ sessions: [{ name: "hub", kind: "hub", icon: "\u{1F916}" }], rows: [hubRow], hubRow });
  assert.equal(hubRow.dataset.cleanK, "hub");
  assert.equal(face(hubRow), pic("clay-terracotta-stack.png"));
});

test("rowFaces: with the setting off no row is typed", async () => {
  const tester = row("tester");
  const s = await scene({ sessions: SESSIONS, rows: [tester], avatars: false });
  assert.equal(s.cls.has("th-clean-av"), false);
  assert.equal(tester.dataset.cleanK, undefined);
  assert.equal(face(tester), undefined);
});

test("bylines: Hub and Core are marked for the hub's icon, the owner for the capital, others stay plain", async () => {
  const [h, c, o, w] = [byline("Hub"), byline("core"), byline("owner"), byline("writer")];
  await scene({ sessions: SESSIONS, rows: [], bylines: [h, c, o, w] });
  assert.equal(h.dataset.clean, "hub");
  assert.equal(c.dataset.clean, "core");
  assert.equal(o.dataset.clean, "");
  assert.equal(o.dataset.cleanOwner, "");
  assert.equal(w.dataset.clean, "");
  assert.equal(w.dataset.cleanOwner, undefined);
});

test("rowFaces: an emoji of one's own stays an emoji, in the row of a hub, of its executor too", async () => {
  const fox = row("fox", { emoji: "\u{1F98A}" }), foxFix = row("fix", { fork: true }), hubRow = row("Hub", { emoji: "\u{1F4E6}" });
  await scene({ sessions: [{ name: "hub", kind: "hub", icon: "\u{1F4E6}" }, { name: "fox", role: "agent", icon: "\u{1F98A}" },
    { name: "fox-fix", raised_by: "fox" }], rows: [hubRow, fox, foxFix], hubRow });
  for (const r of [hubRow, fox, foxFix]) assert.equal(face(r), undefined, r.dataset.cleanK);
  assert.equal(fox.txt.dataset.cleanLetter, undefined, "its emoji is in the frame, not a letter");
  assert.equal(foxFix.txt.dataset.cleanLetter, "F", "an executor of it has no node of its own: the letter shows under no picture");
});

test("rowFaces: a row follows the icon and the raiser of the next snapshot", async () => {
  const exec = row("fix", { fork: true });
  const was = [{ name: "w", role: "agent", icon: "\u{1F3A8}" }, { name: "w-fix", raised_by: "w" }];
  await scene({ sessions: was, rows: [exec] });
  assert.equal(face(exec), pic("fuzzy-lime-star.png"));
  await scene({ sessions: [{ name: "w", role: "agent", icon: "\u{1F98A}" }, was[1]], rows: [exec] });
  assert.equal(face(exec), undefined, "the raiser took an emoji of its own: so did the executor");
});

// A byline as the core draws it: .byline > .av (+ .emoji for a session icon, + .kit-img when the theme has its picture) and b
const avOf = (who, { cls = [], clean = "" } = {}) => {
  const b = node("b", { textContent: who }), line = node("div", { dataset: { clean }, querySelector: (q) => (q === ":scope > b" ? b : null) });
  const set = new Set(cls);
  return node("span", { classList: { contains: (c) => set.has(c), toggle() {}, add() {}, remove() {} }, closest: (q) => (q === ".byline" ? line : null) });
};
const av = (n) => n.dataset.cleanAv;

test("bylines: a letter gets a character by the name, an emoji of one's own and the theme's picture are left alone", async () => {
  const { faceByName, faceOf } = await import(pathToFileURL(path.join(DIR, "faces.js")).href);
  const letter = avOf("tester"), own = avOf("fox", { cls: ["emoji"] }), kit = avOf("writer", { cls: ["emoji", "kit-img"] });
  const hubAv = avOf("Hub", { clean: "hub" }), exec = avOf("writer-exec");
  await scene({ sessions: [...SESSIONS, { name: "fox", icon: "\u{1F98A}" }, { name: "writer-exec", raised_by: "writer" }],
    rows: [], avs: [letter, own, kit, hubAv, exec] });
  assert.equal(av(letter), faceByName("tester"));
  assert.equal(letter.style.props["--clean-av"], `url("${faceByName("tester")}")`);
  assert.equal(av(own), undefined, "an emoji of one's own stays");
  assert.equal(av(kit), undefined, "the core drew the character itself");
  assert.equal(av(hubAv), undefined, "Hub has the picture of its own (CSS)");
  assert.equal(av(exec), faceOf("\u{1F3A8}"), "an executor shows its raiser's character, as in the list");
});

test("bylines: the picture follows the author and the icon the node is kept for (the core keeps nodes between snapshots)", async () => {
  const { faceByName } = await import(pathToFileURL(path.join(DIR, "faces.js")).href);
  const n = avOf("tester");
  await scene({ sessions: SESSIONS, rows: [], avs: [n] });
  assert.equal(av(n), faceByName("tester"));
  // the same node, another author
  n.closest(".byline").querySelector(":scope > b").textContent = "émile";
  await scene({ sessions: SESSIONS, rows: [], avs: [n] });
  assert.equal(av(n), faceByName("émile"));
  assert.equal(n.style.props["--clean-av"], `url("${faceByName("émile")}")`);
  // the same node, the author took an emoji of their own: the picture goes
  n.closest(".byline").querySelector(":scope > b").textContent = "fox";
  await scene({ sessions: [{ name: "fox", icon: "\u{1F98A}" }], rows: [], avs: [n] });
  assert.equal(av(n), undefined);
});
